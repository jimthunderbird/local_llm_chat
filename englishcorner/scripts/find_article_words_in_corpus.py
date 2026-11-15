#!/usr/bin/env python3
"""Find article words that also occur in a frequency-ranked English corpus.

Usage
-----
    python find_article_words_in_corpus.py <article_url> --corpus 10k
    python find_article_words_in_corpus.py <article_url> 20k --json
    python find_article_words_in_corpus.py <article_url> --corpus 10k..20k
    python find_article_words_in_corpus.py <article_url> --corpus 10k..10.5k
    python find_article_words_in_corpus.py <article_url> --corpus 10k..10.05k
    python find_article_words_in_corpus.py <article_url> --corpus 40k..50k

The article body is fetched with ``news_fetch.php --article <url>``.  The
selected corpus is downloaded into ``englishcorner/corpus`` on first use.  The
article words and the selected corpus vocabulary are intersected directly in
this script; ``check_words_in_both_texts.py`` is not used.

A corpus may be a plain name (``10k``, ``20k``, ``30k``, ``50k``) or a
range such as ``10k..20k``, ``10k..10.5k``, ``10k..10.05k`` or
``40k..50k``.  A range selects the words that are outside the intersection
of the two boundary corpora: words present in the ``top <end>`` list or the
``top <start>`` list, but not in both.  For example, ``10k..20k`` means the
words that occur in the 10k or 20k list but are not shared by both lists.
The article intersection is then applied to that vocabulary.  Named corpora
are used directly for the well-known offsets (10k, 20k, 30k, 50k); other
offsets such as 10.5k use the first N words of the smallest corpus that
covers them.
"""

from __future__ import annotations

import argparse
import json
import os
import re
import shutil
import subprocess
import sys
import tempfile
import time
import urllib.error
import urllib.request
from collections.abc import Iterator
from decimal import Decimal, InvalidOperation
from pathlib import Path

PROJECT_DIRECTORY = Path(__file__).resolve().parents[2]
MAIN_CORPUS_DIRECTORY = PROJECT_DIRECTORY / "englishcorner" / "corpus"
NEWS_FETCH_SCRIPT = PROJECT_DIRECTORY / "news_fetch.php"

# A word is one or more Unicode letters/digits, optionally joined by an
# apostrophe or hyphen (so "don't" and "long-stay" stay intact).
WORD_RE = re.compile(r"[^\W_]+(?:['\u2019\-][^\W_]+)*", re.UNICODE)

CORPORA: dict[str, dict[str, object]] = {
    "10k": {
        "url": (
            "https://raw.githubusercontent.com/first20hours/"
            "google-10000-english/refs/heads/master/"
            "google-10000-english-usa.txt"
        ),
        "filename": "10k_english_words.txt",
        "size": 10_000,
    },
    "20k": {
        "url": (
            "https://raw.githubusercontent.com/david47k/top-english-wordlists/"
            "refs/heads/master/top_english_words_mixed_20000.txt"
        ),
        "filename": "20k_english_words.txt",
        "size": 20_000,
    },
    "30k": {
        "url": (
            "https://raw.githubusercontent.com/arstgit/high-frequency-vocabulary/"
            "refs/heads/master/30k.txt"
        ),
        "filename": "30k_english_words.txt",
        "size": 30_000,
    },
    "50k": {
        "url": (
            "https://raw.githubusercontent.com/david47k/top-english-wordlists/"
            "refs/heads/master/top_english_words_mixed_50000.txt"
        ),
        "filename": "50k_english_words.txt",
        "size": 50_000,
    },
}

CORPUS_RANGE_RE = re.compile(
    r"^\s*(?P<start>\d+(?:\.\d+)?k?)\s*\.\.\s*(?P<end>\d+(?:\.\d+)?k?)\s*$",
    re.IGNORECASE,
)

USER_AGENT = "local-llm-chat-english-corner/1.0"


def _read_corpus_words(path: Path) -> list[str]:
    """Read a whitespace-separated corpus file in its original order."""
    return path.read_text(encoding="utf-8").split()


def _download(url: str, destination: Path, attempts: int = 3) -> None:
    """Download ``url`` to ``destination`` atomically, with small retries."""
    destination.parent.mkdir(parents=True, exist_ok=True)
    request = urllib.request.Request(url, headers={"User-Agent": USER_AGENT})
    last_error: Exception | None = None

    for attempt in range(1, attempts + 1):
        temporary: Path | None = None
        try:
            with urllib.request.urlopen(request, timeout=60) as response:
                with tempfile.NamedTemporaryFile(
                    mode="wb",
                    dir=destination.parent,
                    prefix=f".{destination.name}.",
                    suffix=".part",
                    delete=False,
                ) as handle:
                    temporary = Path(handle.name)
                    shutil.copyfileobj(response, handle)
            if temporary.stat().st_size == 0:
                raise OSError("downloaded corpus is empty")
            os.replace(temporary, destination)
            return
        except (OSError, urllib.error.URLError) as exc:
            last_error = exc
            if temporary is not None:
                temporary.unlink(missing_ok=True)
            if attempt < attempts:
                time.sleep(attempt)

    raise RuntimeError(f"could not download corpus from {url}: {last_error}")


def _parse_offset(value: str) -> int:
    """Parse a rank offset such as ``10k``, ``10.5k`` or ``10500``."""
    text = value.strip().lower()
    multiplier = Decimal(1)
    if text.endswith("k"):
        text = text[:-1]
        multiplier = Decimal(1000)
    try:
        number = Decimal(text) * multiplier
    except InvalidOperation as exc:
        raise ValueError(f"invalid corpus offset: {value}") from exc
    if number < 0:
        raise ValueError(f"corpus offset must be non-negative: {value}")
    if number != number.to_integral_value():
        raise ValueError(f"corpus offset must be a whole number of words: {value}")
    return int(number)


def parse_corpus_range(spec: str) -> tuple[int, int] | None:
    """Return ``(start, end)`` for a range spec, or ``None`` for a name."""
    match = CORPUS_RANGE_RE.match(spec)
    if not match:
        return None
    start = _parse_offset(match.group("start"))
    end = _parse_offset(match.group("end"))
    if end <= start:
        raise ValueError(f"corpus range end must be greater than start: {spec}")
    return start, end


def _base_corpus_for_end(end: int) -> str:
    """Pick the smallest corpus whose size covers ``end``."""
    candidates = sorted(CORPORA, key=lambda name: int(CORPORA[name]["size"]))
    for name in candidates:
        if int(CORPORA[name]["size"]) >= end:
            return name
    largest = candidates[-1]
    raise ValueError(
        f"corpus range upper bound {end} exceeds the largest available "
        f"corpus ({largest}, {CORPORA[largest]['size']} words)"
    )


def _top_words(offset: int) -> list[str]:
    """Return the words belonging to the "top <offset>" word list.

    Named corpora are used for the offsets they define (10k, 20k, 30k, 50k).
    Any other offset is the prefix of the smallest corpus that covers it.
    """
    for name, entry in CORPORA.items():
        if int(entry["size"]) == offset:
            return _read_corpus_words(ensure_corpus(name))

    base_name = _base_corpus_for_end(offset)
    words = _read_corpus_words(ensure_corpus(base_name))
    if offset > len(words):
        raise ValueError(
            f"corpus offset {offset} exceeds the {base_name} corpus "
            f"({len(words)} words)"
        )
    return words[:offset]


def _symmetric_difference_corpus(start: int, end: int) -> list[str]:
    """Return words outside the intersection of the two boundary corpora.

    The prompt defines a range as the corpus vocabulary that is *outside the
    intersect* of the start and end word lists.  That is their symmetric
    difference: words in the end list but not the start list, followed by
    words in the start list but not the end list.  The end list's spelling and
    ranking are preserved first, and duplicates are removed
    case-insensitively.
    """
    start_words = _top_words(start)
    end_words = _top_words(end)
    start_keys = {word.casefold() for word in start_words}
    end_keys = {word.casefold() for word in end_words}

    selected: list[str] = []
    seen: set[str] = set()
    for words, other_keys in ((end_words, start_keys), (start_words, end_keys)):
        for word in words:
            key = word.casefold()
            if key in seen or key in other_keys:
                continue
            seen.add(key)
            selected.append(word)
    return selected


def resolve_corpus(spec: str) -> tuple[set[str], str]:
    """Resolve a corpus name or rank range to a normalized word set.

    For a range, the set contains the words outside the intersection of the
    two boundary corpora, exactly as described by the task:
    ``10k..20k`` selects words that are in the 10k list or the 20k list but
    not in both.
    """
    range_bounds = parse_corpus_range(spec)
    if range_bounds is None:
        words = _read_corpus_words(ensure_corpus(spec))
        label = spec
    else:
        start, end = range_bounds
        words = _symmetric_difference_corpus(start, end)
        label = (
            f"{spec} ({len(words):,} words outside the intersection of the "
            f"top {start:,} and top {end:,} lists)"
        )

    vocabulary = {word.casefold() for word in words if word.strip()}
    if not vocabulary:
        raise ValueError(f"corpus {spec} contains no words")
    return vocabulary, label


def ensure_corpus(corpus: str) -> Path:
    """Return the local corpus path, downloading it when necessary."""
    if corpus not in CORPORA:
        raise ValueError(f"unknown corpus: {corpus}")

    entry = CORPORA[corpus]
    destination = MAIN_CORPUS_DIRECTORY / str(entry["filename"])
    if destination.is_file() and destination.stat().st_size > 0:
        return destination

    print(f"Downloading {corpus} corpus from {entry['url']} ...", file=sys.stderr)
    _download(entry["url"], destination)
    print(f"Saved corpus to {destination}", file=sys.stderr)
    return destination


def fetch_article_content(article_url: str, timeout: float = 180.0) -> str:
    """Fetch an article's readable text through news_fetch.php."""
    if not NEWS_FETCH_SCRIPT.is_file():
        raise RuntimeError(f"missing news fetcher: {NEWS_FETCH_SCRIPT}")

    php = shutil.which("php")
    if not php:
        raise RuntimeError("php is not installed or not on PATH")

    command = [php, str(NEWS_FETCH_SCRIPT), "--json", "--article", article_url]
    try:
        result = subprocess.run(
            command,
            cwd=PROJECT_DIRECTORY,
            capture_output=True,
            text=True,
            timeout=timeout,
            check=False,
        )
    except subprocess.TimeoutExpired as exc:
        raise RuntimeError(
            f"news_fetch.php timed out after {timeout:g}s for {article_url}"
        ) from exc

    stdout = result.stdout.strip()
    if result.returncode != 0 and not stdout:
        detail = result.stderr.strip() or "no output"
        raise RuntimeError(f"news_fetch.php failed ({result.returncode}): {detail}")

    try:
        payload = json.loads(stdout)
    except json.JSONDecodeError:
        # Older/custom fetchers may emit plain text; keep it as a fallback.
        if stdout:
            return stdout
        detail = result.stderr.strip() or "no article content"
        raise RuntimeError(detail) from None

    if isinstance(payload, dict):
        paragraphs = payload.get("paragraphs") or []
        text = "\n\n".join(
            str(paragraph).strip()
            for paragraph in paragraphs
            if str(paragraph).strip()
        )
    elif isinstance(payload, list):
        text = "\n\n".join(
            str(item).strip() for item in payload if str(item).strip()
        )
    else:
        text = ""

    if not text:
        detail = result.stderr.strip()
        suffix = f": {detail}" if detail else ""
        raise RuntimeError(f"news_fetch.php returned no article content{suffix}")
    return text


def article_word_sequence(text: str) -> Iterator[str]:
    """Yield unique article words in first-appearance order, case-folded."""
    seen: set[str] = set()
    for match in WORD_RE.finditer(text):
        word = match.group(0).casefold()
        if word in seen:
            continue
        seen.add(word)
        yield word


def find_article_words(
    article_content: str, corpus_vocabulary: set[str]
) -> list[str]:
    """Return article words that are inside ``corpus_vocabulary``.

    The result keeps the article's first-appearance order and has no
    duplicates, matching the previous helper's output.
    """
    return [
        word
        for word in article_word_sequence(article_content)
        if word in corpus_vocabulary
    ]


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description=(
            "Return the words in an article that also appear in a common "
            "English word corpus."
        )
    )
    parser.add_argument("article_url", help="Article URL to fetch and analyze.")
    parser.add_argument(
        "corpus_positional",
        nargs="?",
        help=(
            "Corpus to use (also accepted with --corpus): 10k, 20k, 30k, "
            "50k, or a range such as 10k..20k / 20k..30k / 30k..50k. A "
            "range selects words outside the intersection of the two "
            "boundary corpora (in one list but not both). Default: 10k."
        ),
    )
    parser.add_argument(
        "--corpus",
        help=(
            "Corpus to use: 10k, 20k, 30k, 50k, or a range such as "
            "10k..20k / 20k..30k / 30k..50k. A range selects words "
            "outside the intersection of the two boundary corpora: words "
            "in the end list or the start list, but not both. Default: 10k."
        ),
    )
    parser.add_argument(
        "--json",
        action="store_true",
        help="Print a JSON array instead of one word per line.",
    )
    parser.add_argument(
        "--sort",
        action="store_true",
        help="Sort words alphabetically instead of article order.",
    )
    parser.add_argument(
        "--limit",
        type=int,
        help="Only print the first N matching words.",
    )
    parser.add_argument(
        "--count-only",
        action="store_true",
        help="Print only the number of matching words.",
    )
    parser.add_argument(
        "--timeout",
        type=float,
        default=180.0,
        help="Article fetch timeout in seconds (default: 180).",
    )
    return parser


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)

    corpus = args.corpus or args.corpus_positional or "10k"
    if args.corpus and args.corpus_positional:
        if args.corpus.strip().lower() != args.corpus_positional.strip().lower():
            parser.error("--corpus and the positional corpus disagree")
    if args.limit is not None and args.limit < 0:
        parser.error("--limit must be zero or greater")

    try:
        corpus_vocabulary, corpus_label = resolve_corpus(corpus)
        article_content = fetch_article_content(args.article_url, args.timeout)
        words = find_article_words(article_content, corpus_vocabulary)
    except (RuntimeError, ValueError) as exc:
        parser.exit(1, f"error: {exc}\n")

    if args.sort:
        words.sort()
    if args.limit is not None:
        words = words[: args.limit]

    print(f"Corpus: {corpus_label}", file=sys.stderr)
    if args.count_only:
        print(len(words))
    elif args.json:
        print(json.dumps(words, ensure_ascii=False))
    else:
        for word in words:
            print(word)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
