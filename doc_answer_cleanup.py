#!/usr/bin/env python3
"""Shared cleanup for answers from the official documentation assistants.

When a code-sample question has no exact match in the indexed documentation,
the assistants sometimes open the answer with a caveat such as::

    The indexed PHP documentation does not contain a bubble sort
    implementation. ... Below is a complete, runnable PHP implementation...

The requested code is still generated from the excerpts and the model's own
language knowledge, so that preamble is noise: the user asked for code and
should see the code, not a report about missing excerpts.

``strip_leading_documentation_caveat`` removes those leading caveat
paragraphs.
``DocumentationCaveatFilter`` applies the same cleanup while the answer is
being streamed: it holds the opening text until the first fenced code block
(or a small size limit) and then releases the cleaned text, so the rest of the
answer still streams normally.

The transcript RAG prompt has a separate refusal phrasing ("The provided
context does not contain sufficient information..."). For documentation-backed
tags that refusal must never be shown either, because the official docs answer
or the code rescue follows. ``strip_leading_insufficient_context`` and
``InsufficientContextFilter`` handle that phrasing.

When a long code answer hits the prediction limit, it is continued with a
second request. Small models sometimes begin the continuation with a fresh
```` ```php ```` fence even though the previous message stopped in the middle
of a code line, which produces text such as
``new YouTubeTranscript```phpFetcher(...)``. ``repair_code_fences`` and
``CodeFenceRepairFilter`` remove those stray inline/re-opening fences while
leaving well-formed fenced blocks untouched.

Continuations are also fragile in a second way: a model that receives an
assistant message ending in the middle of a line may invent a plausible but
invalid completion (for example ``$videoId = trim($positional) !== 1) {``)
or restart from an earlier block. ``split_complete_lines`` lets the caller
stop a truncated reply at the last complete line, and
``strip_repeated_continuation_prefix`` removes text that the continuation
repeats from the end of the previous reply.
"""

from __future__ import annotations

import re

# One sentence that says the documentation/excerpts do not contain the
# requested information. Keep this deliberately narrow: it must not match a
# normal explanation such as "The manual shows sort(), which uses Quicksort".
DOCUMENTATION_CAVEAT_PATTERN = re.compile(
    r"(?:"
    r"\b(?:the\s+)?(?:indexed\s+|provided\s+|available\s+|retrieved\s+)?"
    r"(?:[\w./:+-]+\s+){0,6}documentation(?:\s+excerpts?)?\s+"
    r"(?:does|do|did)\s+not\s+(?:contain|include|cover|provide|offer)"
    r"|\b(?:the\s+)?(?:provided\s+|available\s+|retrieved\s+|indexed\s+)?"
    r"excerpts?\s+(?:does|do|did)\s+not\s+"
    r"(?:contain|include|cover|provide|offer)"
    r"|\b(?:there\s+(?:is|are)\s+)?no\s+"
    r"(?:matching|relevant|direct|complete|usable)?\s*"
    r"(?:excerpts?|examples?|implementations?|information|documentation)\b"
    r"|\b(?:i\s+)?(?:cannot|can't|am\s+unable\s+to|was\s+unable\s+to)\s+"
    r"(?:find|provide|give|write|generate)\s+(?:a\s+)?"
    r"(?:complete\s+)?(?:example|implementation|answer|code)"
    r"|\bnot\s+(?:contain|include|cover)\s+(?:a\s+)?(?:complete\s+)?"
    r"(?:example|implementation|answer)"
    r")",
    re.IGNORECASE,
)

# While streaming, wait for the first code fence so the caveat can be removed
# before anything is shown. If the model writes a long preamble without code,
# release the buffer at this size instead of delaying the whole answer.
STREAM_BUFFER_LIMIT = 900

# Documentation answers are streamed with a bounded per-request prediction
# budget. A long code sample can exceed that budget, so when Ollama reports
# ``done_reason == "length"`` the caller asks the model to continue from the
# exact point where it stopped instead of returning a half-written snippet.
# A complete reference implementation (for example the PHP YouTube transcript
# CLI in ./knowledge/php) can be several thousand tokens, so keep the budget
# large enough that the model can finish it in one request; the continuation
# path remains a safety net for exceptionally long answers.
ANSWER_NUM_PREDICT = 8192
MAX_ANSWER_CONTINUATIONS = 2
ANSWER_CONTINUATION_PROMPT = (
    "Continue the previous answer at the exact point where it stopped. "
    "The previous message was cut off in the middle of a line or code block. "
    "Start with the exact next characters only. Do not repeat any text that "
    "is already present, do not repeat a code fence or language tag, do not "
    "add a preamble or a new heading, and finish the current code block and "
    "the rest of the answer. Output only the remaining text."
)

# A continuation must repeat at least this many characters before the repeat
# is treated as an artefact. Shorter suffix/prefix coincidences are common in
# ordinary prose and in short lines of code (for example a closing brace).
CONTINUATION_MIN_OVERLAP_CHARS = 40


def split_complete_lines(text: str) -> tuple[str, str]:
    """Split ``text`` into complete lines and a trailing partial line.

    Callers stream the first element immediately and hold the second one
    until they know whether the model finished or hit its prediction limit.
    Dropping the partial line before a continuation keeps the model history
    on a clean line boundary, which prevents it from inventing a completion
    for half a line it never actually wrote.
    """
    text = str(text or "")
    newline = text.rfind("\n")
    if newline < 0:
        return "", text
    return text[: newline + 1], text[newline + 1 :]


def strip_repeated_continuation_prefix(
    previous: str,
    continuation: str,
    min_overlap: int = CONTINUATION_MIN_OVERLAP_CHARS,
) -> str:
    """Drop a prefix of ``continuation`` that repeats the end of ``previous``.

    Models asked to continue sometimes replay a block they already wrote,
    especially when the previous reply stops inside a long reference
    implementation. The repeated block is a suffix of ``previous`` (the
    remainder of the answer from the point where the model restarted) and a
    prefix of ``continuation``. Removing the longest such overlap keeps the
    code contiguous without touching a genuine continuation, which normally
    shares no more than a few characters with the end of the previous reply.
    """
    previous = str(previous or "").replace("\r\n", "\n").replace("\r", "\n")
    continuation = str(continuation or "").replace("\r\n", "\n").replace(
        "\r", "\n"
    )
    if not previous or not continuation:
        return continuation

    max_overlap = min(len(previous), len(continuation))
    for size in range(max_overlap, min_overlap - 1, -1):
        if previous.endswith(continuation[:size]):
            return continuation[size:]
    return continuation


_FENCE_LINE_PATTERN = re.compile(r"^[ \t]*```", re.MULTILINE)

# The transcript RAG prompt tells the local model to answer "The provided
# context does not contain sufficient information to answer this question."
# when retrieval is thin. For documentation-backed tags that refusal is never
# the final answer: the official documentation path answers instead, and a
# coding question is rescued by the local coding model. Suppress the refusal
# while it streams so it never flashes on screen before the docs answer.
INSUFFICIENT_CONTEXT_PATTERN = re.compile(
    r"(?:"
    r"\b(?:the\s+)?(?:provided|available|given|retrieved|supplied)\s+"
    r"(?:context|chunks?|excerpts?|documents?)\s+"
    r"(?:does|do|did)\s+not\s+(?:contain|include|provide|have)\s+"
    r"(?:(?:sufficient|enough|any)\s+)?"
    r"(?:information|details?|data|evidence|content)?"
    r"|\b(?:the\s+)?(?:provided|available|given)\s+context\s+"
    r"(?:is|seems)\s+(?:insufficient|not\s+enough|too\s+limited)"
    r"|\b(?:insufficient|not\s+(?:enough|sufficient))\s+"
    r"(?:information|context|data|evidence)"
    r"|\b(?:i\s+)?(?:do\s+not|don't)\s+have\s+(?:enough|sufficient)\s+"
    r"(?:information|context|data)"
    r"|\b(?:i\s+)?(?:cannot|can't|am\s+unable\s+to)\s+"
    r"(?:answer|determine)\s+(?:this|the|that)\s+question"
    r"|\b(?:based\s+on\s+)?(?:the\s+)?(?:provided|available|given)?\s*"
    r"context\s*,?\s*(?:i\s+)?(?:cannot|can't|am\s+unable\s+to)\s+"
    r"(?:answer|determine)"
    r"|\bno\s+(?:relevant\s+)?(?:information|details?|data)\s+"
    r"(?:is\s+)?(?:available|provided|found)\s+in\s+the\s+"
    r"(?:provided\s+)?context"
    r"|\bplease\s+(?:provide|share)\s+(?:more|additional|the\s+missing)\s+"
    r"(?:context|information|details?)"
    r"|\b(?:the\s+)?(?:provided|available)\s+(?:context|information)\s+"
    r"(?:is\s+)?(?:not\s+enough|too\s+limited)"
    r"|(?:提供的|给定的|现有的)?(?:上下文|内容|资料|语境)"
    r"(?:中)?(?:不包含|没有|未包含|不足以)"
    r"|(?:无法|不能)(?:根据)?(?:提供的)?(?:上下文|内容|资料)"
    r"(?:回答|确定|得出结论)"
    r")",
    re.IGNORECASE,
)

_PARAGRAPH_SPLIT = re.compile(r"(\n[ \t]*\n)")
# A leading sentence or line, including its terminator. Used to drop only the
# refusal sentence when the model continues with a useful answer in the same
# paragraph.
_LEADING_SENTENCE_PATTERN = re.compile(r"^[^\n.!?]*(?:[.!?]+|\n|$)[ \t]*")


def is_documentation_caveat(text: str) -> bool:
    """Return True when ``text`` reports missing documentation information."""
    return bool(DOCUMENTATION_CAVEAT_PATTERN.search(" ".join(text.split())))


def is_insufficient_context_caveat(text: str) -> bool:
    """Return True when ``text`` reports insufficient retrieved context."""
    return bool(
        INSUFFICIENT_CONTEXT_PATTERN.search(" ".join(text.split()))
    )


def _strip_caveat_paragraphs(text: str) -> str:
    parts = _PARAGRAPH_SPLIT.split(text)
    output: list[str] = []
    still_stripping = True
    for index, part in enumerate(parts):
        # Even indexes are paragraph text; odd indexes are separators.
        if index % 2 == 1:
            if output or not still_stripping:
                output.append(part)
            continue
        if still_stripping:
            if not part.strip() or is_documentation_caveat(part):
                continue
            output.append(part)
            still_stripping = False
        else:
            output.append(part)
    return "".join(output).strip()


def strip_leading_documentation_caveat(answer: str) -> str:
    """Drop leading caveat paragraphs from a documentation answer.

    Text after the first fenced code block is never touched. Only leading
    paragraphs that report missing excerpts are removed; an answer that starts
    with a real explanation is returned unchanged.
    """
    text = str(answer or "")
    if not text:
        return ""

    fence = text.find("```")
    if fence >= 0:
        head, tail = text[:fence], text[fence:]
    else:
        head, tail = text, ""

    if not is_documentation_caveat(head):
        return text

    cleaned_head = _strip_caveat_paragraphs(head)
    if cleaned_head and tail:
        return cleaned_head + "\n\n" + tail.lstrip("\n")
    return cleaned_head + tail


def strip_leading_insufficient_context(answer: str) -> str:
    """Remove leading "provided context is insufficient" sentences.

    The transcript prompt asks the local model to refuse when retrieval is
    thin. That refusal is noise for documentation-backed tags because the
    official documentation (or the coding rescue) supplies the answer. Only
    the leading refusal is removed; text after the first fenced code block is
    never touched, and a normal answer is returned unchanged.
    """
    text = str(answer or "")
    if not text:
        return ""

    fence = text.find("```")
    if fence >= 0:
        head, tail = text[:fence], text[fence:]
    else:
        head, tail = text, ""

    while True:
        stripped = head.lstrip()
        if not stripped:
            head = ""
            break
        match = _LEADING_SENTENCE_PATTERN.match(stripped)
        if not match or not match.group(0).strip():
            head = stripped
            break
        if not is_insufficient_context_caveat(match.group(0)):
            head = stripped
            break
        head = stripped[match.end() :]

    cleaned = head.rstrip()
    if cleaned and tail:
        return cleaned + "\n\n" + tail.lstrip("\n")
    if cleaned:
        return cleaned
    return tail.lstrip("\n") if tail else ""


# Prefixes that could still turn into an insufficiency refusal. Only answers
# that start like one of these are buffered; every other answer streams
# immediately.
_INSUFFICIENT_CONTEXT_PREFIXES = (
    "the provided",
    "the available",
    "the given",
    "the retrieved",
    "the supplied",
    "the context",
    "provided context",
    "provided chunks",
    "provided excerpts",
    "available context",
    "given context",
    "based on the provided",
    "based on the available",
    "based on the given",
    "insufficient",
    "not enough",
    "i cannot",
    "i can't",
    "i am unable",
    "i'm unable",
    "i do not have",
    "i don't have",
    "no relevant",
    "no information",
    "please provide",
    "unfortunately",
    "sorry",
    "提供的",
    "给定的",
    "根据提供",
    "我无法",
    "不能根据",
    "抱歉",
)


def _could_start_insufficient_context(text: str) -> bool:
    prefix = text.lstrip().lower()
    if not prefix:
        return True
    # The buffer may contain only the first few characters of a refusal (for
    # example "the p"), so accept either direction of the prefix match.
    return any(
        item.startswith(prefix) or prefix.startswith(item)
        for item in _INSUFFICIENT_CONTEXT_PREFIXES
    )


class InsufficientContextFilter:
    """Incrementally suppress a leading "provided context is insufficient" refusal.

    Normal answers are released immediately. An answer that starts like a
    refusal is held only until a sentence/paragraph boundary or the first code
    fence, so the refusal can be removed before it reaches the browser. If the
    model continues with a useful answer after the refusal, that remainder is
    released normally.
    """

    def __init__(self, enabled: bool = True) -> None:
        self.enabled = enabled
        self._pending: list[str] = []
        self._released = not enabled

    def _release(self, cleaned: str | None = None) -> str:
        buffered = "".join(self._pending)
        self._pending.clear()
        self._released = True
        if cleaned is None:
            return buffered
        # ``cleaned`` is right-stripped so a buffered refusal does not leave
        # stray blank lines. When the release happens mid-answer, keep the
        # whitespace at the streaming boundary so the next chunk does not run
        # into this one ("Here is a" + "general" -> "Here is ageneral").
        if cleaned:
            trailing = re.search(r"\s+$", buffered)
            if trailing and not cleaned.endswith(trailing.group(0)):
                cleaned += trailing.group(0)
        return cleaned

    def feed(self, piece: str) -> str:
        """Return the part of ``piece`` that is safe to show now."""
        if not piece:
            return ""
        if not self.enabled or self._released:
            return piece
        self._pending.append(piece)
        buffered = "".join(self._pending)
        if "```" in buffered:
            return self._release(
                strip_leading_insufficient_context(buffered)
            )
        if not _could_start_insufficient_context(buffered):
            return self._release()
        if re.search(r"[.!?\n]", buffered):
            cleaned = strip_leading_insufficient_context(buffered)
            if cleaned.strip():
                return self._release(cleaned)
        if len(buffered) >= STREAM_BUFFER_LIMIT:
            return self._release(
                strip_leading_insufficient_context(buffered)
            )
        return ""

    def flush(self) -> str:
        """Return any remaining buffered text once the stream has ended."""
        if not self.enabled or self._released or not self._pending:
            return ""
        buffered = "".join(self._pending)
        self._pending.clear()
        self._released = True
        return strip_leading_insufficient_context(buffered)


class DocumentationCaveatFilter:
    """Incrementally strip a leading documentation caveat while streaming."""

    def __init__(self, enabled: bool = True) -> None:
        self.enabled = enabled
        self._pending: list[str] = []
        self._released = not enabled

    def feed(self, piece: str) -> str:
        """Return the part of ``piece`` that is safe to show now."""
        if not piece:
            return ""
        if not self.enabled:
            return piece
        if self._released:
            return piece
        self._pending.append(piece)
        buffered = "".join(self._pending)
        if "```" not in buffered and len(buffered) < STREAM_BUFFER_LIMIT:
            return ""
        self._pending.clear()
        self._released = True
        return strip_leading_documentation_caveat(buffered)

    def flush(self) -> str:
        """Return any remaining buffered text once the stream has ended."""
        if not self.enabled or self._released or not self._pending:
            return ""
        buffered = "".join(self._pending)
        self._pending.clear()
        self._released = True
        return strip_leading_documentation_caveat(buffered)


def has_unclosed_code_fence(text: str) -> bool:
    """Return True when ``text`` ends inside an unterminated code fence."""
    return len(_FENCE_LINE_PATTERN.findall(str(text or ""))) % 2 == 1


def close_unclosed_code_fence(text: str) -> str:
    """Close a trailing code fence so later text is not rendered as code.

    When the model hits its prediction limit in the middle of a code sample,
    the answer ends with an open fence. The documentation scripts append a
    ``Sources:`` list afterwards; without a closing fence the browser shows
    that list as numbered code lines. Adding the fence keeps the rendered
    answer well formed even when the snippet itself could not be completed.
    """
    text = str(text or "")
    if not has_unclosed_code_fence(text):
        return text
    if text and not text.endswith("\n"):
        text += "\n"
    return text + "```"


# A complete fence line: up to three spaces of indentation, a run of three or
# more backticks/tildes, an optional language token, and nothing else. Markdown
# only recognises a fence when the marker starts the line, so anything else is
# a stray token injected into code or prose.
_FENCE_LINE_RE = re.compile(
    r"^[ \t]{0,3}(?P<marker>`{3,}|~{3,})[ \t]*"
    r"(?P<info>[A-Za-z0-9_+.#-]*)[ \t]*$"
)

# Language names that commonly follow a fence marker. Keeping this list lets
# the inline repair remove `` ```php `` from `` ```phpFetcher `` without
# swallowing the ``Fetcher`` identifier that follows it.
_FENCE_LANGUAGES = (
    "typescript", "javascript", "powershell", "dockerfile", "protobuf",
    "graphql", "markdown", "plaintext", "golang", "python", "kotlin",
    "swift", "scala", "shell", "java", "ruby", "rust", "php", "html",
    "scss", "json", "yaml", "toml", "text", "diff", "makefile", "hcl",
    "xml", "sql", "bash", "zsh", "fish", "go", "cpp", "csharp", "jsx",
    "tsx", "css", "cs", "js", "ts", "py", "rb", "rs", "md", "sh", "c",
)
_FENCE_LANGUAGE_SET = frozenset(_FENCE_LANGUAGES)
_FENCE_LANGUAGE_PATTERN = "|".join(
    re.escape(language)
    for language in sorted(_FENCE_LANGUAGES, key=len, reverse=True)
)
# Inline repairs require a known language token. Removing a bare ``` or ~~~
# from inside a code string would corrupt valid code, while the malformed
# continuation marker always carries the language tag (for example ```php).
_INLINE_FENCE_RE = re.compile(
    r"(?:`{3,}|~{3,})[ \t]*(?:" + _FENCE_LANGUAGE_PATTERN + r")",
    re.IGNORECASE,
)
# A malformed fence at the start of a line: remove the marker (and a known
# language tag, if present) while keeping the code that follows it.
_LEADING_FENCE_RE = re.compile(
    r"^([ \t]*)(?:`{3,}|~{3,})[ \t]*(?:"
    + _FENCE_LANGUAGE_PATTERN
    + r")?",
    re.IGNORECASE,
)


def _repair_fence_line(
    body: str, fence_char: str
) -> tuple[str | None, str]:
    """Repair one line of an answer.

    Returns ``(repaired_line_or_None, fence_char)``. ``None`` means the line
    was a stray reopening fence inside an already open block and should be
    dropped. ``fence_char`` is ``""`` when outside a fenced block, otherwise
    the marker character (backtick or tilde) of the open fence.
    """
    match = _FENCE_LINE_RE.match(body)
    if match:
        info = match.group("info")
        # A camel-cased info string such as "phpFetcher" is not a language
        # tag; it is the stray marker glued to the next identifier. Fall
        # through so the inline repair can keep the identifier intact.
        if info and not info.islower() and info.lower() not in _FENCE_LANGUAGE_SET:
            match = None
    if match:
        marker = match.group("marker")
        marker_char = marker[0]
        info = match.group("info")
        if fence_char:
            if marker_char == fence_char and not info:
                return body, ""
            if marker_char == fence_char and info:
                # A new opening fence (for example ```php) inside an existing
                # code block is almost always a continuation artefact. Drop
                # the line and keep the code contiguous.
                return None, fence_char
            # A different marker is literal code inside the fence.
            return body, fence_char
        return body, marker_char

    # Not a complete fence line. First remove a marker that was injected at
    # the start of a line followed by code, such as "```phpFetcher(...)".
    leading = _LEADING_FENCE_RE.match(body)
    if leading:
        body = leading.group(1) + body[leading.end():]
    # Then remove a marker injected in the middle of the line. A language tag
    # is required here so that a literal ``` or ~~~ inside a code string is
    # left untouched.
    repaired = _INLINE_FENCE_RE.sub("", body)
    return repaired, fence_char


class CodeFenceRepairFilter:
    """Incrementally remove malformed code fences while an answer streams.

    Complete lines are processed as soon as their newline arrives. The final
    partial line is held until :meth:`flush`, so a fence marker split across
    two stream chunks is still recognised as a whole token.
    """

    def __init__(self, enabled: bool = True) -> None:
        self.enabled = enabled
        self._buffer = ""
        self._fence_char = ""

    def feed(self, piece: str) -> str:
        """Return the safely repaired part of ``piece`` that is complete."""
        if not piece:
            return ""
        if not self.enabled:
            return piece
        self._buffer += piece.replace("\r\n", "\n").replace("\r", "\n")
        output: list[str] = []
        while "\n" in self._buffer:
            line, self._buffer = self._buffer.split("\n", 1)
            repaired, self._fence_char = _repair_fence_line(
                line, self._fence_char
            )
            if repaired is not None:
                output.append(repaired + "\n")
        return "".join(output)

    def flush(self) -> str:
        """Return the repaired final partial line, if any."""
        if not self.enabled or not self._buffer:
            return ""
        line, self._buffer = self._buffer, ""
        repaired, self._fence_char = _repair_fence_line(
            line, self._fence_char
        )
        return repaired or ""


def repair_code_fences(text: str) -> str:
    """Return ``text`` with malformed inline/reopening code fences removed."""
    repair = CodeFenceRepairFilter()
    return repair.feed(str(text or "")) + repair.flush()
