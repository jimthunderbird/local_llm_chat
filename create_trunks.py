#!/usr/bin/env python3
"""Create news-style RAG trunks directly from a YouTube transcript.

Usage:
    python3 create_trunks.py <youtube_video_hash>

Reads:
    ./.chat/articles_cache/youtube_<youtube_video_hash>.transcript
    ./.chat/articles_cache/youtube_<youtube_video_hash>.title

Writes:
    ./.chat/articles_cache/youtube_<youtube_video_hash>.0.trunk
    ./.chat/articles_cache/youtube_<youtube_video_hash>.<paragraph_index>.trunk

This mirrors news learning: one compact overview trunk (index 0) plus one
trunk per transcript paragraph. No rewrite file and no embeddings are needed.
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


OUTPUT_DIR = Path.cwd() / ".chat" / "articles_cache"
YOUTUBE_PREFIX = "youtube_"

TRUNK_TEMPLATE = "<article><title>{title}</title><paragraph>{paragraph}</paragraph></article>"
SPEAKER_MARKER_RE = re.compile(r">>\s*")
BRACKETED_SOUND_RE = re.compile(r"\[(?:laughter|music|applause|silence)\]", re.IGNORECASE)


def clean_transcript(transcript: str) -> str:
    """Normalize raw caption text so it splits into readable paragraphs."""
    text = SPEAKER_MARKER_RE.sub(" ", transcript)
    text = BRACKETED_SOUND_RE.sub(" ", text)
    text = text.replace("\r\n", "\n").replace("\r", "\n")
    return text.strip()


def transcript_paragraphs(transcript: str) -> list[str]:
    """Return short, self-contained trunks from a raw transcript."""
    paragraphs = split_paragraphs(clean_transcript(transcript))
    # Captions often append a trailing speaker change or filler token as its
    # own tiny paragraph. Drop fragments that carry no retrievable content.
    return [paragraph for paragraph in paragraphs if len(paragraph.split()) >= 3]


def build_video_trunks(
    video_id: str,
    output_dir: Path = OUTPUT_DIR,
) -> int:
    """Generate the overview and paragraph trunks for one transcript.

    Returns the number of paragraph trunks written (the overview trunk is
    additional). Raises ``FileNotFoundError``/``ValueError`` with a readable
    message when the transcript or title is unusable.
    """
    storage_id = f"{YOUTUBE_PREFIX}{video_id}"
    transcript_path = output_dir / f"{storage_id}.transcript"
    title_path = output_dir / f"{storage_id}.title"

    if not transcript_path.is_file():
        raise FileNotFoundError(
            f"Transcript not found: {transcript_path}\n"
            f"Run: python3 fetch_youtube_transcript.py {video_id}"
        )

    transcript = transcript_path.read_text(encoding="utf-8")
    paragraphs = transcript_paragraphs(transcript)
    if not paragraphs:
        raise ValueError(f"Transcript contains no paragraphs: {transcript_path}")

    title = ""
    if title_path.is_file():
        title = title_path.read_text(encoding="utf-8").strip(" \t\r\n")
    if not title:
        # A missing title must not block learning; the video id still gives
        # every trunk a stable, searchable label.
        title = video_id

    # Remove previously generated trunks for this video before rebuilding.
    for old_trunk in output_dir.glob(f"{storage_id}.*.trunk"):
        old_trunk.unlink()

    # Index 0 mirrors news learning: a compact overview trunk for broad
    # questions such as "what is this video about?".
    overview_trunk = TRUNK_TEMPLATE.format(
        title=escape(title),
        paragraph=escape(paragraphs[0]),
    )
    overview_path = output_dir / f"{storage_id}.0.trunk"
    overview_path.write_text(overview_trunk + "\n", encoding="utf-8")
    trunks = [
        (0, overview_path),
    ]

    for index, paragraph in enumerate(paragraphs, start=1):
        trunk = TRUNK_TEMPLATE.format(
            title=escape(title),
            paragraph=escape(paragraph),
        )
        trunk_path = output_dir / f"{storage_id}.{index}.trunk"
        trunk_path.write_text(trunk + "\n", encoding="utf-8")
        trunks.append((index, trunk_path))

    # Mirror the news learning path: persist the trunks and their search
    # entries together so the unified SQLite BM25 corpus can find a new video
    # immediately, even before the server rebuilds its in-memory chunk list.
    try:
        if output_dir.resolve() == OUTPUT_DIR.resolve():
            import chat

            chat.index_chunks_in_sqlite(
                [
                    chat.Chunk(
                        video_id=video_id, index=index, trunk_path=trunk_path
                    )
                    for index, trunk_path in trunks
                ],
                corpus_key="global",
            )
    except Exception:
        # The trunks on disk are authoritative; a missing/unavailable index
        # is rebuilt lazily on the next question or server start.
        pass

    return len(paragraphs)


def main() -> None:
    parser = argparse.ArgumentParser(
        description="Create news-style trunk files directly from a YouTube transcript."
    )
    parser.add_argument("youtube_video_hash", help="YouTube video ID, e.g. FTwxtyOEGyM")
    args = parser.parse_args(normalize_argv(sys.argv[1:]))

    video_id = args.youtube_video_hash.strip()
    if not re.fullmatch(r"[A-Za-z0-9_-]+", video_id):
        sys.exit(f"Invalid YouTube video hash: {video_id!r}")

    try:
        paragraph_count = build_video_trunks(video_id)
    except (OSError, ValueError) as exc:
        sys.exit(str(exc))

    print(
        f"Created {paragraph_count + 1} trunk files for {video_id} "
        f"(overview + {paragraph_count} paragraphs)"
    )
    print(f"Output directory: {OUTPUT_DIR}")


if __name__ == "__main__":
    main()
