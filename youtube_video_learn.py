#!/usr/bin/env python3
"""Ingest a YouTube video into the RAG knowledge base.

Usage:
    python3 youtube_video_learn.py <youtube_video_hash>
    python3 youtube_video_learn.py --relearn <youtube_video_hash>
    python3 youtube_video_learn.py https://www.youtube.com/watch?v=T594Cg4sTq4
    python3 youtube_video_learn.py https://youtu.be/T594Cg4sTq4?t=227

Runs, in order:
    1. fetch_youtube_transcript.py
    2. create_trunks.py

With ``--relearn``, the video's generated title, trunks, and legacy rewrite /
embedding files are removed first, then rebuilt from the saved transcript.
The ``.transcript`` file itself is never downloaded again or rewritten.

The transcript is chunked directly into news-style trunks: one overview
chunk (index 0) plus one chunk per paragraph. Rewrite files and embeddings
are no longer generated. A newly learned video is written into the one
global corpus and is immediately available to every question.
"""

import argparse
import re
import subprocess
import sys
from pathlib import Path
from urllib.parse import parse_qs, urlparse

SCRIPT_DIR = Path(__file__).resolve().parent
ARTICLES_DIR = SCRIPT_DIR / ".chat" / "articles_cache"
YOUTUBE_PREFIX = "youtube_"

# Files this pipeline (or an older release) generates for one video.  A
# re-learn replaces all of them; the .transcript is the source of truth and
# is deliberately not in this set.
GENERATED_SUFFIXES = {".trunk", ".title", ".rewrite", ".embedding"}

VIDEO_HASH_RE = re.compile(r"^[A-Za-z0-9_-]{11}$")


def normalize_argv(argv):
    """Let positional values begin with '-' (e.g. video ID -PcBFgkrR2o).

    Recognized switches are moved in front of the positional value, so
    ``--relearn -PcBFgkrR2o`` and ``-- -PcBFgkrR2o`` both work.
    """
    known_switches = {"-h", "--help", "--relearn", "--"}
    switches = [arg for arg in argv if arg in known_switches and arg != "--"]
    positionals = [arg for arg in argv if arg not in known_switches]
    if any(arg.startswith("-") for arg in positionals):
        return [*switches, "--", *positionals]
    return [*switches, *positionals]


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
    ("create_trunks.py", ["<hash>"]),
]


def steps_for(relearn: bool = False) -> list[tuple[str, list[str]]]:
    """Return the pipeline steps for a normal learn or a re-learn.

    A re-learn must not touch the saved transcript, so the fetch step runs
    in title-only mode against the cached transcript instead of downloading
    or rewriting it.
    """
    if not relearn:
        return list(STEPS)
    return [
        ("fetch_youtube_transcript.py", ["--reuse-transcript", "<hash>"]),
        ("create_trunks.py", ["<hash>"]),
    ]


def generated_artifacts(
    youtube_video_hash: str, output_dir: Path = ARTICLES_DIR
) -> list[Path]:
    """Return the generated files for one video, excluding the transcript."""
    storage_id = f"{YOUTUBE_PREFIX}{youtube_video_hash}"
    artifacts = []
    for path in output_dir.glob(f"{storage_id}.*"):
        if path.is_file() and path.suffix in GENERATED_SUFFIXES:
            artifacts.append(path)
    return sorted(artifacts)


def remove_generated_artifacts(
    youtube_video_hash: str, output_dir: Path = ARTICLES_DIR
) -> list[Path]:
    """Delete one video's generated title/trunks/legacy files.

    The saved ``.transcript`` is intentionally left in place.  Old trunk rows
    are also removed from the global SQLite BM25 index so paragraphs that are
    not regenerated cannot linger as stale documents.
    """
    artifacts = generated_artifacts(youtube_video_hash, output_dir)
    trunk_keys: list[str] = []
    index_global = output_dir.resolve() == ARTICLES_DIR.resolve()
    if index_global:
        try:
            import chat

            for path in artifacts:
                if path.suffix == ".trunk":
                    trunk_keys.append(
                        chat.chunk_key(
                            chat.Chunk(
                                video_id=youtube_video_hash,
                                index=0,
                                trunk_path=path,
                            )
                        )
                    )
        except Exception:
            trunk_keys = []

    removed: list[Path] = []
    for path in artifacts:
        try:
            path.unlink()
        except FileNotFoundError:
            continue
        removed.append(path)

    if trunk_keys:
        try:
            import chat

            chat.delete_chunks_in_sqlite(trunk_keys, corpus_key="global")
        except Exception:
            # The files are authoritative; a stale index row is pruned the
            # next time the server rebuilds the global corpus.
            pass
    return removed


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
        description="Fetch a YouTube transcript and chunk it into RAG trunks."
    )
    parser.add_argument(
        "youtube_video_hash",
        help="YouTube video ID or URL, e.g. FTwxtyOEGyM or https://youtu.be/FTwxtyOEGyM",
    )
    parser.add_argument(
        "--relearn",
        action="store_true",
        help="rebuild the title and trunks from the saved transcript without "
        "downloading or rewriting the transcript",
    )
    args = parser.parse_args(normalize_argv(sys.argv[1:]))

    try:
        youtube_video_hash = extract_video_hash(args.youtube_video_hash)
    except ValueError as exc:
        sys.exit(str(exc))

    print(f"Video ID: {youtube_video_hash}")

    if args.relearn:
        removed = remove_generated_artifacts(youtube_video_hash)
        print(
            f"Relearn: removed {len(removed)} generated file(s); "
            "the transcript is unchanged."
        )

    for script, script_args in steps_for(args.relearn):
        run_step(script, script_args, youtube_video_hash)

    print(f"\nDone: {youtube_video_hash} is ready for chat.py")


if __name__ == "__main__":
    main()
