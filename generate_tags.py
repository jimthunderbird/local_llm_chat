#!/usr/bin/env python3
"""Tag YouTube knowledge videos with Ollama.

Usage:
    python3 generate_tags.py
    python3 generate_tags.py --youtube_video_hash <youtube_video_hash>
    python3 generate_tags.py --tag <tag_name>

Logic:
    - Read every tag from ./tags (one tag per file).
    - Read an optional description for each tag from ./tag_descriptions.json.
    - Read every title from ./youtube_transcript/*.title. The video hash is the
      filename stem, and the file contains a short summary of the video.
    - Read a content excerpt from ./youtube_transcript/<hash>.rewrite (or
      .transcript) so classification is based on the video's actual subject
      matter rather than a truncated 20-word title.
    - For each video hash / tag pair not already recorded in ./tags/<tag>, ask
      Ollama model ornith:35b whether the video is related to the tag.
    - When the answer is yes, append the video hash to ./tags/<tag> on its own
      line.

Tag descriptions live in ./tag_descriptions.json, for example:
    {"artificial_intelligence": "AI companies, LLMs, machine learning, ..."}
Tags without a description fall back to their filename with underscores
replaced by spaces.

Optional flags are available for testing:
    --youtube_video_hash HASH
                       Only classify this single video hash.
    --dry-run          Classify but do not modify tag files.
    --limit N          Process at most N video titles.
    --tag TAG          Only process the given tag (repeatable). A tag that
                       does not exist yet is created automatically.
    --model MODEL      Override the Ollama model.
"""

import argparse
import json
import os
import re
import sys
import time
from pathlib import Path

try:
    import requests
except ImportError as exc:  # pragma: no cover - dependency guard
    sys.exit(f"Missing dependency: {exc.name}. Install with: pip install requests")


TAGS_DIR = Path.cwd() / "tags"
TRANSCRIPT_DIR = Path.cwd() / "youtube_transcript"
TAG_DESCRIPTIONS_PATH = Path.cwd() / "tag_descriptions.json"

MODEL = "ornith:35b"
CONTEXT_CHARS = 2500

OLLAMA_HOST = os.environ.get("OLLAMA_HOST", "http://localhost:11434").rstrip("/")
if not OLLAMA_HOST.startswith(("http://", "https://")):
    OLLAMA_HOST = f"http://{OLLAMA_HOST}"

PROMPT = """Classify whether a YouTube video belongs under a topic tag.

Tag: {tag}
Tag description: {description}

Video title/summary: {title}
Video content excerpt:
{excerpt}

Decide whether the video is genuinely related to the tag. Judge what the video
is substantively about, including companies, products, technologies, and people
associated with the tag, not just the framing of the title. If the content
discusses the tag's subject, answer YES.

Reply with exactly one word: YES or NO."""

REQUEST_TIMEOUT = (10, 300)


def load_tag_descriptions():
    """Return the optional {tag: description} map from tag_descriptions.json."""
    if not TAG_DESCRIPTIONS_PATH.is_file():
        return {}

    try:
        data = json.loads(TAG_DESCRIPTIONS_PATH.read_text(encoding="utf-8"))
    except (OSError, ValueError) as exc:
        print(
            f"Warning: could not read {TAG_DESCRIPTIONS_PATH}: {exc}",
            file=sys.stderr,
        )
        return {}

    if not isinstance(data, dict):
        print(
            f"Warning: {TAG_DESCRIPTIONS_PATH} must contain a JSON object",
            file=sys.stderr,
        )
        return {}

    return {
        str(tag): " ".join(str(description).split())
        for tag, description in data.items()
        if str(description).strip()
    }


def describe_tag(tag, descriptions):
    """Return a human-readable description for a tag."""
    if tag in descriptions:
        return descriptions[tag]
    return tag.replace("_", " ")


def load_video_excerpt(video_hash):
    """Return a whitespace-normalized excerpt of the video's content."""
    for suffix in (".rewrite", ".transcript"):
        path = TRANSCRIPT_DIR / f"{video_hash}{suffix}"
        try:
            text = path.read_text(encoding="utf-8")
        except FileNotFoundError:
            continue
        except OSError as exc:
            print(f"Warning: could not read {path}: {exc}", file=sys.stderr)
            continue

        excerpt = " ".join(text.split())
        if excerpt:
            return excerpt[:CONTEXT_CHARS]

    return "(no content excerpt available)"


def load_tags(tag_filter):
    """Return the tag names from ./tags, optionally restricted by --tag.

    Tags explicitly requested with --tag are created when they do not exist.
    """
    if not TAGS_DIR.is_dir():
        if not tag_filter:
            sys.exit(f"Tags directory not found: {TAGS_DIR}")
        try:
            TAGS_DIR.mkdir(parents=True, exist_ok=True)
        except OSError as exc:
            sys.exit(f"Could not create tags directory {TAGS_DIR}: {exc}")

    tags = sorted(
        path.name
        for path in TAGS_DIR.iterdir()
        if path.is_file() and not path.name.startswith(".")
    )
    if tag_filter:
        wanted = []
        for tag in tag_filter:
            tag = tag.strip()
            if not tag or tag in {".", ".."} or "/" in tag or "\\" in tag:
                sys.exit(f"Invalid tag name: {tag!r}")
            if tag not in wanted:
                wanted.append(tag)

        for tag in wanted:
            if tag in tags:
                continue
            tag_path = TAGS_DIR / tag
            try:
                tag_path.touch(exist_ok=True)
            except OSError as exc:
                sys.exit(f"Could not create tag file {tag_path}: {exc}")
            tags.append(tag)
            print(f"Created new tag: {tag}", flush=True)

        wanted_set = set(wanted)
        tags = [tag for tag in tags if tag in wanted_set]

    if not tags:
        sys.exit(f"No tags found in {TAGS_DIR}")
    return tags


def load_title_for_hash(video_hash):
    """Return [(video_hash, title)] for a single video hash."""
    video_hash = video_hash.strip()
    if not video_hash or "/" in video_hash or "\\" in video_hash:
        sys.exit(f"Invalid YouTube video hash: {video_hash!r}")

    title_path = TRANSCRIPT_DIR / f"{video_hash}.title"
    if not title_path.is_file():
        sys.exit(
            f"Title file not found: {title_path}\n"
            "Run learn.py for this video first so its title is available."
        )

    try:
        title = title_path.read_text(encoding="utf-8").strip()
    except OSError as exc:
        sys.exit(f"Could not read {title_path}: {exc}")

    if not title:
        sys.exit(f"Title file is empty: {title_path}")

    return [(video_hash, " ".join(title.split()))]


def load_titles(limit):
    """Return [(video_hash, title), ...] from ./youtube_transcript/*.title."""
    if not TRANSCRIPT_DIR.is_dir():
        sys.exit(f"Transcript directory not found: {TRANSCRIPT_DIR}")

    title_paths = sorted(TRANSCRIPT_DIR.glob("*.title"))
    if limit is not None:
        title_paths = title_paths[:limit]

    titles = []
    for path in title_paths:
        try:
            title = path.read_text(encoding="utf-8").strip()
        except OSError as exc:
            print(f"Warning: could not read {path}: {exc}", file=sys.stderr)
            continue
        if not title:
            print(f"Warning: skipping empty title file: {path}", file=sys.stderr)
            continue
        titles.append((path.stem, " ".join(title.split())))

    if not titles:
        sys.exit(f"No populated .title files found in {TRANSCRIPT_DIR}")
    return titles


def load_tagged_hashes(tag_path):
    """Return the set of video hashes already present in a tag file."""
    try:
        text = tag_path.read_text(encoding="utf-8")
    except FileNotFoundError:
        return set()
    except OSError as exc:
        sys.exit(f"Could not read tag file {tag_path}: {exc}")
    return {line.strip() for line in text.splitlines() if line.strip()}


def append_hash(tag_path, video_hash):
    """Append a video hash to a tag file, ensuring it starts on a new line."""
    existing = ""
    try:
        existing = tag_path.read_text(encoding="utf-8")
    except FileNotFoundError:
        pass

    with tag_path.open("a", encoding="utf-8") as handle:
        if existing and not existing.endswith("\n"):
            handle.write("\n")
        handle.write(video_hash + "\n")


def ask_ollama(model, tag, title, description, excerpt):
    """Return True/False when the model says the video relates to the tag."""
    payload = {
        "model": model,
        "prompt": PROMPT.format(
            tag=tag,
            title=title,
            description=description,
            excerpt=excerpt,
        ),
        "stream": False,
        "think": False,
        "options": {
            "temperature": 0,
            "num_ctx": 4096,
            "num_predict": 16,
        },
    }

    try:
        response = requests.post(
            f"{OLLAMA_HOST}/api/generate",
            json=payload,
            timeout=REQUEST_TIMEOUT,
        )
        response.raise_for_status()
        data = response.json()
    except requests.RequestException as exc:
        raise RuntimeError(
            f"Could not reach Ollama at {OLLAMA_HOST}. Is it running? ({exc})"
        ) from exc
    except ValueError as exc:
        raise RuntimeError(f"Ollama returned invalid JSON: {exc}") from exc

    if data.get("error"):
        raise RuntimeError(f"Ollama error: {data['error']}")

    return parse_answer(data.get("response", ""))


def parse_answer(text):
    """Parse a yes/no classification, returning None when it is unclear."""
    cleaned = re.sub(r"[^a-z\s]", " ", text.lower())
    tokens = cleaned.split()

    if not tokens:
        return None

    # Prefer an explicit yes/no token anywhere in the answer.
    if "yes" in tokens:
        return True
    if "no" in tokens:
        return False

    # Fall back to common related/unrelated phrasing.
    if "unrelated" in tokens or "irrelevant" in tokens:
        return False
    if "related" in tokens or "relevant" in tokens:
        return True
    if "true" in tokens:
        return True
    if "false" in tokens:
        return False
    return None


def classify_with_retry(model, tag, title, description, excerpt, attempts=2):
    """Classify with a small retry loop for transient failures/ambiguous output."""
    last_error = None
    for attempt in range(1, attempts + 1):
        try:
            result = ask_ollama(model, tag, title, description, excerpt)
            if result is not None:
                return result
            last_error = "model answer was not a clear yes/no"
        except RuntimeError as exc:
            last_error = str(exc)

        if attempt < attempts:
            time.sleep(1)

    print(f"Warning: could not classify {tag!r}: {last_error}", file=sys.stderr)
    return None


def main():
    parser = argparse.ArgumentParser(
        description="Tag YouTube knowledge videos with Ollama classifications."
    )
    parser.add_argument(
        "--youtube_video_hash",
        metavar="HASH",
        help="only classify this video hash (uses ./youtube_transcript/<hash>.title)",
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="classify videos but do not write to tag files",
    )
    parser.add_argument(
        "--limit",
        type=int,
        metavar="N",
        help="process at most N title files",
    )
    parser.add_argument(
        "--tag",
        action="append",
        metavar="TAG",
        help="only process this tag (may be repeated)",
    )
    parser.add_argument(
        "--model",
        default=MODEL,
        help=f"Ollama model to use (default: {MODEL})",
    )
    args = parser.parse_args()

    tags = load_tags(args.tag)
    descriptions = load_tag_descriptions()
    if args.youtube_video_hash:
        titles = load_title_for_hash(args.youtube_video_hash)
    else:
        titles = load_titles(args.limit)

    print(
        f"Checking {len(titles)} video(s) against {len(tags)} tag(s) "
        f"with {args.model}"
        + (" (dry run)" if args.dry_run else "")
    )

    tagged = {tag: load_tagged_hashes(TAGS_DIR / tag) for tag in tags}
    excerpts = {}

    checked = 0
    added = 0
    skipped = 0
    unclear = 0

    for video_hash, title in titles:
        for tag in tags:
            if video_hash in tagged[tag]:
                skipped += 1
                continue

            checked += 1
            label = f"[{checked}] {tag}: {video_hash} — {title}"
            print(label, flush=True)

            if video_hash not in excerpts:
                excerpts[video_hash] = load_video_excerpt(video_hash)

            related = classify_with_retry(
                args.model,
                tag,
                title,
                describe_tag(tag, descriptions),
                excerpts[video_hash],
            )
            if related is None:
                unclear += 1
                print("    unclear answer, skipped", flush=True)
                continue

            if not related:
                print("    no", flush=True)
                continue

            print("    yes", flush=True)
            if not args.dry_run:
                append_hash(TAGS_DIR / tag, video_hash)
            tagged[tag].add(video_hash)
            added += 1

    print(
        f"Done. checked={checked} added={added} "
        f"already_tagged={skipped} unclear={unclear}"
    )
    if unclear:
        sys.exit(1)


if __name__ == "__main__":
    main()
