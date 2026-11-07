#!/usr/bin/env python3
"""Create embeddings for all RAG trunks of a YouTube video with Ollama.

Usage:
    python3 create_embeddings.py <youtube_video_hash>

Reads:
    ./.chat/youtube_transcripts/<youtube_video_hash>.<index>.trunk

Writes:
    ./.chat/youtube_transcripts/<youtube_video_hash>.<index>.embedding
    ./.chat/youtube_transcripts/<youtube_video_hash>.<index>.paragraph.embedding
    (JSON arrays of floats; the paragraph file is used for retrieval so the
    shared video title does not bias the ranking)
"""

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

def normalize_argv(argv):
    """Let positional values begin with '-' (e.g. video ID -PcBFgkrR2o)."""
    if argv and argv[0].startswith("-") and argv[0] not in {"-h", "--help", "--"}:
        return ["--", *argv]
    return argv


MODEL = "embeddinggemma-2:570m"
KEEP_ALIVE = "30m"
OUTPUT_DIR = Path.cwd() / ".chat" / "youtube_transcripts"
PARAGRAPH_PATTERN = re.compile(
    r"<paragraph>(.*?)</paragraph>", re.DOTALL | re.IGNORECASE
)

OLLAMA_HOST = os.environ.get("OLLAMA_HOST", "http://localhost:11434").rstrip("/")
if not OLLAMA_HOST.startswith(("http://", "https://")):
    OLLAMA_HOST = f"http://{OLLAMA_HOST}"


def create_embeddings(contents: list[str]) -> list[list[float]]:
    """Ask Ollama for embeddings of one or more texts in a single request."""
    payload = {
        "model": MODEL,
        "input": contents,
        "keep_alive": KEEP_ALIVE,
    }

    try:
        response = requests.post(
            f"{OLLAMA_HOST}/api/embed",
            json=payload,
            timeout=(10, 300),
        )
        response.raise_for_status()
        data = response.json()
    except requests.RequestException as exc:
        raise RuntimeError(
            f"Could not reach Ollama at {OLLAMA_HOST}. Is it running? ({exc})"
        ) from exc

    if data.get("error"):
        raise RuntimeError(f"Ollama error: {data['error']}")

    embeddings = data.get("embeddings")
    if not embeddings or len(embeddings) != len(contents):
        raise RuntimeError("Ollama returned an unexpected number of embeddings")
    if any(not embedding for embedding in embeddings):
        raise RuntimeError("Ollama returned an empty embedding")

    return [[float(value) for value in embedding] for embedding in embeddings]


def create_embedding(content: str) -> list[float]:
    """Ask Ollama for an embedding of the given text."""
    return create_embeddings([content])[0]


def extract_paragraph(content: str) -> str:
    """Return the trunk's paragraph body, excluding the XML/title wrapper."""
    match = PARAGRAPH_PATTERN.search(content)
    return match.group(1).strip() if match else content.strip()


def remove_existing_embeddings(video_id: str) -> int:
    """Delete every ./.chat/youtube_transcripts/<video_id>.*.embedding file."""
    removed = 0
    for path in OUTPUT_DIR.glob(f"{video_id}.*.embedding"):
        try:
            path.unlink()
            removed += 1
        except OSError as exc:
            print(f"Warning: cannot remove {path.name}: {exc}", file=sys.stderr)
    return removed


def find_trunks(video_id: str) -> list[tuple[int, Path]]:
    """Return (index, path) pairs sorted numerically by trunk index."""
    pattern = re.compile(rf"^{re.escape(video_id)}\.(\d+)\.trunk$")
    trunks = []
    for path in OUTPUT_DIR.glob(f"{video_id}.*.trunk"):
        match = pattern.fullmatch(path.name)
        if match:
            trunks.append((int(match.group(1)), path))
    trunks.sort(key=lambda item: item[0])
    return trunks


def main() -> None:
    parser = argparse.ArgumentParser(
        description="Create embeddings for all trunks of a YouTube video."
    )
    parser.add_argument("youtube_video_hash", help="YouTube video ID, e.g. FTwxtyOEGyM")
    args = parser.parse_args(normalize_argv(sys.argv[1:]))

    video_id = args.youtube_video_hash.strip()
    if not re.fullmatch(r"[A-Za-z0-9_-]+", video_id):
        sys.exit(f"Invalid YouTube video hash: {video_id!r}")

    removed = remove_existing_embeddings(video_id)
    if removed:
        print(f"Removed {removed} existing embedding file(s) for {video_id}")

    trunks = find_trunks(video_id)
    if not trunks:
        sys.exit(
            f"No trunk files found for {video_id} in {OUTPUT_DIR}\n"
            f"Run: python3 create_trunks.py {video_id}"
        )

    failures = 0
    for index, trunk_path in trunks:
        content = trunk_path.read_text(encoding="utf-8")
        embedding_path = OUTPUT_DIR / f"{video_id}.{index}.embedding"
        paragraph_path = OUTPUT_DIR / f"{video_id}.{index}.paragraph.embedding"
        paragraph = extract_paragraph(content)
        texts = [content]
        if paragraph and paragraph != content:
            texts.append(paragraph)
        try:
            embeddings = create_embeddings(texts)
        except Exception as exc:
            failures += 1
            print(f"[{index}] ERROR {trunk_path.name}: {exc}", file=sys.stderr)
            continue

        embedding = embeddings[0]
        paragraph_embedding = embeddings[1] if len(embeddings) > 1 else embedding

        embedding_path.write_text(json.dumps(embedding), encoding="utf-8")
        paragraph_path.write_text(
            json.dumps(paragraph_embedding), encoding="utf-8"
        )
        print(
            f"[{index}] Saved {embedding_path.name} and "
            f"{paragraph_path.name} ({len(embedding)} dimensions)"
        )

    total = len(trunks)
    if failures:
        sys.exit(f"Created embeddings for {total - failures}/{total} trunks ({failures} failed)")
    print(f"Created embeddings for {total} trunks for {video_id}")
    print(f"Output directory: {OUTPUT_DIR}")


if __name__ == "__main__":
    main()
