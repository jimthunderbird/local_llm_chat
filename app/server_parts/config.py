"""Model names, feature limits, and shared runtime configuration."""

from __future__ import annotations

from .core import *

# Follow-up handling. The browser backend keeps a short server-side
# conversation so a question such as "what does he think about AI?" or
# "when does it open" can be rewritten into a standalone retrieval question
# before BM25 runs. A completely unrelated question starts a fresh context.
CONVERSATION_MAX_TURNS = 6

CONVERSATION_ANSWER_CHARS = 600

# Long sessions are compacted instead of growing the prompt. The newest turns
# stay verbatim; older turns are folded into a compact running summary by the
# small helper model. Compaction is deliberately infrequent: it triggers a few
# turns after the verbatim window is full, then keeps only a short recent tail.
CONVERSATION_COMPACT_AFTER_TURNS = 12

CONVERSATION_RECENT_TURNS_AFTER_COMPACT = 4

CONVERSATION_SUMMARY_MAX_CHARS = 2800  # roughly 700 English tokens

CONVERSATION_SUMMARY_NUM_PREDICT = 320

# One model handles follow-up rewriting as well; keeping it identical to the
# answer model avoids loading a second model for a short classification call.
CONVERSATION_MODEL = chat.CHAT_MODEL

# Ollama unloads a model after OLLAMA_KEEP_ALIVE without traffic, which makes
# the first question after an idle gap pay the multi-second model load. A tiny
# request every few minutes refreshes that timer. Set
# OLLAMA_KEEPALIVE_WATCHDOG=0 to disable it, or OLLAMA_KEEPALIVE_INTERVAL to
# change the interval in seconds (minimum 60).
KEEPALIVE_WATCHDOG_ENABLED = os.environ.get(
    "OLLAMA_KEEPALIVE_WATCHDOG", "1"
).strip().lower() not in {"0", "false", "no", "off"}

try:
    KEEPALIVE_WATCHDOG_INTERVAL_SECONDS = max(
        60, int(os.environ.get("OLLAMA_KEEPALIVE_INTERVAL", "300"))
    )
except ValueError:
    KEEPALIVE_WATCHDOG_INTERVAL_SECONDS = 300

# Code-generation uses ornith-1.5:35b, matching the answer and math tutor models.
# The small LFM3 model remains the helper model for short internal calls.
CODE_GENERATION_MODEL = chat.ANSWER_MODEL

# For Go, Rust, and PHP, every chat answer and every generated code sample
# uses the dedicated programming model instead of the general transcript chat
# model. Keeping it in one constant makes the model visible in the status bar,
# the benchmark line, and the answer-source badges.
PROGRAMMING_MODEL = CODE_GENERATION_MODEL

# Coding mode. Questions that ask for code samples about a programming
# language feature, structure, or architecture are answered by the dedicated
# code-generation model without its reasoning (thinking) mode. The regular
# chat model remains the default for everything else.
CODING_MODE_MODEL = CODE_GENERATION_MODEL

CODING_MODE_PROMPT_PREFIX = """### CODING MODE (highest priority)
The user is asking for a code sample about a programming-language feature,
structure, or architecture. Answer as an experienced software engineer.
- You MAY use your own programming knowledge in addition to the retrieved
  context. If the context is irrelevant or incomplete, do not refuse; still
  provide a correct, self-contained example.
- This overrides the retrieval template's "Missing Data" rule: when the
  user asks for code, never answer that the provided context is insufficient.
  Generate the code from your own knowledge and use whatever excerpts are
  available as hints.
- Show complete, runnable code in fenced blocks with the language tag
  (for example ```python).
- Briefly explain the key idea, then the code, then any complexity or
  trade-offs that matter.
- Do not invent citations or mention chunk IDs. Never write references such
  as (chunk 14), [Chunk 11], or "chunk 7"; state the facts directly.

"""

# GitHub code fallback. When coding mode produces no usable answer, the
# server searches GitHub's public API and constructs an answer from two
# related code snippets. GitHub's code-search endpoint requires
# authentication; when GITHUB_TOKEN/GH_TOKEN is not set, the fallback searches
# repositories and reads source files through the unauthenticated core API.
GITHUB_API_BASE = "https://api.github.com"

GITHUB_RAW_BASE = "https://raw.githubusercontent.com"

GITHUB_TOKEN = (
    os.environ.get("GITHUB_TOKEN") or os.environ.get("GH_TOKEN") or ""
).strip()

GITHUB_HEADERS = {
    "Accept": "application/vnd.github+json",
    "User-Agent": "youtube-local-chat/1.0",
    "X-GitHub-Api-Version": "2022-11-28",
}

GITHUB_TIMEOUT = (4, 12)

GITHUB_TOTAL_TIMEOUT = 25.0

GITHUB_SNIPPET_LIMIT = 2

GITHUB_SNIPPET_MAX_LINES = 80

GITHUB_SNIPPET_MAX_CHARS = 6000

GITHUB_REPOSITORY_LIMIT = 6

GITHUB_MAX_QUERY_TERMS = 8

GITHUB_MAX_FILE_BYTES = 300_000

# Official documentation is used when a question names Go, Rust, or PHP.
# The language is detected from the question itself instead of a tag, so
# every question searches the one global corpus.
GO_LANGUAGE = "Go"

RUST_LANGUAGE = "Rust"

PHP_LANGUAGE = "PHP"

GOLANG_DOC_SEARCH_TIMEOUT = 900

RUST_DOC_SEARCH_TIMEOUT = 900

PHP_DOC_SEARCH_TIMEOUT = 900

# The browser shows a live "answer source" badge while an answer streams. For
# documentation-backed languages the answer starts in the local Ollama model
# and may then be replaced or extended by the official language documentation,
# so the server emits a "source" SSE event at each transition.
SOURCE_OLLAMA = "ollama"

SOURCE_DOCUMENTATION = "documentation"

SOURCE_MERGED = "merged"

DOCUMENTATION_LANGUAGES = {GO_LANGUAGE, RUST_LANGUAGE, PHP_LANGUAGE}

# Documentation-backed languages also pin the language of every generated code
# sample, even when the question does not name a language again.
LANGUAGE_CODE_FENCES = {
    GO_LANGUAGE: "go",
    RUST_LANGUAGE: "rust",
    PHP_LANGUAGE: "php",
}

# The programming_docs_map from ./prompts/programming.prompt. When a question
# asks for code in a documentation-backed language, the answer path loads that
# language's official documentation before any local code generation. Questions
# that only discuss a language or feature do not load these documents. Every
# entry names the documentation script whose cached index and chunker are
# reused, the reference URL to cite, and the local knowledge-note directory
# that supplements the docs.
PROGRAMMING_DOCS_MAP = {
    GO_LANGUAGE: {
        "reference": "https://go.dev/doc/",
        "source_label": "go.dev/doc",
        "module": "golang_doc_search",
        "knowledge_tag": "golang",
    },
    RUST_LANGUAGE: {
        "reference": "https://doc.rust-lang.org/reference",
        "source_label": "doc.rust-lang.org",
        "module": "rust_doc_search",
        "knowledge_tag": "rust",
    },
    PHP_LANGUAGE: {
        "reference": "https://www.php.net/manual/en/",
        "source_label": "www.php.net/manual/en",
        "module": "php_doc_search",
        "knowledge_tag": "php",
    },
}

# Bound the documentation that is folded into a normal chat prompt. The
# excerpts are a reference next to the trunks, not a replacement for them.
PROGRAMMING_DOCS_TOP_K = 6

PROGRAMMING_DOCS_CHUNKS_PER_PAGE = 2

PROGRAMMING_DOCS_MAX_CHARS = 6_000

_PROGRAMMING_DOCS_INDEX_CACHE: dict[str, tuple] = {}

_PROGRAMMING_DOCS_INDEX_LOCK = threading.Lock()
