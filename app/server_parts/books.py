"""/write_a_book, PDF books, and PDF knowledge graphs."""

from __future__ import annotations

from .core import *
from .core import SCRIPT_DIR

# /write_a_book researches recent news, plans chapters from that material,
# and writes each chapter with the local book-writing model. The finished
# book is saved as a self-contained HTML document under ./.chat/books and served by
# /api/book_file so the browser can open it in a new tab.
WRITE_A_BOOK_MODEL = os.environ.get(
    "OLLAMA_BOOK_MODEL", chat.ANSWER_MODEL
)

WRITE_A_BOOK_DIR = SCRIPT_DIR / ".chat" / "books"

PDF_BOOKS_DIR = SCRIPT_DIR / ".chat" / "pdf_books"
PDF_BOOKS_LAST_OPEN_PATH = PDF_BOOKS_DIR / "last_open.json"
_PDF_LAST_OPEN_LOCK = threading.Lock()

# task.more_horizontal_exploration in prompts/write_a_book.prompt asks for
# ten distinct research questions instead of six. task.refactor asks each of
# those questions to contribute an equal share of the research references.
WRITE_A_BOOK_SUB_QUESTION_LIMIT = 10

WRITE_A_BOOK_SUB_QUESTION_ARTICLE_LIMIT = 10

WRITE_A_BOOK_KEYWORD_LIMIT = WRITE_A_BOOK_SUB_QUESTION_LIMIT

# The settings asked by task.ask_question_before_writing_book and
# task.expose_settings_in_form in prompts/write_a_book.prompt. Every one of
# them is exposed in the /write_a_book form. The minima keep the book
# coherent; there is deliberately no maximum, so the caller controls the
# words, chapters, and research breadth.
# Fetch the per-question article budget directly; every planning question is
# searched independently and then sampled round-robin so one prolific query
# cannot dominate the book's references.
WRITE_A_BOOK_SEARCH_LIMIT_PER_KEYWORD = (
    WRITE_A_BOOK_SUB_QUESTION_ARTICLE_LIMIT
)

WRITE_A_BOOK_MIN_SUB_QUESTIONS = 1

WRITE_A_BOOK_MIN_SUB_QUESTION_ARTICLES = 1

# task.exact_sub_questions_article_search_limit in
# prompts/write_a_book.prompt: each keyword query over-fetches up to this
# raw result window so every planning question can still be credited with
# exactly ``WRITE_A_BOOK_SUB_QUESTION_ARTICLE_LIMIT`` distinct articles after
# deduplication, the publication cutoff, and cross-question overlaps.
WRITE_A_BOOK_RAW_SEARCH_WINDOW_MAX = 100

WRITE_A_BOOK_RESEARCH_YEARS = 2

# task.do_not_include_2024 in prompts/write_a_book.prompt: only 2025 and 2026
# material should be used, even though a rolling two-year window still reaches
# back into 2024. Any older year written into a generated query is rewritten to
# this year.
WRITE_A_BOOK_RESEARCH_START_YEAR = 2025

WRITE_A_BOOK_CHAPTER_LIMIT = 60

WRITE_A_BOOK_MIN_CHAPTERS = 3

WRITE_A_BOOK_CHAPTER_WORDS = 800

WRITE_A_BOOK_MIN_CHAPTER_WORDS = 100

WRITE_A_BOOK_EVIDENCE_CHARS = 12_000

WRITE_A_BOOK_DIGEST_CHARS = 8_000

WRITE_A_BOOK_REFERENCE_MAP_CHARS = 6_000

def _book_env_int(name: str, default: int) -> int:
    """Read a positive integer environment override for book research."""
    try:
        return max(1, int(os.environ.get(name, default)))
    except (TypeError, ValueError):
        return default

# Book research must always make forward progress. Each article fetch and each
# Google News query has its own time limit, and the whole research phase has a
# global budget. Without these caps an unresponsive publisher can hold a PHP
# child for three minutes, making /write_a_book look frozen for an hour before
# a single chapter is written. When the budget runs out the research phase
# stops cleanly and the book is still written from the material already read.
WRITE_A_BOOK_RESEARCH_BUDGET_SECONDS = _book_env_int(
    "WRITE_A_BOOK_RESEARCH_BUDGET", 300
)

WRITE_A_BOOK_SEARCH_TIMEOUT_SECONDS = _book_env_int(
    "WRITE_A_BOOK_SEARCH_TIMEOUT", 60
)

WRITE_A_BOOK_ARTICLE_TIMEOUT_SECONDS = _book_env_int(
    "WRITE_A_BOOK_ARTICLE_TIMEOUT", 20
)

WRITE_A_BOOK_RESEARCH_WORKERS = _book_env_int(
    "WRITE_A_BOOK_RESEARCH_WORKERS", 4
)

# task.blocking_detection in prompts/write_a_book.prompt: every research
# article fetch reports whether the body was read and, when it was not,
# distinguishes Google News' "automated queries" interstitial from a publisher
# paywall or bot challenge. These markers are only applied to failure details
# and very short extracted bodies, so a real article that discusses blocking
# is not mistaken for one.
_BOOK_GOOGLE_NEWS_BLOCK_MARKERS = (
    "automated queries",
    "our systems have detected unusual traffic",
)

_BOOK_WEBSITE_BLOCK_MARKERS = (
    "just a moment",
    "security verification",
    "access to this page has been denied",
    "access denied",
    "please enable js and disable any ad blocker",
    "enable javascript and cookies",
    "verify you are human",
    "are you a robot",
    "px-captcha",
    "captcha",
    "attention required",
    "you have been blocked",
    "the action you just performed triggered the security solution",
    "subscription required",
    "already a subscriber",
    "sign in to continue",
    "create a free account to continue",
    "register to continue reading",
    "this content is unavailable",
    "get unlimited access",
    "to continue reading",
    "subscribers only",
    "premium content",
    "paywall",
    "this page is not available",
    "error code cg",
)

_BOOK_BLOCK_HTTP_RE = re.compile(
    r"\b(?:401|403|429)\b|forbidden|too many requests|rate limit"
    r"|access denied",
    re.IGNORECASE,
)

def _book_block_reason(
    text: str, *, from_google_news: bool = False
) -> str:
    """Classify a fetch failure or interstitial using the task's wording."""
    lowered = " ".join(str(text or "").casefold().split())
    if not lowered:
        return ""
    if any(marker in lowered for marker in _BOOK_GOOGLE_NEWS_BLOCK_MARKERS):
        return "blocked by google news"
    if from_google_news and re.search(
        r"\b429\b|too many requests|rate limit", lowered
    ):
        return "blocked by google news"
    if any(marker in lowered for marker in _BOOK_WEBSITE_BLOCK_MARKERS):
        return "blocked by website"
    if _BOOK_BLOCK_HTTP_RE.search(lowered):
        return "blocked by website"
    return ""

def _book_paragraph_block_reason(paragraphs: list[str]) -> str:
    """Detect a block/paywall page accidentally returned as article text."""
    values = [str(paragraph) for paragraph in paragraphs if paragraph]
    if not values:
        return ""
    reason = _book_block_reason(" ".join(values[:3]))
    if not reason:
        return ""
    # A substantive article can mention a paywall or bot challenge. Only treat
    # a short extracted body as an interstitial rather than the story itself.
    if len(" ".join(values)) > 1_200:
        return ""
    return reason

class WriteABookOptions(NamedTuple):
    """All customizable /write_a_book settings after argument parsing."""

    subject: str
    words_per_chapter: int
    chapter_count: int
    sub_question_count: int
    sub_question_article_limit: int
    model: str
    directory: Path

_WRITE_A_BOOK_NUMERIC_OPTIONS = {
    "words": "words",
    "words-per-chapter": "words",
    "chapters": "chapters",
    "sub-questions": "sub_questions",
    "sub-question-articles": "sub_question_articles",
    "sub-questions-article-limit": "sub_question_articles",
}

_WRITE_A_BOOK_STRING_OPTIONS = {
    "model": "model",
    "book-model": "model",
    "directory": "directory",
    "book-directory": "directory",
}

# task.remove.articles_to_learn: the separate articles-to-learn budget was
# removed in favor of the two planning settings. Accept the old flag silently
# so an existing command line cannot accidentally turn its value into part of
# the book subject.
_WRITE_A_BOOK_IGNORED_OPTIONS = {"articles", "articles-to-learn"}

WRITE_A_BOOK_KEYWORD_PROMPT = """You are a research librarian planning a
book about the following subject:

{subject}

Produce exactly {limit} web-search queries that would find recent, factual
articles useful for writing this book. Each query is one sub-question the
book should answer. Cover the main angles, not just synonyms of the subject.

Rules:
- Return one search query per line.
- Do not number the lines, add bullets, or write commentary.
- Keep every query under 100 characters.
- Do not include the year 2024 or any older year in a query. If a year makes
  the query more specific, use 2025 or 2026.
"""

WRITE_A_BOOK_OUTLINE_PROMPT = """You are the editor of a researched
non-fiction book.

Book subject: {subject}

Recent research material:
{digest}

Design a focused book with exactly {chapters} chapters. Each chapter should
cover a distinct, useful part of the subject and should be supported by the
research material above.

Rules:
- Return exactly {chapters} chapter titles, one per line.
- Do not number the lines, add bullets, or write commentary.
- Do not include a preface, table of contents, or sources chapter.
- Keep every title under 90 characters.
"""

WRITE_A_BOOK_CHAPTER_PROMPT = """You are writing one chapter of a researched
non-fiction book.

Book subject: {subject}
Chapter {number}: {title}

Use the research excerpts below as the factual basis. You may explain and
synthesize them, but do not invent names, numbers, dates, or events that are
not supported by the excerpts. Write for a general, curious reader.

All of the excerpts and the numbered references below belong to one connected
research corpus. Do not treat them as unrelated documents: look for links
between different sources, combine complementary facts, notice tensions, and
use creative synthesis to develop ideas that no single source states on its
own. Clearly mark interpretation or speculation as such.

Rules:
- Return only the chapter prose. Do not write a chapter title, heading,
  preface, summary of this prompt, notes, or a source list.
- Use flowing paragraphs, not bullet lists unless a list is genuinely clearer.
- Aim for about {target_words} words. The chapter must never exceed
  {max_words} words.

All research references:
{references}

Research excerpts:
{evidence}

Chapter prose:
"""

def _book_filename(subject: str) -> str:
    """Return a safe, human-readable HTML filename for one book."""
    name = re.sub(r'[\x00-\x1f\x7f/\\:*?"<>|]+', "_", subject).strip(" .")
    name = re.sub(r"\s+", " ", name)[:120].rstrip(" .")
    return f"{name or 'book'}.html"

def _resolve_book_directory(directory: str | Path | None = None) -> Path:
    """Resolve a book directory, defaulting to ./.chat/books."""
    if directory in (None, ""):
        return WRITE_A_BOOK_DIR
    path = Path(os.path.expanduser(str(directory)))
    if not path.is_absolute():
        path = SCRIPT_DIR / path
    return path.resolve()

def written_book_path(
    name: str, directory: str | Path | None = None
) -> Path | None:
    """Resolve a book filename inside a book directory without traversal."""
    name = str(name or "").strip()
    if (
        not name
        or "/" in name
        or "\\" in name
        or name != Path(name).name
        or not name.lower().endswith(".html")
    ):
        return None
    book_dir = _resolve_book_directory(directory)
    path = (book_dir / name).resolve()
    try:
        path.relative_to(book_dir)
    except ValueError:
        return None
    return path

def _book_title_from_html(path: Path) -> str:
    """Read the <title> of a written book, falling back to its filename."""
    try:
        with path.open("r", encoding="utf-8", errors="replace") as handle:
            head = handle.read(8192)
    except OSError:
        return ""
    match = re.search(
        r"<title[^>]*>(.*?)</title>", head, re.IGNORECASE | re.DOTALL
    )
    if match is None:
        return ""
    return html.unescape(re.sub(r"\s+", " ", match.group(1))).strip()

def list_written_books(directory: str | Path | None = None) -> list[dict]:
    """List the HTML books written by /write_a_book, newest first."""
    book_dir = _resolve_book_directory(directory)
    if not book_dir.is_dir():
        return []
    entries: list[dict] = []
    for path in book_dir.iterdir():
        try:
            if not path.is_file() or path.suffix.lower() != ".html":
                continue
            stat = path.stat()
        except OSError:
            continue
        name = path.name
        entries.append(
            {
                "id": name,
                "name": name,
                "title": _book_title_from_html(path) or path.stem,
                "url": f"/api/book_file?name={quote(name)}",
                "size": stat.st_size,
                "mtime": stat.st_mtime,
                "modified": time.strftime(
                    "%Y-%m-%d %H:%M", time.localtime(stat.st_mtime)
                ),
            }
        )
    entries.sort(key=lambda item: item["mtime"], reverse=True)
    return entries

def pdf_book_path(name: str) -> Path | None:
    """Resolve one PDF book inside .chat/pdf_books without traversal."""
    name = str(name or "").strip()
    if (
        not name
        or "/" in name
        or "\\" in name
        or name != Path(name).name
        or not name.lower().endswith(".pdf")
    ):
        return None
    book_dir = PDF_BOOKS_DIR.resolve()
    path = (book_dir / name).resolve()
    try:
        path.relative_to(book_dir)
    except ValueError:
        return None
    return path

def _pdf_book_title(name: str) -> str:
    """Use a readable filename stem as the PDF book title."""
    stem = re.sub(r"[_-]+", " ", Path(name).stem)
    return " ".join(stem.split()) or Path(name).stem

def _load_pdf_last_open() -> dict[str, float]:
    """Read the persisted PDF last-open timestamps, tolerating bad files."""
    try:
        raw = json.loads(PDF_BOOKS_LAST_OPEN_PATH.read_text(encoding="utf-8"))
    except (OSError, ValueError, json.JSONDecodeError):
        return {}
    if not isinstance(raw, dict):
        return {}
    result: dict[str, float] = {}
    for name, value in raw.items():
        try:
            result[str(name)] = float(value)
        except (TypeError, ValueError):
            continue
    return result


def record_pdf_open(name: str) -> None:
    """Record that one PDF was opened in the reader."""
    path = pdf_book_path(name)
    if path is None:
        return
    with _PDF_LAST_OPEN_LOCK:
        mapping = _load_pdf_last_open()
        mapping[path.name] = time.time()
        try:
            PDF_BOOKS_LAST_OPEN_PATH.parent.mkdir(
                parents=True, exist_ok=True
            )
            temporary = PDF_BOOKS_LAST_OPEN_PATH.with_name(
                PDF_BOOKS_LAST_OPEN_PATH.name + ".tmp"
            )
            temporary.write_text(
                json.dumps(mapping, ensure_ascii=False, indent=2) + "\n",
                encoding="utf-8",
            )
            temporary.replace(PDF_BOOKS_LAST_OPEN_PATH)
        except OSError:
            pass


def list_pdf_books() -> list[dict]:
    """List every PDF in .chat/pdf_books by last open time, newest first."""
    if not PDF_BOOKS_DIR.is_dir():
        return []
    last_open_map = _load_pdf_last_open()
    entries: list[dict] = []
    for path in PDF_BOOKS_DIR.iterdir():
        try:
            if not path.is_file() or path.suffix.lower() != ".pdf":
                continue
            stat = path.stat()
        except OSError:
            continue
        name = path.name
        last_open = last_open_map.get(name, 0.0)
        entries.append(
            {
                "id": name,
                "name": name,
                "title": _pdf_book_title(name),
                "url": f"/api/pdf_book_file?name={quote(name)}",
                "size": stat.st_size,
                "mtime": stat.st_mtime,
                "modified": time.strftime(
                    "%Y-%m-%d %H:%M", time.localtime(stat.st_mtime)
                ),
                "last_open": last_open,
                "last_opened": (
                    time.strftime(
                        "%Y-%m-%d %H:%M", time.localtime(last_open)
                    )
                    if last_open
                    else ""
                ),
            }
        )
    entries.sort(
        key=lambda item: (item["last_open"] or item["mtime"], item["mtime"]),
        reverse=True,
    )
    return entries

# ---------------------------------------------------------------------------
# Ocean Of PDF (prompts/pdf_books.prompt task.oceans_of_pdf_search).
#
# https://oceanofpdf.com/ answers with a Cloudflare challenge to plain
# server-side requests (403), so the catalogue is read from the publisher's
# oceanofpdf.com.co mirror, which serves the same search index without the
# bot check.  Book pages expose their download buttons as forms posting to
# https://zerolibrary.xyz/<token>; POSTing a token redirects to the signed
# file URL that streams the PDF.
OCEAN_OF_PDF_BASE = "https://oceanofpdf.com.co"
OCEAN_ZEROLIBRARY_FORM_RE = re.compile(
    r"action=\"https://zerolibrary\.xyz/([0-9a-f]{8,64})\"", re.IGNORECASE
)
OCEAN_FILE_SIZE_LIMIT = 400 * 1024 * 1024
OCEAN_HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
        "AppleWebKit/537.36 (KHTML, like Gecko) "
        "Chrome/124.0 Safari/537.36"
    ),
}

_OCEAN_NON_BOOK_RE = re.compile(
    r"/(?:category|tag|author|page|wp-|feed|contact-us|dmca|disclaimer|"
    r"privacy-policy|cookie-policy|books-categories|recently-added-books|"
    r"what-are-the-best)/",
    re.IGNORECASE,
)


def _ocean_get(url: str) -> str:
    try:
        response = requests.get(
            url, headers=OCEAN_HEADERS, timeout=(10, 60)
        )
        response.raise_for_status()
    except requests.RequestException as exc:
        raise RuntimeError(f"Could not reach {url}: {exc}") from exc
    return response.text


def _ocean_title(fragment: str) -> str:
    return html.unescape(re.sub(r"<[^>]+>", "", fragment)).strip()


def _ocean_title_key(value: str) -> str:
    """Return a collision-safe key for matching titles across the two sites."""
    return re.sub(r"[^a-z0-9]+", " ", str(value or "").casefold()).strip()


def _ollama_web_fetch(url: str) -> dict | None:
    """Fetch a URL through the local Ollama experimental web_fetch API.

    The live https://oceanofpdf.com/ pages answer with a Cloudflare challenge
    to plain requests, so the live search runs through this fetcher (its
    browser-like backend passes the bot check).  Returns None on any failure
    so callers gracefully fall back to the mirror.
    """
    try:
        response = requests.post(
            f"{chat.OLLAMA_HOST}/api/experimental/web_fetch",
            json={"url": url},
            timeout=(5, 120),
        )
        if response.status_code != 200:
            return None
        payload = response.json()
    except (requests.RequestException, ValueError, OSError):
        return None
    return payload if isinstance(payload, dict) else None


_OCEAN_LIVE_LINK_RE = re.compile(
    r"^https://oceanofpdf\.com/(?!category/|tag/|page/|feed/|wp-)[^\s\"'#]+",
)
_OCEAN_DATE_RE = re.compile(
    r"\b([A-Z][a-z]+ [0-9]{1,2}, [0-9]{4})\b"
)


def _ocean_live_results(content: str, links: list[str]) -> list[dict]:
    """Parse the live site's search markdown into result rows.

    The fetcher strips download buttons from a book page, and it exposes at
    most a handful of link tags, so the first results in reading order pick
    their book-page URL from the link pool.
    """
    if not content or "Search Results" not in content:
        return []
    segments = [
        part.strip() for part in content.split("\n## ") if part.strip()
    ]
    pool: list[str] = []
    seen_urls: set[str] = set()
    for link in links or []:
        link = html.unescape(str(link)).split("#", 1)[0].strip()
        if (
            link
            and link not in seen_urls
            and _OCEAN_LIVE_LINK_RE.match(link)
            and not _OCEAN_NON_BOOK_RE.search(link)
        ):
            seen_urls.add(link)
            pool.append(link)
    results: list[dict] = []
    seen_keys: set[str] = set()
    for segment in segments[1:]:
        lines = [line.strip() for line in segment.splitlines() if line.strip()]
        if not lines:
            continue
        title = lines[0].lstrip("#").strip()
        if not title or "download" not in segment.casefold():
            continue
        key = _ocean_title_key(title)
        if not key or key in seen_keys:
            continue
        seen_keys.add(key)
        date = ""
        details = ""
        for line in lines[1:]:
            if not date:
                date_match = _OCEAN_DATE_RE.search(line)
                if date_match:
                    date = date_match.group(1)
            if line.startswith("Author:"):
                details = line
            if line.lower().startswith("[read more"):
                break
        url = pool.pop(0) if pool else ""
        results.append(
            {
                "title": title,
                "url": url,
                "published": date,
                "details": details,
                "source": "live",
            }
        )
        if len(results) >= 12:
            break
    return results


def search_ocean_of_pdf(keyword: str) -> list[dict]:
    """Search the Ocean Of PDF catalogue and return downloadable books."""
    keyword = " ".join(str(keyword or "").split())
    if not keyword:
        return []
    page = _ocean_get(f"{OCEAN_OF_PDF_BASE}/?s={quote_plus(keyword)}")
    matches = list(
        re.finditer(
            r'<h2 class="entry-title"[^>]*>\s*<a[^>]+href="([^"]+)"[^>]*>'
            r"(.*?)</a>",
            page,
            re.DOTALL,
        )
    )
    results: list[dict] = []
    seen: set[str] = set()
    for index, match in enumerate(matches):
        block = page[
            match.start() : matches[index + 1].start()
            if index + 1 < len(matches)
            else match.end() + 6000
        ]
        url = html.unescape(match.group(1)).strip()
        title = _ocean_title(match.group(2))
        if not title or url in seen or _OCEAN_NON_BOOK_RE.search(url):
            continue
        # Non-book pages (guides, listicles, notices) never advertise a
        # free downloadable PDF.
        if "download" not in block.casefold():
            continue
        flat = html.unescape(re.sub(r"\s+", " ", block))
        date_match = re.search(
            r'datetime="([0-9]{4}-[0-9]{2}-[0-9]{2})', flat
        )
        details = [
            _ocean_title(item)
            for item in re.findall(r"<li>(.*?)</li>", flat, re.DOTALL)
        ]
        seen.add(url)
        results.append(
            {
                "index": len(results) + 1,
                "url": url,
                "title": title,
                "published": date_match.group(1) if date_match else "",
                "details": " \u00b7 ".join(details[:8]),
            }
        )
        if len(results) >= 12:
            break

    # prompts/pdf_books.prompt task.bug.six_math_essentials: the mirror's
    # search index lags the live site (newer books such as Six Math
    # Essentials appear only on oceanofpdf.com), so live results are merged
    # in through the Ollama web fetch; titles already found on the mirror
    # collapse into one row.
    live_fetch = _ollama_web_fetch(
        f"https://oceanofpdf.com/?s={quote_plus(keyword)}"
    )
    live_results = (
        _ocean_live_results(
            str(live_fetch.get("content") or ""),
            list(live_fetch.get("links") or []),
        )
        if live_fetch
        else []
    )
    for row in live_results:
        key = _ocean_title_key(row["title"])
        merged = next(
            (
                item
                for item in results
                if _ocean_title_key(item["title"]) == key
            ),
            None,
        )
        if merged is not None:
            merged["details"] = merged["details"] or row["details"]
            merged["published"] = merged["published"] or row["published"]
            continue
        results.append(
            {
                "index": len(results) + 1,
                "url": row["url"],
                "title": row["title"],
                "published": row["published"],
                "details": row["details"],
                "source": "live",
            }
        )
    for index, row in enumerate(results, 1):
        row["index"] = index
    return results[:12]


def _ocean_safe_filename(name: str) -> str:
    stem = unquote(str(name or "")).strip()
    # The file hosts their own branding prefixes (e.g. "Allepub.com___")
    # before the real book filename.
    stem = re.sub(
        r"^[A-Za-z0-9]+\.(?:com|net|org|xyz|com\.co)[_\-\s]*", "", stem
    )
    if stem.casefold().endswith(".pdf"):
        stem = stem[:-4]
    stem = re.sub(r"[^A-Za-z0-9. _-]+", "_", stem)
    stem = re.sub(r"_{2,}", "_", stem).strip("._ -")[:120]
    return f"{stem or 'ocean_of_pdf_book'}.pdf"


def _ocean_pdf_token(page: str, book_url: str) -> str:
    """Return the PDF download token from one book page."""
    found: list[tuple[str, str]] = []
    for match in OCEAN_ZEROLIBRARY_FORM_RE.finditer(page):
        prefix = page[max(0, match.start() - 400) : match.start()].upper()
        if "EPUB" in prefix.rsplit("PDF", 1)[0][-200:]:
            label = "EPUB"
        elif "PDF" in prefix[-200:]:
            label = "PDF"
        else:
            label = ""
        found.append((label, match.group(1)))
    wanted = next(
        (token for label, token in found if label == "PDF"), None
    )
    if wanted is None and len(found) == 1:
        wanted = found[0][1]
    if wanted is None and len(found) >= 2:
        wanted = found[-1][1]
    if not wanted:
        raise ValueError(
            f"No PDF download form was found on {book_url}."
        )
    return wanted


def _ocean_mirror_page_for_live_book(book_url: str, title: str) -> str:
    """Find the same book's page on the mirror by title, if it exists.

    The live site's pages answer with a Cloudflare challenge to plain
    requests, so downloads for live-only listings resolve through the
    mirror, which shares the download-button infrastructure.
    """
    title = str(title or "").strip()
    if not title:
        return ""
    page = _ocean_get(f"{OCEAN_OF_PDF_BASE}/?s={quote_plus(title)}")
    wanted = _ocean_title_key(title)
    for url_match in re.finditer(
        r'<h2 class="entry-title"[^>]*>\s*<a[^>]+href="([^"]+)"[^>]*>(.*?)</a>',
        page,
        re.DOTALL,
    ):
        url = html.unescape(url_match.group(1)).strip()
        candidate = _ocean_title_key(_ocean_title(url_match.group(2)))
        if url and candidate and candidate == wanted:
            return url
    return ""


# ---------------------------------------------------------------------------
# Alternate download source for live-only Ocean Of PDF listings
# (prompts/pdf_books.prompt task.bug.book_not_found).
#
# Some books exist on oceanofpdf.com but not on the mirror, so the live page's
# zerolibrary form token cannot be read without a Cloudflare-capable browser.
# Library Genesis indexes many of those same files and exposes a public IPFS
# CID.  The fallback searches Libgen by title, reads the CID from the file
# page, and downloads it through a public IPFS gateway.
LIBGEN_SEARCH_URL = "https://libgen.li/index.php"
LIBGEN_FILE_URL = "https://libgen.li/file.php"
LIBGEN_IPFS_GATEWAYS = (
    "https://ipfs.filebase.io/ipfs/",
    "https://gateway.pinata.cloud/ipfs/",
)
_LIBGEN_IPFS_RE = re.compile(r"/ipfs/((?:bafy|Qm)[A-Za-z0-9]+)")


def _libgen_title(value: str) -> str:
    """Normalise a Libgen title and drop Ocean Of PDF's format prefixes."""
    title = html.unescape(re.sub(r"<[^>]+>", " ", str(value or "")))
    title = re.sub(r"^\s*\[(?:pdf|epub)\]\s*", "", title, flags=re.I)
    return " ".join(title.split())


def _libgen_get(url: str) -> str:
    """Fetch a Libgen page, retrying the occasionally slow file pages."""
    last_error: Exception | None = None
    for attempt in range(3):
        try:
            response = requests.get(
                url, headers=OCEAN_HEADERS, timeout=(10, 90)
            )
            response.raise_for_status()
            return response.text
        except requests.RequestException as exc:
            last_error = exc
            if attempt < 2:
                time.sleep(1.5 * (attempt + 1))
    raise RuntimeError(f"Could not reach {url}: {last_error}")


def _libgen_candidates(title: str) -> list[dict]:
    """Return Libgen PDF rows ranked by title overlap with ``title``."""
    wanted = _ocean_title_key(_libgen_title(title))
    wanted_tokens = set(wanted.split())
    if not wanted_tokens:
        return []
    page = _libgen_get(f"{LIBGEN_SEARCH_URL}?req={quote_plus(title)}")
    candidates: list[dict] = []
    for row_match in re.finditer(r"<tr[^>]*>(.*?)</tr>", page, re.S | re.I):
        row = row_match.group(1)
        md5_match = re.search(r"ads\.php\?md5=([0-9a-f]{32})", row, re.I)
        if not md5_match:
            continue
        row_text = _libgen_title(row).casefold()
        if "pdf" not in row_text:
            continue
        title_match = re.search(
            r'<a\b.*?href="edition\.php\?id=\d+".*?>(.*?)</a>',
            row,
            re.S | re.I,
        )
        if not title_match:
            continue
        candidate_title = _libgen_title(title_match.group(1))
        candidate_key = _ocean_title_key(candidate_title)
        candidate_tokens = set(candidate_key.split())
        if not candidate_tokens:
            continue
        coverage = len(wanted_tokens & candidate_tokens) / len(wanted_tokens)
        if coverage < 0.5:
            continue
        score = coverage
        if candidate_key == wanted:
            score += 2
        elif wanted in candidate_key or candidate_key in wanted:
            score += 1
        if "english" in row_text:
            score += 0.25
        file_match = re.search(r"/file\.php\?id=(\d+)", row)
        candidates.append(
            {
                "title": candidate_title,
                "md5": md5_match.group(1),
                "file_id": file_match.group(1) if file_match else "",
                "score": score,
            }
        )
    candidates.sort(key=lambda item: item["score"], reverse=True)
    return candidates


def _libgen_ipfs_cid(file_id: str, md5: str) -> str:
    """Read a Libgen file page and return its public IPFS CID."""
    if file_id:
        page = _libgen_get(f"{LIBGEN_FILE_URL}?id={file_id}")
    else:
        page = _libgen_get(f"{LIBGEN_SEARCH_URL}?req={quote_plus(md5)}")
    cids = _LIBGEN_IPFS_RE.findall(page)
    preferred = [cid for cid in cids if cid.startswith("bafy")]
    return (preferred or cids or [""])[0]


def _download_libgen_pdf(title: str) -> dict:
    """Download a live-only Ocean Of PDF book from Libgen's IPFS copy."""
    title = _libgen_title(title)
    if not title:
        raise RuntimeError("The book title is empty, so no alternate source could be searched.")
    candidates = _libgen_candidates(title)
    if not candidates:
        raise RuntimeError("No matching PDF was found in Library Genesis.")
    last_error = ""
    for candidate in candidates[:3]:
        try:
            target = PDF_BOOKS_DIR / _ocean_safe_filename(candidate["title"])
            if target.is_file():
                try:
                    with target.open("rb") as handle:
                        header = handle.read(5)
                    if header.startswith(b"%PDF-"):
                        entries = list_pdf_books()
                        item = next(
                            (entry for entry in entries if entry["name"] == target.name),
                            None,
                        )
                        return {
                            "downloaded": False,
                            "already_downloaded": True,
                            "item": item,
                            "source_url": f"https://libgen.li/file.php?id={candidate['file_id']}",
                        }
                except OSError:
                    pass
            cid = _libgen_ipfs_cid(candidate["file_id"], candidate["md5"])
            if not cid:
                raise RuntimeError("The Library Genesis record has no IPFS copy.")
            PDF_BOOKS_DIR.mkdir(parents=True, exist_ok=True)
            part = target.with_name(target.name + ".part")
            last_error = ""
            for gateway in LIBGEN_IPFS_GATEWAYS:
                try:
                    with requests.get(
                        f"{gateway}{cid}",
                        headers={**OCEAN_HEADERS, "Accept": "application/pdf,*/*"},
                        stream=True,
                        timeout=(10, 300),
                        allow_redirects=True,
                    ) as response:
                        response.raise_for_status()
                        total = 0
                        with part.open("wb") as handle:
                            for chunk in response.iter_content(chunk_size=65536):
                                if not chunk:
                                    continue
                                if total == 0 and not chunk.lstrip()[:5].startswith(b"%PDF-"):
                                    raise RuntimeError("The alternate source did not return a PDF.")
                                total += len(chunk)
                                if total > OCEAN_FILE_SIZE_LIMIT:
                                    raise RuntimeError("The alternate PDF is too large to download.")
                                handle.write(chunk)
                    if not part.is_file() or part.stat().st_size == 0:
                        raise RuntimeError("The alternate PDF download was empty.")
                    os.replace(part, target)
                    entries = list_pdf_books()
                    item = next(
                        (entry for entry in entries if entry["name"] == target.name),
                        None,
                    )
                    return {
                        "downloaded": True,
                        "already_downloaded": False,
                        "item": item,
                        "source_url": f"https://libgen.li/file.php?id={candidate['file_id']}",
                    }
                except (requests.RequestException, RuntimeError, OSError) as exc:
                    last_error = str(exc)
                    try:
                        part.unlink(missing_ok=True)
                    except OSError:
                        pass
        except (requests.RequestException, RuntimeError, OSError, ValueError) as exc:
            last_error = str(exc)
    raise RuntimeError(last_error or "The alternate source could not download this book.")


def download_ocean_of_pdf_book(book_url: str, title: str = "") -> dict:
    """Download one Ocean Of PDF book into {pdf_books_dir}.

    Book pages come from either the live oceanofpdf.com or the
    oceanofpdf.com.co mirror; the mirror always serves plain HTML, while a
    live URL resolves through the mirror's title search.
    """
    host = urlparse(str(book_url or "")).netloc
    if host not in {"oceanofpdf.com", "oceanofpdf.com.co"}:
        raise ValueError("Not an Ocean Of PDF book page URL.")
    if host == "oceanofpdf.com":
        mirror_url = _ocean_mirror_page_for_live_book(book_url, title)
        if not mirror_url:
            # This listing only exists on the live site.  Resolve the same
            # title through Libgen instead of failing on the unavailable
            # zerolibrary form token.
            return _download_libgen_pdf(title)
        book_url = mirror_url
    page = _ocean_get(book_url)
    token = _ocean_pdf_token(page, book_url)
    try:
        response = requests.post(
            f"https://zerolibrary.xyz/{token}",
            headers=OCEAN_HEADERS,
            stream=True,
            timeout=(10, 300),
            allow_redirects=True,
        )
        response.raise_for_status()
    except requests.RequestException as exc:
        raise RuntimeError(f"Could not start the download: {exc}") from exc
    filename = _ocean_safe_filename(
        unquote(urlparse(response.url).path.rpartition("/")[2])
    )
    PDF_BOOKS_DIR.mkdir(parents=True, exist_ok=True)
    target = PDF_BOOKS_DIR / filename
    part = target.with_name(target.name + ".part")
    total = 0
    try:
        with part.open("wb") as handle:
            for chunk in response.iter_content(chunk_size=65536):
                if not chunk:
                    continue
                if total == 0 and not chunk.lstrip()[:5].startswith(b"%PDF-"):
                    raise RuntimeError(
                        "That Ocean Of PDF download did not return a PDF."
                    )
                total += len(chunk)
                if total > OCEAN_FILE_SIZE_LIMIT:
                    raise RuntimeError(
                        "The Ocean Of PDF file is too large to download."
                    )
                handle.write(chunk)
    except Exception:
        try:
            part.unlink(missing_ok=True)
        except OSError:
            pass
        raise
    if not part.is_file() or part.stat().st_size == 0:
        try:
            part.unlink(missing_ok=True)
        except OSError:
            pass
        raise RuntimeError("The Ocean Of PDF download was empty.")
    os.replace(part, target)
    entries = list_pdf_books()
    item = next(
        (entry for entry in entries if entry["name"] == target.name),
        None,
    )
    return {
        "downloaded": True,
        "already_downloaded": False,
        "item": item,
        "source_url": book_url,
    }

# Page extraction opens the PDF once per file version. pypdf extracts one page
# in milliseconds after that, so navigation can stay page-by-page without
# rebuilding the whole book text for every click.
_PDF_BOOK_READERS: dict[tuple[str, float], tuple[object, int]] = {}

_PDF_BOOK_READER_LOCK = threading.Lock()

_PDF_BOOK_WORDS: dict[tuple[str, float, int], list[dict]] = {}

_PDF_BOOK_CHAPTERS: dict[tuple[str, float], list[dict]] = {}

def _pdf_book_reader(path: Path) -> tuple[object, int]:
    """Return a cached pypdf reader and its page count."""
    try:
        from pypdf import PdfReader
    except ImportError as exc:
        raise RuntimeError(
            "PDF support requires pypdf. Install it with pip install pypdf."
        ) from exc
    key = (str(path), path.stat().st_mtime)
    with _PDF_BOOK_READER_LOCK:
        cached = _PDF_BOOK_READERS.get(key)
        if cached is not None:
            return cached
        # Drop stale readers for this same book when its file changes.
        for old_key in list(_PDF_BOOK_READERS):
            if old_key[0] == key[0] and old_key != key:
                del _PDF_BOOK_READERS[old_key]
        reader = PdfReader(str(path))
        cached = (reader, len(reader.pages))
        _PDF_BOOK_READERS[key] = cached
        return cached

def _pdf_page_paragraphs(text: str) -> list[str]:
    """Join PDF line wraps into readable paragraphs for one page."""
    normalized = str(text or "").replace("\r\n", "\n").replace("\r", "\n")
    paragraphs: list[str] = []
    current: list[str] = []
    for raw in normalized.split("\n"):
        line = " ".join(raw.split())
        if line:
            current.append(line)
            continue
        if current:
            paragraphs.append(" ".join(current))
            current = []
    if current:
        paragraphs.append(" ".join(current))
    # Bare page numbers and repeated running headers are page furniture.
    cleaned = [
        paragraph
        for paragraph in paragraphs
        if paragraph and not re.fullmatch(r"\d{1,4}", paragraph)
    ]
    if cleaned:
        return cleaned
    return ["This page has no extractable text."]

_PDF_LAYOUT_BULLET_RE = re.compile(r"^([¡•●‣▪◦·]|[-*+])\s+(.*)$")

_PDF_LAYOUT_NUMBERED_RE = re.compile(r"^(\d{1,2}[.)]|\d{1,2}\s{2,})\s*(.*)$")

_PDF_LAYOUT_SECTION_RE = re.compile(
    r"^(\d+(?:\.\d+)+)\s{2,}(.+)$|^(\d+)\s{2,}(.+)$"
)

_PDF_LAYOUT_CAPTION_RE = re.compile(
    r"^(Listing|Figure|Table)\s+\d+(?:\.\d+)?\b", re.IGNORECASE
)

_PDF_LAYOUT_CODE_RE = re.compile(
    r"^\s*(?:"
    r"def\s+\w+|class\s+\w+|import\s+|from\s+\S+\s+import\s+|"
    r"return\b|yield\b|raise\b|try:|except\b|finally:|"
    r"with\s+.+:|if\s+.+:|elif\s+.+:|else:|for\s+.+ in\s+.+:|"
    r"while\s+.+:|[A-Za-z_][\w.\[\]\'\"]*\s*=\s*|#\s|"
    r"[)}{\]\[]|>>>|\.\.\.|"
    r"(?:pip|python3?|uv|git|curl|npm|ollama)\s|"
    r"print\(|console\.log\(|SELECT\b|INSERT\b|UPDATE\b|DELETE\b"
    r")"
)

def _pdf_page_layout_text(path: Path, page: int) -> str:
    """Return one PDF page's text with poppler's column layout preserved."""
    try:
        result = subprocess.run(
            [
                "pdftotext",
                "-f",
                str(page),
                "-l",
                str(page),
                "-layout",
                "-enc",
                "UTF-8",
                str(path),
                "-",
            ],
            capture_output=True,
            text=True,
            timeout=30,
        )
    except (FileNotFoundError, subprocess.SubprocessError):
        return ""
    if result.returncode != 0:
        return ""
    return result.stdout.replace("\f", "\n")

def _pdf_join_lines(left: str, right: str) -> str:
    """Join two visual PDF lines, repairing a hyphenated word break."""
    left = left.rstrip()
    right = right.lstrip()
    if not left:
        return right
    if not right:
        return left
    match = re.search(r"(\w)\s*-\s*$", left)
    if match:
        if right[:1].islower():
            return left[: match.start()] + match.group(1) + right
        return left[: match.start()] + match.group(1) + "-" + right
    return f"{left} {right}"

def _pdf_layout_word_groups(line: str) -> list[list]:
    """Group words separated by two or more spaces into table columns."""
    words = [
        (match.start(), match.end(), match.group(0))
        for match in re.finditer(r"\S+", line)
    ]
    groups: list[list] = []
    for start, end, word in words:
        if not groups or start - groups[-1][1] >= 2:
            groups.append([start, end, word])
        else:
            groups[-1][1] = end
            groups[-1][2] = f"{groups[-1][2]} {word}"
    return groups

def _pdf_layout_table_markdown(raw_lines: list[str]) -> str:
    """Turn one layout-preserved table block into a Markdown pipe table."""
    lines = [line.rstrip() for line in raw_lines if line.strip()]
    if len(lines) < 2:
        return ""
    header_groups = _pdf_layout_word_groups(lines[0])
    if len(header_groups) < 2:
        return ""
    header = [str(group[2]) for group in header_groups]
    boundaries = [
        (header_groups[index][1] + header_groups[index + 1][0]) / 2
        for index in range(len(header_groups) - 1)
    ]
    rows: list[list[str]] = []
    for line in lines[1:]:
        cells = [""] * len(header)
        for match in re.finditer(r"\S+", line):
            position = match.start()
            word = match.group(0)
            column = len(boundaries)
            for index, boundary in enumerate(boundaries):
                if position < boundary:
                    column = index
                    break
            cells[column] = _pdf_join_lines(cells[column], word)
        nonempty = [index for index, cell in enumerate(cells) if cell]
        if not nonempty:
            continue
        if (
            rows
            and cells[0]
            and rows[-1][0].lower().endswith(
                ("to", "and", "or", "of", "the", "-", ",", "/")
            )
        ):
            for index in nonempty:
                rows[-1][index] = _pdf_join_lines(
                    rows[-1][index], cells[index]
                )
            continue
        if rows and len(nonempty) == 1 and nonempty[0] > 0:
            index = nonempty[0]
            rows[-1][index] = _pdf_join_lines(rows[-1][index], cells[index])
            continue
        rows.append(cells)
    if not rows:
        return ""

    def cell(value: str) -> str:
        return str(value).replace("|", "\\|").strip()

    output = [
        "| " + " | ".join(cell(value) for value in header) + " |",
        "| " + " | ".join("---" for _value in header) + " |",
    ]
    for row in rows:
        output.append("| " + " | ".join(cell(value) for value in row) + " |")
    return "\n".join(output)

def _pdf_layout_page_markdown(text: str, page_number: int = 0) -> str:
    """Format one layout-preserved PDF page as readable Markdown."""
    raw_lines = str(text or "").replace("\f", "\n").splitlines()
    items: list[tuple[int, str, str] | None] = []
    first_nonblank = False
    for raw_line in raw_lines:
        raw_line = raw_line.rstrip()
        if not raw_line.strip():
            items.append(None)
            continue
        indent = len(raw_line) - len(raw_line.lstrip(" "))
        line = raw_line.strip()
        if re.fullmatch(r"\d{1,4}", line):
            continue
        if re.match(
            r"(?i)^(licensed to|all rights reserved|©|copyright)\b", line
        ):
            continue
        if not first_nonblank:
            first_nonblank = True
            drop = False
            if line.endswith("-"):
                drop = False
            elif (
                indent >= 15
                and len(line.split()) <= 15
                and not line.endswith((".", "?", "!", ":", ";"))
            ):
                drop = True
            elif re.match(rf"^{page_number}\s{{2,}}\S", line):
                drop = True
            elif re.match(
                r"^\d{1,4}\s{2,}(Chapter|Contents|Index)\b", line, re.I
            ):
                drop = True
            elif (
                re.match(r"^Chapter\s+\d+\b", line, re.I)
                and len(line) <= 110
            ):
                drop = True
            if drop:
                continue
        items.append((indent, line, raw_line))

    indents: list[int] = []
    for item in items:
        if item is None:
            continue
        indent, line, _raw = item
        if indent <= 0 or len(line) < 30:
            continue
        if (
            _PDF_LAYOUT_BULLET_RE.match(line)
            or _PDF_LAYOUT_NUMBERED_RE.match(line)
            or _PDF_LAYOUT_CAPTION_RE.match(line)
        ):
            continue
        if _PDF_LAYOUT_CODE_RE.match(line):
            continue
        indents.append(indent)
    base_indent = min(indents) if indents else 6

    output: list[str] = []
    paragraph: str | None = None
    paragraph_indent = 0
    bullet: str | None = None
    bullet_indent = 0
    code: list[str] | None = None
    code_indent = 0

    def flush_paragraph() -> None:
        nonlocal paragraph
        if paragraph is not None:
            output.append(paragraph)
            paragraph = None

    def flush_bullet() -> None:
        nonlocal bullet
        if bullet is not None:
            output.append(bullet)
            bullet = None

    def flush_code() -> None:
        nonlocal code, code_indent
        if code is None:
            return
        body = "\n".join(code).rstrip()
        if body.strip():
            language = "python" if re.search(
                r"\b(def|class|import|from)\b", body
            ) else ""
            output.append(f"```{language}\n{body}\n```")
        code = None
        code_indent = 0

    def flush_all() -> None:
        flush_paragraph()
        flush_bullet()
        flush_code()

    index = 0
    while index < len(items):
        item = items[index]
        if item is None:
            flush_all()
            index += 1
            continue
        indent, line, raw_line = item

        if code is not None:
            if indent > base_indent or _PDF_LAYOUT_CODE_RE.match(line):
                code.append(" " * max(0, indent - code_indent) + line)
                index += 1
                continue
            flush_code()

        section = _PDF_LAYOUT_SECTION_RE.match(line)
        heading = False
        if section:
            required = base_indent + 2 if section.group(1) else base_indent
            if indent <= required and len(line) <= 120:
                heading = True
        if (
            not heading
            and indent <= max(0, base_indent - 2)
            and len(line) <= 100
            and not line.endswith((".", "?", "!", ":", ";", ","))
            and not line[:1].islower()
            and not _PDF_LAYOUT_BULLET_RE.match(line)
            and not _PDF_LAYOUT_NUMBERED_RE.match(line)
        ):
            heading = True
        if heading:
            flush_paragraph()
            flush_bullet()
            if section:
                number = section.group(1) or section.group(3)
                heading_text = section.group(2) or section.group(4)
                level = number.count(".")
                hashes = "##" if level == 0 else ("###" if level == 1 else "####")
                output.append(f"{hashes} {heading_text}")
            else:
                output.append(f"## {line}")
            index += 1
            continue

        if _PDF_LAYOUT_CODE_RE.match(line) and (
            indent > base_indent
            or line.lstrip().startswith(
                ("def ", "class ", "import ", "from ", "#", "{", "}", "[", "]")
            )
        ):
            flush_paragraph()
            flush_bullet()
            code = [line]
            code_indent = indent
            index += 1
            while index < len(items):
                next_item = items[index]
                if next_item is None:
                    lookahead = index + 1
                    while (
                        lookahead < len(items)
                        and items[lookahead] is None
                    ):
                        lookahead += 1
                    if lookahead < len(items):
                        candidate = items[lookahead]
                        if candidate is not None and (
                            candidate[0] > base_indent
                            or _PDF_LAYOUT_CODE_RE.match(candidate[1])
                        ):
                            code.append("")
                            index += 1
                            continue
                    break
                next_indent, next_line, _next_raw = next_item
                if next_indent > base_indent or _PDF_LAYOUT_CODE_RE.match(
                    next_line
                ):
                    code.append(
                        " " * max(0, next_indent - code_indent) + next_line
                    )
                    index += 1
                    continue
                break
            flush_code()
            continue

        bullet_match = _PDF_LAYOUT_BULLET_RE.match(line)
        if bullet_match:
            flush_paragraph()
            flush_bullet()
            bullet = f"- {bullet_match.group(2)}"
            bullet_indent = indent
            index += 1
            continue

        numbered_match = _PDF_LAYOUT_NUMBERED_RE.match(line)
        if numbered_match:
            flush_paragraph()
            flush_bullet()
            marker = numbered_match.group(1).strip()
            if not re.match(r"^\d+[.)]$", marker):
                marker = f"{marker}."
            bullet = f"{marker} {numbered_match.group(2)}"
            bullet_indent = indent
            index += 1
            continue

        if bullet is not None:
            if indent > bullet_indent:
                bullet = _pdf_join_lines(bullet, line)
                index += 1
                continue
            flush_bullet()

        if (
            _PDF_LAYOUT_CAPTION_RE.match(line)
            and len(line) <= 110
            and not re.search(r"[.!?]\s+[A-Z]", line)
        ):
            flush_paragraph()
            language = line.split(None, 1)[0].casefold()
            output.append(f"*{line}*")
            index += 1
            if language == "table":
                # A blank line often separates the caption from the table.
                while index < len(items) and items[index] is None:
                    index += 1
                table_lines: list[str] = []
                while index < len(items):
                    table_item = items[index]
                    if table_item is None:
                        break
                    table_indent, table_line, table_raw = table_item
                    if (
                        _PDF_LAYOUT_SECTION_RE.match(table_line)
                        or table_indent <= max(0, base_indent - 2)
                    ):
                        break
                    table_lines.append(table_raw)
                    index += 1
                table = _pdf_layout_table_markdown(table_lines)
                if table:
                    output.append(table)
                elif table_lines:
                    output.append(
                        " ".join(
                            " ".join(line.split()) for line in table_lines
                        )
                    )
            continue

        if paragraph is None:
            paragraph = line
            paragraph_indent = indent
        elif (
            base_indent + 2 <= indent <= base_indent + 8
            and indent > paragraph_indent
        ) or indent < base_indent - 1:
            flush_paragraph()
            paragraph = line
            paragraph_indent = indent
        else:
            paragraph = _pdf_join_lines(paragraph, line)
        index += 1

    flush_all()
    return "\n\n".join(value for value in output if value.strip())

def _merge_pdf_page_blocks(blocks: list[str]) -> list[str]:
    """Join a sentence that poppler wrapped across a page boundary."""
    merged: list[str] = []
    for block in blocks:
        if merged:
            previous = merged[-1]
            previous_last = previous.rsplit("\n\n", 1)[-1]
            next_first = block.split("\n\n", 1)[0]
            if (
                previous_last
                and next_first
                and next_first[:1].islower()
                and not previous_last.endswith(
                    (".", "?", "!", ":", ";", '"', "”", ")", "]", "}", "```")
                )
            ):
                merged[-1] = (
                    previous[: -len(previous_last)]
                    + _pdf_join_lines(previous_last, next_first)
                    + block[len(next_first):]
                )
                continue
        merged.append(block)
    return merged

def _truncate_pdf_markdown(text: str, limit: int) -> str:
    """Cut Markdown at a block boundary and keep a code fence balanced."""
    if len(text) <= limit:
        return text
    cut = text[:limit]
    split = cut.rfind("\n\n")
    if split > limit // 2:
        cut = cut[:split]
    if cut.count("```") % 2:
        cut += "\n```"
    return cut

def _pdf_page_words(path: Path, page: int) -> list[dict]:
    """Return normalized, clickable word boxes for one PDF page.

    poppler's ``pdftotext -bbox`` emits one box per word in PDF points. The
    coordinates are normalized to the page so the browser can place an
    invisible text layer directly over the rendered page image.
    """
    try:
        stat = path.stat()
    except OSError:
        return []
    key = (str(path), stat.st_mtime, page)
    with _PDF_BOOK_READER_LOCK:
        cached = _PDF_BOOK_WORDS.get(key)
        if cached is not None:
            return cached
    try:
        result = subprocess.run(
            [
                "pdftotext",
                "-f",
                str(page),
                "-l",
                str(page),
                "-bbox",
                str(path),
                "-",
            ],
            capture_output=True,
            text=True,
            timeout=30,
        )
    except (FileNotFoundError, subprocess.SubprocessError):
        return []
    if result.returncode != 0:
        return []
    page_match = re.search(
        r'<page\s+width="([0-9.]+)"\s+height="([0-9.]+)"',
        result.stdout,
    )
    if page_match is None:
        return []
    page_width = float(page_match.group(1)) or 1.0
    page_height = float(page_match.group(2)) or 1.0
    words: list[dict] = []
    for match in re.finditer(
        r'<word\s+xMin="([0-9.]+)"\s+yMin="([0-9.]+)"'
        r'\s+xMax="([0-9.]+)"\s+yMax="([0-9.]+)">(.*?)</word>',
        result.stdout,
        re.DOTALL,
    ):
        text = html.unescape(match.group(5)).strip()
        if not text:
            continue
        x_min = float(match.group(1))
        y_min = float(match.group(2))
        x_max = float(match.group(3))
        y_max = float(match.group(4))
        words.append(
            {
                "text": text,
                "x": max(0.0, x_min / page_width),
                "y": max(0.0, y_min / page_height),
                "w": max(0.0, (x_max - x_min) / page_width),
                "h": max(0.0, (y_max - y_min) / page_height),
            }
        )
        if len(words) >= 5000:
            break
    with _PDF_BOOK_READER_LOCK:
        _PDF_BOOK_WORDS[key] = words
    return words

def _pdf_book_chapters(path: Path, reader) -> list[dict]:
    """Return the PDF's top-level chapter outline entries with page numbers."""
    try:
        stat = path.stat()
    except OSError:
        return []
    key = (str(path), stat.st_mtime)
    with _PDF_BOOK_READER_LOCK:
        cached = _PDF_BOOK_CHAPTERS.get(key)
        if cached is not None:
            return cached
    chapters: list[dict] = []
    try:
        outline = reader.outline or []
    except (AttributeError, TypeError):
        outline = []
    for item in outline:
        title = " ".join(str(getattr(item, "title", "") or "").split())
        if not title.lower().startswith("chapter"):
            continue
        try:
            page = reader.get_destination_page_number(item) + 1
        except (AttributeError, TypeError, ValueError):
            continue
        if page < 1:
            continue
        chapters.append({"title": title, "page": page})
    chapters.sort(key=lambda chapter: chapter["page"])
    with _PDF_BOOK_READER_LOCK:
        _PDF_BOOK_CHAPTERS[key] = chapters
    return chapters

_PDF_GRAPH_STOPWORDS = frozenset(
    """
    a an and are as at be been being but by can could did do does doing for
    from had has have having he her here hers him his how i if in into is it
    its itself just like may me more most much must my no nor not now of off on
    once only or other our ours over same she should so some such than that the
    their theirs them then there these they this those through to too under
    until up very was we were what when where which while who whom why will
    with would you your yours chapter figure table section page book example
    examples using used also each any all both few many several such than
    http https www com org net html pdf
    """.split()
)

_PDF_KNOWLEDGE_GRAPH_LOCK = threading.Lock()

def pdf_knowledge_graph_path(name: str) -> Path | None:
    """Return the cached knowledge-graph JSON path for one PDF book."""
    path = pdf_book_path(name)
    if path is None:
        return None
    return PDF_BOOKS_DIR / f"{path.name}.knowlege_graph.json"

def _pdf_outline_nodes(reader) -> list[dict]:
    """Turn the PDF outline into chapter and section concept nodes."""
    nodes: list[dict] = []
    chapter_counter = -1

    def walk(items, parent=None, depth=0, chapter=-1):
        nonlocal chapter_counter
        index = 0
        while index < len(items):
            item = items[index]
            if isinstance(item, list):
                index += 1
                continue
            title = " ".join(str(getattr(item, "title", "") or "").split())
            if not title:
                index += 1
                continue
            is_chapter = depth == 0 and title.lower().startswith("chapter")
            if depth == 0 and not is_chapter:
                index += 1
                continue
            try:
                page = reader.get_destination_page_number(item) + 1
            except (AttributeError, TypeError, ValueError):
                page = None
            if page is None:
                index += 1
                continue
            if is_chapter:
                chapter_counter += 1
                chapter = chapter_counter
            node_id = f"n{len(nodes) + 1}"
            nodes.append(
                {
                    "id": node_id,
                    "label": title,
                    "page": int(page),
                    "level": depth,
                    "parent": parent,
                    "chapter": chapter,
                }
            )
            index += 1
            if index < len(items) and isinstance(items[index], list):
                walk(items[index], node_id, depth + 1, chapter)
                index += 1

    try:
        outline = reader.outline or []
    except (AttributeError, TypeError):
        outline = []
    walk(outline)
    return nodes

def _pdf_heading_nodes(path: Path) -> list[dict]:
    """Detect chapter/section/subsection headings when a PDF has no outline.

    pdftohtml reports the font used by every text run. This fallback groups
    text by font size: large display text becomes chapters, medium text becomes
    sections, and smaller numbered text becomes subsections. It is intended for
    compiled or exported books that omit PDF bookmarks.
    """
    try:
        result = subprocess.run(
            [
                "pdftohtml",
                "-xml",
                "-hidden",
                "-i",
                "-stdout",
                str(path),
            ],
            capture_output=True,
            text=True,
            timeout=120,
        )
    except (FileNotFoundError, subprocess.SubprocessError):
        return []
    if result.returncode != 0 or not result.stdout:
        return []
    font_sizes = {
        font_id: int(size)
        for font_id, size in re.findall(
            r'<fontspec id="(\d+)" size="(\d+)"', result.stdout
        )
    }
    pages = re.findall(
        r'<page number="(\d+)".*?>(.*?)</page>',
        result.stdout,
        re.DOTALL,
    )
    headings: list[dict] = []
    for page_text, body in pages:
        page = int(page_text)
        elements: list[tuple[float, float, int, str]] = []
        for match in re.finditer(
            r'<text top="(-?[0-9.]+)" left="(-?[0-9.]+)"'
            r' width="[0-9.]+" height="[0-9.]+" font="(\d+)">'
            r'(.*?)</text>',
            body,
            re.DOTALL,
        ):
            top = float(match.group(1))
            left = float(match.group(2))
            size = font_sizes.get(match.group(3), 0)
            text = html.unescape(
                re.sub(r"<[^>]+>", "", match.group(4))
            ).strip()
            if text:
                elements.append((top, left, size, text))
        elements.sort(key=lambda item: (item[0], item[1]))

        large = [
            element for element in elements
            if 40 <= element[2] <= 50
        ]
        if large:
            title = " ".join(element[3] for element in large)
            if title.casefold() not in {"contents", "introduction"}:
                headings.append(
                    {
                        "kind": "chapter",
                        "page": page,
                        "top": large[0][0],
                        "title": title,
                    }
                )

        # Sections: a number line and its title line(s) share a font size and
        # sit at the same top coordinate; wrapped titles continue just below.
        section_groups: list[dict] = []
        for top, _left, size, text in elements:
            if size != 19:
                continue
            is_number = bool(re.fullmatch(r"\d+(?:\.\d+)*", text))
            if is_number:
                section_groups.append(
                    {"top": top, "last_top": top, "number": text, "lines": []}
                )
            elif section_groups and top - section_groups[-1]["last_top"] <= 30:
                section_groups[-1]["lines"].append(text)
                section_groups[-1]["last_top"] = top
            else:
                section_groups.append(
                    {"top": top, "last_top": top, "number": "", "lines": [text]}
                )
        for group in section_groups:
            title = " ".join(group["lines"]).strip()
            if group["number"]:
                title = f"{group['number']} {title}".strip()
            if title:
                headings.append(
                    {
                        "kind": "section",
                        "page": page,
                        "top": group["top"],
                        "title": title,
                    }
                )

        for top, _left, size, text in elements:
            if size != 16:
                continue
            if not re.match(r"^\d+(?:\.\d+)+\s+\S", text):
                continue
            headings.append(
                {
                    "kind": "subsection",
                    "page": page,
                    "top": top,
                    "title": " ".join(text.split()),
                }
            )

    headings.sort(
        key=lambda item: (
            item["page"],
            0 if item["kind"] == "chapter" else 1,
            item["top"],
        )
    )
    nodes: list[dict] = []
    chapter_index = -1
    chapter_id: str | None = None
    section_id: str | None = None
    for heading in headings:
        node_id = f"h{len(nodes) + 1}"
        kind = heading["kind"]
        if kind == "chapter":
            chapter_index += 1
            chapter_id = node_id
            section_id = None
            level = 0
            parent = None
        elif kind == "section":
            if chapter_id is None:
                continue
            section_id = node_id
            level = 1
            parent = chapter_id
        else:
            if chapter_id is None:
                continue
            level = 2
            parent = section_id or chapter_id
        nodes.append(
            {
                "id": node_id,
                "label": heading["title"],
                "page": heading["page"],
                "level": level,
                "parent": parent,
                "chapter": max(0, chapter_index),
            }
        )
    return nodes

def _pdf_graph_term_counts(text: str) -> dict[str, int]:
    """Count meaningful lower-case terms in one concept's section text."""
    counts: dict[str, int] = {}
    for raw in re.findall(r"[A-Za-z][A-Za-z0-9'-]{3,}", text):
        term = raw.lower().strip("-'")
        if len(term) < 4 or term in _PDF_GRAPH_STOPWORDS:
            continue
        counts[term] = counts.get(term, 0) + 1
    return counts

def build_pdf_knowledge_graph(
    name: str,
    progress: Callable[[int, str], None] | None = None,
    *,
    rebuild: bool = False,
) -> dict:
    """Build and cache concepts and related-concept links for one PDF."""
    path = pdf_book_path(name)
    if path is None or not path.is_file():
        raise ValueError("PDF book not found")
    graph_path = pdf_knowledge_graph_path(name)
    if graph_path is None:
        raise ValueError("PDF book not found")

    def report(percent: int, message: str) -> None:
        if progress is not None:
            progress(max(0, min(100, int(percent))), message)

    if not rebuild and graph_path.is_file():
        try:
            cached = json.loads(graph_path.read_text(encoding="utf-8"))
            if isinstance(cached, dict) and cached.get("nodes"):
                report(100, "Knowledge graph loaded")
                return cached
        except (OSError, json.JSONDecodeError):
            pass

    with _PDF_KNOWLEDGE_GRAPH_LOCK:
        if not rebuild and graph_path.is_file():
            try:
                cached = json.loads(graph_path.read_text(encoding="utf-8"))
                if isinstance(cached, dict) and cached.get("nodes"):
                    report(100, "Knowledge graph loaded")
                    return cached
            except (OSError, json.JSONDecodeError):
                pass

        reader, count = _pdf_book_reader(path)
        if count <= 0:
            raise RuntimeError("The PDF has no pages")
        report(2, "Reading book outline")
        nodes = _pdf_outline_nodes(reader)
        if not nodes:
            report(
                3,
                "No PDF outline; detecting headings from page layout",
            )
            nodes = _pdf_heading_nodes(path)
        if not nodes:
            raise RuntimeError(
                "This PDF has no outline or detectable headings to build a graph"
            )

        page_texts: list[str] = []
        for index in range(count):
            if index % 5 == 0:
                report(
                    5 + int(50 * index / max(1, count)),
                    f"Reading page {index + 1} of {count}",
                )
            with _PDF_BOOK_READER_LOCK:
                text = reader.pages[index].extract_text() or ""
            page_texts.append(text)
        report(60, "Identifying concepts")

        concept_texts: list[str] = []
        for index, node in enumerate(nodes):
            start = max(0, int(node["page"]) - 1)
            if index + 1 < len(nodes):
                next_page = int(nodes[index + 1]["page"])
            else:
                next_page = count + 1
            end = max(start + 1, min(count, next_page - 1))
            concept_texts.append(" ".join(page_texts[start:end]))
        report(70, "Ranking key terms")

        term_counts = [_pdf_graph_term_counts(text) for text in concept_texts]
        document_frequency: dict[str, int] = {}
        for counts in term_counts:
            for term in counts:
                document_frequency[term] = (
                    document_frequency.get(term, 0) + 1
                )
        total_nodes = max(1, len(nodes))
        keyword_sets: list[set[str]] = []
        for counts in term_counts:
            scored = sorted(
                (
                    (frequency * math.log((total_nodes + 1)
                                         / (document_frequency[term] + 1)), term)
                    for term, frequency in counts.items()
                ),
                reverse=True,
            )
            keyword_sets.append({term for _score, term in scored[:8]})
        report(80, "Linking related concepts")

        edges: list[dict] = []
        seen: set[tuple[str, str]] = set()
        for node in nodes:
            parent = node.get("parent")
            if parent:
                edges.append(
                    {"source": parent, "target": node["id"], "type": "parent"}
                )
                seen.add((parent, node["id"]))
        for index, node in enumerate(nodes):
            candidates: list[tuple[int, int]] = []
            for other_index, other in enumerate(nodes):
                if other_index == index:
                    continue
                shared = len(keyword_sets[index] & keyword_sets[other_index])
                if shared >= 2:
                    candidates.append((shared, other_index))
            candidates.sort(key=lambda item: (-item[0], item[1]))
            for shared, other_index in candidates[:6]:
                left, right = sorted((node["id"], nodes[other_index]["id"]))
                if (left, right) in seen:
                    continue
                seen.add((left, right))
                edges.append(
                    {
                        "source": left,
                        "target": right,
                        "type": "related",
                        "weight": shared,
                    }
                )
            if index % 20 == 0:
                report(
                    80 + int(15 * index / total_nodes),
                    "Linking related concepts",
                )

        report(96, "Saving knowledge graph")
        payload = {
            "name": path.name,
            "title": _pdf_book_title(path.name),
            "pages": count,
            "generated": datetime.now(timezone.utc).isoformat(
                timespec="seconds"
            ),
            "nodes": nodes,
            "edges": edges,
        }
        try:
            PDF_BOOKS_DIR.mkdir(parents=True, exist_ok=True)
            temp_path = graph_path.with_name(graph_path.name + ".tmp")
            temp_path.write_text(
                json.dumps(payload, ensure_ascii=False, indent=2),
                encoding="utf-8",
            )
            temp_path.replace(graph_path)
        except OSError as exc:
            raise RuntimeError(
                f"Could not save the knowledge graph: {exc}"
            ) from exc
        report(100, "Knowledge graph ready")
        return payload

def read_pdf_concept(name: str, node_id: str) -> dict:
    """Return the PDF text belonging to one knowledge-graph concept."""
    path = pdf_book_path(name)
    graph_path = pdf_knowledge_graph_path(name)
    if path is None or not path.is_file():
        raise ValueError("PDF book not found")
    if graph_path is None or not graph_path.is_file():
        raise ValueError("Build the knowledge graph first")
    try:
        graph = json.loads(graph_path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise RuntimeError(f"Cannot read the knowledge graph: {exc}") from exc
    nodes = graph.get("nodes") if isinstance(graph, dict) else None
    if not isinstance(nodes, list):
        raise RuntimeError("The knowledge graph has no nodes")
    index = None
    for position, node in enumerate(nodes):
        if isinstance(node, dict) and str(node.get("id")) == str(node_id):
            index = position
            break
    if index is None:
        raise ValueError("Concept not found")
    node = nodes[index]
    reader, count = _pdf_book_reader(path)
    if count <= 0:
        raise RuntimeError("The PDF has no pages")
    start = max(1, min(count, int(node.get("page") or 1)))
    level = int(node.get("level") or 0)
    next_page = count + 1
    for candidate in nodes[index + 1:]:
        if not isinstance(candidate, dict):
            continue
        page = int(candidate.get("page") or 0)
        candidate_level = int(candidate.get("level") or 0)
        if page > start and candidate_level <= level:
            next_page = page
            break
    full_end = min(count, next_page - 1)
    end = min(full_end, start + 9)
    truncated = end < full_end
    paragraphs: list[str] = []
    used = 0
    for page_number in range(start, end + 1):
        formatted = _pdf_layout_page_markdown(
            _pdf_page_layout_text(path, page_number), page_number
        )
        if not formatted:
            # poppler's layout extractor can fail on unusual PDFs. Fall back
            # to pypdf so the concept modal still has readable content.
            with _PDF_BOOK_READER_LOCK:
                raw_text = reader.pages[page_number - 1].extract_text() or ""
            formatted = "\n\n".join(
                paragraph
                for paragraph in _pdf_page_paragraphs(raw_text)
                if paragraph != "This page has no extractable text."
            )
        if not formatted:
            continue
        if used + len(formatted) + 2 > 30000:
            remaining = max(0, 30000 - used)
            if remaining > 200:
                paragraphs.append(
                    _truncate_pdf_markdown(formatted, remaining)
                )
            truncated = True
            break
        paragraphs.append(formatted)
        used += len(formatted) + 2
    if not paragraphs:
        paragraphs = ["No extractable text for this concept."]
    else:
        paragraphs = _merge_pdf_page_blocks(paragraphs)
    return {
        "id": str(node.get("id")),
        "title": str(node.get("label") or "Concept"),
        "page": start,
        "end_page": end,
        "pages": end - start + 1,
        "paragraphs": paragraphs,
        "text": "\n\n".join(paragraphs),
        "truncated": truncated,
    }

def _pdf_destination_page(reader, destination, page_map: dict) -> int | None:
    """Resolve a PDF destination or named destination to a 1-based page."""
    if destination is None:
        return None
    if isinstance(destination, (str, bytes)):
        name = destination.decode("utf-8", "replace") if isinstance(
            destination, bytes
        ) else destination
        named = getattr(reader, "named_destinations", {}).get(name)
        if named is None:
            return None
        destination = named
        if isinstance(named, dict):
            destination = named.get("/D") or named.get("D")
        elif hasattr(named, "page"):
            return _pdf_destination_page(reader, named.page, page_map)
    if not hasattr(destination, "__getitem__"):
        return None
    try:
        target = destination[0]
    except (IndexError, KeyError, TypeError):
        return None
    if hasattr(target, "idnum"):
        return page_map.get((target.idnum, target.generation))
    if isinstance(target, int):
        return target + 1
    if isinstance(target, float):
        return int(target) + 1
    return None

def _pdf_page_links(reader, page_number: int) -> list[dict]:
    """Return normalized internal link rectangles for one PDF page."""
    try:
        page = reader.pages[page_number - 1]
        page_width = float(page.mediabox.width) or 1.0
        page_height = float(page.mediabox.height) or 1.0
    except (IndexError, TypeError, ValueError):
        return []
    page_map: dict[tuple[int, int], int] = {}
    try:
        pages = reader.pages
    except (AttributeError, TypeError):
        return []
    for index, candidate in enumerate(pages, 1):
        reference = getattr(candidate, "indirect_reference", None)
        if reference is not None:
            page_map[(reference.idnum, reference.generation)] = index
    links: list[dict] = []
    seen: set[tuple[int, int, int, int, int]] = set()
    annots = page.get("/Annots") or []
    for annot in annots:
        try:
            obj = annot.get_object()
            if obj.get("/Subtype") != "/Link":
                continue
            destination = obj.get("/Dest")
            if destination is None:
                action = obj.get("/A")
                if action is None:
                    continue
                action = action.get_object()
                if action.get("/S") != "/GoTo":
                    continue
                destination = action.get("/D")
            target_page = _pdf_destination_page(
                reader, destination, page_map
            )
            if not target_page:
                continue
            rect = obj.get("/Rect")
            if rect is None or len(rect) < 4:
                continue
            x1, y1, x2, y2 = [float(value) for value in rect[:4]]
        except (AttributeError, IndexError, KeyError, TypeError, ValueError):
            continue
        left = max(0.0, min(x1, x2) / page_width)
        top = max(0.0, 1.0 - max(y1, y2) / page_height)
        width = max(0.0, abs(x2 - x1) / page_width)
        height = max(0.0, abs(y2 - y1) / page_height)
        if width <= 0 or height <= 0:
            continue
        key = (
            round(left * 1000), round(top * 1000),
            round(width * 1000), round(height * 1000), target_page,
        )
        if key in seen:
            continue
        seen.add(key)
        links.append(
            {
                "x": left,
                "y": top,
                "w": width,
                "h": height,
                "page": int(target_page),
            }
        )
        if len(links) >= 500:
            break
    return links

def read_pdf_book_page(name: str, page: int) -> dict:
    """Return one page of a PDF book as paragraphs and page metadata."""
    path = pdf_book_path(name)
    if path is None or not path.is_file():
        raise ValueError("PDF book not found")
    record_pdf_open(path.name)
    reader, count = _pdf_book_reader(path)
    if count <= 0:
        raise RuntimeError("The PDF has no pages")
    page_number = max(1, min(int(page), count))
    with _PDF_BOOK_READER_LOCK:
        text = reader.pages[page_number - 1].extract_text() or ""
    paragraphs = _pdf_page_paragraphs(text)
    words = _pdf_page_words(path, page_number)
    links = _pdf_page_links(reader, page_number)
    chapters = _pdf_book_chapters(path, reader)
    return {
        "name": path.name,
        "title": _pdf_book_title(path.name),
        "page": page_number,
        "pages": count,
        "paragraphs": paragraphs,
        "text": "\n\n".join(paragraphs),
        "words": words,
        "links": links,
        "chapters": chapters,
        "image_url": (
            f"/api/pdf_book_page_image?name={quote(path.name)}"
            f"&page={page_number}"
        ),
    }

# PDF pages are rendered as images so figures, diagrams, syntax colors, and
# bold fonts survive extraction. The rendered PNGs are cached per PDF version.
PDF_PAGE_RENDER_DPI = 180

PDF_PAGE_CACHE_DIR = PDF_BOOKS_DIR / ".pages"

_PDF_PAGE_RENDER_LOCK = threading.Lock()

def _pdf_page_cache_path(path: Path, page: int, dpi: int) -> Path:
    key = (
        f"{path.name}:{path.stat().st_mtime}:{dpi}:{page}".encode("utf-8")
    )
    return PDF_PAGE_CACHE_DIR / (hashlib.sha1(key).hexdigest() + ".png")

def render_pdf_book_page(
    name: str, page: int, dpi: int = PDF_PAGE_RENDER_DPI
) -> Path:
    """Render one PDF page to a cached PNG image."""
    path = pdf_book_path(name)
    if path is None or not path.is_file():
        raise ValueError("PDF book not found")
    _reader, count = _pdf_book_reader(path)
    if count <= 0:
        raise RuntimeError("The PDF has no pages")
    page_number = max(1, min(int(page), count))
    dpi = max(72, min(int(dpi), 300))
    cache_path = _pdf_page_cache_path(path, page_number, dpi)
    with _PDF_PAGE_RENDER_LOCK:
        try:
            if cache_path.is_file() and cache_path.stat().st_size > 0:
                return cache_path
        except OSError:
            pass
        try:
            PDF_PAGE_CACHE_DIR.mkdir(parents=True, exist_ok=True)
        except OSError as exc:
            raise RuntimeError(
                f"Cannot create the PDF page cache: {exc}"
            ) from exc
        tmp_prefix = PDF_PAGE_CACHE_DIR / (
            f"tmp-{os.getpid()}-{threading.get_ident()}-{page_number}"
        )
        tmp_path = Path(str(tmp_prefix) + ".png")
        command = [
            "pdftoppm",
            "-f",
            str(page_number),
            "-l",
            str(page_number),
            "-png",
            "-r",
            str(dpi),
            "-singlefile",
            str(path),
            str(tmp_prefix),
        ]
        try:
            result = subprocess.run(
                command,
                capture_output=True,
                text=True,
                timeout=90,
            )
        except FileNotFoundError as exc:
            raise RuntimeError(
                "PDF page rendering requires poppler's pdftoppm command"
            ) from exc
        except subprocess.SubprocessError as exc:
            raise RuntimeError(f"Could not render the PDF page: {exc}") from exc
        if result.returncode != 0 or not tmp_path.is_file():
            detail = " ".join(
                (result.stderr or result.stdout or "").split()
            )[:300]
            try:
                tmp_path.unlink(missing_ok=True)
            except OSError:
                pass
            raise RuntimeError(
                "Could not render the PDF page" + (f": {detail}" if detail else "")
            )
        try:
            tmp_path.replace(cache_path)
        except OSError as exc:
            raise RuntimeError(
                f"Could not store the rendered PDF page: {exc}"
            ) from exc
        return cache_path

def _book_news_timestamp(value: str) -> float | None:
    """Parse an RSS/ISO publication date into a UTC timestamp."""
    text = str(value or "").strip()
    if not text:
        return None
    parsed = None
    try:
        parsed = parsedate_to_datetime(text)
    except (TypeError, ValueError, OverflowError):
        parsed = None
    if parsed is None:
        try:
            parsed = datetime.fromisoformat(text.replace("Z", "+00:00"))
        except ValueError:
            return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed.timestamp()

try:
    from zoneinfo import ZoneInfo

    _BOOK_SOURCE_TIME_ZONE = ZoneInfo("America/Los_Angeles")
except (ImportError, KeyError):
    # Python builds without the system tz database still show a stable
    # Pacific timestamp; DST is unavailable in that fallback.
    _BOOK_SOURCE_TIME_ZONE = timezone(timedelta(hours=-8))

def _book_source_timestamp(value: str) -> str:
    """Render a source timestamp in Pacific time as YYYY-MM-DD HH:MM:SS."""
    text = str(value or "").strip()
    if not text:
        return ""
    timestamp = _book_news_timestamp(text)
    if timestamp is None:
        return text
    local = datetime.fromtimestamp(
        timestamp, tz=timezone.utc
    ).astimezone(_BOOK_SOURCE_TIME_ZONE)
    return local.strftime("%Y-%m-%d %H:%M:%S")

def _parse_book_model_lines(raw: str, limit: int) -> list[str]:
    """Parse a model's one-item-per-line list, removing common wrappers."""
    lines: list[str] = []
    seen: set[str] = set()
    text = (raw or "").strip()
    if text.startswith("["):
        try:
            parsed = json.loads(text)
        except json.JSONDecodeError:
            parsed = None
        if isinstance(parsed, list):
            text = "\n".join(str(item) for item in parsed)
    for raw_line in text.splitlines():
        line = raw_line.strip()
        line = re.sub(r"^\s*(?:[-*\u2022]+|\d+[.)])\s*", "", line)
        line = re.sub(
            r"^\s*(?:keywords?|search queries?|queries|chapters?|outline)\s*[:\uff1a]\s*",
            "",
            line,
            flags=re.IGNORECASE,
        )
        line = line.strip(" \t\"'`#")
        if not line or len(line) > 160:
            continue
        if line.casefold().startswith(
            ("here are", "sure,", "certainly", "i cannot", "as an ai")
        ):
            continue
        key = line.casefold()
        if key in seen:
            continue
        seen.add(key)
        lines.append(line)
        if len(lines) >= limit:
            break
    return lines

_WRITE_A_BOOK_OLD_YEAR_RE = re.compile(
    r"\b(?:19\d{2}|20(?:0\d|1\d|2[0-4]))\b"
)

def _book_recent_keyword(keyword: str) -> str:
    """Keep a generated research query focused on 2025/2026.

    Matches task.do_not_include_2024 in prompts/write_a_book.prompt: the
    keyword model sometimes appends an old year such as ``2024``. Rewriting
    that year keeps the Google News query current instead of searching for
    stale, year-specific material.
    """
    current = _WRITE_A_BOOK_OLD_YEAR_RE.sub(
        str(WRITE_A_BOOK_RESEARCH_START_YEAR), str(keyword)
    )
    return " ".join(current.split())

def _book_keyword_list(
    raw: str, subject: str, limit: int = WRITE_A_BOOK_KEYWORD_LIMIT
) -> list[str]:
    """Turn the keyword-model reply into a bounded, deduplicated list."""
    limit = max(1, int(limit))
    keywords = _parse_book_model_lines(raw, limit)
    if len(keywords) == 1 and "," in keywords[0]:
        split = [
            part.strip(" \t\"'`")
            for part in keywords[0].split(",")
            if part.strip(" \t\"'`")
        ]
        if len(split) > 1:
            keywords = split[:limit]
    normalized: list[str] = []
    seen: set[str] = set()
    for keyword in keywords:
        keyword = _book_recent_keyword(keyword)
        key = keyword.casefold()
        if keyword and key not in seen:
            seen.add(key)
            normalized.append(keyword)
    return normalized or [subject]

def _book_chapter_titles(
    raw: str,
    subject: str,
    chapters: int = WRITE_A_BOOK_CHAPTER_LIMIT,
) -> list[str]:
    """Return exactly ``chapters`` usable titles, with a stable fallback."""
    chapters = max(WRITE_A_BOOK_MIN_CHAPTERS, int(chapters))
    titles = _parse_book_model_lines(raw, chapters)
    cleaned: list[str] = []
    for title in titles:
        title = re.sub(
            r"^chapter\s*\d+\s*[:.\-\u2013]\s*", "", title, flags=re.IGNORECASE
        ).strip()
        if title:
            cleaned.append(title)
    if len(cleaned) < WRITE_A_BOOK_MIN_CHAPTERS:
        cleaned = [
            "Introduction",
            f"Understanding {subject}",
            "Key Developments",
            "People and Institutions",
            "Practical Implications",
            "Challenges and Debates",
            "Case Studies",
            "What Comes Next",
            "Further Perspectives",
            "Conclusion",
        ]
    while len(cleaned) < chapters:
        cleaned.append(f"Further Perspectives {len(cleaned) + 1}")
    return cleaned[:chapters]

def _parse_write_a_book_options(argument: str) -> WriteABookOptions:
    """Split ``<subject> [--setting VALUE ...]`` into book settings.

    Every option exposed by task.expose_settings_in_form in
    prompts/write_a_book.prompt is accepted. Numeric settings are clamped
    only at their minimum; there is no maximum, so the caller can request any
    number of words, chapters, or planning questions. Options may be
    written as ``--name value`` or ``--name=value``; values containing spaces
    can be quoted.
    """
    try:
        tokens = shlex.split(str(argument or ""))
    except ValueError:
        tokens = str(argument or "").split()
    values = {
        "words": WRITE_A_BOOK_CHAPTER_WORDS,
        "chapters": WRITE_A_BOOK_CHAPTER_LIMIT,
        "sub_questions": WRITE_A_BOOK_SUB_QUESTION_LIMIT,
        "sub_question_articles": WRITE_A_BOOK_SUB_QUESTION_ARTICLE_LIMIT,
    }
    model = WRITE_A_BOOK_MODEL
    directory = ""
    subject_tokens: list[str] = []
    index = 0
    while index < len(tokens):
        token = tokens[index]
        name = ""
        inline_value: str | None = None
        if token.startswith("--"):
            name = token[2:]
            if "=" in name:
                name, inline_value = name.split("=", 1)
            name = name.strip().lower()
        if name in _WRITE_A_BOOK_IGNORED_OPTIONS:
            if inline_value is None:
                index += 1
            index += 1
            continue
        if name in _WRITE_A_BOOK_NUMERIC_OPTIONS or name in (
            _WRITE_A_BOOK_STRING_OPTIONS
        ):
            if inline_value is None:
                index += 1
                if index >= len(tokens):
                    break
                inline_value = tokens[index]
            if name in _WRITE_A_BOOK_NUMERIC_OPTIONS:
                key = _WRITE_A_BOOK_NUMERIC_OPTIONS[name]
                try:
                    values[key] = int(inline_value)
                except (TypeError, ValueError):
                    pass
            else:
                key = _WRITE_A_BOOK_STRING_OPTIONS[name]
                if key == "model":
                    model = str(inline_value).strip() or model
                else:
                    directory = str(inline_value).strip()
            index += 1
            continue
        subject_tokens.append(token)
        index += 1

    subject = " ".join(subject_tokens).strip()
    words = max(WRITE_A_BOOK_MIN_CHAPTER_WORDS, values["words"])
    chapters = max(WRITE_A_BOOK_MIN_CHAPTERS, values["chapters"])
    sub_questions = max(
        WRITE_A_BOOK_MIN_SUB_QUESTIONS, values["sub_questions"]
    )
    sub_question_articles = max(
        WRITE_A_BOOK_MIN_SUB_QUESTION_ARTICLES,
        values["sub_question_articles"],
    )
    return WriteABookOptions(
        subject=subject,
        words_per_chapter=words,
        chapter_count=chapters,
        sub_question_count=sub_questions,
        sub_question_article_limit=sub_question_articles,
        model=model,
        directory=_resolve_book_directory(directory),
    )

def _book_search_tokens(text: str) -> list[str]:
    """Tokenize a string for inexpensive lexical evidence selection."""
    return [
        token
        for token in re.findall(r"[a-z0-9][a-z0-9'-]*", text.casefold())
        if len(token) > 2
        and token
        not in {
            "the", "and", "for", "from", "that", "this", "with", "into",
            "about", "after", "before", "their", "there", "these", "those",
            "have", "has", "had", "was", "were", "are", "you", "your",
            "but", "not", "can", "could", "will", "would", "more", "most",
        }
    ]

def _book_source_digest(sources: list[dict], max_chars: int) -> str:
    """Build a compact source digest for chapter planning."""
    parts: list[str] = []
    used = 0
    for source in sources:
        paragraphs = source.get("paragraphs") or []
        summary = " ".join(str(source.get("summary") or "").split())
        if not summary and paragraphs:
            summary = " ".join(str(paragraphs[0]).split())
        published = str(source.get("published") or "").strip()
        line = f"- {source.get('title', 'Untitled')}"
        if published:
            line += f" ({published})"
        if summary:
            line += f": {summary[:520]}"
        if used + len(line) > max_chars and parts:
            break
        parts.append(line)
        used += len(line) + 1
    return "\n".join(parts)

def _book_reference_map(sources: list[dict], max_chars: int) -> str:
    """List every research reference for cross-source creative synthesis.

    Matches the final point of task.refactor in prompts/write_a_book.prompt:
    a chapter should consider all of the learned articles as one connected
    corpus, so the model gets a numbered map of every reference in addition
    to the excerpts selected for the chapter.
    """
    parts: list[str] = []
    used = 0
    for index, source in enumerate(sources, 1):
        title = " ".join(str(source.get("title") or "Untitled").split())
        summary = " ".join(str(source.get("summary") or "").split())
        if not summary:
            paragraphs = source.get("paragraphs") or []
            if paragraphs:
                summary = " ".join(str(paragraphs[0]).split())
        line = f"[{index}] {title}"
        if summary:
            line += f" — {summary[:240]}"
        if used + len(line) > max_chars and parts:
            parts.append(
                f"[... {len(sources) - index + 1} more reference(s)]"
            )
            break
        parts.append(line)
        used += len(line) + 1
    return "\n".join(parts)

def _book_chapter_evidence(
    sources: list[dict], subject: str, chapter_title: str, max_chars: int
) -> str:
    """Select the most relevant source paragraphs for one chapter."""
    query = set(_book_search_tokens(f"{subject} {chapter_title}"))
    ranked: list[tuple[float, int, int, str]] = []
    for source_index, source in enumerate(sources):
        for paragraph_index, paragraph in enumerate(
            source.get("paragraphs") or []
        ):
            tokens = set(_book_search_tokens(str(paragraph)))
            if not tokens:
                continue
            overlap = len(query & tokens)
            score = overlap / math.sqrt(max(len(tokens), 1))
            ranked.append(
                (score, source_index, paragraph_index, str(paragraph))
            )
    ranked.sort(key=lambda item: (-item[0], item[1], item[2]))

    selected: set[tuple[int, int]] = set()
    used = 0
    for score, source_index, paragraph_index, paragraph in ranked:
        if selected and score <= 0:
            break
        if len(paragraph) > max_chars and selected:
            continue
        if used + len(paragraph) > max_chars and selected:
            break
        selected.add((source_index, paragraph_index))
        used += len(paragraph) + 2

    if not selected:
        for source_index, source in enumerate(sources):
            paragraphs = source.get("paragraphs") or []
            if not paragraphs:
                continue
            selected.add((source_index, 0))
            used += len(str(paragraphs[0])) + 2
            if used >= max_chars:
                break

    grouped: dict[int, list[str]] = {}
    for source_index, paragraph_index in sorted(selected):
        grouped.setdefault(source_index, []).append(
            str(sources[source_index]["paragraphs"][paragraph_index])
        )

    blocks: list[str] = []
    used = 0
    for source_index, paragraphs in grouped.items():
        source = sources[source_index]
        header = f"SOURCE: {source.get('title', 'Untitled')}"
        link = str(source.get("link") or "").strip()
        if link:
            header += f" ({link})"
        block = header + "\n" + "\n\n".join(paragraphs)
        if used + len(block) > max_chars and blocks:
            break
        blocks.append(block)
        used += len(block) + 2
    return "\n\n".join(blocks)

def _book_limit_words(text: str, limit: int) -> str:
    """Hard-cap a chapter at ``limit`` words, preferring a sentence end."""
    matches = list(re.finditer(r"\S+", text or ""))
    if len(matches) <= limit:
        return (text or "").strip()
    cut = matches[limit - 1].end()
    trimmed = text[:cut].rstrip()
    sentence_ends = list(re.finditer(r"[.!?][\"')\]]?(?=\s|$)", trimmed))
    if sentence_ends and sentence_ends[-1].end() > len(trimmed) * 0.7:
        trimmed = trimmed[: sentence_ends[-1].end()].rstrip()
    return trimmed

def _book_strip_chapter_heading(text: str, title: str) -> str:
    """Remove a title the model repeated at the start of a chapter."""
    cleaned = (text or "").strip()
    first, _, rest = cleaned.partition("\n")
    first_plain = re.sub(r"^#+\s*", "", first).strip(" *#")
    if first_plain and (
        first_plain.casefold() == title.casefold()
        or re.match(r"^chapter\s*\d+\b", first_plain, re.IGNORECASE)
    ):
        cleaned = rest.strip()
    return cleaned

def _book_inline_html(text: str) -> str:
    """Escape a line and render the small amount of inline Markdown we use."""
    escaped = html.escape(text)
    escaped = re.sub(r"\*\*(.+?)\*\*", r"<strong>\1</strong>", escaped)
    escaped = re.sub(r"`([^`]+)`", r"<code>\1</code>", escaped)
    escaped = re.sub(
        r"(?<!\*)\*(?!\*)([^*\n]+?)(?<!\*)\*(?!\*)", r"<em>\1</em>", escaped
    )
    return escaped

def _book_text_html(text: str) -> str:
    """Render plain chapter prose as safe HTML paragraphs and lists."""
    blocks = re.split(r"\n\s*\n", (text or "").strip())
    rendered: list[str] = []
    for block in blocks:
        lines = [line.strip() for line in block.splitlines() if line.strip()]
        if not lines:
            continue
        if all(re.match(r"^(?:[-*\u2022]|\d+[.)])\s+", line) for line in lines):
            items = []
            for line in lines:
                item = re.sub(
                    r"^(?:[-*\u2022]|\d+[.)])\s+", "", line
                ).strip()
                items.append(f"<li>{_book_inline_html(item)}</li>")
            rendered.append("<ul>" + "".join(items) + "</ul>")
            continue
        paragraph = "<br>".join(_book_inline_html(line) for line in lines)
        rendered.append(f"<p>{paragraph}</p>")
    return "\n".join(rendered)

def _book_html_document(
    subject: str,
    chapters: list[tuple[str, str]],
    sources: list[dict],
    created: str,
) -> str:
    """Build the standalone HTML file saved for /write_a_book."""
    title = html.escape(subject)
    toc: list[str] = []
    sections: list[str] = []
    for index, (chapter_title, chapter_text) in enumerate(chapters, 1):
        words = len(chapter_text.split())
        toc.append(
            f'<li><a href="#chapter-{index}">Chapter {index}: '
            f'{html.escape(chapter_title)}</a></li>'
        )
        sections.append(
            f'<section class="chapter" id="chapter-{index}">'
            f'<h2><span class="chapter-number">Chapter {index}</span>'
            f'{html.escape(chapter_title)}</h2>'
            f'{_book_text_html(chapter_text)}'
            f'<p class="chapter-words">{words:,} words</p>'
            "</section>"
        )

    source_items: list[str] = []
    for source in sources:
        source_title = html.escape(str(source.get("title") or "Untitled"))
        link = str(source.get("link") or "").strip()
        published = html.escape(
            _book_source_timestamp(str(source.get("published") or ""))
        )
        if re.match(r"^https?://", link, re.IGNORECASE):
            source_title = (
                f'<a href="{html.escape(link, quote=True)}" '
                f'target="_blank" rel="noopener noreferrer">{source_title}</a>'
            )
        suffix = f' <span class="muted">{published}</span>' if published else ""
        source_items.append(f"<li>{source_title}{suffix}</li>")

    total_words = sum(len(text.split()) for _title, text in chapters)
    return f"""<!doctype html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>{title}</title>
<style>
  :root {{ color-scheme: light dark; --ink: #1f2328; --muted: #667085;
          --paper: #f7f4ed; --accent: #9a3f1f; --rule: #d8d0c2; }}
  @media (prefers-color-scheme: dark) {{
    :root {{ --ink: #e8e3da; --muted: #a8a29a; --paper: #171614;
            --accent: #e0a37c; --rule: #3b3833; }}
  }}
  * {{ box-sizing: border-box; }}
  body {{ margin: 0; background: var(--paper); color: var(--ink);
         font-family: Georgia, "Iowan Old Style", "Times New Roman", serif;
         line-height: 1.72; }}
  main {{ max-width: 46rem; margin: 0 auto; padding: 3rem 1.35rem 6rem; }}
  header {{ border-bottom: 2px solid var(--rule); margin-bottom: 2.5rem;
           padding-bottom: 1.7rem; }}
  h1 {{ font-size: clamp(2.1rem, 6vw, 3.4rem); line-height: 1.08;
        margin: 0 0 .7rem; letter-spacing: -.02em; }}
  h2 {{ font-size: clamp(1.45rem, 3.5vw, 2rem); line-height: 1.2;
        margin: 0 0 1.2rem; }}
  .chapter-number {{ display: block; color: var(--accent); font-family:
        -apple-system, BlinkMacSystemFont, "Segoe UI", sans-serif;
        font-size: .78rem; font-weight: 700; letter-spacing: .14em;
        text-transform: uppercase; margin-bottom: .45rem; }}
  .meta, .muted, .chapter-words {{ color: var(--muted); }}
  .meta {{ font-family: -apple-system, BlinkMacSystemFont, "Segoe UI", sans-serif;
           font-size: .9rem; }}
  nav {{ background: color-mix(in srgb, var(--paper) 82%, var(--rule));
         border: 1px solid var(--rule); border-radius: .65rem;
         padding: 1.1rem 1.4rem; margin-bottom: 3rem; }}
  nav h2 {{ font-size: .8rem; letter-spacing: .13em; text-transform: uppercase;
            color: var(--muted); margin-bottom: .6rem; }}
  nav ol {{ margin: 0; padding-left: 1.3rem; }}
  nav a, a {{ color: var(--accent); }}
  .chapter {{ margin: 0 0 4rem; scroll-margin-top: 2rem; }}
  p {{ margin: 0 0 1.05rem; }}
  ul {{ padding-left: 1.4rem; }}
  li {{ margin: .35rem 0; }}
  .chapter-words {{ font-family: -apple-system, BlinkMacSystemFont, "Segoe UI", sans-serif;
                    font-size: .78rem; text-align: right; margin-top: 1.4rem; }}
  .sources {{ border-top: 2px solid var(--rule); padding-top: 1.8rem; margin-top: 4rem; }}
  .sources h2 {{ font-size: 1.35rem; }}
  .sources li {{ margin: .6rem 0; }}
  code {{ background: color-mix(in srgb, var(--ink) 10%, transparent);
          border-radius: .25rem; padding: .1rem .3rem; font-size: .92em; }}
</style>
</head>
<body>
<main>
<header>
  <h1>{title}</h1>
  <p class="meta">A researched book · {len(chapters)} chapters · {total_words:,} words · generated {html.escape(created)}</p>
</header>
<nav>
  <h2>Contents</h2>
  <ol>
    {''.join(toc)}
  </ol>
</nav>
{''.join(sections)}
<section class="sources">
  <h2>Research sources</h2>
  <ol>
    {''.join(source_items)}
  </ol>
</section>
</main>
</body>
</html>
"""

def _read_news_ndjson(output: str) -> list[dict]:
    """Collect item and paragraph events from news_fetch.php's NDJSON."""
    by_index: dict[int, dict] = {}
    order: list[int] = []
    for raw_line in (output or "").splitlines():
        line = raw_line.strip()
        if not line.startswith("{"):
            continue
        try:
            event = json.loads(line)
        except json.JSONDecodeError:
            continue
        if not isinstance(event, dict):
            continue
        try:
            index = int(event.get("index"))
        except (TypeError, ValueError):
            index = len(order) + 1
        event_type = str(event.get("type") or "")
        if event_type == "item":
            item = dict(event)
            item["paragraphs"] = []
            by_index[index] = item
            order.append(index)
        elif event_type == "paragraph":
            item = by_index.get(index)
            text = str(event.get("text") or "").strip()
            if item is not None and text:
                item.setdefault("paragraphs", []).append(text)
    return [by_index[index] for index in order if index in by_index]

def _book_clean_paragraphs(values) -> list[str]:
    """Normalize article paragraphs and drop common page furniture."""
    paragraphs: list[str] = []
    seen: set[str] = set()
    if not isinstance(values, list):
        return paragraphs
    for value in values:
        text = " ".join(str(value or "").split())
        if not text or text.casefold() in seen:
            continue
        lowered = text.casefold()
        if lowered in {
            "article continues below this ad",
            "advertisement",
            "read more",
            "related stories",
            "more from",
        }:
            continue
        seen.add(lowered)
        paragraphs.append(text)
    return paragraphs

def _book_plus_keywords(keywords: str) -> str:
    """Join a keyword phrase with ``+`` for Google News search queries.

    Matches task.keywords in prompts/write_a_book.prompt: the generated
    keyword ``japan driving guide for beginner`` becomes
    ``japan+driving+guide+for+beginner`` when passed to /news search.
    """
    return "+".join(str(keywords).split())

def _book_search_candidates(
    keywords: list[str],
    limit: int,
    log: Callable[[str], None],
    *,
    keyword_limit: int = WRITE_A_BOOK_KEYWORD_LIMIT,
    per_question_limit: int = WRITE_A_BOOK_SEARCH_LIMIT_PER_KEYWORD,
    timeout: float = WRITE_A_BOOK_SEARCH_TIMEOUT_SECONDS,
    deadline: float | None = None,
) -> list[dict]:
    """Search recent Google News stories for the book's keyword list.

    Matches task.evenly_distributed_horizontal_search and task.refactor in
    prompts/write_a_book.prompt: each planning question must contribute an
    equal share of the research material. task.exact_sub_questions_article_search_limit
    tightens that share to an exact number: every planning question must
    contribute exactly ``per_question_limit`` distinct articles to the pool,
    never more and never fewer (short of what the search engine can supply,
    which is reported). Two mechanisms keep the number exact. First, each
    keyword is queried with a wider raw window so that intra-query duplicates,
    the publication-date cutoff, and stories shared across questions cannot
    starve a question's share. Second, the round-robin selection stops taking
    from a question the moment its exact share is reached, so a prolific
    query can neither fill the pool on its own nor exceed its own limit.
    """
    script = SCRIPT_DIR / "news_fetch.php"
    if not script.exists():
        raise RuntimeError("missing news_fetch.php")
    limit = max(0, int(limit))
    keyword_limit = max(1, int(keyword_limit))
    per_question_limit = max(1, int(per_question_limit))
    # task.exact_sub_questions_article_search_limit: the raw query window is
    # deliberately wider than the per-question share. Reaching exactly
    # ``per_question_limit`` distinct articles per question requires spare
    # results to replace intra-query duplicates, stories older than the
    # publication cutoff, and stories shared by several planning questions.
    # The extra raw results never enter the pool; only the exact share does.
    raw_window = min(
        WRITE_A_BOOK_RAW_SEARCH_WINDOW_MAX,
        max(4 * per_question_limit, per_question_limit + 10),
    )
    if limit <= 0:
        return []

    unique_keywords: list[str] = []
    seen_keywords: set[str] = set()
    for keyword in keywords:
        keyword = " ".join(str(keyword).split())
        if keyword and keyword.casefold() not in seen_keywords:
            seen_keywords.add(keyword.casefold())
            unique_keywords.append(keyword)
        if len(unique_keywords) >= keyword_limit:
            break

    cutoff = datetime(
        WRITE_A_BOOK_RESEARCH_START_YEAR, 1, 1, tzinfo=timezone.utc
    ).timestamp()
    # One bucket per planning question. Duplicates inside a bucket are
    # removed, but the same story may appear in several buckets: it can be a
    # valid result for more than one question, and keeping it in each bucket
    # lets the even-distribution pass assign it fairly. Duplicates are removed
    # again when the final list is selected.
    buckets: list[list[dict]] = []
    for keyword in unique_keywords:
        if deadline is not None and time.monotonic() >= deadline:
            log(
                "[book: research time budget reached; skipping the "
                "remaining keyword searches]"
            )
            break
        query = (
            f"when:{WRITE_A_BOOK_RESEARCH_YEARS}y "
            f"{_book_plus_keywords(keyword)}"
        )
        log(f"[book: searching recent articles for {keyword!r}]")
        command = [
            "php",
            str(script),
            "--json",
            "--lazy",
            "--source",
            "googlenews",
            "--search",
            query,
            "--limit",
            str(raw_window),
        ]
        try:
            result = subprocess.run(
                command,
                cwd=SCRIPT_DIR,
                capture_output=True,
                text=True,
                timeout=timeout,
            )
        except (OSError, subprocess.SubprocessError) as exc:
            log(f"[book: search failed for {keyword!r}: {exc}]")
            continue
        items = _read_news_ndjson(result.stdout)
        if not items:
            search_output = "\n".join(
                part for part in (result.stdout, result.stderr) if part
            )
            reason = _book_block_reason(
                search_output, from_google_news=True
            )
            if reason:
                log(f"[book: {reason} while searching for {keyword!r}]")
            elif result.returncode != 0:
                detail = (result.stderr or result.stdout or "").strip()
                detail = detail.splitlines()[-1] if detail else "no results"
                log(f"[book: search failed for {keyword!r}: {detail}]")
        bucket: list[dict] = []
        bucket_seen: set[str] = set()
        for item in items:
            title = " ".join(str(item.get("title") or "").split())
            link = str(item.get("link") or "").strip()
            key = (link or title).casefold()
            if not title or not key or key in bucket_seen:
                continue
            published_ts = _book_news_timestamp(
                str(item.get("published") or "")
            )
            if published_ts is not None and published_ts < cutoff:
                continue
            bucket_seen.add(key)
            bucket.append(
                {
                    "title": title,
                    "link": link,
                    "published": str(item.get("published") or "").strip(),
                    "summary": " ".join(
                        str(item.get("summary") or "").split()
                    ),
                    "paragraphs": _book_clean_paragraphs(
                        item.get("paragraphs") or []
                    ),
                }
            )
            if len(bucket) >= raw_window:
                break
        buckets.append(bucket)

    # Round-robin across the question buckets. Every pass takes at most one
    # article per question, skips stories another question already selected,
    # and stops consuming a bucket once its question has contributed exactly
    # ``per_question_limit`` distinct articles. The loop runs until every
    # question has reached its exact share or its raw window is exhausted,
    # so the pool reflects every sub-question with precisely its own share:
    # never more than its limit, and never fewer unless the search engine
    # ran out of distinct usable stories for it.
    selected: list[dict] = []
    selected_keys: set[str] = set()
    cursors = [0] * len(buckets)
    contributed = [0] * len(buckets)

    def take_one_pass() -> bool:
        progressed = False
        for index, bucket in enumerate(buckets):
            if len(selected) >= limit:
                break
            if contributed[index] >= per_question_limit:
                continue
            while cursors[index] < len(bucket):
                item = bucket[cursors[index]]
                cursors[index] += 1
                key = (
                    str(item.get("link") or item.get("title") or "")
                    .casefold()
                )
                if not key or key in selected_keys:
                    continue
                selected_keys.add(key)
                selected.append(item)
                contributed[index] += 1
                progressed = True
                break
        return progressed

    while len(selected) < limit and take_one_pass():
        pass
    if len(buckets) > 1 and selected:
        log(
            "[book: evenly distributed "
            f"{len(selected)} article(s) across {len(buckets)} "
            f"planning question(s), exactly {per_question_limit} each]"
        )
        log(
            "[book: articles per planning question: "
            + ", ".join(str(count) for count in contributed)
            + "]"
        )
    else:
        for index in range(len(buckets)):
            log(
                f"[book: articles for planning question "
                f"{index + 1}: {contributed[index]}]"
            )
    for index in range(len(buckets)):
        if contributed[index] < per_question_limit:
            log(
                "[book: only "
                f"{contributed[index]} of {per_question_limit} distinct "
                f"article(s) were available for planning question "
                f"{index + 1}]"
            )
    return selected

def _book_load_source(
    item: dict,
    log: Callable[[str], None],
    *,
    fetch_timeout: float = WRITE_A_BOOK_ARTICLE_TIMEOUT_SECONDS,
    deadline: float | None = None,
) -> dict | None:
    """Fetch the full article body when the search list only has a summary."""
    title = str(item.get("title") or "").strip()
    link = str(item.get("link") or "").strip()
    paragraphs = _book_clean_paragraphs(item.get("paragraphs") or [])
    if len(paragraphs) >= 2:
        log(
            "[book: article content available from the search result: "
            f"{link} ({len(paragraphs)} paragraph(s))]"
        )
    elif link and deadline is not None and time.monotonic() >= deadline:
        log(
            "[book: research time budget reached; using the search "
            f"excerpt for: {link}]"
        )
    elif link:
        log(f"[book: fetching article: {link}]")
        try:
            article = fetch_news_article(
                link, title, timeout=fetch_timeout
            )
        except (
            OSError,
            subprocess.SubprocessError,
            RuntimeError,
            ValueError,
        ) as exc:
            detail = str(exc)
            reason = _book_block_reason(detail)
            if reason:
                log(f"[book: {reason}: {link}]")
            else:
                log(f"[book: article fetch failed: {link} ({detail})]")
        else:
            fetched = _book_clean_paragraphs(
                article.get("paragraphs") or []
            )
            article_link = str(article.get("link") or link).strip()
            reason = _book_paragraph_block_reason(fetched)
            if reason:
                log(f"[book: {reason}: {article_link}]")
            elif not fetched:
                log(
                    "[book: article fetch failed: "
                    f"{article_link} (empty article body)]"
                )
            else:
                log(
                    "[book: article fetch succeeded: "
                    f"{article_link} ({len(fetched)} paragraph(s))]"
                )
                if len(fetched) > len(paragraphs):
                    paragraphs = fetched
                    link = article_link
                else:
                    log(
                        "[book: keeping the search excerpt: "
                        f"{article_link} ({len(paragraphs)} paragraph(s))]"
                    )
    else:
        log(
            "[book: article fetch failed: "
            f"{title or 'Untitled'} (no URL)]"
        )
    if not paragraphs:
        summary = " ".join(str(item.get("summary") or "").split())
        if summary:
            paragraphs = [summary]
            log(f"[book: using the search summary for: {link}]")
    if not paragraphs:
        log(
            "[book: article fetch failed: "
            f"{link or title or 'Untitled'} (no content)]"
        )
        return None
    return {
        "title": title or "Untitled",
        "link": link,
        "published": str(item.get("published") or "").strip(),
        "summary": " ".join(str(item.get("summary") or "").split()),
        "paragraphs": paragraphs,
    }


# Cross-component imports are placed after the definitions so a
# feature module can depend on another feature module safely.
from .news import fetch_news_article
