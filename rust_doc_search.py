#!/usr/bin/env python3
"""Answer Rust questions using only the official Rust documentation.

Usage:
    python3 rust_doc_search.py <question>
    python3 rust_doc_search.py "how do I share state between threads?"
    python3 rust_doc_search.py --refresh "what is a lifetime?"

Source of truth:
    https://doc.rust-lang.org/ (The Book, the standard library, the language
    reference, Rust by Example, Cargo, the Edition Guide, and related books)
    and, for code-sample questions that name a crate, https://docs.rs/{crate}.

How it works:
    1. Crawl the official Rust documentation once and cache the page text
       locally (default: ~/.cache/rust_doc_search/index.json). The crawl is
       bounded per documentation section so the Book, the reference, and the
       standard library all contribute to the index.
    2. Ask Ollama model ornith:35b (thinking disabled) to turn the question
       into search keywords, and to detect whether the question asks for a
       code sample and which crates it names.
    3. Rank cached documentation chunks with BM25 using those keywords.
    4. If the question asks for a code sample, also inspect the ranking
       excerpts and the question for crate references (for example "the serde
       crate", ``serde_json::Value``, or ``use tokio::...``), then fetch and
       rank https://docs.rs/{crate} for each related crate. Crate pages are
       cached under the same cache directory.
    5. Ask ornith:35b (thinking disabled) to answer strictly from the best
       matching excerpts, citing the doc.rust-lang.org/ and docs.rs/ URLs used.

The index is refreshed automatically when it is older than seven days. Use
--refresh to force a refresh, or --offline to search a cached index without
touching the network.

Only pages under https://doc.rust-lang.org/ and the explicitly requested
https://docs.rs/{crate} pages are used; no other source is used.
"""

from __future__ import annotations

import argparse
import heapq
import json
import os
import re
import sys
import time
from collections import defaultdict
from concurrent.futures import ThreadPoolExecutor
from html.parser import HTMLParser
from pathlib import Path
from urllib.parse import urldefrag, urljoin, urlparse

from doc_answer_cleanup import (
    ANSWER_CONTINUATION_PROMPT,
    ANSWER_NUM_PREDICT,
    MAX_ANSWER_CONTINUATIONS,
    CodeFenceRepairFilter,
    DocumentationCaveatFilter,
    close_unclosed_code_fence,
    split_complete_lines,
    strip_repeated_continuation_prefix,
)
from knowledge_docs import boost_knowledge_scores, knowledge_chunks

try:
    import requests
except ImportError as exc:  # pragma: no cover - dependency guard
    sys.exit(f"Missing dependency: {exc.name}. Install with: pip install requests")

try:
    from rank_bm25 import BM25Okapi
except ImportError:  # pragma: no cover - optional dependency
    BM25Okapi = None


DOC_ROOT = "https://doc.rust-lang.org/"
DOC_HOST = "doc.rust-lang.org"
CRATE_ROOT = "https://docs.rs/"
MODEL = "ornith:35b"

# Documentation sections to crawl. Priorities keep the Book and the language
# reference ahead of the enormous standard-library tree, and the per-section
# limits make sure every major section contributes pages to the index.
DOC_SEEDS: tuple[tuple[float, str], ...] = (
    (0.0, "https://doc.rust-lang.org/book/"),
    (0.1, "https://doc.rust-lang.org/reference/"),
    (0.2, "https://doc.rust-lang.org/std/"),
    # High-value standard-library pages, fetched before the rest of /std/.
    (0.2, "https://doc.rust-lang.org/std/iter/"),
    (0.2, "https://doc.rust-lang.org/std/collections/"),
    (0.2, "https://doc.rust-lang.org/std/vec/"),
    (0.2, "https://doc.rust-lang.org/std/string/"),
    (0.2, "https://doc.rust-lang.org/std/option/"),
    (0.2, "https://doc.rust-lang.org/std/result/"),
    (0.2, "https://doc.rust-lang.org/std/sync/"),
    (0.2, "https://doc.rust-lang.org/std/thread/"),
    (0.2, "https://doc.rust-lang.org/std/fs/"),
    (0.2, "https://doc.rust-lang.org/std/io/"),
    (0.2, "https://doc.rust-lang.org/std/net/"),
    (0.2, "https://doc.rust-lang.org/std/process/"),
    (0.2, "https://doc.rust-lang.org/std/time/"),
    (0.2, "https://doc.rust-lang.org/std/env/"),
    (0.2, "https://doc.rust-lang.org/std/path/"),
    (0.2, "https://doc.rust-lang.org/std/mem/"),
    (0.2, "https://doc.rust-lang.org/std/ptr/"),
    (0.2, "https://doc.rust-lang.org/std/rc/"),
    (0.2, "https://doc.rust-lang.org/std/cell/"),
    (0.2, "https://doc.rust-lang.org/std/convert/"),
    (0.2, "https://doc.rust-lang.org/std/error/"),
    (0.2, "https://doc.rust-lang.org/std/fmt/"),
    (0.2, "https://doc.rust-lang.org/std/hash/"),
    (0.2, "https://doc.rust-lang.org/std/slice/"),
    (0.2, "https://doc.rust-lang.org/std/str/"),
    (0.2, "https://doc.rust-lang.org/std/array/"),
    (0.3, "https://doc.rust-lang.org/rust-by-example/"),
    (0.4, "https://doc.rust-lang.org/cargo/"),
    (0.5, "https://doc.rust-lang.org/edition-guide/"),
    (0.6, "https://doc.rust-lang.org/nomicon/"),
    (0.7, "https://doc.rust-lang.org/rustc/"),
    (0.8, "https://doc.rust-lang.org/rustdoc/"),
    (0.9, "https://doc.rust-lang.org/style-guide/"),
    (1.0, "https://doc.rust-lang.org/embedded-book/"),
    (1.1, "https://doc.rust-lang.org/core/"),
    (1.2, "https://doc.rust-lang.org/alloc/"),
    (1.3, "https://doc.rust-lang.org/proc_macro/"),
    (1.4, "https://doc.rust-lang.org/test/"),
    (1.5, "https://doc.rust-lang.org/error_codes/"),
)
DOC_PATH_PREFIXES = (
    "/book/",
    "/reference/",
    "/std/",
    "/core/",
    "/alloc/",
    "/proc_macro/",
    "/test/",
    "/rust-by-example/",
    "/cargo/",
    "/edition-guide/",
    "/nomicon/",
    "/rustc/",
    "/rustdoc/",
    "/style-guide/",
    "/embedded-book/",
    "/error_codes/",
)
# Per-section page budgets. The standard library is the largest tree, so its
# budget is bigger, but every major section still gets indexed.
SECTION_LIMITS = {
    "/book/": 45,
    "/reference/": 35,
    "/std/": 95,
    "/core/": 12,
    "/alloc/": 8,
    "/proc_macro/": 4,
    "/test/": 4,
    "/rust-by-example/": 25,
    "/cargo/": 20,
    "/edition-guide/": 10,
    "/nomicon/": 10,
    "/rustc/": 8,
    "/rustdoc/": 6,
    "/style-guide/": 4,
    "/embedded-book/": 6,
    "/error_codes/": 4,
    "/": 2,
}
DEFAULT_SECTION_LIMIT = 8

DEFAULT_CACHE_DIR = Path(
    os.environ.get("RUST_DOC_SEARCH_CACHE", "~/.cache/rust_doc_search")
).expanduser()
CACHE_VERSION = 2
CRATE_CACHE_VERSION = 2
CACHE_MAX_AGE_SECONDS = 7 * 24 * 60 * 60

DEFAULT_MAX_PAGES = 250
CRAWL_WORKERS = 4
REQUEST_TIMEOUT = (10, 30)
USER_AGENT = "rust-doc-search/1.0 (+local documentation search)"

CHUNK_TARGET_CHARS = 1100
CHUNK_MAX_CHARS = 1800
CHUNKS_PER_PAGE = 2
CRATE_CHUNKS_PER_PAGE = 3
DEFAULT_TOP_K = 8
DEFAULT_CRATE_TOP_K = 3
MAX_CRATES = 3

OLLAMA_HOST = os.environ.get("OLLAMA_HOST", "http://localhost:11434").rstrip("/")
if not OLLAMA_HOST.startswith(("http://", "https://")):
    OLLAMA_HOST = f"http://{OLLAMA_HOST}"

STOPWORDS = {
    "a", "about", "after", "all", "also", "an", "and", "any", "are", "as",
    "at", "be", "because", "been", "before", "but", "by", "can", "could",
    "do", "does", "doing", "for", "from", "get", "has", "have", "he", "her",
    "here", "him", "his", "how", "i", "if", "in", "into", "is", "it", "its",
    "just", "me", "my", "of", "on", "or", "our", "should", "so", "some",
    "tell", "than", "that", "the", "their", "them", "then", "there", "these",
    "they", "this", "to", "was", "we", "were", "what", "when", "where",
    "which", "who", "why", "will", "with", "would", "you", "your",
}

KEYWORD_PROMPT = """You turn Rust programming questions into search keywords
for the official Rust documentation at https://doc.rust-lang.org/ and, for
crate questions, https://docs.rs/.

Question: {question}

Return a JSON object of exactly this shape:
{{"keywords": ["keyword or short phrase", "..."], "code_sample": false, "crates": ["crate name", "..."]}}

Rules:
- Return 3 to 8 keywords or short phrases.
- Include exact Rust identifiers, traits, types, modules, macros, functions,
  and concepts that would appear in Rust documentation (for example:
  ownership, borrow checker, lifetime, Result, Option, trait, impl, async,
  tokio::spawn, std::sync::Arc, HashMap).
- Prefer terminology used by the Rust documentation over general wording.
- Set "code_sample" to true when the question asks for example code, a
  snippet, a usage demonstration, or an implementation (for example: "show me
  code for ...", "how do I use ...", "give an example of ..."). Otherwise set
  it to false.
- Set "crates" to third-party crate names that are explicitly mentioned in the
  question (for example: serde, serde_json, tokio, anyhow, clap). Do not
  include standard-library modules such as std, core, or alloc. Use an empty
  list when the question does not name any crate. Never guess a crate.
- Do not include explanations, URLs, or markdown.
"""

ANSWER_PROMPT = """You are a Rust documentation assistant. Use the numbered
excerpts inside <context> as the primary source of truth for factual claims.

Question: {question}

<context>
{context}
</context>

Instructions:
- If the question asks for code, a snippet, an example, or an implementation
  and the excerpts do not contain a complete example, DO NOT refuse and DO
  NOT say the excerpts are insufficient. Do not open the answer with a
  sentence about what the documentation lacks. Start directly with the
  explanation or code. Still write a complete, runnable Rust program or
  function that answers the question. Use the APIs and patterns shown in the
  excerpts where they are relevant, and fill any remaining gaps with your own
  general Rust knowledge.
- When you add code that is not directly supported by an excerpt, do not cite
  an excerpt for it, and do not mention that the indexed docs were missing
  the answer at all.
- If the excerpts mention a specific Rust API for the task (for example
  std::sync::Arc), use that API in the working code instead of switching to a
  different crate. Use only std/core/alloc and the crates the excerpts
  explicitly show; never add a third-party crate unless an excerpt uses it.
- For non-code questions, if the excerpts genuinely do not contain the
  answer, say clearly that the indexed Rust documentation
  (doc.rust-lang.org and docs.rs) does not contain this information.
- Cite the excerpt number for each factual claim taken from an excerpt, for
  example: "Use Arc [2]."
- Prefer precise, practical answers. Include short Rust code examples when the
  documentation provides them.
- Never invent APIs, types, traits, macros, or behavior that do not exist in
  Rust. Any generated code must use real standard-library or documented
  packages.
- Be concise but complete.
"""

BLOCK_TAGS = {
    "address", "article", "aside", "blockquote", "br", "dd", "div", "dl",
    "dt", "fieldset", "figcaption", "figure", "footer", "form", "h1", "h2",
    "h3", "h4", "h5", "h6", "header", "hr", "li", "main", "nav", "ol", "p",
    "pre", "section", "table", "tbody", "td", "tfoot", "th", "thead", "tr",
    "ul",
}
# Only skip leaf-ish elements that never contain documentation prose. Do not
# skip nav/header/footer here: Rust documentation uses nested navigation that
# would otherwise swallow the rest of the page.
SKIP_TAGS = {
    "button", "canvas", "noscript", "script", "style", "svg", "template",
}
SKIP_EXTENSIONS = (
    ".css", ".csv", ".gif", ".gz", ".ico", ".jpeg", ".jpg", ".js", ".json",
    ".mp3", ".mp4", ".pdf", ".png", ".svg", ".tar", ".tgz", ".webp", ".woff",
    ".woff2", ".xml", ".zip",
)

CODE_SAMPLE_RE = re.compile(
    r"\b(code|snippet|examples?|sample|implement|implementation|demo|usage|"
    r"write\s+(?:a|an|me|some)|show\s+me\s+(?:how|the)|how\s+(?:do|can|"
    r"should|would)\s+(?:i|we|you)\s+(?:use|write|implement|call|create))\b",
    re.IGNORECASE,
)
# Crate names that are common in Rust code. Explicit syntax (``use X::``,
# ``extern crate X``, "the X crate", docs.rs/X, or backticks) is always
# trusted; this list lets a question such as "show me serde code" be routed
# to docs.rs without guessing.
KNOWN_CRATES = frozenset(
    """
    actix actix-web anyhow async-std axum bytes chrono clap config console
    crossbeam criterion dashmap diesel dialoguer dioxus dotenv egui env_logger
    eyre futures glob hyper image indicatif itertools lazy_static leptos log
    log4rs mockall nalgebra ndarray notify num once_cell owo-colors parking_lot
    plotters poem prost pyo3 quickcheck rand rayon regex reqwest ring rocket
    rstest rustls sea-orm serde serde_json serde_yaml sled snafu sqlx structopt
    tauri tempfile thiserror tokio tokio-util toml tonic tracing
    tracing-subscriber uuid walkdir warp wgpu winit yew
    """.split()
)
# Names that are also ordinary English words. They are only treated as crates
# when the question uses explicit crate syntax.
AMBIGUOUS_CRATES = frozenset(
    "bytes colored config console criterion glob image log notify time"
    .split()
)
RESERVED_CRATES = frozenset(
    """
    std core alloc proc_macro test crate self super rust
    as async await break const continue dyn else enum extern false fn for
    if impl in let loop match mod move mut pub ref return static struct trait
    true type unsafe use where while
    """.split()
)
# Ordinary words that appear next to "crate" in prose ("a new crate",
# "the generated crate") but are not crate names.
NON_CRATE_WORDS = frozenset(
    """
    all any binary current each entire external generated large library local
    new no other own same separate simple single small that this whole
    """.split()
)
# Standard-library item names that users sometimes call "crates". They are
# covered by doc.rust-lang.org, not docs.rs.
STD_ITEM_NAMES = frozenset(
    name.lower()
    for name in """
    Arc BTreeMap BTreeSet Box Cell Cow Duration File HashMap HashSet Instant
    Mutex Option Path PathBuf Rc RefCell Result RwLock String TcpStream Vec
    """.split()
)
CRATE_NAME_RE = re.compile(r"[A-Za-z][A-Za-z0-9_-]*")
CRATE_URL_RE = re.compile(r"docs\.rs/([A-Za-z0-9_][A-Za-z0-9_-]*)")
CRATE_PHRASE_RE = re.compile(
    r"\b(?:the\s+|a\s+|new\s+|new\s+the\s+)?"
    r"`?([a-z][a-z0-9_-]{1,63})`?\s+crate\b",
    re.IGNORECASE,
)
USE_DECL_RE = re.compile(r"\buse\s+(?:::)?([A-Za-z_][A-Za-z0-9_]*)\b")
EXTERN_CRATE_RE = re.compile(
    r"\bextern\s+crate\s+([A-Za-z_][A-Za-z0-9_]*)\b"
)
QUALIFIED_PATH_RE = re.compile(
    r"(?<![\w:])([A-Za-z][A-Za-z0-9_-]*)::"
)
BACKTICK_RE = re.compile(r"`([^`\n]+)`")
# Explicit docs.rs references in documentation code examples.
PKG_DEV_URL_RE = CRATE_URL_RE


class DocHTMLParser(HTMLParser):
    """Extract readable text, the page title, and links from a Rust page."""

    def __init__(self):
        super().__init__(convert_charrefs=True)
        self.title = ""
        self.main_text = ""
        self.links: list[str] = []
        self._in_title = False
        self._in_article = 0
        self._skip_depth = 0
        self._article_parts: list[str] = []
        self._main_parts: list[str] = []
        self._fallback_parts: list[str] = []
        self._capture_depth = 0
        self._saw_main = False
        self._pre_depth = 0

    def handle_starttag(self, tag, attrs):
        tag = tag.lower()
        if tag == "title":
            self._in_title = True
            return
        if tag == "article":
            self._in_article += 1
        if tag == "main":
            self._capture_depth += 1
            self._saw_main = True
        if tag == "pre":
            # Preserve code samples from the documentation as fenced blocks.
            # Whitespace inside <pre> is significant, so it must not be
            # collapsed the way prose is.
            self._pre_depth += 1
            self._append("\n```\n")
            return
        if tag in SKIP_TAGS:
            self._skip_depth += 1
            return
        if tag == "a":
            href = dict(attrs).get("href")
            if href:
                self.links.append(href)
        if tag in BLOCK_TAGS:
            self._append("\n")
        if tag == "img":
            alt = dict(attrs).get("alt")
            if alt:
                self._append(f" {alt} ")

    def handle_endtag(self, tag):
        tag = tag.lower()
        if tag == "title":
            self._in_title = False
            return
        if tag == "article" and self._in_article:
            self._in_article -= 1
        if tag == "main" and self._capture_depth:
            self._capture_depth -= 1
        if tag == "pre" and self._pre_depth:
            self._pre_depth -= 1
            self._append("\n```\n")
            return
        if tag in SKIP_TAGS:
            if self._skip_depth:
                self._skip_depth -= 1
            return
        if tag in BLOCK_TAGS:
            self._append("\n")

    def handle_data(self, data):
        if self._in_title:
            self.title += data
        if self._skip_depth:
            return
        self._append(data)

    def _append(self, text):
        if self._skip_depth:
            return
        if self._in_article:
            self._article_parts.append(text)
        elif self._capture_depth:
            self._main_parts.append(text)
        elif not self._saw_main:
            # Pages without a <main> element: keep the whole page as a
            # fallback (navigation noise is preferable to no content).
            self._fallback_parts.append(text)

    def finish(self):
        article = _clean_text("".join(self._article_parts))
        main = _clean_text("".join(self._main_parts))
        fallback = _clean_text("".join(self._fallback_parts))
        # Prefer <article>, then <main>, then the whole page. Navigation,
        # headers, and footers outside <main> are intentionally ignored.
        for candidate in (article, main, fallback):
            if len(candidate) >= 200:
                self.main_text = candidate
                break
        else:
            self.main_text = max((article, main, fallback), key=len)
        self.title = " ".join(self.title.split())
        return self


def _clean_text(text: str) -> str:
    """Normalize whitespace while keeping paragraph and code boundaries."""
    text = text.replace("\r", "\n").replace("\u00a0", " ")
    lines = []
    in_code = False
    for line in text.split("\n"):
        stripped = line.strip()
        if stripped.startswith("```"):
            in_code = not in_code
            lines.append(stripped)
            continue
        if in_code:
            # Code samples from <pre> keep their indentation and line breaks.
            lines.append(line.rstrip())
            continue
        line = " ".join(line.split())
        if line:
            lines.append(line)
    return "\n".join(lines)


def canonical_url(url: str) -> str:
    url, _fragment = urldefrag(url)
    parsed = urlparse(url)
    path = parsed.path
    # Treat "directory/index.html" and "directory/" as the same page so the
    # bounded crawl does not spend its budget on duplicates.
    if path.endswith("/index.html"):
        path = path[: -len("index.html")]
    return f"{parsed.scheme}://{parsed.netloc}{path}"


def section_for_url(url: str) -> str:
    path = urlparse(url).path
    for prefix in SECTION_LIMITS:
        if prefix != "/" and path.startswith(prefix):
            return prefix
    return "/"


def is_allowed_doc_url(url: str) -> bool:
    parsed = urlparse(url)
    if parsed.scheme not in ("http", "https"):
        return False
    if parsed.netloc != DOC_HOST:
        return False
    path = parsed.path
    if path in ("", "/"):
        return True
    if not any(path.startswith(prefix) for prefix in DOC_PATH_PREFIXES):
        return False
    lowered = path.lower()
    if lowered.endswith(SKIP_EXTENSIONS):
        return False
    # Source listing pages duplicate the item docs and are not useful prose.
    if "/src/" in path or path.startswith("/src"):
        return False
    if "/implementors/" in path:
        return False
    if lowered.endswith("/print.html"):
        return False
    if parsed.query:
        return False
    return True


def fetch_page(session: requests.Session, url: str) -> dict | None:
    try:
        response = session.get(url, timeout=REQUEST_TIMEOUT)
        response.raise_for_status()
    except requests.RequestException as exc:
        print(f"Warning: could not fetch {url}: {exc}", file=sys.stderr)
        return None

    content_type = response.headers.get("Content-Type", "")
    if "html" not in content_type.lower():
        return None
    # doc.rust-lang.org serves UTF-8 HTML without a charset parameter.
    # requests would otherwise fall back to ISO-8859-1 and mangle non-ASCII
    # text in titles and examples.
    if "charset=" not in content_type.lower():
        response.encoding = "utf-8"

    parser = DocHTMLParser()
    try:
        parser.feed(response.text)
        parser.close()
    except Exception as exc:  # defensive: malformed markup
        print(f"Warning: could not parse {url}: {exc}", file=sys.stderr)
        return None

    page = parser.finish()
    text = page.main_text
    if len(text) < 80:
        return None

    links = []
    for href in page.links:
        link = canonical_url(urljoin(response.url, href))
        if is_allowed_doc_url(link):
            links.append(link)

    return {
        "url": canonical_url(response.url),
        "title": page.title or url,
        "text": text,
        # Preserve document order: the Book's navigation order is more useful
        # than alphabetical order when the crawl budget is bounded.
        "links": list(dict.fromkeys(links)),
    }


def crawl_docs(max_pages: int) -> list[dict]:
    """Priority crawl of the official Rust documentation tree."""
    pending: list[tuple[float, str]] = []
    seen: set[str] = set()
    section_queued: defaultdict[str, int] = defaultdict(int)
    section_fetched: defaultdict[str, int] = defaultdict(int)
    for priority, seed in DOC_SEEDS:
        url = canonical_url(seed)
        if url in seen:
            continue
        seen.add(url)
        section = section_for_url(url)
        section_queued[section] += 1
        heapq.heappush(pending, (priority, url))

    pages: dict[str, dict] = {}

    session = requests.Session()
    session.headers.update({"User-Agent": USER_AGENT})

    with ThreadPoolExecutor(max_workers=CRAWL_WORKERS) as pool:
        while pending and len(pages) < max_pages:
            batch: list[tuple[float, str]] = []
            while pending and len(batch) < CRAWL_WORKERS:
                priority, url = heapq.heappop(pending)
                section = section_for_url(url)
                section_queued[section] = max(0, section_queued[section] - 1)
                limit = SECTION_LIMITS.get(section, DEFAULT_SECTION_LIMIT)
                if section_fetched[section] >= limit:
                    continue
                section_fetched[section] += 1
                batch.append((priority, url))

            if not batch:
                break

            futures = [
                pool.submit(fetch_page, session, url) for _priority, url in batch
            ]
            for (priority, url), future in zip(batch, futures):
                page = future.result()
                if page is None:
                    continue
                pages.setdefault(page["url"], page)
                for link in page["links"]:
                    if link in seen:
                        continue
                    section = section_for_url(link)
                    limit = SECTION_LIMITS.get(section, DEFAULT_SECTION_LIMIT)
                    if (
                        section_fetched[section] + section_queued[section]
                        >= limit
                    ):
                        continue
                    if len(seen) >= max_pages * 10:
                        break
                    seen.add(link)
                    section_queued[section] += 1
                    heapq.heappush(pending, (priority + 0.01, link))
                if len(pages) >= max_pages:
                    break

    return list(pages.values())


def cache_path(cache_dir: Path) -> Path:
    return cache_dir / "index.json"


def crate_cache_path(cache_dir: Path, crate: str) -> Path:
    safe = re.sub(r"[^A-Za-z0-9_.\-]+", "__", crate).strip("._")
    return cache_dir / "crates" / f"{safe}.json"


def load_cache(path: Path):
    try:
        with path.open("r", encoding="utf-8") as handle:
            data = json.load(handle)
    except (OSError, ValueError):
        return None
    if not isinstance(data, dict) or data.get("version") != CACHE_VERSION:
        return None
    pages = data.get("pages")
    if not isinstance(pages, list) or not pages:
        return None
    return data


def save_cache(path: Path, pages: list[dict]):
    path.parent.mkdir(parents=True, exist_ok=True)
    payload = {
        "version": CACHE_VERSION,
        "fetched_at": time.time(),
        "source": DOC_ROOT,
        "pages": pages,
    }
    temporary = path.with_suffix(path.suffix + ".tmp")
    with temporary.open("w", encoding="utf-8") as handle:
        json.dump(payload, handle, ensure_ascii=False, separators=(",", ":"))
    temporary.replace(path)


def get_pages(args) -> list[dict]:
    path = cache_path(Path(args.cache_dir).expanduser())
    cached = load_cache(path)
    now = time.time()

    if args.offline:
        if cached is None:
            sys.exit(
                f"No cached documentation index at {path}. "
                "Run without --offline first."
            )
        print(f"Using cached index: {path}", file=sys.stderr)
        return cached["pages"]

    stale = (
        cached is None
        or args.refresh
        or now - float(cached.get("fetched_at", 0)) > CACHE_MAX_AGE_SECONDS
    )
    if not stale:
        print(f"Using cached index: {path}", file=sys.stderr)
        return cached["pages"]

    print(
        f"Indexing {DOC_ROOT} (this happens only occasionally)...",
        file=sys.stderr,
    )
    try:
        pages = crawl_docs(args.max_pages)
    except Exception as exc:
        print(f"Warning: documentation crawl failed: {exc}", file=sys.stderr)
        pages = []

    if not pages:
        if cached is not None:
            print("Warning: using stale cached index instead.", file=sys.stderr)
            return cached["pages"]
        sys.exit(
            "Could not index https://doc.rust-lang.org/. "
            "Check your network connection."
        )

    try:
        save_cache(path, pages)
        print(f"Indexed {len(pages)} pages into {path}", file=sys.stderr)
    except OSError as exc:
        print(f"Warning: could not save cache: {exc}", file=sys.stderr)
    return pages


def load_crate_cache(path: Path):
    try:
        with path.open("r", encoding="utf-8") as handle:
            data = json.load(handle)
    except (OSError, ValueError):
        return None
    if not isinstance(data, dict) or data.get("version") != CRATE_CACHE_VERSION:
        return None
    if not isinstance(data.get("text"), str) or not data["text"]:
        return None
    return data


def save_crate_cache(path: Path, page: dict):
    path.parent.mkdir(parents=True, exist_ok=True)
    payload = {
        "version": CRATE_CACHE_VERSION,
        "fetched_at": time.time(),
        "url": page["url"],
        "title": page["title"],
        "text": page["text"],
    }
    temporary = path.with_suffix(path.suffix + ".tmp")
    with temporary.open("w", encoding="utf-8") as handle:
        json.dump(payload, handle, ensure_ascii=False, separators=(",", ":"))
    temporary.replace(path)


def get_crate_page(
    session: requests.Session,
    crate: str,
    cache_dir: Path,
    refresh: bool = False,
    offline: bool = False,
) -> dict | None:
    """Fetch (or load from cache) https://docs.rs/{crate}."""
    path = crate_cache_path(cache_dir, crate)
    cached = load_crate_cache(path)
    if cached is not None:
        page = {
            "url": cached.get("url") or urljoin(CRATE_ROOT, crate),
            "title": cached.get("title") or crate,
            "text": cached["text"],
            "links": [],
        }
    else:
        page = None

    if offline:
        if page is None:
            print(
                f"Warning: no cached docs.rs page for {crate}; skipping.",
                file=sys.stderr,
            )
        return page

    now = time.time()
    fresh = (
        page is not None
        and not refresh
        and now - float(cached.get("fetched_at", 0)) <= CACHE_MAX_AGE_SECONDS
    )
    if fresh:
        return page

    url = urljoin(CRATE_ROOT, crate)
    print(f"Fetching {url} ...", file=sys.stderr)
    fetched = fetch_page(session, url)
    if fetched is None:
        if page is not None:
            print(
                f"Warning: using stale cached docs.rs page for {crate}.",
                file=sys.stderr,
            )
            return page
        print(
            f"Warning: could not fetch docs.rs page for {crate}; skipping.",
            file=sys.stderr,
        )
        return None

    try:
        save_crate_cache(path, fetched)
    except OSError as exc:
        print(
            f"Warning: could not save docs.rs cache for {crate}: {exc}",
            file=sys.stderr,
        )
    return fetched


def collect_crate_excerpts(question: str, keywords: list[str], crates: list[str], args):
    """Rank excerpts from the docs.rs pages of the named crates."""
    if not crates:
        return []

    cache_dir = Path(args.cache_dir).expanduser()
    session = requests.Session()
    session.headers.update({"User-Agent": USER_AGENT})

    print(
        "Code-sample question detected; searching docs.rs for: "
        + ", ".join(crates),
        file=sys.stderr,
    )

    collected = []
    for crate in crates:
        page = get_crate_page(
            session,
            crate,
            cache_dir,
            refresh=args.refresh,
            offline=args.offline,
        )
        if page is None:
            continue
        crate_chunks = chunk_pages([page])
        if not crate_chunks:
            print(f"Warning: no usable text in docs.rs/{crate}.", file=sys.stderr)
            continue
        picked = rank_chunks(
            question,
            keywords,
            crate_chunks,
            args.crate_top_k,
            per_page_limit=max(args.crate_top_k, CRATE_CHUNKS_PER_PAGE),
            prefer_code=True,
        )
        print(f"docs.rs/{crate}: added {len(picked)} excerpt(s)", file=sys.stderr)
        collected.extend(picked)
    return collected


def tokenize(text: str) -> list[str]:
    return [
        token
        for token in re.findall(r"[a-z0-9_]+", text.lower())
        if len(token) > 1 and token not in STOPWORDS
    ]


def chunk_pages(pages: list[dict]) -> list[dict]:
    chunks = []
    for page in pages:
        current: list[str] = []
        size = 0
        in_code = False

        def flush():
            nonlocal current, size
            body = "\n".join(current).strip()
            if len(body) >= 60:
                chunks.append(
                    {
                        "url": page["url"],
                        "title": page["title"],
                        "text": body,
                    }
                )
            current = []
            size = 0

        for line in page["text"].split("\n"):
            stripped = line.strip()
            if in_code:
                # Keep code-sample lines intact (including indentation and
                # blank lines between statements).
                piece = line.rstrip()
                if piece and current and size + len(piece) > CHUNK_TARGET_CHARS:
                    flush()
                current.append(piece)
                size += len(piece) + 1
                if stripped.startswith("```"):
                    in_code = False
                continue
            if stripped.startswith("```"):
                in_code = True
                if current and size + len(stripped) > CHUNK_TARGET_CHARS:
                    flush()
                current.append(stripped)
                size += len(stripped) + 1
                continue
            paragraph = stripped
            if not paragraph:
                continue
            for piece in _split_long(paragraph, CHUNK_MAX_CHARS):
                if current and size + len(piece) > CHUNK_TARGET_CHARS:
                    flush()
                current.append(piece)
                size += len(piece) + 1
        flush()
    return chunks


def _split_long(text: str, limit: int) -> list[str]:
    if len(text) <= limit:
        return [text]
    pieces = []
    while text:
        cut = text.rfind(" ", 0, limit)
        if cut <= 0:
            cut = limit
        pieces.append(text[:cut].strip())
        text = text[cut:].strip()
    return [piece for piece in pieces if piece]


def normalize_crate(name: str) -> str | None:
    """Return a clean crate name, or None when it is not a valid one."""
    name = str(name).strip().strip("\"'`")
    name = re.sub(r"^(?:https?://)?docs\.rs/", "", name).strip("/")
    name = name.split("/", 1)[0]
    if not name or ".." in name:
        return None
    if not CRATE_NAME_RE.fullmatch(name):
        return None
    if (
        name.lower() in RESERVED_CRATES
        or name.lower() in STOPWORDS
        or name.lower() in NON_CRATE_WORDS
        or name.lower() in STD_ITEM_NAMES
    ):
        return None
    return name


def _backtick_crates(text: str) -> list[str]:
    found = []
    for content in BACKTICK_RE.findall(text):
        content = content.strip()
        first = re.split(r"::|\s|\(|\{|\[", content, maxsplit=1)[0]
        first = first.strip()
        if not CRATE_NAME_RE.fullmatch(first or ""):
            continue
        # A bare backticked type such as `Vec<T>` is not a crate. Accept a
        # known crate, or an unknown crate-looking name used in a Rust path.
        if first.lower() in KNOWN_CRATES or (
            "::" in content and any(char in first for char in "_-")
        ):
            found.append(first)
    return found


def guess_crates(question: str) -> list[str]:
    """Fallback crate detection from the question text."""
    found: list[str] = []
    found.extend(CRATE_URL_RE.findall(question))
    found.extend(CRATE_PHRASE_RE.findall(question))
    for name in USE_DECL_RE.findall(question):
        if name.lower() in KNOWN_CRATES or "_" in name or "-" in name:
            found.append(name)
    for name in EXTERN_CRATE_RE.findall(question):
        if name.lower() in KNOWN_CRATES or "_" in name or "-" in name:
            found.append(name)
    found.extend(_backtick_crates(question))

    lowered = question.lower()
    explicit = bool(
        CRATE_PHRASE_RE.search(question)
        or CRATE_URL_RE.search(question)
        or USE_DECL_RE.search(question)
        or EXTERN_CRATE_RE.search(question)
        or BACKTICK_RE.search(question)
    )
    for crate in KNOWN_CRATES:
        pattern = r"(?<![\w-])" + re.escape(crate) + r"(?![\w-])"
        if re.search(pattern, lowered):
            if crate in AMBIGUOUS_CRATES and not explicit:
                continue
            found.append(crate)
    for match in QUALIFIED_PATH_RE.finditer(question):
        if match.group(1).lower() in KNOWN_CRATES:
            found.append(match.group(1))
    return found


def infer_crates(text: str) -> list[str]:
    """Infer crates from concept words in ``text``.

    Unlike Go's standard-library packages, third-party crates cannot be
    inferred from concept words alone. Only names that are known crates or
    that appear in explicit crate syntax are used.
    """
    scores: dict[str, int] = {}
    for crate in KNOWN_CRATES:
        if crate in AMBIGUOUS_CRATES:
            continue
        pattern = r"(?<![\w-])" + re.escape(crate) + r"(?![\w-])"
        if re.search(pattern, text, re.IGNORECASE):
            scores[crate] = 2
    ordered = sorted(scores, key=lambda crate: (-scores[crate], crate))
    return ordered


def crates_mentioned_in_context(
    question: str,
    keywords: list[str],
    selected,
    exclude=(),
) -> list[str]:
    """Discover crates referenced by the retrieved documentation."""
    excluded = {str(crate).lower() for crate in exclude}
    scores: dict[str, int] = {}

    def add(candidate: str, weight: int) -> None:
        crate = normalize_crate(candidate)
        if crate is None or crate.lower() in excluded:
            return
        scores[crate] = scores.get(crate, 0) + weight

    texts = [
        f"{chunk.get('title', '')}\n{chunk.get('text', '')}"
        for _score, chunk in selected
    ]
    context_text = "\n".join(texts)

    for match in CRATE_PHRASE_RE.finditer(context_text):
        name = match.group(1)
        # Documentation prose contains phrases such as "extern crate" and
        # "the generated crate". Only trust a context phrase when it names a
        # known crate or has a crate-like spelling; the model and the question
        # itself handle unknown single-word crates.
        if (
            name.lower() in KNOWN_CRATES
            or "_" in name
            or "-" in name
        ):
            add(name, 4)
    for match in CRATE_URL_RE.finditer(context_text):
        name = match.group(1)
        # docs.rs pages link to their dependencies. Only promote a linked
        # crate from the retrieved context when it names a known crate or has
        # a crate-like spelling, so dependencies do not become lookups.
        if (
            name.lower() in KNOWN_CRATES
            or "_" in name
            or "-" in name
        ):
            add(name, 4)
    for match in USE_DECL_RE.finditer(context_text):
        name = match.group(1)
        if (
            name.lower() in KNOWN_CRATES
            or "_" in name
            or "-" in name
        ):
            add(name, 2)
    for name in _backtick_crates(context_text):
        if name.lower() in KNOWN_CRATES:
            add(name, 2)

    query_text = " ".join([question, *keywords])
    for crate in guess_crates(query_text):
        add(crate, 3)
    for crate in infer_crates(query_text):
        add(crate, 2)

    ordered = sorted(scores, key=lambda crate: (-scores[crate], crate))
    return ordered


def candidate_crates(question: str) -> list[str]:
    """Return explicitly named or clearly implied crates."""
    return clean_crates(guess_crates(question) + infer_crates(question))


def clean_crates(candidates) -> list[str]:
    """Normalize, deduplicate, and cap a list of crate names."""
    cleaned: list[str] = []
    seen: set[str] = set()
    for crate in candidates if isinstance(candidates, list) else []:
        normalized = normalize_crate(crate)
        if normalized is None or normalized.lower() in seen:
            continue
        seen.add(normalized.lower())
        cleaned.append(normalized)
        if len(cleaned) >= MAX_CRATES:
            break
    return cleaned


def analyze_question(question: str, model: str):
    """Return (keywords, is_code_sample, crates) for the question."""
    payload = {
        "model": model,
        "messages": [
            {"role": "user", "content": KEYWORD_PROMPT.format(question=question)}
        ],
        "stream": False,
        "think": False,
        "format": "json",
        "options": {"temperature": 0, "num_predict": 250},
    }
    keywords = []
    code_sample = None
    crates = []
    try:
        response = requests.post(
            f"{OLLAMA_HOST}/api/chat", json=payload, timeout=(10, 180)
        )
        response.raise_for_status()
        content = response.json().get("message", {}).get("content", "")
        data = json.loads(content)
        keywords = data.get("keywords", [])
        code_sample = data.get("code_sample")
        crates = data.get("crates", [])
    except (requests.RequestException, ValueError, AttributeError) as exc:
        print(
            f"Warning: question analysis failed ({exc}); using the question.",
            file=sys.stderr,
        )

    cleaned = []
    for keyword in keywords if isinstance(keywords, list) else []:
        keyword = " ".join(str(keyword).split())
        if keyword and keyword.lower() not in {k.lower() for k in cleaned}:
            cleaned.append(keyword)
    if not cleaned:
        cleaned = tokenize(question) or [question]
    cleaned = cleaned[:8]

    is_code_sample = code_sample if isinstance(code_sample, bool) else False
    if CODE_SAMPLE_RE.search(question):
        is_code_sample = True

    cleaned_crates = clean_crates(crates)
    if not cleaned_crates:
        cleaned_crates = candidate_crates(question)

    # docs.rs is only consulted for code-sample questions that name a crate.
    if not is_code_sample:
        cleaned_crates = []
    return cleaned, is_code_sample, cleaned_crates


def rank_chunks(
    question: str,
    keywords: list[str],
    chunks: list[dict],
    top_k: int,
    per_page_limit: int = CHUNKS_PER_PAGE,
    prefer_code: bool = False,
):
    query_text = " ".join(keywords * 2) + " " + question
    query_tokens = tokenize(query_text)
    if not query_tokens:
        query_tokens = [question.lower()]

    tokenized = [tokenize(chunk["text"] + " " + chunk["title"]) for chunk in chunks]
    if BM25Okapi is not None:
        scores = list(BM25Okapi(tokenized).get_scores(query_tokens))
    else:  # simple fallback if rank-bm25 is unavailable
        query_counts = {}
        for token in query_tokens:
            query_counts[token] = query_counts.get(token, 0) + 1
        scores = []
        for tokens in tokenized:
            counts = {}
            for token in tokens:
                counts[token] = counts.get(token, 0) + 1
            scores.append(
                sum(
                    counts.get(token, 0) * weight
                    for token, weight in query_counts.items()
                )
            )

    boost_knowledge_scores(chunks, scores)

    phrase_query = " ".join(keywords).lower()
    if prefer_code:
        # Code-sample questions should surface the documentation pages that
        # actually contain examples, especially rust-by-example and the
        # crate-level docs on docs.rs. Use an additive bonus so a negative
        # BM25 score is never amplified in the wrong direction.
        top_score = max(scores) if scores else 0.0
        bonus = max(0.001, top_score * 0.25)
        for index, chunk in enumerate(chunks):
            if "```" in chunk["text"]:
                scores[index] += bonus
    order = sorted(range(len(chunks)), key=lambda i: scores[i], reverse=True)
    selected = []
    per_page: dict[str, int] = {}
    seen_text = set()

    for index in order:
        if len(selected) >= top_k:
            break
        score = scores[index]
        if score <= 0:
            continue
        chunk = chunks[index]
        if (
            not chunk.get("knowledge")
            and per_page.get(chunk["url"], 0) >= per_page_limit
        ):
            continue
        fingerprint = chunk["text"][:160]
        if fingerprint in seen_text:
            continue
        seen_text.add(fingerprint)
        per_page[chunk["url"]] = per_page.get(chunk["url"], 0) + 1
        selected.append((score, chunk))

    # Phrase matches are a useful tie-breaker for exact Rust terminology.
    if phrase_query and len(selected) < top_k:
        for index in order:
            if len(selected) >= top_k:
                break
            chunk = chunks[index]
            if any(chunk is item[1] for item in selected):
                continue
            if phrase_query in chunk["text"].lower():
                selected.append((scores[index], chunk))

    return selected


def build_context(selected) -> str:
    parts = []
    for number, (_score, chunk) in enumerate(selected, start=1):
        parts.append(
            f"[{number}] Source: {chunk['url']}\n"
            f"Title: {chunk['title']}\n"
            f"{chunk['text']}"
        )
    return "\n\n".join(parts)


def stream_answer(
    question: str,
    context: str,
    model: str,
    *,
    code_sample: bool = False,
) -> str:
    prompt = ANSWER_PROMPT.format(question=question, context=context)
    messages = [{"role": "user", "content": prompt}]
    answer_parts = []
    model_parts = []
    caveat_filter = DocumentationCaveatFilter(enabled=code_sample)
    fence_filter = CodeFenceRepairFilter()

    def emit(piece: str) -> None:
        cleaned = caveat_filter.feed(piece)
        if cleaned:
            cleaned = fence_filter.feed(cleaned)
            if cleaned:
                answer_parts.append(cleaned)
                print(cleaned, end="", flush=True)

    def stream_once(continuation: bool) -> str:
        payload = {
            "model": model,
            "messages": messages,
            "stream": True,
            "think": False,
            "options": {
                "temperature": 0,
                "num_predict": ANSWER_NUM_PREDICT,
            },
        }
        done_reason = ""
        raw_parts: list[str] = []
        accepted_parts: list[str] = []
        line_buffer = ""

        def accept(text: str, emit_now: bool | None = None) -> None:
            accepted_parts.append(text)
            if emit_now is None:
                emit_now = not continuation
            if emit_now:
                emit(text)

        try:
            with requests.post(
                f"{OLLAMA_HOST}/api/chat",
                json=payload,
                stream=True,
                timeout=(10, 300),
            ) as response:
                response.raise_for_status()
                for line in response.iter_lines(decode_unicode=True):
                    if not line:
                        continue
                    try:
                        event = json.loads(line)
                    except ValueError:
                        continue
                    if event.get("error"):
                        raise RuntimeError(event["error"])
                    message = event.get("message") or {}
                    piece = message.get("content") or ""
                    if piece:
                        raw_parts.append(piece)
                        line_buffer += piece
                        complete, line_buffer = split_complete_lines(
                            line_buffer
                        )
                        if complete and not continuation:
                            accept(complete)
                    if event.get("done"):
                        done_reason = str(
                            event.get("done_reason") or ""
                        )
        except requests.RequestException as exc:
            sys.exit(f"\nOllama request failed: {exc}")
        except RuntimeError as exc:
            sys.exit(f"\nOllama error: {exc}")

        if not continuation:
            if done_reason == "length":
                # A partial line at a prediction cutoff is where models
                # invent invalid completions. Leave it out of the answer and
                # out of the assistant history so the continuation starts on
                # a clean line.
                line_buffer = ""
            elif line_buffer:
                accept(line_buffer)
                line_buffer = ""
        else:
            # Buffer a continuation so a repeated block can be removed before
            # any of it reaches stdout. A truncated continuation is also
            # stopped at its last complete line.
            raw = "".join(raw_parts)
            if done_reason == "length":
                raw, _partial = split_complete_lines(raw)
            raw = strip_repeated_continuation_prefix(
                "".join(model_parts), raw
            )
            if raw:
                accept(raw, emit_now=True)

        accepted = "".join(accepted_parts)
        if accepted:
            model_parts.append(accepted)
        return done_reason

    for attempt in range(MAX_ANSWER_CONTINUATIONS + 1):
        done_reason = stream_once(continuation=attempt > 0)
        if done_reason != "length":
            break
        if attempt >= MAX_ANSWER_CONTINUATIONS:
            break
        # The model hit the per-request prediction limit in the middle of
        # the answer. Keep the partial reply in the conversation and ask it
        # to resume, so a long code sample is not returned half-written.
        messages = [
            {"role": "user", "content": prompt},
            {"role": "assistant", "content": "".join(model_parts)},
            {"role": "user", "content": ANSWER_CONTINUATION_PROMPT},
        ]

    tail = caveat_filter.flush()
    if tail:
        tail = fence_filter.feed(tail)
        if tail:
            answer_parts.append(tail)
            print(tail, end="", flush=True)
    fence_tail = fence_filter.flush()
    if fence_tail:
        answer_parts.append(fence_tail)
        print(fence_tail, end="", flush=True)

    answer = "".join(answer_parts).strip()
    closed = close_unclosed_code_fence(answer)
    if closed != answer:
        # Keep stdout (captured by app/chat_server.py) in sync with the
        # returned answer so the Sources list is not swallowed by the fence.
        print(closed[len(answer):], end="", flush=True)
        answer = closed
    if answer:
        print()
    return answer


def print_sources(answer: str, selected):
    referenced = {
        int(number)
        for number in re.findall(r"\[(\d+)\]", answer)
        if 1 <= int(number) <= len(selected)
    }
    indexes = sorted(referenced) if referenced else list(range(1, len(selected) + 1))
    if not indexes:
        return
    print("\nSources:")
    for number in indexes:
        chunk = selected[number - 1][1]
        title = chunk["title"] or chunk["url"]
        print(f"  [{number}] {title}\n      {chunk['url']}")


def main(argv=None):
    parser = argparse.ArgumentParser(
        description=(
            "Answer a question using the official Rust documentation at "
            "https://doc.rust-lang.org/ and, for code-sample questions that "
            "name a crate, https://docs.rs/{crate}."
        )
    )
    parser.add_argument("question", nargs="+", help="the Rust question to answer")
    parser.add_argument(
        "--model",
        default=MODEL,
        help=f"Ollama model used for keywords and answers (default: {MODEL})",
    )
    parser.add_argument(
        "--top-k",
        type=int,
        default=DEFAULT_TOP_K,
        help=(
            "number of documentation excerpts to use "
            f"(default: {DEFAULT_TOP_K})"
        ),
    )
    parser.add_argument(
        "--crate-top-k",
        type=int,
        default=DEFAULT_CRATE_TOP_K,
        help=(
            "number of excerpts per docs.rs crate page for code-sample "
            f"questions (default: {DEFAULT_CRATE_TOP_K})"
        ),
    )
    parser.add_argument(
        "--max-pages",
        type=int,
        default=DEFAULT_MAX_PAGES,
        help=(
            "maximum pages to crawl under doc.rust-lang.org "
            f"(default: {DEFAULT_MAX_PAGES})"
        ),
    )
    parser.add_argument(
        "--cache-dir",
        default=str(DEFAULT_CACHE_DIR),
        help=f"index cache directory (default: {DEFAULT_CACHE_DIR})",
    )
    parser.add_argument(
        "--refresh",
        action="store_true",
        help="force a fresh crawl of https://doc.rust-lang.org/",
    )
    parser.add_argument(
        "--offline",
        action="store_true",
        help=(
            "use cached doc.rust-lang.org and docs.rs pages only; "
            "do not crawl"
        ),
    )
    parser.add_argument(
        "--keywords",
        help="comma-separated keywords, skipping the model keyword extraction",
    )
    parser.add_argument(
        "--crates",
        help=(
            "comma-separated crate names for a code-sample question, "
            "skipping the model crate extraction"
        ),
    )
    args = parser.parse_args(argv)

    if args.top_k < 1:
        parser.error("--top-k must be at least 1")
    if args.crate_top_k < 1:
        parser.error("--crate-top-k must be at least 1")
    if args.max_pages < 1:
        parser.error("--max-pages must be at least 1")

    question = " ".join(args.question).strip()
    if not question:
        parser.error("question must not be empty")

    pages = get_pages(args)
    chunks = chunk_pages(pages)
    # Curated notes in ./knowledge/rust are an addition to the official
    # documentation: they are chunked and ranked with the crawled docs.
    chunks.extend(knowledge_chunks("rust", chunk_pages))
    if not chunks:
        sys.exit("The documentation index is empty; run with --refresh.")

    if args.keywords:
        keywords = [
            keyword.strip()
            for keyword in args.keywords.split(",")
            if keyword.strip()
        ]
        is_code_sample = bool(CODE_SAMPLE_RE.search(question))
        if args.crates:
            crates = clean_crates(
                [crate.strip() for crate in args.crates.split(",") if crate.strip()]
            )
        else:
            crates = candidate_crates(question) if is_code_sample else []
    else:
        print("Analyzing question...", file=sys.stderr)
        keywords, is_code_sample, crates = analyze_question(question, args.model)
    print(f"Search keywords: {', '.join(keywords)}", file=sys.stderr)

    selected = rank_chunks(
        question, keywords, chunks, args.top_k, prefer_code=is_code_sample
    )

    crates_to_fetch: list[str] = []
    if is_code_sample:
        # Documentation excerpts often name the crate that implements a
        # concept even when the question only mentions the concept. Discover
        # and fetch those docs.rs pages in addition to crates named in the
        # question.
        context_crates = crates_mentioned_in_context(
            question, keywords, selected, exclude=crates
        )
        if context_crates:
            print(
                "Documentation context references Rust crates: "
                + ", ".join(context_crates),
                file=sys.stderr,
            )
        crates_to_fetch = clean_crates(crates + context_crates)

    if crates_to_fetch:
        crate_selected = collect_crate_excerpts(
            question, keywords, crates_to_fetch, args
        )
        seen_text = {chunk["text"][:160] for _score, chunk in selected}
        for score, chunk in crate_selected:
            fingerprint = chunk["text"][:160]
            if fingerprint in seen_text:
                continue
            seen_text.add(fingerprint)
            selected.append((score, chunk))

    if not selected:
        if not is_code_sample:
            sys.exit(
                "No matching documentation was found under "
                "https://doc.rust-lang.org/ or https://docs.rs/."
            )
        # A code request must still get working code. The answer prompt
        # allows the model to fill gaps from general Rust knowledge, so pass
        # an empty context instead of refusing because the index had no match.
        print(
            "No matching Rust documentation excerpts; generating code from "
            "general Rust knowledge.",
            file=sys.stderr,
        )

    context = build_context(selected)
    answer = stream_answer(
        question, context, args.model, code_sample=is_code_sample
    )
    if not answer:
        sys.exit("The model returned an empty answer.")
    print_sources(answer, selected)


if __name__ == "__main__":
    main()
