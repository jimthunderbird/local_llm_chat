#!/usr/bin/env python3
"""Migrate the YouTube corpus to the transcript-only learning pipeline.

Usage:
    python3 youtube_video_learning_migration.py
    python3 youtube_video_learning_migration.py --dry-run

What it does:
    1. Removes every .embedding (including .paragraph.embedding), .trunk, and
       .rewrite file in ./.chat/youtube_transcripts.
    2. Walks every existing .transcript file and rebuilds news-style trunks:
       one overview trunk (index 0) plus one trunk per transcript paragraph.

New videos learned with youtube_video_learn.py already use this pipeline; the
migration is for the older corpus that still has rewrite/embedding artifacts.
"""

import argparse
import sys
from pathlib import Path

import create_trunks

OUTPUT_DIR = Path.cwd() / ".chat" / "youtube_transcripts"
REMOVED_SUFFIXES = (".embedding", ".trunk", ".rewrite")


def remove_legacy_artifacts(dry_run: bool = False) -> dict[str, int]:
    """Delete old generated files and return a per-suffix count."""
    removed = {suffix: 0 for suffix in REMOVED_SUFFIXES}
    if not OUTPUT_DIR.is_dir():
        return removed

    for path in sorted(OUTPUT_DIR.iterdir()):
        if not path.is_file():
            continue
        for suffix in REMOVED_SUFFIXES:
            if path.name.endswith(suffix):
                if not dry_run:
                    path.unlink()
                removed[suffix] += 1
                break
    return removed


def transcript_video_ids() -> list[str]:
    """Return the video id of every transcript in the corpus."""
    if not OUTPUT_DIR.is_dir():
        return []
    return sorted(
        path.name[: -len(".transcript")]
        for path in OUTPUT_DIR.glob("*.transcript")
        if path.is_file()
    )


def main() -> None:
    parser = argparse.ArgumentParser(
        description=(
            "Remove legacy rewrite/embedding/trunk artifacts and rebuild "
            "news-style trunks from every YouTube transcript."
        )
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="report what would be removed and rebuilt without changing files",
    )
    args = parser.parse_args()

    if not OUTPUT_DIR.is_dir():
        sys.exit(f"Transcript directory not found: {OUTPUT_DIR}")

    removed = remove_legacy_artifacts(dry_run=args.dry_run)
    verb = "Would remove" if args.dry_run else "Removed"
    print(
        f"{verb} {removed['.embedding']:,} embedding file(s), "
        f"{removed['.trunk']:,} trunk file(s), and "
        f"{removed['.rewrite']:,} rewrite file(s)."
    )

    video_ids = transcript_video_ids()
    if not video_ids:
        print("No .transcript files found; nothing to rebuild.")
        return

    failures: list[tuple[str, str]] = []
    built_trunks = 0
    for position, video_id in enumerate(video_ids, start=1):
        if args.dry_run:
            try:
                transcript = (
                    OUTPUT_DIR / f"{video_id}.transcript"
                ).read_text(encoding="utf-8")
                paragraphs = create_trunks.transcript_paragraphs(transcript)
            except (OSError, ValueError) as exc:
                failures.append((video_id, str(exc)))
                continue
            built_trunks += len(paragraphs) + (1 if paragraphs else 0)
            print(
                f"[{position}/{len(video_ids)}] {video_id}: would create "
                f"{len(paragraphs) + 1} trunk(s)"
            )
            continue

        try:
            paragraph_count = create_trunks.build_video_trunks(
                video_id, output_dir=OUTPUT_DIR
            )
        except (OSError, ValueError) as exc:
            failures.append((video_id, str(exc)))
            print(
                f"[{position}/{len(video_ids)}] {video_id}: FAILED ({exc})",
                file=sys.stderr,
            )
            continue

        built_trunks += paragraph_count + 1
        print(
            f"[{position}/{len(video_ids)}] {video_id}: "
            f"{paragraph_count + 1} trunk(s)"
        )

    action = "Would rebuild" if args.dry_run else "Rebuilt"
    print(
        f"\n{action} {built_trunks:,} trunk(s) for "
        f"{len(video_ids) - len(failures):,} of {len(video_ids):,} transcript(s)."
    )
    if failures:
        print(f"{len(failures):,} transcript(s) failed:", file=sys.stderr)
        for video_id, error in failures:
            print(f"  {video_id}: {error}", file=sys.stderr)
        sys.exit(1)


if __name__ == "__main__":
    main()
