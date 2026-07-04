#!/usr/bin/env python3
"""
Checks a YouTube channel RSS feed for new videos and saves their transcripts
as Markdown files under transcripts/.

Audio is downloaded with yt-dlp, uploaded to AssemblyAI, and transcribed
with speaker diarization.

Required env vars:
  CHANNEL_ID         — YouTube channel ID (UC…) or handle (@name)
  ASSEMBLYAI_API_KEY — AssemblyAI API key
"""

import json
import os
import re
import sys
import tempfile
import xml.etree.ElementTree as ET
from pathlib import Path

import assemblyai as aai
import requests
import yt_dlp

BASE_DIR = Path(__file__).parent.parent
DATA_DIR = BASE_DIR / "data"
TRANSCRIPTS_DIR = BASE_DIR / "transcripts"
SEEN_FILE = DATA_DIR / "seen.json"

RSS_URL = "https://www.youtube.com/feeds/videos.xml?channel_id={channel_id}"

RSS_NS = {
    "atom": "http://www.w3.org/2005/Atom",
    "yt": "http://www.youtube.com/xml/schemas/2015",
}

LANGUAGE = "es"


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


def download_audio(video_id: str, out_dir: str) -> Path:
    """Download the best available audio track with yt-dlp, return the file path."""
    opts = {
        "format": "bestaudio/best",
        "outtmpl": f"{out_dir}/{video_id}.%(ext)s",
        "quiet": True,
        "no_warnings": True,
    }

    # Write cookies to a temp file if provided (needed on cloud IPs blocked by YouTube)
    cookies_content = os.environ.get("YOUTUBE_COOKIES", "").strip()
    cookies_path = None
    if cookies_content:
        fd, cookies_path = tempfile.mkstemp(suffix=".txt")
        with os.fdopen(fd, "w") as fh:
            fh.write(cookies_content)
        opts["cookiefile"] = cookies_path

    try:
        with yt_dlp.YoutubeDL(opts) as ydl:
            info = ydl.extract_info(f"https://www.youtube.com/watch?v={video_id}", download=True)
            ext = info.get("ext", "m4a")
        return Path(out_dir) / f"{video_id}.{ext}"
    finally:
        if cookies_path:
            Path(cookies_path).unlink(missing_ok=True)


def transcribe(video_id: str) -> str | None:
    """
    Download audio with yt-dlp, upload to AssemblyAI, return speaker-labelled transcript.
    Returns None for permanent failures (video unavailable, no audio, etc.).
    Raises on transient errors so the video is retried next run.
    """
    tmp_dir = tempfile.mkdtemp()
    audio_path = None
    try:
        print("  Downloading audio with yt-dlp …")
        try:
            audio_path = download_audio(video_id, tmp_dir)
        except yt_dlp.utils.DownloadError as exc:
            print(f"  Download failed — skipping: {exc}")
            return None

        print(f"  Uploading to AssemblyAI ({audio_path.stat().st_size // 1024 // 1024} MB) …")
        config = aai.TranscriptionConfig(
            speaker_labels=True,
            language_code=LANGUAGE,
        )
        transcriber = aai.Transcriber(config=config)
        result = transcriber.transcribe(str(audio_path))

        if result.status == aai.TranscriptStatus.error:
            msg = result.error or "unknown error"
            print(f"  AssemblyAI error — skipping: {msg}")
            return None

        if not result.utterances:
            return result.text or ""

        lines = [f"**Speaker {u.speaker}:** {u.text}" for u in result.utterances]
        return "\n\n".join(lines)

    finally:
        if audio_path and audio_path.exists():
            audio_path.unlink()
        Path(tmp_dir).rmdir()


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
    api_key = os.environ.get("ASSEMBLYAI_API_KEY", "").strip()
    if not api_key:
        print("ERROR: ASSEMBLYAI_API_KEY environment variable is not set.", file=sys.stderr)
        sys.exit(1)
    aai.settings.api_key = api_key

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
            text = transcribe(video["id"])
        except Exception as exc:
            print(f"  Transient error — will retry next run: {exc}")
            continue
        if text is not None:
            save_transcript(video, text)
            processed.append(video)
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
