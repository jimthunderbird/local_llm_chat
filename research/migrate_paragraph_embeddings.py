#!/usr/bin/env python3
"""Backfill paragraph-only embeddings for existing trunks.

Usage:
    python3 research/migrate_paragraph_embeddings.py [--tag japan]
    python3 research/migrate_paragraph_embeddings.py --video <hash> [--force]

For every ./youtube_transcript/<video>.<index>.trunk this writes
./youtube_transcript/<video>.<index>.paragraph.embedding containing the
embedding of the <paragraph> body only. chat.py prefers that file for
retrieval, which removes the old per-question paragraph re-embedding call and
the shared-title bias at the same time.

Existing paragraph embeddings are skipped unless --force is given, so the
script is safe to re-run.
"""

from __future__ import annotations

import argparse
import json
import os
import re
import sys
from pathlib import Path

try:
    import requests
except ImportError as exc:  # pragma: no cover - dependency guard
    sys.exit(f"Missing dependency: {exc.name}. Install with: pip install requests")

MODEL = "embeddinggemma-2:570m"
KEEP_ALIVE = "30m"
SCRIPT_DIR = Path(__file__).resolve().parent
PROJECT_DIR = SCRIPT_DIR.parent
OUTPUT_DIR = PROJECT_DIR / "youtube_transcript"
TAG_DIR = PROJECT_DIR / "tags"

TRUNK_FILE = re.compile(r"^(?P<video>[A-Za-z0-9_-]+)\.(?P<index>\d+)\.trunk$")
PARAGRAPH_PATTERN = re.compile(
    r"<paragraph>(.*?)</paragraph>", re.DOTALL | re.IGNORECASE
)

OLLAMA_HOST = os.environ.get("OLLAMA_HOST", "http://localhost:11434").rstrip("/")
if not OLLAMA_HOST.startswith(("http://", "https://")):
    OLLAMA_HOST = f"http://{OLLAMA_HOST}"


def extract_paragraph(content: str) -> str:
    match = PARAGRAPH_PATTERN.search(content)
    return match.group(1).strip() if match else content.strip()


def tag_video_ids(tag: str) -> set[str]:
    path = TAG_DIR / tag
    if not path.is_file():
        sys.exit(f"Tag file not found: {path}")
    return {
        line.strip()
        for line in path.read_text(encoding="utf-8", errors="replace").splitlines()
        if line.strip()
    }


def embed_batch(texts: list[str]) -> list[list[float]]:
    response = requests.post(
        f"{OLLAMA_HOST}/api/embed",
        json={
            "model": MODEL,
            "input": texts,
            "keep_alive": KEEP_ALIVE,
        },
        timeout=(10, 600),
    )
    response.raise_for_status()
    data = response.json()
    if data.get("error"):
        raise RuntimeError(f"Ollama error: {data['error']}")
    embeddings = data.get("embeddings")
    if not embeddings or len(embeddings) != len(texts):
        raise RuntimeError("Ollama returned an unexpected number of embeddings")
    if any(not embedding for embedding in embeddings):
        raise RuntimeError("Ollama returned an empty embedding")
    return [[float(value) for value in embedding] for embedding in embeddings]


def main() -> None:
    parser = argparse.ArgumentParser(
        description="Backfill paragraph-only embeddings for RAG trunks."
    )
    parser.add_argument(
        "--tag",
        help="only process video ids listed in ./tags/<tag> (default: all trunks)",
    )
    parser.add_argument(
        "--video",
        action="append",
        default=[],
        help="only process this video id (repeatable)",
    )
    parser.add_argument(
        "--batch-size",
        type=int,
        default=16,
        help="paragraphs per Ollama /api/embed request (default: 16)",
    )
    parser.add_argument(
        "--limit",
        type=int,
        default=0,
        help="stop after processing N missing paragraphs (0 = no limit)",
    )
    parser.add_argument(
        "--force",
        action="store_true",
        help="recompute paragraph embeddings even if the file exists",
    )
    args = parser.parse_args()

    if not OUTPUT_DIR.is_dir():
        sys.exit(f"Transcript directory not found: {OUTPUT_DIR}")

    selected_videos: set[str] | None = None
    if args.video:
        selected_videos = set(args.video)
    if args.tag:
        tag_ids = tag_video_ids(args.tag)
        selected_videos = (
            tag_ids if selected_videos is None else selected_videos & tag_ids
        )

    pending: list[tuple[Path, Path, str]] = []
    skipped_existing = 0
    for trunk_path in sorted(OUTPUT_DIR.glob("*.trunk")):
        match = TRUNK_FILE.fullmatch(trunk_path.name)
        if not match:
            continue
        if (
            selected_videos is not None
            and match.group("video") not in selected_videos
        ):
            continue

        paragraph_path = trunk_path.with_name(
            trunk_path.name[: -len(".trunk")] + ".paragraph.embedding"
        )
        if paragraph_path.is_file() and not args.force:
            skipped_existing += 1
            continue

        try:
            content = trunk_path.read_text(encoding="utf-8")
        except OSError as exc:
            print(f"Warning: cannot read {trunk_path.name}: {exc}", file=sys.stderr)
            continue
        paragraph = extract_paragraph(content)
        if not paragraph:
            print(f"Warning: {trunk_path.name} has no paragraph text.", file=sys.stderr)
            continue
        pending.append((trunk_path, paragraph_path, paragraph))

    if args.limit and args.limit > 0:
        pending = pending[: args.limit]

    print(
        f"Paragraph embeddings: {len(pending):,} to create, "
        f"{skipped_existing:,} already present."
    )
    if not pending:
        return

    batch_size = max(1, args.batch_size)
    created = 0
    failed = 0
    for start in range(0, len(pending), batch_size):
        batch = pending[start : start + batch_size]
        texts = [paragraph for _, _, paragraph in batch]
        try:
            embeddings = embed_batch(texts)
        except (requests.RequestException, RuntimeError, ValueError) as exc:
            failed += len(batch)
            names = ", ".join(item[0].name for item in batch[:3])
            print(f"ERROR batch at {start} ({names}...): {exc}", file=sys.stderr)
            continue

        for (trunk_path, paragraph_path, _), embedding in zip(batch, embeddings):
            paragraph_path.write_text(json.dumps(embedding), encoding="utf-8")
            created += 1

        progress = min(start + batch_size, len(pending))
        print(f"[{progress:,}/{len(pending):,}] created {created:,}", flush=True)

    print(
        f"Done: {created:,} paragraph embedding(s) written, "
        f"{failed:,} failed, {skipped_existing:,} skipped."
    )
    print(f"Output directory: {OUTPUT_DIR}")


if __name__ == "__main__":
    main()
