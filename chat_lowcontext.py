#!/usr/bin/env python3
"""Interactive low-context, streaming RAG chatbot over YouTube transcripts.

Usage:
    python3 chat_lowcontext.py [--tag <tag>]

This is a drop-in clone of chat.py: it supports the same CLI flag, commands,
history file, vi key bindings, tag/transcript scoping, /youtube learn,
/wiki, /dict,
/depth, /status, /sources, and Simplified Chinese question/answer translation.

The difference is the answer architecture. Instead of packing every matching
trunk into one large prompt, chat_lowcontext.py implements the low-context
iterative streaming pipeline described in
research/low_context_streaming_rag_architecture.md:

    1. Retrieve the top matching trunks as chat.py does (default depth 100).
       If the question names a specific topic/entity, first restrict the
       candidate pool to chunks that mention it (query-term anchoring). The
       anchor term is chosen by retrieval relevance rather than raw rarity, so
       a generic attribute ("levels" in "what are all levels of wagyu") cannot
       take over from the real topic; a related-but-different entity (for
       example another market) also cannot fill the answer.
    2. Process them in small micro-batches (default 5 chunks per call).
    3. For each batch, ask the model for two things:
           NEW_ENTITIES: <new items that answer the query>
           CONTENT: <a seamless continuation, or SKIP>
       Only CONTENT is streamed to the user. A rolling entity ledger and the
       last couple of sentences from the previous batch are passed forward so
       the answer stays coherent without carrying the full history.
    4. Batches that only repeat covered information return SKIP and are
       suppressed instead of streaming filler.
    5. For aggregation/count/list questions, first narrow the chunks to the
       requested category when the question names one (for example "levels"
       in "what are all levels of wagyu"), then feed the collected entities
       into a final stateful summary that deduplicates aliases and states the
       final count.
    6. For an entity-focused question, chunks that only compare a different
       entity of the same kind (for example another market) are cleaned or
       dropped, and only chunks mentioning the asked attribute (such as an
       opening time) are kept. Factual questions stop after the first batch
       that produces prose, so the answer stays direct instead of repeating
       the fact or adding unasked details.
    7. Known-wrong statements in the stored transcripts are corrected in
       memory before generation (see SOURCE_FACT_CORRECTIONS), so the model
       cannot repeat a bad stored fact verbatim.

The prompt uses vi key bindings (press Esc for normal mode):
    i / a / A          Insert mode before / after / at end of line
    h / l / 0 / $      Move left / right / to start / to end
    w / b              Move by word
    v / V              Visual / visual-line mode, then d or x to delete
    dd / dw            Delete a line / word
    u                  Undo
    Tab                Complete commands (repeat to cycle matches)
    Up / Down          Previous / next saved question

Questions and slash commands are persisted to ./.chat/question_history.txt
and reloaded on every start, so Up/Down can walk the full chat history
(questions and commands).

Commands:
    /memory            Show memory used by the loaded embeddings.
    /tag [tag]         Switch to another tag (reloads embeddings).
    /refresh           Reload all embeddings for the current tag.
    /transcript <hash|keyword>
                       Use one video's transcript (reloads embeddings).
                       A keyword searches the .title files with ag and shows
                       a hinting list of <hash> <title> matches to pick from.
    /search_transcript <keyword>
                       Alias for /transcript <keyword>.
    /youtube learn <hash|url>
                       Fetch, rewrite, chunk, embed, and tag a video.
    /wiki <word>       Look up a Wikipedia summary for <word>.
    /dict <word>       Show English dictionary definitions for <word> from a
                       local Ollama model, plus an explanation of its use and
                       meaning in the latest answer (when the word appears
                       there).
    /depth <n|auto>    Set how many top matching trunks are used as context
                       (default: 100). Use "auto" to restore the
                       similarity-threshold rule.
    /status            Show the current chat status (tag, scope, loaded
                       chunks, retrieval depth, models, and history size).
    /sources           Show the source list for the latest answer.
    /exit, /quit, /q   Leave the chatbot.

Environment overrides:
    LOW_CONTEXT_BATCH_SIZE  chunks per micro-batch (default: 5)
    LOW_CONTEXT_NUM_CTX     Ollama context window per call (default: 8192)
"""

import gc
import json
import math
import os
import re
import sys
from dataclasses import dataclass
from typing import Callable, Iterator

import requests

import chat as base


def _env_int(name: str, default: int, minimum: int = 1) -> int:
    try:
        return max(minimum, int(os.environ.get(name, default)))
    except (TypeError, ValueError):
        return default


# Small batches keep each LLM call comfortably inside the local model's
# context window and cut the number of transitions (and latency) versus one
# call per chunk. See research/low_context_streaming_rag_architecture.md §6.
BATCH_SIZE = _env_int("LOW_CONTEXT_BATCH_SIZE", 5)
LOW_CONTEXT_NUM_CTX = _env_int("LOW_CONTEXT_NUM_CTX", 8192)

# Trunks are small in practice; this is only a safety valve for one huge chunk.
MAX_CHUNK_CHARS = 6000

# A transcript can itself state a fact incorrectly. Because the embeddings were
# built from the stored trunks, such a paragraph keeps ranking highly for the
# relevant question and the model would otherwise repeat the error verbatim.
# These in-memory corrections patch the paragraph after it is read and before
# it is used for anchoring or generation, so every code path (micro-batches,
# SKIP gating, and aggregation) sees the corrected fact. The corpus on disk is
# left untouched. Each entry is an explicit (bad claim, correct claim) pair:
# fuzzy matching is intentionally avoided so healthy sources are never altered.
SOURCE_FACT_CORRECTIONS: tuple[tuple[re.Pattern[str], str], ...] = (
    (
        re.compile(
            r"\bA 90-day long-stay visa requires approximately 30 million yen"
            r" \(around \$200,000 USD\)",
            re.IGNORECASE,
        ),
        "A 90-day long-stay visa has a visa fee of approximately "
        "300,000 yen (around $2,000 USD)",
    ),
)
# How much of the previous section to use as the bridge into the next batch.
TAIL_CONTEXT_CHARS = 240
# How many covered entities to show in each batch prompt. The backend keeps
# them all for the final aggregation; the prompt only needs a compact ledger.
LEDGER_ITEMS = 50

NEW_ENTITIES_HEADER = re.compile(
    r"(?im)^[ \t>*#\-]*NEW_ENTITIES[ \t]*[*_]*[ \t]*:[*_]*[ \t]*"
)
CONTENT_HEADER = re.compile(
    r"(?im)^[ \t>*#\-]*CONTENT[ \t]*[*_]*[ \t]*:[*_]*[ \t]*"
)
SKIP_ONLY = re.compile(
    r"[*_]*\[?\s*SKIP\s*\]?[.!]?[*_]*", re.IGNORECASE
)
SKIP_LINE = re.compile(
    r"^\s*[*_]*\[?\s*SKIP\s*\]?[.!]?[*_]*\s*(?:\r?\n|$)",
    re.IGNORECASE,
)

# Queries that need a global synthesis (count/list/aggregation) get the final
# stateful summary described in §3 of the research document. Descriptive
# queries rely only on the rolling-window streamer.
AGGREGATION_QUERY = re.compile(
    r"\b(?:how many|how much|number of|count(?: the)?|enumerate|list|"
    r"names? of|give me (?:a )?list|show me (?:a )?list|"
    r"what are (?:the|all)|all (?:the|of the|of|my|these|those))\b",
    re.IGNORECASE,
)

# Questions that ask for one concrete fact. These get a concise answer instead
# of the paragraph-by-paragraph narrative used for broad descriptive questions.
FACTUAL_QUERY = re.compile(
    r"(?:\bwhat time\b|\bwhat hours\b|\bopening (?:hours|times?)\b|"
    r"\bwhen\b|\bwhere\b|\bhow (?:far|long|much|many|old|expensive)\b|"
    r"\bwhat (?:is|are) the (?:address|location|price|cost|fee|hours|opening)\b|"
    r"\bwho is\b)",
    re.IGNORECASE,
)

# Query-term anchoring. Rare content words in a question are usually the
# named entity or distinguishing attribute the user cares about ("nijo" in
# "where is nijo market", "airports" in "show me a list of airports"). If such
# a term occurs in at least a few chunks, restrict retrieval to those chunks.
# This prevents a semantically similar but different entity (Toyosu/Tsukiji
# for Nijo Market) from filling the context just because it is also a market.
TOKEN_PATTERN = re.compile(r"[a-z0-9]+")
QUESTION_STOPWORDS = frozenset(
    """
    a an the is are was were be been being am do does did doing have has had
    of in on at to for from by with about against between into through during
    before after above below up down out off over under again further then once
    here there when where why how what which who whom whose this that these
    those i me my mine we our ours you your yours he him his she her hers it its
    they them their theirs and or but if because as until while so than too very
    can could should would will shall may might must please
    """.split()
)
# Common query modifiers, light verbs, and request/format vocabulary are poor
# entity anchors: they describe how the user asks ("give me a recommendation in
# 600 words") rather than what the user asks about. They still count as normal
# semantic query terms; they are only excluded from anchoring. This prevents a
# rare framing word from hijacking the anchor filter and pulling in unrelated
# chunks (for example, a gas-station paragraph that happens to use "words").
ANCHOR_IGNORE_TERMS = frozenset(
    """
    best better worst good great top popular famous recommended recommend
    recommendation recommendations suggest suggestion suggestions advice advise
    show tell give list name names kind kinds type types sort sorts
    thing things place places area areas part parts way ways time times
    visit visits see seen look looking find get got go going come came make
    made take took use used using eat eating buy buying read know learn
    want wants wanted wanting need needs needed needing like likes liked
    prefer prefers preferred preferring wish wishlist interested
    open opened opening opens close closed closing closes hour hours
    price prices cost costs fee fees entrance location address available
    small big large little long short new old cheap cheaper expensive
    affordable free many much more most less least few several different
    similar main primary important interesting beautiful nice local
    traditional typical common usual famous
    word words paragraph paragraphs sentence sentences page pages essay
    essays article articles answer answers response responses reply replies
    explain explains explained explanation explanations describe described
    description descriptions summarize summarized summary summaries overview
    detail details information info example examples reason reasons benefit
    benefits tip tips guide guides option options choice choices idea ideas
    comparison comparisons compare compared review reviews pros cons
    all any each every both some other another none entire whole multiple
    level levels grade grades rank ranks category categories variety varieties
    """.split()
)


def _is_anchor_token(token: str) -> bool:
    """Return True when a token can identify the query's topic/entity.

    Pure numbers ("600" in "600 words") and request/format vocabulary are not
    topics, even when they happen to be rare in the transcript corpus.
    """
    return (
        len(token) >= 3
        and not token.isdigit()
        and token not in QUESTION_STOPWORDS
        and token not in ANCHOR_IGNORE_TERMS
    )


# A term that appears in this share of the corpus is too broad to focus
# retrieval. Such a term can still be the real topic ("japan" in a Japan-only
# scope), but anchoring on it would not narrow anything.
ANCHOR_MAX_DF_RATIO = 0.10
# Which term is the topic cannot be decided by raw rarity alone: in "what are
# all levels of wagyu", the generic attribute "levels" is rarer than the real
# topic "wagyu". Instead, score each candidate term by how similar its
# best-matching chunks are to the question, and keep every term that is nearly
# as relevant as the best one (cosine-similarity margin).
ANCHOR_SIMILAR_SCORE_MARGIN = 0.05
ANCHOR_SCORE_TOP_K = 5
MIN_ANCHOR_CHUNKS = 3

# Category/attribute nouns that commonly end a list-style question ("what are
# all levels of wagyu", "what are the types of sushi"). When an aggregation
# question names one of these, prefer chunks that actually discuss that
# category so the answer does not drift into adjacent facts (cuts, brands,
# side dishes, ...) about the same topic.
CATEGORY_TERMS = frozenset(
    """
    level levels type types kind kinds grade grades rank ranks category
    categories variety varieties sort sorts class classes tier tiers
    stage stages step steps section sections
    """.split()
)

BATCH_PROMPT = """You are incrementally answering the user's query: "{query}"

Answer style:
{style}

Information already covered (do NOT repeat these items or facts): {covered}
The previous section ended with: "{tail}"

Focus rule:
The query may name a specific entity (a place, person, product, event, or
organization). Treat that entity as a hard filter: use only context that
explicitly discusses the queried entity. Never substitute a different entity
from the same category (for example, another market, city, person, or product),
even when the two are related. Keep the answer narrowly focused on the exact
question: include only details that directly answer it. Omit background,
comparisons, opening hours, prices, and facts about other entities unless the
question explicitly asks for them. If the New Context is about a different
entity or does not help answer the exact question asked, set CONTENT to SKIP.

Task:
1. Read the New Context below.
2. Identify only NEW items/entities that help answer the query. List only items
   of the category the query asks for (for example, for "what are all levels
   of X", list levels only, not cuts, brands, dishes, or other facts about X).
3. If the New Context contains new and relevant information, continue the
   answer in the requested style. Do NOT write an introduction or conclusion.
   Do NOT repeat information already covered.
4. If the New Context only repeats covered information or is irrelevant, set
   CONTENT to SKIP.

Reply using exactly this format and nothing else:
NEW_ENTITIES: comma-separated list of new items (or NONE)
CONTENT: continuing prose (or SKIP)

New Context:
{batch}"""

FINAL_SUMMARY_PROMPT = """You are finishing an incremental answer to the user's query: "{query}"

Across the retrieved documents, the following raw item/entity mentions were
collected. They were deduplicated only by spelling, so aliases may remain:
{items}

Write the concluding section of the answer:
1. Merge obvious aliases that refer to the same item. Do not invent items that
   are not in the list. Include only items relevant to the exact query; never
   substitute a different entity from the same category. Include only items of
   the category the query asks for (for example, for "what are all levels of
   X", list levels only, not cuts, brands, dishes, or other facts about X).
2. If the query asks for a count, state the exact final count after merging
   aliases.
3. If the query asks for a list, give the complete deduplicated list.
4. Add one or two sentences summarizing the key findings.
Do not repeat the running answer. Output only the conclusion, starting with a
short Markdown heading."""


@dataclass
class BatchResult:
    """The parsed output of one micro-batch LLM call."""

    content: str
    entities: list[str]
    skipped: bool


def iter_tokens(prompt: str, model: str = base.CHAT_MODEL) -> Iterator[str]:
    """Yield tokens from Ollama's streaming generate endpoint."""
    payload = {
        "model": model,
        "prompt": prompt,
        "stream": True,
        "think": False,
        "options": {
            "temperature": 0.2,
            "num_ctx": LOW_CONTEXT_NUM_CTX,
        },
    }
    try:
        with requests.post(
            f"{base.OLLAMA_HOST}/api/generate",
            json=payload,
            stream=True,
            timeout=(10, 1800),
        ) as response:
            response.raise_for_status()
            for line in response.iter_lines(decode_unicode=True):
                if not line:
                    continue
                try:
                    event = json.loads(line)
                except json.JSONDecodeError:
                    continue
                if event.get("error"):
                    raise RuntimeError(f"Ollama error: {event['error']}")
                token = event.get("response", "")
                if token:
                    yield token
                if event.get("done"):
                    break
    except requests.RequestException as exc:
        raise RuntimeError(
            f"Could not reach Ollama at {base.OLLAMA_HOST}. "
            f"Is it running? ({exc})"
        ) from exc


def parse_entities(region: str) -> list[str]:
    """Parse the NEW_ENTITIES payload into unique item names."""
    if not region.strip():
        return []

    text = re.sub(r"(?i)\bNONE\b", " ", region)
    text = re.sub(r"```.*?```", " ", text, flags=re.DOTALL)
    text = text.replace("[", " ").replace("]", " ")

    entities: list[str] = []
    seen: set[str] = set()
    for chunk in re.split(r"[,;\n]+", text):
        item = re.sub(r"^\s*(?:[-*•]|\d+[.)])\s*", "", chunk)
        item = item.strip().strip("*_`\"'“”").strip().strip(" .")
        if not item:
            continue
        lowered = item.lower()
        if lowered in {
            "none",
            "no new entities",
            "no new items",
            "n/a",
            "na",
            "skip",
            "not applicable",
            "-",
        }:
            continue
        if len(item) > 160:
            continue
        key = re.sub(r"\s+", " ", lowered)
        if key in seen:
            continue
        seen.add(key)
        entities.append(item)
    return entities


def _entities_after_header(region: str) -> str:
    """Return the text following a NEW_ENTITIES: header, if present."""
    match = NEW_ENTITIES_HEADER.search(region)
    return region[match.end() :] if match else ""


def run_batch_stream(prompt: str, on_content: Callable[[str], None]) -> BatchResult:
    """Stream one structured batch and forward only its CONTENT to the user.

    The model is asked for a ``NEW_ENTITIES`` line followed by a ``CONTENT``
    line. Because tokens arrive in arbitrary pieces, the parser buffers just
    enough header text to find the CONTENT marker, then streams prose. It
    holds the first few characters of prose so a bare ``SKIP`` is suppressed
    rather than briefly shown to the user.
    """
    header_buf = ""
    in_content = False
    content_parts: list[str] = []
    pending = ""
    suppressed = False
    first_content = True
    entities_region = ""

    def emit(piece: str) -> None:
        if not piece:
            return
        content_parts.append(piece)
        on_content(piece)

    def flush_pending() -> None:
        nonlocal pending, suppressed, first_content
        if not pending:
            return
        if first_content:
            pending = pending.lstrip()
            if not pending:
                return
            first_content = False
        stripped = pending.strip()
        if not stripped:
            pending = ""
            return
        if SKIP_ONLY.fullmatch(stripped):
            suppressed = True
            pending = ""
            return
        # A model may emit "SKIP" on its own line and then real content.
        skip_line = SKIP_LINE.match(stripped)
        if skip_line:
            rest = stripped[skip_line.end() :].strip()
            if not rest:
                suppressed = True
                pending = ""
                return
            pending = rest
        emit(pending)
        pending = ""

    def process_content(piece: str) -> None:
        nonlocal pending
        if suppressed:
            return
        pending += piece
        if not pending:
            return
        stripped = pending.strip()
        if not stripped:
            return
        # Hold short fragments so a SKIP token is never streamed. Newlines
        # and enough characters let us decide safely.
        if len(stripped) < 12 and "\n" not in stripped:
            return
        flush_pending()

    def start_content(remainder: str) -> None:
        nonlocal in_content
        in_content = True
        remainder = re.sub(r"^[ \t\r\n]+", "", remainder)
        process_content(remainder)

    for token in iter_tokens(prompt):
        if not in_content:
            header_buf += token
            content_match = CONTENT_HEADER.search(header_buf)
            if content_match:
                entities_region += _entities_after_header(
                    header_buf[: content_match.start()]
                )
                remainder = header_buf[content_match.end() :]
                header_buf = ""
                start_content(remainder)
                continue

            # Some small models ignore the format. After a reasonable wait,
            # treat the buffered text as prose rather than stalling forever.
            if len(header_buf) > 1200:
                entities_match = NEW_ENTITIES_HEADER.search(header_buf)
                if entities_match:
                    line_end = header_buf.find("\n", entities_match.end())
                    if line_end == -1:
                        entities_region += header_buf[entities_match.end() :]
                        header_buf = ""
                        in_content = True
                    else:
                        entities_region += header_buf[
                            entities_match.end() : line_end
                        ]
                        remainder = header_buf[line_end + 1 :]
                        header_buf = ""
                        start_content(remainder)
                else:
                    remainder = header_buf
                    header_buf = ""
                    start_content(remainder)
            continue

        process_content(token)

    # The stream ended while still in the header state: handle a missing
    # CONTENT marker and any final prose.
    if not in_content and header_buf:
        content_match = CONTENT_HEADER.search(header_buf)
        if content_match:
            entities_region += _entities_after_header(
                header_buf[: content_match.start()]
            )
            remainder = header_buf[content_match.end() :]
            start_content(remainder)
        else:
            entities_match = NEW_ENTITIES_HEADER.search(header_buf)
            if entities_match:
                line_end = header_buf.find("\n", entities_match.end())
                if line_end == -1:
                    entities_region += header_buf[entities_match.end() :]
                else:
                    entities_region += header_buf[
                        entities_match.end() : line_end
                    ]
                    start_content(header_buf[line_end + 1 :])
            else:
                start_content(header_buf)

    flush_pending()
    content = "".join(content_parts).strip()
    return BatchResult(
        content=content,
        entities=parse_entities(entities_region),
        skipped=suppressed or not content,
    )


def run_plain_stream(prompt: str, on_content: Callable[[str], None]) -> None:
    """Stream an unstructured prompt (used for the final aggregation)."""
    for token in iter_tokens(prompt):
        on_content(token)


class AnswerEmitter:
    """Print model output, optionally translating it as it streams.

    English text is collected for the latest answer (used by /dict), no matter
    whether the user is shown the English stream or its Chinese translation.
    Translation reuses chat.py's sentence-group splitter so the user sees
    Chinese while the answer is still being generated.
    """

    def __init__(self, translate: bool) -> None:
        self.translate = translate
        self.buffer = ""
        self.translation_started = False
        self.english_parts: list[str] = []
        self.last_char = ""

    def feed(self, text: str) -> None:
        if not text:
            return
        self.english_parts.append(text)
        if not self.translate:
            print(text, end="", flush=True)
            self.last_char = text[-1]
            return

        self.buffer += text
        segment, self.buffer = base.pop_translation_segment(self.buffer)
        if segment:
            self._emit_translation(segment)

    def _emit_translation(self, segment: str) -> None:
        if not segment.strip():
            return
        if not self.translation_started:
            print("[translated to Simplified Chinese]\n", flush=True)
            self.translation_started = True
        try:
            translated = base.translate_to_chinese(segment)
        except RuntimeError as exc:
            print(
                f"\nWarning: could not translate part of the answer: {exc}",
                file=sys.stderr,
            )
            translated = segment.strip()

        # Small models often drop list markers ("1.", "- ") while translating;
        # put them back so numbered and bulleted answers keep their structure.
        prefix_match = base.LIST_PREFIX.match(segment)
        if prefix_match and not base.LIST_PREFIX.match(translated):
            translated = f"{prefix_match.group(1)} {translated}"
        print(translated, end="", flush=True)
        trailing_newlines = re.search(r"(\n+)[ \t]*$", segment)
        if trailing_newlines:
            print("\n" * len(trailing_newlines.group(1)), end="", flush=True)
            self.last_char = "\n"
        else:
            self.last_char = translated[-1] if translated else ""

    def flush(self) -> None:
        if self.translate and self.buffer.strip():
            segment, self.buffer = self.buffer, ""
            self._emit_translation(segment)
        self.buffer = ""

    def section_break(self) -> None:
        """Finish a batch's prose and visually separate the next one.

        Sections are joined with a blank line so the streamed output reads
        as one integrated article instead of a series of batch fragments.
        """
        self.flush()
        # Close the current line if needed, then leave one empty line before
        # the next section.
        if self.last_char != "\n":
            print(flush=True)
        print(flush=True)
        self.last_char = "\n"
        self.english_parts.append("\n\n")

    def finish(self) -> str:
        self.flush()
        if self.last_char != "\n":
            print(flush=True)
        text = "".join(self.english_parts).strip()
        return re.sub(r"\n{3,}", "\n\n", text)


def add_entities(entity_map: dict[str, str], entities: list[str]) -> int:
    """Add newly discovered entities to the accumulator, case-insensitively."""
    added = 0
    for entity in entities:
        key = re.sub(r"\s+", " ", entity).strip().lower()
        if key and key not in entity_map:
            entity_map[key] = entity.strip()
            added += 1
    return added


def covered_ledger(entity_map: dict[str, str]) -> str:
    """Return a compact ledger of already-covered entities for the prompt."""
    items = list(entity_map.values())
    if not items:
        return "NONE"
    hidden = max(0, len(items) - LEDGER_ITEMS)
    ledger = ", ".join(items[-LEDGER_ITEMS:])
    if hidden:
        ledger += f" (plus {hidden} earlier item(s))"
    return ledger


def tail_context(text: str) -> str:
    """Keep the last ~2 sentences of a section as the next batch's bridge."""
    text = " ".join(text.split())
    sentences = re.split(r"(?<=[.!?])\s+", text)
    if len(sentences) > 2:
        text = " ".join(sentences[-2:])
    return text[-TAIL_CONTEXT_CHARS:]


def needs_aggregation(question: str) -> bool:
    """Route count/list/aggregation queries to the final summary phase."""
    return bool(AGGREGATION_QUERY.search(question))


def answer_style(question: str) -> str:
    """Return the writing style for this question's micro-batch prompts."""
    if needs_aggregation(question):
        return (
            "The user asked for a list/count. Include every relevant item and "
            "keep each item to a short phrase or sentence."
        )
    if FACTUAL_QUERY.search(question):
        return (
            "The user asked for one specific fact. Answer in at most 3 short "
            "sentences. State only the requested fact and, if necessary, one "
            "directly supporting detail. Do not add background, atmosphere, "
            "menus, prices, history, or other entities."
        )
    return (
        "Write 1-2 short, cohesive paragraphs about the queried entity only. "
        "Do not add unrelated background or other entities."
    )


def _attribute_terms(question: str, phrase: str) -> list[str]:
    """Return the content words that describe the requested attribute.

    For "what time does nijo market open" and phrase "nijo market" this is
    ``['open', 'time']``. Those words let us keep only chunks that actually
    discuss opening times, instead of every chunk that mentions the market.
    """
    entity_words = set(phrase.split())
    return sorted(_content_terms(question) - entity_words)


def _attribute_patterns(terms: list[str]) -> list[re.Pattern[str]]:
    """Build prefix patterns so "open" also matches opens/opening/opened."""
    patterns: list[re.Pattern[str]] = []
    for term in terms:
        stem = term
        if term.endswith("ies") and len(term) > 4:
            stem = term[:-3] + "y"
        else:
            for suffix in ("ing", "ed", "es", "s"):
                if len(term) > len(suffix) + 3 and term.endswith(suffix):
                    stem = term[: -len(suffix)]
                    break
        patterns.append(re.compile(r"\b" + re.escape(stem), re.IGNORECASE))
    return patterns


def _category_terms(question: str) -> list[str]:
    """Return category/attribute nouns requested by a list-style question."""
    return sorted(_content_terms(question) & CATEGORY_TERMS)


def _focus_by_attribute(
    usable: list[tuple[base.Chunk, float, str]],
    question: str,
    phrase: str | None,
    terms: list[str] | None = None,
) -> list[tuple[base.Chunk, float, str]]:
    """Keep chunks that discuss the attribute the question actually asks about.

    Anchoring alone still returns every chunk about Nijo Market, including
    restaurants, prices, and shopping lanes, for a question that only asks
    when it opens. If at least one chunk mentions an asked-about attribute
    (opening/time/price/...), use only those chunks. Fall back to all chunks
    when nothing matches, so synonym-only answers are not lost.

    ``terms`` can be supplied directly when the caller already knows the
    requested category (for example "levels" for an aggregation question).
    """
    if terms is None:
        if phrase is None:
            return usable
        terms = _attribute_terms(question, phrase)
    if not terms:
        return usable
    patterns = _attribute_patterns(terms)
    focused = [
        item
        for item in usable
        if any(pattern.search(item[2]) for pattern in patterns)
    ]
    return focused or usable


def apply_fact_corrections(text: str) -> str:
    """Replace known-wrong statements in one retrieved paragraph.

    Corrections are applied at read time (not at write time) so the learned
    corpus remains unchanged and a corrected fact immediately affects every
    consumer of the paragraph: query-term anchoring, micro-batch prompts, and
    the final aggregation summary.
    """
    for pattern, replacement in SOURCE_FACT_CORRECTIONS:
        text = pattern.sub(replacement, text)
    return text


def _chunk_text(chunk: base.Chunk) -> str:
    """Return the paragraph body used as one low-context chunk."""
    text = apply_fact_corrections(base.paragraph_text(chunk))
    return text[:MAX_CHUNK_CHARS]


def _content_terms(text: str) -> set[str]:
    """Return exact normalized content terms from a question or paragraph.

    Anchoring deliberately does not stem words: a plural or inflected form can
    be much rarer than its base form ("airports" vs. "airport"), and the rare
    form is the useful signal for restricting retrieval.
    """
    return {
        token
        for token in TOKEN_PATTERN.findall(text.lower())
        if token not in QUESTION_STOPWORDS and len(token) >= 3
    }


def _query_phrase_candidates(question: str) -> list[str]:
    """Return contiguous multi-word content phrases from the question.

    Runs are built from adjacent non-stopword, non-modifier words, and every
    sub-phrase of length 2+ is returned. In "what time does nijo market open"
    this yields "nijo market" (a specific entity) as well as the useless
    "market open" sub-phrase; anchor filtering later keeps only the former.
    """
    runs: list[list[str]] = []
    current: list[str] = []
    last_end: int | None = None
    lowered = question.lower()
    for match in TOKEN_PATTERN.finditer(lowered):
        token = match.group()
        gap = lowered[last_end : match.start()] if last_end is not None else ""
        if last_end is not None and not re.fullmatch(r"[\s'\-]*", gap):
            if len(current) >= 2:
                runs.append(current)
            current = []
        if not _is_anchor_token(token):
            if len(current) >= 2:
                runs.append(current)
            current = []
        else:
            current.append(token)
        last_end = match.end()
    if len(current) >= 2:
        runs.append(current)

    phrases: list[str] = []
    seen: set[str] = set()
    for run in runs:
        for start in range(len(run)):
            for end in range(start + 2, len(run) + 1):
                phrase = " ".join(run[start:end])
                if phrase not in seen:
                    seen.add(phrase)
                    phrases.append(phrase)
    return phrases


def _phrase_pattern(phrase: str) -> re.Pattern[str]:
    """Compile a whole-word, whitespace-tolerant phrase pattern."""
    return re.compile(
        r"\b" + r"\s+".join(map(re.escape, phrase.split())) + r"\b"
    )


# Sentence-initial words and determiners are not part of a proper name.
ENTITY_LEADING_WORDS = frozenset(
    """
    the a an this that these those both each all any some many most other
    another at in on for with from by and but if when while its their our his
    her my your no not one two three four five
    """.split()
)


def _named_entity_mentions(text: str, head: str) -> set[str]:
    """Return lowercased proper-name phrases ending in ``head``.

    For head "market" and a paragraph mentioning "Sapporo Morning Market",
    this returns ``{"sapporo morning market"}`` while ignoring generic phrases
    such as "the market" or "each market".
    """
    pattern = re.compile(
        r"\b((?:[A-Z][A-Za-z'\u2019.\-]*\s+){0,4})(?i:"
        + re.escape(head)
        + r")\b"
    )
    mentions: set[str] = set()
    for match in pattern.finditer(text):
        words = (match.group(1) + head).split()
        while words and words[0].lower() in ENTITY_LEADING_WORDS:
            words.pop(0)
        if len(words) >= 2:
            mentions.add(" ".join(words).lower())
    return mentions


def _competing_entity_mentions(text: str, phrase: str) -> set[str]:
    """Return other named entities of the same kind as ``phrase``."""
    words = phrase.split()
    distinctive = set(words[:-1])
    if not distinctive:
        return set()
    return {
        mention
        for mention in _named_entity_mentions(text, words[-1])
        if mention != phrase
        and not distinctive.intersection(mention.split())
    }


# Split off trailing comparison clauses ("..., creating a different
# experience from X") and list introductions ("... markets: X and Y"), but
# do not split ordinary comma-separated lists.
_CLAUSE_BOUNDARY = re.compile(
    r"(?<=[;:])\s+|,\s+(?=(?:which|who|whom|whose|where|when|while|"
    r"whereas|compared|unlike|but|yet|creating|making|offering|providing|"
    r"featuring|serving|giving|allowing|including|such as)\b)",
    re.IGNORECASE,
)
_SENTENCE_BOUNDARY = re.compile(r"(?<=[.!?])\s+")
_SENTENCE_END = re.compile(r"[.!?][\"')\]]*$")


def _sanitize_competing_entities(text: str, phrase: str) -> str:
    """Remove clauses/sentences about a different entity of the same kind.

    Retrieval for "what time does nijo market open" can include a paragraph
    that compares Nijo Market with Sapporo Morning Market. The comparison is
    not requested and lets a small model answer about the wrong market. This
    keeps the parts of the paragraph about the queried entity (including the
    direct answer) and drops the comparison instead of discarding the whole
    chunk, so location and opening-time details survive.
    """
    if not _competing_entity_mentions(text, phrase):
        return text

    kept_sentences: list[str] = []
    for sentence in _SENTENCE_BOUNDARY.split(text):
        if not _competing_entity_mentions(sentence, phrase):
            kept_sentences.append(sentence)
            continue

        clauses = _CLAUSE_BOUNDARY.split(sentence)
        safe = [
            clause
            for clause in clauses
            if not _competing_entity_mentions(clause, phrase)
        ]
        if not safe:
            continue
        last = safe[-1].rstrip()
        if last.endswith((":", ";")) or re.search(
            r"\b(?:such as|including)\s*$", last, re.IGNORECASE
        ):
            continue

        merged = " ".join(clause.strip() for clause in safe).strip()
        merged = re.sub(r"[,;:]\s*$", ".", merged)
        if merged and not _SENTENCE_END.search(merged):
            merged += "."
        kept_sentences.append(merged)

    return " ".join(kept_sentences).strip()


def anchor_retrieval_pool(
    question: str,
    chunks: list[base.Chunk],
    question_embedding: list[float] | None = None,
) -> tuple[list[base.Chunk], list[str]] | None:
    """Restrict retrieval to chunks containing the query's specific entity.

    Embedding similarity is good at finding related topics but can rank a
    different entity of the same kind highly (for example Toyosu Market for a
    question about Nijo Market). Content words in the question are strong
    signals for the entity the user named.

    Three safeguards keep an incidental word from hijacking the filter:

    1. Candidate terms are ranked by retrieval relevance, not raw rarity. For
       "what are all levels of wagyu", the generic attribute "levels" may be
       rarer than "wagyu", but chunks about wagyu are much more similar to
       the question, so only "wagyu" becomes an anchor. Terms whose best
       chunks are nearly as relevant as the best term's chunks are kept
       together (for example "nijo" and "market", or "kyoto" and
       "temples").
    2. A term that occurs in too much of the corpus is too broad to focus
       retrieval (for example "japan" in a Japan-only scope). It is ignored
       as an anchor when a comparably relevant specific term exists.
    3. If the question contains a specific phrase such as "nijo market" and
       enough chunks contain it, that phrase becomes the filter. This excludes
       chunks about a different Nijo entity (for example Nijo Castle) and
       chunks from a video whose title mentions Nijo but whose paragraph does
       not.

    Returns ``(pool, anchor_terms)`` when anchoring applies, otherwise None.
    """
    query_terms = {
        term for term in _content_terms(question) if _is_anchor_token(term)
    }
    if not query_terms:
        return None

    if question_embedding is None:
        question_embedding = base.create_embedding(question)
    question_norm = math.sqrt(
        math.fsum(value * value for value in question_embedding)
    )
    if question_norm == 0.0:
        return None

    chunk_terms: list[tuple[base.Chunk, set[str]]] = []
    chunk_texts: list[tuple[base.Chunk, str]] = []
    term_similarities: dict[str, list[float]] = {
        term: [] for term in query_terms
    }
    for chunk in chunks:
        text = _chunk_text(chunk)
        terms = _content_terms(text)
        if not terms:
            continue
        chunk_texts.append((chunk, text.lower()))
        chunk_terms.append((chunk, terms))

        matched = terms & query_terms
        if not matched:
            continue
        if len(chunk.embedding) != len(question_embedding):
            return None
        similarity = math.fsum(
            q * value
            for q, value in zip(question_embedding, chunk.embedding)
        ) / question_norm
        for term in matched:
            term_similarities[term].append(similarity)
    if not chunk_terms:
        return None

    df = {
        term: len(similarities)
        for term, similarities in term_similarities.items()
    }
    relevance: dict[str, float] = {}
    for term, similarities in term_similarities.items():
        if not similarities:
            continue
        best = sorted(similarities, reverse=True)[:ANCHOR_SCORE_TOP_K]
        relevance[term] = math.fsum(best) / len(best)
    if not relevance:
        return None

    best_score = max(relevance.values())
    retained = sorted(
        term
        for term, score in relevance.items()
        if score >= best_score - ANCHOR_SIMILAR_SCORE_MARGIN
    )
    max_df = max(2, int(len(chunk_terms) * ANCHOR_MAX_DF_RATIO))
    anchors = [term for term in retained if df[term] <= max_df]
    if not anchors:
        # Only corpus-wide terms are close to the best match. Anchoring on
        # them would not narrow retrieval, and anchoring on a less relevant
        # rare term would be wrong, so leave retrieval unfiltered.
        return None

    anchor_set = set(anchors)
    best_pool: list[base.Chunk] = []
    best_phrase = ""
    best_key: tuple[int, int] | None = None
    for phrase in _query_phrase_candidates(question):
        if not anchor_set.intersection(phrase.split()):
            continue
        pattern = _phrase_pattern(phrase)
        pool = [
            chunk for chunk, text in chunk_texts if pattern.search(text)
        ]
        if len(pool) < MIN_ANCHOR_CHUNKS:
            continue
        # Prefer the longest phrase, then the rarest one.
        key = (len(phrase.split()), -len(pool))
        if best_key is None or key > best_key:
            best_key = key
            best_pool = pool
            best_phrase = phrase

    if best_phrase:
        return best_pool, [best_phrase]

    pool = [
        chunk for chunk, terms in chunk_terms if terms & anchor_set
    ]
    if len(pool) < MIN_ANCHOR_CHUNKS:
        return None
    return pool, anchors


def answer(
    question: str,
    chunks: list[base.Chunk],
    depth: int | None = None,
    translate_answer: bool = False,
    batch_size: int = BATCH_SIZE,
) -> tuple[str | None, list[tuple[base.Chunk, float]]]:
    """Retrieve context and stream a low-context iterative answer.

    Retrieval uses chat.py's semantic ranking, preceded by query-topic
    anchoring when the question names a specific entity. The anchor term is
    selected by retrieval relevance, not raw rarity, so generic attribute
    words cannot displace the real topic. Generation is split into
    micro-batches with a rolling entity ledger, tail-window continuity, and
    SKIP gating, followed by a stateful aggregation summary for count/list
    questions.
    """
    question_embedding = base.create_embedding(question)
    retrieval_pool = chunks
    anchor_phrase: str | None = None
    anchored = anchor_retrieval_pool(question, chunks, question_embedding)
    if anchored is not None:
        retrieval_pool, anchor_terms = anchored
        if len(anchor_terms) == 1 and len(anchor_terms[0].split()) >= 2:
            anchor_phrase = anchor_terms[0]
    selected = base.top_chunks(
        question_embedding, retrieval_pool, limit=depth
    )
    if not selected:
        print("No trunks available to use as context.")
        return None, []

    aggregation = needs_aggregation(question)
    sanitize = anchor_phrase is not None and not aggregation
    usable: list[tuple[base.Chunk, float, str]] = []
    for chunk, score in selected:
        text = _chunk_text(chunk)
        if not text:
            print(
                f"Warning: cannot read {chunk.trunk_path.name}, skipping.",
                file=sys.stderr,
            )
            continue
        usable.append((chunk, score, text))
    if aggregation:
        # "what are all levels of wagyu" should aggregate levels, not cuts,
        # brands, or side dishes mentioned by other chunks about wagyu.
        category_terms = _category_terms(question)
        if category_terms:
            usable = _focus_by_attribute(
                usable, question, None, category_terms
            )
    if sanitize:
        usable = _focus_by_attribute(usable, question, anchor_phrase)
        sanitized: list[tuple[base.Chunk, float, str]] = []
        for chunk, score, text in usable:
            clean = _sanitize_competing_entities(text, anchor_phrase)
            if clean:
                sanitized.append((chunk, score, clean))
        usable = sanitized
    if not usable:
        print("I could not read any matching trunk contents.", file=sys.stderr)
        return None, []

    batch_size = max(1, batch_size)
    sources = [(chunk, score) for chunk, score, _ in usable]

    emitter = AnswerEmitter(translate_answer)
    entity_map: dict[str, str] = {}
    tail = ""
    style = answer_style(question)
    # A factual question only needs the first batch that produces prose. Once
    # it answers (e.g. "Nijo Market opens at 7 AM"), later batches tend to
    # repeat the same fact or add unasked details.
    single_pass = bool(FACTUAL_QUERY.search(question)) and not aggregation

    for start in range(0, len(usable), batch_size):
        batch = usable[start : start + batch_size]
        batch_text = "\n\n---\n\n".join(
            f"[{chunk.video_id}.{chunk.index}]\n{text}"
            for chunk, _, text in batch
        )
        prompt = BATCH_PROMPT.format(
            query=question,
            style=style,
            covered=covered_ledger(entity_map),
            tail=tail or "(none - this is the first section)",
            batch=batch_text,
        )
        result = run_batch_stream(prompt, emitter.feed)
        add_entities(entity_map, result.entities)

        if result.content:
            emitter.section_break()
            tail = tail_context(result.content)
            if single_pass:
                break

    if aggregation and entity_map:
        summary_prompt = FINAL_SUMMARY_PROMPT.format(
            query=question,
            items="\n".join(
                f"- {item}" for item in entity_map.values()
            ),
        )
        run_plain_stream(summary_prompt, emitter.feed)
        emitter.section_break()

    answer_text = emitter.finish()
    if not answer_text:
        print("No new relevant information was found in the retrieved sections.")
        return None, []

    return answer_text, sources


def status_report(
    tag: str,
    transcript: str | None,
    chunks: list[base.Chunk],
    depth: int | None,
    batch_size: int = BATCH_SIZE,
) -> str:
    """Show chat.py's status plus the low-context architecture details."""
    report = base.chat_status_report(tag, transcript, chunks, depth)
    calls = math.ceil(len(chunks) / max(1, batch_size)) if chunks else 0
    return "\n".join(
        [
            report,
            "  architecture: low-context iterative streaming RAG",
            f"  batching:     {batch_size} chunk(s)/micro-batch, "
            f"up to {calls} LLM call(s) for the loaded scope",
            f"  gating:       rolling entity ledger + SKIP suppression",
        ]
    )


def main() -> None:
    args = base.parse_args()
    tag = args.tag
    if tag == base.INTERACTIVE_TAG or not tag:
        tag = base.prompt_for_tag()
    chunks = base.load_embeddings(tag)
    print(base.embedding_memory_report(chunks))
    transcript: str | None = None
    latest_answer: str | None = None
    latest_sources: list[tuple[base.Chunk, float]] = []
    depth: int | None = base.DEFAULT_DEPTH

    history = base.PlainFileHistory(base.HISTORY_FILE)
    print(
        f"Chat history: {base.history_count(base.HISTORY_FILE)} saved entries "
        f"(questions + commands) in {base.HISTORY_FILE}"
    )
    tag_state = {"tag": tag}
    session = base.make_prompt_session(history, tag_state)
    print(
        f"Low-context RAG chatbot ready ({base.EMBED_MODEL} + "
        f"{base.CHAT_MODEL}, tag {tag!r}, cosine similarity > "
        f"{base.MIN_SIMILARITY}). Type /exit to quit."
    )
    print(
        f"Low-context mode: {BATCH_SIZE} chunk(s) per batch, rolling entity "
        "ledger, SKIP gating, and a final aggregation for count/list questions."
    )
    print(
        "Simplified Chinese questions are translated to English "
        f"with {base.TRANSLATION_MODEL}; answers are translated back as they "
        "stream."
    )
    print(
        "Vim mode: Esc = normal, i = insert, v/V = visual, "
        "dd/dw = delete, u = undo. Up/Down = question + command history."
    )
    print(
        "Commands: /tag [tag], /refresh, /transcript <hash|keyword>, "
        "/youtube learn <hash|url>, /wiki <word>, /dict <word>, "
        "/depth <n|auto>, "
        "/memory, /status, /sources, /exit. Tab completes them."
    )

    while True:
        try:
            question = session.prompt("You: ").strip()
        except (EOFError, KeyboardInterrupt):
            print()
            break

        if not question:
            continue
        command = question.lower()
        if command in {"/exit", "/quit", "/q", "exit", "quit"}:
            break
        if question.startswith("/"):
            name, _, argument = question.partition(" ")
            name = name.lower()
            if name == "/memory":
                print(base.embedding_memory_report(chunks))
            elif name == "/tag":
                switched = base.switch_tag(argument, chunks)
                if switched is not None:
                    tag, chunks = switched
                    tag_state["tag"] = tag
                    transcript = None
                    print(f"Now using tag {tag!r}.")
            elif name == "/refresh":
                refreshed = base.refresh_tag(tag, chunks)
                if refreshed is not None:
                    chunks = refreshed
                    transcript = None
                    print(f"Now using tag {tag!r} (embeddings refreshed).")
            elif name == "/transcript":
                switched = base.switch_transcript(argument, chunks)
                if switched is not None:
                    transcript, chunks = switched
                    print(
                        f"Now answering only from transcript {transcript!r}."
                    )
            elif name == "/search_transcript":
                switched = base.search_and_switch_transcript(argument, chunks)
                if switched is not None:
                    transcript, chunks = switched
                    print(
                        f"Now answering only from transcript {transcript!r}."
                    )
            elif name == "/youtube":
                subcommand, _, subargument = argument.partition(" ")
                subcommand = subcommand.strip().lower()
                value = subargument.strip()
                if subcommand != "learn":
                    print(
                        "Usage: /youtube learn <youtube_video_hash|url>"
                    )
                    continue
                if not value:
                    try:
                        value = session.prompt("Video hash or URL: ").strip()
                    except (EOFError, KeyboardInterrupt):
                        print()
                        continue
                    if not value:
                        print(
                            "Usage: /youtube learn "
                            "<youtube_video_hash|url>"
                        )
                        continue

                video_id = base.learn_video(value)
                if video_id is None:
                    continue

                if transcript is not None:
                    if video_id == transcript:
                        print(
                            f"Reloading embeddings for transcript "
                            f"{transcript!r}..."
                        )
                        try:
                            new_chunks = base.load_embeddings(
                                video_ids={transcript}
                            )
                        except SystemExit as exc:
                            print(
                                f"Could not reload embeddings: {exc}",
                                file=sys.stderr,
                            )
                        else:
                            chunks.clear()
                            chunks = new_chunks
                            gc.collect()
                            print(base.embedding_memory_report(chunks))
                    else:
                        print(
                            f"Still answering only from transcript "
                            f"{transcript!r}; the loaded embeddings are "
                            "unchanged."
                        )
                elif base.video_is_tagged(video_id, tag):
                    print(f"Reloading embeddings for tag {tag!r}...")
                    try:
                        new_chunks = base.load_embeddings(tag)
                    except SystemExit as exc:
                        print(
                            f"Could not reload embeddings: {exc}",
                            file=sys.stderr,
                        )
                    else:
                        chunks.clear()
                        chunks = new_chunks
                        gc.collect()
                        print(base.embedding_memory_report(chunks))
                else:
                    print(
                        f"{video_id} was not added to tag {tag!r}; "
                        "the loaded embeddings are unchanged."
                    )
            elif name == "/wiki":
                base.lookup_wikipedia(argument)
            elif name == "/dict":
                base.lookup_dictionary(argument)
                base.show_latest_answer_usage(argument, latest_answer)
            elif name == "/status":
                print(status_report(tag, transcript, chunks, depth))
            elif name == "/sources":
                base.show_sources(latest_sources)
            elif name == "/depth":
                value = argument.strip().lower()
                if not value:
                    current = "auto" if depth is None else str(depth)
                    print(f"Retrieval depth: {current}")
                    print("Usage: /depth <number of trunks> | /depth auto")
                    print(f"Default depth: {base.DEFAULT_DEPTH}")
                elif value in {"auto", "default", "0"}:
                    depth = None
                    print(
                        "Retrieval depth: auto (chunks above cosine "
                        f"similarity {base.MIN_SIMILARITY}, else top "
                        f"{base.FALLBACK_TOP_N})."
                    )
                else:
                    try:
                        new_depth = int(value)
                    except ValueError:
                        new_depth = 0
                    if new_depth < 1:
                        print(
                            f"Invalid depth: {argument.strip()!r}. "
                            "Use a positive integer, or 'auto'."
                        )
                    else:
                        depth = new_depth
                        print(
                            f"Retrieval depth: top {depth} matching trunk(s)."
                        )
            else:
                print(
                    f"Unknown command: {name}. "
                    "Try /tag [tag], /refresh, /transcript <hash|keyword>, "
                    "/youtube learn <hash|url>, /wiki <word>, "
                    "/dict <word>, "
                    "/depth <n|auto>, /memory, /status, /sources, /exit."
                )
            continue

        translate_answer = False
        if base.contains_simplified_chinese(question):
            print("[translating the question to English...]", flush=True)
            try:
                english_question = base.translate_to_english(question)
            except RuntimeError as exc:
                print(
                    f"Error: could not translate the question: {exc}",
                    file=sys.stderr,
                )
                continue
            print(f"[translated question: {english_question}]")
            question = english_question
            translate_answer = True

        print("\nAssistant: ", end="", flush=True)
        try:
            response, sources = answer(
                question,
                chunks,
                depth=depth,
                translate_answer=translate_answer,
            )
        except Exception as exc:
            print(f"\nError: {exc}", file=sys.stderr)
        else:
            if response is not None:
                latest_answer = response
                latest_sources = sources


if __name__ == "__main__":
    main()
