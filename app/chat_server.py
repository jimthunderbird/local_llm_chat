#!/usr/bin/env python3
"""HTTP API server for the browser front end in app/chat.html.

The server reuses chat.py for chunk loading, BM25 retrieval, prompt building,
Ollama streaming, tag/transcript/article scoping, history, /memory,
/status, /sources, /benchmark, /learn, /learn_topic, /learn_playlist,
/tag_gen, /news, /wiki, /wiki_article, /article, /dict, and
/ask. The browser is a pure HTML/CSS/JavaScript single-page app served from
./app/chat.html.

Usage:
    python3 app/chat_server.py [--host 127.0.0.1] [--port 8765] [--tag japan]
                                  [--max-context-tokens N] [--progressive]

Endpoints:
    GET  /                       app/chat.html
    GET  /api/state              current chat state + command list + history
    GET  /api/autocomplete       ?kind=commands|tags|transcripts|paths&q=...
    GET  /api/wiki?word=...      JSON Wikipedia summary
    GET  /api/transcripts?q=...  ag title search results
    POST /api/ask                SSE: question or slash-command stream
                                  (answers include a per-paragraph
                                  source-video annotation event; tags other
                                  than "rust", "golang", and "php" automatically
                                  retry other tags when the active tag cannot
                                  answer, while "rust", "golang", and "php"
                                  consult the official documentation instead. For
                                  those tags a code question consults the
                                  official documentation first, before any local
                                  code generation; a "source" event says whether
                                  the currently streaming answer is coming from
                                  the local Ollama model or the official docs.
                                  The tag also pins the generated language:
                                  code in the "rust" tag is always Rust, code in
                                  the "golang" tag is always Go, and code in the
                                  "php" tag is always PHP. /news
                                  streams "news" events from CNN, KQED, and
                                  SF Bay View).
    POST /api/dict               SSE: dictionary + latest-answer usage stream
    POST /api/translate          SSE: paragraph-by-paragraph Simplified Chinese
                                  translation of an answer
    POST /api/explain_line       SSE: explain one line of a code snippet from a
                                  documentation-backed tag (rust/golang/php)
    POST /api/snippet_question   SSE: answer a free-form question about a whole
                                  code snippet (all tags)
"""

from __future__ import annotations

import argparse
import codecs
import gc
import hashlib
import html
import io
import json
import math
import os
import re
import selectors
import subprocess
import sys
import threading
import time
from contextlib import redirect_stderr, redirect_stdout
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Callable, NamedTuple
from urllib.parse import parse_qs, quote, urlparse
from xml.sax.saxutils import escape as xml_escape

try:
    import requests
except ImportError as exc:  # pragma: no cover - dependency guard
    sys.exit(f"Missing dependency: {exc.name}. Install with: pip install requests")

# The server lives in ./app, while chat.py and its data directories live in the
# repository root. Resolve both and make the root importable when run as a script.
APP_DIR = Path(__file__).resolve().parent
SCRIPT_DIR = APP_DIR.parent
if str(SCRIPT_DIR) not in sys.path:
    sys.path.insert(0, str(SCRIPT_DIR))

import chat
from paragraph_utils import split_paragraphs
from doc_answer_cleanup import (
    CodeFenceRepairFilter,
    InsufficientContextFilter,
    repair_code_fences,
    strip_leading_insufficient_context,
)

CHAT_HTML = APP_DIR / "chat.html"
MAX_REQUEST_BYTES = 2 * 1024 * 1024
PATH_COMPLETION_LIMIT = 50

WEB_COMMANDS = {
    **chat.COMMANDS,
    "/help": "show commands and keyboard shortcuts",
    "/new": "start a fresh conversation (clears follow-up context)",
    "/learn_topic": "search YouTube for <topic> and learn the top 3 videos",
    "/learn_playlist": "learn every video in a YouTube playlist",
    "/tag_gen": "classify untagged videos for <tag_name>, creating the tag if needed",
    "/news": "list the latest news from cnn, kqed, or sfbayview (optionally /news [source] [count])",
    "/article": "use one local article file as context; iterative evidence is the default (--simple disables it)",
}

# News sources accepted by /news and completed by news_fetch.php.
NEWS_SOURCES = {
    "cnn": "CNN News",
    "kqed": "KQED News",
    "sfbayview": "SF Bay View",
}

COMMAND_ARGUMENTS = {
    "/tag",
    "/tag_gen",
    "/transcript",
    "/search_transcript",
    "/learn",
    "/learn_topic",
    "/learn_playlist",
    "/ask",
    "/wiki",
    "/wiki_article",
    "/article",
    "/dict",
    "/depth",
    "/benchmark",
    "/news",
}

# The browser's "中文" button translates a finished answer paragraph by
# paragraph. The larger chat model preserves technical terminology and names
# better than the small question-translation model used by chat.py.
ANSWER_TRANSLATION_MODEL = "ornith:35b"
ANSWER_TRANSLATION_NUM_CTX = 8192
MAX_TRANSLATION_CHARS = 200_000
TRANSLATION_PARAGRAPH_PATTERN = re.compile(r"\n[ \t]*\n+")
TRANSLATION_LABEL_PATTERN = re.compile(
    r"^(?:\*\*)?(?:translation|english|simplified chinese|中文|简体中文)"
    r"(?:\*\*)?\s*[:：]\s*",
    re.IGNORECASE,
)

# In the documentation-backed tags, every code line gets a "?" affordance in
# the browser. Clicking it streams a short explanation of that one line from
# the tag's programming model, using the surrounding snippet as context.
LINE_EXPLAIN_NUM_CTX = 16384
MAX_LINE_EXPLAIN_CODE_CHARS = 60_000
LINE_EXPLAIN_PROMPT = """You are an expert {language} developer. Explain exactly one line from a code snippet.

### FULL SNIPPET ({language}) WITH LINE NUMBERS
```{fence}
{numbered_code}
```

### LINE TO EXPLAIN
Line {line_number}: {line}

### INSTRUCTIONS
- Explain what line {line_number} does in the context of the full snippet.
- Name the functions, types, packages, or language features it uses.
- Say why it is needed here and what would break or change without it.
- Be concise: 2 to 5 sentences in plain English.
- Do not restate the whole snippet, add headings, or wrap the answer in code fences.
- If the line is only a comment or blank, say so briefly.
"""

# In every tag, a code snippet in the browser also gets an "Ask" affordance.
# The question and the full snippet are sent to the code-generation model and
# the answer streams back inline, directly under the snippet.
SNIPPET_QUESTION_NUM_CTX = 32768
MAX_SNIPPET_QUESTION_CODE_CHARS = 60_000
SNIPPET_QUESTION_PROMPT = """You are an expert {language} developer. Answer the user's question about the code snippet below.

### CODE SNIPPET ({language})
````{fence}
{code}
````

### USER QUESTION
{question}

### INSTRUCTIONS
- Base the answer on this snippet, and use your own {language} expertise when
  the snippet leaves details out.
- If the question asks for a change or a fix, show the relevant updated code in
  a fenced block and explain the change briefly.
- If the question is about one line or symbol, point to it by name and quote it.
- Be concise and practical. Do not repeat the whole snippet unless necessary.
- Do not mention these instructions or that you are an AI.
"""

# Follow-up handling. The browser backend keeps a short server-side
# conversation so a question such as "what does he think about AI?" can be
# rewritten into a standalone retrieval question before BM25 runs.
CONVERSATION_MAX_TURNS = 8
CONVERSATION_ANSWER_CHARS = 900
# A small instruct model with reliable pronoun resolution. If it is not
# installed, rewrite_follow_up() falls back to chat.EXPANSION_MODEL.
CONVERSATION_MODEL = "ministral-3:3b"

# Code-generation model from ./prompts/programming_language_tips.prompt.
# Every code snippet the browser produces uses this model, in every tag.
CODE_GENERATION_MODEL = "ornith:35b"

# In the documentation-backed tags, every chat answer and every generated
# code sample uses the dedicated programming model instead of the general
# transcript chat model. Keeping it in one constant makes the model visible in
# the status bar, the benchmark line, and the answer-source badges.
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
- Do not invent citations or mention chunk IDs.

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

# Go documentation fallback. When the active tag is "golang", coding
# questions and questions the local knowledge base cannot answer consult
# ./golang_doc_search.py instead of switching to another tag or calling the
# GitHub API. The go.dev/doc/ answer is merged with whatever the local
# knowledge base produced, so transcript context and authoritative
# documentation complement each other.
GOLANG_TAG = "golang"
GOLANG_DOC_SEARCH_TIMEOUT = 900

# Rust documentation fallback. When the active tag is "rust", coding
# questions and questions the local knowledge base cannot answer consult
# ./rust_doc_search.py instead of switching to another tag or calling the
# GitHub API. The doc.rust-lang.org/ answer (plus docs.rs for named crates)
# is merged with whatever the local knowledge base produced.
RUST_TAG = "rust"
RUST_DOC_SEARCH_TIMEOUT = 900

# PHP documentation fallback. When the active tag is "php", coding questions
# and questions the local knowledge base cannot answer consult
# ./php_doc_search.py instead of switching to another tag or calling the
# GitHub API. The www.php.net/manual/en/ answer is merged with whatever the
# local knowledge base produced.
PHP_TAG = "php"
PHP_DOC_SEARCH_TIMEOUT = 900

# The browser shows a live "answer source" badge while an answer streams. For
# documentation-backed tags the answer starts in the local Ollama model and may
# then be replaced or extended by the official language documentation, so the
# server emits a "source" SSE event at each transition.
SOURCE_OLLAMA = "ollama"
SOURCE_DOCUMENTATION = "documentation"
SOURCE_MERGED = "merged"
DOCUMENTATION_TAGS = {GOLANG_TAG, RUST_TAG, PHP_TAG}

# Documentation-backed tags also pin the language of every generated code
# sample. A question such as "write a CLI that prints hello" does not name a
# language, so without this mapping the coding model could answer in Python
# while the user is working in the Rust or Go tag. The tag is the source of
# truth: code is emitted in that tag's language even when the question names
# another one or names none at all.
TAG_CODE_LANGUAGES = {
    GOLANG_TAG: "Go",
    RUST_TAG: "Rust",
    PHP_TAG: "PHP",
}
TAG_CODE_FENCES = {
    GOLANG_TAG: "go",
    RUST_TAG: "rust",
    PHP_TAG: "php",
}


def tag_uses_official_docs(tag: str | None) -> bool:
    """Return True for tags whose code answers must come from official docs.

    The "rust", "golang", and "php" tags are documentation-backed:
    code-sample questions are answered from doc.rust-lang.org, go.dev/doc,
    or www.php.net/manual/en/ (and docs.rs / pkg.go.dev for named
    crates/packages), never from a general code search.
    """
    return (tag or "").strip().lower() in DOCUMENTATION_TAGS


def tag_chat_model(tag: str | None) -> str:
    """Return the answer-generation model for ``tag``.

    The documentation-backed tags (rust, golang, php) use the dedicated
    programming model for ordinary chat answers as well as code, while every
    other tag keeps the general transcript chat model.
    """
    return (
        PROGRAMMING_MODEL
        if tag_uses_official_docs(tag)
        else chat.CHAT_MODEL
    )


def tag_coding_model(tag: str | None) -> str:
    """Return the code-generation model for ``tag``."""
    return (
        PROGRAMMING_MODEL
        if tag_uses_official_docs(tag)
        else CODING_MODE_MODEL
    )


def tag_code_language(tag: str | None) -> str | None:
    """Return the programming language pinned by ``tag``, if any."""
    return TAG_CODE_LANGUAGES.get((tag or "").strip().lower())


def tag_code_fence(tag: str | None) -> str:
    """Return the Markdown fence language for ``tag``'s code samples."""
    return TAG_CODE_FENCES.get((tag or "").strip().lower(), "")


def tag_coding_directive(tag: str | None) -> str:
    """Build the language requirement for code generated under ``tag``.

    Returns an empty string for tags that do not pin a language. For the
    documentation-backed tags the directive is deliberately absolute so the
    local coding model cannot fall back to the language it was trained on
    most (usually Python) when the question omits the language.
    """
    language = tag_code_language(tag)
    if not language:
        return ""
    normalized = (tag or "").strip().lower()
    fence = tag_code_fence(normalized) or language.lower()
    return f"""### LANGUAGE REQUIREMENT (active tag: {normalized})
The active knowledge tag is "{normalized}". The user is working in
{language}. Every code sample, snippet, command, and API reference in the
answer MUST be written in {language}.
- Use complete, idiomatic {language} and fence every code block as
  ```{fence}.
- If the question names another programming language or does not name any
  language, still answer in {language}; never emit code in another language.
- Use only the standard library and documented {language} APIs unless the
  retrieved context explicitly names a dependency.

"""


def coding_prompt_prefix(tag: str | None) -> str:
    """Return the coding-mode prompt for the active tag.

    The generic coding instructions are extended with a tag-specific language
    requirement so that code generated in the "rust", "golang", or "php"
    tag is always Rust, Go, or PHP.
    """
    return CODING_MODE_PROMPT_PREFIX + tag_coding_directive(tag)


def github_code_fallback_allowed(tag: str | None) -> bool:
    """Return False when GitHub code search must not be used for ``tag``.

    This is the single source of truth for the Rust/PHP requirement: with
    those tags the server must never fall back to GitHub for sample code.
    Go is covered by the same documentation-backed rule.
    """
    return not tag_uses_official_docs(tag)

# GitHub's search API takes a language name ("Python"), not the spelling used
# in a question ("python3"). Frameworks map to the language they are written
# in, and the extensions are used to pick source files in repository fallback.
GITHUB_LANGUAGE_MAP: dict[str, tuple[str, tuple[str, ...]]] = {
    "python": ("Python", (".py",)),
    "python3": ("Python", (".py",)),
    "javascript": ("JavaScript", (".js", ".jsx", ".mjs", ".cjs")),
    "typescript": ("TypeScript", (".ts", ".tsx")),
    "node": ("JavaScript", (".js", ".mjs", ".cjs")),
    "node.js": ("JavaScript", (".js", ".mjs", ".cjs")),
    "deno": ("TypeScript", (".ts", ".js")),
    "java": ("Java", (".java",)),
    "c++": ("C++", (".cpp", ".cc", ".cxx", ".hpp", ".h")),
    "cpp": ("C++", (".cpp", ".cc", ".cxx", ".hpp", ".h")),
    "c#": ("C#", (".cs",)),
    "csharp": ("C#", (".cs",)),
    ".net": ("C#", (".cs",)),
    "dotnet": ("C#", (".cs",)),
    "golang": ("Go", (".go",)),
    "rust": ("Rust", (".rs",)),
    "ruby": ("Ruby", (".rb",)),
    "php": ("PHP", (".php",)),
    "swift": ("Swift", (".swift",)),
    "kotlin": ("Kotlin", (".kt", ".kts")),
    "scala": ("Scala", (".scala",)),
    "haskell": ("Haskell", (".hs",)),
    "elixir": ("Elixir", (".ex", ".exs")),
    "erlang": ("Erlang", (".erl",)),
    "clojure": ("Clojure", (".clj",)),
    "lua": ("Lua", (".lua",)),
    "perl": ("Perl", (".pl", ".pm")),
    "objective-c": ("Objective-C", (".m", ".mm")),
    "dart": ("Dart", (".dart",)),
    "flutter": ("Dart", (".dart",)),
    "julia": ("Julia", (".jl",)),
    "fortran": ("Fortran", (".f90", ".f95", ".f")),
    "cobol": ("COBOL", (".cob", ".cbl")),
    "assembly": ("Assembly", (".asm", ".s")),
    "asm": ("Assembly", (".asm", ".s")),
    "sql": ("SQL", (".sql",)),
    "mysql": ("SQL", (".sql",)),
    "postgres": ("SQL", (".sql",)),
    "postgresql": ("SQL", (".sql",)),
    "sqlite": ("SQL", (".sql",)),
    "bash": ("Shell", (".sh", ".bash")),
    "shell": ("Shell", (".sh", ".bash")),
    "zsh": ("Shell", (".sh", ".zsh")),
    "powershell": ("PowerShell", (".ps1",)),
    "html": ("HTML", (".html", ".htm")),
    "css": ("CSS", (".css",)),
    "sass": ("CSS", (".scss", ".sass")),
    "less": ("CSS", (".less",)),
    "react": ("JavaScript", (".js", ".jsx", ".ts", ".tsx")),
    "reactjs": ("JavaScript", (".js", ".jsx", ".ts", ".tsx")),
    "vue": ("JavaScript", (".vue", ".js", ".ts")),
    "vuejs": ("JavaScript", (".vue", ".js", ".ts")),
    "angular": ("TypeScript", (".ts",)),
    "svelte": ("JavaScript", (".svelte", ".js", ".ts")),
    "next.js": ("JavaScript", (".js", ".jsx", ".ts", ".tsx")),
    "nuxt": ("JavaScript", (".js", ".ts", ".vue")),
    "django": ("Python", (".py",)),
    "flask": ("Python", (".py",)),
    "fastapi": ("Python", (".py",)),
    "rails": ("Ruby", (".rb",)),
    "laravel": ("PHP", (".php",)),
    "spring": ("Java", (".java",)),
    "express": ("JavaScript", (".js", ".ts")),
    "pandas": ("Python", (".py",)),
    "numpy": ("Python", (".py",)),
    "pytorch": ("Python", (".py",)),
    "tensorflow": ("Python", (".py",)),
    "jax": ("Python", (".py",)),
    "sklearn": ("Python", (".py",)),
    "scikit-learn": ("Python", (".py",)),
    "tailwind": ("CSS", (".css", ".html")),
    "tailwindcss": ("CSS", (".css", ".html")),
    "webpack": ("JavaScript", (".js",)),
    "vite": ("JavaScript", (".js", ".ts")),
    "babel": ("JavaScript", (".js",)),
    "jest": ("JavaScript", (".js", ".ts")),
    "pytest": ("Python", (".py",)),
    "android": ("Kotlin", (".kt", ".java")),
    "ios": ("Swift", (".swift",)),
    "unity": ("C#", (".cs",)),
    "unreal": ("C++", (".cpp", ".h")),
    "opengl": ("C++", (".cpp", ".h")),
    "vulkan": ("C++", (".cpp", ".h")),
    "webgl": ("JavaScript", (".js",)),
    "wasm": ("WebAssembly", (".wat", ".wasm")),
}
GITHUB_DEFAULT_EXTENSIONS = tuple(
    sorted(
        {
            extension
            for _language, extensions in GITHUB_LANGUAGE_MAP.values()
            for extension in extensions
        }
    )
)
# Filler and code-request words add no signal to a GitHub query. Technical
# nouns such as "class", "function", and "data structure" are deliberately
# kept because they are often the most useful search terms.
GITHUB_QUERY_STOPWORDS = frozenset(chat.FUZZY_ANCHOR_STOPWORDS) | frozenset(
    "i you he she it we they me him her us them my your his its our their "
    "code sample samples snippet snippets example examples demo demonstrate "
    "write show give provide generate create implement build debug refactor "
    "fix explain teach walkthrough walkthrough want need want like "
    "using use used implementation program script".split()
)
# The main language pattern uses \b, which cannot match names that end in a
# symbol ("C++" and "C#"), nor a leading dot (".NET"). This supplementary
# pattern keeps those common languages usable for GitHub queries.
GITHUB_EXTRA_LANGUAGE_PATTERN = re.compile(
    r"(?<![A-Za-z0-9_])(?:c\+\+|c#|\.net)(?![A-Za-z0-9_])",
    re.IGNORECASE,
)

# Words that name a programming language, runtime, or framework. A language
# mention alone is not enough: the question must also ask for code/design
# help or name a programming structure.
PROGRAMMING_LANGUAGE_PATTERN = re.compile(
    r"\b(?:"
    r"python|python3|javascript|typescript|node(?:\.js)?|deno|"
    r"java|c\+\+|cpp|c#|csharp|\.net|dotnet|golang|rust|ruby|"
    r"php|swift|kotlin|scala|haskell|elixir|erlang|clojure|lua|perl|"
    r"objective-c|dart|flutter|julia|fortran|cobol|assembly|asm|"
    r"sql|mysql|postgres(?:ql)?|sqlite|mongodb|redis|"
    r"bash|shell|zsh|powershell|html|css|sass|less|"
    r"react|reactjs|vue|vuejs|angular|svelte|next\.js|nuxt|"
    r"django|flask|fastapi|rails|laravel|spring|express|"
    r"graphql|grpc|docker|kubernetes|terraform|git|"
    r"pandas|numpy|pytorch|tensorflow|jax|sklearn|scikit-learn|"
    r"tailwind(?:css)?|webpack|vite|babel|jest|pytest|"
    r"android|ios|unity|unreal|opengl|vulkan|webgl|wasm"
    r")\b",
    re.IGNORECASE,
)

# Programming structures, algorithms, paradigms, and architecture terms that
# are commonly the subject of a code sample. These only trigger coding mode
# together with an explicit code request or a named programming language, so
# generic uses of words such as "stack" or "architecture" do not match.
PROGRAMMING_CONCEPT_PATTERN = re.compile(
    r"\b(?:"
    r"linked\s+list|doubly\s+linked\s+list|binary\s+(?:tree|search\s+tree)|"
    r"b-?tree|trie|hash\s+(?:map|table|set)|stack|queue|deque|heap|"
    r"priority\s+queue|graph(?:\s+(?:traversal|algorithm))?|"
    r"depth[-\s]first\s+search|breadth[-\s]first\s+search|dfs|bfs|"
    r"dynamic\s+programming|memoization|recursion|recursive|backtracking|"
    r"greedy\s+algorithm|sorting\s+algorithm|binary\s+search|"
    r"quicksort|quick\s+sort|merge\s+sort|heapsort|heap\s+sort|"
    r"bubble\s+sort|insertion\s+sort|selection\s+sort|"
    r"polymorphism|inheritance|encapsulation|abstraction|"
    r"time\s+complexity|space\s+complexity|big[-\s]?o|data\s+structure|"
    r"design\s+pattern|singleton|factory\s+pattern|observer\s+pattern|"
    r"decorator|dependency\s+injection|dependency\s+inversion|"
    r"factory|builder|adapter|strategy|repository\s+pattern|mvc|mvvm|"
    r"event\s+loop|closure|promise|async(?:hronous)?|await|callback|"
    r"coroutine|thread(?:ing|s)?|concurrency|mutex|semaphore|race\s+condition|"
    r"garbage\s+collection|memory\s+leak|pointer|reference\s+count|"
    r"unit\s+test|integration\s+test|test[-\s]driven|mock(?:ing)?|"
    r"regular\s+expression|regex|orm|crud|rest\s+api|api\s+endpoint|"
    r"microservice|monolith|serverless|event[-\s]driven|message\s+queue|"
    r"pub(?:lish)?/?sub(?:scribe)?|load\s+balanc(?:er|ing)|cach(?:e|ing)|"
    r"database\s+index|schema|migration|transaction|acid|"
    r"object[-\s]oriented|functional\s+programming|generics?|interface|"
    r"abstract\s+class|class|function|method|module|package|library|"
    r"framework|compiler|interpreter|virtual\s+machine|bytecode|"
    r"frontend|backend|full[-\s]stack|architecture|system\s+design|"
    r"scalab(?:le|ility)|distributed\s+system|consensus|raft|paxos"
    r")\b",
    re.IGNORECASE,
)

# Unambiguous programming concepts. A question such as "what is dependency
# injection?" is a coding question even without an explicit language, while a
# generic word such as "stack" or "architecture" needs a code request or a
# named language before coding mode is used.
STRONG_PROGRAMMING_CONCEPT_PATTERN = re.compile(
    r"\b(?:"
    r"linked\s+list|doubly\s+linked\s+list|binary\s+(?:tree|search\s+tree)|"
    r"b-?tree|trie|hash\s+(?:map|table|set)|priority\s+queue|deque|"
    r"depth[-\s]first\s+search|breadth[-\s]first\s+search|dfs|bfs|"
    r"dynamic\s+programming|memoization|recursion|recursive|backtracking|"
    r"greedy\s+algorithm|sorting\s+algorithm|binary\s+search|"
    r"quicksort|quick\s+sort|merge\s+sort|heapsort|heap\s+sort|"
    r"bubble\s+sort|insertion\s+sort|selection\s+sort|"
    r"polymorphism|inheritance|encapsulation|abstraction|"
    r"time\s+complexity|space\s+complexity|big[-\s]?o|data\s+structure|"
    r"design\s+pattern|singleton|factory\s+pattern|observer\s+pattern|"
    r"dependency\s+injection|dependency\s+inversion|"
    r"repository\s+pattern|mvc|mvvm|"
    r"event\s+loop|closure|promise|async(?:hronous)?|await|callback|"
    r"coroutine|mutex|semaphore|race\s+condition|"
    r"garbage\s+collection|memory\s+leak|reference\s+count|"
    r"unit\s+test|integration\s+test|test[-\s]driven|mock(?:ing)?|"
    r"regular\s+expression|regex|orm|crud|rest\s+api|api\s+endpoint|"
    r"microservice|monolith|serverless|event[-\s]driven|message\s+queue|"
    r"pub(?:lish)?/?sub(?:scribe)?|load\s+balanc(?:er|ing)|"
    r"database\s+index|acid|object[-\s]oriented|functional\s+programming|"
    r"generics?|compiler|interpreter|virtual\s+machine|bytecode|"
    r"system\s+design|distributed\s+system|consensus|raft|paxos"
    r")\b",
    re.IGNORECASE,
)

# Opinions and recommendations about a technology are not code requests,
# even when they mention a programming concept.
CODING_OPINION_PATTERN = re.compile(
    r"\b(?:what\s+do\s+you\s+think|do\s+you\s+(?:like|prefer|recommend)|"
    r"should\s+(?:i|we)\s+(?:use|choose|learn)|is\s+(?:it|\w+)\s+(?:worth|better)|"
    r"pros\s+and\s+cons|versus|vs\.?|compare|recommend(?:ation)?|"
    r"your\s+opinion|opinions?\s+on)\b",
    re.IGNORECASE,
)

CHINESE_CODING_PATTERN = re.compile(
    r"(?:代码|示例|例子|代码片段|函数|类|实现|编程|程序|架构|算法|"
    r"数据结构|写一个|写个|如何实现|怎么做|用\s*\w+\s*(?:写|实现))"
)

# Explicit requests for code / implementation help. Kept separate from the
# concept list so an ordinary question containing "class" or "function" is
# not misclassified.
CODE_REQUEST_PATTERN = re.compile(
    r"(?:\b(?:show|write|give|provide|generate|create|implement|build|code|"
    r"debug|refactor|fix)\b.{0,60}\b(?:code|sample|snippet|example|function|"
    r"method|class|program|script|implementation|algorithm|data\s+structure|"
    r"query|api|endpoint|architecture|design)\b"
    r"|\b(?:code|sample|example|demo)\s+(?:code|sample|example|snippet|"
    r"implementation|program|for\s+(?:a|an|the))\b"
    r"|\bhow\s+(?:do|can|should|would|to)\s+(?:i|we|you)?\s*(?:implement|"
    r"build|write|code|create|structure|architect|design)\b"
    r"|\b(?:implement|implementing|code|program|architect)\s+(?:a|an|the|this|"
    r"that|my|our)\b"
    r"|\b(?:in|using|with)\s+(?:python|javascript|typescript|java|c\+\+|go|"
    r"rust|ruby|php|swift|kotlin|sql|bash|react|node(?:\.js)?)\b"
    r"|\b(?:explain|teach|demonstrate|walk\s+me\s+through)\b.{0,40}"
    r"\b(?:how\s+to|code|implementation|syntax|language\s+feature)\b"
    r")",
    re.IGNORECASE,
)

# A pasted code snippet is itself a strong signal that the question is a
# coding question, even when it is phrased as "why does this fail?".
CODE_SYNTAX_PATTERN = re.compile(
    r"(?:^|\n)\s*(?:def|class|function|const|let|var|import|from|"
    r"#include|public|private|protected|static|void|int|SELECT|CREATE|"
    r"INSERT|UPDATE|fn|func|package|using|async|await)\b"
    r"|=>|::|\{\s*\n|;\s*$",
    re.MULTILINE,
)


def question_requests_coding(question: str) -> bool:
    """Return True when a question asks for a programming code sample.

    Detection is deliberately conservative: a named language is only enough
    when the question also asks for code/design help or names a programming
    concept. Explicit code requests, pasted code, and language+concept
    questions are all handled without an extra model call.
    """
    text = " ".join(str(question or "").split())
    if not text:
        return False
    if CHINESE_CODING_PATTERN.search(question):
        return True
    if CODE_SYNTAX_PATTERN.search(question):
        return True
    if CODE_REQUEST_PATTERN.search(text):
        return True
    has_language = bool(PROGRAMMING_LANGUAGE_PATTERN.search(text))
    has_concept = bool(PROGRAMMING_CONCEPT_PATTERN.search(text))
    has_strong_concept = bool(STRONG_PROGRAMMING_CONCEPT_PATTERN.search(text))
    is_opinion = bool(CODING_OPINION_PATTERN.search(text))
    if is_opinion:
        return False
    return has_strong_concept or (has_language and has_concept)


def github_query_terms(
    question: str,
) -> tuple[list[str], str | None, tuple[str, ...] | None]:
    """Return GitHub search terms, language, and source extensions.

    The language is inferred from the programming-language names already used
    by coding-mode detection. Frameworks map to their implementation language
    (React -> JavaScript, Django -> Python). The returned terms remove the
    question's filler words and the language name so the query stays focused.
    """
    text = " ".join(str(question or "").split())
    language: str | None = None
    extensions: tuple[str, ...] | None = None
    raw_language = ""
    language_matches = list(GITHUB_EXTRA_LANGUAGE_PATTERN.finditer(text))
    language_matches.extend(PROGRAMMING_LANGUAGE_PATTERN.finditer(text))
    for match in sorted(language_matches, key=lambda item: item.start()):
        token = match.group(0).lower()
        mapped = GITHUB_LANGUAGE_MAP.get(token)
        if mapped is not None:
            language, extensions = mapped
            raw_language = token
            break

    language_tokens = set(chat.tokenize(raw_language))
    terms: list[str] = []
    for token in chat.tokenize(text):
        if len(token) < 2 or token.isdigit():
            continue
        if token in GITHUB_QUERY_STOPWORDS or token in language_tokens:
            continue
        if token not in terms:
            terms.append(token)
    return terms[:GITHUB_MAX_QUERY_TERMS], language, extensions


def _github_headers(text_match: bool = False) -> dict[str, str]:
    headers = dict(GITHUB_HEADERS)
    if text_match:
        # Ask the code-search endpoint for the matched fragments, not just
        # file paths. Without this header every result would need a second
        # request to download the file.
        headers["Accept"] = "application/vnd.github.text-match+json"
    if GITHUB_TOKEN:
        headers["Authorization"] = f"Bearer {GITHUB_TOKEN}"
    return headers


def _github_snippet_language(path: str) -> str:
    suffix = Path(path).suffix.lower()
    for language, extensions in GITHUB_LANGUAGE_MAP.values():
        if suffix in extensions:
            return language
    return ""


def _github_trim_snippet(text: str, terms: list[str]) -> str:
    """Trim a source file or search fragment down to a readable snippet."""
    normalized = str(text or "").replace("\r\n", "\n").replace("\r", "\n")
    if not normalized.strip():
        return ""
    all_lines = normalized.split("\n")
    if len(all_lines) > GITHUB_SNIPPET_MAX_LINES:
        lowered = [line.lower() for line in all_lines]
        anchor: int | None = None
        for term in terms:
            needle = term.lower()
            if not needle:
                continue
            for index, line in enumerate(lowered):
                if needle in line:
                    anchor = index
                    break
            if anchor is not None:
                break
        start = max(0, (anchor if anchor is not None else 0) - 6)
        # Move the start to a nearby declaration/import so the snippet begins
        # at a natural boundary instead of in the middle of an expression.
        for index in range(start, min(len(all_lines), start + 12)):
            if re.match(
                r"\s*(?:def|class|function|func|fn|pub\s+fn|public|private|"
                r"protected|static|void|int|const|let|var|#include|import|"
                r"from|package|using|SELECT|CREATE)\b",
                all_lines[index],
            ):
                start = index
                break
        end = min(len(all_lines), start + GITHUB_SNIPPET_MAX_LINES)
        snippet = "\n".join(all_lines[start:end]).strip()
        if start > 0:
            snippet = "...\n" + snippet
        if end < len(all_lines):
            snippet += "\n..."
    else:
        snippet = normalized.strip()

    if len(snippet) > GITHUB_SNIPPET_MAX_CHARS:
        snippet = snippet[:GITHUB_SNIPPET_MAX_CHARS].rstrip()
        snippet += "\n... (truncated)"
    return snippet


def _github_raw_file(repo: str, branch: str | None, path: str) -> str:
    """Download a text source file from raw.githubusercontent.com."""
    if not repo or not path:
        return ""
    ref = branch or "HEAD"
    url = (
        f"{GITHUB_RAW_BASE}/{quote(repo, safe='/')}/"
        f"{quote(ref, safe='/')}/{quote(path, safe='/')}"
    )
    try:
        response = requests.get(
            url,
            headers={"User-Agent": GITHUB_HEADERS["User-Agent"]},
            timeout=GITHUB_TIMEOUT,
        )
    except requests.RequestException:
        return ""
    if response.status_code != 200:
        return ""
    if len(response.content) > GITHUB_MAX_FILE_BYTES:
        return ""
    # Skip binaries such as images or compiled artifacts.
    if b"\x00" in response.content:
        return ""
    return response.text


def _github_repo_code_paths(
    repo: str,
    branch: str,
    extensions: tuple[str, ...] | None,
    terms: list[str],
    deadline: float,
) -> list[str]:
    """List candidate source files in a repository, best match first."""
    if time.monotonic() >= deadline:
        return []
    url = (
        f"{GITHUB_API_BASE}/repos/{quote(repo, safe='/')}/git/trees/"
        f"{quote(branch or 'HEAD', safe='')}"
    )
    try:
        response = requests.get(
            url,
            params={"recursive": "1"},
            headers=_github_headers(),
            timeout=GITHUB_TIMEOUT,
        )
    except requests.RequestException:
        return []
    if response.status_code != 200:
        return []
    try:
        data = response.json()
    except ValueError:
        return []
    tree = data.get("tree")
    if not isinstance(tree, list):
        return []

    suffixes = extensions or GITHUB_DEFAULT_EXTENSIONS
    normalized_terms = [
        re.sub(r"[^a-z0-9]+", "", term.lower()) for term in terms if term
    ]
    scored: list[tuple[int, int, str]] = []
    for item in tree:
        if not isinstance(item, dict) or item.get("type") != "blob":
            continue
        path = str(item.get("path") or "")
        if not path:
            continue
        if Path(path).suffix.lower() not in suffixes:
            continue
        try:
            size = int(item.get("size") or 0)
        except (TypeError, ValueError):
            size = 0
        if size > GITHUB_MAX_FILE_BYTES:
            continue
        lowered = path.lower()
        normalized_path = re.sub(
            r"[^a-z0-9]+", "", Path(path).with_suffix("").as_posix().lower()
        )
        score = sum(
            2
            for term in normalized_terms
            if term and term in normalized_path
        )
        if re.search(
            r"(?:^|/)(?:tests?|vendor|node_modules|examples?|docs?)/",
            lowered,
        ):
            score -= 1
        scored.append((score, -len(path), path))

    scored.sort(reverse=True)
    matching = [path for score, _length, path in scored if score > 0]
    if matching:
        return matching[:5]
    return [path for _score, _length, path in scored[:3]]


def _github_code_search_snippets(
    query: str,
    language: str | None,
    extensions: tuple[str, ...] | None,
    terms: list[str],
    limit: int,
    deadline: float,
) -> list[dict]:
    """Search GitHub code (requires a token) and return matched fragments."""
    if not GITHUB_TOKEN or limit <= 0 or time.monotonic() >= deadline:
        return []
    search_query = f"{query} in:file"
    if language:
        search_query += f" language:{language}"
    try:
        response = requests.get(
            f"{GITHUB_API_BASE}/search/code",
            params={"q": search_query, "per_page": max(limit * 4, 10)},
            headers=_github_headers(text_match=True),
            timeout=GITHUB_TIMEOUT,
        )
    except requests.RequestException:
        return []
    if response.status_code != 200:
        return []
    try:
        data = response.json()
    except ValueError:
        return []

    snippets: list[dict] = []
    seen: set[str] = set()
    for item in data.get("items") or []:
        if len(snippets) >= limit:
            break
        if not isinstance(item, dict):
            continue
        repository = item.get("repository") or {}
        repo = str(repository.get("full_name") or "")
        path = str(item.get("path") or "")
        url = str(item.get("html_url") or "")
        if not url or url in seen:
            continue
        pieces: list[str] = []
        for match in item.get("text_matches") or []:
            if not isinstance(match, dict):
                continue
            fragment = str(match.get("fragment") or "")
            if fragment.strip():
                pieces.append(fragment)
        code = _github_trim_snippet("\n...\n".join(pieces), terms)
        if not code and repo and path:
            code = _github_trim_snippet(
                _github_raw_file(repo, None, path), terms
            )
        if not code:
            continue
        seen.add(url)
        snippets.append(
            {
                "repo": repo or "GitHub",
                "path": path or "(code search result)",
                "url": url,
                "language": language or _github_snippet_language(path),
                "code": code,
            }
        )
    return snippets


def _github_repository_search_snippets(
    query: str,
    language: str | None,
    extensions: tuple[str, ...] | None,
    terms: list[str],
    limit: int,
    deadline: float,
) -> list[dict]:
    """Unauthenticated fallback: find repositories and read source files."""
    if limit <= 0 or time.monotonic() >= deadline:
        return []
    search_query = query
    if language:
        search_query += f" language:{language}"
    try:
        response = requests.get(
            f"{GITHUB_API_BASE}/search/repositories",
            params={
                "q": search_query,
                "sort": "stars",
                "order": "desc",
                "per_page": GITHUB_REPOSITORY_LIMIT,
            },
            headers=_github_headers(),
            timeout=GITHUB_TIMEOUT,
        )
    except requests.RequestException:
        return []
    if response.status_code != 200:
        return []
    try:
        data = response.json()
    except ValueError:
        return []

    snippets: list[dict] = []
    seen: set[str] = set()
    for repo_data in data.get("items") or []:
        if len(snippets) >= limit or time.monotonic() >= deadline:
            break
        if not isinstance(repo_data, dict):
            continue
        repo = str(repo_data.get("full_name") or "")
        branch = str(repo_data.get("default_branch") or "HEAD")
        if not repo:
            continue
        paths = _github_repo_code_paths(
            repo, branch, extensions, terms, deadline
        )
        for path in paths:
            if len(snippets) >= limit or time.monotonic() >= deadline:
                break
            url = f"https://github.com/{repo}/blob/{branch}/{path}"
            if url in seen:
                continue
            code = _github_trim_snippet(
                _github_raw_file(repo, branch, path), terms
            )
            if not code:
                continue
            seen.add(url)
            snippets.append(
                {
                    "repo": repo,
                    "path": path,
                    "url": url,
                    "language": language or _github_snippet_language(path),
                    "code": code,
                }
            )
            # One snippet per repository keeps the two sources diverse.
            break
    return snippets


def github_code_snippets(
    question: str,
    limit: int = GITHUB_SNIPPET_LIMIT,
    log: Callable[[str], None] | None = None,
    tag: str | None = None,
) -> list[dict]:
    """Return up to ``limit`` GitHub code snippets related to ``question``.

    GitHub's code-search API needs a token. With one, matched fragments come
    back directly. Without one, repository search plus the public git-tree and
    raw-content endpoints provide the same fallback using only anonymous
    requests. Results are deduplicated by URL.

    ``tag`` is a hard safety guard: documentation-backed tags such as "rust"
    never reach GitHub, even if a future caller forgets to check first.
    """

    def emit(message: str) -> None:
        if log is not None:
            log(message)

    if not github_code_fallback_allowed(tag):
        emit(
            f"[github: code search disabled for tag {tag!r}; "
            "official language documentation is used instead]"
        )
        return []

    terms, language, extensions = github_query_terms(question)
    if not terms:
        if not language:
            return []
        terms = [language.lower()]

    deadline = time.monotonic() + GITHUB_TOTAL_TIMEOUT
    query_candidates: list[str] = []
    for count in (len(terms), min(3, len(terms)), 1):
        candidate = " ".join(terms[:count]).strip()
        if candidate and candidate not in query_candidates:
            query_candidates.append(candidate)

    snippets: list[dict] = []
    seen: set[str] = set()
    for query in query_candidates[:2]:
        if len(snippets) >= limit or time.monotonic() >= deadline:
            break
        label = query + (f" ({language})" if language else "")
        emit(f"[github: searching code for {label!r}]")
        found = _github_code_search_snippets(
            query,
            language,
            extensions,
            terms,
            limit - len(snippets),
            deadline,
        )
        if found:
            if len(found) < limit - len(snippets):
                found.extend(
                    _github_repository_search_snippets(
                        query,
                        language,
                        extensions,
                        terms,
                        limit - len(snippets) - len(found),
                        deadline,
                    )
                )
        else:
            emit("[github: code search unavailable; searching repositories]")
            found = _github_repository_search_snippets(
                query,
                language,
                extensions,
                terms,
                limit - len(snippets),
                deadline,
            )
        for snippet in found:
            url = str(snippet.get("url") or "")
            if url and url in seen:
                continue
            if url:
                seen.add(url)
            snippets.append(snippet)
            if len(snippets) >= limit:
                break
    return snippets[:limit]


def format_github_code_answer(snippets: list[dict]) -> str:
    """Build a Markdown answer from the retrieved GitHub code snippets."""
    lines = [
        "I could not find a complete answer in the local knowledge base, so I "
        "searched GitHub for related code. Here are the most relevant "
        "snippets:",
        "",
    ]
    for number, snippet in enumerate(snippets, start=1):
        repo = str(snippet.get("repo") or "GitHub")
        path = str(snippet.get("path") or "")
        url = str(snippet.get("url") or "")
        label = f"{repo}/{path}" if path else repo
        if url:
            lines.append(f"### Snippet {number}: [{label}]({url})")
        else:
            lines.append(f"### Snippet {number}: {label}")
        lines.append("")
        language = str(snippet.get("language") or "").strip()
        lines.append(f"```{language}" if language else "```")
        lines.append(str(snippet.get("code") or "").strip())
        lines.append("```")
        lines.append("")
    lines.append(
        "These snippets are starting points; adapt them to your project's "
        "types, imports, and error handling."
    )
    return "\n".join(lines).strip() + "\n"


def golang_package_docs_request(question: str) -> bool:
    """Return True for a Go code-sample question that names or implies a package.

    ``golang_doc_search.py`` already performs this check with the keyword
    model, but the server needs a cheap, deterministic way to route questions
    such as "how do I use sync.WaitGroup" into coding mode and the Go
    documentation path. Package detection and the code-sample regex are
    imported from the script so both layers agree on what counts as a Go
    package mention; ``candidate_packages`` also maps concept words such as
    "iterator" to the related ``iter`` package.
    """
    try:
        import golang_doc_search as go_docs
    except ImportError:
        return False
    text = " ".join(str(question or "").split())
    if not text:
        return False
    wants_code = bool(
        go_docs.CODE_SAMPLE_RE.search(text)
        or CODE_REQUEST_PATTERN.search(text)
    )
    if not wants_code:
        return False
    return bool(go_docs.candidate_packages(text))


def rust_crate_docs_request(question: str) -> bool:
    """Return True for a Rust code-sample question that names a crate.

    ``rust_doc_search.py`` performs the same check with the keyword model,
    but the server needs a cheap, deterministic way to route questions such
    as "how do I use serde to deserialize JSON" into coding mode and the Rust
    documentation path. The script's code-sample regex and crate detector are
    imported so both layers agree on what counts as a crate mention.
    """
    try:
        import rust_doc_search as rust_docs
    except ImportError:
        return False
    text = " ".join(str(question or "").split())
    if not text:
        return False
    wants_code = bool(
        rust_docs.CODE_SAMPLE_RE.search(text)
        or CODE_REQUEST_PATTERN.search(text)
    )
    if not wants_code:
        return False
    return bool(rust_docs.candidate_crates(text))


def rust_code_docs_request(question: str) -> bool:
    """Return True for any Rust code-sample question.

    In the "rust" tag, code samples must come from the official Rust
    documentation (doc.rust-lang.org and docs.rs), never from the GitHub
    code-search fallback. This check does not require a crate name, so a
    question such as "show me how to iterate over a HashMap" is routed to
    ``rust_doc_search.py`` as well.
    """
    try:
        import rust_doc_search as rust_docs
    except ImportError:
        return False
    text = " ".join(str(question or "").split())
    if not text:
        return False
    return bool(
        rust_docs.CODE_SAMPLE_RE.search(text)
        or CODE_REQUEST_PATTERN.search(text)
        or CHINESE_CODING_PATTERN.search(question)
    )


def php_symbol_docs_request(question: str) -> bool:
    """Return True for a PHP code-sample question that names a symbol.

    ``php_doc_search.py`` performs the same check with the keyword model,
    but the server needs a cheap, deterministic way to route questions such
    as "show me code sample using array_map" into coding mode and the PHP
    manual path. The script's code-sample regex and symbol detector are
    imported so both layers agree on what counts as a PHP symbol mention.
    """
    try:
        import php_doc_search as php_docs
    except ImportError:
        return False
    text = " ".join(str(question or "").split())
    if not text:
        return False
    wants_code = bool(
        php_docs.CODE_SAMPLE_RE.search(text)
        or CODE_REQUEST_PATTERN.search(text)
    )
    if not wants_code:
        return False
    return bool(php_docs.candidate_symbols(text))


def documentation_code_request(tag: str | None, question: str) -> bool:
    """Return True when a documentation-backed tag asks for code.

    The language documentation scripts use their own ``CODE_SAMPLE_RE``,
    which recognises requests such as "write a CLI" that the generic server
    detector may miss. Mirroring that check here guarantees every code
    request in the "rust", "golang", or "php" tag enters coding mode, which
    then pins the language to the tag (see ``tag_coding_directive``).
    """
    normalized = (tag or "").strip().lower()
    if normalized not in DOCUMENTATION_TAGS:
        return False
    text = " ".join(str(question or "").split())
    if not text:
        return False
    if CODE_REQUEST_PATTERN.search(text) or CHINESE_CODING_PATTERN.search(
        question
    ):
        return True
    try:
        if normalized == GOLANG_TAG:
            import golang_doc_search as docs
        elif normalized == RUST_TAG:
            import rust_doc_search as docs
        else:
            import php_doc_search as docs
    except ImportError:
        return False
    return bool(docs.CODE_SAMPLE_RE.search(text))


def _run_doc_search(
    script_name: str,
    question: str,
    *,
    timeout: float,
    log_prefix: str,
    start_message: str,
    success_message: str,
    model: str = PROGRAMMING_MODEL,
    log: Callable[[str], None] | None = None,
    emit: Callable[[str], None] | None = None,
) -> tuple[str | None, str]:
    """Run an official documentation search script and stream its stdout.

    The script prints the documentation answer incrementally. ``emit`` is
    called with each decoded stdout chunk as soon as it arrives, so the HTTP
    handler can forward it to the browser without waiting for the whole
    subprocess to finish. Progress lines from stderr are forwarded through
    ``log`` in the same way.

    Returns ``(answer, "")`` on success and ``(error_answer, error)`` on
    failure. The returned text includes the Sources section printed by the
    script; when a run fails after producing partial output, that partial
    answer is returned together with the error.
    """
    script = SCRIPT_DIR / script_name
    if not script.is_file():
        return None, f"missing script: {script}"

    if log is not None:
        log(f"[{log_prefix}: {start_message}]")
    try:
        process = subprocess.Popen(
            [sys.executable, "-u", str(script), question, "--model", model],
            cwd=SCRIPT_DIR,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            bufsize=0,
        )
    except OSError as exc:
        return None, f"could not run {script_name}: {exc}"

    assert process.stdout is not None and process.stderr is not None
    selector = selectors.DefaultSelector()
    selector.register(process.stdout, selectors.EVENT_READ, "stdout")
    selector.register(process.stderr, selectors.EVENT_READ, "stderr")
    decoder = codecs.getincrementaldecoder("utf-8")("replace")
    fence_filter = CodeFenceRepairFilter()
    answer_parts: list[str] = []
    stderr_text = ""
    stderr_line_buffer = ""
    deadline = time.monotonic() + timeout
    timed_out = False
    returncode: int | None = None

    def deliver_stdout(text: str) -> None:
        """Repair a stdout chunk before it is stored or streamed."""
        repaired = fence_filter.feed(text)
        if not repaired:
            return
        answer_parts.append(repaired)
        if emit is not None:
            emit(repaired)

    try:
        while selector.get_map():
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                timed_out = True
                try:
                    process.kill()
                except OSError:
                    pass
                break
            events = selector.select(timeout=min(remaining, 1.0))
            for key, _mask in events:
                chunk = os.read(key.fileobj.fileno(), 4096)
                if not chunk:
                    selector.unregister(key.fileobj)
                    continue
                if key.data == "stdout":
                    text = decoder.decode(chunk)
                    if not text:
                        continue
                    deliver_stdout(text)
                else:
                    decoded = chunk.decode("utf-8", "replace")
                    stderr_text += decoded
                    stderr_line_buffer += decoded
                    while "\n" in stderr_line_buffer:
                        line, stderr_line_buffer = stderr_line_buffer.split(
                            "\n", 1
                        )
                        line = line.strip()
                        if (
                            line
                            and not line.startswith("Warning:")
                            and log is not None
                        ):
                            log(f"[{log_prefix}: {line}]")

        tail = decoder.decode(b"", final=True)
        if tail:
            deliver_stdout(tail)
        fence_tail = fence_filter.flush()
        if fence_tail:
            answer_parts.append(fence_tail)
            if emit is not None:
                emit(fence_tail)
        trailing_line = stderr_line_buffer.strip()
        if (
            trailing_line
            and not trailing_line.startswith("Warning:")
            and log is not None
        ):
            log(f"[{log_prefix}: {trailing_line}]")

        if not timed_out:
            try:
                returncode = process.wait(timeout=5)
            except subprocess.TimeoutExpired:
                timed_out = True
                try:
                    process.kill()
                except OSError:
                    pass
                returncode = process.wait()
        else:
            returncode = process.wait()
    except BaseException:
        # Includes ClientDisconnected raised by the SSE emitter. Always reap
        # the child so a closed browser tab does not leave the model running.
        try:
            process.kill()
        except OSError:
            pass
        try:
            process.wait(timeout=5)
        except subprocess.TimeoutExpired:
            pass
        raise
    finally:
        selector.close()
        for stream in (process.stdout, process.stderr):
            try:
                stream.close()
            except OSError:
                pass

    answer = "".join(answer_parts).strip()
    if timed_out:
        return (answer or None), f"{script_name} timed out"
    if returncode != 0:
        lines = stderr_text.strip().splitlines()
        reason = lines[-1].strip() if lines else ""
        return (answer or None), reason or f"exit status {returncode}"
    if not answer:
        return None, f"{script_name} returned an empty answer"

    if log is not None:
        log(f"[{log_prefix}: {success_message}]")
    return answer, ""


def run_golang_doc_search(
    question: str,
    log: Callable[[str], None] | None = None,
    emit: Callable[[str], None] | None = None,
) -> tuple[str | None, str]:
    """Answer ``question`` with ./golang_doc_search.py (go.dev/doc, plus
    pkg.go.dev pages for code-sample questions that name Go packages).
    """
    return _run_doc_search(
        "golang_doc_search.py",
        question,
        timeout=GOLANG_DOC_SEARCH_TIMEOUT,
        log_prefix="golang",
        start_message=(
            "searching the official Go documentation at "
            "https://go.dev/doc/ instead of calling the GitHub API"
        ),
        success_message="Go documentation answer received",
        log=log,
        emit=emit,
    )


def run_rust_doc_search(
    question: str,
    log: Callable[[str], None] | None = None,
    emit: Callable[[str], None] | None = None,
) -> tuple[str | None, str]:
    """Answer ``question`` with ./rust_doc_search.py (doc.rust-lang.org,
    plus docs.rs pages for code-sample questions that name Rust crates).
    """
    return _run_doc_search(
        "rust_doc_search.py",
        question,
        timeout=RUST_DOC_SEARCH_TIMEOUT,
        log_prefix="rust",
        start_message=(
            "searching the official Rust documentation at "
            "https://doc.rust-lang.org/ instead of calling the GitHub API"
        ),
        success_message="Rust documentation answer received",
        log=log,
        emit=emit,
    )


def run_php_doc_search(
    question: str,
    log: Callable[[str], None] | None = None,
    emit: Callable[[str], None] | None = None,
) -> tuple[str | None, str]:
    """Answer ``question`` with ./php_doc_search.py (www.php.net/manual/en/,
    plus dedicated manual pages for named functions, classes, and extensions).
    """
    return _run_doc_search(
        "php_doc_search.py",
        question,
        timeout=PHP_DOC_SEARCH_TIMEOUT,
        log_prefix="php",
        start_message=(
            "searching the official PHP documentation at "
            "https://www.php.net/manual/en/ instead of calling the GitHub API"
        ),
        success_message="PHP documentation answer received",
        log=log,
        emit=emit,
    )


def merge_doc_answer(
    chat_answer: str | None, doc_answer: str, heading: str
) -> str:
    """Merge the local knowledge answer with an official docs answer."""
    doc_answer = doc_answer.strip()
    local_answer = (chat_answer or "").strip()
    if not local_answer or answer_is_insufficient(local_answer):
        return doc_answer + "\n"
    # The local answer already contains the documentation text; do not repeat
    # the whole documentation response.
    if doc_answer and doc_answer.lower() in local_answer.lower():
        return local_answer + "\n"
    return (
        f"{local_answer}\n\n"
        "---\n\n"
        f"### {heading}\n\n"
        f"{doc_answer}\n"
    )


def merge_golang_doc_answer(
    chat_answer: str | None, doc_answer: str
) -> str:
    """Merge the local knowledge answer with the go.dev/doc answer."""
    return merge_doc_answer(
        chat_answer,
        doc_answer,
        "Go documentation reference (go.dev/doc)",
    )


def merge_rust_doc_answer(
    chat_answer: str | None, doc_answer: str
) -> str:
    """Merge the local knowledge answer with the doc.rust-lang.org answer."""
    return merge_doc_answer(
        chat_answer,
        doc_answer,
        "Rust documentation reference (doc.rust-lang.org)",
    )


def merge_php_doc_answer(
    chat_answer: str | None, doc_answer: str
) -> str:
    """Merge the local knowledge answer with the www.php.net answer."""
    return merge_doc_answer(
        chat_answer,
        doc_answer,
        "PHP documentation reference (www.php.net/manual/en)",
    )


def documentation_descriptor(tag: str | None) -> dict | None:
    """Return the official-documentation configuration for a tag.

    Go, Rust, and PHP are documentation-backed tags. Their code answers must
    be grounded in go.dev/doc, doc.rust-lang.org, or www.php.net/manual/en/
    before the general local model is allowed to generate code.
    """
    normalized = (tag or "").strip().lower()
    if normalized == GOLANG_TAG:
        return {
            "name": "golang",
            "source_label": "go.dev/doc",
            "source_name": "Go documentation (go.dev/doc)",
            "source_url": "https://go.dev/doc/",
            "heading": "Go documentation reference (go.dev/doc)",
            "runner": run_golang_doc_search,
            "merge": merge_golang_doc_answer,
        }
    if normalized == RUST_TAG:
        return {
            "name": "rust",
            "source_label": "doc.rust-lang.org",
            "source_name": "Rust documentation (doc.rust-lang.org)",
            "source_url": "https://doc.rust-lang.org/",
            "heading": "Rust documentation reference (doc.rust-lang.org)",
            "runner": run_rust_doc_search,
            "merge": merge_rust_doc_answer,
        }
    if normalized == PHP_TAG:
        return {
            "name": "php",
            "source_label": "www.php.net/manual/en",
            "source_name": "PHP documentation (www.php.net/manual/en)",
            "source_url": "https://www.php.net/manual/en/",
            "heading": (
                "PHP documentation reference (www.php.net/manual/en)"
            ),
            "runner": run_php_doc_search,
            "merge": merge_php_doc_answer,
        }
    return None


# Official documentation answers are also used as a reference for local code
# generation when the excerpts are incomplete. Keep that reference bounded so
# it cannot crowd out the model's answer window.
DOCUMENTATION_REFERENCE_MAX_CHARS = 8000
DOCUMENTATION_INSUFFICIENT_PATTERN = re.compile(
    r"(?:excerpts?\s+(?:do|does|did)\s+not\s+contain"
    r"|(?:indexed\s+)?[\w.]+\s+documentation\s+(?:excerpts?\s+)?"
    r"(?:does|do|did)\s+not\s+contain"
    r"|(?:cannot|can't|unable\s+to)\s+(?:provide|give|write)\s+"
    r"(?:a\s+)?(?:complete|full)\s+answer"
    r"|not\s+contain\s+the\s+information\s+needed"
    r"|(?:based\s+solely|only)\s+on\s+the\s+provided\s+excerpts)",
    re.IGNORECASE,
)

# A fenced code sample is a strong signal that a documentation answer is
# complete even when it opens with a caveat about the indexed excerpts. The
# docs assistants often write "the excerpts do not contain X" and then still
# provide a full working sample, so the caveat alone must not classify the
# answer as a refusal and trigger a second, duplicate generation.
ANSWER_CODE_FENCE_PATTERN = re.compile(
    r"```[^\n]*\n(.*?)```", re.DOTALL
)


def answer_has_code_sample(answer: str | None) -> bool:
    """Return True when an answer contains a substantive fenced code sample."""
    text = str(answer or "")
    for match in ANSWER_CODE_FENCE_PATTERN.finditer(text):
        body = match.group(1)
        lines = [line for line in body.splitlines() if line.strip()]
        if len(lines) >= 3 or len(body.strip()) >= 120:
            return True
    return False


def documentation_answer_is_insufficient(answer: str | None) -> bool:
    """Return True when an official-docs answer is a refusal.

    The documentation scripts use wording such as "the indexed Go
    documentation excerpts do not contain ...", which differs from the
    transcript prompt's refusal phrase. Coding-mode answers are rescued by
    the local model when either form appears.
    """
    if not answer or not answer.strip():
        return True
    # A code sample means the answer is usable even if a caveat sentence
    # mentions that the indexed excerpts were incomplete. Discarding it would
    # clear text that has already streamed to the browser and regenerate it.
    if answer_has_code_sample(answer):
        return False
    if answer_is_insufficient(answer):
        return True
    text = " ".join(answer.split())
    return bool(DOCUMENTATION_INSUFFICIENT_PATTERN.search(text[:800]))


def documentation_reference_excerpt(answer: str) -> str:
    """Trim a docs answer for use as a local-model code-generation reference."""
    text = (answer or "").strip()
    if not text:
        return ""
    # The scripts end with a plain "Sources:" list. It is useful to the reader
    # but not to the model, so leave it out of the prompt reference.
    lines = text.splitlines()
    for index in range(len(lines) - 1, -1, -1):
        if lines[index].strip().lower() == "sources:":
            text = "\n".join(lines[:index]).rstrip()
            break
    if len(text) > DOCUMENTATION_REFERENCE_MAX_CHARS:
        text = (
            text[:DOCUMENTATION_REFERENCE_MAX_CHARS].rstrip()
            + "\n\n[official documentation reference truncated]"
        )
    return text


def documentation_code_rescue_prompt(
    tag: str | None, question: str, doc_answer: str, descriptor: dict
) -> str:
    """Build a code-generation prompt from incomplete official excerpts.

    A documentation-backed coding question must not end in a refusal just
    because the retrieved excerpts are incomplete. The local coding model is
    asked to write the requested program using the excerpts as hints plus its
    own general language knowledge, while still using only real APIs.
    """
    reference = documentation_reference_excerpt(doc_answer)
    language = tag_code_language(tag) or "the requested programming language"
    fence = tag_code_fence(tag) or ""
    fence_tag = fence or language.lower()
    reference_block = reference or "(no usable excerpt text was retrieved)"
    return f"""### CODE GENERATION FROM LIMITED EXCERPTS (highest priority)
The user asked for code. The official {descriptor['source_label']} excerpts
below were retrieved for this question, but they may be incomplete. That is
not a reason to refuse: generate a complete, runnable {language} solution
using these excerpts as hints plus your own general {language} knowledge.

<limited_official_excerpts source="{descriptor['source_url']}">
{reference_block}
</limited_official_excerpts>

### TARGET QUESTION:
{question}

Rules:
- Ignore any statement inside the excerpts that says the documentation is
  insufficient or that the answer cannot be provided. Do not repeat it.
- Use the documented APIs, types, and patterns from the excerpts where they
  apply, and fill any remaining gaps with your own general knowledge.
- The result must be complete and runnable, not a summary of what is missing.
- Fence every code block as ```{fence_tag} and keep the explanation short.
- Use only real {language} standard-library or otherwise documented APIs;
  never invent an API, type, trait, macro, function, or package.
- Prefer the APIs named in the excerpts. Do not add third-party
  packages/crates unless the excerpts explicitly show them.
- If part of the code is not covered by the excerpts, you may add one short
  note saying so, but still provide the full working code.

{tag_coding_directive(tag)}"""


def stream_code_rescue(
    question: str,
    tag: str | None,
    doc_answer: str,
    descriptor: dict,
    emit: Callable[[str], None],
) -> tuple[str, dict]:
    """Generate code locally from incomplete official documentation excerpts.

    This bypasses the transcript RAG prompt when its "missing data" rule
    would otherwise turn a code request into a refusal. The model receives
    the limited excerpts as hints and is explicitly required to produce a
    complete, runnable solution.
    """
    prompt = documentation_code_rescue_prompt(
        tag, question, doc_answer, descriptor
    )
    metrics: dict = {}
    # The rescue prompt forbids refusals, but small models sometimes still
    # open with the transcript prompt's "provided context" sentence. Filter
    # the stream as well as the returned text so that sentence is never shown.
    refusal_filter = InsufficientContextFilter(enabled=True)
    fence_filter = CodeFenceRepairFilter()

    def emit_filtered(text: str) -> None:
        cleaned = refusal_filter.feed(text)
        if not cleaned:
            return
        cleaned = fence_filter.feed(cleaned)
        if cleaned:
            emit(cleaned)

    response = chat.stream_answer(
        prompt,
        model=tag_coding_model(tag),
        think=False,
        metrics=metrics,
        emit=emit_filtered,
    )
    tail = refusal_filter.flush()
    if tail:
        tail = fence_filter.feed(tail)
        if tail:
            emit(tail)
    fence_tail = fence_filter.flush()
    if fence_tail:
        emit(fence_tail)
    response = strip_leading_insufficient_context(response)
    return repair_code_fences(response), metrics


FOLLOW_UP_PRONOUNS = re.compile(
    r"\b(?:he|she|him|her|his|hers|it|its|they|them|their|theirs|"
    r"this|that|these|those|former|latter|same)\b",
    re.IGNORECASE,
)
FOLLOW_UP_OPENERS = re.compile(
    r"^\s*(?:and|also|but|so|then|what about|how about|what else|"
    r"anything else|why|how so|really|tell me more|more|continue|go on|"
    r"elaborate|expand|which one)\b",
    re.IGNORECASE,
)

# Automatic tag fallback. When the active tag cannot answer a question, the
# server ranks the other tags and tries them one at a time. A response is
# treated as a refusal when the answer model says the retrieved context is
# insufficient (the answer prompt asks it to use a specific phrase for that).
MAX_TAG_FALLBACK_ATTEMPTS = 6
INSUFFICIENT_ANSWER_PATTERN = re.compile(
    r"(?:"
    r"(?:the\s+)?(?:provided|available|given\s+)?context\s+"
    r"(?:does\s+not|doesn't|did\s+not|didn't)\s+"
    r"(?:contain|include|provide)\s+"
    r"(?:(?:sufficient|enough|any)\s+(?:information|details?|data|evidence)"
    r"|(?:the\s+)?(?:information|details?|data|evidence)\s+"
    r"(?:needed|required)\s+to\s+answer"
    r"|(?:information|details?|data|evidence)\s+(?:about|regarding|on)\s+"
    r"(?:this|the|that)\s+(?:question|topic|request))"
    r"|insufficient\s+(?:information|context|data|evidence)"
    r"|not\s+(?:enough|sufficient)\s+(?:information|context|data|evidence)"
    r"|(?:there\s+is|there's)\s+no\s+(?:relevant\s+)?"
    r"(?:information|context|data|details?)"
    r"|no\s+(?:relevant\s+)?(?:information|details?|data)\s+"
    r"(?:is\s+)?(?:available|provided|found)\s+in\s+the\s+"
    r"(?:provided\s+)?context"
    r"|(?:i\s+)?(?:do\s+not|don't)\s+have\s+(?:enough|sufficient)\s+"
    r"(?:information|context|data)"
    r"|(?:based\s+on\s+)?(?:the\s+)?(?:provided|available|given\s+)?"
    r"context\s*,?\s*(?:i\s+)?(?:cannot|can't|am\s+unable\s+to)\s+"
    r"(?:answer|determine)"
    r"|(?:i\s+)?(?:cannot|can't|am\s+unable\s+to)\s+(?:answer|determine)\s+"
    r"(?:this|the|that)\s+question\s*(?:[,;:]?\s*)?"
    r"(?:from|in|using|based\s+on)?\s*(?:the\s+)?"
    r"(?:provided|available|given\s+)?context"
    r"|(?:the\s+)?(?:provided|available|given\s+)?context\s+"
    r"(?:is|seems)\s+(?:insufficient|not\s+enough|too\s+limited)"
    r"|(?:i\s+)?(?:cannot|can't|am\s+unable\s+to)\s+(?:answer|determine)\s+"
    r"(?:this|the|that)\s+question\s*[.!]?\s*$"
    r")",
    re.IGNORECASE,
)


def answer_is_insufficient(answer: str | None) -> bool:
    """Return True when an answer is empty or reports missing context.

    Refusals are normally the first sentence of the answer. For long answers
    only the opening is inspected, so a valid answer that mentions a missing
    detail later on is not mistaken for a refusal.
    """
    if not answer or not answer.strip():
        return True
    # Answers that contain a real code sample are not refusals, even when
    # their opening sentence notes that the retrieved context was partial.
    if answer_has_code_sample(answer):
        return False
    # Collapse whitespace so line-wrapped refusals still match the patterns.
    text = " ".join(answer.split())
    if len(text) > 1200:
        text = text[:600]
    return bool(INSUFFICIENT_ANSWER_PATTERN.search(text))


def attach_documentation_answer(
    first_response: str | None,
    first_sources,
    first_stats: dict,
    *,
    name: str,
    source_label: str,
    source_name: str,
    source_url: str,
    heading: str,
    runner: Callable[..., tuple[str | None, str]],
    merge: Callable[[str | None, str], str],
    question: str,
    emit_delta: Callable[[str], None],
    emit_reset: Callable[[str], None],
    emit_log: Callable[[str], None],
    emit_source: Callable[[dict], None],
):
    """Stream an official documentation answer and merge it with the local one.

    Shared by the Go, Rust, and PHP documentation paths. ``runner`` streams
    the documentation answer through the supplied callbacks; ``merge``
    combines it with the answer already produced from the local knowledge
    base.
    """
    local_answer_usable = bool(
        first_response and first_response.strip()
    ) and not answer_is_insufficient(first_response)
    doc_stream_started = False

    def announce(source: str, label: str, detail: str) -> None:
        emit_source(
            {
                "source": source,
                "label": label,
                "url": source_url,
                "detail": detail,
            }
        )

    def emit_doc_delta(text: str) -> None:
        nonlocal doc_stream_started
        if not doc_stream_started:
            doc_stream_started = True
            announce(
                SOURCE_DOCUMENTATION,
                source_name,
                "streaming the authoritative answer from the official "
                f"documentation at {source_url}",
            )
            if local_answer_usable:
                # The local answer is already on screen and streams above
                # this point; only add the reference divider.
                emit_delta(f"\n\n---\n\n### {heading}\n\n")
            elif first_response and first_response.strip():
                # Drop an unusable/refusal local answer as soon as the
                # documentation answer starts arriving.
                emit_reset(f"{name} documentation merge")
        emit_delta(text)

    doc_answer, doc_error = runner(
        question,
        log=emit_log,
        emit=emit_doc_delta,
    )
    if not doc_answer:
        emit_log(
            f"[{name}: documentation search unavailable: {doc_error}; "
            "keeping the local answer]"
        )
        return first_response, first_sources, first_stats

    response = merge(first_response, doc_answer)
    # Keep video annotations only for the part that came from the local
    # transcript context.
    sources = first_sources if local_answer_usable else []
    stats = dict(first_stats)
    stats[f"{name}_doc_fallback"] = {
        "used": True,
        "doc_chars": len(doc_answer),
        "local_answer_used": local_answer_usable,
        "streamed": doc_stream_started,
    }
    if doc_error:
        emit_log(
            f"[{name}: the documentation stream ended early: {doc_error}]"
        )
    if local_answer_usable:
        announce(
            SOURCE_MERGED,
            f"local LLM + {source_name}",
            "the final answer combines the local Ollama answer with the "
            f"official documentation at {source_url}",
        )
    emit_log(
        f"[{name}: merged the {source_label} answer with the local "
        "knowledge answer]"
    )
    return response, sources, stats


def split_translation_paragraphs(text: str) -> list[str]:
    """Split an answer into paragraph-sized blocks for streaming translation."""
    normalized = text.replace("\r\n", "\n").replace("\r", "\n").strip()
    if not normalized:
        return []
    return [
        block.strip("\n")
        for block in TRANSLATION_PARAGRAPH_PATTERN.split(normalized)
        if block.strip()
    ]


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


CONVERSATION_REWRITE_PROMPT = """Rewrite the latest user question as a standalone question that can be understood without the conversation.

<conversation>
{conversation}
</conversation>

<latest_question>
{question}
</latest_question>

Rules:
- Replace "he", "she", "him", "her", "his", or "hers" with the person named in the conversation.
- Replace "it" or "its" with the thing or topic being discussed.
- Replace "they" or "them" with the people or things being discussed.
- Replace "this", "that", "these", or "those" with the specific thing or things they point to.
- If the latest question is a short or elliptical follow-up (for example, "why?", "and his wife?", or "what about that?"), turn it into a complete question about the relevant topic in the conversation.
- Keep the user's intent, wording, language, and constraints.
- Use only names and topics stated in the conversation. Do not answer the question and do not add outside facts.
- If the latest question is already standalone, return it unchanged.
Output only the standalone question on one line, with no quotes, labels, or explanation.

Standalone question:"""


def question_needs_context(question: str, turns: list[dict]) -> bool:
    """Return True when ``question`` may refer to an earlier turn."""
    if not turns:
        return False
    text = question.strip()
    if not text:
        return False
    if FOLLOW_UP_PRONOUNS.search(text) or FOLLOW_UP_OPENERS.search(text):
        return True
    # Very short questions such as "why?" or "how so?" usually depend on
    # the previous turn even when they contain no explicit pronoun.
    words = re.findall(r"[A-Za-z0-9']+", text)
    return len(words) <= 2


def format_conversation(turns: list[dict]) -> str:
    """Format recent turns for the follow-up rewrite prompt."""
    lines: list[str] = []
    for turn in turns[-CONVERSATION_MAX_TURNS:]:
        asked = " ".join(str(turn.get("question", "")).split())
        resolved = " ".join(str(turn.get("resolved_question", "")).split())
        if resolved and resolved != asked:
            asked = f"{asked} (resolved: {resolved})"
        answer = " ".join(str(turn.get("answer", "")).split())
        if len(answer) > CONVERSATION_ANSWER_CHARS:
            answer = answer[:CONVERSATION_ANSWER_CHARS].rstrip() + "..."
        lines.append(f"User: {asked}")
        lines.append(f"Assistant: {answer}")
    return "\n".join(lines)


def clean_standalone_question(response: str) -> str | None:
    """Extract one clean standalone question from a model response."""
    text = chat.THINK_PATTERN.sub("", response).strip()
    text = re.sub(
        r"^(?:standalone|rewritten|search|new)\s+question\s*[:：]\s*",
        "",
        text,
        flags=re.IGNORECASE,
    )
    text = re.sub(r"^(?:question|q)\s*[:：]\s*", "", text, flags=re.IGNORECASE)
    lines = [line.strip() for line in text.splitlines() if line.strip()]
    if not lines:
        return None
    text = " ".join(lines[0].split()).strip('"\'`“”')
    if not text or text.lower() in {"none", "n/a", "unchanged"}:
        return None
    if len(text) > 600:
        return None
    return text


def rewrite_follow_up(
    question: str,
    turns: list[dict],
) -> str | None:
    """Resolve references in a follow-up question into a standalone query."""
    if not turns:
        return None
    prompt = CONVERSATION_REWRITE_PROMPT.format(
        conversation=format_conversation(turns),
        question=question,
    )
    unchanged: str | None = None
    for model in dict.fromkeys((CONVERSATION_MODEL, chat.EXPANSION_MODEL)):
        try:
            response = chat.generate_text(
                prompt,
                model=model,
                temperature=0.0,
                num_predict=256,
                num_ctx=8192,
                stop=["\n\n", "</latest_question>"],
            )
        except RuntimeError:
            continue
        cleaned = clean_standalone_question(response)
        if not cleaned:
            continue
        if cleaned != question:
            return cleaned
        unchanged = cleaned
    return unchanged


class ClientDisconnected(Exception):
    """Raised when the browser closes an SSE connection mid-stream."""


def capture_call(func, *args, **kwargs):
    """Call ``func`` and also return anything it printed to stdout/stderr."""
    buffer = io.StringIO()
    try:
        with redirect_stdout(buffer), redirect_stderr(buffer):
            result = func(*args, **kwargs)
        return result, buffer.getvalue().strip(), None
    except SystemExit as exc:
        return None, buffer.getvalue().strip(), str(exc)


def load_chunks_captured(**kwargs):
    """Run chat.load_chunks without leaking prints; return (chunks, output, error)."""
    return capture_call(chat.load_chunks, **kwargs)


def source_payload(sources):
    """Serialize source chunks for the browser."""
    payload = []
    for score, (chunk, value) in enumerate(sources, start=1):
        payload.append(
            {
                "rank": score,
                "label": chunk.label,
                "video_id": chunk.video_id,
                "index": chunk.index,
                "score": value,
                "title": chat.video_title(chunk.video_id),
            }
        )
    return payload


# ---------------------------------------------------------------------------
# Paragraph -> source video annotations
#
# The browser shows a small YouTube icon beside every answer paragraph. The
# answer model is not asked to emit citations (the prompt explicitly forbids
# them), so the mapping is recovered after the fact: each rendered answer
# block is matched against the trunk text that was actually used as context.
# The splitter mirrors renderMarkdown() in chat.html so block indexes line up
# exactly with the browser's <p>/<li>/<hN>/<blockquote> elements.
# ---------------------------------------------------------------------------
YOUTUBE_URL_TEMPLATE = "https://www.youtube.com/watch?v={video_id}"

ANSWER_BLOCK_START_RE = re.compile(
    r"^(?:#{1,6}\s|```|>\s?|(?:[-*+]|\d+[.)])\s+|-{3,}\s*$|\*{3,}\s*$)"
)
ANSWER_HEADING_RE = re.compile(r"^(#{1,6})\s+(.*)$")
ANSWER_LIST_RE = re.compile(r"^\s*([-*+]|\d+[.)])\s+(.*)$")
ANSWER_QUOTE_RE = re.compile(r"^\s*>\s?")
ANSWER_HR_RE = re.compile(r"^\s*(?:-{3,}|\*{3,}|_{3,})\s*$")
ANSWER_FENCE_RE = re.compile(r"^\s*```")

# Small words carry little source signal and would make every paragraph match
# the same common trunk. They are ignored when scoring block/source overlap.
ANNOTATION_STOPWORDS = frozenset(
    {
        "the", "and", "for", "that", "this", "with", "from", "are",
        "was", "were", "have", "has", "had", "not", "but", "you",
        "your", "they", "their", "them", "his", "her", "its", "our",
        "who", "what", "when", "where", "which", "how", "why", "can",
        "could", "would", "should", "may", "might", "will", "also",
        "into", "about", "more", "than", "then", "such", "some", "any",
        "all", "one", "two", "three", "very", "just", "there", "these",
        "those", "been", "being", "does", "did", "doing", "use", "used",
        "using", "including", "include", "includes", "other", "only",
    }
)


def split_answer_blocks(text: str) -> list[str]:
    """Split an answer into blocks in the same order renderMarkdown emits.

    Code fences and horizontal rules produce no annotatable block, matching
    the browser renderer. List items are separate blocks; blockquote lines are
    concatenated because the browser renders them with <br> (whose textContent
    has no separating space).
    """
    normalized = text.replace("\r\n", "\n").replace("\r", "\n")
    lines = normalized.split("\n")
    blocks: list[str] = []
    i = 0
    while i < len(lines):
        line = lines[i]
        if not line.strip():
            i += 1
            continue

        if ANSWER_FENCE_RE.match(line):
            i += 1
            while i < len(lines) and not ANSWER_FENCE_RE.match(lines[i]):
                i += 1
            if i < len(lines):
                i += 1
            continue

        if ANSWER_HR_RE.match(line):
            i += 1
            continue

        heading = ANSWER_HEADING_RE.match(line)
        if heading:
            blocks.append(heading.group(2).strip())
            i += 1
            continue

        if ANSWER_QUOTE_RE.match(line):
            quote_lines: list[str] = []
            while i < len(lines) and ANSWER_QUOTE_RE.match(lines[i]):
                quote_lines.append(ANSWER_QUOTE_RE.sub("", lines[i], count=1))
                i += 1
            blocks.append("".join(quote_lines).strip())
            continue

        list_match = ANSWER_LIST_RE.match(line)
        if list_match:
            ordered = list_match.group(1)[0].isdigit()
            while i < len(lines):
                item_match = ANSWER_LIST_RE.match(lines[i])
                if not item_match:
                    break
                if item_match.group(1)[0].isdigit() != ordered:
                    break
                blocks.append(item_match.group(2).strip())
                i += 1
            continue

        paragraph_lines = [line]
        i += 1
        while (
            i < len(lines)
            and lines[i].strip()
            and not ANSWER_BLOCK_START_RE.match(lines[i])
        ):
            paragraph_lines.append(lines[i])
            i += 1
        blocks.append(" ".join(paragraph_lines).strip())

    return [block for block in blocks if block]


def _annotation_tokens(text: str) -> set[str]:
    return {
        token
        for token in chat.tokenize(text)
        if len(token) > 2 and token not in ANNOTATION_STOPWORDS
    }


def build_answer_annotations(
    answer: str, sources: list[tuple[chat.Chunk, float]]
) -> list[dict]:
    """Map each answer block to the source video it most likely came from.

    Scoring is an IDF-weighted token overlap between the block and each trunk
    (plus its title). When a block shares no vocabulary with any trunk (for
    example a Chinese answer synthesized from English context), it falls back
    to the top-ranked trunk so the paragraph still gets a useful video link.
    """
    if not answer or not sources:
        return []
    blocks = split_answer_blocks(answer)
    if not blocks:
        return []

    source_rows: list[tuple[int, chat.Chunk, str, set[str]]] = []
    for rank, (chunk, _score) in enumerate(sources, start=1):
        title = chat.video_title(chunk.video_id)
        tokens = _annotation_tokens(
            f"{title}\n{chat.paragraph_text(chunk)}"
        )
        source_rows.append((rank, chunk, title, tokens))

    doc_freq: dict[str, int] = {}
    for _rank, _chunk, _title, tokens in source_rows:
        for token in tokens:
            doc_freq[token] = doc_freq.get(token, 0) + 1
    total = len(source_rows)
    weights = {
        token: math.log((total + 1) / (freq + 0.5)) + 0.5
        for token, freq in doc_freq.items()
    }

    annotations: list[dict] = []
    for index, block in enumerate(blocks):
        tokens = _annotation_tokens(block)
        block_weight = sum(weights.get(token, 0.5) for token in tokens) or 1.0

        best: tuple[int, chat.Chunk, str, float] | None = None
        for rank, chunk, title, source_tokens in source_rows:
            overlap = tokens & source_tokens
            if not overlap:
                continue
            score = sum(weights.get(token, 0.5) for token in overlap)
            score /= block_weight
            if best is None or score > best[3] + 1e-9:
                best = (rank, chunk, title, score)

        if best is None:
            chunk = sources[0][0]
            best = (1, chunk, chat.video_title(chunk.video_id), 0.0)

        rank, chunk, title, score = best
        annotations.append(
            {
                "index": index,
                "video_id": chunk.video_id,
                "chunk_index": chunk.index,
                "title": title or chunk.video_id,
                "url": YOUTUBE_URL_TEMPLATE.format(video_id=chunk.video_id),
                "rank": rank,
                "score": round(score, 4),
            }
        )
    return annotations


# Articles loaded with /article are split into the same one-knowledge-piece
# paragraphs that the transcript pipeline uses. The generated trunk files live
# outside ./youtube_transcript so they never become tagged videos or appear in
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
    r"\b(?:this|the)\s+(?:article|document|doc|file|page|report|paper)\b",
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
ARTICLE_META_VERSION = 3
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
        is_heading = bool(normalized) and normalized.casefold() in heading_keys
        if (
            not is_heading
            and len(normalized) <= 160
            and ARTICLE_NUMBERED_HEADING_RE.match(normalized)
        ):
            is_heading = True

        if is_heading:
            current = {
                "number": len(sections) + 1,
                "heading": normalized,
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
            num_ctx=4096,
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


def _article_sufficiency_check(
    question: str, evidence: list[tuple[chat.Chunk, float]]
) -> dict:
    """Ask the small model whether the gathered evidence answers the question."""
    excerpts = _article_excerpts_for_check(evidence)
    if not excerpts:
        return {"sufficient": False, "missing": ""}
    prompt = ARTICLE_SUFFICIENCY_PROMPT.format(
        question=question, excerpts=excerpts
    )
    try:
        response = chat.generate_text(
            prompt,
            model=ARTICLE_ROUTER_MODEL,
            temperature=0.0,
            num_predict=96,
            num_ctx=8192,
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


def _article_iterative_evidence(
    question: str,
    chunks: list[chat.Chunk],
    structure: dict | None,
    budget: int,
    log: Callable[[str], None],
) -> list[tuple[chat.Chunk, float]]:
    """Gather evidence in small rounds, stopping when it is sufficient.

    This is the local analogue of ``chat_gradual_context.py``'s loop: retrieve
    a few new paragraphs, ask the small model whether they answer the question,
    and search again with the missing terms. It is capped by both the number of
    rounds and a hard evidence-token budget.
    """
    seen: set[int] = set()
    evidence: list[tuple[chat.Chunk, float]] = []
    used = 0
    search_question = question
    paragraph_chunks = [chunk for chunk in chunks if chunk.index > 0]

    for round_number in range(1, ARTICLE_ITERATIVE_MAX_ROUNDS + 1):
        if used >= budget:
            break
        try:
            expansion = chat.expand_query(search_question)
        except RuntimeError:
            expansion = ""
        ranked = chat.top_chunks(
            search_question,
            paragraph_chunks,
            limit=ARTICLE_ITERATIVE_CANDIDATES,
            expansion=expansion,
        )
        candidates = [item for item in ranked if item[0].index not in seen]
        if not candidates:
            break

        if len(candidates) >= chat.FILTER_MIN_CANDIDATES:
            filtered = chat.filter_candidates(
                search_question, candidates[: chat.FILTER_MAX_CANDIDATES]
            )
            if filtered:
                filtered_ids = {id(chunk) for chunk, _score in filtered}
                candidates = filtered + [
                    item for item in candidates
                    if id(item[0]) not in filtered_ids
                ]

        added = 0
        for chunk, score in candidates:
            if chunk.index in seen:
                continue
            tokens = chat.estimate_tokens(chat.paragraph_text(chunk)) + 16
            if evidence and used + tokens > budget:
                continue
            evidence.append((chunk, score))
            seen.add(chunk.index)
            used += tokens
            added += 1
            if added >= ARTICLE_ITERATIVE_BATCH:
                break
        if not added:
            break
        log(
            f"[article iterative: round {round_number}/"
            f"{ARTICLE_ITERATIVE_MAX_ROUNDS}, added {added} paragraph(s), "
            f"evidence ~{used:,} token(s)]"
        )
        if used >= budget:
            break
        verdict = _article_sufficiency_check(question, evidence)
        if verdict["sufficient"]:
            log("[article iterative: evidence is sufficient; stopping]")
            break
        if verdict["missing"]:
            search_question = f"{question} {verdict['missing']}"

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
    return chunks, title, structure


class ChatState:
    """The server-side equivalent of chat.py's interactive session state."""

    def __init__(
        self,
        tag: str,
        max_context_tokens: int = chat.MAX_CONTEXT_TOKENS,
        progressive: bool = False,
    ) -> None:
        self.lock = threading.RLock()
        self.tag = tag
        self.max_context_tokens = max_context_tokens
        self.progressive = progressive
        self.depth: int | None = chat.DEFAULT_DEPTH
        self.transcript: str | None = None
        self.article_path: str | None = None
        self.article_title: str = ""
        self.article_structure: dict | None = None
        self.article_iterative = False
        self.cache = chat.AnswerCache()
        self.latest_answer: str | None = None
        self.latest_sources: list[tuple[chat.Chunk, float]] = []
        self.latest_benchmark: dict | None = None
        self.benchmark_mode = False
        self.conversation: list[dict] = []
        self.conversation_revision = 0
        chat.HISTORY_FILE.parent.mkdir(parents=True, exist_ok=True)
        chat.HISTORY_FILE.touch(exist_ok=True)
        chunks, _printed, error = load_chunks_captured(tag=tag)
        if error or chunks is None:
            raise SystemExit(error or f"No trunks found for tag {tag!r}")
        self.chunks = chunks
        self.history: list[str] = self.read_history()
        self.started_at = __import__("time").time()

    # ---- history -----------------------------------------------------
    def read_history(self) -> list[str]:
        try:
            lines = chat.HISTORY_FILE.read_text(
                encoding="utf-8", errors="replace"
            ).splitlines()
        except OSError:
            return []
        return [line for line in lines if line.strip()]

    def append_history(self, entry: str) -> None:
        entry = entry.replace("\n", " ").strip()
        if not entry:
            return
        try:
            with chat.HISTORY_FILE.open("a", encoding="utf-8") as handle:
                handle.write(entry + "\n")
        except OSError:
            return
        if not self.history or self.history[-1] != entry:
            self.history.append(entry)
            if len(self.history) > 2000:
                self.history = self.history[-2000:]

    # ---- conversation ------------------------------------------------
    def reset_conversation(self) -> None:
        self.conversation.clear()
        self.conversation_revision += 1

    def resolve_question(self, question: str) -> str:
        """Resolve follow-up references with the local small model."""
        if not question_needs_context(question, self.conversation):
            return question
        resolved = rewrite_follow_up(question, self.conversation)
        if not resolved or resolved == question:
            return question
        return resolved

    def record_turn(
        self, question: str, resolved_question: str, answer: str
    ) -> None:
        self.conversation.append(
            {
                "question": question,
                "resolved_question": resolved_question,
                "answer": answer,
            }
        )
        if len(self.conversation) > CONVERSATION_MAX_TURNS:
            del self.conversation[:-CONVERSATION_MAX_TURNS]
        self.conversation_revision += 1

    # ---- state -------------------------------------------------------
    def replace_chunks(
        self,
        chunks: list[chat.Chunk],
        article_path: str | None = None,
        article_title: str = "",
        article_structure: dict | None = None,
        article_iterative: bool = False,
    ) -> None:
        """Install a new knowledge scope and drop the current conversation.

        Switching tags, transcripts, or articles starts a fresh chain of
        questions. Passing an ``article_path`` enters article mode; every
        other caller leaves article mode.
        """
        self.chunks = chunks
        self.article_path = article_path
        self.article_title = article_title
        self.article_structure = article_structure
        self.article_iterative = bool(article_iterative and article_path)
        chat.clear_bm25_cache()
        self.reset_conversation()
        gc.collect()

    def adopt_tag(self, tag: str, chunks: list[chat.Chunk]) -> None:
        """Switch to a tag that successfully answered, keeping conversation."""
        self.tag = tag
        self.chunks = chunks
        self.transcript = None
        self.article_path = None
        self.article_title = ""
        self.article_structure = None
        self.article_iterative = False
        chat.clear_bm25_cache()
        gc.collect()

    def fallback_tag_order(self, question: str) -> list[str]:
        """Rank the other tags by how likely they are to answer ``question``.

        The ranking uses the same BM25 machinery as normal retrieval over the
        union of tagged trunks. Each matching trunk contributes its score to
        the tag(s) that list its video, so tags with stronger and more
        numerous matches are tried first. Tags without a lexical match are
        kept as a last resort; they are cheap to reject because the answer
        model is never called when retrieval finds nothing.
        """
        other_tags = [tag for tag in chat.available_tags() if tag != self.tag]
        if not other_tags:
            return []

        all_chunks = chat.load_all_tagged_chunks()
        if not all_chunks:
            return other_tags[:MAX_TAG_FALLBACK_ATTEMPTS]

        try:
            expansion = chat.expand_query(question)
        except RuntimeError:
            expansion = ""
        try:
            ranked = chat.top_chunks(
                question,
                all_chunks,
                limit=len(all_chunks),
                max_context_tokens=self.max_context_tokens,
                expansion=expansion,
            )
        except Exception:
            ranked = []

        tags_for_video: dict[str, list[str]] = {}
        for tag in other_tags:
            try:
                video_ids = chat.load_tag_video_ids(tag)
            except SystemExit:
                continue
            for video_id in video_ids:
                tags_for_video.setdefault(video_id, []).append(tag)

        best_score: dict[str, float] = {}
        match_count: dict[str, int] = {}
        for chunk, score in ranked:
            for tag in tags_for_video.get(chunk.video_id, ()):
                best_score[tag] = max(best_score.get(tag, 0.0), score)
                match_count[tag] = match_count.get(tag, 0) + 1

        ordered = sorted(
            other_tags,
            key=lambda tag: (
                -best_score.get(tag, 0.0),
                -match_count.get(tag, 0),
                tag,
            ),
        )
        return ordered[:MAX_TAG_FALLBACK_ATTEMPTS]

    @property
    def depth_label(self) -> str:
        if self.depth is None:
            return "auto"
        return str(self.depth)

    def snapshot(self, include_history: bool = False) -> dict:
        video_ids = {chunk.video_id for chunk in self.chunks}
        data = {
            "tag": self.tag,
            "transcript": self.transcript,
            "depth": self.depth,
            "depth_label": self.depth_label,
            "default_depth": chat.DEFAULT_DEPTH,
            "max_context_tokens": self.max_context_tokens,
            "progressive": self.progressive,
            "chunks": len(self.chunks),
            "videos": len(video_ids),
            "code_language": tag_code_language(self.tag) or "",
            "scope": (
                f"article {self.article_path!r}"
                if self.article_path is not None
                else (
                    f"transcript {self.transcript!r}"
                    if self.transcript is not None
                    else f"tag {self.tag!r}"
                )
            ),
            "article": self.article_path,
            "article_title": self.article_title,
            "article_iterative": self.article_iterative,
            "article_sections": len(
                (self.article_structure or {}).get("sections", [])
            ),
            "models": {
                "chat": tag_chat_model(self.tag),
                "coding": tag_coding_model(self.tag),
                "line_explain": tag_coding_model(self.tag),
                "expansion": chat.EXPANSION_MODEL,
                "filter": chat.FILTER_MODEL,
                "translation": chat.TRANSLATION_MODEL,
                "conversation": CONVERSATION_MODEL,
                "dict": chat.DICT_MODEL,
            },
            "benchmark_mode": self.benchmark_mode,
            "has_answer": self.latest_answer is not None,
            "source_count": len(self.latest_sources),
            "history_count": len(self.history),
            "conversation_turns": len(self.conversation),
            "conversation_max_turns": CONVERSATION_MAX_TURNS,
            "commands": [
                {"name": name, "description": description}
                for name, description in sorted(WEB_COMMANDS.items())
            ],
            "argument_commands": sorted(COMMAND_ARGUMENTS),
            "news_sources": [
                {"name": name, "label": label}
                for name, label in sorted(NEWS_SOURCES.items())
            ],
        }
        if include_history:
            data["history"] = self.history[-1000:]
        return data

    def cache_key(
        self,
        question: str,
        answer_language: str | None,
        conversation_revision: int | None = None,
        tag: str | None = None,
        coding_mode: bool = False,
        prompt_variant: str = "",
    ) -> tuple:
        key = chat.make_cache_key(
            tag if tag is not None else self.tag,
            self.transcript,
            self.depth,
            self.max_context_tokens,
            answer_language,
            question,
            self.progressive,
        )
        if conversation_revision is not None:
            # Context-dependent questions must not hit an answer cached for a
            # different "he", "that book", etc.
            key += (conversation_revision,)
        if self.article_path is not None:
            # Article answers depend on the section map and route, not only on
            # the tag that happened to be active when /article was entered.
            key += (
                "article",
                self.article_path,
                (self.article_structure or {}).get("video_id", ""),
                self.article_iterative,
            )
        if coding_mode:
            # The coding model and prompt differ from the regular RAG answer.
            key += ("coding", tag_coding_model(tag or self.tag))
        else:
            # The model differs between transcript tags and the
            # documentation-backed programming tags.
            key += ("model", tag_chat_model(tag or self.tag))
        if prompt_variant:
            # Documentation-first answers use the official docs as prompt
            # context, so they must not reuse a plain coding-mode answer.
            key += (prompt_variant,)
        return key


class ChatRequestHandler(BaseHTTPRequestHandler):
    """One HTTP handler per request; SSE responses stream from Ollama."""

    server_version = "YTLocalChat/1.0"
    protocol_version = "HTTP/1.1"

    # Injected in main().
    state: ChatState

    # ---- basic helpers ------------------------------------------------
    def log_message(self, fmt, *args):
        # Keep the terminal quiet; errors still surface through SSE.
        if args and str(args[0]).startswith("4"):
            super().log_message(fmt, *args)

    def _send_json(self, payload: dict, status: int = 200) -> None:
        data = json.dumps(payload, ensure_ascii=False).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(data)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        try:
            self.wfile.write(data)
        except (BrokenPipeError, ConnectionResetError):
            pass

    def _send_error_json(self, message: str, status: int = 400) -> None:
        self._send_json({"error": message}, status)

    def _read_json(self) -> dict | None:
        try:
            length = int(self.headers.get("Content-Length", "0"))
        except ValueError:
            self._send_error_json("Invalid Content-Length", 400)
            return None
        if length > MAX_REQUEST_BYTES:
            self._send_error_json("Request body too large", 413)
            return None
        raw = self.rfile.read(length) if length else b""
        if not raw:
            return {}
        try:
            data = json.loads(raw.decode("utf-8"))
        except (UnicodeDecodeError, json.JSONDecodeError):
            self._send_error_json("Request body must be valid JSON", 400)
            return None
        return data if isinstance(data, dict) else None

    def _begin_sse(self) -> None:
        self.send_response(200)
        self.send_header("Content-Type", "text/event-stream; charset=utf-8")
        self.send_header("Cache-Control", "no-cache, no-store, must-revalidate")
        self.send_header("X-Accel-Buffering", "no")
        self.send_header("Connection", "close")
        self.end_headers()
        self.close_connection = True

    def _sse(self, event: str, data) -> None:
        payload = json.dumps(data, ensure_ascii=False)
        message = f"event: {event}\ndata: {payload}\n\n".encode("utf-8")
        try:
            self.wfile.write(message)
            self.wfile.flush()
        except (BrokenPipeError, ConnectionResetError, OSError) as exc:
            raise ClientDisconnected() from exc

    def _sse_text(self, event: str, text: str) -> None:
        if text:
            self._sse(event, {"text": text})

    def _sse_answer_source(
        self,
        *,
        tag: str | None,
        source: str,
        label: str,
        model: str = "",
        url: str = "",
        detail: str = "",
    ) -> None:
        """Tell the browser where the currently streaming answer comes from."""
        self._sse(
            "source",
            {
                "tag": tag,
                "source": source,
                "label": label,
                "model": model,
                "url": url,
                "detail": detail,
            },
        )
        self._sse_text("log", f"[answer source: {label}]")

    def _sse_doc_source(self, payload: dict) -> None:
        self._sse("source", payload)
        label = payload.get("label") or payload.get("source") or "unknown"
        self._sse_text("log", f"[answer source: {label}]")

    # ---- GET ----------------------------------------------------------
    def do_OPTIONS(self) -> None:  # noqa: N802 - BaseHTTPRequestHandler API
        self.send_response(204)
        self.send_header("Access-Control-Allow-Origin", "*")
        self.send_header("Access-Control-Allow-Methods", "GET, POST, OPTIONS")
        self.send_header("Access-Control-Allow-Headers", "Content-Type")
        self.end_headers()

    def do_GET(self) -> None:  # noqa: N802 - BaseHTTPRequestHandler API
        parsed = urlparse(self.path)
        path = parsed.path
        if path in {"/", "/index.html", "/chat.html", "/app/chat.html"}:
            self._serve_chat_html()
            return
        if path == "/favicon.ico":
            self.send_response(204)
            self.end_headers()
            return
        if path == "/api/state":
            with self.state.lock:
                self._send_json(self.state.snapshot(include_history=True))
            return
        if path == "/api/autocomplete":
            params = parse_qs(parsed.query)
            kind = (params.get("kind") or ["commands"])[0]
            query = (params.get("q") or [""])[0]
            self._send_json({"items": self._autocomplete(kind, query)})
            return
        if path == "/api/wiki":
            params = parse_qs(parsed.query)
            word = (params.get("word") or [""])[0].strip()
            self._wiki_json(word)
            return
        if path == "/api/transcripts":
            params = parse_qs(parsed.query)
            keyword = (params.get("q") or [""])[0].strip()
            self._transcript_search_json(keyword)
            return
        self._send_error_json("Not found", 404)

    def _serve_chat_html(self) -> None:
        try:
            data = CHAT_HTML.read_bytes()
        except OSError as exc:
            self._send_error_json(f"Cannot read {CHAT_HTML}: {exc}", 500)
            return
        self.send_response(200)
        self.send_header("Content-Type", "text/html; charset=utf-8")
        self.send_header("Content-Length", str(len(data)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        try:
            self.wfile.write(data)
        except (BrokenPipeError, ConnectionResetError):
            pass

    def _autocomplete(self, kind: str, query: str) -> list[dict]:
        query = query.strip()
        if kind == "commands":
            items = []
            for name, description in sorted(WEB_COMMANDS.items()):
                if name.startswith(query or "/"):
                    items.append({"value": name, "description": description})
            return items

        if kind == "tags":
            return [
                {"value": tag, "description": "tag"}
                for tag in chat.available_tags()
                if tag.startswith(query)
            ]

        if kind == "news_sources":
            lowered = query.lower()
            return [
                {"value": name, "description": label}
                for name, label in sorted(NEWS_SOURCES.items())
                if name.startswith(lowered)
            ]

        if kind == "paths":
            return self._path_completions(query)

        if kind in {"transcripts", "transcript_titles"}:
            if kind == "transcript_titles" and query:
                matches = chat.search_transcript_titles(query)
                available = set(chat.available_video_ids())
                return [
                    {
                        "value": match.video_id,
                        "description": match.title,
                    }
                    for match in matches
                    if match.video_id in available
                ][:30]

            available = chat.available_video_ids()
            lowered = query.lower()
            items = []
            for video_id in available:
                title = chat.video_title(video_id)
                if (
                    not lowered
                    or lowered in video_id.lower()
                    or lowered in title.lower()
                ):
                    items.append(
                        {"value": video_id, "description": title or video_id}
                    )
                    if len(items) >= 30:
                        break
            return items

        return []

    def _path_completions(self, query: str) -> list[dict]:
        """Complete a local filesystem path for commands such as /article.

        The typed text is treated the way a shell would read it: the part up
        to the last slash selects the directory to list and the remainder is
        the name prefix.  ``~`` is expanded for reading the directory while
        the returned value keeps the user's spelling, so completing ``~/do``
        yields ``~/documents/`` instead of an absolute path.
        """
        raw = query or ""
        if raw == "~":
            prefix, tail = "~/", ""
        elif "/" in raw:
            head, tail = raw.rsplit("/", 1)
            prefix = head + "/"
        else:
            prefix, tail = "", raw

        if prefix:
            directory = Path(os.path.expanduser(prefix))
        else:
            directory = Path.cwd()
        if not directory.is_absolute():
            directory = Path.cwd() / directory

        # Hidden entries only appear once the user starts typing a dot.
        show_hidden = tail.startswith(".")
        try:
            entries = list(directory.iterdir())
        except OSError:
            return []

        directories: list[dict] = []
        files: list[dict] = []
        for entry in entries:
            name = entry.name
            if not name.startswith(tail):
                continue
            if name.startswith(".") and not show_hidden:
                continue
            try:
                is_dir = entry.is_dir()
            except OSError:
                is_dir = False
            value = prefix + name + ("/" if is_dir else "")
            if is_dir:
                directories.append({"value": value, "description": "dir"})
            else:
                try:
                    size = entry.stat().st_size
                    description = f"{size:,} bytes"
                except OSError:
                    description = "file"
                files.append({"value": value, "description": description})

        def sort_key(item: dict) -> tuple[str, str]:
            return (item["value"].rsplit("/", 1)[-1].lower(), item["value"])

        directories.sort(key=sort_key)
        files.sort(key=sort_key)
        return (directories + files)[:PATH_COMPLETION_LIMIT]

    def _wiki_json(self, word: str) -> None:
        if not word:
            self._send_error_json("Usage: /wiki <word>", 400)
            return
        url = chat.WIKI_SUMMARY_URL.format(word=quote(word, safe=""))
        try:
            response = requests.get(
                url, headers=chat.HTTP_HEADERS, timeout=(10, 30)
            )
        except requests.RequestException as exc:
            self._send_error_json(f"Wikipedia lookup failed: {exc}", 502)
            return
        if response.status_code == 404:
            self._send_error_json(f"Wikipedia: no page found for {word!r}.", 404)
            return
        try:
            response.raise_for_status()
            data = response.json()
        except (requests.RequestException, ValueError) as exc:
            self._send_error_json(f"Wikipedia lookup failed: {exc}", 502)
            return
        if not isinstance(data, dict):
            self._send_error_json("Wikipedia returned an unexpected response.", 502)
            return
        page = ((data.get("content_urls") or {}).get("desktop") or {}).get(
            "page"
        )
        self._send_json(
            {
                "word": word,
                "title": data.get("title") or word,
                "extract": data.get("extract") or data.get("description") or "",
                "source": page or "",
            }
        )

    def _transcript_search_json(self, keyword: str) -> None:
        if not keyword:
            self._send_error_json("Usage: /transcript <keyword>", 400)
            return
        matches = chat.search_transcript_titles(keyword)
        available = set(chat.available_video_ids())
        self._send_json(
            {
                "items": [
                    {
                        "video_id": match.video_id,
                        "title": match.title,
                        "has_trunks": match.video_id in available,
                    }
                    for match in matches
                ]
            }
        )

    # ---- POST ---------------------------------------------------------
    def do_POST(self) -> None:  # noqa: N802 - BaseHTTPRequestHandler API
        parsed = urlparse(self.path)
        if parsed.path == "/api/ask":
            payload = self._read_json()
            if payload is None:
                return
            question = str(payload.get("question", "")).strip()
            if not question:
                self._send_error_json("Missing question", 400)
                return
            self._handle_ask(question)
            return
        if parsed.path == "/api/dict":
            payload = self._read_json()
            if payload is None:
                return
            word = str(payload.get("word", "")).strip()
            answer = payload.get("answer")
            self._handle_dict(word, answer if isinstance(answer, str) else None)
            return
        if parsed.path == "/api/translate":
            payload = self._read_json()
            if payload is None:
                return
            text = payload.get("text")
            if not isinstance(text, str) or not text.strip():
                self._send_error_json("Missing text to translate", 400)
                return
            self._handle_translate(text)
            return
        if parsed.path == "/api/explain_line":
            payload = self._read_json()
            if payload is None:
                return
            self._handle_explain_line(payload)
            return
        if parsed.path == "/api/snippet_question":
            payload = self._read_json()
            if payload is None:
                return
            self._handle_snippet_question(payload)
            return
        self._send_error_json("Not found", 404)

    # ---- SSE: ask / commands -----------------------------------------
    def _handle_ask(self, question: str) -> None:
        state = self.state
        with state.lock:
            state.append_history(question)
            self._begin_sse()
            try:
                self._sse("meta", state.snapshot())
                if question.startswith("/"):
                    self._run_command(question)
                else:
                    self._run_question(question)
                self._sse(
                    "done",
                    {
                        "state": state.snapshot(),
                        "benchmark": (
                            chat.format_benchmark_line(state.latest_benchmark)
                            if state.latest_benchmark is not None
                            else ""
                        ),
                    },
                )
            except ClientDisconnected:
                pass
            except Exception as exc:  # Keep the browser informed on failure.
                try:
                    self._sse("error", {"message": str(exc)})
                    self._sse("done", {"state": state.snapshot()})
                except ClientDisconnected:
                    pass

    def _run_question(self, question: str) -> None:
        state = self.state
        article_mode = state.article_path is not None
        answer_language: str | None = None
        retrieval_question = question
        if chat.contains_simplified_chinese(question):
            answer_language = "Simplified Chinese"
            self._sse_text(
                "log",
                "[Simplified Chinese detected; translating the question "
                "into English for BM25 retrieval and answering directly "
                "in Simplified Chinese]",
            )
            try:
                retrieval_question = chat.translate_to_english(question)
            except RuntimeError:
                # chat.answer() retries the translation and reports its own
                # warning if this pre-translation failed.
                retrieval_question = question

        context_sensitive = question_needs_context(
            retrieval_question, state.conversation
        )
        if context_sensitive:
            resolved_question = state.resolve_question(retrieval_question)
            if resolved_question != retrieval_question:
                self._sse_text(
                    "log",
                    "[conversation: interpreting the follow-up as: "
                    f"{resolved_question}]",
                )
                retrieval_question = resolved_question

        article_route: ArticleRoute | None = None
        article_prepare_selected: Callable | None = None
        article_prepare_stats: dict = {}
        article_attempt_depth: int | None = state.depth
        article_attempt_budget = state.max_context_tokens
        article_use_all_chunks = False
        article_prompt_prefix: str | None = None
        article_cache_variant = ""
        article_overview_mode = False
        if article_mode:
            article_route = article_question_route(
                retrieval_question,
                state.article_structure,
                iterative_default=state.article_iterative,
            )
            route_label = article_route.label
            if article_route.section is not None:
                route_label += f" {article_route.section}"
            if article_route.iterative:
                route_label += " + iterative"
            self._sse_text(
                "log",
                f"[article router: {route_label} ({article_route.source})]",
            )

        paragraph_chunks = (
            [chunk for chunk in state.chunks if chunk.index > 0]
            if article_mode
            else state.chunks
        )
        answer_chunks = (
            paragraph_chunks if article_mode else state.chunks
        )
        if article_route is not None:
            if article_route.label == "section" and article_route.section is not None:
                section = (state.article_structure or {}).get(
                    "_section_lookup", {}
                ).get(article_route.section)
                if section is None:
                    article_route = ArticleRoute(
                        "narrow", None, False, "section-missing"
                    )
                else:
                    answer_chunks = [
                        chunk
                        for chunk in paragraph_chunks
                        if int(section["start"]) <= chunk.index <= int(section["end"])
                    ]
                    if not answer_chunks:
                        answer_chunks = paragraph_chunks
                    article_attempt_depth = ARTICLE_SECTION_DEPTH
                    article_attempt_budget = ARTICLE_SECTION_CONTEXT_TOKENS
                    article_cache_variant = (
                        f"article-section:{article_route.section}"
                    )
                    article_prompt_prefix = (
                        "### ARTICLE SECTION\n"
                        f"The context is focused on the article section "
                        f"'{section.get('heading', '')}'. It includes the "
                        "section summary and neighboring paragraphs. Prefer "
                        "the specific paragraphs for facts and use the "
                        "section summary only for orientation. Ground every "
                        "claim in this context; if a detail is absent, omit "
                        "it rather than refusing.\n\n"
                    )
                    article_prepare_selected, article_prepare_stats = (
                        _make_article_expansion(
                            state.article_structure,
                            state.chunks,
                            ARTICLE_SECTION_CONTEXT_TOKENS,
                            lambda text: self._sse_text("log", text),
                        )
                    )
            elif article_route.iterative:
                evidence = _article_iterative_evidence(
                    retrieval_question,
                    state.chunks,
                    state.article_structure,
                    ARTICLE_ITERATIVE_EVIDENCE_TOKENS,
                    lambda text: self._sse_text("log", text),
                )
                if evidence:
                    answer_chunks = [chunk for chunk, _score in evidence]
                    article_use_all_chunks = True
                    article_attempt_depth = state.depth
                    article_attempt_budget = ARTICLE_ITERATIVE_EVIDENCE_TOKENS
                    article_cache_variant = "article-iterative"
                    article_prompt_prefix = (
                        "### ARTICLE EVIDENCE\n"
                        "The context below was gathered in several retrieval "
                        "rounds across the whole article, in document order; "
                        "it includes section summaries. Answer the complete "
                        "question, including enumerations and comparisons, "
                        "using every relevant part of this evidence. Do not "
                        "say that the context is insufficient; if one detail "
                        "is genuinely absent, answer the supported parts and "
                        "omit the unsupported detail.\n\n"
                    )
                else:
                    article_route = ArticleRoute(
                        "narrow", None, False, "iterative-empty"
                    )
                    self._sse_text(
                        "log",
                        "[article iterative: no new evidence found; using the "
                        "normal narrow retrieval path]",
                    )
            if article_route.label == "narrow" and not article_route.iterative:
                article_attempt_depth = state.depth
                article_attempt_budget = ARTICLE_NARROW_CONTEXT_TOKENS
                article_cache_variant = "article-narrow"
                article_prompt_prefix = (
                    "### ARTICLE CONTEXT\n"
                    "The context was retrieved from one article and includes "
                    "parent-section summaries and neighboring paragraphs. "
                    "Use the specific paragraphs for facts and the section "
                    "summary only for orientation. Ground every claim in the "
                    "context; if a specific detail is absent, omit it rather "
                    "than refusing.\n\n"
                )
                article_prepare_selected, article_prepare_stats = (
                    _make_article_expansion(
                        state.article_structure,
                        state.chunks,
                        ARTICLE_NARROW_CONTEXT_TOKENS,
                        lambda text: self._sse_text("log", text),
                    )
                )

        article_overview_mode = bool(
            article_route is not None and article_route.label == "overview"
        )
        article_overview_prefix: str | None = None
        article_overview_context_ready = False
        if article_overview_mode:
            digest_chunks = [
                chunk
                for chunk in state.chunks
                if chunk.index == ARTICLE_DIGEST_INDEX
            ]
            overview_chunks = [
                chunk
                for chunk in state.chunks
                if chunk.index == ARTICLE_OVERVIEW_INDEX
            ]
            requested_words = (
                article_requested_word_count(question)
                or article_requested_word_count(retrieval_question)
            )
            detailed = bool(
                ARTICLE_DETAILED_OVERVIEW_RE.search(question)
                or ARTICLE_DETAILED_OVERVIEW_RE.search(retrieval_question)
            )
            if digest_chunks and (requested_words or detailed):
                # The digest contains the title, outline, TL;DR, and
                # representative content from each major section. Use it as
                # the sole candidate so BM25 cannot replace it with unrelated
                # paragraphs that merely share generic words such as
                # "what"/"this"/"about".
                answer_chunks = digest_chunks[:1]
                context_kind = "article digest"
                context_detail = (
                    "its title, outline, TL;DR/summary, and representative "
                    "content from each major section"
                )
            elif overview_chunks:
                # Concise overview questions only need the compact overview;
                # the digest is reserved for long-form/detailed requests.
                answer_chunks = overview_chunks[:1]
                context_kind = "document overview"
                context_detail = "its title, outline, and TL;DR/summary"
            else:
                context_kind = ""
                context_detail = ""

            if context_kind:
                article_overview_context_ready = True
                if requested_words:
                    length_instruction = (
                        "Target length: approximately "
                        f"{requested_words:,} words. Write a detailed, "
                        "well-structured answer with headings where they "
                        "help. Develop the points supported by the context "
                        "instead of padding with unsupported facts. "
                    )
                elif detailed:
                    length_instruction = (
                        "Write a detailed, well-structured answer with "
                        "headings where they help. "
                    )
                else:
                    length_instruction = (
                        "Answer the user's question with a concise, "
                        "well-organized summary. "
                    )
                article_overview_prefix = (
                    "### ARTICLE OVERVIEW\n"
                    f"The context below is the {context_kind} of the entire "
                    f"article: {context_detail}. "
                    + length_instruction
                    + "Ground every claim in this context. If a specific "
                    "detail is absent, omit it rather than refusing; never "
                    "say that the context is insufficient.\n\n"
                )
                self._sse_text(
                    "log",
                    f"[article: answering from the {context_kind} "
                    "(title, outline, summary, and representative "
                    "sections)]",
                )

        golang_package_docs = (
            not article_mode
            and (state.tag or "").lower() == GOLANG_TAG
            and golang_package_docs_request(retrieval_question)
        )
        rust_crate_docs = (
            not article_mode
            and (state.tag or "").lower() == RUST_TAG
            and rust_crate_docs_request(retrieval_question)
        )
        rust_code_docs = (
            not article_mode
            and (state.tag or "").lower() == RUST_TAG
            and rust_code_docs_request(retrieval_question)
        )
        php_symbol_docs = (
            not article_mode
            and (state.tag or "").lower() == PHP_TAG
            and php_symbol_docs_request(retrieval_question)
        )
        documentation_code = (
            not article_mode
            and documentation_code_request(state.tag, retrieval_question)
        )
        coding_mode = (
            question_requests_coding(retrieval_question)
            or golang_package_docs
            or rust_crate_docs
            or rust_code_docs
            or php_symbol_docs
            or documentation_code
        )
        code_language = tag_code_language(state.tag)
        # Documentation-backed tags answer with the dedicated programming
        # model for both chat and code generation.
        coding_model = tag_coding_model(state.tag)
        chat_model = tag_chat_model(state.tag)
        if golang_package_docs:
            self._sse_text(
                "log",
                "[golang: code-sample question references or implies a Go "
                "package; pkg.go.dev package documentation will be searched]",
            )
        elif documentation_code and code_language == "Go":
            self._sse_text(
                "log",
                "[golang: code-sample question detected; searching the "
                "official Go documentation and answering in Go]",
            )
        if rust_crate_docs:
            self._sse_text(
                "log",
                "[rust: code-sample question references a Rust crate; "
                "docs.rs crate documentation will be searched]",
            )
        elif rust_code_docs:
            self._sse_text(
                "log",
                "[rust: code-sample question detected; searching the official "
                "Rust documentation instead of GitHub]",
            )
        if php_symbol_docs:
            self._sse_text(
                "log",
                "[php: code-sample question references a PHP function, "
                "class, or extension; the dedicated manual page will be "
                "searched]",
            )
        elif documentation_code and code_language == "PHP":
            self._sse_text(
                "log",
                "[php: code-sample question detected; searching the "
                "official PHP manual and answering in PHP]",
            )
        if coding_mode:
            mode_label = (
                f"{code_language} coding mode"
                if code_language
                else "coding mode"
            )
            self._sse(
                "mode",
                {
                    "mode": "coding",
                    "model": coding_model,
                    "think": False,
                    "language": code_language or "",
                    "label": mode_label,
                },
            )
            language_note = (
                f"; all generated code will be {code_language} because the "
                f"active tag is {state.tag!r}"
                if code_language
                else ""
            )
            self._sse_text(
                "log",
                "[coding mode: code/architecture question detected; using "
                f"{coding_model} without thinking{language_note}]",
            )

        def attempt(
            tag: str,
            chunks: list[chat.Chunk],
            scope_label: str,
            report_errors: bool = True,
            prompt_prefix_override: str | None = None,
            cache_variant: str = "",
            use_all_chunks: bool = False,
            depth_override: int | None = None,
            max_context_tokens_override: int | None = None,
            prepare_selected: Callable | None = None,
            prepare_stats: dict | None = None,
        ):
            """Answer once from a single tag; no union fallback inside this."""
            attempt_stats: dict = {}
            # Every answer produced under a documentation-backed tag must use
            # that tag's language for any code it contains, not only answers
            # classified as coding questions. A normal explanation may still
            # include a snippet; without this directive the model could emit
            # Python while the active tag is "rust", "golang", or "php".
            prompt_prefix = (
                prompt_prefix_override
                if prompt_prefix_override is not None
                else (
                    coding_prompt_prefix(tag)
                    if coding_mode
                    else tag_coding_directive(tag)
                )
            )
            emit_delta = lambda text: self._sse("delta", {"text": text})
            docs_backed = tag_uses_official_docs(tag)
            answer_filter = InsufficientContextFilter(enabled=docs_backed)
            fence_filter = CodeFenceRepairFilter(enabled=docs_backed)

            def emit_answer_delta(text: str) -> None:
                cleaned = answer_filter.feed(text)
                if not cleaned:
                    return
                cleaned = fence_filter.feed(cleaned)
                if cleaned:
                    emit_delta(cleaned)

            try:
                response, sources = chat.answer(
                    retrieval_question,
                    chunks,
                    depth=(
                        depth_override
                        if depth_override is not None
                        else state.depth
                    ),
                    max_context_tokens=(
                        max_context_tokens_override
                        if max_context_tokens_override is not None
                        else state.max_context_tokens
                    ),
                    answer_language=answer_language,
                    cache=state.cache,
                    cache_key=state.cache_key(
                        retrieval_question,
                        answer_language,
                        conversation_revision=(
                            state.conversation_revision
                            if context_sensitive
                            else None
                        ),
                        tag=tag,
                        coding_mode=coding_mode,
                        prompt_variant=cache_variant,
                    ),
                    stats=attempt_stats,
                    progressive=state.progressive,
                    prompt_prefix=prompt_prefix,
                    model=tag_coding_model(tag) if coding_mode else tag_chat_model(tag),
                    think=False,
                    # Fallback is handled below one tag at a time so that a
                    # successful answer also switches the active tag.
                    cross_tag_fallback=False,
                    scope_label=scope_label,
                    use_all_chunks=use_all_chunks,
                    prepare_selected=prepare_selected,
                    emit=emit_answer_delta,
                    log=lambda text: self._sse_text("log", text),
                )
            except RuntimeError as exc:
                if report_errors:
                    self._sse("error", {"message": str(exc)})
                return None, [], attempt_stats, str(exc)
            except Exception as exc:  # Keep unexpected failures visible.
                message = f"{type(exc).__name__}: {exc}"
                if report_errors:
                    self._sse("error", {"message": message})
                return None, [], attempt_stats, message
            tail = answer_filter.flush()
            if tail:
                tail = fence_filter.feed(tail)
                if tail:
                    emit_delta(tail)
            fence_tail = fence_filter.flush()
            if fence_tail:
                emit_delta(fence_tail)
            if docs_backed and response is not None:
                # Keep the returned text in sync with what was streamed: the
                # refusal is already suppressed and malformed continuation
                # fences are already repaired, so neither can reappear in the
                # saved answer, annotations, or a documentation merge.
                response = repair_code_fences(
                    strip_leading_insufficient_context(response)
                )
            if prepare_stats is not None and prepare_stats.get("added"):
                attempt_stats["article_expansion"] = dict(prepare_stats)
            return response, sources or [], attempt_stats, None

        original_tag = state.tag
        original_scope = (
            f"article {state.article_path!r}"
            if article_mode
            else (
                f"tag {original_tag!r}"
                if state.transcript is None
                else f"transcript {state.transcript!r}"
            )
        )
        documentation_tag = (
            False if article_mode else tag_uses_official_docs(original_tag)
        )
        local_model = coding_model if coding_mode else chat_model
        documentation_first_done = False
        first_response: str | None = None
        first_sources: list[tuple[chat.Chunk, float]] = []
        first_stats: dict = {}
        response: str | None = None
        sources: list[tuple[chat.Chunk, float]] = []
        stats: dict = {}

        # Documentation-backed tags ("golang", "rust", and "php") must
        # consult the official documentation before the general local model
        # generates any code. The docs answer is streamed first; only then is
        # the local model called, with that answer inserted in its prompt as
        # the authoritative reference. This prevents a code sample being
        # invented before the corresponding documentation has been retrieved.
        if documentation_tag and coding_mode:
            documentation_first_done = True
            descriptor = documentation_descriptor(original_tag)
            assert descriptor is not None
            self._sse_answer_source(
                tag=original_tag,
                source=SOURCE_DOCUMENTATION,
                label=(
                    f"official docs {descriptor['source_label']} via "
                    f"{coding_model}"
                ),
                model=coding_model,
                url=descriptor["source_url"],
                detail=(
                    "the official documentation is retrieved before any "
                    "local code generation"
                ),
            )
            self._sse_text(
                "log",
                f"[docs-first: consulting {descriptor['source_label']} before "
                f"{coding_model} generates code]",
            )
            doc_answer, doc_error = descriptor["runner"](
                retrieval_question,
                log=lambda text: self._sse_text("log", text),
                emit=lambda text: self._sse_text("delta", text),
            )

            doc_incomplete = bool(doc_answer) and (
                coding_mode
                and documentation_answer_is_insufficient(doc_answer)
            )
            if doc_answer and doc_incomplete:
                # The excerpts were too thin for the documentation assistant,
                # which streamed a refusal. A code request must not end there:
                # clear the refusal and let the local coding model write the
                # program from the limited excerpts plus its own knowledge.
                self._sse_text(
                    "log",
                    f"[docs-first: the {descriptor['source_label']} excerpts "
                    "were incomplete; asking the local coding model to "
                    "generate the code from the limited excerpts instead of "
                    "refusing]",
                )
                self._sse_answer_source(
                    tag=original_tag,
                    source=SOURCE_OLLAMA,
                    label=f"local LLM {coding_model} (code rescue)",
                    model=coding_model,
                    detail=(
                        "the official documentation excerpts were incomplete; "
                        "the local model is generating the requested code "
                        "from those excerpts and its own programming knowledge"
                    ),
                )
                self._sse(
                    "reset",
                    {"reason": "documentation answer was incomplete"},
                )
                rescue_response, rescue_metrics = stream_code_rescue(
                    retrieval_question,
                    original_tag,
                    doc_answer,
                    descriptor,
                    emit=lambda text: self._sse("delta", {"text": text}),
                )
                if rescue_response and rescue_response.strip():
                    response = rescue_response + "\n"
                    sources = []
                    stats = dict(rescue_metrics)
                    stats["documentation_first"] = {
                        "used": True,
                        "source": descriptor["source_url"],
                        "doc_chars": len(doc_answer),
                        "doc_insufficient": True,
                        "local_code_rescue": True,
                        "error": doc_error,
                    }
                    first_response = response
                    first_sources = []
                    first_stats = stats
                else:
                    # Extremely unlikely: the rescue model returned nothing.
                    # Restore the documentation answer rather than leaving the
                    # screen empty.
                    self._sse_text(
                        "log",
                        "[docs-first: the local code rescue returned no text; "
                        "keeping the documentation answer]",
                    )
                    response = doc_answer + "\n"
                    self._sse_text("delta", response)
                    sources = []
                    stats = {
                        "documentation_first": {
                            "used": True,
                            "source": descriptor["source_url"],
                            "doc_chars": len(doc_answer),
                            "doc_insufficient": True,
                            "local_code_rescue": False,
                            "error": doc_error,
                        }
                    }
                    first_response = response
                    first_sources = []
                    first_stats = stats
            elif doc_answer:
                # The official documentation answer is streamed as the final
                # answer. The local model is not asked for a second
                # "Complementary notes from the local transcript context"
                # section: the documentation already contains the code and
                # explanation, and the extra section only duplicates it.
                stats = {
                    "documentation_first": {
                        "used": True,
                        "source": descriptor["source_url"],
                        "doc_chars": len(doc_answer),
                        "streamed_before_local": True,
                        "complementary_notes_removed": True,
                        "error": doc_error,
                    }
                }
                response = doc_answer + "\n"
                sources = []
                self._sse_answer_source(
                    tag=original_tag,
                    source=SOURCE_DOCUMENTATION,
                    label=(
                        f"official docs {descriptor['source_label']} via "
                        f"{coding_model}"
                    ),
                    model=coding_model,
                    url=descriptor["source_url"],
                    detail=(
                        "the answer comes from the official documentation; "
                        "no additional local complement is appended"
                    ),
                )
                first_response = response
                first_sources = []
                first_stats = stats
            else:
                self._sse_text(
                    "log",
                    f"[docs-first: documentation unavailable: {doc_error}; "
                    "falling back to the local coding model]",
                )
                self._sse_answer_source(
                    tag=original_tag,
                    source=SOURCE_OLLAMA,
                    label=f"local LLM {local_model}",
                    model=local_model,
                    detail=(
                        "the official documentation was unavailable; the "
                        "local model is answering as a fallback"
                    ),
                )
                (
                    first_response,
                    first_sources,
                    first_stats,
                    error,
                ) = attempt(
                    original_tag,
                    answer_chunks,
                    original_scope,
                    prompt_prefix_override=(
                        article_overview_prefix
                        if article_overview_prefix is not None
                        else article_prompt_prefix
                    ),
                    cache_variant=article_cache_variant,
                    use_all_chunks=(
                        article_overview_context_ready
                        or article_use_all_chunks
                    ),
                    depth_override=article_attempt_depth,
                    max_context_tokens_override=article_attempt_budget,
                    prepare_selected=article_prepare_selected,
                    prepare_stats=article_prepare_stats,
                )
                if error:
                    return
                response, sources, stats = (
                    first_response,
                    first_sources,
                    first_stats,
                )

        if not documentation_first_done:
            if documentation_tag:
                self._sse_answer_source(
                    tag=original_tag,
                    source=SOURCE_OLLAMA,
                    label=f"local LLM {local_model}",
                    model=local_model,
                    detail=(
                        "the answer is being generated by the local Ollama "
                        "model from the transcript knowledge base"
                    ),
                )
            first_response, first_sources, first_stats, error = attempt(
                original_tag,
                answer_chunks,
                original_scope,
                prompt_prefix_override=(
                    article_overview_prefix
                    if article_overview_prefix is not None
                    else article_prompt_prefix
                ),
                cache_variant=article_cache_variant,
                use_all_chunks=(
                    article_overview_context_ready or article_use_all_chunks
                ),
                depth_override=article_attempt_depth,
                max_context_tokens_override=article_attempt_budget,
                prepare_selected=article_prepare_selected,
                prepare_stats=article_prepare_stats,
            )
            if error:
                return
            response, sources, stats = (
                first_response,
                first_sources,
                first_stats,
            )

        switched = False

        # For non-coding questions on the documentation-backed tags, the
        # local knowledge base still answers first and the official docs are
        # consulted when it cannot. Coding questions already took the
        # documentation-first path above, so they are never retried here.
        # The "golang", "rust", and "php" tags never switch to another tag
        # and never call the GitHub API; their code answers come from
        # ./golang_doc_search.py, ./rust_doc_search.py, or
        # ./php_doc_search.py.
        documentation_backed_tag = documentation_tag
        local_answer_insufficient = answer_is_insufficient(first_response)
        documentation = None
        if (
            not documentation_first_done
            and documentation_backed_tag
            and (coding_mode or local_answer_insufficient)
        ):
            documentation = documentation_descriptor(original_tag)

        if documentation is not None:
            if not coding_mode:
                self._sse_text(
                    "log",
                    f"[tag {original_tag!r} could not answer; consulting "
                    f"{documentation['source_label']} with "
                    f"{coding_model} instead of switching tags]",
                )
            response, sources, stats = attach_documentation_answer(
                first_response,
                first_sources,
                first_stats,
                question=retrieval_question,
                emit_delta=lambda text: self._sse_text("delta", text),
                emit_reset=lambda reason: self._sse(
                    "reset", {"reason": reason}
                ),
                emit_log=lambda text: self._sse_text("log", text),
                emit_source=self._sse_doc_source,
                **documentation,
            )
        # For every other tag, an unusable coding answer falls back to
        # GitHub's public API and an answer built from code snippets. Go,
        # Rust, and PHP never take this path: their code samples come from
        # the official documentation above.
        elif (
            coding_mode
            and not article_mode
            and github_code_fallback_allowed(original_tag)
            and answer_is_insufficient(first_response)
        ):
            self._sse_text(
                "log",
                "[coding mode: no usable answer; searching GitHub for "
                "related code snippets]",
            )
            snippets = github_code_snippets(
                retrieval_question,
                limit=GITHUB_SNIPPET_LIMIT,
                log=lambda text: self._sse_text("log", text),
                tag=original_tag,
            )
            if snippets:
                if first_response and first_response.strip():
                    self._sse("reset", {"reason": "github code fallback"})
                response = format_github_code_answer(snippets)
                self._sse_text("delta", response)
                sources = []
                stats = dict(first_stats)
                stats["github_fallback"] = {
                    "snippets": len(snippets),
                    "sources": [
                        {
                            "repo": snippet.get("repo"),
                            "path": snippet.get("path"),
                            "url": snippet.get("url"),
                        }
                        for snippet in snippets
                    ],
                }
                self._sse_text(
                    "log",
                    f"[github fallback: using {len(snippets)} code "
                    "snippet(s)]",
                )
            else:
                self._sse_text(
                    "log",
                    "[github fallback: no related code snippets found]",
                )

        if (
            chat.CROSS_TAG_FALLBACK_ENABLED
            and not coding_mode
            and not documentation_backed_tag
            and not article_mode
            and state.transcript is None
            and local_answer_insufficient
        ):
            candidates = state.fallback_tag_order(retrieval_question)
            attempted: list[str] = []
            visible = bool(first_response and first_response.strip())

            for number, tag in enumerate(candidates, start=1):
                self._sse_text(
                    "log",
                    f"[tag {original_tag!r} could not answer; trying "
                    f"tag {tag!r} ({number}/{len(candidates)})]",
                )
                if visible:
                    # The previous refusal is already on screen. Clear it so
                    # the next tag's stream does not append to it.
                    self._sse("reset", {"reason": f"retrying in tag {tag!r}"})
                    visible = False

                new_chunks, _printed, load_error = load_chunks_captured(tag=tag)
                attempted.append(tag)
                if load_error or new_chunks is None:
                    self._sse_text(
                        "log", f"[cannot load tag {tag!r}: {load_error}]"
                    )
                    continue

                (
                    candidate_response,
                    candidate_sources,
                    candidate_stats,
                    error,
                ) = attempt(
                    tag, new_chunks, f"tag {tag!r}", report_errors=False
                )
                if error:
                    self._sse_text(
                        "log", f"[tag {tag!r} could not answer: {error}]"
                    )
                    continue
                if candidate_response and candidate_response.strip():
                    visible = True

                if not answer_is_insufficient(candidate_response):
                    state.adopt_tag(tag, new_chunks)
                    response = candidate_response
                    sources = candidate_sources
                    stats = candidate_stats
                    stats["tag_fallback"] = {
                        "from": original_tag,
                        "to": tag,
                        "attempts": attempted,
                    }
                    switched = True
                    self._sse_text(
                        "log",
                        f"[answer found in tag {tag!r}; switched from "
                        f"{original_tag!r}]",
                    )
                    break

                self._sse_text(
                    "log", f"[tag {tag!r} could not answer either]"
                )

            if not switched:
                # Restore the original tag's answer so the visible text and
                # the active tag/state stay consistent.
                if visible:
                    self._sse("reset", {"reason": "restoring original answer"})
                self._sse_text(
                    "log",
                    f"[no other tag could answer; keeping tag "
                    f"{original_tag!r}]",
                )
                if first_response and first_response.strip():
                    self._sse_text("delta", first_response)
                else:
                    tried = ", ".join([original_tag, *attempted])
                    self._sse_text(
                        "delta",
                        "No tagged knowledge could answer this question "
                        f"(tried: {tried}).",
                    )
                response, sources, stats = (
                    first_response,
                    first_sources,
                    first_stats,
                )

        if response is None:
            return
        if documentation_tag and response is not None:
            # Final safety net: whatever path produced the answer (local,
            # documentation merge, or code rescue), never persist or annotate
            # a leading "provided context is insufficient" refusal.
            response = strip_leading_insufficient_context(response)
        state.latest_answer = response
        state.latest_sources = sources
        stats["coding_mode"] = coding_mode
        stats["model"] = coding_model if coding_mode else chat_model
        state.latest_benchmark = stats
        annotations = build_answer_annotations(response, sources)
        if article_mode:
            # Article paragraphs have no YouTube source. Keep the paragraph
            # mapping for the UI but suppress the video icon/link and add the
            # nearest cached section heading as a precise citation.
            for item in annotations:
                item["video_id"] = ""
                item["url"] = ""
                title = state.article_title or "article"
                section = _article_source_section(
                    state.article_structure, int(item.get("chunk_index", -1))
                )
                heading = str(section.get("heading", "")) if section else ""
                item["title"] = (
                    f"{title} — {heading}" if heading else title
                )
        self._sse("annotations", {"items": annotations})
        state.record_turn(question, retrieval_question, response)
        if switched:
            self._sse("meta", state.snapshot())
        if state.benchmark_mode:
            self._sse_text("log", chat.format_benchmark_line(stats))

    def _run_command(self, text: str) -> None:
        name, _, argument = text.partition(" ")
        name = name.lower()
        argument = argument.strip()

        if name in {"/exit", "/quit", "/q"}:
            self._sse_text("output", "\nBye. Close the browser tab to exit.\n")
        elif name == "/help":
            self._command_help()
        elif name == "/memory":
            report = chat.index_memory_report(
                self.state.chunks, article=self.state.article_path
            )
            self._sse_text("output", "\n" + report + "\n")
        elif name == "/tag":
            self._command_tag(argument)
        elif name == "/tag_gen":
            self._command_tag_gen(argument)
        elif name == "/refresh":
            self._command_refresh()
        elif name == "/transcript":
            self._command_transcript(argument, direct=False)
        elif name == "/search_transcript":
            self._command_transcript(argument, direct=False, title_only=True)
        elif name == "/learn":
            self._command_learn(argument)
        elif name == "/learn_topic":
            self._command_learn_topic(argument)
        elif name == "/learn_playlist":
            self._command_learn_playlist(argument)
        elif name == "/ask":
            self._command_ask(argument)
        elif name == "/news":
            self._command_news(argument)
        elif name == "/wiki":
            self._command_wiki(argument)
        elif name == "/wiki_article":
            self._command_wiki_article(argument)
        elif name == "/article":
            self._command_article(argument)
        elif name == "/dict":
            self._command_dict(argument)
        elif name == "/depth":
            self._command_depth(argument)
        elif name == "/status":
            report = chat.chat_status_report(
                self.state.tag,
                self.state.transcript,
                self.state.chunks,
                self.state.depth,
                self.state.max_context_tokens,
                len(self.state.cache),
                article=self.state.article_path,
            )
            report += (
                f"  conversation: {len(self.state.conversation)} turn(s) "
                f"(kept: last {CONVERSATION_MAX_TURNS})\n"
            )
            self._sse_text("output", "\n" + report + "\n")
        elif name == "/new":
            self._command_new()
        elif name == "/sources":
            self._command_sources()
        elif name == "/benchmark":
            self._command_benchmark(argument)
        else:
            self._sse_text(
                "output",
                f"\nUnknown command: {name}. Use /help to see the available "
                "commands.\n",
            )

    def _command_new(self) -> None:
        self.state.reset_conversation()
        self._sse_text(
            "output",
            "\nStarted a new conversation (follow-up context cleared; "
            "knowledge scope unchanged).\n",
        )
        self._sse("meta", self.state.snapshot())

    def _command_help(self) -> None:
        lines = ["", "Commands:"]
        for name, description in sorted(WEB_COMMANDS.items()):
            lines.append(f"  {name:<20} {description}")
        lines.extend(
            [
                "",
                "If the current tag cannot answer, other tags are tried",
                "automatically and the chat switches to the one that can.",
                "",
                "Keyboard:",
                "  Esc          normal mode        i / a / A     insert",
                "  h / l / 0 / $                 move left / right / start / end",
                "  w / b                        move by word",
                "  x / dw / dd / D              delete char / word / line / to end",
                "  v / V                        visual / visual-line mode",
                "  u                            undo",
                "  Tab                          complete command, tag, or transcript",
                "  Up / Down, or k / j          previous / next history entry",
                "  Click any word in an answer  open a streaming /dict popup",
                "",
            ]
        )
        self._sse_text("output", "\n".join(lines))

    def _command_tag(self, argument: str) -> None:
        state = self.state
        tags = chat.available_tags()
        if not argument:
            self._sse_text(
                "output",
                "\nAvailable tags: "
                + (", ".join(tags) if tags else "none")
                + "\nUsage: /tag <tag> (Tab completes tag names)\n",
            )
            return
        if argument not in tags:
            self._sse_text(
                "output",
                f"\nUnknown tag: {argument!r}. Choose one of: "
                + ", ".join(tags)
                + "\n",
            )
            return

        new_chunks, printed, error = load_chunks_captured(tag=argument)
        if error or new_chunks is None:
            self._sse_text(
                "output", f"\nCannot switch to tag {argument!r}: {error}\n"
            )
            return
        state.replace_chunks(new_chunks)
        state.tag = argument
        state.transcript = None
        state.cache.clear()
        self._sse_text(
            "output",
            "\n" + (printed + "\n" if printed else "")
            + chat.index_memory_report(new_chunks, build_index=False)
            + f"\nNow using tag {argument!r}.\n",
        )
        self._sse("meta", state.snapshot())

    def _command_refresh(self) -> None:
        state = self.state
        if state.article_path is not None:
            self._refresh_article(state.article_path)
            return
        new_chunks, printed, error = load_chunks_captured(tag=state.tag)
        if error or new_chunks is None:
            self._sse_text(
                "output",
                f"\nCannot refresh tag {state.tag!r}: {error}\n",
            )
            return
        state.replace_chunks(new_chunks)
        state.transcript = None
        state.cache.clear()
        self._sse_text(
            "output",
            "\n" + (printed + "\n" if printed else "")
            + chat.index_memory_report(new_chunks, build_index=False)
            + f"\nNow using tag {state.tag!r} (trunks refreshed).\n",
        )
        self._sse("meta", state.snapshot())

    def _command_transcript(
        self, argument: str, direct: bool = True, title_only: bool = False
    ) -> None:
        state = self.state
        available = set(chat.available_video_ids())
        if not available:
            self._sse_text(
                "output",
                f"\nNo transcripts found in {chat.OUTPUT_DIR}. Use /learn first.\n",
            )
            return
        if not argument:
            self._sse_text(
                "output",
                "\nUsage: /transcript <hash|URL|keyword> "
                "(Tab completes ids and titles)\n",
            )
            return

        video_id = None if title_only else chat.extract_transcript_video_id(argument)
        if video_id is not None:
            if video_id not in available:
                self._sse_text(
                    "output",
                    f"\nNo trunk files for transcript {video_id!r}. "
                    "Use /learn first.\n",
                )
                return
            self._activate_transcript(video_id)
            return

        matches = chat.search_transcript_titles(argument)
        usable = [match for match in matches if match.video_id in available]
        if not usable:
            if matches:
                self._sse_text(
                    "output",
                    f"\nFound {len(matches)} title match(es) for "
                    f"{argument!r}, but none have trunks. Use /learn first.\n",
                )
            else:
                self._sse_text(
                    "output", f"\nNo transcript titles matched {argument!r}.\n"
                )
            return
        if len(usable) == 1:
            self._activate_transcript(usable[0].video_id)
            return

        lines = [f"\nFound {len(usable)} transcript(s) for {argument!r}:"]
        for index, match in enumerate(usable, start=1):
            lines.append(f"  {index:>4}. {match.video_id}  {match.title}")
        lines.append(
            "Select one with /transcript <video_id> or click a result below.\n"
        )
        self._sse_text("output", "\n".join(lines))
        self._sse(
            "choices",
            {
                "kind": "transcript",
                "items": [
                    {
                        "value": match.video_id,
                        "description": match.title,
                    }
                    for match in usable
                ],
            },
        )

    def _refresh_article(self, article_path: str) -> None:
        """Re-read the active article and rebuild its paragraph trunks."""
        state = self.state
        path = Path(article_path)
        try:
            new_chunks, title, structure = _load_article_chunks(path)
        except (OSError, ValueError) as exc:
            self._sse_text(
                "output",
                f"\nCannot refresh article {article_path!r}: {exc}\n",
            )
            return
        state.replace_chunks(
            new_chunks,
            article_path=article_path,
            article_title=title,
            article_structure=structure,
            article_iterative=state.article_iterative,
        )
        state.cache.clear()
        self._sse_text(
            "output",
            "\n"
            + chat.index_memory_report(
                new_chunks, build_index=False, article=article_path
            )
            + f"\nNow answering from article {article_path!r} "
            f"({sum(1 for chunk in new_chunks if chunk.index > 0)} "
            "paragraph(s)).\n",
        )
        self._sse("meta", state.snapshot())

    def _activate_transcript(self, video_id: str) -> None:
        state = self.state
        new_chunks, printed, error = load_chunks_captured(video_ids={video_id})
        if error or new_chunks is None:
            self._sse_text(
                "output",
                f"\nCannot switch to transcript {video_id!r}: {error}\n",
            )
            return
        state.replace_chunks(new_chunks)
        state.transcript = video_id
        state.cache.clear()
        self._sse_text(
            "output",
            "\n" + (printed + "\n" if printed else "")
            + chat.index_memory_report(new_chunks, build_index=False)
            + f"\nNow answering only from transcript {video_id!r}.\n",
        )
        self._sse("meta", state.snapshot())

    def _command_learn(self, argument: str) -> None:
        if not argument:
            self._sse_text(
                "output", "\nUsage: /learn <youtube_video_hash|url>\n"
            )
            return
        try:
            import learn
        except ImportError as exc:
            self._sse_text(
                "output", f"\n/learn is unavailable: cannot import learn.py ({exc})\n"
            )
            return
        try:
            video_id = learn.extract_video_hash(argument)
        except ValueError as exc:
            self._sse_text("output", f"\n/learn: {exc}\n")
            return

        self._sse_text("output", f"\n/learn: {video_id}\n")
        if not self._learn_video(video_id, label="/learn"):
            return
        self._finish_learn([video_id], label="/learn")

    def _command_learn_topic(self, argument: str) -> None:
        topic = argument.strip()
        if not topic:
            self._sse_text(
                "output", "\nUsage: /learn_topic <topic>\n"
            )
            return

        self._sse_text(
            "output", f"\n/learn_topic: searching YouTube for {topic!r}...\n"
        )
        try:
            result = subprocess.run(
                [
                    sys.executable,
                    "-u",
                    str(SCRIPT_DIR / "search_youtube.py"),
                    "--limit",
                    "3",
                    "--",
                    topic,
                ],
                cwd=SCRIPT_DIR,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                text=True,
                timeout=600,
            )
        except subprocess.TimeoutExpired:
            self._sse_text(
                "output", "\n/learn_topic: YouTube search timed out.\n"
            )
            return
        except OSError as exc:
            self._sse_text(
                "output", f"\n/learn_topic: could not run search_youtube.py: {exc}\n"
            )
            return

        if result.stderr.strip():
            self._sse_text(
                "output",
                result.stderr if result.stderr.endswith("\n") else result.stderr + "\n",
            )
        if result.returncode != 0:
            self._sse_text(
                "output",
                f"\n/learn_topic: search_youtube.py failed with exit code "
                f"{result.returncode}.\n",
            )
            return

        video_ids = []
        for line in result.stdout.splitlines():
            video_id = line.strip()
            if (
                re.fullmatch(r"[A-Za-z0-9_-]{11}", video_id)
                and video_id not in video_ids
            ):
                video_ids.append(video_id)
        video_ids = video_ids[:3]

        if not video_ids:
            self._sse_text(
                "output",
                "\n/learn_topic: no videos with an English transcript were "
                "found for this topic.\n",
            )
            return

        self._sse_text(
            "output",
            f"\n/learn_topic: learning {len(video_ids)} video(s): "
            f"{', '.join(video_ids)}\n",
        )
        learned = []
        for video_id in video_ids:
            self._sse_text("output", f"\n/learn_topic: {video_id}\n")
            if self._learn_video(video_id, label="/learn_topic"):
                learned.append(video_id)
            else:
                self._sse_text(
                    "output",
                    f"\n/learn_topic: could not learn {video_id}; continuing "
                    "with any remaining videos.\n",
                )

        if not learned:
            self._sse_text(
                "output", "\n/learn_topic: no videos could be learned.\n"
            )
            return
        self._finish_learn(learned, label="/learn_topic")

    def _command_learn_playlist(self, argument: str) -> None:
        """Learn every video in a YouTube playlist."""
        playlist = argument.strip()
        if not playlist:
            self._sse_text(
                "output",
                "\nUsage: /learn_playlist <youtube_playlist_id|url>\n",
            )
            return

        self._sse_text(
            "output",
            f"\n/learn_playlist: fetching videos from {playlist!r}...\n",
        )
        try:
            result = subprocess.run(
                [
                    sys.executable,
                    "-u",
                    str(SCRIPT_DIR / "get_videos_in_playlist.py"),
                    playlist,
                ],
                cwd=SCRIPT_DIR,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                text=True,
                timeout=600,
            )
        except subprocess.TimeoutExpired:
            self._sse_text(
                "output",
                "\n/learn_playlist: playlist fetch timed out.\n",
            )
            return
        except OSError as exc:
            self._sse_text(
                "output",
                "\n/learn_playlist: could not run "
                f"get_videos_in_playlist.py: {exc}\n",
            )
            return

        if result.stderr.strip():
            self._sse_text(
                "output",
                result.stderr
                if result.stderr.endswith("\n")
                else result.stderr + "\n",
            )
        if result.returncode != 0:
            self._sse_text(
                "output",
                "\n/learn_playlist: get_videos_in_playlist.py failed with "
                f"exit code {result.returncode}.\n",
            )
            return

        video_ids: list[str] = []
        for line in result.stdout.splitlines():
            video_id = line.strip()
            if (
                re.fullmatch(r"[A-Za-z0-9_-]{11}", video_id)
                and video_id not in video_ids
            ):
                video_ids.append(video_id)

        if not video_ids:
            self._sse_text(
                "output",
                "\n/learn_playlist: the playlist contains no videos.\n",
            )
            return

        total = len(video_ids)
        self._sse_text(
            "output",
            f"\n/learn_playlist: learning {total} video(s) from the "
            "playlist.\n",
        )
        learned = []
        for index, video_id in enumerate(video_ids, start=1):
            self._sse_text(
                "output",
                f"\n/learn_playlist: [{index}/{total}] {video_id}\n",
            )
            if self._learn_video(video_id, label="/learn_playlist"):
                learned.append(video_id)
            else:
                self._sse_text(
                    "output",
                    f"\n/learn_playlist: could not learn {video_id}; "
                    "continuing with any remaining videos.\n",
                )

        if not learned:
            self._sse_text(
                "output",
                "\n/learn_playlist: no videos could be learned.\n",
            )
            return
        self._finish_learn(learned, label="/learn_playlist")

    def _learn_video(self, video_id: str, label: str = "/learn") -> bool:
        """Run the learn.py pipeline for one video, streaming its output."""
        try:
            import learn
        except ImportError as exc:
            self._sse_text(
                "output",
                f"\n{label} is unavailable: cannot import learn.py ({exc})\n",
            )
            return False

        for script, script_args in learn.STEPS:
            argv = [arg.replace("<hash>", video_id) for arg in script_args]
            if not self._run_learn_step(script, argv, label=label):
                return False
        return True

    def _run_learn_step(
        self, script: str, argv: list[str], label: str = "/learn"
    ) -> bool:
        """Run one learn pipeline step, streaming its output."""
        self._sse_text(
            "output", f"\n=== {script} {' '.join(argv)} ===\n"
        )
        process = None
        try:
            process = subprocess.Popen(
                [
                    sys.executable,
                    "-u",
                    str(SCRIPT_DIR / script),
                    *argv,
                ],
                cwd=SCRIPT_DIR,
                stdout=subprocess.PIPE,
                stderr=subprocess.STDOUT,
                text=True,
                bufsize=1,
            )
            assert process.stdout is not None
            try:
                for line in process.stdout:
                    self._sse_text("output", line)
                returncode = process.wait()
            except ClientDisconnected:
                # The browser cancelled the stream; do not leave the
                # learn pipeline running in the background.
                process.terminate()
                try:
                    process.wait(timeout=5)
                except subprocess.TimeoutExpired:
                    process.kill()
                    process.wait()
                raise
        except OSError as exc:
            self._sse_text(
                "output", f"\n{label}: could not run {script}: {exc}\n"
            )
            return False
        if returncode != 0:
            self._sse_text(
                "output",
                f"\n{label} aborted: {script} failed with exit code "
                f"{returncode}\n",
            )
            return False
        return True

    def _finish_learn(self, video_ids: list[str], label: str = "/learn") -> None:
        """Invalidate tag caches and reload the active scope after learning."""
        state = self.state
        chat.invalidate_all_tagged_chunks()
        state.cache.clear()
        for video_id in video_ids:
            self._sse_text(
                "output",
                f"\n{label}: {video_id} added to the knowledge base.\n",
            )

        new_chunks = printed = error = None
        reloaded = False
        if state.article_path is not None:
            self._sse_text(
                "output",
                f"\nStill answering from article {state.article_path!r}; "
                "the loaded article context is unchanged.\n",
            )
        elif state.transcript is not None:
            if state.transcript in video_ids:
                new_chunks, printed, error = load_chunks_captured(
                    video_ids={state.transcript}
                )
                reloaded = True
            else:
                self._sse_text(
                    "output",
                    f"\nStill answering only from transcript "
                    f"{state.transcript!r}; the loaded trunks are unchanged.\n",
                )
        elif any(
            chat.video_is_tagged(video_id, state.tag) for video_id in video_ids
        ):
            new_chunks, printed, error = load_chunks_captured(tag=state.tag)
            reloaded = True
        else:
            listed = ", ".join(repr(video_id) for video_id in video_ids)
            verb = "was" if len(video_ids) == 1 else "were"
            self._sse_text(
                "output",
                f"\n{listed} {verb} not added to tag {state.tag!r}; "
                "the loaded trunks are unchanged.\n",
            )

        if reloaded:
            if error or new_chunks is None:
                self._sse_text(
                    "output",
                    f"\n{label}: could not reload the active scope: {error}\n",
                )
            else:
                state.replace_chunks(new_chunks)
                self._sse_text(
                    "output",
                    "\n" + (printed + "\n" if printed else "")
                    + chat.index_memory_report(
                        new_chunks, build_index=False
                    )
                    + "\n",
                )
        self._sse("meta", state.snapshot())

    def _command_tag_gen(self, argument: str) -> None:
        state = self.state
        tag = argument.strip()
        if not tag:
            self._sse_text(
                "output",
                "\nUsage: /tag_gen <tag_name> "
                "(creates the tag if new; Tab completes existing names)\n",
            )
            return

        tags = chat.available_tags()
        if tag in {".", ".."} or "/" in tag or "\\" in tag:
            self._sse_text(
                "output", f"\nInvalid tag name: {tag!r}.\n"
            )
            return
        if tag not in tags:
            # generate_tags.py creates missing tags itself; don't reject the
            # request just because ./tags does not contain it yet.
            self._sse_text(
                "output",
                f"\n/tag_gen: tag {tag!r} does not exist yet; it will be "
                "created by the classifier.\n",
            )

        script = "generate_tags.py"
        argv = ["--tag", tag]
        self._sse_text(
            "output",
            f"\n/tag_gen: classifying untagged videos against tag {tag!r}.\n"
            f"\n=== {script} {' '.join(argv)} ===\n",
        )
        process = None
        try:
            process = subprocess.Popen(
                [
                    sys.executable,
                    "-u",
                    str(SCRIPT_DIR / script),
                    *argv,
                ],
                cwd=SCRIPT_DIR,
                stdout=subprocess.PIPE,
                stderr=subprocess.STDOUT,
                text=True,
                bufsize=1,
            )
            assert process.stdout is not None
            try:
                for line in process.stdout:
                    self._sse_text("output", line)
                returncode = process.wait()
            except ClientDisconnected:
                # The browser cancelled the stream; stop the classifier too.
                process.terminate()
                try:
                    process.wait(timeout=5)
                except subprocess.TimeoutExpired:
                    process.kill()
                    process.wait()
                raise
        except OSError as exc:
            self._sse_text(
                "output", f"\n/tag_gen: could not run {script}: {exc}\n"
            )
            return
        if returncode != 0:
            self._sse_text(
                "output",
                f"\n/tag_gen: {script} exited with code {returncode}; "
                "reloading from whatever was written so far.\n",
            )

        # tag files can change even on a non-zero exit (for example when the
        # model returns an unclear answer), so always drop every cached
        # tagged-chunk set (including the union used by cross-tag fallback)
        # and this session's answer cache.
        chat.invalidate_all_tagged_chunks()
        state.cache.clear()
        self._sse_text(
            "output",
            f"\n/tag_gen: finished updating tag {tag!r}.\n",
        )

        if (
            state.article_path is None
            and state.transcript is None
            and state.tag == tag
        ):
            new_chunks, printed, error = load_chunks_captured(tag=tag)
            if error or new_chunks is None:
                self._sse_text(
                    "output",
                    f"\n/tag_gen: could not reload tag {tag!r}: {error}\n",
                )
            else:
                state.replace_chunks(new_chunks)
                self._sse_text(
                    "output",
                    "\n" + (printed + "\n" if printed else "")
                    + chat.index_memory_report(
                        new_chunks, build_index=False
                    )
                    + f"\nNow using tag {tag!r} (trunks refreshed).\n",
                )
        else:
            self._sse_text(
                "output",
                f"\nThe active scope was not reloaded. Use /refresh (or /tag "
                f"{tag}) to pick up the new trunks.\n",
            )
        self._sse("meta", state.snapshot())

    def _command_news(self, argument: str) -> None:
        """Stream the latest news stories to the browser.

        Usage: /news [cnn|kqed|sfbayview] [number-of-stories]
        Without a source, every supported source is shown as its own section.
        """
        argv: list[str] = []
        source = None
        for part in argument.split():
            lowered = part.lower()
            if part.isdigit():
                if int(part) < 1:
                    self._sse_text(
                        "output",
                        "\nUsage: /news [cnn|kqed|sfbayview] "
                        "[number-of-stories]\n",
                    )
                    return
                argv.extend(["--limit", part])
            elif lowered in NEWS_SOURCES:
                if source is not None:
                    self._sse_text(
                        "output",
                        "\nUsage: /news [cnn|kqed|sfbayview] "
                        "[number-of-stories]\n",
                    )
                    return
                source = lowered
            else:
                self._sse_text(
                    "output",
                    "\nUsage: /news [cnn|kqed|sfbayview] "
                    "[number-of-stories]\n",
                )
                return
        if source is not None:
            argv.extend(["--source", source])
        self._stream_news_events("news_fetch.php", "/news", argv)

    def _stream_news_events(
        self, script_name: str, command: str, extra_argv: list[str]
    ) -> None:
        """Stream a news fetcher's NDJSON events as `news` SSE events.

        news_fetch.php emits one JSON event per line; relaying each one lets
        the title list appear story by story while the feeds are still being
        downloaded.
        """
        script = SCRIPT_DIR / script_name
        if not script.exists():
            self._sse_text(
                "output", f"\n{command}: missing {script.name}.\n"
            )
            return

        argv = ["php", str(script), "--json", *extra_argv]
        log_prefix = command.lstrip("/")
        process = None
        item_count = 0
        try:
            process = subprocess.Popen(
                argv,
                cwd=SCRIPT_DIR,
                stdout=subprocess.PIPE,
                stderr=subprocess.STDOUT,
                text=True,
                bufsize=1,
            )
            assert process.stdout is not None
            try:
                for raw_line in process.stdout:
                    line = raw_line.strip()
                    if not line:
                        continue
                    try:
                        payload = json.loads(line)
                    except json.JSONDecodeError:
                        self._sse_text("log", f"[{log_prefix}: {line}]")
                        continue
                    if not isinstance(payload, dict):
                        continue
                    if payload.get("type") == "item":
                        item_count += 1
                    self._sse("news", payload)
                returncode = process.wait()
            except ClientDisconnected:
                process.terminate()
                try:
                    process.wait(timeout=5)
                except subprocess.TimeoutExpired:
                    process.kill()
                    process.wait()
                raise
        except OSError as exc:
            self._sse_text(
                "output", f"\n{command}: could not run {script.name}: {exc}\n"
            )
            return

        if returncode != 0:
            self._sse_text(
                "output",
                f"\n{command}: {script.name} exited with code {returncode}.\n",
            )
        elif item_count == 0:
            self._sse_text(
                "output", f"\n{command}: no stories were found in the feed.\n"
            )

    def _command_ask(self, argument: str) -> None:
        """Answer a quick, self-contained question without retrieval."""
        question = argument.strip()
        if not question:
            self._sse_text("output", "\nUsage: /ask <question>\n")
            return
        stats: dict = {}
        started = time.monotonic()
        try:
            response = chat.answer_simple_question(
                question,
                metrics=stats,
                emit=lambda text: self._sse_text("delta", text),
            )
        except RuntimeError as exc:
            self._sse_text("output", f"\nQuick answer failed: {exc}\n")
            return
        if response:
            self.state.latest_answer = response
            self.state.latest_sources = []
            stats.update(
                {
                    "cached": False,
                    "model": chat.SIMPLE_ANSWER_MODEL,
                    "retrieval": 0.0,
                    "expansion": 0.0,
                    "bm25": 0.0,
                    "filter": 0.0,
                    "candidates": 0,
                    "trunks": 0,
                    "context_build": 0.0,
                    "prompt_build": 0.0,
                    "context_tokens": 0,
                    "prefill": stats.get("prompt_eval_duration", 0.0),
                    "load": stats.get("load_duration", 0.0),
                    "end_to_end_ttft": stats.get("request_ttft", 0.0),
                    "prompt_tokens": stats.get("prompt_eval_count", 0),
                    "prompt_tokens_cached": stats.get(
                        "prompt_eval_cached_count", 0
                    ),
                    "response_tokens": stats.get("eval_count", 0),
                    "total": time.monotonic() - started,
                }
            )
            self.state.latest_benchmark = stats

    def _command_wiki(self, argument: str) -> None:
        question = argument.strip()
        if not question:
            self._sse_text("output", "\nUsage: /wiki <question>\n")
            return
        response = chat.answer_wiki_question(
            question,
            emit=lambda text: self._sse("delta", {"text": text}),
            log=lambda text: self._sse_text("log", text),
        )
        if response:
            self.state.latest_answer = response
            self.state.latest_sources = []

    def _command_wiki_article(self, argument: str) -> None:
        question = argument.strip()
        if not question:
            self._sse_text("output", "\nUsage: /wiki_article <question>\n")
            return
        response = chat.print_wiki_full_articles(
            question,
            emit=lambda text: self._sse("delta", {"text": text}),
            log=lambda text: self._sse_text("log", text),
        )
        if response:
            self.state.latest_answer = response
            self.state.latest_sources = []

    def _command_article(self, argument: str) -> None:
        """Use one local article file as the complete context for the chat."""
        state = self.state
        raw_path = argument.strip()
        simple = bool(
            re.search(r"(?:^|\s)--(?:simple|fast)\b", raw_path)
        )
        iterative = not simple or bool(
            re.search(r"(?:^|\s)--(?:iterative|deep)\b", raw_path)
        )
        raw_path = re.sub(
            r"(?:^|\s)--(?:iterative|deep|simple|fast)\b", " ", raw_path
        ).strip()
        if not raw_path:
            self._sse_text(
                "output",
                "\nUsage: /article [--simple] <path_to_an_article>\n"
                "The file becomes the only context; follow-up questions keep "
                "the article scope until /tag or /transcript is used.\n"
                "The capped multi-round evidence loop is enabled by default "
                "(--simple or --fast disables it).\n",
            )
            return
        if (
            len(raw_path) >= 2
            and raw_path[0] == raw_path[-1]
            and raw_path[0] in {"'", '"'}
        ):
            raw_path = raw_path[1:-1]
        path = Path(raw_path).expanduser()
        if not path.is_absolute():
            path = SCRIPT_DIR / path
        path = path.resolve()
        if not path.is_file():
            self._sse_text(
                "output", f"\nArticle not found: {path}\n"
            )
            return
        try:
            new_chunks, title, structure = _load_article_chunks(path)
        except (OSError, ValueError) as exc:
            self._sse_text(
                "output",
                f"\nCannot use article {path}: {exc}\n",
            )
            return
        # /article replaces the tag/transcript scope for subsequent questions.
        state.transcript = None
        state.replace_chunks(
            new_chunks,
            article_path=str(path),
            article_title=title,
            article_structure=structure,
            article_iterative=iterative,
        )
        state.cache.clear()
        self._sse_text(
            "output",
            "\n"
            + chat.index_memory_report(
                new_chunks, build_index=False, article=str(path)
            )
            + f"\nNow answering from article {str(path)!r} "
            f"({sum(1 for chunk in new_chunks if chunk.index > 0)} "
            "paragraph(s), "
            f"{len(structure.get('sections', []))} section(s)). Existing "
            "conversation context was cleared; follow-up questions stay on "
            "this article."
            + (
                " Iterative evidence mode is enabled.\n"
                if state.article_iterative
                else " Fast single-pass retrieval mode is enabled.\n"
            ),
        )
        self._sse("meta", state.snapshot())

    def _command_dict(self, argument: str) -> None:
        word = argument.strip()
        if not word:
            self._sse_text("output", "\nUsage: /dict <word>\n")
            return
        self._stream_dictionary(word, self.state.latest_answer)

    def _command_sources(self) -> None:
        sources = self.state.latest_sources
        if not sources:
            self._sse_text(
                "output", "\nNo sources available yet. Ask a question first.\n"
            )
            return
        lines = ["", "Sources for the latest answer:"]
        for index, (chunk, score) in enumerate(sources, start=1):
            label = chunk.label
            if self.state.article_path is not None:
                section = _article_source_section(
                    self.state.article_structure, chunk.index
                )
                if section is not None:
                    heading = str(section.get("heading", ""))
                    if heading:
                        label += f" ({heading})"
            lines.append(f"  [Chunk {index}] {label} (bm25 {score:.4f})")
        self._sse_text("output", "\n".join(lines) + "\n")

    def _command_benchmark(self, argument: str) -> None:
        state = self.state
        value = argument.strip().lower()
        if value in {"", "last", "show"}:
            if state.latest_benchmark is None:
                self._sse_text(
                    "output",
                    "\nNo benchmark data yet. Ask a question first, or use "
                    "/benchmark on to show metrics after each answer.\n",
                )
            else:
                self._sse_text(
                    "output",
                    "\n" + chat.format_benchmark_line(state.latest_benchmark) + "\n",
                )
            return
        if value in {"on", "enable", "enabled"}:
            state.benchmark_mode = True
            message = (
                "\nBenchmark display: on (metrics shown after each answer).\n"
            )
            if state.latest_benchmark is not None:
                message += chat.format_benchmark_line(state.latest_benchmark) + "\n"
            self._sse_text("output", message)
            self._sse("meta", state.snapshot())
            return
        if value in {"off", "disable", "disabled"}:
            state.benchmark_mode = False
            self._sse_text("output", "\nBenchmark display: off.\n")
            self._sse("meta", state.snapshot())
            return
        self._sse_text(
            "output",
            "\nUsage: /benchmark [on|off] (no argument shows the latest "
            "answer's metrics)\n",
        )

    def _command_depth(self, argument: str) -> None:
        state = self.state
        value = argument.strip().lower()
        if not value:
            current = "auto" if state.depth is None else str(state.depth)
            self._sse_text(
                "output",
                f"\nRetrieval depth: {current}\n"
                "Usage: /depth <number of trunks> | /depth auto\n"
                f"Default: {chat.DEFAULT_DEPTH} top BM25 matching trunks; "
                "/depth auto enables adaptive retrieval.\n",
            )
            return
        if value in {"auto", "adaptive", "0"}:
            state.depth = None
            state.cache.clear()
            self._sse_text(
                "output",
                "\nRetrieval depth: auto (BM25 score elbow, token budget).\n",
            )
            self._sse("meta", state.snapshot())
            return
        if value in {"default", "reset"}:
            state.depth = chat.DEFAULT_DEPTH
            state.cache.clear()
            self._sse_text(
                "output",
                f"\nRetrieval depth: top {state.depth} BM25 matching trunk(s) "
                "(default).\n",
            )
            self._sse("meta", state.snapshot())
            return
        try:
            new_depth = int(value)
        except ValueError:
            new_depth = 0
        if new_depth < 1:
            self._sse_text(
                "output",
                f"\nInvalid depth: {argument!r}. Use a positive integer, "
                "'auto', or 'default'.\n",
            )
            return
        state.depth = new_depth
        state.cache.clear()
        self._sse_text(
            "output",
            f"\nRetrieval depth: top {new_depth} BM25 matching trunk(s).\n",
        )
        self._sse("meta", state.snapshot())

    # ---- SSE: answer translation -------------------------------------
    def _handle_translate(self, text: str) -> None:
        """Translate a finished answer into Simplified Chinese paragraph by paragraph."""
        self._begin_sse()
        try:
            if len(text) > MAX_TRANSLATION_CHARS:
                raise RuntimeError(
                    f"Answer is too long to translate (limit "
                    f"{MAX_TRANSLATION_CHARS:,} characters)."
                )
            paragraphs = split_translation_paragraphs(text)
            if not paragraphs:
                raise RuntimeError("There is no answer text to translate.")

            self._sse(
                "start",
                {
                    "model": ANSWER_TRANSLATION_MODEL,
                    "paragraphs": len(paragraphs),
                },
            )
            for index, paragraph in enumerate(paragraphs):
                if index:
                    self._sse_text("delta", "\n\n")
                self._stream_translation_paragraph(paragraph)
                self._sse(
                    "paragraph",
                    {"index": index + 1, "total": len(paragraphs)},
                )
            self._sse("done", {"paragraphs": len(paragraphs)})
        except ClientDisconnected:
            pass
        except Exception as exc:
            try:
                self._sse("error", {"message": f"Translation failed: {exc}"})
                self._sse("done", {})
            except ClientDisconnected:
                pass

    def _stream_translation_paragraph(self, paragraph: str) -> None:
        """Stream one paragraph's Simplified Chinese translation."""
        payload = {
            "model": ANSWER_TRANSLATION_MODEL,
            "prompt": chat.ANSWER_TRANSLATION_PROMPT.format(text=paragraph),
            "stream": True,
            "think": False,
            "keep_alive": chat.OLLAMA_KEEP_ALIVE,
            "options": {
                "temperature": 0.1,
                "num_ctx": ANSWER_TRANSLATION_NUM_CTX,
            },
        }
        prefix_filter = TranslationPrefixFilter()
        try:
            with requests.post(
                f"{chat.OLLAMA_HOST}/api/generate",
                json=payload,
                stream=True,
                timeout=(10, 900),
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
                        raise RuntimeError(
                            f"Ollama error: {event['error']}"
                        )
                    token = event.get("response", "")
                    if token:
                        cleaned = prefix_filter.feed(token)
                        if cleaned:
                            self._sse_text("delta", cleaned)
                    if event.get("done"):
                        break
        except requests.RequestException as exc:
            raise RuntimeError(
                f"Could not reach Ollama at {chat.OLLAMA_HOST}. "
                f"Is it running? ({exc})"
            ) from exc

        trailing = prefix_filter.flush()
        if trailing:
            self._sse_text("delta", trailing)

    # ---- SSE: single code-line explanation ---------------------------
    def _handle_explain_line(self, payload: dict) -> None:
        """Explain one line of a code snippet from a documentation-backed tag."""
        tag = str(payload.get("tag") or self.state.tag or "").strip().lower()
        code = payload.get("code")
        try:
            line_number = int(payload.get("line") or 0)
        except (TypeError, ValueError):
            line_number = 0

        if not tag_uses_official_docs(tag):
            self._send_error_json(
                "Line explanations are available for rust, golang, and php tags.",
                400,
            )
            return
        if not isinstance(code, str) or not code.strip():
            self._send_error_json("Missing code snippet", 400)
            return
        if len(code) > MAX_LINE_EXPLAIN_CODE_CHARS:
            self._send_error_json(
                f"Code snippet is too long to explain "
                f"(limit {MAX_LINE_EXPLAIN_CODE_CHARS:,} characters).",
                413,
            )
            return
        code_lines = code.splitlines()
        if line_number < 1 or line_number > len(code_lines):
            self._send_error_json(
                f"Line {line_number} is outside the code snippet "
                f"(1-{len(code_lines)}).",
                400,
            )
            return

        language = tag_code_language(tag) or "the programming language"
        fence = tag_code_fence(tag) or "text"
        model = tag_coding_model(tag)
        self._begin_sse()
        try:
            self._sse(
                "start",
                {
                    "model": model,
                    "tag": tag,
                    "language": language,
                    "line": line_number,
                },
            )
            self._stream_line_explanation(
                code_lines, line_number, language, fence, model
            )
            self._sse("done", {"line": line_number})
        except ClientDisconnected:
            pass
        except Exception as exc:
            try:
                self._sse("error", {"message": f"Line explanation failed: {exc}"})
                self._sse("done", {})
            except ClientDisconnected:
                pass

    def _stream_line_explanation(
        self,
        code_lines: list[str],
        line_number: int,
        language: str,
        fence: str,
        model: str,
    ) -> None:
        numbered_code = "\n".join(
            f"{index:>4} | {line}"
            for index, line in enumerate(code_lines, start=1)
        )
        prompt = LINE_EXPLAIN_PROMPT.format(
            language=language,
            fence=fence,
            numbered_code=numbered_code,
            line_number=line_number,
            line=code_lines[line_number - 1],
        )
        payload = {
            "model": model,
            "prompt": prompt,
            "stream": True,
            "think": False,
            "keep_alive": chat.OLLAMA_KEEP_ALIVE,
            "options": {
                "temperature": 0.2,
                "num_ctx": LINE_EXPLAIN_NUM_CTX,
            },
        }
        try:
            with requests.post(
                f"{chat.OLLAMA_HOST}/api/generate",
                json=payload,
                stream=True,
                timeout=(10, 900),
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
                        self._sse_text("delta", token)
                    if event.get("done"):
                        break
        except requests.RequestException as exc:
            raise RuntimeError(
                f"Could not reach Ollama at {chat.OLLAMA_HOST}. "
                f"Is it running? ({exc})"
            ) from exc

    # ---- SSE: ask a question about a whole code snippet -----------------
    def _handle_snippet_question(self, payload: dict) -> None:
        """Answer a free-form question about one code snippet."""
        tag = str(payload.get("tag") or self.state.tag or "").strip().lower()
        code = payload.get("code")
        question = str(payload.get("question") or "").strip()
        raw_language = str(
            payload.get("language") or tag_code_language(tag) or ""
        ).strip()

        if not question:
            self._send_error_json("Missing question", 400)
            return
        if not isinstance(code, str) or not code.strip():
            self._send_error_json("Missing code snippet", 400)
            return
        if len(code) > MAX_SNIPPET_QUESTION_CODE_CHARS:
            self._send_error_json(
                f"Code snippet is too long to ask about "
                f"(limit {MAX_SNIPPET_QUESTION_CODE_CHARS:,} characters).",
                413,
            )
            return

        unsupported = ("", "default", "text", "plaintext")
        language = (
            "programming"
            if raw_language.lower() in unsupported
            else raw_language
        )
        fence = re.sub(r"[^A-Za-z0-9_+.#-]", "", raw_language) or "text"
        model = CODE_GENERATION_MODEL

        self._begin_sse()
        try:
            self._sse(
                "start",
                {"model": model, "tag": tag, "language": language},
            )
            self._stream_snippet_question(
                code, question, language, fence, model
            )
            self._sse("done", {})
        except ClientDisconnected:
            pass
        except Exception as exc:
            try:
                self._sse(
                    "error",
                    {"message": f"Snippet answer failed: {exc}"},
                )
                self._sse("done", {})
            except ClientDisconnected:
                pass

    def _stream_snippet_question(
        self,
        code: str,
        question: str,
        language: str,
        fence: str,
        model: str,
    ) -> None:
        prompt = SNIPPET_QUESTION_PROMPT.format(
            language=language,
            fence=fence,
            code=code,
            question=question,
        )
        payload = {
            "model": model,
            "prompt": prompt,
            "stream": True,
            "think": False,
            "keep_alive": chat.OLLAMA_KEEP_ALIVE,
            "options": {
                "temperature": 0.2,
                "num_ctx": SNIPPET_QUESTION_NUM_CTX,
            },
        }
        try:
            with requests.post(
                f"{chat.OLLAMA_HOST}/api/generate",
                json=payload,
                stream=True,
                timeout=(10, 900),
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
                        self._sse_text("delta", token)
                    if event.get("done"):
                        break
        except requests.RequestException as exc:
            raise RuntimeError(
                f"Could not reach Ollama at {chat.OLLAMA_HOST}. "
                f"Is it running? ({exc})"
            ) from exc

    # ---- SSE: dictionary popup ---------------------------------------
    def _handle_dict(self, word: str, answer: str | None) -> None:
        self._begin_sse()
        try:
            if not word:
                self._sse_text("delta", "Usage: /dict <word>\n")
            else:
                context = answer if answer is not None else self.state.latest_answer
                self._stream_dictionary(word, context)
            self._sse("done", {"word": word})
        except ClientDisconnected:
            pass
        except Exception as exc:
            try:
                self._sse("error", {"message": str(exc)})
                self._sse("done", {"word": word})
            except ClientDisconnected:
                pass

    def _stream_dictionary(self, word: str, latest_answer: str | None) -> None:
        # The popup shows exactly: word, IPA, part of speech, quote, and the
        # contextual meaning. Keep the prompt and the streamed output limited
        # to those fields (no model banner or follow-up usage section).
        prompt = chat.DICT_POPUP_PROMPT.format(
            word=word,
            latest_answer=(latest_answer or "(no previous answer available)"),
        )
        try:
            chat.stream_answer(
                prompt,
                model=chat.DICT_MODEL,
                emit=lambda text: self._sse("delta", {"text": text}),
            )
        except RuntimeError as exc:
            self._sse("error", {"message": f"Dictionary lookup failed: {exc}"})


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="HTTP backend for the app/chat.html browser console."
    )
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", type=int, default=8765)
    parser.add_argument(
        "--tag",
        default=chat.DEFAULT_TAG,
        help=f"tag to load at startup (default: {chat.DEFAULT_TAG})",
    )
    parser.add_argument(
        "--max-context-tokens",
        type=int,
        default=chat.MAX_CONTEXT_TOKENS,
        metavar="N",
        help="estimated-token budget for adaptive retrieval",
    )
    parser.add_argument(
        "--progressive",
        action="store_true",
        help="stream a fast first pass, then append an expansion pass",
    )
    parser.add_argument(
        "--no-prewarm",
        action="store_true",
        help="do not warm the Ollama models at startup",
    )
    return parser.parse_args(argv)


def main() -> None:
    args = parse_args()
    if not CHAT_HTML.is_file():
        sys.exit(f"Missing front end: {CHAT_HTML}")

    try:
        state = ChatState(
            args.tag,
            max_context_tokens=args.max_context_tokens,
            progressive=args.progressive,
        )
    except SystemExit as exc:
        sys.exit(f"Cannot start chat server: {exc}")

    if not args.no_prewarm:
        threading.Thread(target=chat.prewarm, daemon=True).start()

    ChatRequestHandler.state = state
    server = ThreadingHTTPServer((args.host, args.port), ChatRequestHandler)
    server.daemon_threads = True
    address = f"http://{args.host}:{args.port}/"
    print(f"Loaded {len(state.chunks)} trunks for tag {state.tag!r}.")
    print(f"YouTube local chat server running at {address}")
    print("Press Ctrl+C to stop.")
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        print("\nStopping chat server.")
    finally:
        server.server_close()


if __name__ == "__main__":
    main()
