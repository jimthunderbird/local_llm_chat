"""Paragraph translation helpers."""

from __future__ import annotations

from .core import *

# The browser's "中文" button translates a finished answer paragraph by
# paragraph. ./prompts/translation.prompt requires qwen3:8b for translation to
# Simplified Chinese, so it is kept separate from the small helper model.
ANSWER_TRANSLATION_MODEL = chat.CHINESE_TRANSLATION_MODEL

ANSWER_TRANSLATION_NUM_CTX = chat.ANSWER_NUM_CTX

MAX_TRANSLATION_CHARS = 200_000

TRANSLATION_PARAGRAPH_PATTERN = re.compile(r"\n[ \t]*\n+")

TRANSLATION_LABEL_PATTERN = re.compile(
    r"^(?:\*\*)?(?:translation|english|simplified chinese|中文|简体中文)"
    r"(?:\*\*)?\s*[::]\s*",
    re.IGNORECASE,
)

class TranslationPrefixFilter:
    """Strip a leading model label such as ``Translation:`` while streaming."""

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
        # Wait briefly so a label split across tokens can be removed as a
        # whole. Short paragraphs are released by flush().
        if "\n" not in stripped and len(stripped) < 16:
            return ""
        self._ready = True
        return TRANSLATION_LABEL_PATTERN.sub("", stripped, count=1)

    def flush(self) -> str:
        if self._ready:
            return ""
        self._ready = True
        return TRANSLATION_LABEL_PATTERN.sub(
            "", self._buffer.lstrip(), count=1
        )
