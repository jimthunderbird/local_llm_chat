"""Rewrite articles in simple English for the article reader."""

from __future__ import annotations

from .core import *

# The reader's "Simplified" button rewrites an article with the local
# ornith-1.5:35b answer model, using a deliberately small everyday vocabulary.
SIMPLIFY_MODEL = chat.ANSWER_MODEL
SIMPLIFY_NUM_CTX = chat.ANSWER_NUM_CTX
MAX_SIMPLIFY_CHARS = 200_000
SIMPLIFY_CHUNK_CHARS = 6_000

SIMPLIFY_PROMPT = """You are an English editor. Rewrite the article below in simple English using only the roughly 2,500 most common English words.

Rules:
- Keep every important fact, name, number, and detail.
- Do not summarize, shorten, or add commentary.
- Keep the same paragraph breaks.
- If a technical term is unavoidable, keep it and explain it in simple words.
- Return only the rewritten article. Do not add a title, preface, or label.

<article>
{text}
</article>"""

SIMPLIFY_LABEL_PATTERN = re.compile(
    r"^(?:\*\*)?(?:simplified(?:\s+(?:version|article|text))?"
    r"|simple\s+english(?:\s+version)?"
    r"|rewritten(?:\s+(?:version|article|text))?"
    r"|here\s+is(?:\s+the)?(?:\s+simplified)?(?:\s+(?:version|article|text))?)"
    r"(?:\*\*)?\s*[::]\s*",
    re.IGNORECASE,
)

_SENTENCE_BREAK_RE = re.compile(r"(?<=[.!?])\s+")


class SimplificationPrefixFilter:
    """Strip a leading model label such as ``Simplified version:``."""

    def __init__(self) -> None:
        self._buffer = ""
        self._ready = False

    def feed(self, token: str) -> str:
        if self._ready:
            return token
        self._buffer += token
        stripped = self._buffer.lstrip()
        if not stripped:
            return ""
        if "\n" not in stripped and len(stripped) < 24:
            return ""
        self._ready = True
        return SIMPLIFY_LABEL_PATTERN.sub("", stripped, count=1)

    def flush(self) -> str:
        if self._ready:
            return ""
        self._ready = True
        return SIMPLIFY_LABEL_PATTERN.sub("", self._buffer.lstrip(), count=1)


def split_simplify_paragraphs(text: str) -> list[str]:
    """Split article text into paragraph-sized blocks for streaming rewrites.

    Very long paragraphs are split again at sentence boundaries so each model
    request stays comfortably inside the answer model's context window.
    """
    normalized = text.replace("\r\n", "\n").replace("\r", "\n").strip()
    if not normalized:
        return []
    blocks: list[str] = []
    for paragraph in re.split(r"\n[ \t]*\n+", normalized):
        paragraph = paragraph.strip()
        if not paragraph:
            continue
        if len(paragraph) <= SIMPLIFY_CHUNK_CHARS:
            blocks.append(paragraph)
            continue
        chunk = ""
        for sentence in _SENTENCE_BREAK_RE.split(paragraph):
            sentence = sentence.strip()
            if not sentence:
                continue
            if chunk and len(chunk) + 1 + len(sentence) > SIMPLIFY_CHUNK_CHARS:
                blocks.append(chunk)
                chunk = sentence
            else:
                chunk = f"{chunk} {sentence}".strip() if chunk else sentence
        if chunk:
            blocks.append(chunk)
    return blocks
