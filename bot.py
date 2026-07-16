#!/usr/bin/env python3
"""
YouTube -> Bluesky reposter.

Polls a channel's public RSS feed (no API key, no quota), and posts any new
videos to Bluesky as a link card with the real thumbnail attached.

Env vars required:
    BSKY_HANDLE        e.g. bbcarchivebot.bsky.social  (or a custom domain handle)
    BSKY_APP_PASSWORD  app password from Settings -> Privacy and security
    YT_CHANNEL_ID      e.g. UCxxxxxxxxxxxxxxxxxxxxxx

Optional:
    STATE_FILE         default: seen.json
    POST_TEMPLATE      default: "{title}"   placeholders: {title} {url} {channel}
    MAX_BACKFILL       default: 3   (on first run, how many recent videos to post)
    DRY_RUN            set to "1" to print instead of posting
"""

import html
import io
import json
import os
import re
import sys
import time
import xml.etree.ElementTree as ET
from pathlib import Path

import requests
from PIL import Image
from atproto import Client, models

FEED_URL = "https://www.youtube.com/feeds/videos.xml?channel_id={}"
NS = {
    "atom": "http://www.w3.org/2005/Atom",
    "yt": "http://www.youtube.com/xml/schemas/2015",
    "media": "http://search.yahoo.com/mrss/",
}

# Bluesky rejects blobs over ~976 KB. Leave headroom.
MAX_BLOB_BYTES = 900_000
POST_LIMIT = 300  # graphemes; we approximate with characters


# --------------------------------------------------------------------------
# Title cleaning
# --------------------------------------------------------------------------

# Junk that commonly gets bolted onto YouTube titles. Order matters a little:
# trailing separators are stripped after the parenthetical noise is gone.
NOISE_PATTERNS = [
    # (Official Video), [Official Music Video], (Official Lyric Video) etc.
    r"[\(\[\{]\s*official\s+(music\s+|lyric\s+|audio\s+)?(video|audio|visualiser|visualizer)\s*[\)\]\}]",
    # (Official Trailer), [Trailer]
    r"[\(\[\{]\s*(official\s+)?trailer\s*[\)\]\}]",
    # quality / format tags: [4K], (HD), [1080p], (60fps), [Full HD]
    r"[\(\[\{]\s*(full\s+)?(4k|8k|hd|uhd|1080p?|720p?|60\s*fps)\s*[\)\]\}]",
    # (Part 1 of 3) is meaningful, but (Ep. 12) style suffixes usually aren't —
    # comment this line out if you want episode numbers kept.
    # r"[\(\[\{]\s*(ep|episode)\.?\s*\d+\s*[\)\]\}]",
    # engagement bait
    r"[\(\[\{]\s*(must\s+watch|you\s+won'?t\s+believe|shocking|viral)[^\)\]\}]*[\)\]\}]",
    r"[\(\[\{]\s*(subscribe|like\s+and\s+subscribe)[^\)\]\}]*[\)\]\}]",
    # #hashtags anywhere
    r"(?<!\w)#\w+",
    # emoji-ish decoration blocks; conservative range
    r"[\U0001F300-\U0001FAFF\U00002600-\U000027BF\uFE0F]",
]

# Trailing " | Channel Name" or " - Channel Name" style attributions.
TRAILING_ATTRIB = r"\s*[\|\-\u2013\u2014\u00b7•]\s*[^\|\-\u2013\u2014\u00b7•]{2,40}\s*$"


def clean_title(title: str, channel_name: str = "", strip_attrib: bool = True) -> str:
    """Strip the usual YouTube title barnacles. Conservative by design: it is
    better to leave a stray tag in than to eat half a real title."""
    t = html.unescape(title).strip()

    for pat in NOISE_PATTERNS:
        t = re.sub(pat, " ", t, flags=re.IGNORECASE)

    # Empty brackets left behind by the above
    t = re.sub(r"[\(\[\{]\s*[\)\]\}]", " ", t)

    # Drop a trailing "| Channel Name" only if it actually is the channel name,
    # which avoids amputating titles that legitimately end in "| Part Two".
    if channel_name:
        t = re.sub(
            r"\s*[\|\-\u2013\u2014\u00b7•]\s*" + re.escape(channel_name) + r"\s*$",
            "",
            t,
            flags=re.IGNORECASE,
        )
    elif strip_attrib:
        t = re.sub(TRAILING_ATTRIB, "", t)

    # Tidy whitespace and dangling punctuation
    t = re.sub(r"\s{2,}", " ", t).strip()
    t = re.sub(r"^[\|\-\u2013\u2014\u00b7•,:;]\s*", "", t)
    t = re.sub(r"\s*[\|\-\u2013\u2014\u00b7•,:;]$", "", t)

    # De-SHOUT anything that is all caps and long enough to be a sentence
    if len(t) > 12 and t.upper() == t and re.search(r"[A-Z]{4,}", t):
        t = t.title()

    return t.strip()


def truncate(text: str, limit: int = POST_LIMIT) -> str:
    if len(text) <= limit:
        return text
    cut = text[: limit - 1].rsplit(" ", 1)[0]
    return cut + "\u2026"


# --------------------------------------------------------------------------
# Feed
# --------------------------------------------------------------------------

def fetch_feed(channel_id: str) -> tuple[str, list[dict]]:
    r = requests.get(FEED_URL.format(channel_id), timeout=30)
    r.raise_for_status()
    root = ET.fromstring(r.content)

    channel_name = ""
    author = root.find("atom:author/atom:name", NS)
    if author is not None and author.text:
        channel_name = author.text.strip()

    videos = []
    for entry in root.findall("atom:entry", NS):
        vid = entry.findtext("yt:videoId", default="", namespaces=NS)
        title = entry.findtext("atom:title", default="", namespaces=NS)
        published = entry.findtext("atom:published", default="", namespaces=NS)
        desc = entry.findtext("media:group/media:description", default="", namespaces=NS)
        if not vid:
            continue
        videos.append(
            {
                "id": vid,
                "title": title,
                "published": published,
                "description": (desc or "").strip(),
                "url": f"https://www.youtube.com/watch?v={vid}",
            }
        )
    # Feed is newest-first; return oldest-first so posts go out in order.
    videos.reverse()
    return channel_name, videos


# --------------------------------------------------------------------------
# Thumbnail
# --------------------------------------------------------------------------

THUMB_CANDIDATES = ["maxresdefault", "sddefault", "hqdefault", "mqdefault"]


def fetch_thumbnail(video_id: str) -> bytes | None:
    """Get the best available thumbnail and squeeze it under the blob limit."""
    raw = None
    for name in THUMB_CANDIDATES:
        url = f"https://i.ytimg.com/vi/{video_id}/{name}.jpg"
        try:
            r = requests.get(url, timeout=30)
        except requests.RequestException:
            continue
        # YouTube returns a 120x90 grey placeholder rather than a 404 for
        # missing sizes, so check the payload is a plausible size too.
        if r.status_code == 200 and len(r.content) > 3000:
            raw = r.content
            break
    if raw is None:
        return None

    if len(raw) <= MAX_BLOB_BYTES:
        return raw

    img = Image.open(io.BytesIO(raw)).convert("RGB")
    for quality in (85, 75, 65, 55):
        buf = io.BytesIO()
        img.save(buf, format="JPEG", quality=quality, optimize=True)
        if buf.tell() <= MAX_BLOB_BYTES:
            return buf.getvalue()
    img.thumbnail((1280, 720))
    buf = io.BytesIO()
    img.save(buf, format="JPEG", quality=75, optimize=True)
    return buf.getvalue()


# --------------------------------------------------------------------------
# State
# --------------------------------------------------------------------------

def load_state(path: Path) -> dict:
    if path.exists():
        try:
            return json.loads(path.read_text())
        except json.JSONDecodeError:
            pass
    return {"seen": [], "initialised": False}


def save_state(path: Path, state: dict) -> None:
    # Keep the list bounded; the feed only ever returns ~15 entries.
    state["seen"] = state["seen"][-200:]
    path.write_text(json.dumps(state, indent=2))


# --------------------------------------------------------------------------
# Posting
# --------------------------------------------------------------------------

def post_video(client: Client, video: dict, channel_name: str, template: str) -> None:
    title = clean_title(video["title"], channel_name)
    text = template.format(title=title, url=video["url"], channel=channel_name)
    text = truncate(text)

    thumb_blob = None
    thumb_bytes = fetch_thumbnail(video["id"])
    if thumb_bytes:
        thumb_blob = client.upload_blob(thumb_bytes).blob

    description = video["description"].split("\n")[0][:250] or channel_name

    embed = models.AppBskyEmbedExternal.Main(
        external=models.AppBskyEmbedExternal.External(
            uri=video["url"],
            title=title,
            description=description,
            thumb=thumb_blob,
        )
    )
    client.send_post(text=text, embed=embed)


def main() -> int:
    handle = os.environ.get("BSKY_HANDLE")
    app_password = os.environ.get("BSKY_APP_PASSWORD")
    channel_id = os.environ.get("YT_CHANNEL_ID")
    if not all([handle, app_password, channel_id]):
        print("Missing BSKY_HANDLE, BSKY_APP_PASSWORD or YT_CHANNEL_ID", file=sys.stderr)
        return 1

    state_path = Path(os.environ.get("STATE_FILE", "seen.json"))
    template = os.environ.get("POST_TEMPLATE", "{title}")
    max_backfill = int(os.environ.get("MAX_BACKFILL", "3"))
    dry_run = os.environ.get("DRY_RUN") == "1"

    state = load_state(state_path)
    seen = set(state["seen"])

    channel_name, videos = fetch_feed(channel_id)
    new = [v for v in videos if v["id"] not in seen]

    if not state["initialised"]:
        # First run: don't dump the entire back catalogue onto the timeline.
        if len(new) > max_backfill:
            for v in new[:-max_backfill]:
                seen.add(v["id"])
            new = new[-max_backfill:]
        state["initialised"] = True

    if not new:
        print("No new videos.")
        state["seen"] = list(seen)
        save_state(state_path, state)
        return 0

    client = None
    if not dry_run:
        client = Client()
        client.login(handle, app_password)

    for v in new:
        cleaned = clean_title(v["title"], channel_name)
        if dry_run:
            print(f"[dry run] {v['id']}")
            print(f"  raw:     {v['title']}")
            print(f"  cleaned: {cleaned}")
            print(f"  url:     {v['url']}")
        else:
            try:
                post_video(client, v, channel_name, template)
                print(f"Posted {v['id']}: {cleaned}")
            except Exception as e:  # keep going; don't lose the rest of the batch
                print(f"FAILED {v['id']}: {e}", file=sys.stderr)
                continue
            time.sleep(2)  # gentle on the rate limiter
        seen.add(v["id"])

    state["seen"] = list(seen)
    save_state(state_path, state)
    return 0


if __name__ == "__main__":
    sys.exit(main())
