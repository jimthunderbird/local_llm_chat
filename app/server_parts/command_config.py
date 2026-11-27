"""Slash-command argument catalogues and documentation URLs."""

from __future__ import annotations

from .core import *
from .core import SCRIPT_DIR

COMMAND_ARGUMENTS = {
    "/youtube",
    "/ask",
    "/game",
    "/wiki",
    "/wiki_article",
    "/article",
    "/dict",
    "/depth",
    "/benchmark",
    "/news",
    "/mdn",
    "/britanica",
    "/googlenews",
    "/gkg",
    "/howstuffworks",
    "/tablelog",
    "/stock",
    "/robinhood",
    "/zillow",
    "/moonbit",
    "/sat",
    "/medium",
    "/gutenberg",
    "/openlibrary",
    "/internet_archive",
    "/stackoverflow",
    "/deepresearch",
    "/research_with_the_news",
    "/anthem",
    "/united_healthcare",
    "/write_a_book",
    "/settings",
}

# Subcommands grouped under /gutenberg and /openlibrary.
GUTENBERG_SUBCOMMANDS = {
    "search": "search Project Gutenberg by title, author, or keyword",
}

OPENLIBRARY_SUBCOMMANDS = {
    "search": "search inside Open Library books by keyword",
}

STACKOVERFLOW_SUBCOMMANDS = {
    "tag": "list the newest questions for one Stack Overflow tag",
    "search": "search Stack Overflow questions by keyword, newest first",
}

# Project Gutenberg search and per-book chat. /gutenberg search mirrors the
# /news search flow: it asks news_fetch.php for a Google News RSS search
# restricted to site:gutenberg.org, resolves each Google News redirect to its
# gutenberg.org page, and turns the matching ebook links into book cards. The
# Open Library search API and the Project Gutenberg catalogue search page stay
# as a fallback when the news feed has no usable book links. The chosen
# plaintext is downloaded once and cached under .chat. Book chat reuses the
# /article section-aware RAG pipeline, so a multi-megabyte novel never goes
# into the model's context at once.
OPENLIBRARY_SEARCH_URL = "https://openlibrary.org/search.json"

OPENLIBRARY_INSIDE_SEARCH_URL = "https://openlibrary.org/search/inside.json"

OPENLIBRARY_BOOK_PAGE_URL = "https://openlibrary.org{key}"

OPENLIBRARY_WORK_JSON_URL = "https://openlibrary.org{key}.json"

OPENLIBRARY_COVER_ID_URL = "https://covers.openlibrary.org/b/id/{cover_id}-M.jpg"

OPENLIBRARY_COVER_OLID_URL = "https://covers.openlibrary.org/b/olid/{olid}-M.jpg"

OPENLIBRARY_SEARCH_FIELDS = (
    "key,title,author_name,first_publish_year,language,subject,"
    "id_project_gutenberg,cover_i,cover_edition_key,edition_count,ebook_access"
)

OPENLIBRARY_RESULT_LIMIT = 20

OPENLIBRARY_METADATA_SEARCH_LIMIT = 60

OPENLIBRARY_HIGHLIGHT_LIMIT = 3

OPENLIBRARY_DESCRIPTION_LIMIT = 520

OPENLIBRARY_DESCRIPTION_CACHE: dict[str, str] = {}

OPENLIBRARY_DESCRIPTION_CACHE_LOCK = threading.Lock()

# Internet Archive search. The beta full-text search endpoint powers the
# site's "Search text contents" mode and returns matching OCR passages plus
# item metadata without an API key. The Advanced Search endpoint is the
# catalogue fallback. Both are queried with an explicit JSON Accept header:
# the beta endpoint answers some requests with a flat 404 without it.
INTERNET_ARCHIVE_FTS_URL = "https://archive.org/services/search/beta/page_production/"

INTERNET_ARCHIVE_SEARCH_URL = "https://archive.org/advancedsearch.php"

INTERNET_ARCHIVE_DETAILS_URL = "https://archive.org/details/{identifier}"

INTERNET_ARCHIVE_METADATA_URL = "https://archive.org/metadata/{identifier}"

INTERNET_ARCHIVE_DOWNLOAD_URL = (
    "https://archive.org/download/{identifier}/{filename}"
)

INTERNET_ARCHIVE_COVER_URL = "https://archive.org/services/img/{identifier}"

INTERNET_ARCHIVE_SEARCH_FIELDS = (
    "identifier,title,creator,year,date,publicdate,addeddate,description,"
    "downloads,language,subject,mediatype,collection,lending___status,"
    "access-restricted-item"
)

# The beta full-text endpoint only returns fields named in this parameter.
# `access-restricted-item` is not exposed by that index, so lending status and
# collection membership are used alongside it when classifying an item.
INTERNET_ARCHIVE_FTS_FIELDS = (
    "identifier,title,creator,year,date,description,downloads,"
    "language,subject,mediatype,collection,lending___status"
)

INTERNET_ARCHIVE_RESTRICTED_COLLECTIONS = frozenset(
    {
        "borrowable",
        "inlibrary",
        "lendinglibrary",
        "printdisabled",
    }
)

INTERNET_ARCHIVE_RESTRICTED_LENDING_STATUSES = frozenset(
    {
        "available_to_borrow",
        "available_to_browse",
        "is_browsable",
        "is_lendable",
        "is_printdisabled",
    }
)

INTERNET_ARCHIVE_RESULT_LIMIT = 20

INTERNET_ARCHIVE_HIGHLIGHT_LIMIT = 3

INTERNET_ARCHIVE_DESCRIPTION_LIMIT = 520

# Item metadata/file lists keyed by identifier, so each search enriches every
# public card with one metadata request instead of one per data point.
INTERNET_ARCHIVE_METADATA_CACHE: dict[str, dict] = {}

INTERNET_ARCHIVE_METADATA_CACHE_LOCK = threading.Lock()

INTERNET_ARCHIVE_HEADERS = {
    **chat.HTTP_HEADERS,
    "User-Agent": "Mozilla/5.0 (compatible; personal-chatbot/1.0)",
    "Accept": "application/json",
}

GUTENBERG_SEARCH_URL = "https://www.gutenberg.org/ebooks/search/"

GUTENBERG_SEARCH_BLOCK_RE = re.compile(
    r'<li class="booklink">(.*?)</li>', re.IGNORECASE | re.DOTALL
)

GUTENBERG_SEARCH_LINK_RE = re.compile(
    r'<a class="link" href="/ebooks/(\d+)"', re.IGNORECASE
)

GUTENBERG_SEARCH_TITLE_RE = re.compile(
    r'<span class="title">(.*?)</span>', re.IGNORECASE | re.DOTALL
)

GUTENBERG_SEARCH_AUTHOR_RE = re.compile(
    r'<span class="subtitle">(.*?)</span>', re.IGNORECASE | re.DOTALL
)

GUTENBERG_SEARCH_EXTRA_RE = re.compile(
    r'<span class="extra">(.*?)</span>', re.IGNORECASE | re.DOTALL
)

GUTENBERG_SEARCH_DOWNLOADS_RE = re.compile(r"([\d,]+)\s+downloads", re.IGNORECASE)

GUTENBERG_NEWS_SITE_QUERY = "site:gutenberg.org"

GUTENBERG_NEWS_BOOK_LINK_RE = re.compile(
    r"gutenberg\.org/(?:"
    r"ebooks/(?P<ebook>\d+)/?(?:[?#]|$)"
    r"|cache/epub/(?P<cache>\d+)/"
    r"|files/(?P<files>\d+)/"
    r")",
    re.IGNORECASE,
)

GUTENBERG_NEWS_TITLE_SUFFIX_RE = re.compile(
    r"\s*[|\u2013\u2014-]\s*Project Gutenberg\s*$", re.IGNORECASE
)

GUTENBERG_NEWS_BY_RE = re.compile(
    r"^(?P<title>.+)\s+by\s+(?P<authors>.+)$", re.IGNORECASE
)

GUTENBERG_BOOK_URL = "https://www.gutenberg.org/ebooks/{book_id}"

GUTENBERG_COVER_URL = (
    "https://www.gutenberg.org/cache/epub/{book_id}/pg{book_id}.cover.medium.jpg"
)

GUTENBERG_BOOK_CACHE_DIR = SCRIPT_DIR / ".chat" / "gutenberg_cache"

GUTENBERG_BOOK_RESULT_LIMIT = 12

GUTENBERG_BOOK_STATE_LIMIT = 3

GUTENBERG_TEXT_MAX_BYTES = 30 * 1024 * 1024

GUTENBERG_START_MARKER_RE = re.compile(
    r"\*\*\*\s*START OF (?:THE|THIS) PROJECT GUTENBERG EBOOK.*?\*\*\*",
    re.IGNORECASE | re.DOTALL,
)

GUTENBERG_END_MARKER_RE = re.compile(
    r"\*\*\*\s*END OF (?:THE|THIS) PROJECT GUTENBERG EBOOK.*?\*\*\*",
    re.IGNORECASE | re.DOTALL,
)

GUTENBERG_OLD_END_RE = re.compile(
    r"\n\s*End of (?:the )?Project Gutenberg(?:'s)?.*\Z",
    re.IGNORECASE | re.DOTALL,
)

GUTENBERG_CHAPTER_RE = re.compile(
    r"^\s*(?:CHAPTER|BOOK|PART|VOLUME|ACT|SCENE)\s+"
    r"(?:[IVXLCDM]+|\d+)\b[^\n]*$",
    re.IGNORECASE,
)

GUTENBERG_BOOK_ID_RE = re.compile(r"\d{1,8}")

# Subcommands grouped under /youtube. Kept in display order for Tab
# completion and the /help panel.
YOUTUBE_SUBCOMMANDS = {
    "learn": "fetch one transcript and chunk it into RAG trunks",
    "relearn": "rebuild one video's title and trunks from its saved transcript",
    "learn_topic": "search YouTube for <topic> and learn the top 3 videos",
    "learn_playlist": "learn every video in a YouTube playlist",
    "search": "load the YouTube search-results page for <keyword> and show matching videos with channel, duration, and publish date",
    "transcript": "use one transcript by hash/URL (learning it first if needed), search titles with ag, clear accumulated context, and switch to Local mode",
    "search_transcript": "search transcript titles with ag and pick a video",
    "bookmark": "list bookmarked videos, latest first: /youtube bookmark list",
}

# /youtube search shows six cards at a time. The "More" button below the
# cards asks /api/youtube_more for the next six from the same cached search.
# Search itself reads YouTube's own results page, so the cards also carry the
# channel, duration, and relative publish date.
YOUTUBE_SEARCH_PAGE_SIZE = 6

# One YouTube results page normally contains about twenty videoRenderer
# entries.  Fetching all of them at once would be slower than painting the
# first six, so they are cached and revealed by the "More" button.
YOUTUBE_SEARCH_PAGE_RESULTS = 20

# Cache at most this many results per keyword, even after many "More" clicks.
YOUTUBE_SEARCH_MAX_RESULTS = YOUTUBE_SEARCH_PAGE_RESULTS

# After the first page is painted, quietly fill the cache with the remaining
# videos from the results page.  The "More" button then reads from memory
# instead of paying for another search.
YOUTUBE_SEARCH_PREFETCH_RESULTS = YOUTUBE_SEARCH_PAGE_RESULTS

# How long a "More" request will wait for that prefetch before doing its own
# search.  A prefetch normally finishes in a few seconds.
YOUTUBE_PREFETCH_WAIT_SECONDS = 8.0

def strip_surrounding_search_quotes(keyword: str) -> str:
    """Normalize a command/API search keyword like ``/youtube search "..."``.

    The slash command receives shell-style quotes only when typed directly in
    the terminal.  The browser API sends JSON, so it applies the same
    normalization explicitly: a single pair of surrounding single or double
    quotes groups the phrase but is not part of the text sent to YouTube.
    """
    keyword = (keyword or "").strip()
    if (
        len(keyword) >= 2
        and keyword[0] == keyword[-1]
        and keyword[0] in "\"'"
    ):
        return keyword[1:-1].strip()
    return keyword
