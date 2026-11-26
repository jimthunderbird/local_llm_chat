"""MDN and Encyclopaedia Britannica documentation search."""

from __future__ import annotations

from .core import *

# /mdn searches the public MDN Web Docs search API and renders the matches as
# a /news-style result list. Each result opens in the existing story modal so
# readers can translate, read aloud, or follow the original documentation link.
MDN_SEARCH_URL = "https://developer.mozilla.org/api/v1/search"

MDN_DOCS_BASE_URL = "https://developer.mozilla.org"

MDN_LOCALE = "en-US"

MDN_RESULT_LIMIT = 10

MDN_HIGHLIGHT_LIMIT = 3

# /britanica searches Encyclopædia Britannica's encyclopedia and renders the
# top matches as a /news-style result list. The canonical search page on
# www.britannica.com is protected by a Cloudflare challenge for non-browser
# clients, so it is requested first and school.eb.com (Britannica's school
# portal, backed by the same article database) is used as the reader fallback.
BRITANNICA_SEARCH_URL = "https://www.britannica.com/search"

BRITANNICA_SCHOOL_SEARCH_URL = "https://school.eb.com/search"

BRITANNICA_BASE_URL = "https://www.britannica.com"

BRITANNICA_SCHOOL_BASE_URL = "https://school.eb.com"

BRITANNICA_RESULT_LIMIT = 5

BRITANNICA_SOURCE_LABEL = "Encyclopædia Britannica"

BRITANNICA_HEADERS = {
    **chat.HTTP_HEADERS,
    "User-Agent": (
        "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
        "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/131.0.0.0 Safari/537.36"
    ),
    "Accept": (
        "text/html,application/xhtml+xml,application/xml;q=0.9,"
        "image/avif,image/webp,*/*;q=0.8"
    ),
    "Accept-Language": "en-US,en;q=0.9",
}

_BRITANNICA_ARTICLE_RE = re.compile(r"/article/([^/]+)/(\d+)")

_BRITANNICA_BLOCKED_MARKERS = (
    "just a moment...",
    "challenge-platform",
    "cf-chl-",
)

def _mdn_plain_text(value: object) -> str:
    """Turn one MDN summary or highlighted snippet into readable text."""
    if isinstance(value, list):
        return "\n\n".join(
            text for text in (_mdn_plain_text(item) for item in value) if text
        )
    text = html.unescape(re.sub(r"<[^>]*>", "", str(value or "")))
    text = re.sub(r"[ \t]+", " ", text)
    text = re.sub(r"\n{3,}", "\n\n", text)
    return text.strip()

def search_mdn_docs(keyword: str, limit: int = MDN_RESULT_LIMIT) -> list[dict]:
    """Search MDN Web Docs and return normalized documentation result cards."""
    keyword = " ".join(str(keyword or "").split())
    if not keyword:
        return []
    try:
        response = requests.get(
            MDN_SEARCH_URL,
            params={"q": keyword, "locale": MDN_LOCALE},
            headers={**chat.HTTP_HEADERS, "Accept": "application/json"},
            timeout=(10, 30),
        )
        response.raise_for_status()
        data = response.json()
    except (requests.RequestException, ValueError) as exc:
        raise RuntimeError(f"MDN search failed: {exc}") from exc

    documents = data.get("documents") if isinstance(data, dict) else None
    if not isinstance(documents, list):
        raise RuntimeError("MDN returned an unexpected response.")

    results: list[dict] = []
    for document in documents[: max(1, int(limit))]:
        if not isinstance(document, dict):
            continue
        mdn_url = str(document.get("mdn_url") or "").strip()
        title = _mdn_plain_text(document.get("title")) or "(untitled)"
        summary = _mdn_plain_text(document.get("summary"))
        if not mdn_url and not summary:
            continue
        link = (
            MDN_DOCS_BASE_URL + mdn_url
            if mdn_url.startswith("/")
            else mdn_url
        )
        passages: list[str] = []
        highlight = document.get("highlight")
        bodies = highlight.get("body") if isinstance(highlight, dict) else None
        if isinstance(bodies, list):
            for raw in bodies[:MDN_HIGHLIGHT_LIMIT]:
                passage = _mdn_plain_text(raw)
                if (
                    passage
                    and passage not in passages
                    and passage.casefold() not in summary.casefold()
                ):
                    passages.append(passage)
        results.append(
            {
                "title": title,
                "link": link,
                "summary": summary,
                "paragraphs": passages,
            }
        )
    return results

def _britannica_clean_text(value: object) -> str:
    """Collapse markup and whitespace in one Britannica search field."""
    text = html.unescape(re.sub(r"<[^>]*>", " ", str(value or "")))
    text = " ".join(text.split())
    return re.sub(r"\s+([,.;:!?])", r"\1", text).strip()

def _britannica_summary(value: object) -> str:
    """Render a Britannica search snippet without its leading match label."""
    text = _britannica_clean_text(value)
    if "|" in text:
        label, snippet = text.split("|", 1)
        if len(label.split()) <= 8:
            text = snippet.strip()
    return text

class _BritannicaSearchHTMLParser(HTMLParser):
    """Collect article links and summaries from a Britannica search page."""

    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.entries: list[dict[str, str]] = []
        self._title_parts: list[str] | None = None
        self._summary_parts: list[str] | None = None

    def handle_starttag(
        self, tag: str, attrs: list[tuple[str, str | None]]
    ) -> None:
        attributes = {
            name.lower(): value or "" for name, value in attrs
        }
        classes = attributes.get("class", "").split()
        if tag == "a" and "primary-article-link" in classes:
            self._title_parts = []
            self.entries.append(
                {
                    "href": attributes.get("href", "").strip(),
                    "title": "",
                    "summary": "",
                }
            )
        elif tag == "p" and "h6" in classes:
            self._summary_parts = []

    def handle_data(self, data: str) -> None:
        if self._title_parts is not None:
            self._title_parts.append(data)
        elif self._summary_parts is not None:
            self._summary_parts.append(data)

    def handle_endtag(self, tag: str) -> None:
        if tag == "a" and self._title_parts is not None:
            if self.entries:
                self.entries[-1]["title"] = _britannica_clean_text(
                    " ".join(self._title_parts)
                )
            self._title_parts = None
        elif tag == "p" and self._summary_parts is not None:
            summary = _britannica_summary(" ".join(self._summary_parts))
            if self.entries and not self.entries[-1]["summary"]:
                self.entries[-1]["summary"] = summary
            self._summary_parts = None

def _britannica_absolute_url(base_url: str, href: str) -> str:
    """Resolve a search result href against its Britannica portal."""
    if href.startswith(("http://", "https://")):
        return href
    return base_url.rstrip("/") + "/" + href.lstrip("/")

def _britannica_results(
    entries: list[dict[str, str]], base_url: str, limit: int
) -> list[dict]:
    """Deduplicate Britannica entries by article id and keep the top results."""
    results: list[dict] = []
    index_by_key: dict[str, int] = {}
    for entry in entries:
        href = str(entry.get("href") or "").strip()
        match = _BRITANNICA_ARTICLE_RE.search(href)
        if match is None:
            continue
        key = match.group(2)
        title = _britannica_clean_text(entry.get("title"))
        if not title:
            title = match.group(1).replace("-", " ")
        summary = _britannica_summary(entry.get("summary"))
        if key not in index_by_key:
            index_by_key[key] = len(results)
            results.append(
                {
                    "title": title,
                    "link": _britannica_absolute_url(base_url, href),
                    "summary": summary,
                }
            )
            continue
        # A top-level result can be followed by section matches for the same
        # article. Borrow the first section snippet when the article itself
        # has no snippet, so every card carries useful text.
        current = results[index_by_key[key]]
        if not current["summary"] and summary:
            current["summary"] = summary
    return results[: max(1, int(limit))]

def search_britannica_articles(
    keyword: str, limit: int = BRITANNICA_RESULT_LIMIT
) -> list[dict]:
    """Search Britannica and return normalized article result cards."""
    keyword = " ".join(str(keyword or "").split())
    if not keyword:
        return []

    failures: list[str] = []
    endpoints = (
        (BRITANNICA_SEARCH_URL, BRITANNICA_BASE_URL),
        (BRITANNICA_SCHOOL_SEARCH_URL, BRITANNICA_SCHOOL_BASE_URL),
    )
    for search_url, base_url in endpoints:
        host = urlparse(search_url).netloc or search_url
        try:
            response = requests.get(
                search_url,
                params={"query": keyword},
                headers=BRITANNICA_HEADERS,
                timeout=(10, 30),
            )
            response.raise_for_status()
            page = response.text
        except requests.RequestException as exc:
            failures.append(f"{host}: {exc}")
            continue

        lowered = page.casefold()
        if any(marker in lowered for marker in _BRITANNICA_BLOCKED_MARKERS):
            failures.append(f"{host}: blocked by a bot challenge")
            continue

        parser = _BritannicaSearchHTMLParser()
        try:
            parser.feed(page)
        except Exception as exc:  # HTMLParser is lenient; keep the fallback.
            failures.append(f"{host}: {exc}")
            continue
        results = _britannica_results(parser.entries, base_url, limit)
        if results:
            return results
        failures.append(f"{host}: no matching article results")

    detail = "; ".join(failures) or "no search endpoint responded"
    raise RuntimeError(f"Britannica search failed: {detail}")
