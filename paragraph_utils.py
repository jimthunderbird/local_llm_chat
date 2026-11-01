"""Helpers for turning rewritten transcripts into small RAG paragraphs."""

import re

# A trunk should stay small enough for focused retrieval.
MAX_PARAGRAPH_WORDS = 90
# A line this long is almost certainly one whole knowledge paragraph, not a
# hard-wrapped line of a larger paragraph.
MIN_PARAGRAPH_LINE_WORDS = 15

_SENTENCE_SPLIT_RE = re.compile(r"(?<=[.!?])\s+")


def split_long_paragraph(text: str, max_words: int = MAX_PARAGRAPH_WORDS) -> list[str]:
    """Split a long paragraph into sentence-aligned chunks of at most max_words."""
    if len(text.split()) <= max_words:
        return [text]

    sentences = [s.strip() for s in _SENTENCE_SPLIT_RE.split(text) if s.strip()]
    if len(sentences) <= 1:
        words = text.split()
        return [
            " ".join(words[start : start + max_words])
            for start in range(0, len(words), max_words)
        ]

    paragraphs: list[str] = []
    current: list[str] = []
    current_words = 0

    for sentence in sentences:
        sentence_words = len(sentence.split())

        if sentence_words > max_words:
            if current:
                paragraphs.append(" ".join(current))
                current = []
                current_words = 0
            words = sentence.split()
            paragraphs.extend(
                " ".join(words[start : start + max_words])
                for start in range(0, len(words), max_words)
            )
            continue

        if current and current_words + sentence_words > max_words:
            paragraphs.append(" ".join(current))
            current = []
            current_words = 0

        current.append(sentence)
        current_words += sentence_words

    if current:
        paragraphs.append(" ".join(current))

    return paragraphs


def split_paragraphs(rewritten_content: str) -> list[str]:
    """Return short, self-contained paragraphs from rewritten content.

    Blank-line-separated paragraphs are preferred. Model output that instead
    uses one paragraph per line, or emits one very long block, is normalized
    here so every generated trunk remains small.
    """
    paragraphs: list[str] = []

    for block in re.split(r"\n\s*\n", rewritten_content):
        block = block.strip(" \t\r\n")
        if not block:
            continue

        lines = [line.strip() for line in block.splitlines() if line.strip()]
        paragraph_like_lines = len(lines) > 1 and all(
            len(line.split()) >= MIN_PARAGRAPH_LINE_WORDS for line in lines
        )

        candidates = lines if paragraph_like_lines else [" ".join(lines)]
        for candidate in candidates:
            paragraphs.extend(split_long_paragraph(candidate))

    return paragraphs
