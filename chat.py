#!/usr/bin/env python3
"""Interactive lexical RAG chatbot over the YouTube transcript trunks.

Usage:
    python3 chat.py

At startup every ./.chat/youtube_transcripts/<youtube_video_hash>.<index>.trunk
file is loaded into one global corpus. Every question searches the whole
corpus; there is no tag scope or per-topic partition. No embedding files and
no cosine similarity are used. The BM25 postings live in an on-disk SQLite
FTS5 database at ./.chat/articles_cache/bm25_index_fts.db.

For each question:
    1. Expand the question with Ollama model qwen3:1.7b into a short list of
       synonyms and related terms (32 output tokens, single line).
    2. Score every loaded trunk through the SQLite FTS5 BM25 index, using the
       original question tokens (weighted twice) plus the expanded tokens.
    3. Keep the top 20 BM25 candidates by default. `/depth <n>` changes that
       count; `/depth auto` enables adaptive selection (BM25 score elbow,
       per-video cap, near-duplicate removal, and a token budget).
    4. Filter the candidate chunks with the small qwen3:1.7b question filter
       before synthesis (skipped for list/aggregation questions, which need
       broad coverage).
    5. Wrap every selected trunk in a numbered <chunk id="N"> block and build
       the answer prompt from ./research/100_trunks_prompt.txt, so the model
       stays grounded in the retrieved passages without exposing chunk ids.
    6. Stream the answer from Ollama model ornith:35b. Retrieval / filter /
       prefill / TTFT / prompt-token measurements are hidden by default and
       can be shown with `/benchmark` (or continuously with `/benchmark on`).

Simplified Chinese questions are detected automatically and translated into
English only for BM25 retrieval; the answer is requested in Simplified
Chinese directly, so no answer post-translation is needed.

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
    /memory            Show memory used by the BM25 index.
    /refresh           Reload the global trunk corpus.
    /youtube transcript <hash|keyword>
                       Use one video's transcript (reloads trunks).
                       A keyword searches the .title files with ag and shows
                       a hinting list of <hash> <title> matches to pick from.
    /youtube search_transcript <keyword>
                       Search transcript titles with ag and pick a video.
    /youtube learn <hash|url>
                       Fetch, rewrite, chunk, and embed a video.
    /wiki <question>   For a narrow fact question, search the full Wikipedia
                       article excerpts with ag and answer directly. Otherwise
                       output the summary for each main entity.
    /wiki_article <question>
                       Print the full Wikipedia article for each main entity
                       of <question> as soon as it is fetched.
    /dict <word>       Show English dictionary definitions for <word> from a
                       local Ollama model, plus an explanation of its use and
                       meaning in the latest answer (when the word appears
                       there).
    /depth <n|auto>    Set the number of top BM25 matching trunks (default: 20),
                       or use adaptive retrieval (score elbow + token budget).
    /status            Show the current chat status (scope, loaded chunks,
                       retrieval depth, budget, cache, and history).
    /sources           Show the source list for the latest answer.
    /benchmark [on|off]
                       Show retrieval / filter / prefill / TTFT / token
                       metrics for the latest answer. `on` keeps showing them
                       after every answer; `off` hides them again.
    /exit, /quit, /q   Leave the chatbot.
"""

import argparse
import contextlib
import difflib
import gc
import hashlib
import io
import json
import math
import os
import re
import subprocess
import sys
import threading
import time
from collections import OrderedDict
from dataclasses import dataclass
from functools import lru_cache
from pathlib import Path
from typing import Callable
from urllib.parse import quote, unquote

try:
    import requests
except ImportError as exc:  # pragma: no cover - dependency guard
    sys.exit(f"Missing dependency: {exc.name}. Install with: pip install requests")

import sqlite_bm25

try:
    from prompt_toolkit import PromptSession
    from prompt_toolkit.application import get_app
    from prompt_toolkit.completion import Completer, Completion
    from prompt_toolkit.filters import vi_insert_mode
    from prompt_toolkit.history import History
    from prompt_toolkit.key_binding import KeyBindings
    from prompt_toolkit.key_binding.vi_state import InputMode
except ImportError as exc:  # pragma: no cover - dependency guard
    sys.exit(
        f"Missing dependency: {exc.name}. Install with: "
        "python3 -m pip install --user prompt_toolkit"
    )

# Model used to expand questions into BM25 search terms.
EXPANSION_MODEL = "qwen3:1.7b"
# Small model used to filter BM25 candidates before synthesis.
FILTER_MODEL = "qwen3:1.7b"
CHAT_MODEL = "ornith:35b"
# Coding questions (code samples for a language feature, structure, or
# architecture) are routed to the code-generation model from
# ./prompts/programming_language_tips.prompt, without thinking enabled.
CODING_MODEL = "ornith:35b"
# Local model used by /ask for quick questions that do not use retrieval.
SIMPLE_ANSWER_MODEL = "ornith:35b"
# Local model used to translate Simplified Chinese questions for retrieval.
TRANSLATION_MODEL = "qwen3:1.7b"
# Local model used by /dict to generate definitions.
DICT_MODEL = "ornith:35b"
FALLBACK_TOP_N = 10
# Default number of top BM25 trunk matches used as context. `/depth <n>`
# changes it; `/depth auto` switches to adaptive retrieval (depth=None).
DEFAULT_DEPTH = 20
# Estimated-token budgets for adaptive (auto) retrieval only. List-style
# questions get a larger budget because they need broader coverage.
MAX_CONTEXT_TOKENS = 4096
LIST_CONTEXT_TOKENS = 6144
# Deterministic stopping rules for auto retrieval (no extra model call).
AUTO_CANDIDATE_LIMIT = 50
SCORE_ELBOW_RATIO = 0.35
SCORE_FLOOR_RATIO = 0.15
# Diversity controls: cap how many chunks one video may contribute and drop
# near-duplicates with highly overlapping token sets.
MAX_CHUNKS_PER_VIDEO = 5
NEAR_DUPLICATE_JACCARD = 0.90
# Small-LLM filter controls. Filtering only applies to a bounded candidate
# set; list/aggregation questions skip it so they keep broad coverage.
FILTER_MIN_CANDIDATES = 5
FILTER_MAX_CANDIDATES = 20
FILTER_MIN_KEEP = 3
# If the small relevance filter rejects every BM25 candidate, trust the
# lexical ranking instead of returning an empty context. This prevents a
# false "not in the knowledge base" answer when the filter disagrees with
# strong BM25 evidence (for example a misspelled name or a singular/plural
# variant in the question).
FILTER_FALSE_NEGATIVE_KEEP = 5
# The small filter may exclude a strong lexical match because its prompt uses
# the user's exact wording (which can be misspelled or phrased differently
# from the transcript). Always keep this many top BM25 candidates and let the
# filter add any other relevant candidates on top of them.
FILTER_ALWAYS_KEEP = 5
# Query words that carry little entity/context information. They are ignored
# when fuzzy-correcting a misspelled name, so "terrance tao" is resolved using
# "tao" rather than generic words such as "any" or "does".
FUZZY_ANCHOR_STOPWORDS = frozenset(
    "a an the and or but if then than that this these those there here "
    "what which who whom whose when where why how is are was were be been "
    "being do does did done have has had can could should would will may "
    "might must shall to of in on at for from with about into over after "
    "before under between during any some all more most other another "
    "such only just also very much many few own same so not no nor too "
    "tell show give me please new latest current".split()
)
FILTER_SNIPPET_CHARS = 700
FILTER_NUM_PREDICT = 96
LLM_FILTER_ENABLED = True
# Query expansion is deliberately tiny: a single line, at most 32 tokens.
EXPANSION_NUM_PREDICT = 32
# All small-model helper calls use this context size. Keeping it identical
# everywhere (including prewarm) prevents Ollama from reloading the model.
EXPANSION_NUM_CTX = 8192
# Fuzzy query-term correction. Proper nouns are frequently misspelled in
# questions ("terrence tao" / "terrance tao" vs. "Terence Tao") and BM25
# matches whole tokens exactly, so an out-of-vocabulary word contributes
# nothing. Words that do not exist in the index vocabulary are matched
# against nearby vocabulary words and the best spellings are added to the
# query. This is generic: it also helps plurals, inflections, and typos.
FUZZY_MATCH_ENABLED = True
FUZZY_MIN_TOKEN_LENGTH = 5
FUZZY_LENGTH_DELTA = 2
FUZZY_SCORE_CUTOFF = 0.78
FUZZY_MAX_MATCHES = 2
# A query phrase is treated as a named entity when its words almost always
# occur together in the corpus (for example "terence tao"). Candidate chunks
# are then limited to the ones that mention that entity, so a generic word in
# the question ("books") cannot pull in unrelated chunks about other books.
FUZZY_ENTITY_COOCCURRENCE_RATIO = 0.5
FUZZY_ENTITY_MIN_COOCCURRENCE = 2
# Inside an entity phrase, a word whose document frequency is this many times
# higher than the phrase's rarest word is a generic head noun (for example
# "market" in "Nijo Market"). Such a word must not match other entities on
# its own, so retrieval anchors on the distinctive word(s) instead.
FUZZY_ENTITY_GENERIC_DF_RATIO = 4.0
# Topic anchors used to choose among an entity's chunks. Short but meaningful
# terms such as "AI" must count, and answer-format words ("in 600 words") or
# bare numbers must not. A narrow topic subset is only trusted when it keeps
# enough entity chunks to be evidence rather than an accident of one rare
# word; otherwise the entity ranking is left intact.
FUZZY_ENTITY_TOPIC_MIN_TERM_LENGTH = 2
FUZZY_ENTITY_TOPIC_MIN_MATCHES = 3
FUZZY_ENTITY_TOPIC_STOPWORDS = FUZZY_ANCHOR_STOPWORDS | frozenset(
    "it its he him his she her they them we us our you your i my mine "
    "word words essay paragraph paragraphs sentence sentences summary "
    "summarize explanation describe description".split()
)
# Acronyms in questions (for example "IDP") are often spelled out in the
# corpus next to their definition ("International Driving Permit (IDP)").
# BM25 cannot bridge the two forms on its own, so the expansion is mined from
# the documents that contain the acronym and added to the query.
ACRONYM_QUERY_PATTERN = re.compile(r"\b[A-Z][A-Z0-9]{1,7}\b")
ACRONYM_STOPWORDS = frozenset(
    "a an the of and for in on to from with".split()
)
# Context window requested from Ollama for answer generation.
ANSWER_NUM_CTX = 32768
# Keep all models resident between questions to avoid cold-start latency.
OLLAMA_KEEP_ALIVE = "30m"
# Questions that ask for an exhaustive or comparative answer get more context.
LIST_QUERY_PATTERN = re.compile(
    r"\b(list|all|every|each|compare|comparison|details?|detailed|enumerate|"
    r"options|types?|kinds?|levels?|examples?|recommendations?)\b",
    re.IGNORECASE,
)
# Optional two-stage streaming: stream the first pass from a small context, then
# append an expansion pass over the remaining selected chunks.
PROGRESSIVE_FIRST_PASS = 10
# Rough token estimate: about four ASCII characters or one non-ASCII
# character (for example CJK) per token.
CHARS_PER_TOKEN = 4
SCRIPT_DIR = Path(__file__).resolve().parent
OUTPUT_DIR = Path.cwd() / ".chat" / "youtube_transcripts"
CHAT_DIR = Path.cwd() / ".chat"
# Learned articles (news stories and learned web articles) live beside the
# YouTube corpus. Each learned article has a .title file and one .trunk file
# per knowledge paragraph, so the same BM25 retrieval code can search articles
# and transcripts together.
ARTICLES_DIR = CHAT_DIR / "articles_cache"
# The on-disk FTS5 BM25 index. Learned articles write their trunks and their
# search entries here together, and every question scores against it.
BM25_DB_PATH = ARTICLES_DIR / "bm25_index_fts.db"
HISTORY_FILE = CHAT_DIR / "question_history.txt"

OLLAMA_HOST = os.environ.get("OLLAMA_HOST", "http://localhost:11434").rstrip("/")
if not OLLAMA_HOST.startswith(("http://", "https://")):
    OLLAMA_HOST = f"http://{OLLAMA_HOST}"

TRUNK_FILE = re.compile(
    r"^(?P<video>[A-Za-z0-9_-]+)\.(?P<index>\d+)\.trunk$"
)
PARAGRAPH_PATTERN = re.compile(
    r"<paragraph>(.*?)</paragraph>", re.DOTALL | re.IGNORECASE
)
# Output of `ag --vimgrep`: <path>:<line>:<column>:<matching text>.
AG_MATCH_PATTERN = re.compile(
    r"^(?P<path>.+?):\d+:\d+:(?P<title>.*)$"
)

WIKI_SUMMARY_URL = "https://en.wikipedia.org/api/rest_v1/page/summary/{word}"
# The /wiki command finds the main entities of a question. Narrow fact
# questions are answered from ag keyword hits in each entity's full article;
# broad questions output each entity's Wikipedia REST summary immediately.
# /wiki_article uses this helper to print complete Wikipedia articles.
WIKI_FULL_ARTICLE_SCRIPT = Path(__file__).resolve().parent / (
    "wiki_full_article_search.php"
)
# The REST summary endpoint resolves a bare title such as "Rust" to that
# word's primary topic (the iron oxide), even when the question is about the
# programming language. Ambiguous bare titles are therefore checked against
# Wikipedia search results using the question's own qualifiers.
WIKI_SEARCH_API_URL = "https://en.wikipedia.org/w/rest.php/v1/search/page"
WIKI_SEARCH_LIMIT = 8
# Words that carry no disambiguating information when they appear in a
# question. They are ignored while matching question qualifiers against the
# words in a Wikipedia title and its description.
WIKI_QUESTION_STOPWORDS = frozenset(
    {
        "about",
        "after",
        "again",
        "all",
        "also",
        "and",
        "any",
        "are",
        "article",
        "as",
        "at",
        "be",
        "been",
        "before",
        "being",
        "between",
        "both",
        "but",
        "by",
        "can",
        "could",
        "describe",
        "did",
        "do",
        "does",
        "each",
        "explain",
        "few",
        "for",
        "from",
        "get",
        "give",
        "had",
        "has",
        "have",
        "he",
        "her",
        "here",
        "hers",
        "him",
        "his",
        "how",
        "i",
        "if",
        "in",
        "into",
        "is",
        "it",
        "its",
        "just",
        "know",
        "latest",
        "let",
        "list",
        "main",
        "many",
        "may",
        "me",
        "more",
        "most",
        "much",
        "must",
        "my",
        "new",
        "no",
        "not",
        "now",
        "of",
        "on",
        "one",
        "only",
        "or",
        "other",
        "our",
        "out",
        "over",
        "own",
        "please",
        "same",
        "she",
        "should",
        "show",
        "so",
        "some",
        "such",
        "tell",
        "than",
        "that",
        "the",
        "their",
        "them",
        "then",
        "there",
        "these",
        "they",
        "this",
        "those",
        "through",
        "to",
        "too",
        "under",
        "up",
        "us",
        "use",
        "very",
        "was",
        "we",
        "were",
        "what",
        "when",
        "where",
        "which",
        "while",
        "who",
        "whom",
        "whose",
        "why",
        "will",
        "with",
        "would",
        "you",
        "your",
    }
)
WIKI_TITLE_QUALIFIER_PATTERN = re.compile(
    r"^(?P<base>.+?)\s*\((?P<qualifier>[^()]+)\)\s*$"
)
WIKI_ENTITY_MODEL = CHAT_MODEL
WIKI_ANSWER_MODEL = CHAT_MODEL
WIKI_KEYWORD_MODEL = CHAT_MODEL
WIKI_MAX_ENTITIES = 4
WIKI_MAX_KEYWORDS = 5
# Keyword hits should survive inflection differences between the question and
# the article: a question about the "creator" of something is answered by a
# sentence saying it was "created". Each keyword is therefore also searched by
# its stem, and the stem matches any longer word ("creat" -> created, creator,
# creation).
WIKI_KEYWORD_SUFFIXES = (
    "ations",
    "ation",
    "ings",
    "ing",
    "ers",
    "er",
    "ors",
    "or",
    "ed",
    "es",
    "s",
    "ly",
)
WIKI_AG_CONTEXT_LINES = 2
WIKI_EXCERPT_MAX_CHARS = 12_000
# A question often misspells the subject it is about ("terrence tao"). Once the
# entity model has resolved the canonical Wikipedia title, spellings that are
# close to that title are rewritten before the article is searched and the
# answer is composed. This keeps a typo in the question from making the local
# model reject an excerpt that plainly contains the fact.
WIKI_NAME_MATCH_CUTOFF = 0.84
WIKI_NAME_TOKEN_PATTERN = re.compile(r"[A-Za-z0-9][A-Za-z0-9'’-]*")
WIKI_ARTICLE_TIMEOUT = 75
WIKI_MAX_ARTICLE_CHARS = 24_000
WIKI_MIN_ARTICLE_CHARS = 8_000
WIKI_MAX_CONTEXT_CHARS = 72_000
WIKI_GENERIC_TITLE_TOKENS = frozenset(
    {
        "a",
        "an",
        "and",
        "city",
        "company",
        "for",
        "history",
        "in",
        "inc",
        "list",
        "market",
        "of",
        "on",
        "the",
        "university",
        "works",
    }
)
ANSI_ESCAPE_PATTERN = re.compile(r"\x1b\[[0-9;?]*[ -/]*[@-~]")
WIKI_SEARCH_FALLBACK_LINE = re.compile(
    r"^Exact match not found for .*?Searching\.\.\.$", re.MULTILINE
)
WIKI_SOURCE_LINE = re.compile(r"^Source:\s*(\S+)\s*$", re.MULTILINE)
WIKI_NO_ARTICLE_LINE = re.compile(
    r"^(?:No article found matching|No search results found for) .*$",
    re.MULTILINE,
)
# A narrow question asks for one specific fact (a birthday, a population, a
# location), so the full article can be searched for the answer instead of
# streaming every entity summary. Broad "tell me about" questions keep the
# summary behavior.
WIKI_BROAD_QUESTION_PATTERN = re.compile(
    r"\b(?:all\s+about|biography|compare|describe|explain|history|list|"
    r"main\s+(?:attractions|points|topics)|overview|summary|tell\s+me\s+about|"
    r"what\s+are\s+the\s+(?:main|key))\b",
    re.IGNORECASE,
)
WIKI_WH_QUESTION_PATTERN = re.compile(
    r"\b(?:when|where|who|whom|whose)\b",
    re.IGNORECASE,
)
# Yes/no questions ("is terrence tao from china?", "did he win the prize?")
# ask for one specific fact just like a wh-question, even though they contain
# no wh-word. Treating them as narrow lets /wiki answer from the article
# instead of dumping the entity summaries.
WIKI_YES_NO_QUESTION_PATTERN = re.compile(
    r"^\s*(?:is|are|was|were|am|do|does|did|can|could|will|would|shall|should|"
    r"may|might|must|has|have|had|isn['’]t|aren['’]t|wasn['’]t|weren['’]t|"
    r"don['’]t|doesn['’]t|didn['’]t|can['’]t|couldn['’]t|won['’]t|"
    r"wouldn['’]t|shouldn['’]t)\b",
    re.IGNORECASE,
)
WIKI_HOW_QUANTITY_PATTERN = re.compile(
    r"\bhow\s+(?:many|much|old|long|tall|far|large|big|deep|high|wide|"
    r"heavy)\b",
    re.IGNORECASE,
)
WIKI_FACT_TERM_PATTERN = re.compile(
    r"\b(?:age|address|airport|area|author|birth|birthday|born|capital|"
    r"ceo|children|close[sd]?|cost|currency|date|death|died|distance|"
    r"founded|founder|headquarters|height|language|length|location|"
    r"married|mayor|net\s+worth|opening|owner|population|president|"
    r"price|published|release|revenue|salary|spouse|weight)\b",
    re.IGNORECASE,
)
WIKI_NO_ANSWER_PATTERN = re.compile(
    r"^\s*(?:NO[_ ]ANSWER|INSUFFICIENT[_ ]INFORMATION|NOT[_ ]FOUND)\b",
    re.IGNORECASE,
)
HTTP_HEADERS = {
    "User-Agent": "article-vector-strategy-chat/1.0 (local RAG chatbot)",
    "Accept": "application/json",
}

# Answer prompt template. The default prompt is file-based so it can be tuned
# without touching the retrieval code; the fallback keeps chat.py usable if
# the research directory is missing.
PROMPT_TEMPLATE_PATH = (
    Path(__file__).resolve().parent / "research" / "100_trunks_prompt.txt"
)
FALLBACK_PROMPT = """You are a precise, analytical retrieval-augmented assistant. Your task is to answer the user's target question strictly using the provided context chunks.

### INSTRUCTIONS & CONSTRAINTS:
1. Grounding: Rely ONLY on the information directly stated in the chunks below. Do not infer unstated facts or incorporate external knowledge.
2. No Citations: Do not mention chunk IDs, chunk numbers, or the word "Chunk" anywhere in the answer. The numbered chunk blocks are internal retrieval metadata only.
3. Conflict Resolution: If two chunks present conflicting figures or statements, highlight the discrepancy.
4. Aggregations & Counts: If asked to count, list, or aggregate distinct entities across chunks, verify that duplicates or overlapping mentions are deduplicated before providing a final count.
5. Missing Data: If the provided chunks do not contain sufficient evidence to answer the question, state: "The provided context does not contain sufficient information to answer this question."

<context>
[Insert context chunks here]
</context>

### TARGET QUESTION:
[Insert your question here]

### RESPONSE:
Provide a direct answer / synthesis. Do not include a separate evidence
section.
"""
PROMPT_CONTEXT_PATTERN = re.compile(
    r"<context>.*?</context>", re.DOTALL | re.IGNORECASE
)
PROMPT_QUESTION_PLACEHOLDER = "[Insert your question here]"
PROMPT_QUESTION_MARKER = "### TARGET QUESTION:"


def load_prompt_template() -> str:
    """Load the 100-trunk prompt template, with a built-in fallback."""
    try:
        template = PROMPT_TEMPLATE_PATH.read_text(
            encoding="utf-8", errors="replace"
        ).strip()
    except OSError:
        return FALLBACK_PROMPT
    if "<context>" not in template.lower():
        return FALLBACK_PROMPT
    return template


LANGUAGE_INSTRUCTION = "Answer in {language}. Do not translate proper nouns.\n"

PROGRESSIVE_PROMPT = """<context>{context}</context>
{language_instruction}You are continuing an answer that has already started.
<answer_so_far>
{answer_so_far}
</answer_so_far>
Use only the new <context> above to add missing details to the answer. Do not
repeat any fact, heading, list item, or phrasing that is already in
<answer_so_far>. If the new context adds nothing, output nothing and do not
mention this instruction. Continue in the same style and language."""

DICT_USAGE_PROMPT = """The following is the latest answer in this conversation:
<latest_answer>
{latest_answer}
</latest_answer>

The word "{word}" appears in that answer. Explain how it is used there
(its part of speech and grammatical role, if clear) and what it means in
that specific context. Quote the relevant phrase or sentence, then give the
contextual meaning. Keep the explanation concise."""

DICT_PROMPT = """You are an English Dictionary, you are given a context <context>{context}</context>
Base on <context>, show me the following only, DO NOT show me the quote!:
1. The meaning of {word}
2. The word's IPA (International Phonetic Alphabet)
3. Contextural Meaning of {word}
"""

# The word-click dictionary popup shows only these four labelled fields.
DICT_POPUP_PROMPT = """You are an English Dictionary. For the word or phrase
"{word}", answer with exactly these four labelled fields, in this order, and
nothing else:

Word: {word}

IPA: <International Phonetic Alphabet transcription, or — if unknown>

Part of Speech: <part of speech, or — if unknown>

Contextual Meaning: <one or two sentences giving the meaning of "{word}"
and explaining what it means in the surrounding context>

Rules:
- Separate each section from the next with exactly one blank line.
- Do not add a title, preamble, conclusion, notes, or any other section.
- Do not include a Quote section or quote the surrounding text.
- Do not wrap the labels in markdown or add bullets.
- Keep the whole answer under 100 words.
- If "{word}" is not a valid English word or phrase, keep the four fields
  and say so in Contextual Meaning.

<context>
{context}
</context>"""

QUESTION_TRANSLATION_PROMPT = """Translate the following Simplified Chinese question into natural English.
Preserve names, titles, technical terms, and the intent of the question exactly.
Japanese place names and proper nouns must use their standard romanized
Japanese spelling, not a literal Chinese reading. Examples: 二条市场 ->
Nijo Market; 京都 -> Kyoto; 札幌 -> Sapporo.
Output only the English translation, with no explanation or notes.

<question>
{question}
</question>"""

QUERY_EXPANSION_PROMPT = """You are a search query expansion engine. Given a user query, output 4 to 8
relevant synonyms, related technical keywords, or alternative phrasings.
Output ONLY the space-separated words on a single line. Do not explain.

Query: {query}"""

FILTER_PROMPT = """You are a retrieval relevance filter for a question-answering system.

Question:
<question>
{question}
</question>

Candidates:
{candidates}

Select the candidate numbers that contain information useful for answering
the question. Keep every candidate that is clearly on-topic; prefer precision
but do not discard a candidate merely because it covers only part of the
question. Do not use outside knowledge.

Return ONLY a comma-separated list of candidate numbers, for example: 1, 3, 7
If none are useful, return exactly: NONE"""

ANSWER_TRANSLATION_PROMPT = """Translate the following English text into Simplified Chinese.
Translate every ordinary English word, but leave proper nouns in their original
Latin spelling. Never transliterate place names or invent Chinese names.

Examples:
English: "Takayama is a small town." -> Chinese: "Takayama 是一个小镇。"
English: "I visited Okutama and Hinohara." -> Chinese: "我参观了 Okutama 和 Hinohara。"
English: "1. Kyoto" -> Chinese: "1. Kyoto"

Preserve Markdown formatting and lists. Output only the Simplified Chinese
translation, with no explanation or notes.

<text>
{text}
</text>"""

SIMPLE_ANSWER_PROMPT = """Answer the question below directly and concisely from your own knowledge.
Do not mention retrieval context, chunks, documents, or a knowledge base.
Reply in the same language as the question. If you do not know the answer,
say so plainly instead of inventing facts.

<question>
{question}
</question>

### RESPONSE:"""

WIKI_ENTITY_PROMPT = """Identify the main English Wikipedia article topics needed to answer a question.

<question>
{question}
</question>

Return at most {limit} canonical English Wikipedia article titles, one per
line. Use article titles, not questions or descriptions (for example "Kyoto",
"Terence Tao", or "Photosynthesis"). If the question names a person, place,
organization, event, work, or concept, include it. When the subject has an
ambiguous name, return the exact disambiguated Wikipedia title that fits the
question (for example "Rust (programming language)", "Mercury (planet)", or
"Java (programming language)"), not just the bare word. Keep the parenthetical
disambiguator; it is part of the article title. Do not number the titles, do
not use bullets, and do not add explanations or aliases.

Titles:"""

WIKI_KEYWORD_PROMPT = """Generate up to {limit} English search keywords for finding the answer to a
question inside a Wikipedia article. Include likely synonyms and fact words
that would appear in the article (for example, use "born" for a birthday
question, or "population" for a question about how many people live there).
Also include the inflected forms a sentence might use instead of the word in
the question (for example, "wrote", "written", "author", and "created" for
the question "who wrote ...").
Do not include the article subject's own name, and do not explain anything.

<question>
{question}
</question>

Return one keyword per line, with no numbering or bullets.

Keywords:"""

WIKI_EXCERPT_ANSWER_PROMPT = """Answer the target question using only the Wikipedia excerpts below.
The excerpts are the lines around keyword matches from the article "{title}".

<excerpts>
{excerpt}
</excerpts>

### TARGET QUESTION:
{question}

If the excerpts contain the answer, reply with a direct, concise answer. For
yes/no questions, begin with "Yes" or "No" and then explain the relevant
facts. Do not mention excerpts, keyword matches, the provided context, or
Wikipedia.
If the excerpts do not contain enough information to answer the question,
reply with exactly: NO_ANSWER
"""

WIKI_ARTICLE_ANSWER_PROMPT = """Answer the target question using only the Wikipedia article context below.
The context is the beginning of the article "{title}".

<article>
{context}
</article>

### TARGET QUESTION:
{question}

If the article contains the answer, reply with a direct, concise answer. For
yes/no questions, begin with "Yes" or "No" and then explain the relevant
facts. Do not mention the provided context or Wikipedia.
If the article does not contain enough information to answer the question,
reply with exactly: NO_ANSWER
"""

WIKI_ANSWER_PROMPT = """You are a precise research assistant. Answer the target question using the full Wikipedia article context below.

<context>
{context}
</context>

### TARGET QUESTION:
{question}

### INSTRUCTIONS
1. Grounding: Use the facts stated in the Wikipedia articles above. You may
   combine details from several articles.
2. Directness: Start with a direct answer, then add the useful context. Use
   Markdown headings, lists, and tables when they make the answer clearer.
3. No internal metadata: Do not mention article tags, chunks, excerpts,
   "the provided context", or that any article was shortened.
4. Missing facts: If the context does not contain something the question asks
   for, say so briefly instead of inventing it.
5. Sources: Do not add a Sources, References, or Citations section; the
   application adds the article links automatically.
"""

HAN_PATTERN = re.compile(r"[\u3400-\u4dbf\u4e00-\u9fff]")
KANA_PATTERN = re.compile(r"[\u3040-\u30ff]")
THINK_PATTERN = re.compile(
    r"<think(?:ing)?>.*?</think(?:ing)?>", re.DOTALL | re.IGNORECASE
)

# Characters that only occur in Simplified Chinese. Used to tell Simplified
# from Traditional Chinese without pulling in a full conversion library.
SIMPLIFIED_ONLY_CHARS = frozenset(
    "这个们说国学时会来对开关门问题吗么无电话见现实发东马鸟龙书车长风吹飞"
    "买卖钱银铁错别认识语讲谁请谢让该还过进远边运达选迟适遗邮邻释针钟钢键"
    "镇闭闻间闷闹阁阅队阶阳阴陆陈险随隐难雾静韩页顶项顺须顽顾顿预领颇颈频"
    "颗颜愿类显饭饮饲饱饰馆驾驶验骑骗鱼鸡鸣鸦鸭鸽鹅鹰麦黄齐齿龄龟爱亿仪价"
    "众优传伤侦侧侨债倾仅从伞伟伪杰备儿两册冻净准凉减凑汤温测济涛湾湿灯争"
    "尔墙独获环产画当疗尽监盘碍礼税稳穷竞笔节范筑简签篮筹粮纪约红级纯纸紧"
    "线练组细终结绝统丝经绿维网绵编缘县总织继续"
)

# Characters that only occur in Traditional Chinese (the counterparts of the
# simplified characters above).
TRADITIONAL_ONLY_CHARS = frozenset(
    "這個們說國學會時來對開關門問題嗎麼無電話見現實發東馬鳥龍書車長風飛"
    "買賣錢銀鐵錯別認識語講誰請謝讓該還過進遠邊運達選遲適遺郵鄰釋針鐘鋼鍵"
    "鎮閉聞間悶鬧閣閱隊階陽陰陸陳險隨隱難霧靜韓頁頂項順須頑顧頓預領頗頸頻"
    "顆顏願類顯飯飲飼飽飾館駕駛驗騎騙魚雞鳴鴉鴨鴿鵝鷹麥黃齊齒齡龜愛億儀價"
    "眾優傳傷偵側僑債傾僅從傘偉偽傑備兒兩冊凍淨準涼減湊湯溫測濟濤灣濕燈爭"
    "爾牆獨獲環產畫當療盡監盤礙禮稅穩窮競筆節範築簡簽籃籌糧紀約紅級純紙緊"
    "線練組細終結絕統絲經綠維網綿編緣縣總織繼續"
)

# Sentence groups are translated while the English answer is still streaming.
TRANSLATION_MIN_CHARS = 40
TRANSLATION_MAX_CHARS = 400
TRANSLATION_BOUNDARY = re.compile(
    r"[.!?。！？](?:[\"'”’)\]]*)\s*"
)
LIST_PREFIX = re.compile(r"^(\s*(?:\d+[.)]|[-*+]))\s+")

# Every chatbot command, with the help text shown next to its completion.
COMMANDS = {
    "/memory": "show memory used by the BM25 index",
    "/refresh": "reload the global trunk corpus",
    "/youtube": "YouTube commands: /youtube learn <hash|url> | transcript <hash|URL|keyword> | search_transcript <keyword>",
    "/ask": "ask a quick question directly (no knowledge-base retrieval)",
    "/wiki": "answer narrow fact questions from Wikipedia article excerpts, else show entity summaries",
    "/wiki_article": "print the full Wikipedia article for each main entity of <question>",
    "/dict": "show English dictionary definitions for <word> (local Ollama) and explain its use and meaning in the latest answer",
    "/depth": "set the number of top matching trunks used as context (or 'auto')",
    "/status": "show the current chat status",
    "/sources": "show the source list for the latest answer",
    "/benchmark": "show retrieval/prefill/TTFT metrics for the latest answer (or on|off)",
    "/exit": "leave the chatbot",
    "/quit": "leave the chatbot",
    "/q": "leave the chatbot",
}

# Subcommands grouped under /youtube.
YOUTUBE_SUBCOMMANDS = {
    "learn": "fetch, rewrite, chunk, and embed one video",
    "transcript": "use one transcript by hash/URL, or search titles with ag",
    "search_transcript": "search transcript titles with ag and pick a video",
}


@dataclass
class Chunk:
    """One knowledge trunk used by the BM25 index."""

    video_id: str
    index: int
    trunk_path: Path
    # Article paragraphs inherit the heading of the section they belong to.
    # The heading is indexed with the paragraph body so a question about a
    # section ("what can i eat in Izumo" -> the "Eat" section) retrieves the
    # actual paragraph, while the answer context still contains only the
    # paragraph text and never the standalone heading.
    section_heading: str = ""

    @property
    def label(self) -> str:
        return f"{self.video_id}.{self.index}"


ARTICLE_CHUNK_PREFIXES = ("news_", "article_")


def _article_heading_like(text: str) -> bool:
    """Return True when ``text`` looks like a short article section label."""
    text = " ".join(text.split())
    if not text or len(text) > 120 or len(text.split()) > 12:
        return False
    if re.match(r"^(?:\d+(?:\.\d+)*\.?|[IVXLC]+\.)\s+", text):
        return True
    return not text.endswith((".", "!", "?", ";", ","))


def annotate_section_headings(chunks: list[Chunk]) -> None:
    """Attach each learned-article paragraph to its enclosing section heading.

    BM25 otherwise ranks a one-word heading such as "Eat" above the food
    paragraphs below it, or lets repeated city mentions in travel paragraphs
    outrank the short section body that actually answers the question. The
    paragraph body remains the answer context; only the searchable document
    gains the section label.
    """
    grouped: dict[str, list[Chunk]] = {}
    for chunk in chunks:
        grouped.setdefault(chunk.video_id, []).append(chunk)
    for video_id, video_chunks in grouped.items():
        article = video_id.startswith(ARTICLE_CHUNK_PREFIXES) or any(
            chunk.trunk_path.parent == ARTICLES_DIR for chunk in video_chunks
        )
        if not article:
            continue
        heading = ""
        for chunk in sorted(video_chunks, key=lambda item: item.index):
            if chunk.index <= 0:
                continue
            text = " ".join(paragraph_text(chunk).split())
            if not text:
                continue
            if _article_heading_like(text):
                heading = text
                chunk.section_heading = ""
            else:
                chunk.section_heading = heading


class AnswerCache:
    """Small bounded LRU of complete answers for repeated questions."""

    def __init__(self, max_entries: int = 32) -> None:
        self.max_entries = max_entries
        self._entries: OrderedDict[tuple, dict] = OrderedDict()

    def get(self, key: tuple) -> dict | None:
        entry = self._entries.get(key)
        if entry is None:
            return None
        self._entries.move_to_end(key)
        return entry

    def put(
        self,
        key: tuple,
        answer: str,
        sources: list[tuple[Chunk, float]],
        context_tokens: int,
    ) -> None:
        self._entries[key] = {
            "answer": answer,
            "sources": list(sources),
            "context_tokens": context_tokens,
        }
        self._entries.move_to_end(key)
        while len(self._entries) > self.max_entries:
            self._entries.popitem(last=False)

    def clear(self) -> None:
        self._entries.clear()

    def __len__(self) -> int:
        return len(self._entries)


@dataclass(frozen=True)
class TranscriptMatch:
    """A title-file match returned by /youtube transcript and
    /youtube search_transcript."""

    video_id: str
    title: str


TOKEN_PATTERN = re.compile(r"\b\w+\b", re.UNICODE)
# Time expressions are written inconsistently in questions and transcripts:
# "3am", "3 am", "3 a.m.", "3:00am". Canonicalize them before tokenizing so
# the same time is one lexical term on both sides of the BM25 query.
TIME_EXPRESSION_PATTERN = re.compile(
    r"(?<!\w)(?P<hour>\d{1,2})(?::(?P<minute>\d{2}))?\s*"
    r"(?P<meridiem>a\.?m\.?|p\.?m\.?)(?!\w)",
    re.IGNORECASE,
)


def _normalize_time_expressions(text: str) -> str:
    """Rewrite clock times to one compact token (``3 a.m.`` -> ``3am``)."""

    def replace(match: re.Match[str]) -> str:
        minute = match.group("minute")
        meridiem = match.group("meridiem").replace(".", "").lower()
        if minute and minute != "00":
            return f"{int(match.group('hour'))}{minute}{meridiem}"
        return f"{int(match.group('hour'))}{meridiem}"

    return TIME_EXPRESSION_PATTERN.sub(replace, text)

# Words whose trailing "s" is part of the word rather than a plural marker.
# They are left untouched so that, for example, "news" does not collide with
# "new" in the BM25 index.
TOKEN_NORMALIZATION_EXCEPTIONS = frozenset(
    {
        "news",
        "series",
        "species",
        "always",
        "perhaps",
        "physics",
        "mathematics",
        "economics",
        "politics",
        "ethics",
        "athletics",
        "business",
        "process",
        "access",
        "address",
        "success",
        "press",
        "class",
        "glass",
    }
)


def normalize_token(token: str) -> str:
    """Fold common plural/inflection endings for lexical matching.

    BM25 compares whole tokens, so without this step ``book`` in a transcript
    would not match ``books`` in a question. The rules are deliberately
    conservative: they only remove plural ``-s``/``-es`` endings and keep
    every other word unchanged. Both the index and the query pass through the
    same function, so the normalization is symmetric.
    """
    if len(token) <= 3 or token in TOKEN_NORMALIZATION_EXCEPTIONS:
        return token
    if token.endswith("'s") and len(token) > 4:
        return token[:-2]
    if token.endswith("es") and len(token) >= 4:
        base = token[:-2]
        if base.endswith(("s", "x", "z", "ch", "sh", "o")):
            return base
        # Most other -es plurals keep the base and drop only the s.
        return token[:-1]
    if (
        token.endswith("s")
        and len(token) > 3
        and not token.endswith(("ss", "us", "is"))
    ):
        return token[:-1]
    return token


def tokenize(text: str) -> list[str]:
    """Lowercase, lightly normalized word-boundary tokenizer for BM25."""
    text = _normalize_time_expressions(text)
    return [
        normalize_token(token)
        for token in TOKEN_PATTERN.findall(text.lower())
    ]


@dataclass
class BM25Index:
    """Lexical index for one list of chunks, backed by SQLite FTS5.

    The postings and document text live on disk in
    ``.chat/articles_cache/bm25_index_fts.db``.  Only the vocabulary and the
    document frequency of each term are kept in Python, which is what the
    query-expansion and entity-focus checks need.
    """

    chunks: list[Chunk]
    chunk_keys: list[str]
    chunk_key_by_id: dict[int, str]
    bm25: sqlite_bm25.SQLiteBM25
    vocabulary: frozenset[str]
    # Number of documents containing each token. Used to separate a question's
    # distinctive anchors from generic words when validating query expansion.
    document_frequency: dict[str, int]
    # (first letter, length) -> vocabulary words, used for cheap fuzzy lookup.
    fuzzy_buckets: dict[tuple[str, int], list[str]]
    document_count: int
    token_count: int

    def key_for(self, chunk: Chunk) -> str:
        return self.chunk_key_by_id.get(id(chunk), "")

    def matching_keys(self, tokens, mode: str = "AND") -> set[str]:
        return self.bm25.matching_keys(tokens, mode)


# Keep a couple of indexes alive (global corpus + explicit transcript) so
# switching scopes does not force an immediate rebuild.
_BM25_CACHE: "OrderedDict[int, tuple[list[Chunk], BM25Index]]" = OrderedDict()
BM25_CACHE_ENTRIES = 2
# Acronym -> full form (or None when the corpus never defines it), memoized per
# chunk list so repeated questions do not rescan the documents.
_ACRONYM_CACHE: "OrderedDict[int, tuple[list[Chunk], dict[str, str | None]]]" = (
    OrderedDict()
)
ACRONYM_CACHE_ENTRIES = 2


def chunk_key(chunk: Chunk) -> str:
    """Return a stable key for a trunk file's row in the SQLite index."""
    path = chunk.trunk_path.resolve()
    try:
        return str(path.relative_to(CHAT_DIR.resolve()))
    except ValueError:
        # The /article cache lives under app/.article_cache rather than .chat.
        return str(path)


def _bm25_document_tokens(chunk: Chunk) -> list[str]:
    """Tokenize exactly the text the old in-memory index used for BM25."""
    text = paragraph_text(chunk)
    title = video_title(chunk.video_id)
    # Learned article paragraphs inherit their section heading, so the body of
    # the "Eat" section matches a question that uses the word "eat" even
    # though the heading itself is not a standalone answer.
    heading = getattr(chunk, "section_heading", "")
    parts = [part for part in (title, heading, text) if part]
    return tokenize("\n".join(parts))


def _bm25_corpus_key(chunks: list[Chunk], keys: list[str]) -> str:
    """Return the FTS5 corpus name for a chunk list.

    The global knowledge base gets one stable table.  A filtered global list
    (for example the paragraph-only list used by iterative retrieval) still
    covers every known video/article id, so it shares that table instead of
    duplicating 26k documents into a scope table.  A transcript, single
    article, or book gets its own table so its BM25 document count and
    average length stay correct.
    """
    chunk_ids = {chunk.video_id for chunk in chunks}
    try:
        known_ids = set(available_video_ids())
        for path in ARTICLES_DIR.glob("*.trunk"):
            match = TRUNK_FILE.fullmatch(path.name)
            if match:
                known_ids.add(match.group("video"))
    except OSError:
        known_ids = set()
    if chunk_ids and chunk_ids == known_ids:
        return "global"
    digest = hashlib.sha1(
        "\n".join(sorted(keys)).encode("utf-8")
    ).hexdigest()[:16]
    return f"scope_{digest}"


def _bm25_sync_entries(
    chunks: list[Chunk], corpus_key: str
) -> list[tuple[str, list[str], int, int]]:
    """Describe the chunks for the SQLite index, skipping unchanged files.

    Tokenizing every trunk on every cache miss is the expensive part of an
    index build.  The FTS5 database already knows each trunk's mtime and size,
    so only new or rewritten trunks are read and tokenized here.
    """
    backend = sqlite_bm25.get_index(BM25_DB_PATH, corpus_key)
    signatures = backend.file_signatures()
    entries: list[tuple[str, list[str], int, int]] = []
    for chunk in chunks:
        key = chunk_key(chunk)
        try:
            stat = chunk.trunk_path.stat()
            mtime_ns = int(stat.st_mtime_ns)
            size = int(stat.st_size)
        except OSError:
            mtime_ns = 0
            size = 0
        if signatures.get(key) == (mtime_ns, size):
            entries.append((key, [], mtime_ns, size))
        else:
            entries.append((key, _bm25_document_tokens(chunk), mtime_ns, size))
    return entries


def build_bm25_index(chunks: list[Chunk], prune: bool = False) -> BM25Index:
    """Index ``chunks`` in the on-disk FTS5 database and load its metadata."""
    keys = [chunk_key(chunk) for chunk in chunks]
    corpus_key = _bm25_corpus_key(chunks, keys)
    backend = sqlite_bm25.get_index(BM25_DB_PATH, corpus_key)
    backend.sync(_bm25_sync_entries(chunks, corpus_key))

    if prune and corpus_key == "global":
        # Never prune a transcript/article/book scope: it holds only that
        # explicit source, and stale rows are limited to the same source.
        backend.prune(keys)

    # Load vocabulary metadata only after any stale rows were removed, so the
    # in-memory document frequencies match the rows that remain on disk.
    document_frequency = backend.vocabulary()
    vocabulary = frozenset(document_frequency)
    fuzzy_buckets: dict[tuple[str, int], list[str]] = {}
    for token in vocabulary:
        if len(token) < FUZZY_MIN_TOKEN_LENGTH or not token.isalpha():
            continue
        fuzzy_buckets.setdefault((token[0], len(token)), []).append(token)

    return BM25Index(
        chunks=chunks,
        chunk_keys=keys,
        chunk_key_by_id={id(chunk): key for chunk, key in zip(chunks, keys)},
        bm25=backend,
        vocabulary=vocabulary,
        document_frequency=document_frequency,
        fuzzy_buckets=fuzzy_buckets,
        document_count=backend.document_count(),
        token_count=backend.token_count(),
    )


def index_chunks_in_sqlite(
    chunks: list[Chunk], corpus_key: str | None = None
) -> dict[str, object]:
    """Persist newly learned chunks in the SQLite BM25 index immediately.

    Used by the article learning paths so a learned article has both its trunk
    files and its search index on disk even before the next question.  News
    articles always join the global corpus; an explicit article file gets its
    own scoped table.
    """
    if not chunks:
        return {"added": 0, "updated": 0, "unchanged": 0}
    keys = [chunk_key(chunk) for chunk in chunks]
    if corpus_key is None:
        corpus_key = _bm25_corpus_key(chunks, keys)
    backend = sqlite_bm25.get_index(BM25_DB_PATH, corpus_key)
    return backend.sync(_bm25_sync_entries(chunks, corpus_key))


def get_bm25_index(chunks: list[Chunk], prune: bool = False) -> BM25Index:
    """Return a cached BM25 index for ``chunks``, building it on first use."""
    key = id(chunks)
    entry = _BM25_CACHE.get(key)
    if entry is not None and entry[0] is chunks:
        _BM25_CACHE.move_to_end(key)
        return entry[1]

    index = build_bm25_index(chunks, prune=prune)
    _BM25_CACHE[key] = (chunks, index)
    _BM25_CACHE.move_to_end(key)
    while len(_BM25_CACHE) > BM25_CACHE_ENTRIES:
        _BM25_CACHE.popitem(last=False)
    return index


def clear_bm25_cache() -> None:
    """Drop every cached BM25 index after the knowledge base changes."""
    _BM25_CACHE.clear()
    _ACRONYM_CACHE.clear()


def _acronym_phrase(phrase: str, acronym: str) -> str | None:
    """Return the suffix of ``phrase`` whose initials match ``acronym``.

    A definition sentence like "An International Driving Permit (IDP)" yields
    a leading article, so the match is allowed to start at a later word.
    """
    words = phrase.split()
    for start in range(len(words)):
        candidates = [
            word
            for word in words[start:]
            if word.lower().strip("'’&.-”) ") not in ACRONYM_STOPWORDS
        ]
        if len(candidates) < 2:
            continue
        initials = "".join(word[0] for word in candidates).upper()
        if initials == acronym:
            return " ".join(candidates)
    return None


def acronym_expansion(
    acronym: str, chunks: list[Chunk], index: BM25Index
) -> str | None:
    """Mine the full form of ``acronym`` from documents that use it.

    Only documents whose token list contains the acronym are scanned, which
    keeps the lookup cheap even on the full corpus. The result is cached per
    chunk list and invalidated by ``clear_bm25_cache``.
    """
    key = acronym.upper()
    entry = _ACRONYM_CACHE.get(id(chunks))
    if entry is None or entry[0] is not chunks:
        entry = (chunks, {})
        _ACRONYM_CACHE[id(chunks)] = entry
        _ACRONYM_CACHE.move_to_end(id(chunks))
        while len(_ACRONYM_CACHE) > ACRONYM_CACHE_ENTRIES:
            _ACRONYM_CACHE.popitem(last=False)
    memo = entry[1]
    if key in memo:
        return memo[key]

    token = key.lower()
    pattern = re.compile(
        r"(?P<phrase>(?:[A-Za-z][A-Za-z'’&.-]*\s+){0,8})"
        r"\(\s*" + re.escape(key) + r"s?\s*\)"
    )
    expansion: str | None = None
    # FTS5 finds the documents containing the acronym; the chunk order is
    # preserved so the first definition in corpus order is still chosen.
    matching = index.matching_keys([token])
    for chunk, document_key in zip(chunks, index.chunk_keys):
        if document_key not in matching:
            continue
        text = paragraph_text(chunk)
        for match in pattern.finditer(text):
            phrase = _acronym_phrase(match.group("phrase"), key)
            if phrase:
                expansion = phrase
                break
        if expansion:
            break

    memo[key] = expansion
    return expansion


def question_acronym_expansions(
    question: str, chunks: list[Chunk], index: BM25Index
) -> list[str]:
    """Return corpus-derived full forms for acronyms in ``question``."""
    expansions: list[str] = []
    seen: set[str] = set()
    for acronym in ACRONYM_QUERY_PATTERN.findall(question):
        key = acronym.upper()
        if key in seen:
            continue
        seen.add(key)
        expansion = acronym_expansion(key, chunks, index)
        if expansion:
            expansions.append(expansion)
    return expansions


def _collect_chunks(selected_video_ids: set[str]) -> tuple[list[Chunk], int]:
    """Return trunk chunks whose video id is in ``selected_video_ids``."""
    chunks: list[Chunk] = []
    filtered = 0
    paths = sorted(
        OUTPUT_DIR.glob("*.trunk"),
        key=lambda p: (p.name.split(".", 1)[0], p.name),
    )
    for path in paths:
        match = TRUNK_FILE.fullmatch(path.name)
        if not match:
            continue
        if match.group("video") not in selected_video_ids:
            filtered += 1
            continue
        chunks.append(
            Chunk(
                video_id=match.group("video"),
                index=int(match.group("index")),
                trunk_path=path,
            )
        )
    chunks.sort(key=lambda chunk: (chunk.video_id, chunk.index))
    return chunks, filtered


def load_chunks(
    video_ids: set[str] | None = None,
    announce: bool = True,
) -> list[Chunk]:
    """Load the global trunk corpus, or an explicit set of video ids.

    When ``video_ids`` is given (the /youtube transcript command), only those
    videos are loaded. No embedding files are read; BM25 indexes the trunk
    text lazily on the first question. Set ``announce`` to False when the
    caller wants to print one combined transcript + news summary.
    """
    if not OUTPUT_DIR.is_dir():
        sys.exit(f"Transcript directory not found: {OUTPUT_DIR}")

    if video_ids is not None:
        selected_video_ids = set(video_ids)
        scope = "transcript " + ", ".join(sorted(selected_video_ids))
        outside_scope = "from other transcripts"
    else:
        selected_video_ids = set(available_video_ids())
        scope = "the global corpus"
        outside_scope = "outside the global corpus"

    chunks, filtered = _collect_chunks(selected_video_ids)
    if not chunks:
        sys.exit(
            f"No trunk files found for {scope} in {OUTPUT_DIR}\n"
            "Run: python3 youtube_video_learn.py <youtube_video_hash>"
        )

    details = []
    if filtered and video_ids is not None:
        details.append(f"filtered {filtered} trunk(s) {outside_scope}")
    suffix = f" ({', '.join(details)})" if details else ""
    if announce:
        print(f"Loaded {len(chunks):,} trunks from "
              f"{len(selected_video_ids):,} video(s) for {scope}{suffix}.")
    return chunks


def load_news_chunks(news_ids: set[str] | None = None) -> list[Chunk]:
    """Load learned article trunks, optionally limited to specific article ids."""
    if not ARTICLES_DIR.is_dir():
        return []
    selected = set(news_ids) if news_ids is not None else None
    chunks: list[Chunk] = []
    try:
        paths = sorted(
            ARTICLES_DIR.glob("*.trunk"),
            key=lambda p: (p.name.split(".", 1)[0], p.name),
        )
    except OSError:
        return []
    for path in paths:
        match = TRUNK_FILE.fullmatch(path.name)
        if not match:
            continue
        news_id = match.group("video")
        if selected is not None and news_id not in selected:
            continue
        chunks.append(
            Chunk(
                video_id=news_id,
                index=int(match.group("index")),
                trunk_path=path,
            )
        )
    chunks.sort(key=lambda chunk: (chunk.video_id, chunk.index))
    annotate_section_headings(chunks)
    return chunks


def load_all_chunks() -> list[Chunk]:
    """Load every learned transcript and article trunk into one global corpus."""
    chunks: list[Chunk] = []
    if OUTPUT_DIR.is_dir() and available_video_ids():
        chunks = load_chunks(announce=False)
    news_chunks = load_news_chunks()
    if not chunks and not news_chunks:
        sys.exit(
            "No learned trunks found in "
            f"{OUTPUT_DIR} or {ARTICLES_DIR}\n"
            "Learn a YouTube video with /youtube learn or an article "
            "from its /news detail view."
        )
    all_chunks = chunks + news_chunks
    video_count = len({chunk.video_id for chunk in chunks})
    news_count = len({chunk.video_id for chunk in news_chunks})
    source_summary = f"{video_count:,} video(s)"
    if news_count:
        source_summary += f" and {news_count:,} article(s)"
    print(
        f"Loaded {len(all_chunks):,} trunks from {source_summary} "
        "for the global corpus."
    )
    return all_chunks


# Backwards-compatible names for callers that only need the loaded chunks.
load_embeddings = load_chunks


def format_bytes(num: float) -> str:
    """Format a byte count for the memory report."""
    value = float(num)
    for unit in ("B", "KiB", "MiB", "GiB", "TiB"):
        if abs(value) < 1024.0 or unit == "TiB":
            return f"{value:.1f} {unit}"
        value /= 1024.0
    return f"{value:.1f} TiB"


def index_memory_report(
    chunks: list[Chunk],
    build_index: bool = True,
    article: str | None = None,
) -> str:
    """Describe the loaded trunks and the on-disk BM25 index.

    When ``article`` is set the active scope is a single article file rather
    than a set of YouTube transcripts, so the source count is labelled
    ``Article(s)`` instead of ``video(s)``.
    """
    if not chunks:
        return "BM25 index: no trunks loaded."

    video_ids = {chunk.video_id for chunk in chunks}
    source_label = "Article(s)" if article is not None else "video(s)"
    lines = [
        f"BM25 index: {len(chunks):,} trunk(s) from "
        f"{len(video_ids):,} {source_label}"
    ]
    if build_index:
        index = get_bm25_index(chunks)
        token_count = index.token_count
        lines.append(
            f"  {token_count:,} tokenized terms (SQLite FTS5 on disk at "
            f"{BM25_DB_PATH}, no embedding vectors)"
        )
    else:
        lines.append("  index: built lazily on the first question")

    try:
        import resource

        rss = resource.getrusage(resource.RUSAGE_SELF).ru_maxrss
        if sys.platform != "darwin":  # Linux reports KiB, macOS reports bytes
            rss *= 1024
        lines.append(f"  process peak RSS: {format_bytes(rss)}")
    except (ImportError, OSError):  # pragma: no cover - non-Unix platforms
        pass
    return "\n".join(lines)


# Backwards-compatible name for existing callers.
embedding_memory_report = index_memory_report


def chat_status_report(
    transcript: str | None,
    chunks: list[Chunk],
    depth: int | None,
    max_context_tokens: int = MAX_CONTEXT_TOKENS,
    cache_entries: int = 0,
    article: str | None = None,
) -> str:
    """Describe the chatbot's current configuration and loaded data."""
    all_ids = {chunk.video_id for chunk in chunks}
    news_ids = {video_id for video_id in all_ids if video_id.startswith("news_")}
    video_ids = all_ids - news_ids
    if article is not None:
        scope = f"single article {article!r}"
    elif transcript is not None:
        scope = f"single transcript {transcript!r}"
    else:
        scope = "global corpus (all learned trunks)"

    if depth is None:
        budget = (
            "disabled"
            if max_context_tokens <= 0
            else f"{max_context_tokens:,} estimated tokens"
        )
        retrieval = f"auto (BM25 score elbow, budget: {budget})"
    else:
        suffix = " (default)" if depth == DEFAULT_DEPTH else ""
        retrieval = f"top {depth} matching trunk(s){suffix} by BM25"

    if article is not None:
        loaded_label = f"{len(video_ids):,} Article(s)"
    elif news_ids:
        loaded_label = (
            f"{len(video_ids):,} video(s) and "
            f"{len(news_ids):,} article(s)"
        )
    else:
        loaded_label = f"{len(video_ids):,} video(s)"

    lines = [
        f"  models:      {CHAT_MODEL} (chat), {EXPANSION_MODEL} "
        "(query expansion), "
        f"{FILTER_MODEL} (candidate filter)",
        f"  Ollama host: {OLLAMA_HOST}",
        f"  scope:       {scope}",
        f"  loaded:      {len(chunks):,} trunks from {loaded_label}",
        f"  retrieval:   {retrieval}",
        "  filter:      small-LLM question relevance filter",
        f"  answer cache: {cache_entries} entr{'y' if cache_entries == 1 else 'ies'}",
        f"  history:     {history_count(HISTORY_FILE):,} saved entries",
    ]
    return "\n".join(lines)


class CommandCompleter(Completer):
    """Complete chatbot commands when the first word starts with '/'."""

    def get_completions(self, document, complete_event):
        text = document.text_before_cursor
        if not text.startswith("/"):
            return
        # Commands are always the first word. Once an argument is typed,
        # /youtube and /youtube transcript get completions.
        if any(ch.isspace() for ch in text):
            name, _, argument = text.partition(" ")
            prefix = argument.lstrip()
            if name.lower() == "/youtube":
                subcommand, separator, subargument = prefix.partition(" ")
                if separator:
                    if subcommand.lower() in {
                        "transcript",
                        "search_transcript",
                    }:
                        video_prefix = subargument.lstrip()
                        for video_id in available_video_ids():
                            if video_id.startswith(video_prefix):
                                yield Completion(
                                    video_id,
                                    start_position=-len(video_prefix),
                                )
                    return
                for subcommand, description in YOUTUBE_SUBCOMMANDS.items():
                    if subcommand.startswith(prefix.lower()):
                        yield Completion(
                            subcommand,
                            start_position=-len(prefix),
                            display=subcommand,
                            display_meta=description,
                        )
            return
        for command in sorted(COMMANDS):
            if command.startswith(text):
                yield Completion(
                    command,
                    start_position=-len(text),
                    display=command,
                    display_meta=COMMANDS[command],
                )


def available_video_ids() -> list[str]:
    """Return video ids that have trunk files in ./.chat/youtube_transcripts."""
    try:
        entries = list(OUTPUT_DIR.glob("*.trunk"))
    except OSError:
        return []
    video_ids = set()
    for entry in entries:
        match = TRUNK_FILE.fullmatch(entry.name)
        if match:
            video_ids.add(match.group("video"))
    return sorted(video_ids)


class VideoCompleter(Completer):
    """Tab-complete video ids that have trunks."""

    def get_completions(self, document, complete_event):
        prefix = document.text_before_cursor
        for video_id in available_video_ids():
            if video_id.startswith(prefix):
                yield Completion(video_id, start_position=-len(prefix))


class TranscriptMatchCompleter(Completer):
    """Tab-complete video ids from the current /youtube transcript matches."""

    def __init__(self, matches: list[TranscriptMatch]) -> None:
        self.matches = matches

    def get_completions(self, document, complete_event):
        prefix = document.text_before_cursor.strip()
        for match in self.matches:
            if match.video_id.startswith(prefix):
                yield Completion(
                    match.video_id,
                    start_position=-len(prefix),
                    display=f"{match.video_id}  {match.title}",
                    display_meta=match.title,
                )


def prompt_for_video_id() -> str:
    """Prompt for a transcript video id, tab-completing available ones."""
    video_ids = available_video_ids()
    if not video_ids:
        sys.exit(f"No transcripts with trunks found in {OUTPUT_DIR}")

    print(f"{len(video_ids)} transcripts available (Tab to complete).")
    session = PromptSession(
        completer=VideoCompleter(),
        complete_while_typing=True,
        multiline=False,
    )
    while True:
        try:
            video_id = session.prompt("Transcript: ").strip()
        except (EOFError, KeyboardInterrupt):
            print()
            sys.exit("No transcript selected.")
        if not video_id:
            continue
        if video_id in video_ids:
            return video_id
        print(
            f"Unknown transcript: {video_id!r}. "
            "Type a video id with trunks, or use /youtube learn first."
        )


class PlainFileHistory(History):
    """One chat entry per line in ./.chat/question_history.txt.

    Both questions and slash commands are stored so Up/Down can navigate
    back and forth through the complete session history.
    """

    def __init__(self, path: Path) -> None:
        super().__init__()
        self.path = path
        try:
            self.path.parent.mkdir(parents=True, exist_ok=True)
            self.path.touch(exist_ok=True)
        except OSError as exc:
            print(f"Warning: cannot create {self.path}: {exc}", file=sys.stderr)

    def append_string(self, string: str) -> None:
        if not string.strip():
            return
        super().append_string(string)

    def load_history_strings(self):
        """Yield the most recent entry first, as prompt_toolkit expects."""
        try:
            lines = self.path.read_text(
                encoding="utf-8", errors="replace"
            ).splitlines()
        except OSError:
            return
        for line in reversed(lines):
            if line.strip():
                yield line

    def store_string(self, string: str) -> None:
        try:
            with self.path.open("a", encoding="utf-8") as handle:
                handle.write(string.replace("\n", " ") + "\n")
        except OSError as exc:
            print(
                f"Warning: cannot append to {self.path}: {exc}", file=sys.stderr
            )


def make_prompt_session(history: PlainFileHistory) -> PromptSession:
    """Create a vi-mode prompt with history and a mode-aware toolbar."""
    bindings = KeyBindings()

    @bindings.add("up", filter=vi_insert_mode)
    def _history_up(event) -> None:
        event.current_buffer.auto_up(count=event.arg)

    @bindings.add("down", filter=vi_insert_mode)
    def _history_down(event) -> None:
        event.current_buffer.auto_down(count=event.arg)

    @bindings.add("tab")
    def _complete(event) -> None:
        """Tab completes commands; repeated Tab cycles the matches."""
        buffer = event.current_buffer
        if buffer.complete_state:
            buffer.complete_next()
        else:
            buffer.start_completion(select_first=False)

    def bottom_toolbar() -> str:
        app = get_app()
        buffer = app.current_buffer
        if buffer.selection_state is not None:
            mode = (
                "VISUAL-LINE"
                if buffer.selection_state.type.name == "LINES"
                else "VISUAL"
            )
        elif app.vi_state.input_mode in {
            InputMode.INSERT,
            InputMode.INSERT_MULTIPLE,
            InputMode.REPLACE,
            InputMode.REPLACE_SINGLE,
        }:
            mode = "INSERT"
        else:
            mode = "NORMAL"
        return (
            f" {mode}  |  Esc normal  i insert  "
            "v/V visual  dd/dw delete  u undo  Tab complete  Up/Down history "
        )

    session = PromptSession(
        history=history,
        vi_mode=True,
        key_bindings=bindings,
        completer=CommandCompleter(),
        bottom_toolbar=bottom_toolbar,
        complete_while_typing=False,
        multiline=False,
    )

    # After typing a command that takes an argument, append a space so the
    # word can be entered immediately. This uses the insert event (not the
    # text-changed event) so deleting characters never triggers it again.
    def _auto_space(buffer) -> None:
        if buffer.text in {
            "/youtube",
            "/youtube learn",
            "/youtube transcript",
            "/youtube search_transcript",
            "/wiki",
            "/wiki_article",
            "/dict",
            "/depth",
            "/benchmark",
        }:
            buffer.cursor_position = len(buffer.text)
            buffer.insert_text(" ", fire_event=False)

    session.default_buffer.on_text_insert += _auto_space
    return session


def history_count(path: Path) -> int:
    try:
        return sum(
            1
            for line in path.read_text(
                encoding="utf-8", errors="replace"
            ).splitlines()
            if line.strip()
        )
    except OSError:
        return 0


@lru_cache(maxsize=8192)
def _read_paragraph_cached(path: str) -> str:
    try:
        content = Path(path).read_text(encoding="utf-8")
    except OSError:
        return ""
    match = PARAGRAPH_PATTERN.search(content)
    return match.group(1).strip() if match else content.strip()


def paragraph_text(chunk: Chunk) -> str:
    """Return only the paragraph body of a trunk, excluding its shared title."""
    return _read_paragraph_cached(str(chunk.trunk_path))


@lru_cache(maxsize=4096)
def _read_title_cached(path: str) -> str:
    try:
        return Path(path).read_text(encoding="utf-8", errors="replace").strip()
    except OSError:
        return ""


def video_title(video_id: str) -> str:
    """Return the saved title for a video or learned article, if one exists."""
    title = _read_title_cached(str(OUTPUT_DIR / f"{video_id}.title"))
    if title:
        return title
    return _read_title_cached(str(ARTICLES_DIR / f"{video_id}.title"))


def _token_set(text: str) -> frozenset[str]:
    return frozenset(tokenize(text))


def _jaccard(left: frozenset[str], right: frozenset[str]) -> float:
    if not left or not right:
        return 0.0
    union = len(left | right)
    return len(left & right) / union if union else 0.0


def _select_auto_chunks(
    ranked: list[tuple[Chunk, float]],
    fallback_top_n: int,
    max_context_tokens: int | None,
) -> list[tuple[Chunk, float]]:
    """Apply BM25 score elbow, diversity, and token-budget rules."""
    if not ranked:
        return []

    candidates = ranked[:AUTO_CANDIDATE_LIMIT]
    min_chunks = min(fallback_top_n, len(candidates))
    top_score = candidates[0][1]
    score_floor = max(0.0, top_score * SCORE_FLOOR_RATIO)
    selected: list[tuple[Chunk, float]] = []
    selected_token_sets: list[frozenset[str]] = []
    per_video: dict[str, int] = {}
    estimated_tokens = 0
    previous_score: float | None = None

    for chunk, score in candidates:
        if len(selected) >= min_chunks:
            # Stop once scores fall off sharply relative to the best match.
            if score < score_floor:
                break
            if (
                previous_score is not None
                and previous_score > 0.0
                and (previous_score - score) / previous_score
                > SCORE_ELBOW_RATIO
            ):
                break

        if per_video.get(chunk.video_id, 0) >= MAX_CHUNKS_PER_VIDEO:
            previous_score = score
            continue

        if selected_token_sets:
            # Remove near-duplicates rather than spending prompt tokens twice.
            token_set = _token_set(paragraph_text(chunk))
            if any(
                _jaccard(token_set, existing) >= NEAR_DUPLICATE_JACCARD
                for existing in selected_token_sets
            ):
                previous_score = score
                continue
        else:
            token_set = _token_set(paragraph_text(chunk))

        chunk_tokens = estimate_tokens(paragraph_text(chunk)) + 16
        if (
            max_context_tokens is not None
            and max_context_tokens > 0
            and len(selected) >= min_chunks
            and estimated_tokens + chunk_tokens > max_context_tokens
        ):
            break

        selected.append((chunk, score))
        selected_token_sets.append(token_set)
        per_video[chunk.video_id] = per_video.get(chunk.video_id, 0) + 1
        estimated_tokens += chunk_tokens
        previous_score = score

        if (
            max_context_tokens is not None
            and max_context_tokens > 0
            and estimated_tokens >= max_context_tokens
        ):
            break

    # Never return an empty context, even when the first chunk is huge.
    if not selected:
        selected = [ranked[0]]
    return selected


def fuzzy_query_terms(query_tokens: list[str], index: BM25Index) -> list[str]:
    """Return vocabulary spellings for out-of-vocabulary query tokens.

    Only tokens that do not appear in the index vocabulary are corrected, so
    correctly spelled words never pull in unrelated neighbours. Candidates
    are restricted to the same first letter and a similar length, then ranked
    by string similarity *and* by how often they co-occur with the other
    meaningful query terms. The co-occurrence signal matters for names:
    ``terrance`` is closer to ``terrace`` than to ``terence``, but only
    ``terence`` appears alongside ``tao`` in the corpus. The resulting
    correction is added to the BM25 query at full weight because the
    misspelled original contributes no matches at all.
    """
    if not FUZZY_MATCH_ENABLED or not index.fuzzy_buckets:
        return []

    known = set(query_tokens)
    processed: set[str] = set()
    corrections: list[str] = []
    anchors = {
        token
        for token in query_tokens
        if len(token) >= 3
        and token in index.vocabulary
        and token not in FUZZY_ANCHOR_STOPWORDS
    }
    def cooccurrence_support(candidate: str) -> int:
        """Count documents containing the candidate and another query term."""
        if not anchors:
            return 0
        return index.bm25.count_cooccurrence(candidate, sorted(anchors))

    for token in query_tokens:
        if token in processed:
            continue
        processed.add(token)
        if (
            token in index.vocabulary
            or len(token) < FUZZY_MIN_TOKEN_LENGTH
            or not token.isalpha()
        ):
            continue
        candidates: list[str] = []
        for length in range(
            len(token) - FUZZY_LENGTH_DELTA,
            len(token) + FUZZY_LENGTH_DELTA + 1,
        ):
            candidates.extend(index.fuzzy_buckets.get((token[0], length), ()))
        if not candidates:
            continue
        close_matches = difflib.get_close_matches(
            token,
            candidates,
            n=len(candidates),
            cutoff=FUZZY_SCORE_CUTOFF,
        )
        if not close_matches:
            continue
        scored = [
            (
                cooccurrence_support(match),
                difflib.SequenceMatcher(None, token, match).ratio(),
                match,
            )
            for match in close_matches
        ]
        best_support = max(item[0] for item in scored)
        if best_support > 0:
            # A candidate that actually appears next to the rest of the query
            # is a much better correction than a marginally closer spelling.
            scored = [item for item in scored if item[0] == best_support]
        scored.sort(key=lambda item: (item[0], item[1]), reverse=True)
        for _support, _ratio, match in scored[:FUZZY_MAX_MATCHES]:
            if match not in known:
                corrections.append(match)
                known.add(match)
    return corrections


def fuzzy_entity_terms(
    query_tokens: list[str],
    index: BM25Index,
    corrections: list[str],
) -> list[frozenset[str]]:
    """Return strongly co-occurring query words grouped into entity phrases.

    Adjacent query words whose documents almost always contain both words are
    treated as an entity phrase. For example, in the corpus ``terence`` and
    ``tao`` occur together in 25 of 25 documents, while ``new`` and ``book``
    almost never do. Each returned phrase groups words that belong to the same
    entity; the caller uses :func:`entity_focus_terms` to decide which of
    those words are distinctive enough to identify it.
    """
    if not query_tokens or not index.document_count:
        return []

    known = set(index.vocabulary)
    effective: list[list[str]] = []
    for token in query_tokens:
        if token in known:
            effective.append([token])
        elif corrections:
            # An out-of-vocabulary token may have a corrected spelling. Try
            # every correction so a misspelled name still forms its phrase.
            effective.append(corrections)
        else:
            effective.append([token])

    # Document frequencies and pairwise co-occurrence come from the FTS5
    # vocabulary/index instead of a materialized set of every document's
    # tokens.
    document_frequency: dict[str, int] = {}
    pair_frequency: dict[tuple[str, str], int] = {}

    def df(token: str) -> int:
        if token not in document_frequency:
            document_frequency[token] = index.document_frequency.get(token, 0)
        return document_frequency[token]

    def cooccurrence(left: str, right: str) -> int:
        key = (left, right) if left <= right else (right, left)
        if key not in pair_frequency:
            a, b = key
            pair_frequency[key] = index.bm25.count_matching([a, b], "AND")
        return pair_frequency[key]

    entity_phrases: list[frozenset[str]] = []
    seen_phrases: set[frozenset[str]] = set()
    for position, words in enumerate(effective):
        for neighbour_position in (position - 1, position + 1):
            if not 0 <= neighbour_position < len(effective):
                continue
            for left in words:
                for right in effective[neighbour_position]:
                    if left == right:
                        continue
                    if (
                        len(left) < 3
                        or len(right) < 3
                        or left in FUZZY_ANCHOR_STOPWORDS
                        or right in FUZZY_ANCHOR_STOPWORDS
                    ):
                        continue
                    left_df, right_df = df(left), df(right)
                    if not left_df or not right_df:
                        continue
                    together = cooccurrence(left, right)
                    ratio = together / min(left_df, right_df)
                    if (
                        together >= FUZZY_ENTITY_MIN_COOCCURRENCE
                        and ratio >= FUZZY_ENTITY_COOCCURRENCE_RATIO
                    ):
                        phrase = frozenset((left, right))
                        if phrase not in seen_phrases:
                            seen_phrases.add(phrase)
                            entity_phrases.append(phrase)
    return entity_phrases


def entity_focus_terms(
    entity_phrases: list[frozenset[str]], index: BM25Index
) -> tuple[list[frozenset[str]], set[str]]:
    """Split entity phrases into complete-phrase and distinctive-term filters.

    A generic head noun such as ``market`` occurs in hundreds of chunks, while
    the modifier in ``Nijo Market`` occurs in only a few. Such a phrase must
    match completely: requiring both ``nijo`` and ``market`` keeps out chunks
    about other markets *and* chunks about Nijo Castle. When every word in a
    phrase is similarly distinctive (for example ``terence`` and ``tao``), any
    one word is enough, so a chunk using an alias such as ``Terry Tao`` can
    still match through the shared surname.

    Returns ``(strict_phrases, loose_tokens)`` where each strict phrase must
    occur in full and a loose token may match on its own.
    """
    if not entity_phrases or not index.document_count:
        return [], set()

    frequencies: dict[str, int] = {}

    def df(token: str) -> int:
        if token not in frequencies:
            frequencies[token] = index.document_frequency.get(token, 0)
        return frequencies[token]

    strict_phrases: list[frozenset[str]] = []
    loose_tokens: set[str] = set()
    for phrase in entity_phrases:
        phrase_frequencies = {token: df(token) for token in phrase}
        rarest = min(phrase_frequencies.values())
        if rarest <= 0:
            continue
        if max(phrase_frequencies.values()) >= (
            rarest * FUZZY_ENTITY_GENERIC_DF_RATIO
        ):
            strict_phrases.append(phrase)
        else:
            loose_tokens.update(phrase)
    return strict_phrases, loose_tokens


# A small model can occasionally answer the expansion prompt with an unrelated
# topic (for example turning "3am in Tokyo" into "timezone utc offset"). Those
# terms then dominate BM25 and push the real paragraphs out of the candidate
# list. An expansion term is accepted only when a meaningful share of its
# corpus occurrences are next to a distinctive word from the original
# question; generic words such as "time" therefore do not qualify while
# genuinely related terms such as "nightlife" do. The cache is keyed by corpus
# identity so repeated iterative rounds do not rescan the documents.
_EXPANSION_FILTER_CACHE: "OrderedDict[tuple, tuple[list[Chunk], str]]" = (
    OrderedDict()
)
EXPANSION_FILTER_CACHE_ENTRIES = 512
# Minimum share of an expansion term's documents that must also contain a
# question anchor. Below this ratio the association is likely incidental.
EXPANSION_ANCHOR_RATIO = 0.35


def _question_anchor_terms(
    question_tokens: list[str], index: BM25Index
) -> list[str]:
    """Return the distinctive, in-vocabulary words that anchor a question.

    Generic question words are ignored. Among the remaining words, prefer the
    rarest ones: they identify the entity or topic the question is about and
    therefore provide a reliable co-occurrence test for expansion terms. When
    every word is common (for example in a one-video corpus), fall back to the
    rarest available word so there is still one anchor.
    """
    candidates = {
        token
        for token in question_tokens
        if len(token) > 1
        and token in index.vocabulary
        and token not in FUZZY_ANCHOR_STOPWORDS
        and not token.isdigit()
    }
    if not candidates:
        return []
    ranked = sorted(
        candidates, key=lambda token: index.document_frequency.get(token, 0)
    )
    document_count = max(1, index.document_count)
    cutoff = max(2, int(document_count * 0.10))
    anchors = [
        token
        for token in ranked
        if index.document_frequency.get(token, 0) <= cutoff
    ]
    return anchors or ranked[:1]


def _filtered_expansion_terms(
    question_tokens: list[str], expansion: str, index: BM25Index
) -> list[str]:
    """Drop query-expansion terms that drifted away from the question.

    An expansion term is accepted when a sufficient share of the documents
    containing it also contain an anchor from the original question. This is a
    generic guard against small-model hallucination; it does not require the
    expansion to be a synonym, only that the corpus itself supports the
    association. Duplicate terms are removed so that a model repeating a
    phrase cannot outweigh the user's own words in the BM25 query.
    """
    expansion_tokens = list(dict.fromkeys(tokenize(expansion)))
    if not expansion_tokens:
        return []
    anchors = _question_anchor_terms(question_tokens, index)
    if not anchors:
        # The question has no in-vocabulary anchor at all. Expansion is then
        # the only usable signal, so leave it unfiltered rather than dropping
        # every term.
        return expansion_tokens

    cache_key = (id(index.chunks), tuple(question_tokens), expansion)
    cached = _EXPANSION_FILTER_CACHE.get(cache_key)
    if cached is not None and cached[0] is index.chunks:
        _EXPANSION_FILTER_CACHE.move_to_end(cache_key)
        return tokenize(cached[1])

    anchor_set = set(anchors)
    kept: list[str] = []
    for token in expansion_tokens:
        # The FTS5 index counts, for each expansion term, how many of its
        # documents also contain one of the question's anchors.
        cooccurrence = index.bm25.count_cooccurrence(token, sorted(anchor_set))
        if not cooccurrence:
            continue
        frequency = index.document_frequency.get(token, 0)
        if frequency and cooccurrence / frequency >= EXPANSION_ANCHOR_RATIO:
            kept.append(token)

    filtered = " ".join(kept)
    _EXPANSION_FILTER_CACHE[cache_key] = (index.chunks, filtered)
    _EXPANSION_FILTER_CACHE.move_to_end(cache_key)
    while len(_EXPANSION_FILTER_CACHE) > EXPANSION_FILTER_CACHE_ENTRIES:
        _EXPANSION_FILTER_CACHE.popitem(last=False)
    return kept


def top_chunks(
    question: str,
    chunks: list[Chunk],
    limit: int | None = None,
    max_context_tokens: int | None = None,
    expansion: str = "",
) -> list[tuple[Chunk, float]]:
    """Return (chunk, BM25 score) pairs, best first.

    The BM25 query is the original question tokens (weighted twice) plus the
    expanded terms produced by the small expansion model, plus fuzzy
    corrections for question words that do not exist in the index vocabulary
    (for example a misspelled person or place name). An explicit ``limit``
    (the /depth command) returns the top ``limit`` chunks. When ``limit`` is
    None, adaptive retrieval applies a score elbow, per-video cap,
    near-duplicate removal, and token budget.
    """
    if not chunks:
        return []

    index = get_bm25_index(chunks)
    question_tokens = tokenize(question)
    corrections = fuzzy_query_terms(question_tokens, index)
    acronym_terms = tokenize(
        " ".join(question_acronym_expansions(question, chunks, index))
    )
    query_tokens = (
        question_tokens * 2
        + corrections * 2
        + acronym_terms * 2
        + _filtered_expansion_terms(question_tokens, expansion, index)
    )
    if not query_tokens:
        return []

    scores = index.bm25.get_scores(query_tokens, index.chunk_keys)
    ranked = [
        (chunk, float(score))
        for chunk, score in zip(chunks, scores)
        if score > 0.0
    ]
    if ranked:
        ranked.sort(key=lambda item: item[1], reverse=True)
    else:
        # BM25's idf is negative when a term appears in more than half of the
        # documents. In a very small corpus (for example one learned news
        # story with three paragraphs) every query term can be that common,
        # so all scores are negative even though the paragraphs clearly match.
        # Fall back to the chunks that contain a distinctive query term.
        meaningful = {
            token
            for token in query_tokens
            if len(token) > 1 and token not in FUZZY_ANCHOR_STOPWORDS
        }
        fallback = []
        for chunk, score in zip(chunks, scores):
            # The BM25 document repeats the shared title, so count the
            # fallback overlap against the paragraph body itself.
            tokens = set(tokenize(paragraph_text(chunk)))
            overlap = meaningful.intersection(tokens)
            if overlap:
                fallback.append((len(overlap), float(score), chunk))
        # More distinct query terms first; BM25 score breaks ties. A raw
        # negative-score sort would prefer a paragraph that contains only one
        # common term over a paragraph that contains the actual topic.
        fallback.sort(key=lambda item: (-item[0], -item[1]))
        ranked = [(chunk, score) for _overlap, score, chunk in fallback]
    if not ranked:
        return []

    entity_phrases = fuzzy_entity_terms(question_tokens, index, corrections)
    if entity_phrases:
        # When the question names an entity, keep only chunks that match its
        # focus filter. For a phrase with a generic head noun ("Nijo Market"),
        # the complete phrase is required, which stops a chunk about a Fukuoka
        # seafood market from entering the context merely because it contains
        # "market". For equally distinctive names ("Terence Tao"), either name
        # is enough, so alias spellings remain retrievable. This is a soft
        # constraint: if nothing matches (for example the name is not in the
        # corpus), leave the BM25 ranking untouched so retrieval can
        # still reconsider the question.
        strict_phrases, loose_tokens = entity_focus_terms(
            entity_phrases, index
        )
        allowed_keys: set[str] = set()
        for phrase in strict_phrases:
            # Each strict phrase must occur in full; the FTS5 MATCH query does
            # the intersection on disk instead of materializing every
            # document's token set in Python.
            allowed_keys |= index.matching_keys(sorted(phrase), "AND")
        if loose_tokens:
            allowed_keys |= index.matching_keys(sorted(loose_tokens), "OR")
        focused = [
            (chunk, score)
            for chunk, score in ranked
            if index.key_for(chunk) in allowed_keys
        ]
        if focused:
            # Within the entity's chunks, prefer those that also mention the
            # question's other content word(s). For "new books by Tao" this
            # keeps the chunk about Tao's book rather than every chunk that
            # merely contains his name. The topic filter is a fallback: if it
            # would remove everything, keep the entity-only matches.
            entity_tokens = set().union(*entity_phrases)
            topic_terms = {
                token
                for token in question_tokens
                if len(token) >= FUZZY_ENTITY_TOPIC_MIN_TERM_LENGTH
                and not token.isdigit()
                and token in index.vocabulary
                and token not in FUZZY_ENTITY_TOPIC_STOPWORDS
                and token not in entity_tokens
            }
            if topic_terms:
                topic_keys = index.matching_keys(
                    sorted(topic_terms), "OR"
                )
                topical = [
                    (chunk, score)
                    for chunk, score in focused
                    if index.key_for(chunk) in topic_keys
                ]
                # A single generic or incidental word must not collapse a
                # large entity set into a couple of chunks. Keep the topical
                # subset only when it still represents the entity evidence.
                min_topic_matches = min(
                    FUZZY_ENTITY_TOPIC_MIN_MATCHES, len(focused)
                )
                if len(topical) >= min_topic_matches:
                    focused = topical
            ranked = focused

    if limit is not None:
        return ranked[: max(1, limit)]
    return _select_auto_chunks(ranked, FALLBACK_TOP_N, max_context_tokens)


def build_context(chunks: list[Chunk], start_index: int = 1) -> str:
    """Build numbered <chunk> blocks from the selected paragraphs.

    Chunk ids are internal retrieval metadata used to count and organize the
    context; the answer prompt explicitly forbids the model from exposing
    them in its response. Each video's title is emitted once, inside the
    first chunk from that video, to avoid repeating boilerplate.
    """
    parts: list[str] = []
    titled_videos: set[str] = set()
    chunk_id = start_index
    for chunk in chunks:
        text = paragraph_text(chunk)
        if not text:
            continue
        title_line = ""
        if chunk.video_id not in titled_videos:
            title = video_title(chunk.video_id)
            if title:
                title_line = f"[Video: {title}]\n"
            titled_videos.add(chunk.video_id)
        parts.append(
            f'<chunk id="{chunk_id}">\n{title_line}{text}\n</chunk>'
        )
        chunk_id += 1
    return "\n".join(parts)


def estimate_tokens(text: str) -> int:
    """Return a rough token count for ``text`` without a tokenizer.

    ASCII text averages roughly four characters per token, while each CJK
    character is usually its own token. This is intentionally an estimate;
    it is used only for the context-size report.
    """
    ascii_chars = sum(1 for char in text if ord(char) < 128)
    other_chars = len(text) - ascii_chars
    return (ascii_chars + CHARS_PER_TOKEN - 1) // CHARS_PER_TOKEN + other_chars


def contains_simplified_chinese(text: str) -> bool:
    """Return True when ``text`` looks like Simplified Chinese.

    Japanese text (which uses kana), English-only text, and text that leans
    Traditional Chinese (more traditional-only than simplified-only
    characters) are excluded. Text made only of shared Han characters, such
    as ``你好``, is treated as Simplified Chinese.
    """
    if KANA_PATTERN.search(text) or not HAN_PATTERN.search(text):
        return False
    simplified = sum(char in SIMPLIFIED_ONLY_CHARS for char in text)
    traditional = sum(char in TRADITIONAL_ONLY_CHARS for char in text)
    return simplified >= traditional


def clean_translation(text: str) -> str:
    """Strip model chatter, thinking blocks, labels, and wrapping quotes."""
    text = THINK_PATTERN.sub("", text).strip()
    text = re.sub(
        r"^(?:translation|english|simplified chinese|简体中文|中文)\s*[:：]\s*",
        "",
        text,
        flags=re.IGNORECASE,
    )
    text = text.strip()
    if len(text) >= 2 and text[0] in '"“' and text[-1] in '"”':
        text = text[1:-1].strip()
    return text


def generate_text(
    prompt: str,
    model: str = TRANSLATION_MODEL,
    temperature: float = 0.1,
    num_predict: int | None = None,
    num_ctx: int = 8192,
    stop: list[str] | None = None,
) -> str:
    """Send a prompt to Ollama and return the complete (non-streamed) reply."""
    options: dict = {
        "temperature": temperature,
        "num_ctx": num_ctx,
    }
    if num_predict is not None:
        options["num_predict"] = num_predict
    if stop:
        options["stop"] = stop
    payload = {
        "model": model,
        "prompt": prompt,
        "stream": False,
        "think": False,
        "keep_alive": OLLAMA_KEEP_ALIVE,
        "options": options,
    }
    try:
        response = requests.post(
            f"{OLLAMA_HOST}/api/generate",
            json=payload,
            timeout=(10, 300),
        )
        response.raise_for_status()
        data = response.json()
    except requests.RequestException as exc:
        raise RuntimeError(
            f"Could not reach Ollama at {OLLAMA_HOST}. Is it running? ({exc})"
        ) from exc

    if data.get("error"):
        raise RuntimeError(f"Ollama error: {data['error']}")
    return str(data.get("response", ""))


def translate_to_english(text: str, model: str = TRANSLATION_MODEL) -> str:
    """Translate a Simplified Chinese question into English."""
    translation = clean_translation(
        generate_text(
            QUESTION_TRANSLATION_PROMPT.format(question=text), model=model
        )
    )
    if not translation:
        raise RuntimeError("The translation model returned an empty answer")
    return translation


def translate_to_chinese(text: str, model: str = TRANSLATION_MODEL) -> str:
    """Translate English text into Simplified Chinese."""
    translation = clean_translation(
        generate_text(ANSWER_TRANSLATION_PROMPT.format(text=text), model=model)
    )
    if not translation:
        raise RuntimeError("The translation model returned an empty answer")
    return translation


@lru_cache(maxsize=512)
def _expand_query_cached(query: str, model: str) -> str:
    """Generate and cache a short line of BM25 synonyms and related terms.

    The iterative evidence loop expands the same question repeatedly; caching
    avoids paying for the small model on every round.
    """
    response = generate_text(
        QUERY_EXPANSION_PROMPT.format(query=query),
        model=model,
        temperature=0.1,
        num_predict=EXPANSION_NUM_PREDICT,
        stop=["\n", "User:", "Assistant:"],
    )
    expanded = THINK_PATTERN.sub("", response)
    expanded = re.sub(r"[^\w\s-]", " ", expanded)
    return " ".join(expanded.split())[:500]


def expand_query(query: str, model: str = EXPANSION_MODEL) -> str:
    """Generate a short line of BM25 synonyms and related terms (cached)."""
    return _expand_query_cached(query, model)


def filter_candidates(
    question: str,
    candidates: list[tuple[Chunk, float]],
    model: str = FILTER_MODEL,
) -> list[tuple[Chunk, float]] | None:
    """Use a small model to keep only BM25 candidates relevant to the question.

    Returns the filtered candidates, an empty list when the model explicitly
    says NONE, or None when filtering was skipped or failed (in which case the
    caller should keep the unfiltered BM25 candidates).
    """
    if not LLM_FILTER_ENABLED:
        return None
    if len(candidates) < FILTER_MIN_CANDIDATES:
        return list(candidates)
    if len(candidates) > FILTER_MAX_CANDIDATES:
        return None

    lines = []
    for number, (chunk, _score) in enumerate(candidates, start=1):
        snippet = " ".join(paragraph_text(chunk).split())
        if len(snippet) > FILTER_SNIPPET_CHARS:
            snippet = snippet[:FILTER_SNIPPET_CHARS].rstrip() + "..."
        lines.append(f"[{number}] {snippet}")

    prompt = FILTER_PROMPT.format(
        question=question, candidates="\n".join(lines)
    )
    try:
        response = generate_text(
            prompt,
            model=model,
            temperature=0.0,
            num_predict=FILTER_NUM_PREDICT,
            stop=["\n\n"],
        )
    except RuntimeError as exc:
        print(f"Warning: candidate filter failed: {exc}", file=sys.stderr)
        return None

    cleaned = THINK_PATTERN.sub("", response).strip()
    if re.search(r"\bNONE\b", cleaned, re.IGNORECASE):
        return []

    chosen: set[int] = set()
    for token in re.findall(r"\d+", cleaned):
        number = int(token)
        if 1 <= number <= len(candidates):
            chosen.add(number)
    if not chosen:
        return None
    return [
        item
        for number, item in enumerate(candidates, start=1)
        if number in chosen
    ]


def pop_translation_segment(buffer: str, final: bool = False) -> tuple[str, str]:
    """Split off the next piece of streamed text that can be translated alone."""
    if final:
        return buffer, ""

    # Prefer line boundaries: list items and short lines translate much better
    # when they are not folded into the next paragraph. Wait for one more
    # character after the newline so trailing blank lines join this segment.
    newline = buffer.find("\n")
    if 0 <= newline < TRANSLATION_MAX_CHARS:
        end = newline + 1
        while end < len(buffer) and buffer[end] in "\r\n":
            end += 1
        if end < len(buffer):
            return buffer[:end], buffer[end:]

    if len(buffer) < TRANSLATION_MIN_CHARS:
        return "", buffer

    for match in TRANSLATION_BOUNDARY.finditer(buffer):
        # If the boundary is the very last character we have received, wait
        # for the next token so trailing spaces/newlines join this segment
        # instead of leaking into the next translation.
        if match.end() == len(buffer):
            break
        if match.end() >= TRANSLATION_MIN_CHARS:
            return buffer[: match.end()], buffer[match.end() :]

    if len(buffer) >= TRANSLATION_MAX_CHARS:
        cut = buffer.rfind(" ", 0, TRANSLATION_MAX_CHARS)
        if cut < TRANSLATION_MIN_CHARS:
            cut = TRANSLATION_MAX_CHARS
        return buffer[:cut], buffer[cut:]
    return "", buffer


# Chunk ids are internal retrieval metadata. The prompt explicitly forbids
# exposing them, and this filter removes citations such as [Chunk 11] or
# [Chunk 11, 14] from the streamed answer as a safety net.
CHUNK_CITATION_PATTERN = re.compile(
    r"[ \t]*\[\s*chunk(?:s)?\s*#?\s*\d+"
    r"(?:\s*(?:,|&|and|[-–—])\s*\d+)*\s*\]",
    re.IGNORECASE,
)
CHUNK_CITATION_PARTIAL_PATTERN = re.compile(
    r"^\[\s*(?:c(?:h(?:u(?:n(?:k(?:s)?)?)?)?)?"
    r"(?:\s*#?\s*\d*(?:\s*(?:,|&|and|[-–—])\s*\d*)*)?)?\s*$",
    re.IGNORECASE,
)
CHUNK_CITATION_MAX_PENDING = 80

# The answer prompt asks for a direct answer only. Some models still append a
# "Supporting Evidence" section out of habit, so remove that section both from
# the streamed output and from the returned answer.
SUPPORTING_EVIDENCE_HEADING_PATTERN = re.compile(
    r"(?im)^[ \t]*(?:(?:#{1,6}[ \t]*)|(?:[-*+][ \t]+)|(?:>[ \t]*)|"
    r"(?:\*\*[ \t]*|__[ \t]*))*"
    r"(?:\*\*|__)?[ \t]*supporting[ \t]+evidence[ \t]*"
    r"(?:\*\*|__)?[ \t]*(?::.*)?\r?$"
)
SUPPORTING_EVIDENCE_HEADING = "supporting evidence"


def strip_chunk_citations(text: str) -> str:
    """Remove internal chunk references like ``[Chunk 11]`` from text."""
    return CHUNK_CITATION_PATTERN.sub("", text)


def strip_supporting_evidence(text: str) -> str:
    """Drop a trailing ``Supporting Evidence`` section from an answer."""
    match = SUPPORTING_EVIDENCE_HEADING_PATTERN.search(text)
    if match is None:
        return text
    return text[: match.start()].rstrip()


def _could_be_supporting_evidence_heading(line: str) -> bool:
    """Return True while ``line`` could still become an evidence heading."""
    candidate = line.lstrip(" \t")
    # Hold lines that contain only Markdown decoration so a bullet or bold
    # marker does not leak before the heading itself arrives.
    if re.fullmatch(r"[#*_+>\- \t]*", candidate):
        return True
    previous = None
    while candidate != previous:
        previous = candidate
        candidate = re.sub(
            r"^(?:#{1,6}[ \t]*|[-*+][ \t]+|>[ \t]*|\*\*[ \t]*|__[ \t]*)",
            "",
            candidate,
        )
    candidate = candidate.lower()
    return candidate.startswith(SUPPORTING_EVIDENCE_HEADING) or (
        SUPPORTING_EVIDENCE_HEADING.startswith(candidate)
    )


class SupportingEvidenceFilter:
    """Streaming filter that drops a Supporting Evidence section."""

    def __init__(self) -> None:
        self._pending = ""
        self._suppressed = False

    def feed(self, text: str) -> str:
        """Accept streamed text and return only the portion to display."""
        if self._suppressed:
            return ""
        self._pending += text
        output: list[str] = []
        while self._pending:
            newline = self._pending.find("\n")
            if newline < 0:
                if _could_be_supporting_evidence_heading(self._pending):
                    break
                output.append(self._pending)
                self._pending = ""
                break

            line = self._pending[: newline + 1]
            self._pending = self._pending[newline + 1 :]
            if SUPPORTING_EVIDENCE_HEADING_PATTERN.match(line):
                self._suppressed = True
                self._pending = ""
                break
            output.append(line)
        return "".join(output)

    def flush(self) -> str:
        """Return buffered text unless it was the start of a suppressed section."""
        pending, self._pending = self._pending, ""
        if self._suppressed or SUPPORTING_EVIDENCE_HEADING_PATTERN.match(pending):
            return ""
        return pending


class ChunkCitationFilter:
    """Streaming filter that removes chunk citations token by token."""

    def __init__(self) -> None:
        self._pending = ""

    def feed(self, text: str) -> str:
        """Accept streamed text and return the portion safe to display."""
        self._pending += text
        output: list[str] = []
        while self._pending:
            start = self._pending.find("[")
            if start < 0:
                # Hold trailing horizontal whitespace until the next token. If
                # a citation starts next, the space is dropped with it instead
                # of being printed twice around the removed reference.
                trimmed = self._pending.rstrip(" \t")
                if trimmed != self._pending:
                    output.append(trimmed)
                    self._pending = self._pending[len(trimmed) :]
                else:
                    output.append(self._pending)
                    self._pending = ""
                break

            before = self._pending[:start]
            remainder = self._pending[start:]
            match = CHUNK_CITATION_PATTERN.match(remainder)
            if match:
                output.append(before.rstrip(" \t"))
                self._pending = remainder[match.end() :]
                continue

            if (
                len(remainder) <= CHUNK_CITATION_MAX_PENDING
                and CHUNK_CITATION_PARTIAL_PATTERN.match(remainder)
            ):
                # Hold whitespace immediately before a possible citation so
                # removing it does not leave a double space behind.
                trimmed = before.rstrip(" \t")
                output.append(trimmed)
                self._pending = before[len(trimmed) :] + remainder
                break

            output.append(before)
            output.append("[")
            self._pending = remainder[1:]
        return "".join(output)

    def flush(self) -> str:
        """Return any buffered text once the response is complete."""
        pending, self._pending = self._pending, ""
        return pending


def stream_answer(
    prompt: str,
    model: str = CHAT_MODEL,
    translate_answer: bool = False,
    metrics: dict | None = None,
    emit: Callable[[str], None] | None = None,
    think: bool = False,
) -> str:
    """Send the prompt to Ollama and stream the generated answer to stdout.

    When ``translate_answer`` is true the English text is not printed.
    Instead, completed sentence groups are translated with TRANSLATION_MODEL
    and printed as Simplified Chinese while the answer is still streaming.
    The full English answer is always returned.

    When ``metrics`` is provided it is filled with request timing and Ollama's
    final prompt statistics, which makes TTFT regressions measurable.

    ``emit`` is an optional text sink used by the HTTP server. When it is
    provided, streamed answer text is delivered there instead of stdout.

    ``think`` enables the model's reasoning mode. Ollama streams reasoning in
    a separate ``thinking`` field, so it never leaks into the answer text.
    """
    def output(text: str, end: str = "", flush: bool = True) -> None:
        if emit is not None:
            if text:
                emit(text)
            return
        print(text, end=end, flush=flush)

    payload = {
        "model": model,
        "prompt": prompt,
        "stream": True,
        "think": think,
        "keep_alive": OLLAMA_KEEP_ALIVE,
        "options": {
            "temperature": 0.2,
            "num_ctx": ANSWER_NUM_CTX,
        },
    }

    chunks: list[str] = []
    citation_filter = ChunkCitationFilter()
    evidence_filter = SupportingEvidenceFilter()
    translation_buffer = ""
    translation_started = False
    request_started = time.monotonic()
    first_token_elapsed: float | None = None
    final_event: dict = {}

    def emit_translation(segment: str) -> None:
        nonlocal translation_started
        if not segment.strip():
            return
        if not translation_started:
            output("[translated to Simplified Chinese]\n\n")
            translation_started = True
        try:
            translated = translate_to_chinese(segment)
        except RuntimeError as exc:
            print(
                f"\nWarning: could not translate part of the answer: {exc}",
                file=sys.stderr,
            )
            translated = segment.strip()
        # Small models often drop list markers ("1.", "- ") while translating;
        # put them back so numbered and bulleted answers keep their structure.
        prefix_match = LIST_PREFIX.match(segment)
        if prefix_match and not LIST_PREFIX.match(translated):
            translated = f"{prefix_match.group(1)} {translated}"
        output(translated)
        trailing_newlines = re.search(r"(\n+)[ \t]*$", segment)
        if trailing_newlines:
            output("\n" * len(trailing_newlines.group(1)))

    try:
        with requests.post(
            f"{OLLAMA_HOST}/api/generate",
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
                    token = citation_filter.feed(token)
                    token = evidence_filter.feed(token)
                    if token:
                        chunks.append(token)
                        if first_token_elapsed is None:
                            first_token_elapsed = (
                                time.monotonic() - request_started
                            )
                        if translate_answer:
                            translation_buffer += token
                            segment, translation_buffer = pop_translation_segment(
                                translation_buffer
                            )
                            if segment:
                                emit_translation(segment)
                        else:
                            output(token)
                if event.get("done"):
                    final_event = event
                    break
    except requests.RequestException as exc:
        raise RuntimeError(
            f"Could not reach Ollama at {OLLAMA_HOST}. Is it running? ({exc})"
        ) from exc

    trailing = evidence_filter.feed(citation_filter.flush())
    trailing += evidence_filter.flush()
    if trailing:
        chunks.append(trailing)
        if translate_answer:
            translation_buffer += trailing
        else:
            output(trailing)

    if translate_answer:
        if translation_buffer.strip():
            emit_translation(translation_buffer)
        output("\n")
    else:
        output("\n")

    if metrics is not None:
        metrics.update(
            {
                "request_ttft": first_token_elapsed
                if first_token_elapsed is not None
                else time.monotonic() - request_started,
                "load_duration": final_event.get("load_duration", 0) / 1e9,
                "prompt_eval_count": final_event.get("prompt_eval_count", 0),
                "prompt_eval_cached_count": final_event.get(
                    "prompt_eval_cached_count", 0
                ),
                "prompt_eval_duration": final_event.get(
                    "prompt_eval_duration", 0
                )
                / 1e9,
                "eval_count": final_event.get("eval_count", 0),
                "eval_duration": final_event.get("eval_duration", 0) / 1e9,
            }
        )
    return "".join(chunks)


def answer_simple_question(
    question: str,
    model: str = SIMPLE_ANSWER_MODEL,
    translate_answer: bool = False,
    metrics: dict | None = None,
    emit: Callable[[str], None] | None = None,
) -> str:
    """Answer ``question`` directly with the model, without retrieval.

    This backs the ``/ask`` command: it is meant for quick, self-contained
    questions that should not be grounded in the loaded trunks or in any
    other retrieval context. Output streams exactly like a normal answer.
    """
    prompt = SIMPLE_ANSWER_PROMPT.format(question=question.strip())
    return stream_answer(
        prompt,
        model=model,
        translate_answer=translate_answer,
        metrics=metrics,
        emit=emit,
    )


def _wiki_log(log: Callable[[str], None] | None, message: str) -> None:
    """Report /wiki progress without polluting the streamed answer."""
    if log is not None:
        log(message)
    else:
        print(message, file=sys.stderr)


def _wiki_title_tokens(title: str) -> set[str]:
    return {
        token
        for token in re.findall(r"[a-z0-9]+", title.casefold())
        if len(token) > 1 and token not in WIKI_GENERIC_TITLE_TOKENS
    }


def wiki_related_entities(
    question: str,
    model: str = WIKI_ENTITY_MODEL,
    limit: int = WIKI_MAX_ENTITIES,
) -> list[str]:
    """Return the Wikipedia article titles needed to answer ``question``.

    The entity list is produced by a local Ollama model. If the model is
    unavailable or returns nothing usable, the caller falls back to the
    question itself as the Wikipedia search term.
    """
    question = question.strip()
    if not question:
        return []
    try:
        response = generate_text(
            WIKI_ENTITY_PROMPT.format(question=question, limit=limit),
            model=model,
            temperature=0.1,
            num_predict=128,
            stop=["\n\nQuestion:", "\n\nTitles:"],
        )
    except RuntimeError:
        return []

    entities: list[str] = []
    seen: set[str] = set()
    for raw_line in THINK_PATTERN.sub("", response).splitlines():
        line = raw_line.strip()
        if not line or line.casefold().startswith(("titles:", "question:")):
            continue
        line = re.sub(r"^(?:[-*+]|\d+[.)])\s+", "", line)
        # Parentheses are preserved: they are part of a disambiguated
        # Wikipedia title such as "Rust (programming language)". Explanatory
        # aliases still fall back to the base title when the exact page does
        # not exist (see _wiki_entity_title_candidates).
        line = re.split(r"\s+[-–—]\s+|\s*:\s*", line, maxsplit=1)[0]
        line = line.strip().strip("\"'`“”").strip().rstrip(".")
        if not line or len(line) > 100:
            continue
        for part in re.split(r",\s+", line):
            entity = part.strip().strip("\"'`“”").strip().rstrip(".")
            if not entity or len(entity) > 100:
                continue
            key = entity.casefold()
            if key in seen:
                continue
            seen.add(key)
            entities.append(entity)
            if len(entities) >= limit:
                return entities
    return entities


def _wiki_name_parts(text: str) -> list[str]:
    """Normalize a name into comparable lowercase word parts."""
    parts: list[str] = []
    for match in WIKI_NAME_TOKEN_PATTERN.finditer(text):
        part = match.group(0).casefold().replace("’", "'")
        part = re.sub(r"'s$", "", part)
        part = re.sub(r"[^a-z0-9]+", "", part)
        if part:
            parts.append(part)
    return parts


def _wiki_entity_display_title(entity: str) -> str:
    """Return ``entity`` without a parenthetical disambiguator.

    "Rust (programming language)" is displayed as "Rust" in a corrected
    question, while the full title is still used to fetch the article.
    """
    entity = entity.strip()
    match = WIKI_TITLE_QUALIFIER_PATTERN.match(entity)
    if match:
        base = match.group("base").strip()
        if base:
            return base
    return entity


def _wiki_entity_title_candidates(entity: str) -> list[str]:
    """Return the titles to try for ``entity``, exact title first."""
    entity = entity.strip()
    if not entity:
        return []
    candidates = [entity]
    base = _wiki_entity_display_title(entity)
    if base and base.casefold() != entity.casefold():
        candidates.append(base)
    return candidates


def _wiki_question_qualifiers(question: str, entity: str) -> set[str]:
    """Return the meaningful question words that can disambiguate ``entity``."""
    entity_terms = _wiki_title_tokens(_wiki_entity_display_title(entity))
    return {
        term
        for term in _wiki_title_tokens(question)
        if term not in entity_terms and term not in WIKI_QUESTION_STOPWORDS
    }


def _wiki_search_pages(
    query: str, limit: int = WIKI_SEARCH_LIMIT
) -> list[dict]:
    """Return Wikipedia search results for ``query`` (empty list on failure)."""
    if not query.strip():
        return []
    for attempt in range(2):
        try:
            response = requests.get(
                WIKI_SEARCH_API_URL,
                params={"q": query, "limit": limit},
                headers=HTTP_HEADERS,
                timeout=(10, 30),
            )
        except requests.RequestException:
            return []
        if response.status_code == 429 and attempt == 0:
            # Be a good Wikipedia API citizen before retrying once.
            time.sleep(2)
            continue
        if response.status_code != 200:
            return []
        try:
            data = response.json()
        except ValueError:
            return []
        pages = data.get("pages") if isinstance(data, dict) else None
        return pages if isinstance(pages, list) else []
    return []


def wiki_disambiguate_entity(entity: str, question: str) -> str:
    """Resolve an ambiguous bare title using the question's context.

    A bare title such as "Rust" resolves to that word's primary Wikipedia
    topic (the iron oxide) even when the question is about the programming
    language. For single-word titles, search Wikipedia with the question and
    prefer a candidate whose title shares the entity's name and whose
    qualifier or description uses words from the question, for example
    "Rust (programming language)". Titles that already carry a disambiguator
    are left untouched.
    """
    entity = entity.strip()
    if not entity or "(" in entity:
        return entity
    base_terms = _wiki_title_tokens(entity)
    if len(base_terms) != 1:
        return entity
    qualifiers = _wiki_question_qualifiers(question, entity)
    if not qualifiers:
        return entity

    best_title = ""
    best_overlap = 0
    for page in _wiki_search_pages(question):
        title = str(page.get("title") or "").strip()
        if not title:
            continue
        match = WIKI_TITLE_QUALIFIER_PATTERN.match(title)
        base = match.group("base") if match else title
        if _wiki_title_tokens(base) != base_terms:
            continue
        title_terms = _wiki_title_tokens(title) - base_terms
        description_terms = _wiki_title_tokens(
            str(page.get("description") or "")
        )
        overlap = (title_terms | description_terms) & qualifiers
        if not overlap or len(overlap) <= best_overlap:
            continue
        best_title = title
        best_overlap = len(overlap)
    return best_title or entity


def _wiki_entity_aliases(entity: str) -> list[list[str]]:
    """Return normalized names a question might use for ``entity``.

    Wikipedia titles sometimes carry a middle name ("Terence Chi-Shen Tao")
    while a question only uses the first and last name. Both the full title
    and the first-plus-last-name form are therefore considered aliases. A
    parenthetical disambiguator is not part of the name a question uses, so
    only the base title is considered here.
    """
    parts = _wiki_name_parts(_wiki_entity_display_title(entity))
    if not parts:
        return []
    aliases = [parts]
    if len(parts) > 2:
        aliases.append([parts[0], parts[-1]])
    return aliases


def wiki_canonicalize_question(question: str, entities: list[str]) -> str:
    """Rewrite close misspellings of resolved entity names in ``question``.

    The entity extractor may correct "terrence tao" to the article title
    "Terence Tao", but the answer model still sees the original misspelling
    in the question. Matching question word windows against the resolved
    titles and replacing the closest one keeps the question consistent with
    the article we are about to search.
    """
    token_matches = list(WIKI_NAME_TOKEN_PATTERN.finditer(question))
    if not token_matches:
        return question
    tokens: list[str] = []
    for match in token_matches:
        parts = _wiki_name_parts(match.group(0))
        tokens.append(parts[0] if parts else "")

    candidates: list[tuple[float, int, int, str]] = []
    for entity in entities:
        title = entity.strip()
        if not title:
            continue
        display_title = _wiki_entity_display_title(title)
        for alias in _wiki_entity_aliases(title):
            # Single-word aliases are deliberately skipped: matching them
            # against ordinary question words (for example the month "May")
            # would rewrite unrelated text. Multi-word names are unambiguous
            # enough for fuzzy matching.
            if len(alias) < 2:
                continue
            target = " ".join(alias)
            for start in range(len(tokens) - len(alias) + 1):
                window = tokens[start:start + len(alias)]
                if not all(window):
                    continue
                ratio = difflib.SequenceMatcher(
                    None, target, " ".join(window)
                ).ratio()
                if ratio >= WIKI_NAME_MATCH_CUTOFF:
                    candidates.append(
                        (ratio, start, start + len(alias), display_title)
                    )

    if not candidates:
        return question

    # Prefer the closest match, and never apply overlapping replacements.
    candidates.sort(key=lambda item: (-item[0], item[2] - item[1]))
    chosen: list[tuple[float, int, int, str]] = []
    for candidate in candidates:
        _, start, end, _ = candidate
        if any(
            not (end <= chosen_start or start >= chosen_end)
            for _, chosen_start, chosen_end, _ in chosen
        ):
            continue
        chosen.append(candidate)

    result = question
    for _, start, end, title in sorted(
        chosen, key=lambda item: item[1], reverse=True
    ):
        begin = token_matches[start].start()
        finish = token_matches[end - 1].end()
        original = question[begin:finish]
        # Keep a possessive that belonged to the user's spelling, so
        # "terrence tao's birthday" becomes "Terence Tao's birthday".
        possessive = bool(re.search(r"['’]s$", original, re.IGNORECASE))
        replacement = title
        if possessive and not re.search(r"['’]s$", title, re.IGNORECASE):
            replacement += "'s"
        result = result[:begin] + replacement + result[finish:]
    return result


def _wiki_title_from_url(url: str, fallback: str) -> str:
    if url:
        slug = url.split("#", 1)[0].split("?", 1)[0].rstrip("/")
        slug = slug.rsplit("/", 1)[-1]
        title = unquote(slug).replace("_", " ").strip()
        if title:
            return title
    return fallback


def _run_wiki_full_article_script(entity: str) -> tuple[str, bool]:
    """Run ./wiki_full_article_search.php and return (stdout, search_fallback)."""
    script = WIKI_FULL_ARTICLE_SCRIPT
    if not script.exists():
        return "", False
    commands = (
        [str(script), entity],
        ["php", str(script), entity],
    )
    for command in commands:
        try:
            completed = subprocess.run(
                command,
                cwd=str(SCRIPT_DIR),
                stdin=subprocess.DEVNULL,
                capture_output=True,
                text=True,
                timeout=WIKI_ARTICLE_TIMEOUT,
            )
        except (OSError, subprocess.SubprocessError):
            continue
        if completed.stdout.strip():
            raw = ANSI_ESCAPE_PATTERN.sub("", completed.stdout)
            search_fallback = bool(WIKI_SEARCH_FALLBACK_LINE.search(raw))
            raw = WIKI_SEARCH_FALLBACK_LINE.sub("", raw).strip()
            return raw, search_fallback
    return "", False


def fetch_wiki_summary_article(entity: str) -> dict | None:
    """Fallback to the Wikipedia REST summary when the PHP helper fails.

    When the entity was extracted together with an explanatory parenthetical
    ("Terence Tao (mathematician)") rather than a real disambiguator, the
    exact title does not exist. The base title is then tried as well.
    """
    for title in _wiki_entity_title_candidates(entity):
        article = _fetch_wiki_summary_title(title, entity)
        if article is not None:
            return article
    return None


def _fetch_wiki_summary_title(title: str, entity: str) -> dict | None:
    """Fetch the REST summary for one exact title.

    Wikipedia occasionally answers with a transient 429 or a dropped
    connection. A couple of short retries keep a multi-entity question from
    silently losing one of its subjects.
    """
    url = WIKI_SUMMARY_URL.format(word=quote(title, safe=""))
    for attempt in range(3):
        try:
            response = requests.get(
                url, headers=HTTP_HEADERS, timeout=(10, 30)
            )
        except requests.RequestException:
            if attempt == 2:
                return None
            time.sleep(1 + attempt)
            continue
        if response.status_code == 404:
            return None
        if response.status_code == 429 and attempt < 2:
            retry_after = response.headers.get("Retry-After", "")
            try:
                delay = float(retry_after)
            except ValueError:
                delay = 1 + attempt
            time.sleep(min(max(delay, 1.0), 5.0))
            continue
        try:
            response.raise_for_status()
            data = response.json()
        except (requests.RequestException, ValueError):
            if attempt == 2:
                return None
            time.sleep(1 + attempt)
            continue
        break
    else:
        return None
    if not isinstance(data, dict):
        return None
    text = data.get("extract") or data.get("description") or ""
    text = str(text).strip()
    if not text:
        return None
    page = ((data.get("content_urls") or {}).get("desktop") or {}).get("page")
    page_url = str(page or "")
    page_title = str(data.get("title") or title)
    fallback_url = (
        "https://en.wikipedia.org/wiki/"
        + quote(page_title.replace(" ", "_"))
    )
    return {
        "entity": entity,
        "title": page_title,
        "text": text,
        "url": page_url or fallback_url,
        "search_fallback": False,
    }


def fetch_wiki_full_article(entity: str) -> dict | None:
    """Fetch the complete Wikipedia article for ``entity``.

    The PHP helper is the primary source, as required by the /wiki command.
    The REST summary endpoint is used only when the helper is unavailable or
    returns no usable text.
    """
    text, search_fallback = _run_wiki_full_article_script(entity)
    if text:
        source_match = WIKI_SOURCE_LINE.search(text)
        url = source_match.group(1) if source_match else ""
        body = WIKI_SOURCE_LINE.sub("", text).strip()
        if body and not WIKI_NO_ARTICLE_LINE.search(body):
            return {
                "entity": entity,
                "title": _wiki_title_from_url(url, entity),
                "text": body,
                "url": url,
                "search_fallback": search_fallback,
            }
    return fetch_wiki_summary_article(entity)


def _wiki_article_matches_entity(entity: str, article: dict) -> bool:
    """Reject an obviously unrelated search result from the PHP helper.

    When an exact article title is missing, the helper searches Wikipedia and
    keeps the first hit. For example, a search for a nonexistent "Nijo Market"
    article can return an unrelated play. If the result shares no meaningful
    title word with the entity, it is safer to skip it than to feed the answer
    model a completely different topic.
    """
    if not article.get("search_fallback"):
        return True
    entity_tokens = _wiki_title_tokens(entity)
    title_tokens = _wiki_title_tokens(str(article.get("title", "")))
    return bool(entity_tokens & title_tokens)


def _wiki_question_name_tokens(question: str) -> list[str]:
    """Return the normalized name-like tokens of ``question``."""
    tokens: list[str] = []
    for match in WIKI_NAME_TOKEN_PATTERN.finditer(question):
        parts = _wiki_name_parts(match.group(0))
        tokens.append(parts[0] if parts else "")
    return tokens


def _wiki_entity_mentioned_in_question(entity: str, question: str) -> bool:
    """Return True when ``question`` explicitly names ``entity``.

    A question can mention several subjects ("when is Donald Trump and Elon
    Musk's birthday?"). Those explicitly named entities must each contribute
    to the answer, while entities the extractor added only as related topics
    remain optional. Single-word names are matched exactly so ordinary words
    (for example the month "May") are not treated as entity mentions;
    multi-word names tolerate the same spelling differences as
    :func:`wiki_canonicalize_question`.
    """
    question_tokens = _wiki_question_name_tokens(question)
    if not question_tokens:
        return False
    for alias in _wiki_entity_aliases(entity):
        if not alias:
            continue
        if len(alias) == 1:
            if alias[0] in question_tokens:
                return True
            continue
        target = " ".join(alias)
        for start in range(len(question_tokens) - len(alias) + 1):
            window = question_tokens[start:start + len(alias)]
            if not all(window):
                continue
            ratio = difflib.SequenceMatcher(
                None, target, " ".join(window)
            ).ratio()
            if ratio >= WIKI_NAME_MATCH_CUTOFF:
                return True
    return False


def _wiki_entity_is_redundant(entity: str, entities: list[str]) -> bool:
    """Return True for a phrase the extractor built from another entity.

    The entity model sometimes returns a question phrase such as "Donald
    Trump birthday" in addition to the real article title "Donald Trump".
    Treating that phrase as a second subject makes a single-subject question
    look like a multi-subject one. A title without a disambiguator is
    redundant when its meaningful words are a superset of another extracted
    title and the extra words are only question/fact words.
    """
    title = entity.strip()
    if not title or "(" in title:
        return False
    tokens = _wiki_title_tokens(_wiki_entity_display_title(title))
    if not tokens:
        return False
    for other in entities:
        other_title = other.strip()
        if not other_title or other_title == title or "(" in other_title:
            continue
        other_tokens = _wiki_title_tokens(
            _wiki_entity_display_title(other_title)
        )
        if not other_tokens or other_tokens == tokens:
            continue
        if not other_tokens < tokens:
            continue
        extra = tokens - other_tokens
        if extra and all(
            token in WIKI_QUESTION_STOPWORDS
            or token in WIKI_GENERIC_TITLE_TOKENS
            or WIKI_FACT_TERM_PATTERN.fullmatch(token)
            for token in extra
        ):
            return True
    return False


def _wiki_clean_entities(entities: list[str]) -> list[str]:
    """Drop duplicate and question-phrase entities while keeping order."""
    cleaned: list[str] = []
    seen: set[str] = set()
    for entity in entities:
        key = entity.casefold()
        if key in seen or _wiki_entity_is_redundant(entity, entities):
            continue
        seen.add(key)
        cleaned.append(entity)
    return cleaned


def _wiki_article_identity(article: dict) -> str:
    """Return a stable key identifying the underlying Wikipedia article.

    Different entity names can resolve to the same page: the extractor may
    return both "NATO" and "North Atlantic Treaty Organization", and
    Wikipedia redirects the latter (and the former's aliases) to one article.
    Keying on the resolved article title, rather than the requested entity,
    keeps the same summary from being emitted twice.
    """
    title = str(article.get("title") or "").strip()
    if title:
        return title.casefold().replace("_", " ")
    url = str(article.get("url") or "").strip()
    return url.casefold()


def _truncate_wiki_text(text: str, budget: int) -> str:
    if len(text) <= budget:
        return text
    cut = text[:budget]
    paragraph_break = cut.rfind("\n\n")
    if paragraph_break > budget * 0.6:
        cut = cut[:paragraph_break]
    return cut.rstrip()


def wiki_is_narrow_question(question: str) -> bool:
    """Return True when the question asks for one specific fact.

    Narrow questions (including yes/no questions such as "is X from Y?")
    benefit from scanning the full article for a few keywords. Broad overview
    questions do not; for those, the entity summaries are a better answer.
    """
    question = question.strip()
    if not question or WIKI_BROAD_QUESTION_PATTERN.search(question):
        return False
    if WIKI_YES_NO_QUESTION_PATTERN.search(question):
        return True
    if WIKI_WH_QUESTION_PATTERN.search(question):
        return True
    if WIKI_HOW_QUANTITY_PATTERN.search(question):
        return True
    if re.search(r"\b(?:what|which)\b", question, re.IGNORECASE):
        return bool(WIKI_FACT_TERM_PATTERN.search(question))
    return False


def wiki_question_keywords(
    question: str,
    model: str = WIKI_KEYWORD_MODEL,
    limit: int = WIKI_MAX_KEYWORDS,
) -> list[str]:
    """Ask a local model for a few keywords that locate a fact in an article."""
    question = question.strip()
    if not question:
        return []
    try:
        response = generate_text(
            WIKI_KEYWORD_PROMPT.format(question=question, limit=limit),
            model=model,
            temperature=0.0,
            num_predict=96,
            stop=["\n\nQuestion:", "\n\nKeywords:"],
        )
    except RuntimeError:
        return []

    keywords: list[str] = []
    seen: set[str] = set()
    for raw_line in THINK_PATTERN.sub("", response).splitlines():
        line = raw_line.strip()
        if not line or line.casefold().startswith(("keywords:", "question:")):
            continue
        line = re.sub(r"^(?:[-*+]|\d+[.)])\s+", "", line)
        line = line.strip().strip("\"'`“”").strip().rstrip(".,;")
        for part in re.split(r"\s*(?:,|;|\||\t)\s*", line):
            keyword = part.strip().strip("\"'`“”").strip().rstrip(".,;")
            if not keyword or len(keyword) > 60:
                continue
            key = keyword.casefold()
            if key in seen:
                continue
            seen.add(key)
            keywords.append(keyword)
            if len(keywords) >= limit:
                return keywords
    return keywords


def _wiki_keyword_search_terms(keywords: list[str]) -> list[str]:
    """Return keywords plus their stems for inflection-tolerant matching.

    The answer model often words a fact differently from the question: "who
    wrote Rust" is answered by "Graydon Hoare created Rust", and "creator"
    is answered by "created". Stripping common suffixes lets the regex below
    match every form of the keyword without a full linguistic stemmer.
    """
    terms: set[str] = set()
    for keyword in keywords:
        folded = keyword.casefold().strip()
        if not folded:
            continue
        terms.add(folded)
        for suffix in WIKI_KEYWORD_SUFFIXES:
            if folded.endswith(suffix) and len(folded) - len(suffix) >= 4:
                terms.add(folded[: -len(suffix)])
    return sorted(terms, key=len, reverse=True)


def _wiki_keyword_pattern(keywords: list[str]) -> re.Pattern[str] | None:
    """Compile the stem-aware, case-insensitive pattern used for searching."""
    terms = _wiki_keyword_search_terms(keywords)
    if not terms:
        return None
    pattern = r"\b(?:" + "|".join(re.escape(term) for term in terms) + r")\w*"
    return re.compile(pattern, re.IGNORECASE)


def _wiki_python_keyword_search(
    article_text: str,
    keywords: list[str],
    context_lines: int = WIKI_AG_CONTEXT_LINES,
) -> str:
    """Fallback excerpt finder used when ag is unavailable or fails."""
    pattern = _wiki_keyword_pattern(keywords)
    if pattern is None:
        return ""
    lines = article_text.splitlines()
    selected: set[int] = set()
    for index, line in enumerate(lines):
        if pattern.search(line):
            start = max(0, index - context_lines)
            end = min(len(lines), index + context_lines + 1)
            selected.update(range(start, end))
    if not selected:
        return ""
    excerpt = "\n".join(lines[index] for index in sorted(selected))
    return _truncate_wiki_text(excerpt.strip(), WIKI_EXCERPT_MAX_CHARS)


def wiki_ag_search(article_text: str, keywords: list[str]) -> str:
    """Search a full article for ``keywords`` with ag and return the hits.

    ag does not support multiple -e patterns in this project's version, so the
    keywords are combined into one escaped alternation. Keyword stems are
    included so inflected forms in the article still match ("creator" finds
    "created"). The article is passed on stdin and ag prints only the matching
    line groups, not the whole text.
    """
    if not article_text.strip() or not keywords:
        return ""
    terms = _wiki_keyword_search_terms(keywords)
    if not terms:
        return ""
    pattern = r"\b(?:" + "|".join(re.escape(term) for term in terms) + r")\w*"
    command = [
        "ag",
        "--nocolor",
        # Use the short forms here: ag 2.2.0 mis-parses the long
        # --ignore-case/--context options when reading an article on stdin.
        "-i",
        "-C",
        str(WIKI_AG_CONTEXT_LINES),
        "--no-filename",
        "--",
        pattern,
    ]
    try:
        result = subprocess.run(
            command,
            input=article_text,
            capture_output=True,
            text=True,
            timeout=30,
        )
    except FileNotFoundError:
        return _wiki_python_keyword_search(article_text, keywords)
    except (OSError, subprocess.SubprocessError):
        return _wiki_python_keyword_search(article_text, keywords)

    # ag exits 1 when there are no matches; anything else is an error.
    if result.returncode not in (0, 1):
        return _wiki_python_keyword_search(article_text, keywords)
    excerpt = ANSI_ESCAPE_PATTERN.sub("", result.stdout).strip()
    return _truncate_wiki_text(excerpt, WIKI_EXCERPT_MAX_CHARS)


def wiki_answer_from_excerpt(
    question: str,
    title: str,
    excerpt: str,
    model: str = WIKI_ANSWER_MODEL,
) -> str | None:
    """Answer ``question`` from ag hits, or return None if they lack the fact."""
    if not excerpt.strip():
        return None
    response = generate_text(
        WIKI_EXCERPT_ANSWER_PROMPT.format(
            title=title, excerpt=excerpt, question=question
        ),
        model=model,
        temperature=0.0,
        num_predict=320,
    )
    answer = THINK_PATTERN.sub("", response).strip()
    if not answer or WIKI_NO_ANSWER_PATTERN.match(answer):
        return None
    return answer


def wiki_answer_from_article(
    question: str,
    title: str,
    article_text: str,
    model: str = WIKI_ANSWER_MODEL,
) -> str | None:
    """Answer from the full article, or return None if the fact is absent.

    Keyword hits can miss a fact when the article words it with a synonym the
    keyword model did not think of. Falling back to the article itself keeps
    narrow questions answerable without dropping to a one-paragraph summary.
    """
    context = _truncate_wiki_text(
        article_text.strip(), WIKI_MAX_ARTICLE_CHARS
    )
    if not context:
        return None
    response = generate_text(
        WIKI_ARTICLE_ANSWER_PROMPT.format(
            title=title, context=context, question=question
        ),
        model=model,
        temperature=0.0,
        num_predict=320,
    )
    answer = THINK_PATTERN.sub("", response).strip()
    if not answer or WIKI_NO_ANSWER_PATTERN.match(answer):
        return None
    return answer


def _wiki_answer_multiple_entities(
    question: str,
    entities: list[str],
    keywords: list[str],
    *,
    emit: Callable[[str], None] | None = None,
    log: Callable[[str], None] | None = None,
) -> str:
    """Answer one question that asks about several explicitly named entities.

    Asking the model about each article separately fails for questions such as
    "when is Donald Trump and Elon Musk's birthday?": an article about Trump
    alone cannot satisfy the two-subject question, so the model rejects it and
    only the last entity's article ends up answering. Collecting the keyword
    excerpts from every required article into one context lets the model
    answer all the requested facts together.
    """
    sections: list[tuple[str, str, str]] = []
    seen_articles: set[str] = set()
    for entity in entities:
        _wiki_log(log, f"[wiki: searching the full article for {entity!r}]")
        article = fetch_wiki_full_article(entity)
        if article is None:
            _wiki_log(log, f"[wiki: no article found for {entity!r}]")
            continue
        if not _wiki_article_matches_entity(entity, article):
            _wiki_log(
                log,
                f"[wiki: skipping the unrelated result {article['title']!r}]",
            )
            continue

        title = str(article.get("title") or entity)
        article_key = title.casefold()
        if article_key in seen_articles:
            _wiki_log(log, f"[wiki: {title!r} was already searched]")
            continue
        seen_articles.add(article_key)

        article_text = str(article.get("text", ""))
        context = wiki_ag_search(article_text, keywords)
        if not context:
            _wiki_log(
                log,
                f"[wiki: no keyword hits in {title!r}; using the full "
                "article]",
            )
            context = _truncate_wiki_text(
                article_text.strip(), WIKI_MAX_ARTICLE_CHARS
            )
        if not context:
            _wiki_log(log, f"[wiki: the article {title!r} is empty]")
            continue
        sections.append((title, str(article.get("url") or ""), context))

    if not sections:
        return ""

    context = "\n\n".join(
        f"## {title}\n\n{text}" for title, _, text in sections
    )
    context = _truncate_wiki_text(context, WIKI_MAX_CONTEXT_CHARS)
    try:
        response = generate_text(
            WIKI_ANSWER_PROMPT.format(context=context, question=question),
            model=WIKI_ANSWER_MODEL,
            temperature=0.0,
            num_predict=480,
        )
    except RuntimeError as exc:
        _wiki_log(log, f"[wiki: could not answer the question: {exc}]")
        return ""
    answer = THINK_PATTERN.sub("", response).strip()
    if not answer or WIKI_NO_ANSWER_PATTERN.match(answer):
        _wiki_log(
            log,
            "[wiki: the combined articles do not answer the question]",
        )
        return ""

    heading = ", ".join(title for title, _, _ in sections)
    chunk = f"# {heading}\n\n{answer}"
    urls = [url for _, url, _ in sections if url]
    if urls:
        chunk += "\n\nSources:\n" + "\n".join(f"- {url}" for url in urls)
    chunk += "\n"
    if emit is not None:
        emit(chunk)
    else:
        print(chunk, end="", flush=True)
    return chunk


def _answer_wiki_narrow_question(
    question: str,
    entities: list[str],
    *,
    emit: Callable[[str], None] | None = None,
    log: Callable[[str], None] | None = None,
) -> str:
    """Search each entity's full article with ag and stop at the first answer.

    When the question explicitly names two or more entities, every named
    entity's article is required, so their excerpts are answered together
    instead of stopping at the first article that happens to contain a fact.
    """
    keywords = wiki_question_keywords(question)
    if not keywords:
        _wiki_log(log, "[wiki: could not generate search keywords]")
        return ""
    _wiki_log(log, f"[wiki: searching with keywords: {', '.join(keywords)}]")

    mentioned = [
        entity
        for entity in entities
        if _wiki_entity_mentioned_in_question(entity, question)
    ]
    if len(mentioned) >= 2:
        _wiki_log(
            log,
            "[wiki: the question names several entities; answering them "
            f"together: {', '.join(mentioned)}]",
        )
        combined = _wiki_answer_multiple_entities(
            question, mentioned, keywords, emit=emit, log=log
        )
        if combined:
            return combined
        # Every named entity is required, so a partial per-article answer
        # would repeat the original bug. Returning nothing lets the caller
        # fall back to the summaries of all named entities instead.
        _wiki_log(
            log,
            "[wiki: the combined answer failed; using entity summaries "
            "instead]",
        )
        return ""

    # Search explicitly named entities first. Entities that the extractor
    # inferred as related topics are still available as fallbacks.
    mentioned_keys = {entity.casefold() for entity in mentioned}
    ordered_entities = mentioned + [
        entity
        for entity in entities
        if entity.casefold() not in mentioned_keys
    ]

    seen_articles: set[str] = set()
    for entity in ordered_entities:
        _wiki_log(log, f"[wiki: searching the full article for {entity!r}]")
        article = fetch_wiki_full_article(entity)
        if article is None:
            _wiki_log(log, f"[wiki: no article found for {entity!r}]")
            continue
        if not _wiki_article_matches_entity(entity, article):
            _wiki_log(
                log,
                f"[wiki: skipping the unrelated result {article['title']!r}]",
            )
            continue

        title = str(article.get("title") or entity)
        # An entity can redirect to a page we already searched ("Graydon
        # Hoare" redirects to "Rust (programming language)"). Search each
        # article once instead of repeating the same expensive requests.
        article_key = title.casefold()
        if article_key in seen_articles:
            _wiki_log(log, f"[wiki: {title!r} was already searched]")
            continue
        seen_articles.add(article_key)

        article_text = str(article.get("text", ""))
        excerpt = wiki_ag_search(article_text, keywords)
        answer: str | None = None
        try:
            if excerpt:
                answer = wiki_answer_from_excerpt(question, title, excerpt)
            if not answer:
                _wiki_log(
                    log,
                    f"[wiki: keyword hits in {title!r} did not answer the "
                    "question; answering from the full article]",
                )
                answer = wiki_answer_from_article(
                    question, title, article_text
                )
        except RuntimeError as exc:
            _wiki_log(log, f"[wiki: could not check the keyword hits: {exc}]")
            return ""
        if not answer:
            _wiki_log(
                log,
                f"[wiki: the article {title!r} does not answer the question]",
            )
            continue

        url = str(article.get("url") or "")
        chunk = f"# {title}\n\n{answer}"
        if url:
            chunk += f"\n\nSource: {url}"
        chunk += "\n"
        if emit is not None:
            emit(chunk)
        else:
            print(chunk, end="", flush=True)
        return chunk

    return ""


def answer_wiki_question(
    question: str,
    *,
    emit: Callable[[str], None] | None = None,
    log: Callable[[str], None] | None = None,
    model: str = WIKI_ANSWER_MODEL,
) -> str:
    """Answer ``question`` from Wikipedia, using the most efficient path.

    For narrow fact questions:
    1. Ask a local model for the question's main entities and a few keywords.
    2. Fetch each entity's full article and search it with ag for the keywords.
    3. Answer from the matching excerpts and stop at the first article that
       contains the fact. If none do, fall back to the entity summaries.

    For broad questions, output the Wikipedia summary for each main entity.

    ``emit`` receives streamed text (the HTTP server pipes it to SSE); when
    omitted, the answer is printed to stdout like the other CLI commands.
    ``model`` is kept for backward compatibility with callers.
    """
    del model
    question = question.strip()
    if not question:
        return ""

    entities = wiki_related_entities(question)
    if entities:
        resolved_entities: list[str] = []
        for entity in entities:
            resolved = wiki_disambiguate_entity(entity, question)
            if resolved != entity:
                _wiki_log(
                    log, f"[wiki: resolved {entity!r} to {resolved!r}]"
                )
            resolved_entities.append(resolved)
        entities = resolved_entities
        entities = _wiki_clean_entities(entities)
        _wiki_log(log, f"[wiki: main entities: {', '.join(entities)}]")
        canonical_question = wiki_canonicalize_question(question, entities)
        if canonical_question != question:
            _wiki_log(
                log,
                f"[wiki: using the corrected question: {canonical_question}]",
            )
            question = canonical_question
    else:
        entities = [question]
        _wiki_log(
            log,
            "[wiki: could not extract entities; using the question as the "
            "Wikipedia search term]",
        )

    # Narrow fact questions are answered from the lines around ag matches in
    # the full article. If no article yields the fact, fall through to the
    # regular entity summaries so the command still returns useful content.
    if wiki_is_narrow_question(question):
        keyword_output = _answer_wiki_narrow_question(
            question, entities, emit=emit, log=log
        )
        if keyword_output:
            return keyword_output
        _wiki_log(
            log,
            "[wiki: keyword search found no direct answer; using entity "
            "summaries instead]",
        )

    output: list[str] = []
    attempted: set[str] = set()
    emitted_articles: set[str] = set()

    def fetch_and_emit(entity: str) -> bool:
        attempted.add(entity.casefold())
        _wiki_log(log, f"[wiki: fetching the summary for {entity!r}]")
        article = fetch_wiki_summary_article(entity)
        if article is None:
            _wiki_log(log, f"[wiki: no summary found for {entity!r}]")
            return False
        article_key = _wiki_article_identity(article)
        if article_key and article_key in emitted_articles:
            _wiki_log(
                log,
                f"[wiki: {article['title']!r} was already shown; skipping "
                f"the duplicate {entity!r}]",
            )
            return False
        _wiki_log(log, f"[wiki: using the summary of {article['title']!r}]")
        chunk = _format_wiki_summary_article(article)
        if output:
            chunk = "\n" + chunk
        if emit is not None:
            emit(chunk)
        else:
            print(chunk, end="", flush=True)
        output.append(chunk)
        if article_key:
            emitted_articles.add(article_key)
        return True

    for entity in entities:
        fetch_and_emit(entity)

    # If entity extraction was useless, still try the original question as the
    # literal /wiki fallback.
    if not output and question.casefold() not in attempted:
        _wiki_log(log, "[wiki: retrying with the full question]")
        fetch_and_emit(question)

    if not output:
        message = (
            f"Wikipedia has no usable summary for {question!r}. "
            "Try a more specific name or topic.\n"
        )
        if emit is not None:
            emit(message)
        else:
            print(message, end="", file=sys.stderr)
        return ""

    return "".join(output)


def _stream_wiki_full_article_script(
    entity: str,
    emit: Callable[[str], None] | None,
    *,
    strip_ansi: bool = True,
) -> tuple[str, bool]:
    """Run ./wiki_full_article_search.php and forward its output immediately.

    The helper prints the article through stdout, so streaming it line by line
    lets the browser/terminal show each article as soon as it is available
    instead of waiting for every entity to finish. Returns the captured text
    and whether the helper fell back to a Wikipedia search hit.
    """
    script = WIKI_FULL_ARTICLE_SCRIPT
    if not script.exists():
        return "", False

    commands = (
        [str(script), entity],
        ["php", str(script), entity],
    )
    for command in commands:
        try:
            process = subprocess.Popen(
                command,
                cwd=str(SCRIPT_DIR),
                stdin=subprocess.DEVNULL,
                stdout=subprocess.PIPE,
                stderr=subprocess.STDOUT,
                text=True,
                bufsize=1,
            )
        except OSError:
            continue

        assert process.stdout is not None
        chunks: list[str] = []
        try:
            for raw_line in process.stdout:
                line = (
                    ANSI_ESCAPE_PATTERN.sub("", raw_line)
                    if strip_ansi
                    else raw_line
                )
                chunks.append(line)
                if emit is not None:
                    # The helper announces a Wikipedia-search fallback before
                    # the article; the title that follows already makes that
                    # clear, so keep the notice out of the streamed article.
                    if not WIKI_SEARCH_FALLBACK_LINE.fullmatch(line.strip()):
                        emit(line)
                else:
                    print(line, end="", flush=True)
            try:
                process.wait(timeout=WIKI_ARTICLE_TIMEOUT)
            except subprocess.TimeoutExpired:
                process.kill()
                process.wait()
        except BaseException:
            process.terminate()
            try:
                process.wait(timeout=5)
            except subprocess.TimeoutExpired:
                process.kill()
                process.wait()
            raise

        text = "".join(chunks)
        if text.strip():
            return text, bool(WIKI_SEARCH_FALLBACK_LINE.search(text))

    return "", False


def _format_wiki_summary_article(article: dict) -> str:
    """Render the REST summary fallback in the same plain-text style."""
    title = str(article.get("title") or article.get("entity") or "Unknown")
    text = str(article.get("text") or "").strip()
    url = str(article.get("url") or "")
    lines = [f"# {title}", "", text]
    if url:
        lines.extend(["", f"Source: {url}"])
    return "\n".join(lines).rstrip() + "\n"


def print_wiki_full_articles(
    question: str,
    *,
    emit: Callable[[str], None] | None = None,
    log: Callable[[str], None] | None = None,
) -> str:
    """Print each main entity's complete Wikipedia article as it is fetched.

    Implements /wiki_article: extract the question's main entities, then run
    ./wiki_full_article_search.php <entity> for each one and forward the
    result immediately. The helper's REST-summary fallback is used only when
    the PHP script is unavailable or returns no article.
    """
    question = question.strip()
    if not question:
        return ""

    entities = wiki_related_entities(question)
    if entities:
        resolved_entities: list[str] = []
        for entity in entities:
            resolved = wiki_disambiguate_entity(entity, question)
            if resolved != entity:
                _wiki_log(
                    log,
                    f"[wiki_article: resolved {entity!r} to {resolved!r}]",
                )
            resolved_entities.append(resolved)
        entities = resolved_entities
        entities = _wiki_clean_entities(entities)
        _wiki_log(
            log, f"[wiki_article: main entities: {', '.join(entities)}]"
        )
    else:
        entities = [question]
        _wiki_log(
            log,
            "[wiki_article: could not extract entities; using the question "
            "as the Wikipedia search term]",
        )

    output: list[str] = []
    for entity in entities:
        _wiki_log(
            log, f"[wiki_article: fetching the full article for {entity!r}]"
        )
        text, _ = _stream_wiki_full_article_script(
            entity, emit, strip_ansi=emit is not None
        )
        if text.strip() and not WIKI_NO_ARTICLE_LINE.search(text):
            if not text.endswith("\n"):
                text += "\n"
            output.append(text)
            continue

        article = fetch_wiki_summary_article(entity)
        if article is not None:
            fallback = _format_wiki_summary_article(article)
            _wiki_log(
                log,
                f"[wiki_article: using the summary fallback for {entity!r}]",
            )
            if emit is not None:
                emit(fallback)
            else:
                print(fallback, end="")
            output.append(fallback)
        else:
            message = f"No article found matching {entity!r}.\n"
            if emit is not None:
                emit(message)
            else:
                print(message, end="")
            output.append(message)

    return "".join(output)


def lookup_wikipedia(question: str) -> str:
    """Answer a narrow fact question or output the main entity summaries."""
    question = question.strip()
    if not question:
        print("Usage: /wiki <question>")
        return ""
    return answer_wiki_question(question)


def lookup_wiki_article(question: str) -> str:
    """Print the full Wikipedia article for every main entity of a question."""
    question = question.strip()
    if not question:
        print("Usage: /wiki_article <question>")
        return ""
    return print_wiki_full_articles(question)


def dictionary_surrounding_context(
    word: str, text: str | None, window: int = 20
) -> str:
    """Return up to ``window`` words before and after ``word`` in ``text``.

    Dictionary lookups only need the local usage of a word. Passing the
    entire latest answer to the model can distract it, so extract the first
    whole-word occurrence and keep a small surrounding window instead.
    """
    word = (word or "").strip()
    text = (text or "").strip()
    if not word or not text:
        return ""

    match = re.search(
        rf"(?<!\w){re.escape(word)}(?!\w)", text, flags=re.IGNORECASE
    )
    if match is None:
        return ""

    before = list(re.finditer(r"\S+", text[: match.start()]))
    if len(before) > window:
        start = before[-window].start()
        prefix = "… "
    else:
        start = 0
        prefix = ""

    after = list(re.finditer(r"\S+", text[match.end() :]))
    if len(after) > window:
        end = match.end() + after[window - 1].end()
        suffix = " …"
    else:
        end = len(text)
        suffix = ""

    snippet = text[start:end].strip()
    return f"{prefix}{snippet}{suffix}".strip()


def lookup_dictionary(word: str, latest_answer: str | None = None) -> None:
    """Print an English dictionary entry for ``word`` from a local model.

    Definitions are generated by Ollama using ``DICT_MODEL``; no external
    dictionary API is contacted. When ``latest_answer`` contains the word,
    only a 20-word window around it is sent to the model.
    """
    word = word.strip()
    if not word:
        print("Usage: /dict <word>")
        return

    context = dictionary_surrounding_context(word, latest_answer)
    print(f"\nDictionary ({DICT_MODEL}):")
    prompt = DICT_PROMPT.format(
        word=word,
        context=context or "(no surrounding context available)",
    )
    try:
        stream_answer(prompt, model=DICT_MODEL)
    except RuntimeError as exc:
        print(f"Dictionary lookup failed: {exc}", file=sys.stderr)


def show_latest_answer_usage(word: str, latest_answer: str | None) -> None:
    """Explain how ``word`` is used in the most recent answer, if it appears."""
    word = word.strip()
    if not word or not latest_answer:
        return

    pattern = re.compile(rf"(?<![\w]){re.escape(word)}(?![\w])", re.IGNORECASE)
    if not pattern.search(latest_answer):
        return

    print(f"\nHow {word!r} is used in the latest answer:")
    prompt = DICT_USAGE_PROMPT.format(word=word, latest_answer=latest_answer)
    try:
        stream_answer(prompt)
    except RuntimeError as exc:
        print(f"Could not explain the latest usage: {exc}", file=sys.stderr)


def run_learn_step(script: str, argv: list[str]) -> bool:
    """Run one pipeline script, echoing its output into the chat."""
    process = None
    try:
        # -u keeps the child's output unbuffered so it appears as it happens.
        process = subprocess.Popen(
            [sys.executable, "-u", str(SCRIPT_DIR / script), *argv],
            cwd=SCRIPT_DIR,
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            text=True,
            bufsize=1,
        )
        assert process.stdout is not None
        for line in process.stdout:
            print(line, end="", flush=True)
        returncode = process.wait()
    except KeyboardInterrupt:
        if process is not None:
            process.terminate()
            try:
                process.wait(timeout=5)
            except subprocess.TimeoutExpired:
                process.kill()
                process.wait()
        print("\n/youtube learn interrupted.", file=sys.stderr)
        return False
    except OSError as exc:
        print(f"/youtube learn: could not run {script}: {exc}", file=sys.stderr)
        return False

    if returncode != 0:
        print(
            f"/youtube learn aborted: {script} failed with exit code "
            f"{returncode}",
            file=sys.stderr,
        )
        return False
    return True


def learn_video(value: str) -> str | None:
    """Run the youtube_video_learn.py pipeline for a video hash/URL.

    Returns the normalized video id on success, or None when the input is
    invalid or a pipeline step fails.
    """
    try:
        import youtube_video_learn as learn
    except ImportError as exc:  # pragma: no cover - ships alongside
        print(
            f"/youtube learn is unavailable: cannot import "
            f"youtube_video_learn.py ({exc})",
            file=sys.stderr,
        )
        return None

    try:
        video_id = learn.extract_video_hash(value)
    except ValueError as exc:
        print(f"/youtube learn: {exc}", file=sys.stderr)
        return None

    print(f"/youtube learn: {video_id}")
    for script, script_args in learn.STEPS:
        argv = [arg.replace("<hash>", video_id) for arg in script_args]
        print(f"\n=== {script} {' '.join(argv)} ===", flush=True)
        if not run_learn_step(script, argv):
            return None

    print(f"\n/youtube learn: {video_id} added to the knowledge base.")
    return video_id


def refresh_corpus(current_chunks: list[Chunk]) -> list[Chunk] | None:
    """Handle /refresh: reload the global trunk corpus.

    Returns the freshly loaded chunks, or None when loading failed (in which
    case the current trunks stay loaded).
    """
    print("Refreshing the global knowledge corpus...")
    try:
        new_chunks = load_all_chunks()
    except SystemExit as exc:
        print(f"Cannot refresh the corpus: {exc}", file=sys.stderr)
        return None

    # Drop the old trunks only after the reload succeeded.
    current_chunks.clear()
    clear_bm25_cache()
    gc.collect()
    print(index_memory_report(new_chunks, build_index=False))
    return new_chunks


def extract_transcript_video_id(value: str) -> str | None:
    """Accept a bare video id or YouTube URL and return the video id."""
    value = value.strip()
    try:
        import youtube_video_learn as learn

        return learn.extract_video_hash(value)
    except (ImportError, ValueError):
        # youtube_video_learn.py may be unavailable; still accept a bare id.
        return value if re.fullmatch(r"[A-Za-z0-9_-]{11}", value) else None


def activate_transcript(
    video_id: str, current_chunks: list[Chunk]
) -> tuple[str, list[Chunk]] | None:
    """Load only one transcript's trunks and release the old ones."""
    print(f"Switching to transcript {video_id!r}, reloading trunks...")
    try:
        new_chunks = load_chunks(video_ids={video_id})
    except SystemExit as exc:
        print(
            f"Cannot switch to transcript {video_id!r}: {exc}",
            file=sys.stderr,
        )
        return None

    # Drop the old trunks only after the new transcript loaded successfully.
    current_chunks.clear()
    clear_bm25_cache()
    gc.collect()
    print(index_memory_report(new_chunks, build_index=False))
    return video_id, new_chunks


def switch_transcript(
    argument: str, current_chunks: list[Chunk]
) -> tuple[str, list[Chunk]] | None:
    """Handle /youtube transcript: pick one video's trunks and release the rest.

    The argument may be a video id, a YouTube URL, or a keyword. Keywords
    are searched against ./.chat/youtube_transcripts/*.title with ag, and the
    matches are offered as a <hash> <title> hinting list to select from.

    Returns the new video id and chunks, or None when the switch was cancelled
    or failed (in which case the current trunks stay loaded).
    """
    video_ids = available_video_ids()
    if not video_ids:
        print(f"No transcripts found in {OUTPUT_DIR}.", file=sys.stderr)
        return None

    value = argument.strip()
    try:
        if not value:
            video_id = prompt_for_video_id()
            return activate_transcript(video_id, current_chunks)

        # A bare video id or a YouTube URL still switches directly.
        video_id = extract_transcript_video_id(value)
        if video_id is not None:
            if video_id not in video_ids:
                print(
                    f"No trunk files for transcript {video_id!r}. "
                    "Use /youtube learn first."
                )
                return None
            return activate_transcript(video_id, current_chunks)

        # Otherwise treat the argument as an ag title keyword search.
        return search_and_switch_transcript(value, current_chunks)
    except SystemExit as exc:
        print(f"Transcript switch cancelled: {exc}", file=sys.stderr)
        return None


def search_transcript_titles(keyword: str) -> list[TranscriptMatch]:
    """Search ./.chat/youtube_transcripts/*.title files with ag."""
    keyword = keyword.strip()
    if not keyword:
        return []

    command = [
        "ag",
        "--nocolor",
        "--literal",
        "--vimgrep",
        "-G",
        r"\.title$",
        "--",
        keyword,
        str(OUTPUT_DIR),
    ]
    try:
        result = subprocess.run(command, capture_output=True, text=True)
    except FileNotFoundError:
        print(
            "Cannot search transcripts: the 'ag' (the_silver_searcher) "
            "command was not found.",
            file=sys.stderr,
        )
        return []
    except OSError as exc:
        print(f"Cannot search transcripts with ag: {exc}", file=sys.stderr)
        return []

    # ag exits 1 when there are no matches; anything else is an error.
    if result.returncode not in (0, 1):
        detail = result.stderr.strip() or f"ag exited with {result.returncode}"
        print(f"Transcript search failed: {detail}", file=sys.stderr)
        return []

    matches: dict[str, TranscriptMatch] = {}
    for line in result.stdout.splitlines():
        parsed = AG_MATCH_PATTERN.match(line)
        if not parsed:
            continue
        name = Path(parsed.group("path")).name
        if not name.endswith(".title"):
            continue
        video_id = name[: -len(".title")]
        title = parsed.group("title").strip()
        if video_id and video_id not in matches:
            matches[video_id] = TranscriptMatch(video_id, title)

    return sorted(matches.values(), key=lambda item: item.video_id.lower())


def select_transcript_match(
    matches: list[TranscriptMatch],
) -> TranscriptMatch | None:
    """Show the ag matches as a hinting list and let the user pick one."""
    print(f"\nFound {len(matches)} transcript(s):")
    for number, match in enumerate(matches, start=1):
        print(f"  {number:>4}. {match.video_id}  {match.title}")

    session = PromptSession(
        completer=TranscriptMatchCompleter(matches),
        complete_while_typing=False,
        multiline=False,
    )
    while True:
        try:
            choice = session.prompt(
                "Select transcript (number or id, Tab to complete, "
                "Enter to cancel): "
            ).strip()
        except (EOFError, KeyboardInterrupt):
            print()
            return None
        if not choice:
            print("Transcript selection cancelled.")
            return None

        if choice.isdigit():
            index = int(choice)
            if 1 <= index <= len(matches):
                return matches[index - 1]
            print(f"Choose a number between 1 and {len(matches)}.")
            continue

        exact = [match for match in matches if match.video_id == choice]
        if exact:
            return exact[0]

        by_prefix = [
            match for match in matches if match.video_id.startswith(choice)
        ]
        if len(by_prefix) == 1:
            return by_prefix[0]
        if len(by_prefix) > 1:
            print(
                f"{len(by_prefix)} video ids start with {choice!r}; "
                "type more characters."
            )
            continue

        by_title = [
            match for match in matches if choice.lower() in match.title.lower()
        ]
        if len(by_title) == 1:
            return by_title[0]
        if len(by_title) > 1:
            print(
                f"{len(by_title)} titles contain {choice!r}; "
                "type a number or a video id."
            )
            continue

        print(
            f"No match for {choice!r} in the list. "
            "Enter a number or video id."
        )


def search_and_switch_transcript(
    argument: str, current_chunks: list[Chunk]
) -> tuple[str, list[Chunk]] | None:
    """Handle /youtube search_transcript and /youtube transcript <keyword>."""
    keyword = argument.strip()
    if not keyword:
        print("Usage: /youtube search_transcript <keyword>")
        return None

    matches = search_transcript_titles(keyword)
    if not matches:
        print(f"No transcript titles matched {keyword!r}.")
        return None

    available = set(available_video_ids())
    usable = [match for match in matches if match.video_id in available]
    skipped = len(matches) - len(usable)
    if not usable:
        print(
            f"Found {len(matches)} title match(es) for {keyword!r}, but none "
            "have trunks. Use /youtube learn first."
        )
        return None
    if skipped:
        print(f"Skipping {skipped} title match(es) without trunks.")

    match = select_transcript_match(usable)
    if match is None:
        return None
    return activate_transcript(match.video_id, current_chunks)


def normalize_question(question: str) -> str:
    """Normalize a question for the answer cache key."""
    return " ".join(question.lower().split())


def make_cache_key(
    transcript: str | None,
    depth: int | None,
    max_context_tokens: int,
    answer_language: str | None,
    question: str,
    progressive: bool = False,
) -> tuple:
    return (
        transcript,
        depth,
        max_context_tokens,
        answer_language or "",
        progressive,
        normalize_question(question),
    )


def build_prompt(
    context: str,
    question: str,
    answer_language: str | None = None,
    prefix: str = "",
) -> str:
    """Fill the file-based 100-trunk prompt template.

    ``context`` is already a sequence of numbered <chunk> blocks. The template's
    placeholder <context> block and question slot are replaced, while the
    template's grounding, no-citation, conflict, and structured-response rules
    are preserved.
    """
    template = load_prompt_template()
    context_block = f"<context>\n{context}\n</context>"
    template, replaced = PROMPT_CONTEXT_PATTERN.subn(
        lambda _match: context_block, template, count=1
    )
    if not replaced:
        template = f"{template}\n\n{context_block}"

    if PROMPT_QUESTION_PLACEHOLDER in template:
        template = template.replace(
            PROMPT_QUESTION_PLACEHOLDER, question, 1
        )
    else:
        template = f"{template}\n\n{question}"

    # Keep the template's missing-data rule accurate when retrieval returns
    # fewer chunks than the template describes (small corpus or /depth auto).
    chunk_count = context.count('<chunk id="')
    if chunk_count:
        template = template.replace(
            "the 100 chunks", f"the {chunk_count} chunks", 1
        )

    if answer_language:
        instruction = LANGUAGE_INSTRUCTION.format(language=answer_language)
        if PROMPT_QUESTION_MARKER in template:
            template = template.replace(
                PROMPT_QUESTION_MARKER,
                f"{instruction}{PROMPT_QUESTION_MARKER}",
                1,
            )
        else:
            template = f"{instruction}{template}"
    return prefix + template


def answer(
    question: str,
    chunks: list[Chunk],
    depth: int | None = DEFAULT_DEPTH,
    translate_answer: bool = False,
    max_context_tokens: int = MAX_CONTEXT_TOKENS,
    answer_language: str | None = None,
    cache: AnswerCache | None = None,
    cache_key: tuple | None = None,
    stats: dict | None = None,
    progressive: bool = False,
    prompt_prefix: str = "",
    model: str | None = None,
    think: bool = False,
    use_all_chunks: bool = False,
    prepare_selected: Callable[
        [list[tuple[Chunk, float]], list[tuple[Chunk, float]]],
        list[tuple[Chunk, float]],
    ]
    | None = None,
    emit: Callable[[str], None] | None = None,
    log: Callable[[str], None] | None = None,
) -> tuple[str | None, list[tuple[Chunk, float]]]:
    """Retrieve context for the question and stream the model's answer.

    ``emit`` receives streamed answer text and ``log`` receives status lines.
    When they are omitted, output goes to stdout/stderr exactly as before.
    ``model`` overrides the default chat model and ``think`` optionally
    enables the model's reasoning mode (disabled by default).
    ``use_all_chunks`` skips BM25 and uses every supplied chunk in order;
    callers use it for a prebuilt synthetic context (an article digest).
    ``prepare_selected`` receives the BM25 selection and may append parent
    summaries or neighboring paragraphs before the context is built; this is
    used by /article's section-level expansion.
    """
    def _emit(text: str) -> None:
        if emit is not None:
            if text:
                emit(text)
        else:
            print(text, end="", flush=True)

    def _log(message: str) -> None:
        if log is not None:
            log(message)
        else:
            print(message, flush=True)

    def _warn(message: str) -> None:
        if log is not None:
            log(message)
        else:
            print(message, file=sys.stderr)

    answer_model = model or CHAT_MODEL

    if cache is not None and cache_key is not None:
        cached = cache.get(cache_key)
        if cached is not None:
            cached_answer = strip_chunk_citations(cached["answer"])
            cached_answer = strip_supporting_evidence(cached_answer)
            cached["answer"] = cached_answer
            _emit(cached_answer + "\n")
            if stats is not None:
                stats.update(
                    {
                        "cached": True,
                        "retrieval": 0.0,
                        "expansion": 0.0,
                        "bm25": 0.0,
                        "filter": 0.0,
                        "candidates": 0,
                        "context_build": 0.0,
                        "prompt_build": 0.0,
                        "context_tokens": cached["context_tokens"],
                        "trunks": len(cached["sources"]),
                        "prefill": 0.0,
                        "load": 0.0,
                        "request_ttft": 0.0,
                        "end_to_end_ttft": 0.0,
                        "prompt_tokens": 0,
                        "prompt_tokens_cached": 0,
                        "response_tokens": 0,
                        "eval_seconds": 0.0,
                        "total": 0.0,
                    }
                )
            return cached_answer, cached["sources"]

    started = time.monotonic()
    search_question = question
    expansion = ""
    expansion_started = started
    expansion_done = started
    if not use_all_chunks:
        # The caller already selected the evidence, so there is no BM25 query
        # to translate or expand. Skipping both avoids needless small-model
        # calls before streaming an iterative or prebuilt context.
        if contains_simplified_chinese(question):
            try:
                search_question = translate_to_english(question)
            except RuntimeError as exc:
                _warn(
                    f"Warning: could not translate the question for BM25 "
                    f"retrieval: {exc}"
                )

        expansion_started = time.monotonic()
        try:
            expansion = expand_query(search_question)
        except RuntimeError as exc:
            expansion = ""
            _warn(
                f"Warning: query expansion failed; searching with the original "
                f"question only: {exc}"
            )
        expansion_done = time.monotonic()

    effective_budget = max_context_tokens
    if (
        depth is None
        and max_context_tokens > 0
        and LIST_QUERY_PATTERN.search(question)
    ):
        effective_budget = max(max_context_tokens, LIST_CONTEXT_TOKENS)

    bm25_seconds = 0.0
    filter_seconds = 0.0

    def retrieve_for(
        candidate_chunks: list[Chunk],
    ) -> tuple[
        list[tuple[Chunk, float]],
        list[tuple[Chunk, float]],
        bool,
        bool,
    ]:
        """Return (selected, raw BM25, filter applied, filter returned NONE)."""
        nonlocal bm25_seconds, filter_seconds
        bm25_started = time.monotonic()
        raw = top_chunks(
            search_question,
            candidate_chunks,
            limit=depth,
            max_context_tokens=effective_budget,
            expansion=expansion,
        )
        bm25_seconds += time.monotonic() - bm25_started
        if not raw:
            return [], [], False, False

        should_filter = (
            LLM_FILTER_ENABLED
            and not LIST_QUERY_PATTERN.search(question)
            and FILTER_MIN_CANDIDATES <= len(raw) <= FILTER_MAX_CANDIDATES
        )
        if not should_filter:
            return raw, raw, False, False

        filter_started = time.monotonic()
        filtered = filter_candidates(search_question, raw)
        filter_seconds += time.monotonic() - filter_started
        if filtered is None:
            return raw, raw, True, False
        if not filtered:
            return [], raw, True, True
        # Never let the small filter discard the strongest BM25 evidence.
        # Keep its choices plus the top BM25 candidates, in score order.
        filtered_ids = {id(chunk) for chunk, _score in filtered}
        merged = [
            item
            for number, item in enumerate(raw)
            if number < FILTER_ALWAYS_KEEP or id(item[0]) in filtered_ids
        ]
        return merged, raw, True, False

    if use_all_chunks:
        # The caller has already selected/constructed the context. In
        # particular, a single synthetic article digest cannot be scored by
        # BM25 (one document has no positive IDF), so keep it verbatim.
        selected = [(chunk, 0.0) for chunk in chunks]
        raw_selected = selected
        filter_applied = False
        filter_empty = False
        fallback_used = False
    else:
        selected, raw_selected, filter_applied, filter_empty = retrieve_for(
            chunks
        )
        fallback_used = False

    if (
        not use_all_chunks
        and not selected
        and raw_selected
    ):
        # The small question filter can be overly strict. Keep a minimal BM25
        # context rather than turning a borderline question into a false
        # refusal.
        selected = raw_selected[:FILTER_FALSE_NEGATIVE_KEEP]
        _log(
            "[relevance filter returned no candidates; keeping the top "
            f"{len(selected)} BM25 match(es)]"
        )

    bm25_selected_count = len(selected)
    if selected and prepare_selected is not None:
        try:
            prepared = prepare_selected(selected, raw_selected)
        except Exception as exc:  # A failed expansion must not lose context.
            _warn(f"Warning: context expansion failed: {exc}")
            prepared = selected
        if prepared:
            selected = prepared

    retrieval_done = time.monotonic()
    if not selected:
        _log("No trunks available to use as context.")
        return None, []

    context = build_context([chunk for chunk, _ in selected])
    context_done = time.monotonic()
    if not context:
        _warn("I could not read any matching trunk contents.")
        return None, []
    context_tokens = estimate_tokens(context)
    candidate_count = len(raw_selected)

    if depth is not None:
        depth_label = "default" if depth == DEFAULT_DEPTH else "explicit"
        if use_all_chunks:
            _log(
                f"[using {len(selected)} prebuilt context trunk(s); "
                "BM25 skipped]"
            )
        else:
            _log(
                f"[using {len(selected)} context trunk(s) "
                f"({bm25_selected_count} BM25 match(es) of "
                f"{candidate_count}, depth {depth}, {depth_label})]"
            )
    else:
        budget = (
            "disabled"
            if effective_budget <= 0
            else f"{effective_budget:,} tokens"
        )
        _log(
            f"[auto retrieval: {len(selected)} of {candidate_count} "
            f"BM25 trunk(s) selected, budget {budget}]"
        )

    _log(
        f"[context size: ~{context_tokens:,} token(s) estimated "
        f"across {len(selected)} trunk(s); "
        f"context window: {ANSWER_NUM_CTX:,} token(s)]"
    )
    metrics: dict = {}
    prompt_done = context_done
    if progressive and len(selected) > PROGRESSIVE_FIRST_PASS:
        first_selected = selected[:PROGRESSIVE_FIRST_PASS]
        expansion_selected = selected[PROGRESSIVE_FIRST_PASS:]
        first_context = build_context([chunk for chunk, _ in first_selected])
        first_prompt = build_prompt(
            first_context, question, answer_language, prompt_prefix
        )
        prompt_done = time.monotonic()
        first_metrics: dict = {}
        first_response = stream_answer(
            first_prompt,
            model=answer_model,
            translate_answer=translate_answer,
            metrics=first_metrics,
            emit=emit,
            think=think,
        )
        expansion_metrics: dict = {}
        expansion_response = ""
        expansion_context = build_context(
            [chunk for chunk, _ in expansion_selected],
            start_index=PROGRESSIVE_FIRST_PASS + 1,
        )
        if expansion_context.strip():
            _log(
                f"\n[expanding context: adding {len(expansion_selected)} "
                "trunk(s) while keeping the answer above]\n"
            )
            language_instruction = (
                LANGUAGE_INSTRUCTION.format(language=answer_language)
                if answer_language
                else ""
            )
            expansion_prompt = PROGRESSIVE_PROMPT.format(
                context=expansion_context,
                language_instruction=language_instruction,
                answer_so_far=first_response,
            )
            expansion_response = stream_answer(
                expansion_prompt,
                model=answer_model,
                translate_answer=translate_answer,
                metrics=expansion_metrics,
                emit=emit,
                think=think,
            )
        response = first_response
        if expansion_response.strip():
            response = f"{first_response}\n\n{expansion_response}"
        metrics = first_metrics
        metrics["expansion_prompt_eval_count"] = expansion_metrics.get(
            "prompt_eval_count", 0
        )
        metrics["expansion_prompt_eval_cached_count"] = (
            expansion_metrics.get("prompt_eval_cached_count", 0)
        )
        metrics["expansion_prompt_eval_duration"] = expansion_metrics.get(
            "prompt_eval_duration", 0.0
        )
        metrics["expansion_eval_count"] = expansion_metrics.get(
            "eval_count", 0
        )
        metrics["expansion_eval_duration"] = expansion_metrics.get(
            "eval_duration", 0.0
        )
    else:
        prompt = build_prompt(context, question, answer_language, prompt_prefix)
        prompt_done = time.monotonic()
        response = stream_answer(
            prompt,
            model=answer_model,
            translate_answer=translate_answer,
            metrics=metrics,
            emit=emit,
            think=think,
        )
    finished = time.monotonic()
    response = strip_supporting_evidence(strip_chunk_citations(response))

    retrieval_seconds = retrieval_done - started
    prefill_seconds = metrics.get("prompt_eval_duration", 0.0)
    load_seconds = metrics.get("load_duration", 0.0)
    request_ttft = metrics.get("request_ttft", 0.0)
    end_to_end_ttft = retrieval_seconds + request_ttft

    if cache is not None and cache_key is not None:
        cache.put(cache_key, response, selected, context_tokens)
    if stats is not None:
        stats.update(
            {
                "cached": False,
                "retrieval": retrieval_done - started,
                "expansion": expansion_done - expansion_started,
                "bm25": bm25_seconds,
                "filter": filter_seconds,
                "candidates": candidate_count,
                "filter_applied": filter_applied,
                "fallback": fallback_used,
                "context_build": context_done - retrieval_done,
                "prompt_build": prompt_done - context_done,
                "context_tokens": context_tokens,
                "trunks": len(selected),
                "prefill": prefill_seconds,
                "load": load_seconds,
                "request_ttft": request_ttft,
                "end_to_end_ttft": end_to_end_ttft,
                "prompt_tokens": metrics.get("prompt_eval_count", 0)
                + metrics.get("expansion_prompt_eval_count", 0),
                "prompt_tokens_cached": metrics.get(
                    "prompt_eval_cached_count", 0
                )
                + metrics.get("expansion_prompt_eval_cached_count", 0),
                "response_tokens": metrics.get("eval_count", 0)
                + metrics.get("expansion_eval_count", 0),
                "eval_seconds": metrics.get("eval_duration", 0.0)
                + metrics.get("expansion_eval_duration", 0.0),
                "total": finished - started,
            }
        )
    return response, selected


def format_benchmark_line(stats: dict) -> str:
    """Format the retrieval/prefill/TTFT metrics for /benchmark."""
    if stats.get("cached"):
        return (
            f"[cache hit | context ~{stats.get('context_tokens', 0):,} "
            "token(s) estimated | no retrieval or prefill]"
        )
    return (
        f"[retrieval {stats.get('retrieval', 0.0):.2f} s "
        f"(expand {stats.get('expansion', 0.0):.2f} s, "
        f"bm25 {stats.get('bm25', 0.0):.2f} s, "
        f"filter {stats.get('filter', 0.0):.2f} s) | "
        f"trunks {stats.get('trunks', 0)}/{stats.get('candidates', 0)} | "
        f"context {stats.get('context_build', 0.0):.3f} s | "
        f"prompt {stats.get('prompt_build', 0.0):.3f} s | "
        f"prefill {stats.get('prefill', 0.0):.2f} s | "
        f"load {stats.get('load', 0.0):.2f} s | "
        f"TTFT {stats.get('end_to_end_ttft', 0.0):.2f} s | "
        f"prompt_tokens {stats.get('prompt_tokens', 0):,} actual "
        f"(cached {stats.get('prompt_tokens_cached', 0):,}) | "
        f"answer_tokens {stats.get('response_tokens', 0):,} | "
        f"context {stats.get('context_tokens', 0):,} estimated | "
        f"total {stats.get('total', 0.0):.2f} s]"
    )


def show_sources(sources: list[tuple[Chunk, float]]) -> None:
    """Print the source list for the latest answer."""
    if not sources:
        print("No sources available yet. Ask a question first.")
        return
    print("Sources for the latest answer:")
    for index, (chunk, score) in enumerate(sources, start=1):
        print(f"  [Chunk {index}] {chunk.label} (bm25 {score:.4f})")


def prewarm() -> None:
    """Load the expansion/filter and chat models and keep them resident.

    Warm each model with the same ``num_ctx`` the real calls use. Ollama
    reloads a model whenever the requested context size changes, so warming
    with the default context used to guarantee a slow reload on the first
    question (and another one whenever the small and answer models alternated).
    """
    targets = (
        (EXPANSION_MODEL, EXPANSION_NUM_CTX),
        (FILTER_MODEL, EXPANSION_NUM_CTX),
        (CHAT_MODEL, ANSWER_NUM_CTX),
    )
    for model, num_ctx in dict.fromkeys(targets):
        try:
            requests.post(
                f"{OLLAMA_HOST}/api/generate",
                json={
                    "model": model,
                    "prompt": "",
                    "stream": False,
                    "keep_alive": OLLAMA_KEEP_ALIVE,
                    "options": {"num_predict": 1, "num_ctx": num_ctx},
                },
                timeout=(2, 180),
            )
        except requests.RequestException:
            # Prewarming is best-effort; the first real request still works.
            pass


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Interactive BM25 RAG chatbot over YouTube transcript trunks."
    )
    parser.add_argument(
        "--max-context-tokens",
        type=int,
        default=MAX_CONTEXT_TOKENS,
        metavar="N",
        help="estimated-token budget for adaptive retrieval, used only with "
        "/depth auto "
        f"(default: {MAX_CONTEXT_TOKENS}; 0 disables the budget)",
    )
    parser.add_argument(
        "--progressive",
        action="store_true",
        help="stream a fast first pass, then append an expansion pass over the "
        "remaining selected chunks (default: single coherent call)",
    )
    return parser.parse_args(argv)


def main() -> None:
    args = parse_args()
    # Warm the models while the trunks are being loaded; do not block input.
    threading.Thread(target=prewarm, daemon=True).start()

    chunks = load_all_chunks()
    print(index_memory_report(chunks, build_index=False))
    transcript: str | None = None
    latest_answer: str | None = None
    latest_sources: list[tuple[Chunk, float]] = []
    latest_benchmark: dict | None = None
    # When true, the retrieval/prefill/TTFT line is printed after each answer.
    benchmark_mode = False
    # The default is a fixed top-20 BM25 selection. `/depth auto` enables
    # adaptive retrieval instead.
    depth: int | None = DEFAULT_DEPTH
    max_context_tokens = args.max_context_tokens
    cache = AnswerCache()

    history = PlainFileHistory(HISTORY_FILE)
    print(
        f"Chat history: {history_count(HISTORY_FILE)} saved entries "
        f"(questions + commands) in {HISTORY_FILE}"
    )
    session = make_prompt_session(history)
    print(
        f"RAG chatbot ready ({EXPANSION_MODEL} expansion/filter + "
        f"{CHAT_MODEL} chat, global corpus, top {depth} BM25 matching "
        "trunks "
        f"by default, prompt {PROMPT_TEMPLATE_PATH.name or 'fallback'}). "
        "Type /exit to quit."
    )
    print(
        "Simplified Chinese questions are translated into English for BM25 "
        "retrieval and answered in Simplified Chinese."
    )
    print(
        "Vim mode: Esc = normal, i = insert, v/V = visual, "
        "dd/dw = delete, u = undo. Up/Down = question + command history."
    )
    print(
        "Commands: /refresh, /youtube transcript <hash|keyword>, "
        "/youtube learn <hash|url>, /ask <question>, /wiki <question>, "
        "/wiki_article <question>, /dict <word>, /depth <n|auto>, "
        "/memory, /status, /sources, /benchmark [on|off], /exit. "
        "Tab completes them."
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
                print(index_memory_report(chunks))
            elif name == "/refresh":
                refreshed = refresh_corpus(chunks)
                if refreshed is not None:
                    chunks = refreshed
                    transcript = None
                    cache.clear()
                    print("Global corpus refreshed.")
            elif name == "/youtube":
                subcommand, _, subargument = argument.partition(" ")
                subcommand = subcommand.strip().lower()
                value = subargument.strip()
                if subcommand in {"transcript", "search_transcript"}:
                    if subcommand == "transcript":
                        switched = switch_transcript(value, chunks)
                    else:
                        switched = search_and_switch_transcript(value, chunks)
                    if switched is not None:
                        transcript, chunks = switched
                        cache.clear()
                        print(
                            f"Now answering only from transcript {transcript!r}."
                        )
                    continue
                if subcommand != "learn":
                    print(
                        "Usage: /youtube learn <youtube_video_hash|url> | "
                        "transcript <hash|URL|keyword> | "
                        "search_transcript <keyword>"
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

                video_id = learn_video(value)
                if video_id is None:
                    continue
                clear_bm25_cache()
                cache.clear()

                if transcript is not None:
                    if video_id == transcript:
                        print(
                            f"Reloading trunks for transcript "
                            f"{transcript!r}..."
                        )
                        try:
                            new_chunks = load_chunks(
                                video_ids={transcript}
                            )
                        except SystemExit as exc:
                            print(
                                f"Could not reload trunks: {exc}",
                                file=sys.stderr,
                            )
                        else:
                            chunks.clear()
                            chunks = new_chunks
                            clear_bm25_cache()
                            gc.collect()
                            print(
                                index_memory_report(
                                    chunks, build_index=False
                                )
                            )
                    else:
                        print(
                            f"Still answering only from transcript "
                            f"{transcript!r}; the loaded trunks are "
                            "unchanged."
                        )
                elif transcript is None:
                    print("Reloading the global knowledge corpus...")
                    try:
                        new_chunks = load_all_chunks()
                    except SystemExit as exc:
                        print(
                            f"Could not reload trunks: {exc}",
                            file=sys.stderr,
                        )
                    else:
                        chunks.clear()
                        chunks = new_chunks
                        clear_bm25_cache()
                        gc.collect()
                        print(
                            index_memory_report(chunks, build_index=False)
                        )
            elif name == "/ask":
                quick_question = argument.strip()
                if not quick_question:
                    print("Usage: /ask <question>")
                else:
                    print("\nAssistant: ", end="", flush=True)
                    try:
                        quick_answer = answer_simple_question(
                            quick_question,
                            translate_answer=contains_simplified_chinese(
                                quick_question
                            ),
                        )
                    except RuntimeError as exc:
                        print(f"\nQuick answer failed: {exc}")
                    else:
                        latest_answer = quick_answer
                        latest_sources = []
                        print()
            elif name == "/wiki":
                wiki_response = lookup_wikipedia(argument)
                if wiki_response:
                    latest_answer = wiki_response
                    latest_sources = []
            elif name == "/wiki_article":
                wiki_article_response = lookup_wiki_article(argument)
                if wiki_article_response:
                    latest_answer = wiki_article_response
                    latest_sources = []
            elif name == "/dict":
                lookup_dictionary(argument, latest_answer)
            elif name == "/status":
                print(
                    chat_status_report(
                        transcript,
                        chunks,
                        depth,
                        max_context_tokens,
                        len(cache),
                    )
                )
            elif name == "/sources":
                show_sources(latest_sources)
            elif name == "/benchmark":
                value = argument.strip().lower()
                if value in {"", "last", "show"}:
                    if latest_benchmark is None:
                        print(
                            "No benchmark data yet. Ask a question first, "
                            "or use /benchmark on to show metrics after "
                            "each answer."
                        )
                    else:
                        print(format_benchmark_line(latest_benchmark))
                elif value in {"on", "enable", "enabled"}:
                    benchmark_mode = True
                    print(
                        "Benchmark display: on (metrics shown after each "
                        "answer)."
                    )
                    if latest_benchmark is not None:
                        print(format_benchmark_line(latest_benchmark))
                elif value in {"off", "disable", "disabled"}:
                    benchmark_mode = False
                    print("Benchmark display: off.")
                else:
                    print(
                        "Usage: /benchmark [on|off] (no argument shows the "
                        "latest answer's metrics)"
                    )
            elif name == "/depth":
                value = argument.strip().lower()
                if not value:
                    current = "auto" if depth is None else str(depth)
                    print(f"Retrieval depth: {current}")
                    print("Usage: /depth <number of trunks> | /depth auto")
                    print(
                        f"Default: {DEFAULT_DEPTH} top BM25 matching trunks; "
                        "/depth auto enables adaptive retrieval."
                    )
                elif value in {"auto", "adaptive", "0"}:
                    depth = None
                    cache.clear()
                    print(
                        "Retrieval depth: auto (BM25 score elbow, token "
                        "budget)."
                    )
                elif value in {"default", "reset"}:
                    depth = DEFAULT_DEPTH
                    cache.clear()
                    print(
                        f"Retrieval depth: top {depth} BM25 matching "
                        "trunk(s) (default)."
                    )
                else:
                    try:
                        new_depth = int(value)
                    except ValueError:
                        new_depth = 0
                    if new_depth < 1:
                        print(
                            f"Invalid depth: {argument.strip()!r}. "
                            "Use a positive integer, 'auto', or 'default'."
                        )
                    else:
                        depth = new_depth
                        cache.clear()
                        print(
                            f"Retrieval depth: top {depth} BM25 matching "
                            "trunk(s)."
                        )
            else:
                print(
                    f"Unknown command: {name}. "
                    "Try /refresh, "
                    "/youtube transcript <hash|keyword>, "
                    "/youtube learn <hash|url>, /ask <question>, /wiki <question>, "
                    "/wiki_article <question>, /dict <word>, "
                    "/depth <n|auto>, /memory, /status, /sources, "
                    "/benchmark [on|off], /exit."
                )
            continue

        answer_language: str | None = None
        if contains_simplified_chinese(question):
            answer_language = "Simplified Chinese"
            print(
                "[Simplified Chinese detected; translating the question "
                "into English for BM25 retrieval and answering directly "
                "in Simplified Chinese]",
                flush=True,
            )

        print("\nAssistant: ", end="", flush=True)
        cache_key = make_cache_key(
            transcript,
            depth,
            max_context_tokens,
            answer_language,
            question,
            args.progressive,
        )
        benchmark_stats: dict = {}
        try:
            response, sources = answer(
                question,
                chunks,
                depth=depth,
                max_context_tokens=max_context_tokens,
                answer_language=answer_language,
                cache=cache,
                cache_key=cache_key,
                stats=benchmark_stats,
                progressive=args.progressive,
            )
        except Exception as exc:
            print(f"\nError: {exc}", file=sys.stderr)
        else:
            if response is not None:
                latest_answer = response
                latest_sources = sources
                latest_benchmark = benchmark_stats
                if benchmark_mode:
                    print(format_benchmark_line(benchmark_stats))


if __name__ == "__main__":
    main()
