"""Curated Markdown notes that supplement the official language documentation.

Notes are stored under ``./knowledge/<tag>/*.md``. The documentation-search
scripts treat every note as an additional documentation page: its paragraphs
are chunked and ranked together with the official documentation, so a
hand-written tip can answer a question that the indexed manual pages only
cover indirectly (for example, calling ``yt-dlp`` from a PHP CLI script).

Keeping this loader in one place means the Go, Rust, and PHP search scripts
all discover knowledge notes the same way.
"""

from __future__ import annotations

import re
from pathlib import Path

KNOWLEDGE_ROOT = Path(__file__).resolve().parent / "knowledge"

# Notes in these directories supplement every documentation-backed language.
# ``common_language`` holds cross-language recipes (for example, building a
# Wikipedia CLI) that apply equally to Go, Rust, and PHP questions.
SHARED_KNOWLEDGE_TAGS = ("common_language",)

# A matched note can be much more useful as a single, complete reference than
# as several unrelated BM25 fragments (a long code sample otherwise gets split
# in the middle of a function and the model has to guess the missing parts).
# Keep that whole-note block bounded, but large enough for a real CLI program.
KNOWLEDGE_NOTE_MAX_CHARS = 60_000
DEFAULT_KNOWLEDGE_NOTE_TERMS = 2

_SAFE_TAG_RE = re.compile(r"[^a-z0-9_-]+")
_HEADING_PREFIX_RE = re.compile(
    r"^(?:question|q|title)\s*[:：]\s*", re.IGNORECASE
)
_QUERY_TERM_RE = re.compile(r"[a-z0-9_]+(?:-[a-z0-9_]+)*")

# Request words that do not say what a note is about. They are ignored when
# deciding whether a note matches a question, so "write a script" alone never
# pulls in an unrelated note.
_NOTE_QUERY_STOPWORDS = frozenset(
    """
    a an and are as at be been being but by can could create do does doing
    example examples for from get give how i if implement implementation in
    into is it its just make me my of on or our please program sample scripts
    script show snippet so some tell than that the their them then there
    these they this to use using was we were what when where which who why
    will with would write you your cli command line tool
    """.split()
)


def normalize_tag(tag: str | None) -> str:
    """Return a filesystem-safe tag name used to look up knowledge notes."""
    return _SAFE_TAG_RE.sub("", str(tag or "").strip().lower())


def knowledge_directory(tag: str | None) -> Path | None:
    """Return ``./knowledge/<tag>`` when it exists, otherwise ``None``."""
    safe_tag = normalize_tag(tag)
    if not safe_tag:
        return None
    directory = KNOWLEDGE_ROOT / safe_tag
    if not directory.is_dir():
        return None
    return directory


def _document_title(text: str, fallback: str) -> str:
    """Use the first Markdown heading as the note title when available."""
    for line in text.splitlines():
        heading = line.strip()
        if not heading.startswith("#"):
            continue
        title = heading.lstrip("#").strip()
        title = _HEADING_PREFIX_RE.sub("", title).strip()
        if title:
            return title[:200]
    return fallback


def knowledge_chunks(tag: str | None, chunk_pages) -> list[dict]:
    """Chunk the knowledge notes for ``tag`` using a docs script's chunker.

    Notes under ``./knowledge/common_language`` are added to every tag, so a
    language question can reuse a cross-language recipe even when the active
    tag only has language-specific notes. The shared notes are skipped when
    the caller already asks for them, which avoids duplicate chunks.
    """
    safe_tag = normalize_tag(tag)
    tags: list[str] = [tag] if safe_tag else []
    if safe_tag:
        for shared_tag in SHARED_KNOWLEDGE_TAGS:
            if shared_tag != safe_tag:
                tags.append(shared_tag)

    chunks: list[dict] = []
    for candidate in tags:
        chunks.extend(chunk_pages(knowledge_pages(candidate)))
    for chunk in chunks:
        chunk["knowledge"] = True
    return chunks


def boost_knowledge_scores(chunks: list[dict], scores: list[float]) -> None:
    """Give matching knowledge excerpts a small boost over official docs.

    The boost is relative to the best positive BM25 score and is applied only
    when the knowledge excerpt already matches the query, so an unrelated
    note is never promoted. It is large enough to keep a relevant local note
    in the top ``k`` when the official manual has many partially-matching
    pages, but it does not let the note outrank a strongly matching official
    page.
    """
    best_positive = max((score for score in scores if score > 0), default=0.0)
    if best_positive <= 0:
        return
    bonus = max(0.001, best_positive * 0.35)
    for index, chunk in enumerate(chunks):
        if chunk.get("knowledge") and scores[index] > 0:
            scores[index] += bonus


def knowledge_pages(tag: str | None) -> list[dict]:
    """Load all Markdown knowledge notes for ``tag`` as documentation pages.

    Each returned page has the same ``url``, ``title`` and ``text`` keys as
    the pages produced by the documentation crawlers, so the existing
    ``chunk_pages`` and BM25 ranking code can consume it unchanged. The extra
    ``knowledge`` flag lets the rankers recognise these excerpts.
    """
    directory = knowledge_directory(tag)
    if directory is None:
        return []

    pages: list[dict] = []
    for path in sorted(directory.rglob("*.md")):
        if not path.is_file():
            continue
        try:
            text = path.read_text(encoding="utf-8")
        except OSError:
            continue
        if not text.strip():
            continue
        try:
            relative = path.relative_to(KNOWLEDGE_ROOT.parent).as_posix()
        except ValueError:
            relative = path.as_posix()
        fallback_title = path.stem.replace("_", " ").replace("-", " ")
        title = _document_title(text, fallback_title)
        pages.append(
            {
                "url": relative,
                "title": f"Local knowledge: {title}",
                "text": text,
                "knowledge": True,
            }
        )
    return pages


def _note_query_terms(text: str) -> set[str]:
    """Return the meaningful tokens used to match notes to a question."""
    terms = set()
    for term in _QUERY_TERM_RE.findall(str(text or "").lower()):
        if len(term) < 2 or term in _NOTE_QUERY_STOPWORDS:
            continue
        terms.add(term)
    return terms


def _note_phrases(keywords) -> list[str]:
    """Return multi-word keyword phrases from the question analyser."""
    phrases = []
    for keyword in keywords or ():
        phrase = " ".join(str(keyword).lower().split())
        if len(phrase.split()) < 2 or len(phrase) < 5:
            continue
        phrases.append(phrase)
    return phrases


def matching_knowledge_notes(
    tag: str | None,
    question: str,
    keywords=(),
    *,
    limit: int = 1,
    min_terms: int = DEFAULT_KNOWLEDGE_NOTE_TERMS,
) -> list[dict]:
    """Return whole-note chunks for local notes that match ``question``.

    Documentation search normally chunks every page, including local notes.
    That is useful for ranking, but it is harmful for a long reference
    implementation: the top excerpts can contain only a few unrelated
    fragments of the code sample, never the complete solution. This helper
    identifies a note whose topic clearly matches the question and returns it
    as one complete context excerpt. Callers can then pin that excerpt ahead
    of the ordinary manual excerpts.

    Matching is deliberately conservative: at least ``min_terms`` meaningful
    query terms must occur in the note, or a multi-word keyword phrase must
    occur verbatim. A note is never selected merely because the question says
    "write a script" or "show code".
    """
    safe_tag = normalize_tag(tag)
    terms = _note_query_terms(question)
    for keyword in keywords or ():
        terms.update(_note_query_terms(str(keyword)))
    phrases = _note_phrases(keywords)
    if not terms and not phrases:
        return []

    candidate_tags: list[str] = [tag] if safe_tag else []
    if safe_tag:
        candidate_tags.extend(
            shared for shared in SHARED_KNOWLEDGE_TAGS if shared != safe_tag
        )

    scored: list[tuple[float, dict]] = []
    seen_urls: set[str] = set()
    for candidate in candidate_tags:
        for page in knowledge_pages(candidate):
            if page["url"] in seen_urls:
                continue
            seen_urls.add(page["url"])
            haystack = f"{page['title']}\n{page['text']}".lower()
            matched = {term for term in terms if term in haystack}
            phrase_hits = sum(1 for phrase in phrases if phrase in haystack)
            if len(matched) < min_terms and not phrase_hits:
                continue
            # Prefer notes that cover more of the question, and give an
            # explicit phrase match a little extra weight.
            score = float(len(matched)) + 1.5 * phrase_hits
            text = page["text"]
            if len(text) > KNOWLEDGE_NOTE_MAX_CHARS:
                text = (
                    text[:KNOWLEDGE_NOTE_MAX_CHARS].rstrip()
                    + "\n\n[local knowledge note truncated]"
                )
            scored.append(
                (
                    score,
                    {
                        "url": page["url"],
                        "title": (
                            f"{page['title']} "
                            "(complete local reference implementation)"
                        ),
                        "text": text,
                        "knowledge": True,
                        "whole_note": True,
                    },
                )
            )

    scored.sort(key=lambda item: (-item[0], item[1]["url"]))
    return [chunk for _score, chunk in scored[: max(0, limit)]]
