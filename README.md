# YouTube Transcript Bot

Automatically transcribes new videos from a YouTube channel and commits the
transcripts to this repository. Runs on GitHub Actions every hour with no API
key required.

## How it works

1. A GitHub Actions workflow triggers on a cron schedule (hourly) or manually.
2. `scripts/check_and_transcribe.py` fetches the channel's public RSS feed
   (`https://www.youtube.com/feeds/videos.xml?channel_id=CHANNEL_ID`).
3. It compares the feed against `data/seen.json` to find videos not yet processed.
4. For each new video it fetches the auto-generated or manual captions via
   [`youtube-transcript-api`](https://github.com/jdepoix/youtube-transcript-api)
   (no API key needed).
5. Each transcript is saved as a Markdown file in `transcripts/` with YAML
   frontmatter (title, video ID, URL, published date).
6. The workflow commits any new files back to the repo automatically.

## Setup

### 1. Set the channel ID

Go to your repository → **Settings → Variables → Actions** and create a
repository variable:

| Name | Value |
|------|-------|
| `CHANNEL_ID` | The YouTube channel ID (starts with `UC`, e.g. `UCVHkD_8EEcGBg6bZGBLdVAA`) |

You can also pass a **handle** (e.g. `@mkbhd`) — the script will resolve it to
a channel ID automatically by fetching the channel page.

**Finding a channel ID from a handle:**

```bash
CHANNEL_ID=$(python - <<'EOF'
import re, sys
import requests
handle = "@mkbhd"   # replace with your handle
r = requests.get(f"https://www.youtube.com/{handle}", headers={"User-Agent":"Mozilla/5.0"})
m = re.search(r'"channelId"\s*:\s*"(UC[^"]+)"', r.text)
print(m.group(1) if m else "not found")
EOF
)
echo $CHANNEL_ID
```

Alternatively, set `CHANNEL_ID` directly in a `config.json` at the repo root:

```json
{ "channel_id": "UCxxxxxxxxxxxxxxxxxxxxxx" }
```

### 2. Grant write access to the workflow

The workflow already has `permissions: contents: write`, so GitHub Actions can
push commits. No extra token configuration is needed for public repos.

For **private repos** you may need to enable *Allow GitHub Actions to create
and approve pull requests* under **Settings → Actions → General → Workflow
permissions**.

### 3. Enable the scheduled workflow

Push the code to `main`. The cron job becomes active automatically once the
workflow file is on the default branch. You can also trigger it on demand:

**Actions → Check and Transcribe New Videos → Run workflow**

## Running locally

```bash
# Install dependencies
pip install -r requirements.txt

# Set the channel (pick one):
export CHANNEL_ID="UCxxxxxxxxxxxxxxxxxxxxxx"
# or: echo '{"channel_id":"UCxxxxxxxxxxxxxxxxxxxxxx"}' > config.json

python scripts/check_and_transcribe.py
```

Transcripts are written to `transcripts/` and `data/seen.json` is updated.
Re-running the script skips already-processed videos.

## Output format

Each transcript file is named:

```
transcripts/YYYY-MM-DD-<video-id>-<slugified-title>.md
```

With a YAML frontmatter header:

```markdown
---
title: "Video Title"
video_id: dQw4w9WgXcQ
url: https://www.youtube.com/watch?v=dQw4w9WgXcQ
published: 2024-01-15T18:00:00+00:00
---

Full transcript text …
```

## Notes

- Videos without captions (disabled or unavailable) are logged and skipped —
  they are still added to `seen.json` so they are not retried on every run.
- The RSS feed returns the 15 most recent videos, so the first run may process
  up to 15 videos at once.
- Bot commits use the `github-actions[bot]` identity and do not trigger
  additional workflow runs.
