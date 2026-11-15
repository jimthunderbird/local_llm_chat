#!/usr/bin/env python3
"""Return the unique words that occur in both an input text and a corpus list.

The corpus is a whitespace-separated list of words.  Matching is
case-insensitive and uses a single set intersection, so both inputs are
scanned only once and memory use is proportional to the number of unique
words rather than the size of the text.

Examples
--------
    python check_words_in_both_texts.py "The quick brown fox" "quick fox dog"
    quick
    fox

Arguments may also be a path prefixed with ``@`` (useful when a caller has
more text than the operating system allows in one argv entry), and ``-``
reads the input text from stdin:

    python check_words_in_both_texts.py @article.txt @corpus.txt
    cat article.txt | python check_words_in_both_texts.py - @corpus.txt
"""

from __future__ import annotations

import argparse
import json
import re
import sys
from pathlib import Path

# A word is one or more Unicode letters/digits, optionally joined by an
# apostrophe or hyphen (so "don't" and "long-stay" stay intact).
_WORD_RE = re.compile(r"[^\W_]+(?:['\u2019\-][^\W_]+)*", re.UNICODE)
_CORPUS_SPLIT_RE = re.compile(r"\s+")


def _as_text(value: str, *, stdin_label: str) -> str:
    """Resolve a CLI value that may be literal text or a file reference."""
    if value == "-":
        return sys.stdin.read()
    if value.startswith("@"):
        path = Path(value[1:])
        try:
            return path.read_text(encoding="utf-8")
        except OSError as exc:
            raise SystemExit(f"error: cannot read {path}: {exc}") from exc
    return value


def _normalize(word: str) -> str:
    return word.casefold()


def _article_words(text: str) -> dict[str, str]:
    """Map normalized word -> first spelling seen in the article."""
    words: dict[str, str] = {}
    for match in _WORD_RE.finditer(text):
        original = match.group(0)
        normalized = _normalize(original)
        if normalized not in words:
            words[normalized] = original
    return words


def _corpus_word_set(corpus_words: str) -> set[str]:
    """Build a set from the whitespace-separated corpus word list."""
    words: set[str] = set()
    for token in _CORPUS_SPLIT_RE.split(corpus_words):
        token = token.strip()
        if not token:
            continue
        # Be tolerant of punctuation in a corpus file while still treating
        # ordinary words as a single token.
        matches = _WORD_RE.findall(token)
        if matches:
            for match in matches:
                words.add(_normalize(match))
        else:
            words.add(_normalize(token))
    return words


def words_in_both(input_content: str, corpus_words: str) -> list[str]:
    """Return unique article words also present in the corpus.

    The result is in order of first appearance in the article by default.
    """
    article = _article_words(input_content)
    corpus = _corpus_word_set(corpus_words)
    return [word for word in article if word in corpus]


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description=(
            "Return the unique words that appear in both a text and a "
            "whitespace-separated corpus word list."
        )
    )
    parser.add_argument(
        "input_content",
        nargs="?",
        help="Text to inspect, '-' for stdin, or '@path' to read a file.",
    )
    parser.add_argument(
        "corpus_words",
        nargs="?",
        help="Whitespace-separated corpus words, or '@path' to read a file.",
    )
    parser.add_argument(
        "--input-file",
        help="Read the input text from this file instead of the positional value.",
    )
    parser.add_argument(
        "--corpus-file",
        help="Read the corpus words from this file instead of the positional value.",
    )
    parser.add_argument(
        "--json",
        action="store_true",
        help="Print a JSON array instead of one word per line.",
    )
    parser.add_argument(
        "--sort",
        action="store_true",
        help="Sort the result alphabetically instead of article order.",
    )
    parser.add_argument(
        "--count-only",
        action="store_true",
        help="Print only the number of matching words.",
    )
    return parser


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)

    if args.input_file:
        try:
            input_content = Path(args.input_file).read_text(encoding="utf-8")
        except OSError as exc:
            parser.error(f"cannot read --input-file: {exc}")
    elif args.input_content is not None:
        input_content = _as_text(args.input_content, stdin_label="input")
    else:
        parser.error("provide input_content, --input-file, or '-'")

    if args.corpus_file:
        try:
            corpus_words = Path(args.corpus_file).read_text(encoding="utf-8")
        except OSError as exc:
            parser.error(f"cannot read --corpus-file: {exc}")
    elif args.corpus_words is not None:
        corpus_words = _as_text(args.corpus_words, stdin_label="corpus")
    else:
        parser.error("provide corpus_words, --corpus-file, or '@path'")

    matches = words_in_both(input_content, corpus_words)
    if args.sort:
        matches.sort()

    if args.count_only:
        print(len(matches))
    elif args.json:
        print(json.dumps(matches, ensure_ascii=False))
    else:
        for word in matches:
            print(word)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
