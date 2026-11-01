#!/usr/bin/env python3
"""Answer PHP questions using only the official PHP documentation.

Usage:
    python3 php_doc_search.py <question>
    python3 php_doc_search.py "how do I sort an associative array?"
    python3 php_doc_search.py --refresh "what is an arrow function?"

Source of truth:
    https://www.php.net/manual/en/ (the PHP manual: language reference,
    function reference, class reference, security, configuration, and
    installation chapters).

How it works:
    1. Crawl the English PHP manual once and cache the page text locally
       (default: ~/.cache/php_doc_search/index.json). The crawl is bounded per
       manual section so the language reference, the function reference, and
       the class reference all contribute to the index.
    2. Ask Ollama model ornith:35b (thinking disabled) to turn the question
       into search keywords, to detect whether the question asks for a code
       sample, and to find function, class, or extension names it mentions.
    3. Rank cached manual chunks with BM25 using those keywords.
    4. For code-sample questions, also fetch the dedicated manual pages for
       named symbols (for example function.array-map.php, class.datetime.php,
       or ref.curl.php) and add their best excerpts. Symbols referenced by the
       best-ranked excerpts are looked up too, so a question about "sorting
       associative arrays" can pull in the relevant array functions.
    5. Pin a matching note from ./knowledge/php (or ./knowledge/common_language)
       as one complete context block. A long note is a reference
       implementation, so chunking it into BM25 fragments would hide most of
       its code; the remaining context slots are filled with manual excerpts.
    6. Ask ornith:35b (thinking disabled) to answer from the best matching
       excerpts and the complete reference implementation when one applies,
       citing the www.php.net/manual/en/ URLs used.

The index is refreshed automatically when it is older than seven days. Use
--refresh to force a refresh, or --offline to search a cached index without
touching the network.

Only pages under https://www.php.net/manual/en/ are used; no other source is
used.
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
from knowledge_docs import (
    boost_knowledge_scores,
    knowledge_chunks,
    matching_knowledge_notes,
)

try:
    import requests
except ImportError as exc:  # pragma: no cover - dependency guard
    sys.exit(f"Missing dependency: {exc.name}. Install with: pip install requests")

try:
    from rank_bm25 import BM25Okapi
except ImportError:  # pragma: no cover - optional dependency
    BM25Okapi = None


DOC_ROOT = "https://www.php.net/manual/en/"
DOC_HOST = "www.php.net"
DOC_PATH_PREFIX = "/manual/en/"
MODEL = "ornith:35b"

# Manual sections to crawl. Priorities keep the language reference and the
# most useful reference chapters ahead of installation and appendices, and the
# per-section limits make sure every major section contributes pages.
DOC_SEEDS: tuple[tuple[float, str], ...] = (
    (0.0, DOC_ROOT),
    (0.05, "https://www.php.net/manual/en/language.types.php"),
    (0.05, "https://www.php.net/manual/en/language.control-structures.php"),
    (0.05, "https://www.php.net/manual/en/language.functions.php"),
    (0.05, "https://www.php.net/manual/en/language.oop5.php"),
    (0.05, "https://www.php.net/manual/en/language.namespaces.php"),
    (0.05, "https://www.php.net/manual/en/reserved.exceptions.php"),
    (0.05, "https://www.php.net/manual/en/language.enumerations.php"),
    (0.05, "https://www.php.net/manual/en/language.generators.php"),
    (0.05, "https://www.php.net/manual/en/language.references.php"),
    # High-value function and class reference chapters.
    (0.10, "https://www.php.net/manual/en/ref.array.php"),
    (0.10, "https://www.php.net/manual/en/ref.strings.php"),
    (0.10, "https://www.php.net/manual/en/ref.datetime.php"),
    (0.10, "https://www.php.net/manual/en/ref.filesystem.php"),
    (0.10, "https://www.php.net/manual/en/ref.json.php"),
    (0.10, "https://www.php.net/manual/en/ref.pcre.php"),
    (0.10, "https://www.php.net/manual/en/ref.curl.php"),
    (0.10, "https://www.php.net/manual/en/ref.pdo-mysql.php"),
    (0.10, "https://www.php.net/manual/en/ref.mysqli.php"),
    (0.10, "https://www.php.net/manual/en/class.datetime.php"),
    (0.10, "https://www.php.net/manual/en/class.pdo.php"),
    (0.10, "https://www.php.net/manual/en/class.exception.php"),
    (0.10, "https://www.php.net/manual/en/class.arrayobject.php"),
    (0.10, "https://www.php.net/manual/en/class.closure.php"),
    (0.10, "https://www.php.net/manual/en/class.generator.php"),
    (0.20, "https://www.php.net/manual/en/security.php"),
    (0.20, "https://www.php.net/manual/en/features.php"),
    (0.20, "https://www.php.net/manual/en/configuration.php"),
    (0.20, "https://www.php.net/manual/en/install.php"),
    (0.30, "https://www.php.net/manual/en/faq.php"),
    (0.30, "https://www.php.net/manual/en/appendices.php"),
)

# Per-section page budgets. Individual function pages are numerous, so they
# get a modest budget; questions about a specific function fetch that page on
# demand (see collect_symbol_excerpts).
SECTION_LIMITS = {
    "/": 10,
    "index": 6,
    "language.": 70,
    "functions.": 20,
    "reserved.": 10,
    "function.": 45,
    "class.": 40,
    "ref.": 45,
    "security.": 8,
    "features.": 8,
    "configuration.": 10,
    "install.": 12,
    "faq.": 6,
    "appendices.": 8,
    "internals2.": 4,
    "changelog.": 2,
}
DEFAULT_SECTION_LIMIT = 6

DEFAULT_CACHE_DIR = Path(
    os.environ.get("PHP_DOC_SEARCH_CACHE", "~/.cache/php_doc_search")
).expanduser()
CACHE_VERSION = 1
SYMBOL_CACHE_VERSION = 1
CACHE_MAX_AGE_SECONDS = 7 * 24 * 60 * 60

DEFAULT_MAX_PAGES = 250
CRAWL_WORKERS = 4
REQUEST_TIMEOUT = (10, 30)
USER_AGENT = "php-doc-search/1.0 (+local documentation search)"

CHUNK_TARGET_CHARS = 1100
CHUNK_MAX_CHARS = 1800
CHUNKS_PER_PAGE = 2
DEFAULT_TOP_K = 8
DEFAULT_SYMBOL_TOP_K = 3
MAX_SYMBOLS = 4

OLLAMA_HOST = os.environ.get("OLLAMA_HOST", "http://localhost:11434").rstrip("/")
if not OLLAMA_HOST.startswith(("http://", "https://")):
    OLLAMA_HOST = f"http://{OLLAMA_HOST}"

STOPWORDS = {
    "a", "about", "after", "all", "also", "an", "and", "any", "are", "as",
    "at", "be", "because", "been", "before", "but", "by", "can", "could",
    "do", "does", "doing", "for", "from", "get", "has", "have", "he", "her",
    "here", "him", "his", "how", "i", "if", "in", "into", "is", "it", "its",
    "just", "me", "my", "of", "on", "or", "our", "php", "should", "so",
    "some", "tell", "than", "that", "the", "their", "them", "then", "there",
    "these", "they", "this", "to", "was", "we", "were", "what", "when",
    "where", "which", "who", "why", "will", "with", "would", "you", "your",
}

KEYWORD_PROMPT = """You turn PHP programming questions into search keywords
for the official PHP manual at https://www.php.net/manual/en/.

Question: {question}

Return a JSON object of exactly this shape:
{{"keywords": ["keyword or short phrase", "..."], "code_sample": false, "symbols": ["function, class, or extension name", "..."]}}

Rules:
- Return 3 to 8 keywords or short phrases.
- Include exact PHP identifiers, functions, classes, methods, extensions, and
  concepts that would appear in the PHP manual (for example: array_map,
  PDO::prepare, DateTimeImmutable, closure, arrow function, generator,
  session_start, password_hash, json_encode, regular expression).
- Prefer terminology used by the PHP manual over general wording.
- Set "code_sample" to true when the question asks for example code, a
  snippet, a usage demonstration, or an implementation (for example: "show me
  code for ...", "how do I use ...", "give an example of ..."). Otherwise set
  it to false.
- Set "symbols" to PHP function names (array_map), class or method names
  (DateTime, PDO::prepare), or extension names (curl, pdo, pcre) explicitly
  mentioned in the question. Do not include variables, language keywords such
  as if/for/function, or generic words such as array or string unless they are
  part of a concrete name. Use an empty list when the question does not name
  any. Never guess.
- Do not include explanations, URLs, or markdown.
"""

ANSWER_PROMPT = """You are a PHP documentation assistant. Use the numbered
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
  explanation or code. Still write a complete, runnable PHP program or
  function that answers the question. Use the APIs and patterns shown in the
  excerpts where they are relevant, and fill any remaining gaps with your own
  general PHP knowledge.
- An excerpt marked "Complete local knowledge reference implementation" is a
  curated, working program kept with this project. When it matches the task,
  treat it as the reference implementation: preserve its architecture, helper
  functions, request headers, endpoint sequence, fallback clients, and
  argument handling, and adapt it to the exact request instead of replacing
  it with a different, untested approach. Keep all of its required helper
  functions in your answer; do not shorten a long reference implementation
  merely to be brief.
- When you add code that is not directly supported by an excerpt, do not cite
  an excerpt for it, and do not mention that the indexed manual was missing
  the answer at all.
- If the excerpts mention a specific PHP API for the task (for example
  array_map, PDO::prepare, or DateTimeImmutable::format), use that API in the
  working code instead of switching to a different extension. Use only PHP
  core/standard extensions and the extensions the excerpts explicitly show;
  never add a PECL or Composer package unless an excerpt uses it.
- Fence every PHP code block as ```php and include the opening <?php tag for
  complete scripts.
- For non-code questions, if the excerpts genuinely do not contain the
  answer, say clearly that the indexed PHP documentation
  (www.php.net/manual/en/) does not contain this information.
- Cite the excerpt number for each factual claim taken from an excerpt, for
  example: "Use PDO::prepare() [2]."
- Prefer precise, practical answers. Include short PHP code examples when the
  manual provides them.
- Never invent functions, classes, methods, constants, or behavior that do
  not exist in PHP. Any generated code must use real documented APIs.
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
# skip nav/header/footer here: the PHP manual nests navigation inside the
# page, and skipping it wholesale would swallow the documentation.
SKIP_TAGS = {
    "button", "canvas", "noscript", "script", "style", "svg", "template",
}
# The language selector and the user-contributed notes are noise for a
# documentation index. They are skipped by id/class (see DocHTMLParser).
SKIP_IDS = {"page-tools", "usernotes", "allnotes"}
SKIP_CLASSES = {"page-tools", "usernotes"}
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

# Common PHP functions. Explicit call syntax (``array_map(...)``) and the
# keyword model are always trusted; this list lets a question such as "how do
# I sort an associative array" find the relevant function pages without
# guessing.
KNOWN_FUNCTIONS = frozenset(
    """
    array_change_key_case array_chunk array_column array_combine array_count_values
    array_diff array_filter array_flip array_intersect array_key_exists array_keys
    array_map array_merge array_multisort array_pad array_pop array_push array_rand
    array_reduce array_reverse array_search array_shift array_slice array_splice
    array_sum array_unique array_unshift array_values array_walk array_walk_recursive
    arsort asort count current end in_array key_exists krsort ksort next pos prev
    reset rsort shuffle sort uasort uksort usort
    strlen strtolower strtoupper ucfirst ucwords trim ltrim rtrim substr substr_count
    substr_replace str_replace str_ireplace str_pad str_repeat str_contains
    str_starts_with str_ends_with str_split strpos stripos strrpos strstr stristr
    nl2br htmlspecialchars htmlentities strip_tags addslashes stripslashes explode
    implode join sprintf printf vsprintf number_format wordwrap similar_text
    soundex metaphone levenshtein strcmp strcasecmp strncmp strnatcmp
    preg_match preg_match_all preg_replace preg_split preg_quote preg_grep
    json_encode json_decode json_last_error
    file_get_contents file_put_contents fopen fclose fread fwrite fgets fgetcsv
    fputcsv file_exists is_file is_dir mkdir unlink rename copy filesize
    pathinfo basename dirname realpath glob scandir filemtime fileperms
    date time mktime strtotime checkdate date_default_timezone_set
    date_create date_format date_modify date_diff
    session_start session_destroy session_unset setcookie header
    filter_var filter_input filter_var_array
    password_hash password_verify password_needs_rehash
    hash hash_equals hash_hmac random_bytes random_int
    curl_init curl_setopt curl_setopt_array curl_exec curl_close
    mysqli_connect mysqli_query mysqli_fetch_assoc mysqli_prepare
    is_null is_array is_string is_int is_float is_bool is_numeric is_callable
    isset empty intval floatval strval boolval settype var_dump print_r var_export
    serialize unserialize gettype get_class method_exists property_exists
    class_exists function_exists spl_autoload_register
    call_user_func call_user_func_array usleep sleep microtime
    throw_error error_log error_reporting set_error_handler
    """.split()
)
# Common PHP classes and interfaces. Explicit mentions are always trusted;
# this list supports questions that use the class name as ordinary prose.
KNOWN_CLASSES = frozenset(
    """
    ArrayAccess ArrayIterator ArrayObject Closure Countable DateTime
    DateTimeImmutable DateTimeInterface DateInterval DatePeriod Error Exception
    Generator InvalidArgumentException Iterator IteratorAggregate JsonException
    JsonSerializable LogicException PDO PDOException PDOStatement
    RuntimeException SplFileInfo SplFileObject SplObjectStorage SplQueue
    SplStack Stringable Throwable TypeError UnexpectedValueException
    WeakMap WeakReference
    """.split()
)
# Manual reference chapters that are commonly called "extensions".
KNOWN_EXTENSIONS = frozenset(
    """
    apcu bcmath calendar curl ctype dom exif fileinfo filter ftp gd gettext gmp
    hash iconv imagick imap intl json ldap libxml mailparse mbstring memcached
    mongodb mysqli openssl pcntl pcre pdo pdo-mysql pdo-pgsql pgsql phar posix
    random readline redis reflection session shmop simplexml soap sockets sodium
    spl sqlite3 standard sysvmsg sysvsem sysvshm tidy tokenizer xml xmlreader
    xmlwriter yaml zip zlib
    """.split()
)

# Language constructs and control words that must never be looked up as
# functions even when they are followed by "(" in prose.
NON_FUNCTION_WORDS = frozenset(
    """
    abstract and array as break callable case catch class clone const continue
    declare default die do echo else elseif empty enddeclare endfor endforeach
    endif endswitch endwhile enum eval exit extends false final finally fn for
    foreach function global goto if implements include include_once instanceof
    insteadof interface isset list match namespace new null or print private
    protected public readonly require require_once return static switch throw
    trait true try unset use var while xor yield
    """.split()
)
GENERIC_SYMBOL_WORDS = frozenset(
    """
    code example examples sample samples snippet snippets php manual function
    class method extension array arrays string strings value values key keys
    data variable variables object objects type types file files
    """.split()
)

SYMBOL_NAME_RE = re.compile(r"[A-Za-z_][A-Za-z0-9_]*")
FUNCTION_CALL_RE = re.compile(
    r"(?<![\w$>:])([A-Za-z_][A-Za-z0-9_]*)\s*\("
)
STATIC_CLASS_RE = re.compile(r"\b([A-Z][A-Za-z0-9_]*)::")
NEW_CLASS_RE = re.compile(r"\bnew\s+\\?([A-Z][A-Za-z0-9_]*)\b")
CLASS_PHRASE_RE = re.compile(
    r"\b(?:the\s+|a\s+|new\s+|new\s+the\s+)?"
    r"\\?([A-Za-z_][A-Za-z0-9_]*)\s+(?:class|interface|trait)\b",
    re.IGNORECASE,
)
EXTENSION_PHRASE_RE = re.compile(
    r"\b(?:the\s+|a\s+|new\s+|new\s+the\s+)?"
    r"`?([a-z][a-z0-9_-]{1,63})`?\s+extension\b",
    re.IGNORECASE,
)
FUNCTION_PHRASE_RE = re.compile(
    r"\b(?:the\s+|a\s+|new\s+|new\s+the\s+)?"
    r"`?([a-z_][a-z0-9_-]{1,63})`?\s+function\b",
    re.IGNORECASE,
)
BACKTICK_RE = re.compile(r"`([^`\n]+)`")
MANUAL_URL_RE = re.compile(
    r"php\.net/manual/en/(?:function|class|ref)\.([A-Za-z0-9_.-]+)\.php",
    re.IGNORECASE,
)
QUALIFIED_MEMBER_RE = re.compile(
    r"\b([A-Z][A-Za-z0-9_]*)::([A-Za-z_][A-Za-z0-9_]*)\b"
)


class DocHTMLParser(HTMLParser):
    """Extract readable text, the title, and links from a PHP manual page."""

    def __init__(self):
        super().__init__(convert_charrefs=True)
        self.title = ""
        self.main_text = ""
        self.links: list[str] = []
        self._in_title = False
        self._content_tag: str | None = None
        self._content_depth = 0
        self._content_started = False
        self._content_parts: list[str] = []
        self._fallback_parts: list[str] = []
        self._skip_tag: str | None = None
        self._skip_depth = 0
        self._pre_depth = 0

    def handle_starttag(self, tag, attrs):
        tag = tag.lower()
        attributes = dict(attrs)
        if tag == "title":
            self._in_title = True
            return

        if self._skip_tag is not None:
            if tag == self._skip_tag:
                self._skip_depth += 1
            return

        if tag in SKIP_TAGS:
            self._skip_tag = tag
            self._skip_depth = 1
            return

        element_id = (attributes.get("id") or "").strip().lower()
        classes = {
            item.strip().lower()
            for item in (attributes.get("class") or "").split()
            if item.strip()
        }
        if element_id in SKIP_IDS or classes & SKIP_CLASSES:
            self._skip_tag = tag
            self._skip_depth = 1
            return

        if self._content_tag is None:
            if element_id == "layout-content" or tag in ("article", "main"):
                self._content_tag = tag
                self._content_depth = 1
                self._content_started = True
                # Fall through so the opening element is processed normally.
            elif tag == "body":
                self._content_started = False
        elif tag == self._content_tag:
            self._content_depth += 1

        if tag == "pre":
            # Preserve code samples as fenced blocks. Whitespace inside <pre>
            # is significant, so it must not be collapsed like prose.
            self._pre_depth += 1
            self._append("\n```\n")
            return
        if tag == "a":
            href = attributes.get("href")
            if href:
                self.links.append(href)
        if tag in BLOCK_TAGS:
            self._append("\n")
        if tag == "img":
            alt = attributes.get("alt")
            if alt:
                self._append(f" {alt} ")

    def handle_endtag(self, tag):
        tag = tag.lower()
        if tag == "title":
            self._in_title = False
            return
        if self._skip_tag is not None:
            if tag == self._skip_tag:
                self._skip_depth -= 1
                if self._skip_depth <= 0:
                    self._skip_tag = None
            return
        if self._content_tag is not None and tag == self._content_tag:
            self._content_depth -= 1
            if self._content_depth <= 0:
                self._content_tag = None
                self._content_started = False
        if tag == "pre" and self._pre_depth:
            self._pre_depth -= 1
            self._append("\n```\n")
            return
        if tag in BLOCK_TAGS:
            self._append("\n")

    def handle_data(self, data):
        if self._in_title:
            self.title += data
        if self._skip_tag is not None:
            return
        self._append(data)

    def _append(self, text):
        if self._skip_tag is not None:
            return
        if self._content_started:
            self._content_parts.append(text)
        else:
            self._fallback_parts.append(text)

    def finish(self):
        content = _clean_text("".join(self._content_parts))
        fallback = _clean_text("".join(self._fallback_parts))
        # Prefer the manual content area. Fall back to the whole page only when
        # the page has no recognizable content container.
        self.main_text = content if len(content) >= 200 else fallback or content
        self.title = _clean_title(self.title)
        return self


def _clean_title(title: str) -> str:
    title = " ".join(title.split())
    if title.lower().startswith("php:"):
        title = title[4:].strip()
    if title.lower().endswith("- manual"):
        title = title[: -len("- manual")].rstrip(" -")
    return title


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
    # Treat "directory/index.php" and "directory/" as the same page so the
    # bounded crawl does not spend its budget on duplicates.
    if path.endswith("/index.php"):
        path = path[: -len("index.php")]
    return f"{parsed.scheme}://{parsed.netloc}{path}"


def section_for_url(url: str) -> str:
    path = urlparse(url).path
    if path.endswith("/"):
        return path.rstrip("/").rsplit("/", 1)[-1] or "/"
    name = path.rsplit("/", 1)[-1].lower()
    if name.startswith("language."):
        return "language."
    if name.startswith("functions."):
        return "functions."
    if name.startswith("reserved."):
        return "reserved."
    if name.startswith("function."):
        return "function."
    if name.startswith("class."):
        return "class."
    if name.startswith("ref."):
        return "ref."
    if name.startswith("security."):
        return "security."
    if name.startswith("features."):
        return "features."
    if name.startswith("configuration."):
        return "configuration."
    if name.startswith("install."):
        return "install."
    if name.startswith("faq."):
        return "faq."
    if name.startswith("appendices."):
        return "appendices."
    if name.startswith("internals2."):
        return "internals2."
    if name.startswith("changelog."):
        return "changelog."
    if name in ("index.php", ""):
        return "index"
    return "/"


def is_allowed_doc_url(url: str) -> bool:
    parsed = urlparse(url)
    if parsed.scheme not in ("http", "https"):
        return False
    if parsed.netloc != DOC_HOST:
        return False
    path = parsed.path
    if path in ("", "/manual/en/", "/manual/en"):
        return True
    if not path.startswith(DOC_PATH_PREFIX):
        return False
    if parsed.query:
        return False
    lowered = path.lower()
    if lowered.endswith(SKIP_EXTENSIONS):
        return False
    # Search, lookup, note, and language-switching pages are not documentation
    # content and would waste the crawl budget.
    if any(
        marker in lowered
        for marker in (
            "/manual/change.php",
            "/manual/add-note.php",
            "/manual/vote-note.php",
            "/lookup-form.php",
            "/search.php",
            "/cached.php",
            "/copyright.php",
        )
    ):
        return False
    if "help-translate" in lowered:
        return False
    return True


def fetch_page(session: requests.Session, url: str, *, quiet: bool = False) -> dict | None:
    try:
        response = session.get(url, timeout=REQUEST_TIMEOUT)
        response.raise_for_status()
    except requests.RequestException as exc:
        if not quiet:
            print(f"Warning: could not fetch {url}: {exc}", file=sys.stderr)
        return None

    content_type = response.headers.get("Content-Type", "")
    if "html" not in content_type.lower():
        return None
    # The PHP manual serves UTF-8 HTML without a charset parameter.
    # requests would otherwise fall back to ISO-8859-1 and mangle non-ASCII
    # text in titles and examples.
    if "charset=" not in content_type.lower():
        response.encoding = "utf-8"

    parser = DocHTMLParser()
    try:
        parser.feed(response.text)
        parser.close()
    except Exception as exc:  # defensive: malformed markup
        if not quiet:
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
        # Preserve document order: the manual's chapter order is more useful
        # than alphabetical order when the crawl budget is bounded.
        "links": list(dict.fromkeys(links)),
    }


def crawl_docs(max_pages: int) -> list[dict]:
    """Priority crawl of the English PHP manual."""
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


def symbol_cache_path(cache_dir: Path, symbol: str) -> Path:
    safe = re.sub(r"[^A-Za-z0-9_.\-]+", "__", symbol).strip("._")
    return cache_dir / "symbols" / f"{safe}.json"


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
            "Could not index https://www.php.net/manual/en/. "
            "Check your network connection."
        )

    try:
        save_cache(path, pages)
        print(f"Indexed {len(pages)} pages into {path}", file=sys.stderr)
    except OSError as exc:
        print(f"Warning: could not save cache: {exc}", file=sys.stderr)
    return pages


def load_symbol_cache(path: Path):
    try:
        with path.open("r", encoding="utf-8") as handle:
            data = json.load(handle)
    except (OSError, ValueError):
        return None
    if not isinstance(data, dict) or data.get("version") != SYMBOL_CACHE_VERSION:
        return None
    if not isinstance(data.get("text"), str) or not data["text"]:
        return None
    return data


def save_symbol_cache(path: Path, page: dict):
    path.parent.mkdir(parents=True, exist_ok=True)
    payload = {
        "version": SYMBOL_CACHE_VERSION,
        "fetched_at": time.time(),
        "url": page["url"],
        "title": page["title"],
        "text": page["text"],
    }
    temporary = path.with_suffix(path.suffix + ".tmp")
    with temporary.open("w", encoding="utf-8") as handle:
        json.dump(payload, handle, ensure_ascii=False, separators=(",", ":"))
    temporary.replace(path)


def symbol_url_candidates(symbol: str) -> list[str]:
    """Return PHP manual URLs that may document ``symbol``.

    Function pages replace underscores with hyphens (array_map lives at
    function.array-map.php), class pages use the lowercase class name, method
    pages use <class>.<method>.php (PDO::prepare lives at pdo.prepare.php),
    and extension chapters use ref.<name>.php. Candidates are tried in an
    order that matches the symbol's spelling: an uppercase name is probably a
    class, a lowercase name is probably a function.
    """
    name = symbol.strip().lstrip("\\")
    if "::" in name:
        class_name, _, member_name = name.partition("::")
        class_name = class_name.strip().lstrip("\\")
        member_name = member_name.strip()
        if not (
            SYMBOL_NAME_RE.fullmatch(class_name)
            and SYMBOL_NAME_RE.fullmatch(member_name)
        ):
            return []
        lower_class = class_name.lower()
        lower_member = member_name.lower()
        hyphen_member = lower_member.replace("_", "-")
        ordered = [
            urljoin(DOC_ROOT, f"{lower_class}.{lower_member}.php"),
            urljoin(DOC_ROOT, f"{lower_class}.{hyphen_member}.php"),
            urljoin(DOC_ROOT, f"class.{lower_class}.php"),
        ]
        return list(dict.fromkeys(ordered))
    if not SYMBOL_NAME_RE.fullmatch(name):
        return []
    lower = name.lower()
    hyphenated = lower.replace("_", "-")
    function_url = urljoin(
        DOC_ROOT, f"function.{hyphenated}.php"
    )
    class_url = urljoin(DOC_ROOT, f"class.{lower}.php")
    ref_url = urljoin(DOC_ROOT, f"ref.{hyphenated}.php")
    ref_plain_url = urljoin(DOC_ROOT, f"ref.{lower}.php")
    if name[:1].isupper() or name in KNOWN_CLASSES:
        ordered = [class_url, function_url]
    else:
        ordered = [function_url, class_url]
    ordered.extend([ref_url, ref_plain_url])
    if lower.endswith("s") and len(lower) > 3:
        # "arrays" -> ref.array.php, "strings" stays ref.strings.php first.
        ordered.append(urljoin(DOC_ROOT, f"ref.{hyphenated[:-1]}.php"))
    return list(dict.fromkeys(ordered))


def get_symbol_page(
    session: requests.Session,
    symbol: str,
    cache_dir: Path,
    refresh: bool = False,
    offline: bool = False,
) -> dict | None:
    """Fetch (or load from cache) the manual page for a PHP symbol."""
    cached = load_symbol_cache(symbol_cache_path(cache_dir, symbol))
    if cached is not None:
        page = {
            "url": cached.get("url") or urljoin(DOC_ROOT, symbol),
            "title": cached.get("title") or symbol,
            "text": cached["text"],
            "links": [],
        }
    else:
        page = None

    if offline:
        if page is None:
            print(
                f"Warning: no cached manual page for {symbol}; skipping.",
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

    fetched = None
    for url in symbol_url_candidates(symbol):
        candidate = fetch_page(session, url, quiet=True)
        if candidate is not None:
            fetched = candidate
            break

    if fetched is None:
        if page is not None:
            print(
                f"Warning: using stale cached manual page for {symbol}.",
                file=sys.stderr,
            )
            return page
        print(
            f"Warning: could not find a manual page for {symbol}; skipping.",
            file=sys.stderr,
        )
        return None

    try:
        save_symbol_cache(symbol_cache_path(cache_dir, symbol), fetched)
    except OSError as exc:
        print(
            f"Warning: could not save manual cache for {symbol}: {exc}",
            file=sys.stderr,
        )
    return fetched


def collect_symbol_excerpts(question: str, keywords: list[str], symbols: list[str], args):
    """Rank excerpts from the manual pages of the named PHP symbols."""
    if not symbols:
        return []

    cache_dir = Path(args.cache_dir).expanduser()
    session = requests.Session()
    session.headers.update({"User-Agent": USER_AGENT})

    print(
        "Code-sample question detected; searching the PHP manual for: "
        + ", ".join(symbols),
        file=sys.stderr,
    )

    collected = []
    for symbol in symbols:
        page = get_symbol_page(
            session,
            symbol,
            cache_dir,
            refresh=args.refresh,
            offline=args.offline,
        )
        if page is None:
            continue
        symbol_chunks = chunk_pages([page])
        if not symbol_chunks:
            print(
                f"Warning: no usable text in the manual page for {symbol}.",
                file=sys.stderr,
            )
            continue
        picked = rank_chunks(
            question,
            keywords,
            symbol_chunks,
            args.symbol_top_k,
            per_page_limit=max(args.symbol_top_k, CHUNKS_PER_PAGE),
            prefer_code=True,
        )
        print(
            f"{page['url']}: added {len(picked)} excerpt(s)", file=sys.stderr
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


def normalize_symbol(name: str) -> str | None:
    """Return a clean PHP symbol name, or None when it is not a valid one.

    A static member keeps its class (``PDO::prepare``) so the dedicated method
    page can be fetched. An instance member (``$pdo->prepare``) cannot be
    resolved to a class from the question alone, so it is ignored.
    """
    name = str(name).strip().strip("\"'`")
    name = re.sub(
        r"^(?:https?://)?(?:www\.)?php\.net/manual/en/", "", name
    )
    name = re.sub(
        r"^(?:function|class|ref)\.", "", name, flags=re.IGNORECASE
    )
    name = re.sub(r"\.php$", "", name, flags=re.IGNORECASE)
    name = name.strip().rstrip("()")
    if "->" in name:
        return None
    if "::" in name:
        class_name, _, member_name = name.partition("::")
        class_name = class_name.strip().lstrip("\\")
        member_name = member_name.strip().rstrip("()")
        if not (
            SYMBOL_NAME_RE.fullmatch(class_name)
            and SYMBOL_NAME_RE.fullmatch(member_name)
        ):
            return None
        if (
            class_name.lower() in NON_FUNCTION_WORDS
            or class_name.lower() in STOPWORDS
            or class_name.lower() in GENERIC_SYMBOL_WORDS
            or member_name.lower() in NON_FUNCTION_WORDS
            or member_name.lower() in GENERIC_SYMBOL_WORDS
        ):
            return None
        return f"{class_name}::{member_name}"
    name = name.lstrip("\\")
    if not name or ".." in name or "/" in name:
        return None
    if not SYMBOL_NAME_RE.fullmatch(name):
        return None
    lowered = name.lower()
    if (
        lowered in NON_FUNCTION_WORDS
        or lowered in STOPWORDS
        or lowered in GENERIC_SYMBOL_WORDS
    ):
        return None
    return name


def _backtick_symbols(text: str) -> list[str]:
    found = []
    for content in BACKTICK_RE.findall(text):
        content = content.strip()
        first = re.split(r"\s|\(|\{|\[|->|::", content, maxsplit=1)[0]
        first = first.strip().lstrip("\\")
        if not first:
            continue
        if SYMBOL_NAME_RE.fullmatch(first):
            found.append(first)
    return found


def guess_symbols(question: str) -> list[str]:
    """Fallback symbol detection from the question text."""
    found: list[str] = []

    found.extend(MANUAL_URL_RE.findall(question))
    found.extend(EXTENSION_PHRASE_RE.findall(question))
    found.extend(FUNCTION_PHRASE_RE.findall(question))
    found.extend(CLASS_PHRASE_RE.findall(question))
    members = [match.group(0) for match in QUALIFIED_MEMBER_RE.finditer(question)]
    found.extend(members)
    member_classes = {
        match.group(1).lower() for match in QUALIFIED_MEMBER_RE.finditer(question)
    }
    found.extend(
        class_name
        for class_name in STATIC_CLASS_RE.findall(question)
        if class_name.lower() not in member_classes
    )
    found.extend(NEW_CLASS_RE.findall(question))
    found.extend(_backtick_symbols(question))

    for match in FUNCTION_CALL_RE.finditer(question):
        name = match.group(1)
        if name.lower() in NON_FUNCTION_WORDS:
            continue
        # Function calls in the question are explicit enough to trust, but
        # ignore generic words such as "example(...)".
        if name.lower() in GENERIC_SYMBOL_WORDS:
            continue
        found.append(name)

    lowered = question.lower()
    explicit = bool(
        MANUAL_URL_RE.search(question)
        or BACKTICK_RE.search(question)
        or FUNCTION_CALL_RE.search(question)
        or CLASS_PHRASE_RE.search(question)
        or EXTENSION_PHRASE_RE.search(question)
    )
    for function in KNOWN_FUNCTIONS:
        pattern = r"(?<![\w$])" + re.escape(function) + r"(?![\w])"
        if re.search(pattern, lowered):
            found.append(function)
    for class_name in KNOWN_CLASSES:
        pattern = r"(?<![\w\\])" + re.escape(class_name) + r"(?![\w])"
        if re.search(pattern, question):
            found.append(class_name)
    if explicit:
        for extension in KNOWN_EXTENSIONS:
            pattern = r"(?<![\w-])" + re.escape(extension) + r"(?![\w-])"
            if re.search(pattern, lowered):
                found.append(extension)
    return found


def infer_symbols(text: str) -> list[str]:
    """Infer symbols from concept words and known PHP identifiers."""
    scores: dict[str, int] = {}
    lowered = text.lower()
    for function in KNOWN_FUNCTIONS:
        pattern = r"(?<![\w$])" + re.escape(function) + r"(?![\w])"
        if re.search(pattern, lowered):
            scores[function] = 2
    for class_name in KNOWN_CLASSES:
        pattern = r"(?<![\w\\])" + re.escape(class_name) + r"(?![\w])"
        if re.search(pattern, text):
            scores[class_name] = 2
    ordered = sorted(scores, key=lambda item: (-scores[item], item))
    return ordered


def symbols_mentioned_in_context(
    question: str,
    keywords: list[str],
    selected,
    exclude=(),
) -> list[str]:
    """Discover PHP symbols referenced by the retrieved documentation."""
    excluded = {str(symbol).lower() for symbol in exclude}
    scores: dict[str, int] = {}

    def add(candidate: str, weight: int) -> None:
        symbol = normalize_symbol(candidate)
        if symbol is None or symbol.lower() in excluded:
            return
        scores[symbol] = scores.get(symbol, 0) + weight

    texts = [
        f"{chunk.get('title', '')}\n{chunk.get('text', '')}"
        for _score, chunk in selected
    ]
    context_text = "\n".join(texts)

    # Only trust explicit references in the retrieved excerpts. PHP examples
    # contain dozens of incidental function calls, so treating every call as a
    # symbol to look up would add a lot of unrelated manual pages.
    for match in EXTENSION_PHRASE_RE.finditer(context_text):
        add(match.group(1), 4)
    for match in FUNCTION_PHRASE_RE.finditer(context_text):
        add(match.group(1), 4)
    for match in CLASS_PHRASE_RE.finditer(context_text):
        add(match.group(1), 4)
    for match in MANUAL_URL_RE.finditer(context_text):
        add(match.group(1), 4)
    for name in _backtick_symbols(context_text):
        if name.lower() in KNOWN_FUNCTIONS or name in KNOWN_CLASSES:
            add(name, 2)

    query_text = " ".join([question, *keywords])
    for symbol in guess_symbols(query_text):
        add(symbol, 3)
    for symbol in infer_symbols(query_text):
        add(symbol, 2)

    ordered = sorted(scores, key=lambda item: (-scores[item], item))
    return ordered


def candidate_symbols(question: str) -> list[str]:
    """Return explicitly named or clearly implied PHP symbols."""
    return clean_symbols(guess_symbols(question) + infer_symbols(question))


def clean_symbols(candidates) -> list[str]:
    """Normalize, deduplicate, and cap a list of PHP symbol names."""
    cleaned: list[str] = []
    seen: set[str] = set()
    deferred_classes: list[str] = []
    member_classes: set[str] = set()
    raw_candidates = candidates if isinstance(candidates, list) else []
    for symbol in raw_candidates:
        normalized = normalize_symbol(symbol)
        if normalized is None or normalized.lower() in seen:
            continue
        seen.add(normalized.lower())
        if "::" in normalized:
            member_classes.add(normalized.split("::", 1)[0].lower())
            cleaned.append(normalized)
        elif normalized.lower() in member_classes:
            continue
        else:
            deferred_classes.append(normalized)
        if len(cleaned) >= MAX_SYMBOLS:
            break
    # A method's manual page is more specific than its class page. Drop a
    # plain class that is already represented by one of its methods.
    cleaned.extend(
        name for name in deferred_classes if name.lower() not in member_classes
    )
    return cleaned[:MAX_SYMBOLS]


def analyze_question(question: str, model: str):
    """Return (keywords, is_code_sample, symbols) for the question."""
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
    symbols = []
    try:
        response = requests.post(
            f"{OLLAMA_HOST}/api/chat", json=payload, timeout=(10, 180)
        )
        response.raise_for_status()
        content = response.json().get("message", {}).get("content", "")
        data = json.loads(content)
        keywords = data.get("keywords", [])
        code_sample = data.get("code_sample")
        symbols = data.get("symbols", [])
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

    cleaned_symbols = clean_symbols(symbols)
    if not cleaned_symbols:
        cleaned_symbols = candidate_symbols(question)

    # Dedicated symbol pages are only consulted for code-sample questions.
    if not is_code_sample:
        cleaned_symbols = []
    return cleaned, is_code_sample, cleaned_symbols


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
        # Code-sample questions should surface the manual pages that actually
        # contain examples. Use an additive bonus so a negative BM25 score is
        # never amplified in the wrong direction.
        top_score = max(scores) if scores else 0.0
        bonus = max(0.001, top_score * 0.25)
        for index, chunk in enumerate(chunks):
            if "```" in chunk["text"] or "<?php" in chunk["text"]:
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

    # Phrase matches are a useful tie-breaker for exact PHP terminology.
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
        if chunk.get("whole_note"):
            source_type = (
                "Complete local knowledge reference implementation"
            )
        elif chunk.get("knowledge"):
            source_type = "Curated local knowledge excerpt"
        else:
            source_type = "Official PHP manual excerpt"
        parts.append(
            f"[{number}] Source: {chunk['url']}\n"
            f"Type: {source_type}\n"
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
            "Answer a question using the official PHP manual at "
            "https://www.php.net/manual/en/."
        )
    )
    parser.add_argument("question", nargs="+", help="the PHP question to answer")
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
            "number of manual excerpts to use "
            f"(default: {DEFAULT_TOP_K})"
        ),
    )
    parser.add_argument(
        "--symbol-top-k",
        type=int,
        default=DEFAULT_SYMBOL_TOP_K,
        help=(
            "number of excerpts per symbol page for code-sample questions "
            f"(default: {DEFAULT_SYMBOL_TOP_K})"
        ),
    )
    parser.add_argument(
        "--max-pages",
        type=int,
        default=DEFAULT_MAX_PAGES,
        help=(
            "maximum pages to crawl under /manual/en/ "
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
        help="force a fresh crawl of https://www.php.net/manual/en/",
    )
    parser.add_argument(
        "--offline",
        action="store_true",
        help=(
            "use cached php.net manual pages only; do not crawl"
        ),
    )
    parser.add_argument(
        "--keywords",
        help="comma-separated keywords, skipping the model keyword extraction",
    )
    parser.add_argument(
        "--symbols",
        help=(
            "comma-separated function/class/extension names for a code-sample "
            "question, skipping the model symbol extraction"
        ),
    )
    args = parser.parse_args(argv)

    if args.top_k < 1:
        parser.error("--top-k must be at least 1")
    if args.symbol_top_k < 1:
        parser.error("--symbol-top-k must be at least 1")
    if args.max_pages < 1:
        parser.error("--max-pages must be at least 1")

    question = " ".join(args.question).strip()
    if not question:
        parser.error("question must not be empty")

    pages = get_pages(args)
    chunks = chunk_pages(pages)
    # Curated notes in ./knowledge/php are an addition to the official manual:
    # they are chunked and ranked with the crawled PHP documentation.
    chunks.extend(knowledge_chunks("php", chunk_pages))
    if not chunks:
        sys.exit("The documentation index is empty; run with --refresh.")

    if args.keywords:
        keywords = [
            keyword.strip()
            for keyword in args.keywords.split(",")
            if keyword.strip()
        ]
        is_code_sample = bool(CODE_SAMPLE_RE.search(question))
        if args.symbols:
            symbols = clean_symbols(
                [
                    symbol.strip()
                    for symbol in args.symbols.split(",")
                    if symbol.strip()
                ]
            )
        else:
            symbols = candidate_symbols(question) if is_code_sample else []
    else:
        print("Analyzing question...", file=sys.stderr)
        keywords, is_code_sample, symbols = analyze_question(
            question, args.model
        )
    print(f"Search keywords: {', '.join(keywords)}", file=sys.stderr)

    # A long curated note (for example the PHP YouTube transcript CLI) is a
    # complete reference implementation. Rank it as one block instead of
    # letting BM25 select scattered fragments from the middle of its code
    # sample, and reserve one context slot for it so the model always sees the
    # whole program before filling the remaining slots with manual excerpts.
    knowledge_notes = matching_knowledge_notes(
        "php", question, keywords, limit=1
    )
    ranked_chunks = chunks
    ranked_top_k = args.top_k
    if knowledge_notes:
        # The complete note replaces every chunked fragment of local knowledge
        # for this question. Other notes' fragments may still match generic
        # request words (for example "HTTP request" or "JSON"), and mixing
        # them with the pinned implementation only distracts the model from
        # the working program and the official manual API pages.
        ranked_chunks = [
            chunk for chunk in chunks if not chunk.get("knowledge")
        ]
        ranked_top_k = max(0, args.top_k - len(knowledge_notes))

    selected = (
        rank_chunks(
            question,
            keywords,
            ranked_chunks,
            ranked_top_k,
            prefer_code=is_code_sample,
        )
        if ranked_top_k > 0
        else []
    )
    if knowledge_notes:
        selected = [
            (float("inf"), note) for note in knowledge_notes
        ] + selected

    symbols_to_fetch: list[str] = []
    if is_code_sample:
        symbols_to_fetch = list(symbols)
        if not symbols_to_fetch:
            # When the question does not name a symbol, look for an explicit
            # "the X extension/function/class" reference in the best-ranked
            # excerpts. This is deliberately conservative: incidental function
            # calls in PHP examples are not treated as lookup targets.
            context_symbols = symbols_mentioned_in_context(
                question, keywords, selected, exclude=symbols
            )
            if context_symbols:
                print(
                    "Documentation context references PHP symbols: "
                    + ", ".join(context_symbols),
                    file=sys.stderr,
                )
            symbols_to_fetch = clean_symbols(context_symbols)

    if symbols_to_fetch:
        symbol_selected = collect_symbol_excerpts(
            question, keywords, symbols_to_fetch, args
        )
        seen_text = {chunk["text"][:160] for _score, chunk in selected}
        for score, chunk in symbol_selected:
            fingerprint = chunk["text"][:160]
            if fingerprint in seen_text:
                continue
            seen_text.add(fingerprint)
            selected.append((score, chunk))

    if not selected:
        if not is_code_sample:
            sys.exit(
                "No matching documentation was found under "
                "https://www.php.net/manual/en/."
            )
        # A code request must still get working code. The answer prompt
        # allows the model to fill gaps from general PHP knowledge, so pass
        # an empty context instead of refusing because the index had no match.
        print(
            "No matching PHP documentation excerpts; generating code from "
            "general PHP knowledge.",
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
