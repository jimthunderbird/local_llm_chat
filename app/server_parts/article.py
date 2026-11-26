"""Article loading, section routing, and iterative evidence."""

from __future__ import annotations

from .core import *
from .core import APP_DIR

# Articles loaded with /article are split into the same one-knowledge-piece
# paragraphs that the transcript pipeline uses. The generated trunk files live
# outside ./.chat/articles_cache so they never become corpus videos or appear in
# transcript autocomplete.
ARTICLE_CACHE_DIR = APP_DIR / ".article_cache"

ARTICLE_HTML_BLOCK_RE = re.compile(
    r"<\s*(script|style|noscript|svg|head)\b[^>]*>.*?<\s*/\s*\1\s*>",
    re.IGNORECASE | re.DOTALL,
)

ARTICLE_HTML_BREAK_RE = re.compile(
    r"<\s*/?\s*(?:br|p|div|li|ul|ol|h[1-6]|tr|td|th|section|article|"
    r"blockquote|pre)\b[^>]*>",
    re.IGNORECASE,
)

ARTICLE_HTML_TAG_RE = re.compile(r"<[^>]+>")

ARTICLE_MARKDOWN_HEADING_RE = re.compile(
    r"^\s{0,3}#{1,6}\s+(?P<title>.+?)\s*#*\s*$"
)

ARTICLE_HTML_HEADING_RE = re.compile(
    r"<\s*h[1-6]\b[^>]*>(?P<heading>.*?)<\s*/\s*h[1-6]\s*>",
    re.IGNORECASE | re.DOTALL,
)

ARTICLE_SUMMARY_HEADING_RE = re.compile(
    r"^\s*(?:\d+(?:\.\d+)*[.)]?\s*)?"
    r"(?:tl\s*;?\s*dr|summary|overview|abstract|executive summary)\b",
    re.IGNORECASE,
)

ARTICLE_NUMBERED_HEADING_RE = re.compile(
    r"^\s*\d+(?:\.\d+)*[.)]?\s+[A-Za-z]"
)

ARTICLE_OVERVIEW_REFERENCE_RE = re.compile(
    r"\b(?:this|the)\s+(?:article|document|doc|file|page|report|paper|book|novel)\b",
    re.IGNORECASE,
)

# A bare deictic ("what is this about?") refers to the whole article. The
# lookahead excludes cases where "this" is a determiner for another noun
# ("what is this item tag about?"), which is a question about one concept.
ARTICLE_BARE_DEICTIC_RE = re.compile(
    r"\b(?:this|it)\b(?=\s*(?:$|[,.;:!?]|"
    r"(?:about|all|in|with|for|on|by|to|of|and|or)\b))",
    re.IGNORECASE,
)

ARTICLE_OVERVIEW_INTENT_RE = re.compile(
    r"\b(?:about|summari[sz]e|summary|overview|tl\s*;?\s*dr|main|"
    r"topics?|discuss(?:es|ed)?|cover(?:s|ed)?|describe(?:s|d)?|purpose|"
    r"what is|what's|what does)\b",
    re.IGNORECASE,
)

ARTICLE_BROAD_OVERVIEW_RE = re.compile(
    r"\bwhat(?:'s|\s+is|\s+was)?\s+(?:this|it)\s+(?:all\s+)?about\b"
    r"|\b(?:summari[sz]e|overview|tl\s*;?\s*dr)\s+(?:this|it)\b"
    r"|\b(?:tell|show)\s+me\s+(?:about\s+)?(?:this|it)\b"
    r"|\bdescribe\s+(?:this|it)\b"
    r"|\b(?:main|key|top|core)\s+(?:recommendations?|takeaways?|"
    r"points?|findings?|conclusions?)\b",
    re.IGNORECASE,
)

ARTICLE_DETAILED_OVERVIEW_RE = re.compile(
    r"\b(?:detail(?:ed)?|in[- ]depth|comprehensive|thorough|long|extended|"
    r"elaborate|exhaustive)\b",
    re.IGNORECASE,
)

ARTICLE_WORD_COUNT_RE = re.compile(
    r"\b(\d[\d,]*)\s*[- ]?(?:words?|word\s+answer)\b",
    re.IGNORECASE,
)

ARTICLE_OVERVIEW_OUTLINE_CHARS = 1400

ARTICLE_OVERVIEW_SUMMARY_CHARS = 2400

ARTICLE_DIGEST_CHARS = 16000

ARTICLE_DIGEST_MIN_SECTION_CHARS = 300

ARTICLE_DIGEST_MAX_SECTION_CHARS = 1400

ARTICLE_OVERVIEW_INDEX = 0

ARTICLE_DIGEST_INDEX = -1

ARTICLE_TOP_SECTION_RE = re.compile(r"^\s*\d+[.)]?\s+\S")

ARTICLE_SUBSECTION_RE = re.compile(r"^\s*\d+\.\d+")

# Structure-aware article routing. The cache directory is content-addressed, so
# these generated artifacts can live beside the paragraph trunks and be reused
# until the article bytes change.
ARTICLE_META_VERSION = 4

ARTICLE_META_FILENAME = "article_structure.json"

ARTICLE_SECTION_SUMMARY_CHARS = 1400

ARTICLE_SECTION_SUMMARY_INDEX_BASE = -100

ARTICLE_SECTION_DEPTH = 12

ARTICLE_SECTION_CONTEXT_TOKENS = 8192

ARTICLE_NARROW_CONTEXT_TOKENS = 6144

ARTICLE_ITERATIVE_MAX_ROUNDS = 4

ARTICLE_ITERATIVE_EVIDENCE_TOKENS = 16000

ARTICLE_ITERATIVE_BATCH = 4

ARTICLE_ITERATIVE_CANDIDATES = 50

ARTICLE_ROUTER_MODEL = chat.FILTER_MODEL

# Normal transcript questions reuse the /article iterative evidence loop.
# YouTube transcripts are shorter per paragraph, but the global corpus spans
# many videos, so the same bounded-round design applies. Enumeration questions
# need wider coverage.
YOUTUBE_ITERATIVE_MAX_ROUNDS = 3

YOUTUBE_ITERATIVE_ENUMERATIVE_ROUNDS = 5

YOUTUBE_ITERATIVE_EVIDENCE_TOKENS = 16000

YOUTUBE_ITERATIVE_BATCH = 4

YOUTUBE_ITERATIVE_ENUMERATIVE_BATCH = 6

YOUTUBE_ITERATIVE_CANDIDATES = 50

# A long-form request ("write me a 1000 word article about ...") is judged by
# its output, not by whether the evidence can answer a factual question. Asking
# the sufficiency checker three times can only return NO, and each extra round
# adds latency before the writer starts. Gather one broad evidence batch and
# let the writer produce the article instead.
LONGFORM_ITERATIVE_FIRST_BATCH = 16

LONGFORM_ITERATIVE_CANDIDATES = 20

MISSING_TERMS_MAX = 10

YOUTUBE_ITERATIVE_PROMPT_PREFIX = """### RETRIEVED EVIDENCE
The context below was gathered in several retrieval rounds from the knowledge
base and is sufficient for the complete question. Answer every part of the
question, including lists, comparisons, and enumerations, using this evidence.
Do not say that the context is insufficient; if a detail is genuinely absent,
answer the supported parts and omit the unsupported detail.

"""

# Current-status questions are resolved with a recency rule. The knowledge
# base intentionally keeps old news for history, so an outdated prediction
# ("Huang is expected to keep his job") can sit beside the newest report
# ("Huang has resigned"). The publication dates are included with learned
# article context, and this instruction makes the newer report authoritative.
# A "timeline" question needs the older events too, but they must be dated
# history, not competing descriptions of the present. Both cases need the same
# rule: never present a superseded status as current.
TEMPORAL_QUESTION_PROMPT_PREFIX = """### CURRENT INFORMATION
Today is {today}. This question asks about the current state, the latest
development, or a timeline of events. The context contains dated articles;
when sources disagree, the most recently published evidence supersedes older
statements, predictions, and analysis.
- Report the latest known status. Never repeat a superseded claim as the
  current situation, and never list one under "ongoing" or "current status".
- If the question asks for a timeline or chronology, present the events in
  chronological order with their dates, then state the latest known status
  clearly and separately.
- An older statement may appear only as a historical event, explicitly dated;
  it must not be used to contradict the newest report.
- These rules override the general conflict-resolution instruction: do not
  present two contradictory current statuses as equally valid.

"""

# A learned news story, a single transcript, or a book modal is a small,
# explicitly-scoped corpus. If the question has no lexical match in that scope,
# BM25 can return nothing and the answer stream ends silently (the article
# modal then looks broken). In that case use the scope's reading-order text as
# context, up to the normal context budget, instead of refusing with no output.
SCOPED_CONTEXT_FALLBACK_PROMPT_PREFIX = """### SCOPED SOURCE
The question did not lexically match any single paragraph, so the context
below is the available text of the active source in reading order. Use this
source to answer. If the requested detail is genuinely absent, say briefly
that this source does not cover it; never return an empty answer.

"""

ARTICLE_ENUMERATIVE_RE = re.compile(
    r"\b(?:list|all|every|each|enumerate|compare|comparison|across\s+the\s+"
    r"(?:article|document)|types?|kinds?|levels?|categories)\b",
    re.IGNORECASE,
)

ARTICLE_SECTION_INTENT_RE = re.compile(
    r"\b(?:explain|describe|walk\s+me\s+through|how\s+(?:does|do|is|are)|"
    r"workflow|process|section|chapter|part\s+\d+|why\s+(?:does|do|is|are))\b",
    re.IGNORECASE,
)

ARTICLE_ROUTER_PROMPT = """Classify a question about one long article.

Choose exactly one label:
- OVERVIEW: the question asks for a summary of the whole article.
- SECTION: the question is mainly about one section. Also name its number.
- NARROW: the question asks for a specific fact from somewhere in the article.

Article sections:
{sections}

Question: {question}

Reply with one line in exactly one of these forms:
OVERVIEW
SECTION <number>
NARROW
"""

ARTICLE_SUFFICIENCY_PROMPT = """You are checking whether retrieved excerpts are enough to answer a question.

Question: {question}

Excerpts:
{excerpts}

Can every part of the question be answered with a specific fact or quote from
these excerpts? Reply in exactly this format:
VERDICT: YES or NO
EVIDENCE: [excerpt number] (required when the verdict is YES)
MISSING: what is still missing (short; write NONE when nothing is missing)
"""

# List/comparison questions need every requested item, but the generic prompt
# is satisfied as soon as one example is present. Ask specifically whether the
# set is complete so an enumeration is retrieved across more rounds instead of
# stopping on the first matching item.
ARTICLE_ENUMERATIVE_SUFFICIENCY_PROMPT = """You are checking whether retrieved excerpts can answer an enumeration question.

Question: {question}

Excerpts:
{excerpts}

The question asks for a list, comparison, or other enumeration. A single
example, a partial list, or a mention of the topic is not enough. Answer
VERDICT: YES only when the excerpts enumerate the requested items (or cover
every compared aspect). Reply in exactly this format:
VERDICT: YES or NO
EVIDENCE: [excerpt number] (required when the verdict is YES)
MISSING: which requested item(s) or category(ies) are still absent (short; write NONE when nothing is missing)
"""

class ArticleRoute(NamedTuple):
    """Routing decision for one question about the active article."""

    label: str
    section: int | None = None
    iterative: bool = False
    source: str = "regex"

def article_requested_word_count(question: str) -> int | None:
    """Return the requested answer length ("in 1000 words"), if any."""
    match = ARTICLE_WORD_COUNT_RE.search(question)
    if not match:
        return None
    try:
        words = int(match.group(1).replace(",", ""))
    except ValueError:
        return None
    return words if 30 <= words <= 4000 else None

def article_overview_question(question: str) -> bool:
    """Return True for questions about the article as a whole.

    These questions need the document title, outline, and opening/summary
    section rather than the normal BM25 paragraphs. Without this branch a
    broad question such as "what is this article about?" matches generic
    words ("what", "this") in unrelated paragraphs and the answer model sees
    no useful overview.

    Phrasings that use a bare deictic ("what is this about, in 1000 words")
    count as well because this helper is only consulted in article mode.
    """
    if ARTICLE_BROAD_OVERVIEW_RE.search(question):
        return True
    if not ARTICLE_OVERVIEW_INTENT_RE.search(question):
        return False
    if ARTICLE_OVERVIEW_REFERENCE_RE.search(question):
        return True
    if ARTICLE_BARE_DEICTIC_RE.search(question):
        return True
    return False

def _clean_article_heading(raw: str) -> str:
    text = ARTICLE_HTML_TAG_RE.sub(" ", raw)
    return " ".join(html.unescape(text).split())

def _article_headings(raw_text: str, text: str) -> list[str]:
    """Collect HTML or Markdown headings in document order."""
    headings: list[str] = []
    seen: set[str] = set()

    def add(heading: str) -> None:
        heading = " ".join(heading.split())
        key = heading.casefold()
        if heading and len(heading) <= 200 and key not in seen:
            seen.add(key)
            headings.append(heading)

    heading_source = ARTICLE_HTML_BLOCK_RE.sub("\n", raw_text)
    for raw in ARTICLE_HTML_HEADING_RE.findall(heading_source):
        add(_clean_article_heading(raw))
    for line in text.splitlines():
        match = ARTICLE_MARKDOWN_HEADING_RE.match(line)
        if match:
            add(match.group("title"))
    return headings

def _article_heading_like(paragraph: str) -> bool:
    """Return True for short, heading-shaped lines (including nav entries)."""
    if len(paragraph) > 120:
        return False
    if ARTICLE_NUMBERED_HEADING_RE.match(paragraph):
        return True
    return (
        len(paragraph.split()) <= 12
        and not paragraph.endswith((".", "!", "?", ";", ","))
    )

def _article_summary_paragraphs(paragraphs: list[str]) -> list[str]:
    """Return the paragraphs under a TL;DR/summary/overview heading."""
    normalized = [" ".join(paragraph.split()) for paragraph in paragraphs]
    matches = [
        index
        for index, paragraph in enumerate(normalized)
        if len(paragraph) <= 120
        and ARTICLE_SUMMARY_HEADING_RE.match(paragraph)
    ]
    if not matches:
        return []
    # An article often repeats "TL;DR" in its table of contents. Prefer the
    # numbered section heading ("1. TL;DR") when one exists.
    start = next(
        (
            index
            for index in matches
            if ARTICLE_NUMBERED_HEADING_RE.match(normalized[index])
        ),
        matches[0],
    )

    result: list[str] = []
    char_count = 0
    for paragraph in normalized[start + 1 :]:
        if not paragraph:
            continue
        # Skip outline/navigation entries between a summary link and the real
        # summary body (common in HTML pages with a table of contents).
        if not result and _article_heading_like(paragraph):
            continue
        if result and _article_heading_like(paragraph):
            break
        result.append(paragraph)
        char_count += len(paragraph)
        if len(result) >= 12 or char_count >= ARTICLE_OVERVIEW_SUMMARY_CHARS:
            break
    return result

def _article_opening_paragraphs(paragraphs: list[str]) -> list[str]:
    """Fallback overview text when the article has no summary section."""
    result: list[str] = []
    char_count = 0
    for paragraph in paragraphs[1:]:
        paragraph = " ".join(paragraph.split())
        if not paragraph:
            continue
        if len(paragraph) <= 120 and (
            ARTICLE_NUMBERED_HEADING_RE.match(paragraph)
            or (
                len(paragraph.split()) <= 10
                and not paragraph.endswith((".", "!", "?", ";"))
            )
        ):
            continue
        result.append(paragraph)
        char_count += len(paragraph)
        if len(result) >= 6 or char_count >= ARTICLE_OVERVIEW_SUMMARY_CHARS:
            break
    return result

def _article_overview(
    title: str, headings: list[str], paragraphs: list[str]
) -> str:
    """Build an always-in-context overview trunk for a loaded article.

    It contains the title, a compact outline, and the TL;DR/summary section
    (or the opening paragraphs when there is no explicit summary). The normal
    paragraph trunks remain unchanged, so follow-up questions still use BM25
    over the full article.
    """
    summary = _article_summary_paragraphs(paragraphs)
    if not summary:
        summary = _article_opening_paragraphs(paragraphs)

    parts = [
        f"Article title: {title}",
        "Document overview: this article summarizes the main topics, "
        "purpose, and summary of the document.",
    ]
    if headings:
        outline: list[str] = []
        outline_chars = 0
        for heading in headings[:60]:
            line = f"- {heading}"
            if outline_chars + len(line) > ARTICLE_OVERVIEW_OUTLINE_CHARS:
                break
            outline.append(line)
            outline_chars += len(line) + 1
        if outline:
            parts.append("Article outline:")
            parts.extend(outline)
    if summary:
        parts.append("Article summary:")
        parts.extend(summary)
    return "\n".join(parts)

def _article_digest(
    title: str,
    headings: list[str],
    paragraphs: list[str],
    max_chars: int = ARTICLE_DIGEST_CHARS,
) -> str:
    """Build a bounded, article-wide digest for overview questions.

    The overview alone (title, outline, TL;DR) is often under ~1,000 tokens,
    which is not enough for a request such as "what is this about, in 1000
    words". This digest keeps the overview and adds the first substantive
    content from every major section, spread across the whole document and
    capped by a character budget. It is used as one synthetic trunk so BM25
    cannot replace it with unrelated generic-word matches.
    """
    overview = _article_overview(title, headings, paragraphs)
    parts = [overview]
    used = len(overview)
    remaining = max_chars - used
    if remaining < 300:
        return overview

    normalized = [" ".join(paragraph.split()) for paragraph in paragraphs]
    anchors = [
        index
        for index, paragraph in enumerate(normalized)
        if ARTICLE_TOP_SECTION_RE.match(paragraph)
        and not ARTICLE_SUBSECTION_RE.match(paragraph)
    ]
    if len(anchors) < 3:
        anchors = [
            index
            for index, paragraph in enumerate(normalized)
            if _article_heading_like(paragraph)
        ]
    anchors = sorted(set(anchors))

    section_budget = ARTICLE_DIGEST_MIN_SECTION_CHARS
    if anchors:
        section_budget = max(
            ARTICLE_DIGEST_MIN_SECTION_CHARS,
            min(
                ARTICLE_DIGEST_MAX_SECTION_CHARS,
                remaining // len(anchors),
            ),
        )

    for position, start in enumerate(anchors):
        if used >= max_chars:
            break
        end = anchors[position + 1] if position + 1 < len(anchors) else len(
            normalized
        )
        block: list[str] = []
        block_chars = 0
        for index in range(start, end):
            paragraph = normalized[index]
            if not paragraph:
                continue
            # Keep subsection headings as structure, but avoid tiny
            # navigation fragments and inline code noise.
            if len(paragraph) < 20 and index != start:
                continue
            if (
                block_chars + len(paragraph) > section_budget
                and block
            ):
                break
            block.append(paragraph)
            block_chars += len(paragraph) + 1
            if block_chars >= section_budget:
                break
        if block:
            section = "\n".join(block)
            parts.append(section)
            used += len(section) + 2

    if not anchors:
        # No usable headings: sample substantive paragraphs evenly across the
        # document so the digest still represents its whole span.
        content_indexes = [
            index
            for index, paragraph in enumerate(normalized)
            if len(paragraph) >= 80 and not _article_heading_like(paragraph)
        ]
        if content_indexes:
            slots = max(1, remaining // 400)
            step = max(1, len(content_indexes) // slots)
            for index in content_indexes[::step]:
                paragraph = normalized[index]
                if used + len(paragraph) + 2 > max_chars:
                    break
                parts.append(paragraph)
                used += len(paragraph) + 2
                if used >= max_chars:
                    break

    digest = "\n\n".join(parts)
    return digest[:max_chars]

def _normalize_article_text(text: str) -> str:
    return " ".join(text.split())

def _article_section_map(
    paragraphs: list[str], headings: list[str], title: str
) -> list[dict]:
    """Record each paragraph's nearest preceding heading and section span.

    Headings are matched against the paragraphs produced by the normal
    HTML/Markdown extractor. Numbered headings are also accepted as a fallback
    for documents whose markup does not use ``<h1>``-``<h6>`` tags. The result
    is persisted next to the article trunks, so parent-section expansion does
    not need to re-parse the document for every question.
    """
    heading_keys = {
        _normalize_article_text(heading).casefold()
        for heading in headings
        if _normalize_article_text(heading)
    }
    sections: list[dict] = []
    current: dict | None = None

    for index, paragraph in enumerate(paragraphs, start=1):
        normalized = _normalize_article_text(paragraph)
        # Markdown headings keep their ``##`` marker in the paragraph text,
        # while _article_headings() stores the heading without it. Compare
        # both spellings so Markdown sections (and Gutenberg chapter
        # headings promoted to Markdown) are recognized.
        heading_text = ARTICLE_MARKDOWN_HEADING_RE.sub(
            lambda match: match.group("title"), normalized
        )
        is_heading = bool(normalized) and (
            normalized.casefold() in heading_keys
            or heading_text.casefold() in heading_keys
        )
        if (
            not is_heading
            and len(normalized) <= 160
            and ARTICLE_NUMBERED_HEADING_RE.match(normalized)
        ):
            is_heading = True

        if is_heading:
            current = {
                "number": len(sections) + 1,
                "heading": heading_text or normalized,
                "heading_index": index,
                "start": index,
                "end": index,
            }
            sections.append(current)
        elif current is None:
            current = {
                "number": len(sections) + 1,
                "heading": title or "Introduction",
                "heading_index": 0,
                "start": index,
                "end": index,
            }
            sections.append(current)
        else:
            current["end"] = index

    return sections

def _article_section_summary(
    section: dict, paragraphs: list[str]
) -> str:
    """Return a fast, deterministic extractive summary for one section."""
    heading_index = int(section.get("heading_index") or 0)
    start = heading_index + 1 if heading_index else int(section["start"])
    end = int(section["end"])
    chosen: list[str] = []
    used = 0
    for index in range(start, end + 1):
        if index < 1 or index > len(paragraphs):
            continue
        paragraph = _normalize_article_text(paragraphs[index - 1])
        if not paragraph or _article_heading_like(paragraph):
            continue
        if len(paragraph) < 24 and chosen:
            continue
        chosen.append(paragraph)
        used += len(paragraph) + 1
        if used >= ARTICLE_SECTION_SUMMARY_CHARS:
            break
    if not chosen:
        return ""
    summary = "\n".join(chosen)
    if len(summary) > ARTICLE_SECTION_SUMMARY_CHARS:
        summary = summary[:ARTICLE_SECTION_SUMMARY_CHARS].rsplit(" ", 1)[0]
    return summary

def _article_build_structure(
    video_id: str,
    title: str,
    headings: list[str],
    paragraphs: list[str],
) -> dict:
    """Build and return the persisted article structure/summary tree."""
    sections = _article_section_map(paragraphs, headings, title)
    for section in sections:
        section["summary"] = _article_section_summary(section, paragraphs)

    paragraph_map: dict[int, dict] = {}
    for section in sections:
        for index in range(int(section["start"]), int(section["end"]) + 1):
            paragraph_map[index] = {
                "index": index,
                "heading": section["heading"],
                "section": section["number"],
                "section_start": section["start"],
                "section_end": section["end"],
            }

    return {
        "version": ARTICLE_META_VERSION,
        "video_id": video_id,
        "title": title,
        "headings": headings,
        "overview": _article_overview(title, headings, paragraphs),
        "digest": _article_digest(title, headings, paragraphs),
        "sections": sections,
        "paragraph_map": [
            paragraph_map[index] for index in sorted(paragraph_map)
        ],
    }

def _article_structure_is_valid(
    structure: object, video_id: str, paragraph_count: int
) -> bool:
    if not isinstance(structure, dict):
        return False
    if structure.get("version") != ARTICLE_META_VERSION:
        return False
    if structure.get("video_id") != video_id:
        return False
    if not isinstance(structure.get("overview"), str):
        return False
    if not isinstance(structure.get("digest"), str):
        return False
    if not isinstance(structure.get("sections"), list):
        return False
    paragraph_map = structure.get("paragraph_map")
    return isinstance(paragraph_map, list) and len(paragraph_map) == paragraph_count

def _load_article_structure(
    cache_dir: Path, video_id: str, paragraph_count: int
) -> dict | None:
    meta_path = cache_dir / ARTICLE_META_FILENAME
    try:
        structure = json.loads(meta_path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    if not _article_structure_is_valid(structure, video_id, paragraph_count):
        return None
    return structure

def _save_article_structure(cache_dir: Path, structure: dict) -> None:
    persisted = {
        key: value
        for key, value in structure.items()
        if not key.startswith("_")
    }
    try:
        (cache_dir / ARTICLE_META_FILENAME).write_text(
            json.dumps(persisted, ensure_ascii=False, indent=2),
            encoding="utf-8",
        )
    except OSError:
        # The structure is already in memory; a read-only cache must not stop
        # /article from working.
        pass

def _article_add_runtime_indexes(structure: dict) -> dict:
    structure["_section_lookup"] = {
        section["number"]: section for section in structure.get("sections", [])
    }
    structure["_paragraph_lookup"] = {
        item["index"]: item for item in structure.get("paragraph_map", [])
    }
    return structure

def _article_heading_tokens(text: str) -> set[str]:
    return {
        token
        for token in chat.tokenize(text)
        if not token.isdigit() and token not in chat.FUZZY_ANCHOR_STOPWORDS
    }

def _article_match_section(question: str, structure: dict | None) -> int | None:
    """Return the section whose heading best matches the question, if any."""
    if not structure:
        return None
    question_text = question.casefold()
    question_tokens = _article_heading_tokens(question)
    best: tuple[float, int] | None = None
    for section in structure.get("sections", []):
        if int(section.get("end", 0)) <= int(
            section.get("heading_index") or section.get("start", 0)
        ):
            continue
        heading = str(section.get("heading", ""))
        heading_key = heading.casefold().strip()
        if not heading_key:
            continue
        heading_tokens = _article_heading_tokens(heading)
        score = 0.0
        if len(heading_key) >= 4 and heading_key in question_text:
            score = 1.5
        elif heading_tokens:
            overlap = question_tokens & heading_tokens
            coverage = len(overlap) / len(heading_tokens)
            if len(overlap) >= 1 and coverage >= 0.6:
                score = coverage + min(0.4, len(overlap) / 10)
        if score <= 0:
            continue
        if best is None or score > best[0]:
            best = (score, int(section["number"]))
    return best[1] if best is not None else None

def _article_classify_with_model(
    question: str, structure: dict | None
) -> ArticleRoute | None:
    """Ask the small model to disambiguate an uncertain article question."""
    if not structure:
        return None
    usable = [
        section
        for section in structure.get("sections", [])
        if int(section.get("end", 0))
        > int(section.get("heading_index") or section.get("start", 0))
    ]
    if len(usable) < 2:
        return None
    lines = []
    for section in usable[:80]:
        heading = str(section.get("heading", "")).splitlines()[0][:160]
        lines.append(f"{section['number']}. {heading}")
    prompt = ARTICLE_ROUTER_PROMPT.format(
        sections="\n".join(lines), question=question
    )
    try:
        response = chat.generate_text(
            prompt,
            model=ARTICLE_ROUTER_MODEL,
            temperature=0.0,
            num_predict=32,
            num_ctx=chat.ANSWER_NUM_CTX,
            stop=["\n"],
        )
    except RuntimeError:
        return None
    cleaned = chat.THINK_PATTERN.sub("", response).strip().upper()
    if cleaned.startswith("OVERVIEW"):
        return ArticleRoute("overview", source="model")
    match = re.search(r"SECTION\s+(\d+)", cleaned)
    if match:
        number = int(match.group(1))
        if number in structure.get("_section_lookup", {}):
            return ArticleRoute("section", number, source="model")
    if "NARROW" in cleaned:
        return ArticleRoute("narrow", source="model")
    return None

def article_question_route(
    question: str,
    structure: dict | None,
    iterative_default: bool = True,
) -> ArticleRoute:
    """Route an article question to overview, section, or narrow retrieval.

    The existing regex rules remain the final fallback. The small model is only
    consulted when the question looks section-oriented but no heading matches;
    a failed or unparseable model response therefore behaves exactly like the
    previous ``article_overview_question`` implementation.
    """
    if article_overview_question(question):
        return ArticleRoute("overview", source="regex")

    enumerative = bool(ARTICLE_ENUMERATIVE_RE.search(question))
    iterative = iterative_default or enumerative
    section = _article_match_section(question, structure)
    if section is not None and not enumerative:
        return ArticleRoute("section", section, False, "heading")

    if (
        section is None
        and not enumerative
        and structure
        and ARTICLE_SECTION_INTENT_RE.search(question)
    ):
        classified = _article_classify_with_model(question, structure)
        if classified is not None:
            if classified.label == "overview":
                return classified
            if classified.label == "section":
                return ArticleRoute(
                    "section", classified.section, False, "model"
                )
            if classified.label == "narrow":
                return ArticleRoute("narrow", None, iterative, "model")
    return ArticleRoute("narrow", None, iterative, "regex")

def _article_section_summary_index(section_number: int) -> int:
    return ARTICLE_SECTION_SUMMARY_INDEX_BASE - section_number

def _article_source_section(
    structure: dict | None, chunk_index: int
) -> dict | None:
    if not structure:
        return None
    section_lookup = structure.get("_section_lookup", {})
    if chunk_index <= ARTICLE_SECTION_SUMMARY_INDEX_BASE:
        return section_lookup.get(
            ARTICLE_SECTION_SUMMARY_INDEX_BASE - chunk_index
        )
    item = structure.get("_paragraph_lookup", {}).get(chunk_index)
    if not item:
        return None
    return section_lookup.get(item["section"])

def _make_article_expansion(
    structure: dict | None,
    chunks: list[chat.Chunk],
    budget: int,
    log: Callable[[str], None],
) -> tuple[Callable, dict]:
    """Return a parent-section/neighbor expansion callback for chat.answer."""
    by_index = {chunk.index: chunk for chunk in chunks}
    stats: dict = {"summaries": 0, "neighbors": 0, "added": 0}

    def prepare(
        selected: list[tuple[chat.Chunk, float]],
        _raw_selected: list[tuple[chat.Chunk, float]],
    ) -> list[tuple[chat.Chunk, float]]:
        if not structure:
            return selected
        result: list[tuple[chat.Chunk, float]] = []
        seen: set[int] = set()
        used = 0

        def add(chunk: chat.Chunk | None, score: float) -> bool:
            nonlocal used
            if chunk is None or chunk.index in seen:
                return False
            text = chat.paragraph_text(chunk)
            if not text:
                return False
            tokens = chat.estimate_tokens(text) + 16
            if result and used + tokens > budget:
                return False
            seen.add(chunk.index)
            result.append((chunk, score))
            used += tokens
            return True

        # Parent summaries first, so the model sees the heading that gives each
        # paragraph its meaning. Limit this to the strongest few matches.
        summary_sections: list[int] = []
        for chunk, _score in selected[:4]:
            item = structure.get("_paragraph_lookup", {}).get(chunk.index)
            if item and item["section"] not in summary_sections:
                summary_sections.append(item["section"])
        for number in summary_sections:
            summary_chunk = by_index.get(_article_section_summary_index(number))
            if add(summary_chunk, 0.0):
                stats["summaries"] += 1

        for chunk, score in selected:
            add(chunk, score)

        for chunk, _score in selected:
            item = structure.get("_paragraph_lookup", {}).get(chunk.index)
            if not item:
                continue
            # Neighbor expansion is deliberately local: one paragraph on each
            # side inside the same section, never the whole section.
            for neighbor_index in (chunk.index - 1, chunk.index + 1):
                neighbor_item = structure.get("_paragraph_lookup", {}).get(
                    neighbor_index
                )
                if not neighbor_item or neighbor_item["section"] != item["section"]:
                    continue
                if add(by_index.get(neighbor_index), 0.0):
                    stats["neighbors"] += 1

        stats["added"] = max(0, len(result) - len(selected))
        if stats["added"]:
            log(
                "[article: parent-section expansion added "
                f"{stats['summaries']} section summary(ies) and "
                f"{stats['neighbors']} neighbor paragraph(s)]"
            )
        return result or selected

    return prepare, stats

def _article_excerpts_for_check(
    evidence: list[tuple[chat.Chunk, float]], max_chars: int = 6000
) -> str:
    lines: list[str] = []
    used = 0
    for number, (chunk, _score) in enumerate(evidence, start=1):
        text = _normalize_article_text(chat.paragraph_text(chunk))
        if not text:
            continue
        snippet = text
        remaining = max_chars - used
        if remaining <= 0:
            break
        if len(snippet) > remaining:
            snippet = snippet[:remaining].rsplit(" ", 1)[0]
        lines.append(f"[{number}] {snippet}")
        used += len(snippet) + 8
    return "\n".join(lines)

def _missing_search_terms(missing: str) -> str:
    """Reduce a checker's prose answer to a few safe BM25 hint terms.

    The sufficiency model is asked for a short gap, but it often returns a
    sentence or a run of upper-case headings. Appending that verbatim made the
    next BM25 query treat every capitalized word as an acronym, scanning the
    whole corpus once per word (tens of seconds on a large corpus). Keep only
    a bounded set of normalized content words; these are retrieval hints, not
    part of the user's question.
    """
    terms: list[str] = []
    seen: set[str] = set()
    for token in chat.tokenize(missing):
        if (
            len(token) <= 1
            or token.isdigit()
            or token in chat.BM25_QUERY_STOPWORDS
            or token in seen
        ):
            continue
        seen.add(token)
        terms.append(token)
        if len(terms) >= MISSING_TERMS_MAX:
            break
    return " ".join(terms)

def _article_sufficiency_check(
    question: str,
    evidence: list[tuple[chat.Chunk, float]],
    *,
    enumerative: bool = False,
) -> dict:
    """Ask the small model whether the gathered evidence answers the question."""
    excerpts = _article_excerpts_for_check(evidence)
    if not excerpts:
        return {"sufficient": False, "missing": ""}
    template = (
        ARTICLE_ENUMERATIVE_SUFFICIENCY_PROMPT
        if enumerative
        else ARTICLE_SUFFICIENCY_PROMPT
    )
    prompt = template.format(question=question, excerpts=excerpts)
    try:
        response = chat.generate_text(
            prompt,
            model=ARTICLE_ROUTER_MODEL,
            temperature=0.0,
            num_predict=64,
            num_ctx=chat.ANSWER_NUM_CTX,
            stop=["\n\n"],
        )
    except RuntimeError:
        return {"sufficient": False, "missing": ""}
    cleaned = chat.THINK_PATTERN.sub("", response)
    verdict = re.search(r"VERDICT\s*:\s*(YES|NO)", cleaned, re.IGNORECASE)
    citation = re.search(r"EVIDENCE\s*:[^\n]*\d", cleaned, re.IGNORECASE)
    missing_match = re.search(r"MISSING\s*:\s*(.+)", cleaned, re.IGNORECASE)
    missing = missing_match.group(1).strip() if missing_match else ""
    if missing.upper().startswith("NONE"):
        missing = ""
    sufficient = bool(
        verdict
        and verdict.group(1).upper() == "YES"
        and citation is not None
    )
    return {"sufficient": sufficient, "missing": missing[:300]}

_PARAGRAPH_CHUNKS_CACHE: dict[int, tuple[list[chat.Chunk], list[chat.Chunk]]] = {}

def _is_article_heading_chunk(chunk: chat.Chunk) -> bool:
    """Return True when a learned-article chunk is just a section heading.

    Article trunks preserve headings as their own paragraph (Wikivoyage's
    ``Eat``, ``Sleep``, ``By plane``, and so on). A heading is a structural
    label, not a knowledge paragraph, and BM25's length normalization gives
    such a tiny document a very high score. For example, "what can i eat in
    Izumo" ranked the one-word ``Eat`` heading far above the soba and
    restaurant paragraphs it introduces, so the answer never contained them.
    Keep headings out of paragraph retrieval and let the body paragraphs,
    which repeat the useful section vocabulary, answer the question.
    """
    text = " ".join(chat.paragraph_text(chunk).split())
    return bool(text) and _article_heading_like(text)

def _iterative_paragraph_chunks(chunks: list[chat.Chunk]) -> list[chat.Chunk]:
    """Return a stable paragraph-only list for BM25 indexing.

    ``get_bm25_index`` caches by list identity. Building a fresh
    ``[chunk for chunk in chunks if chunk.index > 0]`` list on every question
    defeated that cache and rebuilt the whole corpus index (~2 seconds) each
    time. Keep one filtered list per source corpus instead. Standalone article
    headings are excluded because they are section labels, not evidence.
    """
    if not any(chunk.index <= 0 for chunk in chunks):
        # YouTube corpora contain only paragraph chunks, so no copy is needed.
        return chunks
    key = id(chunks)
    entry = _PARAGRAPH_CHUNKS_CACHE.get(key)
    if entry is not None and entry[0] is chunks:
        return entry[1]
    # Annotate article paragraphs with their enclosing section heading before
    # dropping the heading chunks. The heading then contributes to BM25 for
    # the section body without becoming evidence itself. For example, the
    # "Eat" heading makes the Izumo soba and Hara paragraphs match "what can i
    # eat in Izumo" even though those paragraphs do not repeat the word
    # "eat".
    chat.annotate_section_headings(chunks)
    filtered: list[chat.Chunk] = []
    for chunk in chunks:
        if chunk.index <= 0:
            continue
        # Heading chunks only exist in learned articles. Checking every
        # transcript paragraph here read the whole corpus on the first
        # question, so keep YouTube chunks without an extra file read.
        if chunk.video_id.startswith(chat.ARTICLE_CHUNK_PREFIXES) and (
            _is_article_heading_chunk(chunk)
        ):
            continue
        filtered.append(chunk)
    _PARAGRAPH_CHUNKS_CACHE[key] = (chunks, filtered)
    while len(_PARAGRAPH_CHUNKS_CACHE) > 4:
        _PARAGRAPH_CHUNKS_CACHE.pop(next(iter(_PARAGRAPH_CHUNKS_CACHE)))
    return filtered

def _evidence_covers_question(
    question: str, evidence: list[tuple[chat.Chunk, float]]
) -> bool:
    """Cheap check: does the gathered evidence mention every question term?

    The small-model sufficiency check costs more than the rest of a simple
    retrieval round. When the evidence already contains every content word
    from the question, there is no reason to ask the model. A miss (including
    a synonym or a misspelling) still falls back to the model, so this is a
    conservative fast path. Heading-only chunks do not count: a question word
    that appears only in the label ``Eat`` is not evidence that the gathered
    paragraphs describe what to eat.
    """
    if not evidence:
        return False
    question_terms = {
        token
        for token in chat.tokenize(question)
        if len(token) > 1 and token not in chat.FUZZY_ANCHOR_STOPWORDS
    }
    if not question_terms:
        return False
    substantive = [
        chunk
        for chunk, _score in evidence
        if not _is_article_heading_chunk(chunk)
    ]
    if not substantive:
        return False
    evidence_text = " ".join(
        chat.paragraph_text(chunk) for chunk in substantive
    )
    evidence_terms = set(chat.tokenize(evidence_text))
    return question_terms.issubset(evidence_terms)

def _iterative_evidence(
    question: str,
    chunks: list[chat.Chunk],
    budget: int,
    log: Callable[[str], None],
    *,
    label: str,
    structure: dict | None = None,
    max_rounds: int = 4,
    batch: int = 4,
    candidate_limit: int = 50,
    require_sufficient: bool = False,
    accept_insufficient: bool = False,
    apply_filter: bool = True,
    check_every: int = 1,
    enumerative: bool = False,
) -> list[tuple[chat.Chunk, float]]:
    """Gather evidence in small rounds, stopping when it is sufficient.

    This is the local analogue of ``chat_gradual_context.py``'s loop: retrieve
    a few new paragraphs, ask the small model whether they answer the question,
    and search again with the missing terms. It is capped by both the number of
    rounds and a hard evidence-token budget. ``/article`` adds section summaries
    through ``structure``.
    """
    seen: set[tuple[str, int]] = set()
    evidence: list[tuple[chat.Chunk, float]] = []
    used = 0
    search_question = question
    sufficient = False
    # A generative request has no factual "sufficient" state: the checker sees
    # "write me a 1000 word article" and can only ask for the article itself.
    # Make one wide retrieval pass and hand a useful context to the writer.
    long_form = chat.is_article_format_request(question)
    if long_form:
        max_rounds = 1
    enumerative = enumerative or bool(
        chat.LIST_QUERY_PATTERN.search(question)
    )
    paragraph_chunks = _iterative_paragraph_chunks(chunks)
    # Query expansion is a small-model call. Defer it until the first raw BM25
    # pass fails to cover the question: a direct fact question is usually
    # answered by the original terms alone, and the expanded terms only help
    # the follow-up round when retrieval came up short.
    expansion = ""
    expansion_done = False

    def ensure_expansion() -> str:
        nonlocal expansion, expansion_done
        if not expansion_done:
            expansion_done = True
            try:
                expansion = chat.expand_query(question)
            except RuntimeError:
                expansion = ""
        return expansion

    def rank_candidates(
        query: str, use_expansion: bool
    ) -> list[tuple[chat.Chunk, float]]:
        ranked = chat.top_chunks(
            query,
            paragraph_chunks,
            limit=candidate_limit,
            expansion=ensure_expansion() if use_expansion else "",
        )
        return [
            item
            for item in ranked
            if (item[0].video_id, item[0].index) not in seen
        ]

    filter_done = False
    previous_missing = ""

    for round_number in range(1, max_rounds + 1):
        if used >= budget:
            break
        candidates = rank_candidates(search_question, round_number > 1)
        if not candidates and round_number == 1 and not expansion_done:
            # The raw first pass found nothing at all. Retry it once with
            # expansion before concluding the corpus has no match.
            candidates = rank_candidates(search_question, True)
        if not candidates:
            break

        added = 0
        # Gather a wider first batch. A sufficiency check is the most
        # expensive step in the loop, so front-loading a few extra paragraphs
        # often answers the question after one check instead of two or three.
        round_batch = batch * 2 if round_number == 1 else batch
        if long_form:
            round_batch = max(round_batch, LONGFORM_ITERATIVE_FIRST_BATCH)

        # Cheap coverage check before the small-model relevance filter. When
        # the top ``batch`` candidates already contain every question term,
        # the evidence is self-evidently on-topic: skip the filter call and
        # keep the smaller batch, which cuts the answer model's prefill.
        covered_by_top_batch = (
            not long_form
            and not enumerative
            and _evidence_covers_question(
                question, evidence + candidates[:batch]
            )
        )
        if covered_by_top_batch:
            round_batch = min(round_batch, batch)

        # Run the small relevance filter once, when the candidate pool is in
        # its effective range, instead of on every round. Long-form requests
        # skip it entirely: their context is intentionally broad and the
        # filter would only delay the writer.
        if (
            apply_filter
            and not long_form
            and not covered_by_top_batch
            and not filter_done
            and chat.FILTER_MIN_CANDIDATES
            <= len(candidates)
            <= chat.FILTER_MAX_CANDIDATES
        ):
            filter_done = True
            filtered = chat.filter_candidates(
                search_question, candidates[: chat.FILTER_MAX_CANDIDATES]
            )
            if filtered:
                filtered_ids = {id(chunk) for chunk, _score in filtered}
                candidates = filtered + [
                    item for item in candidates
                    if id(item[0]) not in filtered_ids
                ]
        for chunk, score in candidates:
            key = (chunk.video_id, chunk.index)
            if key in seen:
                continue
            tokens = chat.estimate_tokens(chat.paragraph_text(chunk)) + 16
            if evidence and used + tokens > budget:
                continue
            evidence.append((chunk, score))
            seen.add(key)
            used += tokens
            added += 1
            if added >= round_batch:
                break
        if not added:
            break
        log(
            f"[{label}: round {round_number}/{max_rounds}, "
            f"added {added} paragraph(s), evidence ~{used:,} token(s)]"
        )
        if long_form:
            # One broad batch is the whole retrieval phase for an article,
            # essay, report, or guide request.
            sufficient = True
            log(
                f"[{label}: long-form request; using the "
                f"{len(evidence)} gathered paragraph(s)]"
            )
            break
        # Fast path: for a simple question, evidence that already contains all
        # of the question's content words is sufficient by inspection. This
        # skips a small-model call on most direct fact questions.
        if not enumerative and _evidence_covers_question(question, evidence):
            sufficient = True
            log(
                f"[{label}: evidence covers the question terms; "
                "stopping]"
            )
            break
        # Checking sufficiency is the expensive step. For enumerative
        # questions the wide first batch is checked immediately, then every
        # other round (1, 3, 5), so a complete list stops early while a
        # partial list still receives the full round budget. The enumerative
        # prompt also rejects a verdict based on one example.
        if (
            check_every > 1
            and round_number % check_every != 1
            and round_number != max_rounds
        ):
            continue
        verdict = _article_sufficiency_check(
            question, evidence, enumerative=enumerative
        )
        if verdict["sufficient"]:
            sufficient = True
            log(f"[{label}: evidence is sufficient; stopping]")
            break
        if verdict["missing"]:
            missing_terms = _missing_search_terms(verdict["missing"])
            missing_key = missing_terms or verdict["missing"].strip().lower()
            if round_number > 1 and missing_key == previous_missing:
                # The last round did not change what the model says is
                # missing, so another identical search will not help.
                log(
                    f"[{label}: missing terms did not change; "
                    "stopping early]"
                )
                break
            previous_missing = missing_key
            if missing_terms:
                search_question = f"{question} {missing_terms}"

    if require_sufficient and not sufficient:
        if not accept_insufficient:
            # The gathered paragraphs never answered the question. Fall back
            # to the normal retrieval path instead of forcing unrelated
            # evidence into the prompt.
            log(
                f"[{label}: evidence stayed insufficient after "
                f"{max_rounds} round(s); using normal retrieval]"
            )
            return []
        # The checker is intentionally conservative (especially for list
        # questions). We already have question-ranked paragraphs, so use them
        # and let the answer model decide. Falling back to a second BM25 pass
        # repeated roughly two seconds of work and replaced broader evidence
        # with a much smaller context.
        log(
            f"[{label}: checker still wanted more after "
            f"{max_rounds} round(s); using the {len(evidence)} gathered "
            "paragraph(s)]"
        )

    if not evidence or not structure:
        return evidence

    # Add one parent summary per section represented in the evidence, keeping
    # the final context in document order for coherent synthesis.
    final: list[tuple[chat.Chunk, float]] = []
    seen_summaries: set[int] = set()
    by_index = {chunk.index: chunk for chunk in chunks}
    for chunk, score in sorted(evidence, key=lambda item: item[0].index):
        item = structure.get("_paragraph_lookup", {}).get(chunk.index)
        if item and item["section"] not in seen_summaries:
            seen_summaries.add(item["section"])
            summary_chunk = by_index.get(
                _article_section_summary_index(item["section"])
            )
            if summary_chunk is not None:
                final.append((summary_chunk, 0.0))
        final.append((chunk, score))

    limited: list[tuple[chat.Chunk, float]] = []
    total = 0
    for chunk, score in final:
        tokens = chat.estimate_tokens(chat.paragraph_text(chunk)) + 16
        if limited and total + tokens > budget:
            continue
        limited.append((chunk, score))
        total += tokens
    return limited

def _article_iterative_evidence(
    question: str,
    chunks: list[chat.Chunk],
    structure: dict | None,
    budget: int,
    log: Callable[[str], None],
) -> list[tuple[chat.Chunk, float]]:
    """Run iterative evidence gathering for the active article."""
    return _iterative_evidence(
        question,
        chunks,
        budget,
        log,
        label="context gathering",
        structure=structure,
        max_rounds=ARTICLE_ITERATIVE_MAX_ROUNDS,
        batch=ARTICLE_ITERATIVE_BATCH,
        candidate_limit=ARTICLE_ITERATIVE_CANDIDATES,
    )

def _scoped_context_fallback(
    chunks: list[chat.Chunk],
    budget: int,
) -> list[chat.Chunk]:
    """Return a scoped corpus in reading order within the context budget.

    This is the last line of defence for an isolated session (a learned news
    story, one transcript, or one book). A question can have no BM25 overlap
    with an otherwise useful article; returning no context made the modal's
    chat appear to do nothing at all. Keeping the overview/first chunks plus
    as many following chunks as fit gives the answer model real source text
    while still respecting the context window.
    """
    if not chunks:
        return []
    if budget is None or budget <= 0:
        budget = YOUTUBE_ITERATIVE_EVIDENCE_TOKENS
    ordered = sorted(chunks, key=lambda chunk: (chunk.index, chunk.video_id))
    selected: list[chat.Chunk] = []
    for chunk in ordered:
        trial = selected + [chunk]
        context = chat.build_context(trial)
        if selected and chat.estimate_tokens(context) > budget:
            continue
        selected = trial
        if chat.estimate_tokens(context) >= budget:
            break
    return selected

def _youtube_iterative_evidence(
    question: str,
    chunks: list[chat.Chunk],
    budget: int,
    log: Callable[[str], None],
    depth: int | None = None,
    cache: dict | None = None,
) -> list[tuple[chat.Chunk, float]]:
    """Iteratively gather YouTube transcript evidence for a normal question.

    ``/article`` already answers by retrieving a few paragraphs at a time and
    asking the small model whether the evidence is sufficient. All transcript
    questions use the same loop here: the global knowledge base is searched in
    rounds, and retrieval stops as soon as the gathered paragraphs can answer
    the whole question. List/enumeration questions get more rounds and a larger
    budget because they need broader coverage.

    ``cache`` stores the evidence for the current question so repeated scopes
    can reuse it instead of rerunning expansion, BM25, and the small-model
    sufficiency loop.
    """
    enumerative = bool(chat.LIST_QUERY_PATTERN.search(question))
    effective_budget = budget
    if effective_budget <= 0:
        effective_budget = YOUTUBE_ITERATIVE_EVIDENCE_TOKENS
    if enumerative:
        effective_budget = max(effective_budget, chat.LIST_CONTEXT_TOKENS)
    candidate_limit = (
        depth if depth is not None and depth > 0 else YOUTUBE_ITERATIVE_CANDIDATES
    )
    if chat.is_article_format_request(question):
        # A long-form answer needs a broader evidence base than a factual
        # question, and it gets only one retrieval pass. Keep enough
        # candidates for the writer without exceeding the relevance filter's
        # bounded range.
        candidate_limit = max(candidate_limit, LONGFORM_ITERATIVE_CANDIDATES)
    # Keep the generic relevance filter to remove off-topic BM25 matches.
    base_apply_filter = not enumerative
    cache_key = (
        question,
        id(chunks),
        effective_budget,
        candidate_limit,
        enumerative,
        base_apply_filter,
    )
    base = cache.get(cache_key) if cache is not None else None
    if base is None:
        base = _iterative_evidence(
            question,
            chunks,
            effective_budget,
            log,
            label="context gathering",
            max_rounds=(
                YOUTUBE_ITERATIVE_ENUMERATIVE_ROUNDS
                if enumerative
                else YOUTUBE_ITERATIVE_MAX_ROUNDS
            ),
            batch=(
                YOUTUBE_ITERATIVE_ENUMERATIVE_BATCH
                if enumerative
                else YOUTUBE_ITERATIVE_BATCH
            ),
            candidate_limit=candidate_limit,
            require_sufficient=True,
            # A conservative "not sufficient" verdict must not throw away
            # question-ranked evidence and trigger a second retrieval pass.
            accept_insufficient=True,
            apply_filter=base_apply_filter,
            check_every=2 if enumerative else 1,
            enumerative=enumerative,
        )
        if cache is not None:
            cache[cache_key] = base

    return base

def _decode_article_bytes(data: bytes) -> str:
    """Decode a local article without assuming a single text encoding."""
    if data.startswith((b"\xff\xfe", b"\xfe\xff")):
        return data.decode("utf-16")
    for encoding in ("utf-8-sig", "utf-8", "cp1252", "latin-1"):
        try:
            return data.decode(encoding)
        except UnicodeDecodeError:
            continue
    return data.decode("utf-8", errors="replace")

def _article_title(text: str, path: Path) -> str:
    """Return a short title for the article, falling back to its filename."""
    for line in text.splitlines():
        line = line.strip()
        if not line:
            continue
        heading = ARTICLE_MARKDOWN_HEADING_RE.match(line)
        if heading:
            line = heading.group("title").strip()
        if line:
            return line[:200]
    return path.stem

def _load_article_chunks(
    path: Path,
) -> tuple[list[chat.Chunk], str, dict]:
    """Split one article into a cached summary tree and paragraph trunks.

    The path and bytes are content-hashed, so reloading an unchanged article
    reuses the generated structure: document overview, digest, per-section
    extractive summaries, and the section span for every paragraph. Paragraph
    trunks remain the retrieval source of truth; the summaries are synthetic
    context used by the article router.
    """
    data = path.read_bytes()
    raw_text = _decode_article_bytes(data)
    text = raw_text
    if path.suffix.lower() in {".html", ".htm", ".xhtml"}:
        text = ARTICLE_HTML_BLOCK_RE.sub("\n", text)
        text = ARTICLE_HTML_BREAK_RE.sub("\n\n", text)
        text = ARTICLE_HTML_TAG_RE.sub(" ", text)
        text = html.unescape(text)
    text = text.replace("\r\n", "\n").replace("\r", "\n").strip()
    if not text:
        raise ValueError("the article is empty")

    headings = _article_headings(raw_text, text)
    title = _article_title(text, path)
    paragraphs = split_paragraphs(text)
    if not paragraphs:
        raise ValueError("the article contains no readable paragraphs")

    digest = hashlib.sha1(
        str(path).encode("utf-8") + b"\0" + data
    ).hexdigest()[:16]
    video_id = f"article_{digest}"
    cache_dir = ARTICLE_CACHE_DIR / video_id
    cache_dir.mkdir(parents=True, exist_ok=True)
    for old_trunk in cache_dir.glob("*.trunk"):
        old_trunk.unlink()

    # The summary tree is expensive to build and is a pure function of the
    # article bytes, so load it from the content-addressed cache when valid.
    structure = _load_article_structure(
        cache_dir, video_id, len(paragraphs)
    )
    if structure is None:
        structure = _article_build_structure(
            video_id, title, headings, paragraphs
        )
        _save_article_structure(cache_dir, structure)
    _article_add_runtime_indexes(structure)
    overview = str(structure["overview"])
    digest_text = str(structure["digest"])

    # Make the title part of the first paragraph so a broad question such as
    # "what is this about?" still retrieves the article heading. Do this after
    # the section map is built, so paragraph indexes stay aligned.
    if title and title.casefold() not in paragraphs[0].casefold():
        paragraphs[0] = f"{title}\n\n{paragraphs[0]}"

    # Chunk 0 is a compact document overview (title + outline + TL;DR). It
    # gives article-wide questions useful context even when their generic
    # wording ("what is this article about?") has no good BM25 match among
    # thousands of paragraphs.
    overview_trunk = (
        f"<article><title>{xml_escape(title)}</title>"
        f"<paragraph>{xml_escape(overview)}</paragraph></article>"
    )
    overview_path = cache_dir / f"{video_id}.0.trunk"
    overview_path.write_text(overview_trunk + "\n", encoding="utf-8")
    chunks: list[chat.Chunk] = [
        chat.Chunk(video_id=video_id, index=0, trunk_path=overview_path)
    ]

    # Chunk -1 is a broader digest used only for article-wide questions. It
    # is excluded from ordinary BM25 retrieval (see _run_question) so its
    # many headings and terms cannot crowd out question-specific paragraphs.
    digest_trunk = (
        f"<article><title>{xml_escape(title)}</title>"
        f"<paragraph>{xml_escape(digest_text)}</paragraph></article>"
    )
    digest_path = cache_dir / f"{video_id}.-1.trunk"
    digest_path.write_text(digest_trunk + "\n", encoding="utf-8")
    chunks.append(
        chat.Chunk(video_id=video_id, index=-1, trunk_path=digest_path)
    )

    # Per-section summaries are cached in the structure file. Each one becomes
    # a synthetic trunk with a unique negative index so it can be attached as
    # parent context without entering normal BM25 retrieval.
    for section in structure["sections"]:
        summary = str(section.get("summary") or "")
        if not summary:
            continue
        summary_index = _article_section_summary_index(int(section["number"]))
        summary_text = f"{section['heading']}\n{summary}"
        summary_trunk = (
            f"<article><title>{xml_escape(title)}</title>"
            f"<paragraph>{xml_escape(summary_text)}</paragraph></article>"
        )
        summary_path = (
            cache_dir / f"{video_id}.section-{section['number']}.trunk"
        )
        summary_path.write_text(summary_trunk + "\n", encoding="utf-8")
        chunks.append(
            chat.Chunk(
                video_id=video_id,
                index=summary_index,
                trunk_path=summary_path,
            )
        )

    for index, paragraph in enumerate(paragraphs, start=1):
        trunk = (
            f"<article><title>{xml_escape(title)}</title>"
            f"<paragraph>{xml_escape(paragraph)}</paragraph></article>"
        )
        trunk_path = cache_dir / f"{video_id}.{index}.trunk"
        trunk_path.write_text(trunk + "\n", encoding="utf-8")
        chunks.append(
            chat.Chunk(video_id=video_id, index=index, trunk_path=trunk_path)
        )
    # Persist the article's trunks and inverted index together, matching the
    # news learning path. Unchanged chunks are skipped by the file signature.
    try:
        chat.index_chunks_in_sqlite(chunks)
    except Exception:
        pass
    return chunks, title, structure

# The /gutenberg command and its two-column book modal. The catalog search is
# intentionally small and stateless; the per-book chat state is created lazily
# the first time a question is asked, so opening the modal costs only the
# plaintext download. Each book gets at most one on-disk copy and one in-memory
# article index.
GUTENBERG_CATALOG_CACHE: dict[str, dict] = {}

GUTENBERG_DOWNLOAD_LOCKS: dict[str, threading.Lock] = {}
