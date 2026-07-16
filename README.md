# YouTube to Bluesky reposter

Polls a YouTube channel's public RSS feed and posts new videos to Bluesky as a
link card with the real thumbnail and a cleaned-up title.

No YouTube API key. No quota. Runs free on GitHub Actions.

## Setup

### 1. Find the channel ID

It must be the `UC...` ID, not the `@handle`. Go to the channel page, view
source, and search for `channelId`. Or use a lookup site such as
commentpicker.com/youtube-channel-id.php.

Test it: `https://www.youtube.com/feeds/videos.xml?channel_id=UCxxxx` should
return XML in your browser.

### 2. Make a Bluesky app password

Bluesky: Settings, then Privacy and security, then App passwords. Never use
your account password.

### 3. Put it on GitHub

Create a repo (private is fine, see costs below) and add these files. Then in
Settings, Secrets and variables, Actions, add three repository secrets:

- `BSKY_HANDLE`
- `BSKY_APP_PASSWORD`
- `YT_CHANNEL_ID`

Push, then open the Actions tab and trigger "YouTube to Bluesky" manually to
check it works. After that it runs every 30 minutes.

### 4. Test locally first

```bash
pip install -r requirements.txt
cp .env.example .env      # fill it in
export $(cat .env | xargs)
DRY_RUN=1 python bot.py
```

Dry run prints the raw and cleaned titles without posting anything. Worth doing
against your real channel to tune the cleaning rules before it goes live.

## Configuration

| Variable | Default | Notes |
|---|---|---|
| `POST_TEMPLATE` | `{title}` | Placeholders: `{title}`, `{url}`, `{channel}`. The URL is already in the card, so you usually don't need it in the text. |
| `MAX_BACKFILL` | `3` | First run only: how many recent videos to post rather than the whole feed. |
| `STATE_FILE` | `seen.json` | Tracks posted video IDs. |
| `DRY_RUN` | unset | Set to `1` to print instead of post. |

## Title cleaning

`clean_title()` in bot.py removes: `(Official Video)` and variants, quality tags
like `[4K]` and `(HD)`, hashtags, emoji, engagement bait, and a trailing
`| Channel Name` attribution. It also converts ALL CAPS titles to title case.

It only strips a trailing `| Something` when that something matches the channel
name, so a title like `Heritage Wheat Trials | Part Two` survives intact.

Edit `NOISE_PATTERNS` to add your own rules. Run with `DRY_RUN=1` after any
change.

## Notes and gotchas

- The feed carries roughly the 15 most recent videos and updates within a few
  minutes of publishing. A 30 minute cron is plenty; going below 15 minutes
  gains you little.
- Scheduled GitHub Actions on a repo with no pushes get disabled after 60 days
  of inactivity. You'll get an email; one click re-enables it. The state commit
  each time a video posts also counts as activity.
- Scheduled runs on GitHub can be delayed by 5 to 15 minutes at busy times. Not
  an issue for this.
- Bluesky rejects image blobs over about 976 KB, so thumbnails are recompressed
  if needed.
- Premieres and unlisted-then-public videos can appear in the feed at odd times.
  If that bites, filter on the `published` field.

## Costs

- Public repo: Actions minutes are free and unlimited.
- Private repo: 2,000 free minutes a month on the free plan. This job takes
  under a minute, so roughly 48 minutes a day at a 30 minute cadence. That's
  around 1,500 a month, which fits, but it's tight. Either go public (there are
  no secrets in the code, only in Actions secrets) or drop the cron to hourly.

## Alternatives to GitHub Actions

- Any always-on machine with cron: `*/30 * * * * cd /path && python bot.py`
- A cheap VPS, or a Raspberry Pi at home
- Fly.io or similar free tiers, though a cron job is overkill for a container
