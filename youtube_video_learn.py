#!/usr/bin/env python3
"""Ingest a YouTube video into the RAG knowledge base.

Usage:
    python3 youtube_video_learn.py <youtube_video_hash>
    python3 youtube_video_learn.py https://www.youtube.com/watch?v=T594Cg4sTq4
    python3 youtube_video_learn.py https://youtu.be/T594Cg4sTq4?t=227

Runs, in order:
    1. fetch_youtube_transcript.py
    2. rewrite.py
    3. create_trunks.py
    4. create_embeddings.py

Tagging is no longer part of ingestion: tags in ./tags.toml are soft
query-time scopes, so a newly learned video is immediately available to every
valid tag.
"""

import argparse
import re
import subprocess
import sys
from pathlib import Path
from urllib.parse import parse_qs, urlparse

SCRIPT_DIR = Path(__file__).resolve().parent

VIDEO_HASH_RE = re.compile(r"^[A-Za-z0-9_-]{11}$")


def normalize_argv(argv):
    """Let positional values begin with '-' (e.g. video ID -PcBFgkrR2o)."""
    if argv and argv[0].startswith("-") and argv[0] not in {"-h", "--help", "--"}:
        return ["--", *argv]
    return argv


def extract_video_hash(value: str) -> str:
    """Extract an 11-character YouTube video ID from a hash or URL.

    Accepts bare IDs and common YouTube URL forms, including
    youtube.com/watch?v=..., youtu.be/..., /shorts/, /embed/, /live/,
    with optional extra query parameters (e.g. ?t=227).
    """
    value = value.strip()

    # Bare video ID.
    if VIDEO_HASH_RE.fullmatch(value):
        return value

    # Bare ID with a query string or fragment, e.g. T594Cg4sTq4?t=227.
    bare = value.split("?", 1)[0].split("#", 1)[0].strip("/")
    if VIDEO_HASH_RE.fullmatch(bare):
        return bare

    # Parse as a URL, tolerating a missing scheme (e.g. youtu.be/ID).
    parsed = urlparse(value if "://" in value else "https://" + value)
    host = (parsed.hostname or "").lower()
    if host.startswith("www."):
        host = host[4:]

    if host == "youtu.be":
        candidate = parsed.path.lstrip("/").split("/", 1)[0]
        if VIDEO_HASH_RE.fullmatch(candidate):
            return candidate

    if host in {"youtube.com", "m.youtube.com", "music.youtube.com", "youtube-nocookie.com"}:
        if parsed.path.rstrip("/") == "/watch":
            candidate = parse_qs(parsed.query).get("v", [""])[0]
            if VIDEO_HASH_RE.fullmatch(candidate):
                return candidate

        parts = [part for part in parsed.path.split("/") if part]
        if len(parts) >= 2 and parts[0] in {"shorts", "embed", "live", "v"}:
            if VIDEO_HASH_RE.fullmatch(parts[1]):
                return parts[1]

    raise ValueError(
        f"Could not extract a YouTube video ID from {value!r}. "
        "Expected an 11-character ID or a YouTube URL."
    )


STEPS = [
    ("fetch_youtube_transcript.py", ["<hash>"]),
    ("rewrite.py", ["<hash>"]),
    ("create_trunks.py", ["<hash>"]),
    ("create_embeddings.py", ["<hash>"]),
]


def run_step(script: str, script_args: list[str], youtube_video_hash: str) -> None:
    """Run a pipeline step, streaming its output; abort on failure."""
    path = SCRIPT_DIR / script
    argv = [arg.replace("<hash>", youtube_video_hash) for arg in script_args]

    print(f"\n=== {script} {' '.join(argv)} ===", flush=True)
    result = subprocess.run(
        [sys.executable, str(path), *argv],
        cwd=SCRIPT_DIR,
    )

    if result.returncode != 0:
        sys.exit(f"{script} failed with exit code {result.returncode}")


def main() -> None:
    parser = argparse.ArgumentParser(
        description="Fetch, rewrite, chunk, and embed a YouTube video transcript."
    )
    parser.add_argument(
        "youtube_video_hash",
        help="YouTube video ID or URL, e.g. FTwxtyOEGyM or https://youtu.be/FTwxtyOEGyM",
    )
    args = parser.parse_args(normalize_argv(sys.argv[1:]))

    try:
        youtube_video_hash = extract_video_hash(args.youtube_video_hash)
    except ValueError as exc:
        sys.exit(str(exc))

    print(f"Video ID: {youtube_video_hash}")

    for script, script_args in STEPS:
        run_step(script, script_args, youtube_video_hash)

    print(f"\nDone: {youtube_video_hash} is ready for chat.py")


if __name__ == "__main__":
    main()
