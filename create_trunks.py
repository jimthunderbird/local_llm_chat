#!/usr/bin/env python3
"""Create RAG trunks from a rewritten YouTube transcript.

Usage:
    python3 create_trunks.py <youtube_video_hash>

Reads:
    ./youtube_transcript/<youtube_video_hash>.rewrite
    ./youtube_transcript/<youtube_video_hash>.title

Writes:
    ./youtube_transcript/<youtube_video_hash>.<paragraph_index>.trunk
"""

import argparse
import re
import sys
from pathlib import Path
from xml.sax.saxutils import escape

from paragraph_utils import split_paragraphs

def normalize_argv(argv):
    """Let positional values begin with '-' (e.g. video ID -PcBFgkrR2o)."""
    if argv and argv[0].startswith("-") and argv[0] not in {"-h", "--help", "--"}:
        return ["--", *argv]
    return argv


OUTPUT_DIR = Path.cwd() / "youtube_transcript"

TRUNK_TEMPLATE = "<article><title>{title}</title><paragraph>{paragraph}</paragraph></article>"


def main() -> None:
    parser = argparse.ArgumentParser(
        description="Create trunk files from a rewritten YouTube transcript."
    )
    parser.add_argument("youtube_video_hash", help="YouTube video ID, e.g. FTwxtyOEGyM")
    args = parser.parse_args(normalize_argv(sys.argv[1:]))

    video_id = args.youtube_video_hash.strip()
    if not re.fullmatch(r"[A-Za-z0-9_-]+", video_id):
        sys.exit(f"Invalid YouTube video hash: {video_id!r}")

    rewrite_path = OUTPUT_DIR / f"{video_id}.rewrite"
    title_path = OUTPUT_DIR / f"{video_id}.title"

    if not rewrite_path.is_file():
        sys.exit(
            f"Rewrite not found: {rewrite_path}\n"
            f"Run: python3 rewrite.py {video_id}"
        )
    if not title_path.is_file():
        sys.exit(
            f"Title not found: {title_path}\n"
            f"Run: python3 fetch_youtube_transcript.py {video_id}"
        )

    rewritten_content = rewrite_path.read_text(encoding="utf-8")
    title = title_path.read_text(encoding="utf-8").strip(" \t\r\n")
    if not title:
        sys.exit(f"Title is empty: {title_path}")

    paragraphs = split_paragraphs(rewritten_content)
    if not paragraphs:
        sys.exit(f"Rewrite contains no paragraphs: {rewrite_path}")

    # Remove previously generated trunks for this video.
    for old_trunk in OUTPUT_DIR.glob(f"{video_id}.*.trunk"):
        old_trunk.unlink()

    for index, paragraph in enumerate(paragraphs, start=1):
        trunk = TRUNK_TEMPLATE.format(
            title=escape(title),
            paragraph=escape(paragraph),
        )
        trunk_path = OUTPUT_DIR / f"{video_id}.{index}.trunk"
        trunk_path.write_text(trunk + "\n", encoding="utf-8")

    print(f"Created {len(paragraphs)} trunk files for {video_id}")
    print(f"Output directory: {OUTPUT_DIR}")


if __name__ == "__main__":
    main()
