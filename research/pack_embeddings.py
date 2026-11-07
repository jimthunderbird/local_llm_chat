#!/usr/bin/env python3
"""Pack a tag's paragraph embeddings into one mmap-able float32 matrix.

Usage:
    python3 research/pack_embeddings.py --tag japan
    python3 research/pack_embeddings.py --all

Writes:
    .chat/packs/<tag>.npy   normalized paragraph vectors, shape (n, dim)
    .chat/packs/<tag>.json  row metadata and a freshness fingerprint

chat.py uses the pack automatically when it is present and fresh, which makes
startup, /tag, and /refresh fast and reduces Python memory. If the pack is
missing or stale, chat.py falls back to the original JSON embedding loader.
"""

from __future__ import annotations

import argparse
import json
import re
import sys
from pathlib import Path

try:
    import numpy as np
except ImportError:  # pragma: no cover - optional optimization
    sys.exit("Missing dependency: numpy. Install with: pip install numpy")

SCRIPT_DIR = Path(__file__).resolve().parent
PROJECT_DIR = SCRIPT_DIR.parent
OUTPUT_DIR = PROJECT_DIR / ".chat" / "youtube_transcripts"
TAG_DIR = PROJECT_DIR / "tags"
PACK_DIR = PROJECT_DIR / ".chat" / "packs"

TRUNK_EMBEDDING = re.compile(
    r"^(?P<video>[A-Za-z0-9_-]+)\.(?P<index>\d+)\.embedding$"
)


def tag_names() -> list[str]:
    try:
        return sorted(
            entry.name
            for entry in TAG_DIR.iterdir()
            if entry.is_file() and not entry.name.startswith(".")
        )
    except OSError:
        return []


def tag_video_ids(tag: str) -> set[str]:
    path = TAG_DIR / tag
    if not path.is_file():
        sys.exit(f"Tag file not found: {path}")
    return {
        line.strip()
        for line in path.read_text(encoding="utf-8", errors="replace").splitlines()
        if line.strip()
    }


def embedding_paths(video_ids: set[str]) -> list[Path]:
    paths = []
    for path in OUTPUT_DIR.glob("*.embedding"):
        match = TRUNK_EMBEDDING.fullmatch(path.name)
        if match and match.group("video") in video_ids:
            paths.append(path)
    return sorted(paths, key=lambda p: (p.name.split(".", 1)[0], p.name))


def load_vector(path: Path) -> list[float]:
    values = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(values, list) or not values:
        raise ValueError("empty or non-list embedding")
    return [float(value) for value in values]


def normalized(values: list[float]) -> np.ndarray:
    vector = np.asarray(values, dtype=np.float32)
    norm = float(np.linalg.norm(vector))
    if norm == 0.0:
        raise ValueError("zero embedding")
    return vector / norm


def pack_tag(tag: str) -> bool:
    video_ids = tag_video_ids(tag)
    paths = embedding_paths(video_ids)
    if not paths:
        print(f"{tag}: no embeddings found; skipped.", file=sys.stderr)
        return False

    rows: list[np.ndarray] = []
    items: list[list] = []
    expected_dim: int | None = None
    paragraph_fallback = 0
    skipped = 0

    for path in paths:
        match = TRUNK_EMBEDDING.fullmatch(path.name)
        assert match is not None
        trunk_path = path.with_name(path.name[: -len(".embedding")] + ".trunk")
        if not trunk_path.is_file():
            skipped += 1
            continue

        paragraph_path = path.with_name(
            path.name[: -len(".embedding")] + ".paragraph.embedding"
        )
        source_path = paragraph_path if paragraph_path.is_file() else path
        if source_path is path:
            paragraph_fallback += 1
        try:
            vector = normalized(load_vector(source_path))
        except (OSError, ValueError, TypeError, json.JSONDecodeError) as exc:
            print(f"Warning: cannot read {source_path.name}: {exc}", file=sys.stderr)
            skipped += 1
            continue

        if expected_dim is None:
            expected_dim = len(vector)
        elif len(vector) != expected_dim:
            print(
                f"Warning: {source_path.name} has {len(vector)} dimensions, "
                f"expected {expected_dim}; skipped.",
                file=sys.stderr,
            )
            skipped += 1
            continue

        rows.append(vector)
        items.append(
            [
                match.group("video"),
                int(match.group("index")),
                trunk_path.name,
            ]
        )

    if not rows:
        print(f"{tag}: no usable embeddings; skipped.", file=sys.stderr)
        return False

    matrix = np.vstack(rows).astype(np.float32)
    source_mtimes = []
    for path in paths:
        try:
            source_mtimes.append(path.stat().st_mtime_ns)
            paragraph_path = path.with_name(
                path.name[: -len(".embedding")] + ".paragraph.embedding"
            )
            if paragraph_path.is_file():
                source_mtimes.append(paragraph_path.stat().st_mtime_ns)
        except OSError:
            pass

    PACK_DIR.mkdir(parents=True, exist_ok=True)
    matrix_path = PACK_DIR / f"{tag}.npy"
    meta_path = PACK_DIR / f"{tag}.json"
    np.save(matrix_path, matrix)
    metadata = {
        "version": 1,
        "tag": tag,
        "dim": int(matrix.shape[1]),
        "count": int(matrix.shape[0]),
        "source_file_count": len(paths),
        "source_max_mtime_ns": max(source_mtimes) if source_mtimes else 0,
        "paragraph_fallback": paragraph_fallback,
        "skipped": skipped,
        "items": items,
    }
    meta_path.write_text(json.dumps(metadata), encoding="utf-8")
    print(
        f"{tag}: packed {len(rows):,} rows x {matrix.shape[1]} dims "
        f"({matrix.nbytes / 1024 / 1024:.1f} MiB) -> {matrix_path}"
    )
    if paragraph_fallback:
        print(
            f"  {paragraph_fallback:,} row(s) used the trunk embedding "
            "because a paragraph embedding was missing."
        )
    if skipped:
        print(f"  {skipped:,} row(s) skipped.")
    return True


def main() -> None:
    parser = argparse.ArgumentParser(
        description="Pack a tag's paragraph embeddings for fast mmap loading."
    )
    parser.add_argument("--tag", help="tag to pack")
    parser.add_argument(
        "--all",
        action="store_true",
        help="pack every tag file in ./tags",
    )
    args = parser.parse_args()

    if not OUTPUT_DIR.is_dir():
        sys.exit(f"Transcript directory not found: {OUTPUT_DIR}")

    if args.all:
        tags = tag_names()
    elif args.tag:
        tags = [args.tag]
    else:
        parser.error("provide --tag <tag> or --all")

    failures = 0
    for tag in tags:
        if not (TAG_DIR / tag).is_file():
            print(f"Tag file not found: {TAG_DIR / tag}", file=sys.stderr)
            failures += 1
            continue
        if not pack_tag(tag):
            failures += 1
    if failures:
        sys.exit(f"{failures} tag(s) failed to pack")


if __name__ == "__main__":
    main()
