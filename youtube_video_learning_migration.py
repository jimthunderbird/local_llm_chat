#!/usr/bin/env python3
"""Migrate the YouTube corpus into the unified articles cache.

Usage:
    python3 youtube_video_learning_migration.py
    python3 youtube_video_learning_migration.py --dry-run
    python3 youtube_video_learning_migration.py --rebuild-trunk

The YouTube corpus used to live in ``.chat/youtube_transcripts`` with files
named ``<video>.<index>.trunk``, ``<video>.transcript``, and ``<video>.title``.
It now shares ``.chat/articles_cache`` with learned news stories and web
articles, and every YouTube artifact carries a ``youtube_`` prefix:

    youtube_<video>.<index>.trunk
    youtube_<video>.transcript
    youtube_<video>.title

This migration moves and renames those files in place, removes obsolete
rewrite/embedding artifacts, and rebuilds the SQLite FTS5 BM25 index. Pass
``--rebuild-trunk`` to also regenerate every trunk from its transcript using
the current news-style chunker (normally unnecessary because the moved trunks
are already in that format).
"""

from __future__ import annotations

import argparse
import re
import sys
from pathlib import Path

import create_trunks

PROJECT_DIR = Path(__file__).resolve().parent
CHAT_DIR = PROJECT_DIR / ".chat"
OLD_DIR = CHAT_DIR / "youtube_transcripts"
NEW_DIR = CHAT_DIR / "articles_cache"
YOUTUBE_PREFIX = "youtube_"

VIDEO_ID = r"[A-Za-z0-9_-]+"
TRUNK_RE = re.compile(rf"^(?P<id>{VIDEO_ID})\.(?P<index>\d+)\.trunk$")
TRANSCRIPT_RE = re.compile(rf"^(?P<id>{VIDEO_ID})\.transcript$")
TITLE_RE = re.compile(rf"^(?P<id>{VIDEO_ID})\.title$")
# Files written by the retired rewrite/embedding pipeline. They are not part
# of the current learning process, so they are removed rather than moved.
LEGACY_SUFFIXES = (".embedding", ".paragraph.embedding", ".rewrite")


def _new_name(name: str) -> str | None:
    """Return the prefixed name for a legacy YouTube artifact."""
    if name.startswith(YOUTUBE_PREFIX):
        # Already in the unified-cache naming scheme.
        if TRUNK_RE.fullmatch(name[len(YOUTUBE_PREFIX) :]) or TRANSCRIPT_RE.fullmatch(
            name[len(YOUTUBE_PREFIX) :]
        ) or TITLE_RE.fullmatch(name[len(YOUTUBE_PREFIX) :]):
            return name
        return None
    match = TRUNK_RE.fullmatch(name)
    if match:
        return f"{YOUTUBE_PREFIX}{match.group('id')}.{match.group('index')}.trunk"
    match = TRANSCRIPT_RE.fullmatch(name)
    if match:
        return f"{YOUTUBE_PREFIX}{match.group('id')}.transcript"
    match = TITLE_RE.fullmatch(name)
    if match:
        return f"{YOUTUBE_PREFIX}{match.group('id')}.title"
    return None


def _remove_legacy_artifacts(directory: Path, dry_run: bool) -> dict[str, int]:
    removed = {suffix: 0 for suffix in LEGACY_SUFFIXES}
    if not directory.is_dir():
        return removed
    for path in sorted(directory.iterdir()):
        if not path.is_file():
            continue
        for suffix in LEGACY_SUFFIXES:
            if path.name.endswith(suffix):
                if not dry_run:
                    path.unlink()
                removed[suffix] += 1
                break
    return removed


def move_legacy_files(dry_run: bool = False) -> dict[str, int]:
    """Move legacy youtube_transcripts files into the articles cache."""
    result = {
        "moved": 0,
        "unchanged": 0,
        "conflicts": 0,
        "ignored": 0,
        "removed_embedding": 0,
        "removed_rewrite": 0,
        "removed_paragraph_embedding": 0,
    }
    if not OLD_DIR.is_dir():
        return result

    removed = _remove_legacy_artifacts(OLD_DIR, dry_run)
    result["removed_embedding"] = removed[".embedding"]
    result["removed_rewrite"] = removed[".rewrite"]
    result["removed_paragraph_embedding"] = removed[".paragraph.embedding"]

    if not dry_run:
        NEW_DIR.mkdir(parents=True, exist_ok=True)

    for source in sorted(OLD_DIR.iterdir()):
        if not source.is_file():
            continue
        if any(source.name.endswith(suffix) for suffix in LEGACY_SUFFIXES):
            continue
        new_name = _new_name(source.name)
        if new_name is None:
            result["ignored"] += 1
            continue
        destination = NEW_DIR / new_name
        if destination.exists():
            # Never overwrite an already migrated or newly learned artifact.
            result["conflicts"] += 1
            continue
        if not dry_run:
            source.replace(destination)
        result["moved"] += 1

    if not dry_run:
        # Remove the old directory when empty so there is only one corpus
        # location from now on.
        try:
            OLD_DIR.rmdir()
        except OSError:
            pass
    return result


def rebuild_trunks(dry_run: bool = False) -> tuple[int, list[tuple[str, str]]]:
    """Regenerate news-style trunks from every migrated transcript."""
    if not NEW_DIR.is_dir():
        return 0, []
    transcript_ids = sorted(
        path.name[len(YOUTUBE_PREFIX) : -len(".transcript")]
        for path in NEW_DIR.glob(f"{YOUTUBE_PREFIX}*.transcript")
        if path.is_file()
    )
    built = 0
    failures: list[tuple[str, str]] = []
    for position, video_id in enumerate(transcript_ids, start=1):
        try:
            if dry_run:
                transcript = (
                    NEW_DIR / f"{YOUTUBE_PREFIX}{video_id}.transcript"
                ).read_text(encoding="utf-8")
                paragraphs = create_trunks.transcript_paragraphs(transcript)
                count = len(paragraphs) + (1 if paragraphs else 0)
            else:
                count = create_trunks.build_video_trunks(
                    video_id, output_dir=NEW_DIR
                ) + 1
        except (OSError, ValueError) as exc:
            failures.append((video_id, str(exc)))
            print(
                f"[{position}/{len(transcript_ids)}] {video_id}: FAILED ({exc})",
                file=sys.stderr,
            )
            continue
        built += count
        print(
            f"[{position}/{len(transcript_ids)}] {video_id}: "
            f"{'would create' if dry_run else 'created'} {count} trunk(s)"
        )
    return built, failures


def rebuild_index() -> None:
    """Index all moved trunks in the shared SQLite FTS5 BM25 corpus."""
    import chat

    chunks = chat.load_all_chunks()
    chat.clear_bm25_cache()
    index = chat.get_bm25_index(chunks, prune=True)
    print(
        f"Indexed {len(chunks):,} trunk(s) in the global SQLite BM25 corpus "
        f"({index.document_count:,} document(s))."
    )


def main() -> None:
    parser = argparse.ArgumentParser(
        description=(
            "Move YouTube artifacts into .chat/articles_cache with a "
            "youtube_ prefix and rebuild the SQLite BM25 index."
        )
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="report what would be moved without changing files",
    )
    parser.add_argument(
        "--rebuild-trunk",
        action="store_true",
        help="also regenerate every trunk from its transcript",
    )
    args = parser.parse_args()

    result = move_legacy_files(dry_run=args.dry_run)
    verb = "Would move" if args.dry_run else "Moved"
    print(
        f"{verb} {result['moved']:,} file(s) to {NEW_DIR}; "
        f"{result['unchanged']:,} unchanged, "
        f"{result['conflicts']:,} conflict(s), "
        f"{result['ignored']:,} unrecognized file(s)."
    )
    print(
        f"Removed legacy artifacts: "
        f"{result['removed_embedding']:,} .embedding, "
        f"{result['removed_paragraph_embedding']:,} .paragraph.embedding, "
        f"{result['removed_rewrite']:,} .rewrite."
    )
    if result["conflicts"]:
        print(
            "Conflicts were left in the old directory and were not "
            "overwritten.",
            file=sys.stderr,
        )

    if args.rebuild_trunk:
        built, failures = rebuild_trunks(dry_run=args.dry_run)
        action = "Would rebuild" if args.dry_run else "Rebuilt"
        print(f"{action} {built:,} trunk(s).")
        if failures:
            print(f"{len(failures):,} transcript(s) failed:", file=sys.stderr)
            for video_id, error in failures:
                print(f"  {video_id}: {error}", file=sys.stderr)
            sys.exit(1)

    if args.dry_run:
        return

    try:
        rebuild_index()
    except SystemExit as exc:
        print(
            f"Files were migrated, but the SQLite index could not be "
            f"rebuilt in this Python environment: {exc}\n"
            "Run the migration again with the project's Python environment "
            "(the one that has the requests package).",
            file=sys.stderr,
        )
    if OLD_DIR.exists():
        remaining = sum(1 for path in OLD_DIR.iterdir() if path.is_file())
        if remaining:
            print(
                f"Note: {remaining:,} unrecognized file(s) remain in "
                f"{OLD_DIR}.",
                file=sys.stderr,
            )


if __name__ == "__main__":
    main()
