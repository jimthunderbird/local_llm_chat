#!/usr/bin/env python3
"""Answer Go questions using only the official Go documentation.

Usage:
    python3 golang_doc_search.py <question>
    python3 golang_doc_search.py "how do I handle errors in Go?"
    python3 golang_doc_search.py --refresh "what is a goroutine?"

Source of truth:
    https://go.dev/doc/ (and the pages linked below it)

How it works:
    1. Crawl https://go.dev/doc/ once and cache the page text locally
       (default: ~/.cache/golang_doc_search/index.json).
    2. Ask the local LFM3 model (thinking disabled) to turn the question
       into search keywords, and to detect whether the question asks for a
       code sample and which Go import paths it names.
    3. Rank cached documentation chunks with BM25 using those keywords.
    4. If the question asks for a code sample, also inspect the ranking
       excerpts and the question for Go package references. This catches
       concepts (for example "iterator" -> "the iter package") and explicit
       mentions, then fetches and ranks https://pkg.go.dev/{package} for each
       related package. Package pages are cached under the same cache
       directory.
    5. Ask the local LFM3 model (thinking disabled) to answer strictly from the best
       matching excerpts, citing the go.dev/doc/ and pkg.go.dev/ URLs used.

The index is refreshed automatically when it is older than seven days. Use
--refresh to force a refresh, or --offline to search a cached index without
touching the network.

Only pages under https://go.dev/doc/ and the explicitly requested
https://pkg.go.dev/{package} pages are used; no other source is used.
"""

from __future__ import annotations

import argparse
import json
import os
import re
import sys
import time
from collections import deque
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


DOC_ROOT = "https://go.dev/doc/"
DOC_HOST = "go.dev"
DOC_PATH_PREFIX = "/doc/"
PKG_ROOT = "https://pkg.go.dev/"
MODEL = os.environ.get(
    "OLLAMA_LFM_MODEL", "milodule3/LFM3-1.7B-aura-grounded:latest"
)

DEFAULT_CACHE_DIR = Path(
    os.environ.get("GOLANG_DOC_SEARCH_CACHE", "~/.cache/golang_doc_search")
).expanduser()
CACHE_VERSION = 2
PKG_CACHE_VERSION = 1
CACHE_MAX_AGE_SECONDS = 7 * 24 * 60 * 60

DEFAULT_MAX_PAGES = 250
CRAWL_WORKERS = 4
REQUEST_TIMEOUT = (10, 30)
USER_AGENT = "golang-doc-search/1.0 (+local documentation search)"

CHUNK_TARGET_CHARS = 1100
CHUNK_MAX_CHARS = 1800
CHUNKS_PER_PAGE = 2
DEFAULT_TOP_K = 8
DEFAULT_PKG_TOP_K = 2
MAX_PACKAGES = 3

OLLAMA_HOST = os.environ.get("OLLAMA_HOST", "http://localhost:11434").rstrip("/")
if not OLLAMA_HOST.startswith(("http://", "https://")):
    OLLAMA_HOST = f"http://{OLLAMA_HOST}"

STOPWORDS = {
    "a", "about", "after", "all", "also", "an", "and", "any", "are", "as",
    "at", "be", "because", "been", "before", "but", "by", "can", "could",
    "do", "does", "doing", "for", "from", "get", "go", "golang", "has",
    "have", "he", "her", "here", "him", "his", "how", "i", "if", "in",
    "into", "is", "it", "its", "just", "me", "my", "of", "on", "or",
    "our", "should", "so", "some", "tell", "than", "that", "the", "their",
    "them", "then", "there", "these", "they", "this", "to", "was", "we",
    "were", "what", "when", "where", "which", "who", "why", "will", "with",
    "would", "you", "your",
}

KEYWORD_PROMPT = """You turn Go programming questions into search keywords for
the official Go documentation at https://go.dev/doc/.

Question: {question}

Return a JSON object of exactly this shape:
{{"keywords": ["keyword or short phrase", "..."], "code_sample": false, "packages": ["import path", "..."]}}

Rules:
- Return 3 to 8 keywords or short phrases.
- Include exact Go identifiers, package names, functions, types, and concepts
  that would appear in Go documentation (for example: goroutine, defer,
  context cancellation, errors.Is, sync.WaitGroup).
- Prefer terminology used by the Go docs over general wording.
- Set "code_sample" to true when the question asks for example code, a
  snippet, a usage demonstration, or an implementation (for example: "show me
  code for ...", "how do I use ...", "give an example of ..."). Otherwise set
  it to false.
- Set "packages" to Go import paths that are explicitly mentioned in the
  question (for example: net/http, encoding/json,
  github.com/gin-gonic/gin). Use an empty list when the question does not name
  any package. Never guess a package.
- Do not include explanations, URLs, or markdown.
"""

ANSWER_PROMPT = """You are a Go documentation assistant. Use the numbered
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
  explanation or code. Still write a complete, runnable Go program or function
  that answers the question. Use the APIs and patterns shown in the excerpts
  where they are relevant, and fill any remaining gaps with your own general
  Go knowledge.
- When you add code that is not directly supported by an excerpt, do not cite
  an excerpt for it, and do not mention that the indexed docs were missing
  the answer at all.
- If the excerpts mention a specific Go API for the task (for example
  sort.Search), use that API in the working code instead of switching to a
  different package. Use only the Go standard library and the packages the
  excerpts explicitly show; never add golang.org/x/... or other third-party
  imports unless an excerpt uses them.
- For non-code questions, if the excerpts genuinely do not contain the
  answer, say clearly that the indexed Go documentation (go.dev/doc/ and
  pkg.go.dev) does not contain this information.
- Cite the excerpt number for each factual claim taken from an excerpt, for
  example: "Use errors.Is [2]."
- Prefer precise, practical answers. Include short Go code examples when the
  documentation provides them.
- Never invent APIs, functions, or behavior that do not exist in Go. Any
  generated code must use real standard-library or documented packages.
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
# skip nav/header/footer here: go.dev has nested, sometimes unclosed nav tags
# that would otherwise swallow the rest of the page.
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
    r"should|would)\s+(?:i|we|you)\s+(?:use|write|implement|call))\b",
    re.IGNORECASE,
)
# Domain-qualified import paths such as github.com/gin-gonic/gin.
IMPORT_PATH_RE = re.compile(
    r"\b[a-z0-9][a-z0-9._\-]*\.[a-z]{2,}/[A-Za-z0-9_./\-]+"
)
# Standard-library import paths commonly named in code-sample questions. Used
# as a fallback when the keyword model does not return any packages.
COMMON_STDLIB_PACKAGES = {
    "archive/tar", "archive/zip", "bufio", "bytes", "cmp",
    "compress/gzip", "container/heap", "container/list", "context",
    "crypto/rand", "crypto/tls", "database/sql", "encoding/binary",
    "encoding/csv", "encoding/json", "encoding/xml", "errors", "flag",
    "fmt", "go/ast", "go/parser", "go/token", "hash", "html/template",
    "io", "io/fs", "iter", "log", "log/slog", "maps", "math",
    "math/big", "math/rand", "mime", "net", "net/http", "net/netip",
    "net/url", "os", "os/exec", "path/filepath", "reflect", "regexp",
    "runtime", "slices", "sort", "strconv", "strings", "structs",
    "sync", "sync/atomic", "testing", "text/template", "time",
    "unicode", "unicode/utf8", "unique", "weak",
}
IMPORT_PATH_FULL_RE = re.compile(
    r"[A-Za-z0-9_.~\-]+(?:/[A-Za-z0-9_.~\-]+)*"
)
# Package names as they appear in prose, for example "the iter package" or
# "the new slices package". The captured group may be a full import path.
PACKAGE_PHRASE_RE = re.compile(
    r"\b(?:the\s+|new\s+|new\s+the\s+)?"
    r"((?:[a-z][a-z0-9_.~\-]*/)*[a-z][a-z0-9_.~\-]*)\s+package\b",
    re.IGNORECASE,
)
# Explicit pkg.go.dev references and import declarations in documentation.
PKG_DEV_URL_RE = re.compile(r"pkg\.go\.dev/((?:[A-Za-z0-9_.~\-]+/)*[A-Za-z0-9_.~\-]+)")
IMPORT_DECL_RE = re.compile(
    r"\bimport\s+(?:\([^)]*?\b)?(?:[A-Za-z_.][A-Za-z0-9_.]*\s+)?\"([^\"\n]+)\"",
    re.DOTALL,
)
# Qualified identifiers such as iter.Seq, sync.WaitGroup, or http.Client.
# The qualifier is checked against known standard-library package names.
QUALIFIED_IDENT_RE = re.compile(
    r"(?<![\w/.])((?:[a-z][a-z0-9_]*/)*[a-z][a-z0-9_]*)\.([A-Z][A-Za-z0-9_]*)\b"
)
STDLIB_LEAVES = {
    path.rsplit("/", 1)[-1] for path in COMMON_STDLIB_PACKAGES
}
# Short names are too ambiguous for concept inference (io, os, go, ...).
MIN_INFERRED_LEAF_LENGTH = 3


class DocHTMLParser(HTMLParser):
    """Extract readable text, the page title, and links from a go.dev page."""

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
    """Normalize whitespace while keeping paragraph boundaries."""
    text = text.replace("\r", "\n").replace("\u00a0", " ")
    lines = []
    for line in text.split("\n"):
        line = " ".join(line.split())
        if line:
            lines.append(line)
    return "\n".join(lines)


def canonical_url(url: str) -> str:
    # Query strings on go.dev/doc/ URLs are view parameters (for example the
    # codewalk fileprint links), not distinct documentation pages. Dropping
    # them avoids indexing the same content many times.
    url, _fragment = urldefrag(url)
    parsed = urlparse(url)
    return f"{parsed.scheme}://{parsed.netloc}{parsed.path}"


def is_allowed_doc_url(url: str) -> bool:
    parsed = urlparse(url)
    if parsed.scheme not in ("http", "https"):
        return False
    if parsed.netloc != DOC_HOST:
        return False
    path = parsed.path
    if path == "/doc" or path == "/doc/":
        return True
    if not path.startswith(DOC_PATH_PREFIX):
        return False
    if path.lower().endswith(SKIP_EXTENSIONS):
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
        link = canonical_url(urljoin(url, href))
        if is_allowed_doc_url(link):
            links.append(link)

    return {
        "url": canonical_url(url),
        "title": page.title or url,
        "text": text,
        "links": sorted(set(links)),
    }


def crawl_docs(max_pages: int) -> list[dict]:
    """Breadth-first crawl of the /doc/ subtree, with a small worker pool."""
    start = canonical_url(DOC_ROOT)
    seen = {start}
    pending = deque([start])
    pages: dict[str, dict] = {}

    session = requests.Session()
    session.headers.update({"User-Agent": USER_AGENT})

    with ThreadPoolExecutor(max_workers=CRAWL_WORKERS) as pool:
        while pending and len(pages) < max_pages:
            batch = []
            while pending and len(batch) < CRAWL_WORKERS:
                batch.append(pending.popleft())

            futures = [pool.submit(fetch_page, session, url) for url in batch]
            for future in futures:
                page = future.result()
                if page is None:
                    continue
                pages.setdefault(page["url"], page)
                for link in page["links"]:
                    if link not in seen and len(seen) < max_pages * 8:
                        seen.add(link)
                        pending.append(link)
                if len(pages) >= max_pages:
                    break

    return list(pages.values())


def cache_path(cache_dir: Path) -> Path:
    return cache_dir / "index.json"


def package_cache_path(cache_dir: Path, package: str) -> Path:
    safe = re.sub(r"[^A-Za-z0-9_.\-]+", "__", package).strip("._")
    return cache_dir / "packages" / f"{safe}.json"


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
            sys.exit(f"No cached documentation index at {path}. Run without --offline first.")
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

    print(f"Indexing {DOC_ROOT} (this happens only occasionally)...", file=sys.stderr)
    try:
        pages = crawl_docs(args.max_pages)
    except Exception as exc:
        print(f"Warning: documentation crawl failed: {exc}", file=sys.stderr)
        pages = []

    if not pages:
        if cached is not None:
            print("Warning: using stale cached index instead.", file=sys.stderr)
            return cached["pages"]
        sys.exit("Could not index https://go.dev/doc/. Check your network connection.")

    try:
        save_cache(path, pages)
        print(f"Indexed {len(pages)} pages into {path}", file=sys.stderr)
    except OSError as exc:
        print(f"Warning: could not save cache: {exc}", file=sys.stderr)
    return pages


def load_package_cache(path: Path):
    try:
        with path.open("r", encoding="utf-8") as handle:
            data = json.load(handle)
    except (OSError, ValueError):
        return None
    if not isinstance(data, dict) or data.get("version") != PKG_CACHE_VERSION:
        return None
    if not isinstance(data.get("text"), str) or not data["text"]:
        return None
    return data


def save_package_cache(path: Path, page: dict):
    path.parent.mkdir(parents=True, exist_ok=True)
    payload = {
        "version": PKG_CACHE_VERSION,
        "fetched_at": time.time(),
        "url": page["url"],
        "title": page["title"],
        "text": page["text"],
    }
    temporary = path.with_suffix(path.suffix + ".tmp")
    with temporary.open("w", encoding="utf-8") as handle:
        json.dump(payload, handle, ensure_ascii=False, separators=(",", ":"))
    temporary.replace(path)


def get_package_page(
    session: requests.Session,
    package: str,
    cache_dir: Path,
    refresh: bool = False,
    offline: bool = False,
) -> dict | None:
    """Fetch (or load from cache) https://pkg.go.dev/{package}."""
    path = package_cache_path(cache_dir, package)
    cached = load_package_cache(path)
    if cached is not None:
        page = {
            "url": cached.get("url") or urljoin(PKG_ROOT, package),
            "title": cached.get("title") or package,
            "text": cached["text"],
            "links": [],
        }
    else:
        page = None

    if offline:
        if page is None:
            print(
                f"Warning: no cached pkg.go.dev page for {package}; skipping.",
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

    url = urljoin(PKG_ROOT, package)
    print(f"Fetching {url} ...", file=sys.stderr)
    fetched = fetch_page(session, url)
    if fetched is None:
        if page is not None:
            print(
                f"Warning: using stale cached pkg.go.dev page for {package}.",
                file=sys.stderr,
            )
            return page
        print(f"Warning: could not fetch pkg.go.dev page for {package}; skipping.", file=sys.stderr)
        return None

    try:
        save_package_cache(path, fetched)
    except OSError as exc:
        print(f"Warning: could not save pkg.go.dev cache for {package}: {exc}", file=sys.stderr)
    return fetched


def collect_package_excerpts(question: str, keywords: list[str], packages: list[str], args):
    """Rank excerpts from the pkg.go.dev pages of the named packages."""
    if not packages:
        return []

    cache_dir = Path(args.cache_dir).expanduser()
    session = requests.Session()
    session.headers.update({"User-Agent": USER_AGENT})

    print(
        "Code-sample question detected; searching pkg.go.dev for: "
        + ", ".join(packages),
        file=sys.stderr,
    )

    collected = []
    for package in packages:
        page = get_package_page(
            session,
            package,
            cache_dir,
            refresh=args.refresh,
            offline=args.offline,
        )
        if page is None:
            continue
        package_chunks = chunk_pages([page])
        if not package_chunks:
            print(f"Warning: no usable text in pkg.go.dev/{package}.", file=sys.stderr)
            continue
        picked = rank_chunks(question, keywords, package_chunks, args.pkg_top_k)
        print(
            f"pkg.go.dev/{package}: added {len(picked)} excerpt(s)",
            file=sys.stderr,
        )
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

        def flush():
            nonlocal current, size
            body = " ".join(current).strip()
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

        for paragraph in page["text"].split("\n"):
            paragraph = paragraph.strip()
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


def normalize_package(name: str) -> str | None:
    """Return a clean Go import path, or None when it is not a valid one."""
    name = str(name).strip().strip("\"'`")
    name = re.sub(r"^(?:https?://)?pkg\.go\.dev/", "", name).strip("/")
    if not name or ".." in name:
        return None
    if not IMPORT_PATH_FULL_RE.fullmatch(name):
        return None
    return name


def guess_packages(question: str) -> list[str]:
    """Fallback package detection from the question text."""
    found: list[str] = []
    lowered = question.lower()
    for package in COMMON_STDLIB_PACKAGES:
        pattern = r"(?<![\w/])" + re.escape(package) + r"(?![\w/])"
        if re.search(pattern, lowered):
            found.append(package)
    for match in IMPORT_PATH_RE.findall(question):
        found.append(match)
    return found


def infer_packages(text: str) -> list[str]:
    """Infer standard-library packages from concept words in ``text``.

    Go documentation often calls a concept by a word that is not the import
    path itself, for example "iterator" for the ``iter`` package or
    "slices" for ``slices``. Treating a package as related when its name is
    the stem of a concept word (or its plural) keeps pkg.go.dev lookup
    generic instead of maintaining a hand-written alias table.
    """
    tokens = set(tokenize(text))
    scores: dict[str, int] = {}
    for package in COMMON_STDLIB_PACKAGES:
        leaf = package.rsplit("/", 1)[-1]
        if len(leaf) < MIN_INFERRED_LEAF_LENGTH:
            continue
        for token in tokens:
            if token == leaf:
                score = 2
            elif token.startswith(leaf) and len(token) > len(leaf):
                # iterator -> iter, sorting -> sort, marshalling -> marshal
                score = 1
            elif leaf == token + "s":
                # slice -> slices, map -> maps, string -> strings
                score = 1
            else:
                continue
            scores[package] = max(scores.get(package, 0), score)
    ordered = sorted(
        scores, key=lambda package: (-scores[package], package.count("/"), package)
    )
    return ordered


def packages_mentioned_in_context(
    question: str,
    keywords: list[str],
    selected,
    exclude=(),
) -> list[str]:
    """Discover Go packages referenced by the retrieved documentation.

    The question may ask about a concept without naming its package (for
    example "show me a code sample of iterator"). The selected go.dev/doc
    excerpts usually name the package ("the iter package", ``iter.Seq``,
    ``import "iter"``, or a pkg.go.dev link). Ranking those mentions lets the
    script fetch the matching pkg.go.dev pages without hard-coding aliases.
    """
    excluded = {str(package).lower() for package in exclude}
    scores: dict[str, int] = {}

    def add(candidate: str, weight: int) -> None:
        package = normalize_package(candidate)
        if package is None or package.lower() in excluded:
            return
        leaf = package.rsplit("/", 1)[-1]
        is_known_stdlib = package in COMMON_STDLIB_PACKAGES or leaf in STDLIB_LEAVES
        is_module_path = "/" in package and "." in package.split("/", 1)[0]
        if not (is_known_stdlib or is_module_path):
            return
        scores[package] = scores.get(package, 0) + weight

    texts = [
        f"{chunk.get('title', '')}\n{chunk.get('text', '')}"
        for _score, chunk in selected
    ]
    context_text = "\n".join(texts)

    for match in PACKAGE_PHRASE_RE.finditer(context_text):
        add(match.group(1), 4)
    for match in PKG_DEV_URL_RE.finditer(context_text):
        add(match.group(1), 4)
    for match in IMPORT_DECL_RE.finditer(context_text):
        add(match.group(1), 4)
    for match in QUALIFIED_IDENT_RE.finditer(context_text):
        qualifier = match.group(1)
        # Only treat the qualifier as a package when it is a known package
        # name; otherwise it is likely a local variable or receiver.
        if qualifier.rsplit("/", 1)[-1] in STDLIB_LEAVES:
            add(qualifier, 1)

    query_text = " ".join([question, *keywords])
    query_tokens = set(tokenize(query_text))
    for package in infer_packages(query_text):
        add(package, 3)

    for package in list(scores):
        leaf = package.rsplit("/", 1)[-1]
        if leaf in query_tokens:
            scores[package] += 4
        elif any(token.startswith(leaf) for token in query_tokens):
            scores[package] += 2

    ordered = sorted(scores, key=lambda package: (-scores[package], package))
    return ordered


def candidate_packages(question: str) -> list[str]:
    """Return explicitly named or clearly implied Go packages."""
    return clean_packages(guess_packages(question) + infer_packages(question))


def clean_packages(candidates) -> list[str]:
    """Normalize, deduplicate, and cap a list of package names."""
    cleaned: list[str] = []
    seen: set[str] = set()
    for package in candidates if isinstance(candidates, list) else []:
        normalized = normalize_package(package)
        if normalized is None or normalized.lower() in seen:
            continue
        seen.add(normalized.lower())
        cleaned.append(normalized)
        if len(cleaned) >= MAX_PACKAGES:
            break
    return cleaned


def analyze_question(question: str, model: str):
    """Return (keywords, is_code_sample, packages) for the question."""
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
    packages = []
    try:
        response = requests.post(
            f"{OLLAMA_HOST}/api/chat", json=payload, timeout=(10, 180)
        )
        response.raise_for_status()
        content = response.json().get("message", {}).get("content", "")
        data = json.loads(content)
        keywords = data.get("keywords", [])
        code_sample = data.get("code_sample")
        packages = data.get("packages", [])
    except (requests.RequestException, ValueError, AttributeError) as exc:
        print(f"Warning: question analysis failed ({exc}); using the question.", file=sys.stderr)

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

    cleaned_packages = clean_packages(packages)
    if not cleaned_packages:
        # The question may imply a package without naming its import path
        # (for example "iterator" implies "iter").
        cleaned_packages = candidate_packages(question)

    # pkg.go.dev is only consulted for code-sample questions that explicitly
    # mention a package.
    if not is_code_sample:
        cleaned_packages = []
    return cleaned, is_code_sample, cleaned_packages


def rank_chunks(question: str, keywords: list[str], chunks: list[dict], top_k: int):
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
                sum(counts.get(token, 0) * weight for token, weight in query_counts.items())
            )

    boost_knowledge_scores(chunks, scores)

    phrase_query = " ".join(keywords).lower()
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
            and per_page.get(chunk["url"], 0) >= CHUNKS_PER_PAGE
        ):
            continue
        fingerprint = chunk["text"][:160]
        if fingerprint in seen_text:
            continue
        seen_text.add(fingerprint)
        per_page[chunk["url"]] = per_page.get(chunk["url"], 0) + 1
        selected.append((score, chunk))

    # Phrase matches are a useful tie-breaker for exact Go terminology.
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
            "Answer a question using the official Go documentation at "
            "https://go.dev/doc/ and, for code-sample questions that name a "
            "package, https://pkg.go.dev/{package}."
        )
    )
    parser.add_argument("question", nargs="+", help="the Go question to answer")
    parser.add_argument(
        "--model",
        default=MODEL,
        help=f"Ollama model used for keywords and answers (default: {MODEL})",
    )
    parser.add_argument(
        "--top-k",
        type=int,
        default=DEFAULT_TOP_K,
        help=f"number of documentation excerpts to use (default: {DEFAULT_TOP_K})",
    )
    parser.add_argument(
        "--pkg-top-k",
        type=int,
        default=DEFAULT_PKG_TOP_K,
        help=(
            "number of excerpts per pkg.go.dev package page for code-sample "
            f"questions (default: {DEFAULT_PKG_TOP_K})"
        ),
    )
    parser.add_argument(
        "--max-pages",
        type=int,
        default=DEFAULT_MAX_PAGES,
        help=f"maximum pages to crawl under /doc/ (default: {DEFAULT_MAX_PAGES})",
    )
    parser.add_argument(
        "--cache-dir",
        default=str(DEFAULT_CACHE_DIR),
        help=f"index cache directory (default: {DEFAULT_CACHE_DIR})",
    )
    parser.add_argument(
        "--refresh",
        action="store_true",
        help="force a fresh crawl of https://go.dev/doc/",
    )
    parser.add_argument(
        "--offline",
        action="store_true",
        help="use cached go.dev/doc/ and pkg.go.dev pages only; do not crawl",
    )
    parser.add_argument(
        "--keywords",
        help="comma-separated keywords, skipping the model keyword extraction",
    )
    args = parser.parse_args(argv)

    if args.top_k < 1:
        parser.error("--top-k must be at least 1")
    if args.pkg_top_k < 1:
        parser.error("--pkg-top-k must be at least 1")
    if args.max_pages < 1:
        parser.error("--max-pages must be at least 1")

    question = " ".join(args.question).strip()
    if not question:
        parser.error("question must not be empty")

    pages = get_pages(args)
    chunks = chunk_pages(pages)
    # Curated notes in ./knowledge/golang are an addition to the official Go
    # documentation: they are chunked and ranked with the crawled docs.
    chunks.extend(knowledge_chunks("golang", chunk_pages))
    if not chunks:
        sys.exit("The documentation index is empty; run with --refresh.")

    if args.keywords:
        keywords = [
            keyword.strip()
            for keyword in args.keywords.split(",")
            if keyword.strip()
        ]
        is_code_sample = bool(CODE_SAMPLE_RE.search(question))
        packages = candidate_packages(question) if is_code_sample else []
    else:
        print("Analyzing question...", file=sys.stderr)
        keywords, is_code_sample, packages = analyze_question(question, args.model)
    print(f"Search keywords: {', '.join(keywords)}", file=sys.stderr)

    selected = rank_chunks(question, keywords, chunks, args.top_k)

    packages_to_fetch: list[str] = []
    if is_code_sample:
        # Documentation excerpts often name the package that implements a
        # concept even when the question only uses the concept word (for
        # example "iterator" -> "the iter package"). Discover and fetch those
        # pkg.go.dev pages in addition to packages named in the question.
        context_packages = packages_mentioned_in_context(
            question, keywords, selected, exclude=packages
        )
        if context_packages:
            print(
                "Documentation context references Go packages: "
                + ", ".join(context_packages),
                file=sys.stderr,
            )
        packages_to_fetch = clean_packages(packages + context_packages)

    if packages_to_fetch:
        package_selected = collect_package_excerpts(
            question, keywords, packages_to_fetch, args
        )
        seen_text = {chunk["text"][:160] for _score, chunk in selected}
        for score, chunk in package_selected:
            fingerprint = chunk["text"][:160]
            if fingerprint in seen_text:
                continue
            seen_text.add(fingerprint)
            selected.append((score, chunk))

    if not selected:
        if not is_code_sample:
            sys.exit(
                "No matching documentation was found under https://go.dev/doc/ "
                "or https://pkg.go.dev/."
            )
        # A code request must still get working code. The answer prompt
        # allows the model to fill gaps from general Go knowledge, so pass
        # an empty context instead of refusing because the index had no match.
        print(
            "No matching Go documentation excerpts; generating code from "
            "general Go knowledge.",
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
