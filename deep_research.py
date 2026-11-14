"""Bounded deep-research loop used by the web chat's ``/deepresearch`` command.

The normal answer path retrieves one BM25 slice and writes one answer. This
module implements the loop described in ``research/deep_research_mode.html``:

* classify the request and plan 3-6 sub-questions;
* retrieve a small evidence set for every sub-question with several queries;
* keep an append-only evidence ledger with source ids and excerpts;
* check gaps after the first round and retrieve only for what is still missing;
* write the report section by section inside the answer model's context window;
* verify citation integrity and report what the corpus does not establish.

The module deliberately depends only on ``chat`` so the HTTP server can pass
its current in-memory corpus and stream callbacks. It does not import the
server, which keeps the loop testable without starting a browser session.
"""

from __future__ import annotations

import json
import re
import time
from dataclasses import dataclass, field
from datetime import datetime
from typing import Callable

import chat

LogFn = Callable[[str], None]
EmitFn = Callable[[str], None]
SourceInfoFn = Callable[[chat.Chunk], tuple[str, str]]

RESEARCH_DIR = chat.CHAT_DIR / "research"
PLANNER_MODEL = chat.EXPANSION_MODEL
WRITER_MODEL = chat.ANSWER_MODEL

MIN_SUBQUESTIONS = 3
MAX_SUBQUESTIONS = 6
MAX_SEED_QUERIES = 3
CANDIDATES_PER_QUERY = 8
EVIDENCE_PER_SUBQUESTION = 5
MAX_UNIQUE_SOURCES = 30
MAX_CHUNKS_PER_SOURCE = 4
MAX_ROUNDS = 2
SECTION_EVIDENCE_TOKENS = 5_200
SUMMARY_EVIDENCE_TOKENS = 3_600
MAX_EVIDENCE_TEXT_CHARS = 2_000

PLANNER_SCHEMA = {
    "type": "object",
    "properties": {
        "intent": {"type": "string"},
        "subquestions": {
            "type": "array",
            "items": {
                "type": "object",
                "properties": {
                    "id": {"type": "string"},
                    "text": {"type": "string"},
                    "priority": {"type": "integer"},
                    "seed_queries": {
                        "type": "array",
                        "items": {"type": "string"},
                    },
                },
                "required": ["text"],
            },
        },
    },
    "required": ["intent", "subquestions"],
}

GAP_SCHEMA = {
    "type": "object",
    "properties": {
        "gaps": {
            "type": "array",
            "items": {
                "type": "object",
                "properties": {
                    "id": {"type": "string"},
                    "status": {"type": "string"},
                    "missing": {"type": "string"},
                    "new_queries": {
                        "type": "array",
                        "items": {"type": "string"},
                    },
                },
                "required": ["id", "status"],
            },
        },
        "contradictions": {
            "type": "array",
            "items": {
                "type": "object",
                "properties": {
                    "claim": {"type": "string"},
                    "sources": {
                        "type": "array",
                        "items": {"type": "string"},
                    },
                },
                "required": ["claim"],
            },
        },
    },
    "required": ["gaps"],
}

PLANNER_SYSTEM = (
    "You are a careful research planner. Decompose the user's question into "
    "a small set of sub-questions that, taken together, would answer it. Do "
    "not answer the question in the plan. Return JSON only."
)

WRITER_RULES = """\
Write one section of a grounded research report.
Rules:
- Use ONLY the evidence below. Do not add facts from memory.
- Cite every factual sentence with the matching evidence id, for example [E01].
- Never cite an id that is not in the evidence list.
- If the evidence does not support a sentence, omit the sentence.
- If sources disagree, say so explicitly and cite both sides.
- Do not mention "chunk", "context", "provided documents", or internal ids.
- Do not write a source list; the report has one at the end.
- Do not repeat the section title and do not add a heading. The application
  already writes the section heading.
- Write clear, compact prose. Use a short bullet list only when it helps.
"""


@dataclass
class Evidence:
    """One piece of retrieved evidence in the research ledger."""

    evidence_id: str
    chunk: chat.Chunk
    score: float
    subquestions: set[str] = field(default_factory=set)
    excerpt: str = ""
    title: str = ""
    url: str = ""
    source_quality: str = "corpus"

    @property
    def key(self) -> tuple[str, int]:
        return (self.chunk.video_id, self.chunk.index)

    @property
    def chunk_key(self) -> str:
        return chat.chunk_key(self.chunk)

    def as_json(self) -> dict:
        return {
            "evidence_id": self.evidence_id,
            "chunk_key": self.chunk_key,
            "source_id": self.chunk.video_id,
            "chunk_index": self.chunk.index,
            "title": self.title,
            "url": self.url,
            "bm25_score": self.score,
            "subquestions": sorted(self.subquestions),
            "excerpt": self.excerpt,
            "source_quality": self.source_quality,
            "text": chat.paragraph_text(self.chunk)[:MAX_EVIDENCE_TEXT_CHARS],
        }


def _log(log: LogFn | None, message: str) -> None:
    if log is not None:
        log(message)


def _emit(emit: EmitFn | None, text: str) -> None:
    if emit is not None and text:
        emit(text)


def _clean_text(value: object, limit: int = 500) -> str:
    text = " ".join(str(value or "").split())
    return text[:limit].strip()


def _short_title(text: str, limit: int = 96) -> str:
    title = _clean_text(text, limit * 2).rstrip("?.!:;")
    if len(title) > limit:
        title = title[: limit - 1].rsplit(" ", 1)[0] + "…"
    return title or "Research question"


def _source_info(
    chunk: chat.Chunk, source_info: SourceInfoFn | None
) -> tuple[str, str, str]:
    """Return (title, url, quality) without depending on the HTTP server."""
    if source_info is not None:
        try:
            title, url = source_info(chunk)
            return (
                _clean_text(title, 240) or chunk.video_id,
                _clean_text(url, 500),
                _quality_for_chunk(chunk),
            )
        except Exception:  # A metadata lookup must never stop research.
            pass
    title = ""
    try:
        title = chat.video_title(chunk.video_id)
    except Exception:
        title = ""
    if chunk.video_id.startswith(chat.NEWS_CHUNK_PREFIX):
        url = ""
    elif chunk.video_id.startswith(chat.ARTICLE_CHUNK_PREFIXES):
        url = ""
    else:
        url = f"https://www.youtube.com/watch?v={chunk.video_id}"
    return title or chunk.video_id, url, _quality_for_chunk(chunk)


def _quality_for_chunk(chunk: chat.Chunk) -> str:
    if chunk.video_id.startswith(chat.NEWS_CHUNK_PREFIX):
        return "news"
    if chunk.video_id.startswith(chat.ARTICLE_CHUNK_PREFIXES):
        return "learned_article"
    return "video_transcript"


def _tokens(text: str) -> set[str]:
    return set(chat.tokenize(text))


def _jaccard(left: set[str], right: set[str]) -> float:
    if not left or not right:
        return 0.0
    union = left | right
    return len(left & right) / len(union) if union else 0.0


def _excerpt(text: str, query: str, max_chars: int = 420) -> str:
    """Return the most query-relevant sentence(s) from one paragraph."""
    body = " ".join((text or "").split())
    if not body:
        return ""
    query_terms = _tokens(query)
    sentences = re.split(r"(?<=[.!?])\s+", body)
    best = ""
    best_overlap = -1
    for sentence in sentences:
        overlap = len(_tokens(sentence) & query_terms)
        if overlap > best_overlap:
            best = sentence
            best_overlap = overlap
    if not best:
        best = body
    if len(best) > max_chars:
        best = best[: max_chars - 1].rsplit(" ", 1)[0] + "…"
    return best.strip()


def _fallback_plan(topic: str) -> dict:
    """A plan that works even when the helper model is unavailable."""
    core = _question_core(topic) or topic
    lowered = topic.lower()
    if chat.LIST_QUERY_PATTERN.search(topic):
        intent = "enumeration"
    elif re.search(r"\b(?:compare|comparison|versus|vs\.?)\b", lowered):
        intent = "comparison"
    elif re.search(r"\b(?:timeline|history|when did|chronolog)\b", lowered):
        intent = "timeline"
    else:
        intent = "open_ended"
    return {
        "intent": intent,
        "subquestions": [
            {
                "id": "q1",
                "text": f"What are the direct facts reported about {core}?",
                "seed_queries": [topic, f"{core} facts"],
            },
            {
                "id": "q2",
                "text": f"Which names, dates, numbers, and specific details are reported about {core}?",
                "seed_queries": [
                    f"{core} details",
                    f"{core} dates numbers",
                ],
            },
            {
                "id": "q3",
                "text": f"What background, causes, or explanations are given about {core}?",
                "seed_queries": [
                    f"{core} background",
                    f"{core} explanation",
                ],
            },
            {
                "id": "q4",
                "text": f"What remains uncertain, disputed, or unverified about {core}?",
                "seed_queries": [
                    f"{core} uncertain disputed unverified",
                    f"{core} reported alleged confirmed",
                ],
            },
        ],
    }


def _question_core(topic: str) -> str:
    """Drop question framing words so fallback queries keep the entity."""
    tokens = [
        token
        for token in chat.tokenize(topic)
        if len(token) > 1
        and token not in chat.BM25_QUERY_STOPWORDS
        and token not in chat.FUZZY_ANCHOR_STOPWORDS
    ]
    return " ".join(tokens)


def _plan_topic(topic: str, log: LogFn | None) -> dict:
    prompt = (
        f"Question: {topic}\n\n"
        f"Create {MIN_SUBQUESTIONS}-{MAX_SUBQUESTIONS} sub-questions. "
        "Each sub-question needs 2-3 short seed_queries for a local BM25 "
        "search over a corpus of learned articles and video transcripts. "
        "For speculation or private facts, include separate questions for "
        "what is reported, what is confirmed or admitted, what is only "
        "speculated, and what is unknown.\n\n"
        "Return JSON with this shape:\n"
        '{"intent":"fact|timeline|comparison|enumeration|causal|'
        'open_ended|speculation","subquestions":[{"id":"q1","text":"...",'
        '"priority":1,"seed_queries":["...","..."]}]}'
    )
    try:
        raw, _event = chat._lfm_request_json(
            PLANNER_SYSTEM,
            prompt,
            PLANNER_SCHEMA,
            PLANNER_MODEL,
            temperature=0.0,
            num_predict=700,
        )
    except RuntimeError as exc:
        _log(
            log,
            f"[research] planner model unavailable ({exc}); using the "
            "deterministic research plan",
        )
        raw = None
    if not raw:
        return _fallback_plan(topic)

    subquestions = raw.get("subquestions")
    if not isinstance(subquestions, list):
        return _fallback_plan(topic)
    sanitized: list[dict] = []
    for index, item in enumerate(subquestions[:MAX_SUBQUESTIONS], start=1):
        if not isinstance(item, dict):
            continue
        text = _clean_text(item.get("text"), 300)
        if not text:
            continue
        seeds = item.get("seed_queries")
        queries: list[str] = []
        if isinstance(seeds, list):
            for seed in seeds:
                query = _clean_text(seed, 300)
                if query and query not in queries:
                    queries.append(query)
                if len(queries) >= MAX_SEED_QUERIES:
                    break
        if text not in queries:
            queries.insert(0, text)
        sanitized.append(
            {
                "id": f"q{index}",
                "text": text,
                "priority": int(item.get("priority") or index),
                "seed_queries": queries[:MAX_SEED_QUERIES],
            }
        )
    if len(sanitized) < MIN_SUBQUESTIONS:
        return _fallback_plan(topic)
    return {
        "intent": _clean_text(raw.get("intent"), 40) or "open_ended",
        "subquestions": sanitized,
    }


def _add_evidence(
    ledger: list[Evidence],
    by_key: dict[tuple[str, int], Evidence],
    source_counts: dict[str, int],
    text_tokens: list[set[str]],
    chunk: chat.Chunk,
    score: float,
    subquestion_id: str,
    query: str,
    source_info: SourceInfoFn | None,
) -> bool:
    """Add one chunk, merging duplicates and enforcing diversity caps."""
    key = (chunk.video_id, chunk.index)
    existing = by_key.get(key)
    if existing is not None:
        existing.subquestions.add(subquestion_id)
        if score > existing.score:
            existing.score = score
        return False
    if source_counts.get(chunk.video_id, 0) >= MAX_CHUNKS_PER_SOURCE:
        return False
    if len({item.chunk.video_id for item in ledger}) >= MAX_UNIQUE_SOURCES:
        if chunk.video_id not in source_counts:
            return False
    body = chat.paragraph_text(chunk)
    tokens = _tokens(body)
    if any(
        _jaccard(tokens, previous) >= chat.NEAR_DUPLICATE_JACCARD
        for previous in text_tokens
    ):
        return False
    title, url, quality = _source_info(chunk, source_info)
    evidence = Evidence(
        evidence_id=f"E{len(ledger) + 1:02d}",
        chunk=chunk,
        score=float(score),
        subquestions={subquestion_id},
        excerpt=_excerpt(body, query),
        title=title,
        url=url,
        source_quality=quality,
    )
    ledger.append(evidence)
    by_key[key] = evidence
    source_counts[chunk.video_id] = source_counts.get(chunk.video_id, 0) + 1
    text_tokens.append(tokens)
    return True


def _retrieve_round(
    queries: list[tuple[str, str]],
    chunks: list[chat.Chunk],
    ledger: list[Evidence],
    by_key: dict[tuple[str, int], Evidence],
    source_counts: dict[str, int],
    text_tokens: list[set[str]],
    source_info: SourceInfoFn | None,
    log: LogFn | None,
    round_number: int,
) -> tuple[int, int]:
    """Run one retrieval round; return (candidate count, added evidence)."""
    candidates_seen: set[tuple[str, int]] = set()
    added = 0
    for query, subquestion_id in queries:
        try:
            ranked = chat.top_chunks(
                query,
                chunks,
                limit=CANDIDATES_PER_QUERY,
                expansion="",
            )
        except Exception as exc:  # Retrieval must not stop the research loop.
            _log(log, f"[research] retrieval failed for {query!r}: {exc}")
            continue
        for chunk, score in ranked:
            candidates_seen.add((chunk.video_id, chunk.index))
            if _add_evidence(
                ledger,
                by_key,
                source_counts,
                text_tokens,
                chunk,
                score,
                subquestion_id,
                query,
                source_info,
            ):
                added += 1
    _log(
        log,
        f"[research] round {round_number}: {len(candidates_seen)} candidate(s) "
        f"-> {added} new evidence item(s), {len(ledger)} total",
    )
    return len(candidates_seen), added


def _queries_for_round_one(plan: dict, topic: str) -> list[tuple[str, str]]:
    queries: list[tuple[str, str]] = []
    for index, subquestion in enumerate(plan["subquestions"]):
        subquestion_id = str(subquestion["id"])
        seeds = list(subquestion.get("seed_queries") or [])
        if index == 0 and topic not in seeds:
            seeds.insert(0, topic)
        for seed in seeds[:MAX_SEED_QUERIES]:
            queries.append((seed, subquestion_id))
    return queries


def _deterministic_gap_queries(
    plan: dict, ledger: list[Evidence]
) -> list[tuple[str, str]]:
    """Fallback second-round queries for sub-questions with little evidence."""
    counts = {str(item["id"]): 0 for item in plan["subquestions"]}
    for evidence in ledger:
        for subquestion_id in evidence.subquestions:
            if subquestion_id in counts:
                counts[subquestion_id] += 1
    queries: list[tuple[str, str]] = []
    for subquestion in plan["subquestions"]:
        subquestion_id = str(subquestion["id"])
        if counts.get(subquestion_id, 0) >= 2:
            continue
        text = str(subquestion["text"]).rstrip("?")
        seeds = list(subquestion.get("seed_queries") or [])
        for query in [text, f"{text} report", *seeds]:
            candidate = (query, subquestion_id)
            if candidate not in queries:
                queries.append(candidate)
            if len(queries) >= 2 * MAX_SUBQUESTIONS:
                break
        if len(queries) >= 2 * MAX_SUBQUESTIONS:
            break
    return queries


def _evidence_excerpts(evidence: list[Evidence], max_chars: int = 3_600) -> str:
    lines: list[str] = []
    used = 0
    for item in evidence:
        line = f"[{item.evidence_id}] {item.title}: {item.excerpt}"
        if used + len(line) > max_chars and lines:
            break
        lines.append(line)
        used += len(line) + 1
    return "\n".join(lines)


def _gap_check(
    topic: str,
    plan: dict,
    ledger: list[Evidence],
    log: LogFn | None,
) -> tuple[list[tuple[str, str]], list[str]]:
    """Ask the helper model which sub-questions still need evidence.

    Returns ``(new_queries, contradictions)``. The queries target only the
    missing facts; contradictions are kept for the research trace.
    """
    sections: list[str] = []
    for subquestion in plan["subquestions"]:
        subquestion_id = str(subquestion["id"])
        items = [
            item
            for item in ledger
            if subquestion_id in item.subquestions
        ][:3]
        sections.append(
            f"Sub-question {subquestion_id}: {subquestion['text']}\n"
            + (
                _evidence_excerpts(items, max_chars=1_200)
                or "(no evidence found)"
            )
        )
    prompt = (
        f"Original question: {topic}\n\n"
        + "\n\n".join(sections)
        + "\n\nFor each sub-question, decide whether the evidence states the "
        "fact (answered), states part of it (partial), or does not state it "
        "(unanswered). Missing facts must be specific. New queries must target "
        "the missing fact instead of repeating earlier queries. Also list any "
        "material contradictions between evidence items.\n\n"
        "Return JSON:\n"
        '{"gaps":[{"id":"q1","status":"answered|partial|unanswered",'
        '"missing":"...","new_queries":["..."]}],'
        '"contradictions":[{"claim":"...","sources":["E01","E02"]}]}'
    )
    try:
        raw, _event = chat._lfm_request_json(
            "You are a strict research gap checker. Return JSON only.",
            prompt,
            GAP_SCHEMA,
            PLANNER_MODEL,
            temperature=0.0,
            num_predict=800,
        )
    except RuntimeError as exc:
        _log(
            log,
            f"[research] gap checker unavailable ({exc}); using deterministic "
            "gap queries",
        )
        return _deterministic_gap_queries(plan, ledger), []
    if not raw:
        return _deterministic_gap_queries(plan, ledger), []

    queries: list[tuple[str, str]] = []
    contradictions_found: list[str] = []
    answered = 0
    partial = 0
    unanswered = 0
    gaps = raw.get("gaps")
    if isinstance(gaps, list):
        for gap in gaps:
            if not isinstance(gap, dict):
                continue
            subquestion_id = _clean_text(gap.get("id"), 20)
            if not subquestion_id:
                continue
            status = _clean_text(gap.get("status"), 20).lower()
            if status == "answered":
                answered += 1
                continue
            if status == "partial":
                partial += 1
            else:
                unanswered += 1
            missing = _clean_text(gap.get("missing"), 200)
            new_queries = gap.get("new_queries")
            added = 0
            if isinstance(new_queries, list):
                for query in new_queries:
                    value = _clean_text(query, 300)
                    if value:
                        queries.append((value, subquestion_id))
                        added += 1
                    if added >= 2:
                        break
            if added == 0:
                subquestion = next(
                    (
                        item
                        for item in plan["subquestions"]
                        if str(item["id"]) == subquestion_id
                    ),
                    None,
                )
                if subquestion is not None:
                    queries.append(
                        (
                            f"{subquestion['text'].rstrip('?')} {missing}".strip(),
                            subquestion_id,
                        )
                    )
    contradictions = raw.get("contradictions")
    if isinstance(contradictions, list):
        for item in contradictions[:5]:
            if not isinstance(item, dict):
                continue
            claim = _clean_text(item.get("claim"), 300)
            sources = item.get("sources")
            source_text = ""
            if isinstance(sources, list):
                source_text = ", ".join(
                    _clean_text(source, 20) for source in sources if source
                )
            if claim:
                note = claim + (f" ({source_text})" if source_text else "")
                contradictions_found.append(note)
                _log(log, f"[research] contradiction noted: {note}")
    _log(
        log,
        f"[research] gap check: {answered} answered, {partial} partial, "
        f"{unanswered} unanswered",
    )
    if not queries:
        queries = _deterministic_gap_queries(plan, ledger)
    return queries, contradictions_found


def _evidence_block(item: Evidence) -> str:
    text = chat.paragraph_text(item.chunk)
    if len(text) > MAX_EVIDENCE_TEXT_CHARS:
        text = text[:MAX_EVIDENCE_TEXT_CHARS].rsplit(" ", 1)[0] + "…"
    source = f"{item.title}"
    if item.url:
        source += f" | {item.url}"
    return f"[{item.evidence_id}] {source}\n{text}"


def _select_evidence(
    evidence: list[Evidence], budget_tokens: int, limit: int = 8
) -> list[Evidence]:
    selected: list[Evidence] = []
    used = 0
    for item in sorted(evidence, key=lambda entry: entry.score, reverse=True):
        block = _evidence_block(item)
        tokens = chat.estimate_tokens(block)
        if selected and used + tokens > budget_tokens:
            continue
        selected.append(item)
        used += tokens
        if used >= budget_tokens or len(selected) >= limit:
            break
    return selected


def _write_with_model(
    prompt: str,
    writer_model: str,
    emit: EmitFn | None,
    log: LogFn | None,
) -> str | None:
    try:
        text = chat.stream_answer(prompt, model=writer_model, emit=emit)
    except RuntimeError as exc:
        # Do not reset the answer here: earlier sections are already visible.
        # The caller replaces only this section with a grounded extractive
        # fallback so the rest of the report survives.
        _log(log, f"[research] writer model unavailable: {exc}")
        return None
    return text.strip()


def _strip_leading_heading(text: str) -> str:
    """Drop a heading the writer repeated even though the UI supplies one."""
    lines = text.lstrip().splitlines()
    while lines:
        first = lines[0].strip()
        if not first:
            lines.pop(0)
            continue
        if re.match(r"^#{1,6}\s+", first):
            lines.pop(0)
            continue
        break
    return "\n".join(lines).strip()


def _writer_prompt(topic: str, section: str, evidence: list[Evidence]) -> str:
    evidence_text = "\n\n".join(_evidence_block(item) for item in evidence)
    return (
        f"{WRITER_RULES}\n"
        f"Original question: {topic}\n"
        f"Section to write: {section}\n\n"
        f"Evidence:\n{evidence_text}\n\n"
        f"Write the section now. Keep it focused on the evidence above."
    )


def _extractive_report(
    topic: str,
    plan: dict,
    ledger: list[Evidence],
) -> str:
    """A grounded fallback used when the writer model cannot run."""
    lines = [
        f"# Deep research: {topic}",
        "",
        "## Summary",
        "",
        "The writer model was unavailable, so this report lists the strongest "
        "evidence retrieved by the research loop instead of expanding it into "
        "prose.",
        "",
    ]
    top = _select_evidence(ledger, SUMMARY_EVIDENCE_TOKENS, limit=5)
    for item in top:
        lines.append(f"- {item.excerpt} [{item.evidence_id}]")
    lines.append("")
    for subquestion in plan["subquestions"]:
        subquestion_id = str(subquestion["id"])
        items = [
            item
            for item in ledger
            if subquestion_id in item.subquestions
        ]
        lines.append(f"## {_short_title(subquestion['text'])}")
        lines.append("")
        if not items:
            lines.append("No direct evidence was found in the local corpus.")
        else:
            for item in _select_evidence(items, SECTION_EVIDENCE_TOKENS, limit=5):
                lines.append(f"- {item.excerpt} [{item.evidence_id}]")
        lines.append("")
    return "\n".join(lines).strip() + "\n"


def _citation_ids(text: str) -> list[str]:
    return [
        f"E{int(number):02d}" for number in re.findall(r"\[E(\d+)\]", text)
    ]


def _verify_report(
    report: str, ledger: list[Evidence], log: LogFn | None
) -> dict:
    """Check citation integrity and count uncited factual sentences."""
    known = {item.evidence_id for item in ledger}
    invalid: set[str] = set()

    def replace_invalid(match: re.Match) -> str:
        evidence_id = f"E{int(match.group(1)):02d}"
        if evidence_id in known:
            # Normalize [E1] to [E01] so the source table and the report use
            # one citation spelling.
            return f"[{evidence_id}]"
        invalid.add(evidence_id)
        return ""

    report = re.sub(r"\[E(\d+)\]", replace_invalid, report)
    report = re.sub(r"[ \t]+\n", "\n", report)
    report = re.sub(r" {2,}", " ", report)

    cited = 0
    uncited = 0
    in_sources = False
    for line in report.splitlines():
        stripped = line.strip()
        if stripped.startswith("## Sources") or stripped.startswith(
            "## Research trace"
        ):
            in_sources = stripped.startswith("## Sources")
            continue
        if in_sources or not stripped or stripped.startswith(("#", "-", "*")):
            continue
        for sentence in re.split(r"(?<=[.!?])\s+", stripped):
            sentence = sentence.strip()
            if len(sentence.split()) < 6:
                continue
            if _citation_ids(sentence):
                cited += 1
            else:
                uncited += 1
    _log(
        log,
        f"[research] verification: {cited} cited claim(s), {uncited} uncited "
        f"sentence(s), {len(invalid)} invalid citation(s) removed",
    )
    return {
        "cited_claims": cited,
        "uncited_sentences": uncited,
        "invalid_citations": sorted(invalid),
        "report": report,
    }


def _sources_section(ledger: list[Evidence]) -> str:
    if not ledger:
        return "## Sources\n\nNo usable sources were found in the local corpus."
    lines = ["## Sources", ""]
    for item in ledger:
        location = item.url or item.chunk_key
        lines.append(f"- [{item.evidence_id}] {item.title} — {location}")
    return "\n".join(lines)


def _unknowns_section(plan: dict, ledger: list[Evidence]) -> str:
    counts = {str(item["id"]): 0 for item in plan["subquestions"]}
    for evidence in ledger:
        for subquestion_id in evidence.subquestions:
            if subquestion_id in counts:
                counts[subquestion_id] += 1
    gaps = [
        str(item["text"])
        for item in plan["subquestions"]
        if counts.get(str(item["id"]), 0) == 0
    ]
    lines = ["## What the sources do not establish", ""]
    if gaps:
        lines.append(
            "The local corpus did not provide direct evidence for these "
            "sub-questions:"
        )
        for gap in gaps:
            lines.append(f"- {gap}")
        lines.append("")
    lines.append(
        "This report is limited to the learned articles and video transcripts "
        "in the local corpus. It cannot establish facts that are absent from "
        "those sources, and claims about private, disputed, or speculative "
        "matters remain unverified unless a source explicitly supports them."
    )
    return "\n".join(lines)


def _trace_section(trace: dict) -> str:
    lines = [
        "## Research trace",
        "",
        f"- Intent: {trace.get('intent', 'open_ended')}",
        f"- Sub-questions: {trace.get('subquestion_count', 0)}",
        f"- Retrieval rounds: {trace.get('rounds', 0)}",
        f"- Evidence items: {trace.get('evidence_count', 0)}",
        f"- Unique sources: {trace.get('unique_sources', 0)}",
        f"- Writer model: {trace.get('writer_model', '')}",
        f"- Stop reason: {trace.get('stop_reason', '')}",
        f"- Elapsed: {trace.get('elapsed_seconds', 0):.1f} s",
    ]
    contradictions = trace.get("contradictions") or []
    if contradictions:
        lines.append("- Contradictions noted during retrieval:")
        for contradiction in contradictions:
            lines.append(f"  - {contradiction}")
    return "\n".join(lines)


def _slug(topic: str) -> str:
    slug = re.sub(r"[^a-z0-9]+", "-", topic.lower()).strip("-")[:60]
    return slug or "research"


def _save_research(
    topic: str,
    plan: dict,
    ledger: list[Evidence],
    sections: list[dict],
    report: str,
    trace: dict,
    log: LogFn | None,
) -> str | None:
    stamp = datetime.now().strftime("%Y%m%d-%H%M%S")
    directory = RESEARCH_DIR / f"{stamp}-{_slug(topic)}"
    try:
        directory.mkdir(parents=True, exist_ok=True)
        (directory / "plan.json").write_text(
            json.dumps(plan, indent=2, ensure_ascii=False) + "\n",
            encoding="utf-8",
        )
        with (directory / "evidence.jsonl").open("w", encoding="utf-8") as handle:
            for item in ledger:
                handle.write(json.dumps(item.as_json(), ensure_ascii=False) + "\n")
        with (directory / "sections.jsonl").open("w", encoding="utf-8") as handle:
            for section in sections:
                handle.write(json.dumps(section, ensure_ascii=False) + "\n")
        (directory / "report.md").write_text(report, encoding="utf-8")
        trace["saved_dir"] = str(directory)
        (directory / "trace.json").write_text(
            json.dumps(trace, indent=2, ensure_ascii=False) + "\n",
            encoding="utf-8",
        )
        sources = [
            {
                "evidence_id": item.evidence_id,
                "title": item.title,
                "url": item.url,
                "source_id": item.chunk.video_id,
                "chunk_index": item.chunk.index,
                "quality": item.source_quality,
                "bm25_score": item.score,
            }
            for item in ledger
        ]
        (directory / "sources.json").write_text(
            json.dumps(sources, indent=2, ensure_ascii=False) + "\n",
            encoding="utf-8",
        )
    except OSError as exc:
        _log(log, f"[research] could not save the research trace: {exc}")
        return None
    return str(directory)


def run_deep_research(
    topic: str,
    chunks: list[chat.Chunk],
    *,
    log: LogFn | None = None,
    emit: EmitFn | None = None,
    source_info: SourceInfoFn | None = None,
    writer_model: str = WRITER_MODEL,
) -> tuple[str, list[tuple[chat.Chunk, float]], dict]:
    """Run the bounded research loop and stream the report.

    Returns ``(report, sources, trace)``. ``sources`` is the evidence list in
    citation order so the server can build the usual paragraph annotations.
    """
    started = time.monotonic()
    topic = _clean_text(topic, 500)
    if not topic:
        raise ValueError("the research topic is empty")
    if not chunks:
        raise ValueError("the current corpus contains no trunks")

    plan = _plan_topic(topic, log)
    subquestions = plan["subquestions"]
    _log(
        log,
        f"[research] intent: {plan['intent']}; "
        f"{len(subquestions)} sub-question(s)",
    )
    _log(
        log,
        "[research] plan: "
        + "; ".join(
            f"{item['id']} {_clean_text(item['text'], 90)}"
            for item in subquestions
        ),
    )

    ledger: list[Evidence] = []
    by_key: dict[tuple[str, int], Evidence] = {}
    source_counts: dict[str, int] = {}
    text_tokens: list[set[str]] = []
    rounds = 0

    rounds += 1
    _retrieve_round(
        _queries_for_round_one(plan, topic),
        chunks,
        ledger,
        by_key,
        source_counts,
        text_tokens,
        source_info,
        log,
        rounds,
    )

    gap_queries: list[tuple[str, str]] = []
    contradictions: list[str] = []
    if rounds < MAX_ROUNDS and ledger:
        gap_queries, contradictions = _gap_check(topic, plan, ledger, log)
    if gap_queries:
        rounds += 1
        _retrieve_round(
            gap_queries,
            chunks,
            ledger,
            by_key,
            source_counts,
            text_tokens,
            source_info,
            log,
            rounds,
        )

    stop_reason = (
        "all planned rounds reached"
        if rounds >= MAX_ROUNDS
        else "no remaining gap queries"
    )
    if not ledger:
        stop_reason = "no matching evidence in the local corpus"
    _log(
        log,
        f"[research] stop: {stop_reason}; {len(ledger)} evidence item(s), "
        f"{len({item.chunk.video_id for item in ledger})} unique source(s)",
    )

    # ---- section-wise synthesis -------------------------------------
    report_parts: list[str] = [f"# Deep research: {topic}"]
    sections: list[dict] = []
    _emit(emit, report_parts[0] + "\n\n")

    summary_evidence = _select_evidence(
        ledger, SUMMARY_EVIDENCE_TOKENS, limit=8
    )
    if summary_evidence:
        _log(log, "[research] writing section 1: summary")
        _emit(emit, "## Summary\n\n")
        summary = _write_with_model(
            _writer_prompt(
                topic,
                "Write an executive summary of the whole research report. "
                "State the answer first, then the most important supporting "
                "facts, then the main uncertainty.",
                summary_evidence,
            ),
            writer_model,
            emit,
            log,
        )
        if summary:
            summary = _strip_leading_heading(summary)
            report_parts.append(f"## Summary\n\n{summary}")
            sections.append(
                {
                    "heading": "Summary",
                    "evidence_ids": [
                        item.evidence_id for item in summary_evidence
                    ],
                    "text": summary,
                }
            )

    total_sections = len(subquestions) + (1 if summary_evidence else 0)
    for index, subquestion in enumerate(subquestions, start=1):
        subquestion_id = str(subquestion["id"])
        items = [
            item
            for item in ledger
            if subquestion_id in item.subquestions
        ]
        heading = _short_title(subquestion["text"])
        _log(
            log,
            f"[research] writing section {index + (1 if summary_evidence else 0)}"
            f"/{total_sections}: {heading}",
        )
        _emit(emit, f"## {heading}\n\n")
        if not items:
            text = (
                "The local corpus did not contain direct evidence for this "
                "sub-question, so it remains unresolved."
            )
            _emit(emit, text + "\n\n")
            report_parts.append(f"## {heading}\n\n{text}")
            sections.append(
                {"heading": heading, "evidence_ids": [], "text": text}
            )
            continue
        selected = _select_evidence(
            items, SECTION_EVIDENCE_TOKENS, limit=8
        )
        section_text = _write_with_model(
            _writer_prompt(topic, subquestion["text"], selected),
            writer_model,
            emit,
            log,
        )
        if section_text is None:
            # The writer failed after the heading was already emitted. Build a
            # deterministic section from the same evidence so the report still
            # contains a grounded result.
            section_text = "\n".join(
                f"- {item.excerpt} [{item.evidence_id}]"
                for item in selected
            )
            _emit(emit, section_text + "\n\n")
        else:
            section_text = _strip_leading_heading(section_text)
        report_parts.append(f"## {heading}\n\n{section_text}")
        sections.append(
            {
                "heading": heading,
                "evidence_ids": [item.evidence_id for item in selected],
                "text": section_text,
            }
        )

    unknown_text = _unknowns_section(plan, ledger)
    _emit(emit, unknown_text + "\n\n")
    report_parts.append(unknown_text)
    sections.append(
        {
            "heading": "What the sources do not establish",
            "evidence_ids": [],
            "text": unknown_text,
        }
    )

    trace: dict = {
        "topic": topic,
        "intent": plan["intent"],
        "subquestion_count": len(subquestions),
        "subquestions": subquestions,
        "rounds": rounds,
        "evidence_count": len(ledger),
        "unique_sources": len({item.chunk.video_id for item in ledger}),
        "writer_model": writer_model,
        "stop_reason": stop_reason,
        "contradictions": contradictions,
        "elapsed_seconds": 0.0,
        "saved_dir": None,
    }
    sources_text = _sources_section(ledger)
    _emit(emit, sources_text + "\n\n")
    report_parts.append(sources_text)

    trace["elapsed_seconds"] = time.monotonic() - started
    trace_text = _trace_section(trace)
    _emit(emit, trace_text + "\n")
    report_parts.append(trace_text)
    report = "\n\n".join(report_parts).strip() + "\n"
    verification = _verify_report(report, ledger, log)
    report = verification["report"]
    trace["verification"] = {
        key: value
        for key, value in verification.items()
        if key != "report"
    }

    # Save after all model calls so an interrupted run still leaves the ledger
    # and sections in the trace directory. The ledger itself is already on
    # disk only at this point; failures during synthesis still preserve the
    # retrieved evidence in the returned tuple and in the chat logs.
    saved_dir = _save_research(
        topic, plan, ledger, sections, report, trace, log
    )
    if saved_dir:
        trace["saved_dir"] = saved_dir
        _log(log, f"[research] trace saved to {saved_dir}")

    # If every writer call failed, replace the partially written report with a
    # deterministic extractive one. This is the last-resort path that still
    # gives the user a grounded result from the corpus.
    if not any(section["text"] for section in sections if section["evidence_ids"]):
        extractive = _extractive_report(topic, plan, ledger)
        if report != extractive:
            _emit(emit, extractive)
            report = extractive
            saved_dir = _save_research(
                topic, plan, ledger, sections, report, trace, log
            )

    sources = sorted(
        ledger,
        key=lambda item: (
            _first_citation_index(report, item.evidence_id),
            -item.score,
        ),
    )
    _log(
        log,
        f"[research] report ready: {len(ledger)} source(s), "
        f"{len(report.split())} word(s), "
        f"{time.monotonic() - started:.1f} s",
    )
    return report, [(item.chunk, item.score) for item in sources], trace


def _first_citation_index(report: str, evidence_id: str) -> int:
    match = re.search(rf"\[{re.escape(evidence_id)}\]", report)
    return match.start() if match else 10**9
