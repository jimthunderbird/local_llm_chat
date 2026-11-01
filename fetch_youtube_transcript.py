#!/usr/bin/env python3
"""Fetch an English YouTube transcript and summarize it with Ollama.

Usage:
    python3 fetch_youtube_transcript.py <youtube_video_hash>

Outputs:
    ./youtube_transcript/<youtube_video_hash>.transcript
    ./youtube_transcript/<youtube_video_hash>.title
"""

import argparse
import os
import re
import sys
from pathlib import Path

try:
    import requests
    from youtube_transcript_api import YouTubeTranscriptApi
except ImportError as exc:  # pragma: no cover - dependency guard
    sys.exit(
        f"Missing dependency: {exc.name}. "
        "Install with: pip install youtube-transcript-api requests"
    )

def normalize_argv(argv):
    """Let positional values begin with '-' (e.g. video ID -PcBFgkrR2o)."""
    if argv and argv[0].startswith("-") and argv[0] not in {"-h", "--help", "--"}:
        return ["--", *argv]
    return argv


MODEL = "ornith:35b"
OUTPUT_DIR = Path.cwd() / "youtube_transcript"

OLLAMA_HOST = os.environ.get("OLLAMA_HOST", "http://localhost:11434").rstrip("/")
if not OLLAMA_HOST.startswith(("http://", "https://")):
    OLLAMA_HOST = f"http://{OLLAMA_HOST}"


def fetch_transcript(video_id: str) -> str:
    """Return the video's English transcript as plain text."""
    api = YouTubeTranscriptApi()

    try:
        fetched = api.fetch(video_id, languages=["en", "en-US", "en-GB"])
    except Exception:
        # Fall back to any available English transcript (e.g. auto-generated
        # variants whose language code is not exactly "en").
        available = list(api.list(video_id))
        english = [
            transcript
            for transcript in available
            if transcript.language_code.lower().startswith("en")
        ]
        if not english:
            raise RuntimeError(f"No English transcript available for {video_id}")
        fetched = english[0].fetch()

    text = " ".join(" ".join(snippet.text.split()) for snippet in fetched)
    if not text.strip():
        raise RuntimeError(f"Transcript for {video_id} is empty")
    return text.strip()


def summarize(transcript: str) -> str:
    """Ask the local Ollama model for a 20-word summary."""
    prompt = (
        "Summarize the following YouTube transcript in exactly 20 words. "
        "Return only the summary, with no quotes, labels, or explanation.\n\n"
        f"Transcript:\n{transcript}"
    )
    payload = {
        "model": MODEL,
        "prompt": prompt,
        "stream": False,
        "think": False,
        "options": {"temperature": 0.2, "num_predict": 64},
    }

    try:
        response = requests.post(
            f"{OLLAMA_HOST}/api/generate", json=payload, timeout=600
        )
        response.raise_for_status()
    except requests.RequestException as exc:
        raise RuntimeError(
            f"Could not reach Ollama at {OLLAMA_HOST}. Is it running? ({exc})"
        ) from exc

    summary = response.json().get("response", "").strip()
    summary = re.sub(r"^(summary|title)\s*:\s*", "", summary, flags=re.IGNORECASE)
    summary = summary.strip().strip("\"'“”").strip()
    summary = re.sub(r"\s+", " ", summary)

    # Enforce the 20-word limit even if the model overshoots.
    words = summary.split()
    if len(words) > 20:
        summary = " ".join(words[:20])

    if not summary:
        raise RuntimeError("Ollama returned an empty summary")
    return summary


def main() -> None:
    parser = argparse.ArgumentParser(
        description="Fetch a YouTube transcript and summarize it into 20 words."
    )
    parser.add_argument("youtube_video_hash", help="YouTube video ID, e.g. FTwxtyOEGyM")
    args = parser.parse_args(normalize_argv(sys.argv[1:]))

    video_id = args.youtube_video_hash.strip()
    if not re.fullmatch(r"[A-Za-z0-9_-]+", video_id):
        sys.exit(f"Invalid YouTube video hash: {video_id!r}")

    try:
        transcript = fetch_transcript(video_id)
        summary = summarize(transcript)
    except Exception as exc:
        sys.exit(f"Error: {exc}")

    OUTPUT_DIR.mkdir(parents=True, exist_ok=True)
    transcript_path = OUTPUT_DIR / f"{video_id}.transcript"
    title_path = OUTPUT_DIR / f"{video_id}.title"

    transcript_path.write_text(transcript + "\n", encoding="utf-8")
    title_path.write_text(summary + "\n", encoding="utf-8")

    print(f"Saved transcript: {transcript_path}")
    print(f"Saved title:      {title_path}")
    print(f"Title: {summary}")


if __name__ == "__main__":
    main()
