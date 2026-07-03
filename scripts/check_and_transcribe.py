#!/usr/bin/env python3
"""
Checks a YouTube channel RSS feed for new videos and saves their transcripts
as Markdown files under transcripts/.

Channel ID is read from the CHANNEL_ID env var (preferred) or config.json.
A YouTube handle (@name) is also accepted and resolved automatically.
"""

import json
import os
import re
import sys
import xml.etree.ElementTree as ET
from pathlib import Path

import requests
from youtube_transcript_api import YouTubeTranscriptApi
from youtube_transcript_api._errors import (
    CouldNotRetrieveTranscript,
    IpBlocked,
    NoTranscriptFound,
    TranscriptsDisabled,
)

BASE_DIR = Path(__file__).parent.parent
DATA_DIR = BASE_DIR / "data"
TRANSCRIPTS_DIR = BASE_DIR / "transcripts"
SEEN_FILE = DATA_DIR / "seen.json"

RSS_URL = "https://www.youtube.com/feeds/videos.xml?channel_id={channel_id}"

RSS_NS = {
    "atom": "http://www.w3.org/2005/Atom",
    "yt": "http://www.youtube.com/xml/schemas/2015",
}


def resolve_channel_id(value: str) -> str:
    """Accept a channel ID (UC…) or a handle (@name / name) and return a channel ID."""
    value = value.strip()
    if re.match(r"^UC[\w-]{20,}$", value):
        return value

    handle = value.lstrip("@")
    url = f"https://www.youtube.com/@{handle}"
    print(f"Resolving handle @{handle} → channel ID …")
    resp = requests.get(url, headers={"User-Agent": "Mozilla/5.0"}, timeout=15)
    resp.raise_for_status()
    match = re.search(r'"channelId"\s*:\s*"(UC[^"]{20,})"', resp.text)
    if not match:
        raise ValueError(f"Could not find channel ID on page {url}")
    channel_id = match.group(1)
    print(f"  → {channel_id}")
    return channel_id


def fetch_feed(channel_id: str) -> list[dict]:
    url = RSS_URL.format(channel_id=channel_id)
    print(f"Fetching RSS feed: {url}")
    resp = requests.get(url, timeout=15)
    resp.raise_for_status()

    root = ET.fromstring(resp.text)
    videos = []
    for entry in root.findall("atom:entry", RSS_NS):
        video_id = entry.find("yt:videoId", RSS_NS).text
        title = entry.find("atom:title", RSS_NS).text
        published = entry.find("atom:published", RSS_NS).text
        videos.append({"id": video_id, "title": title, "published": published})
    return videos


def load_seen() -> set[str]:
    if SEEN_FILE.exists():
        return set(json.loads(SEEN_FILE.read_text()).get("seen", []))
    return set()


def save_seen(seen: set[str]) -> None:
    DATA_DIR.mkdir(parents=True, exist_ok=True)
    SEEN_FILE.write_text(json.dumps({"seen": sorted(seen)}, indent=2) + "\n")


def slugify(text: str) -> str:
    text = text.lower()
    text = re.sub(r"[^\w\s-]", "", text)
    text = re.sub(r"[\s_-]+", "-", text)
    return text.strip("-")[:60]


class TransientError(Exception):
    """Raised for temporary failures (IP block, network error) — don't mark as seen."""


def fetch_transcript(video_id: str) -> str | None:
    """Return transcript text, None if permanently unavailable, raise TransientError if temporary."""
    api = YouTubeTranscriptApi()
    try:
        try:
            transcript = api.fetch(video_id, languages=["en"])
        except NoTranscriptFound:
            # English not available — try whatever language is first in the list
            transcript_list = api.list(video_id)
            transcript = next(iter(transcript_list)).fetch()
        return " ".join(snippet.text for snippet in transcript)
    except IpBlocked as exc:
        # IpBlocked is a subclass of CouldNotRetrieveTranscript — catch it first
        raise TransientError("IP blocked by YouTube") from exc
    except (TranscriptsDisabled, NoTranscriptFound, CouldNotRetrieveTranscript) as exc:
        print(f"  No captions available: {exc}")
        return None
    except requests.RequestException as exc:
        raise TransientError(f"Network error: {exc}") from exc
    except Exception as exc:
        print(f"  Transcript fetch error (skipping): {exc}")
        return None


def save_transcript(video: dict, text: str) -> Path:
    TRANSCRIPTS_DIR.mkdir(parents=True, exist_ok=True)
    date = video["published"][:10]
    slug = slugify(video["title"])
    filename = f"{date}-{video['id']}-{slug}.md"
    filepath = TRANSCRIPTS_DIR / filename

    escaped_title = video["title"].replace('"', '\\"')
    content = (
        f'---\ntitle: "{escaped_title}"\n'
        f"video_id: {video['id']}\n"
        f"url: https://www.youtube.com/watch?v={video['id']}\n"
        f"published: {video['published']}\n"
        f"---\n\n{text}\n"
    )
    filepath.write_text(content)
    print(f"  Saved: transcripts/{filename}")
    return filepath


def set_github_output(processed: list[dict]) -> None:
    output_file = os.environ.get("GITHUB_OUTPUT")
    if not output_file:
        return
    titles = ", ".join(v["title"] for v in processed[:3])
    if len(processed) > 3:
        titles += f" and {len(processed) - 3} more"
    with open(output_file, "a") as fh:
        fh.write(f"commit_message=Add transcripts: {titles}\n")
        fh.write("has_new=true\n")


def main() -> None:
    channel_id = os.environ.get("CHANNEL_ID", "").strip()

    if not channel_id:
        config_path = BASE_DIR / "config.json"
        if config_path.exists():
            channel_id = json.loads(config_path.read_text()).get("channel_id", "")

    if not channel_id:
        print(
            "ERROR: Set the CHANNEL_ID environment variable or create config.json "
            'with {"channel_id": "UCxxxxxx"}.',
            file=sys.stderr,
        )
        sys.exit(1)

    channel_id = resolve_channel_id(channel_id)
    seen = load_seen()
    videos = fetch_feed(channel_id)

    new_videos = [v for v in videos if v["id"] not in seen]
    print(f"Feed has {len(videos)} videos; {len(new_videos)} are new.")

    processed = []
    for video in new_videos:
        print(f"\nProcessing: {video['title']}  ({video['id']})")
        try:
            transcript = fetch_transcript(video["id"])
        except TransientError as exc:
            print(f"  Transient error — will retry next run: {exc}")
            continue
        if transcript:
            save_transcript(video, transcript)
            processed.append(video)
        # Mark as seen only after a definitive outcome (success or permanent no-captions)
        seen.add(video["id"])

    if new_videos:
        save_seen(seen)

    if processed:
        set_github_output(processed)
        print(f"\nDone — saved {len(processed)} transcript(s).")
    else:
        print("\nNo new transcripts to save.")


if __name__ == "__main__":
    main()
