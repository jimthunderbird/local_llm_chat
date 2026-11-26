"""Wikivoyage search and full-article extraction."""

from __future__ import annotations

from .core import *

# /wikivoyage searches the Wikivoyage REST API and renders the destinations as
# a /news-style result list. Each card carries the destination description,
# summary extract, coordinates, and a link to the full Wikivoyage article.
WIKIVOYAGE_SEARCH_URL = "https://en.wikivoyage.org/w/rest.php/v1/search/page"

WIKIVOYAGE_SUMMARY_URL = (
    "https://en.wikivoyage.org/api/rest_v1/page/summary/{title}"
)

WIKIVOYAGE_BASE_URL = "https://en.wikivoyage.org"

WIKIVOYAGE_RESULT_LIMIT = 10

WIKIVOYAGE_SUMMARY_WORKERS = 8

def _wikivoyage_plain_text(value: object) -> str:
    """Turn a Wikivoyage excerpt, extract, or description into plain text."""
    text = html.unescape(re.sub(r"<[^>]*>", "", str(value or "")))
    text = re.sub(r"[ \t]+", " ", text)
    text = re.sub(r"\n{3,}", "\n\n", text)
    return text.strip()

def _wikivoyage_page_link(page: dict, summary: dict) -> str:
    """Return the canonical Wikivoyage article URL for one search page."""
    content_urls = summary.get("content_urls")
    if isinstance(content_urls, dict):
        desktop = content_urls.get("desktop")
        if isinstance(desktop, dict):
            page_url = str(desktop.get("page") or "").strip()
            if page_url:
                return page_url
    key = str(page.get("key") or page.get("title") or "").strip()
    if not key:
        return ""
    return (
        WIKIVOYAGE_BASE_URL
        + "/wiki/"
        + quote(key.replace(" ", "_"), safe="/()_',.:!-~")
    )

def _wikivoyage_summary(key: str) -> dict:
    """Fetch one destination summary, returning an empty dict on failure."""
    if not key:
        return {}
    try:
        response = requests.get(
            WIKIVOYAGE_SUMMARY_URL.format(
                title=quote(key.replace(" ", "_"), safe="")
            ),
            headers={**chat.HTTP_HEADERS, "Accept": "application/json"},
            timeout=(10, 30),
        )
        response.raise_for_status()
        data = response.json()
    except (requests.RequestException, ValueError):
        return {}
    return data if isinstance(data, dict) else {}

def search_wikivoyage_destinations(
    keyword: str, limit: int = WIKIVOYAGE_RESULT_LIMIT
) -> list[dict]:
    """Search Wikivoyage and return normalized destination result cards."""
    keyword = " ".join(str(keyword or "").split())
    if not keyword:
        return []
    try:
        response = requests.get(
            WIKIVOYAGE_SEARCH_URL,
            params={"q": keyword, "limit": max(1, int(limit))},
            headers={**chat.HTTP_HEADERS, "Accept": "application/json"},
            timeout=(10, 30),
        )
        response.raise_for_status()
        data = response.json()
    except (requests.RequestException, ValueError) as exc:
        raise RuntimeError(f"Wikivoyage search failed: {exc}") from exc

    pages = data.get("pages") if isinstance(data, dict) else None
    if not isinstance(pages, list):
        raise RuntimeError("Wikivoyage returned an unexpected response.")
    pages = [page for page in pages if isinstance(page, dict)][: max(1, int(limit))]

    summaries: dict[int, dict] = {}
    if pages:
        with ThreadPoolExecutor(
            max_workers=min(WIKIVOYAGE_SUMMARY_WORKERS, len(pages))
        ) as executor:
            futures = {
                executor.submit(
                    _wikivoyage_summary,
                    str(page.get("key") or page.get("title") or "").strip(),
                ): index
                for index, page in enumerate(pages)
            }
            for future in as_completed(futures):
                try:
                    summaries[futures[future]] = future.result()
                except Exception:  # pragma: no cover - defensive fallback
                    summaries[futures[future]] = {}

    results: list[dict] = []
    for index, page in enumerate(pages):
        summary = summaries.get(index) or {}
        title = _wikivoyage_plain_text(
            summary.get("title") or page.get("title")
        ) or "(untitled)"
        description = _wikivoyage_plain_text(
            summary.get("description") or page.get("description")
        )
        excerpt = _wikivoyage_plain_text(page.get("excerpt"))
        extract = _wikivoyage_plain_text(summary.get("extract")) or excerpt
        link = _wikivoyage_page_link(page, summary)
        if not link and not extract:
            continue

        paragraphs: list[str] = []
        if extract:
            paragraphs.append(extract)
        coordinates = summary.get("coordinates")
        if isinstance(coordinates, dict):
            latitude = coordinates.get("lat")
            longitude = coordinates.get("lon")
            if isinstance(latitude, (int, float)) and isinstance(
                longitude, (int, float)
            ):
                paragraphs.append(
                    f"Coordinates: {latitude:.6g}, {longitude:.6g}"
                )

        card_summary = description
        if extract and extract != description:
            card_summary = f"{description} - {extract}" if description else extract

        thumbnail = page.get("thumbnail") or summary.get("thumbnail") or {}
        image_url = ""
        if isinstance(thumbnail, dict):
            image_url = str(
                thumbnail.get("source") or thumbnail.get("url") or ""
            ).strip()
            if image_url.startswith("//"):
                image_url = "https:" + image_url

        results.append(
            {
                "title": title,
                "link": link,
                "description": description,
                "summary": card_summary,
                "paragraphs": paragraphs,
                "images": [image_url] if image_url else [],
            }
        )
    return results

# The detail modal uses the MediaWiki parse API so a destination opens as the
# complete article, not just the search API's short overview extract. The
# returned HTML is well-formed XML, which lets us keep headings, paragraphs,
# lists, and figures in their original reading order.
WIKIVOYAGE_API_URL = "https://en.wikivoyage.org/w/api.php"

# The normal page-render endpoint is CDN-cached and is not subject to the
# MediaWiki API's tight per-IP request limit. A /wikivoyage search already
# makes one search request plus a summary request per card, so opening a card
# immediately afterwards through action=parse could return HTTP 429 and leave
# the modal showing only the short search extract.
WIKIVOYAGE_RENDER_URL = WIKIVOYAGE_BASE_URL + "/w/index.php"

WIKIVOYAGE_RETRY_MAX_SECONDS = 35.0

WIKIVOYAGE_ARTICLE_CACHE_TTL = 3600.0

_WIKIVOYAGE_ARTICLE_CACHE: dict[str, tuple[float, dict]] = {}

_WIKIVOYAGE_ARTICLE_CACHE_LOCK = threading.Lock()

_WIKIVOYAGE_VOID_TAGS = (
    "area",
    "base",
    "br",
    "col",
    "embed",
    "hr",
    "img",
    "input",
    "link",
    "meta",
    "param",
    "source",
    "track",
    "wbr",
)

_WIKIVOYAGE_PREDEFINED_ENTITIES = {"amp", "apos", "gt", "lt", "quot"}

WIKIVOYAGE_ARTICLE_IMAGE_LIMIT = 80

_WIKIVOYAGE_SKIP_TABLE_CLASSES = (
    "infobox",
    "routebox",
    "article-status",
    "navbox",
    "metadata",
    "noprint",
)

def _wikivoyage_title_from_url(url: str, title: str = "") -> str:
    """Return the article title encoded in a Wikivoyage URL."""
    fallback = " ".join(str(title or "").split())
    parsed = urlparse(str(url or "").strip())
    path = unquote(parsed.path or "")
    if path.startswith("/wiki/"):
        candidate = path[len("/wiki/") :].replace("_", " ").strip()
        candidate = candidate.split("#", 1)[0].strip()
        if candidate and not candidate.lower().startswith("special:"):
            return candidate
    query_title = (parse_qs(parsed.query).get("title") or [""])[0].strip()
    if query_title:
        return query_title
    return fallback

def _wikivoyage_element_text(element) -> str:
    """Return normalized plain text for one parsed HTML element.

    MediaWiki pages embed their templatestyles in <style> elements inside the
    article body. Those rules are presentation, not article text, so exclude
    them (and edit links) from headings, paragraphs, listings, and tables.
    """
    if element is None:
        return ""
    parts: list[str] = []

    def collect(node) -> None:
        tag = str(node.tag or "").lower()
        if tag in {"style", "script", "sup", "nav"}:
            return
        if node.text:
            parts.append(str(node.text))
        for child in node:
            collect(child)
            if child.tail:
                parts.append(str(child.tail))

    collect(element)
    return " ".join("".join(parts).split())

def _wikivoyage_image_url(image) -> str:
    """Return the largest available absolute URL for one article image."""
    sources: list[str] = []
    srcset = str(image.get("srcset") or "").strip()
    # Wikimedia map URLs contain commas in the path (osm-intl,10,...), so a
    # plain split on every comma truncates them to a broken URL. srcset
    # candidates are only separated by a comma followed by the next URL.
    for candidate in re.split(r",\s*(?=https?://)", srcset):
        parts = candidate.strip().split()
        if parts:
            sources.append(parts[0])
    source = str(image.get("src") or "").strip()
    if source:
        sources.append(source)
    if not sources:
        return ""
    source = html.unescape(sources[0]).strip()
    if source.startswith("//"):
        source = "https:" + source
    return source if source.startswith(("http://", "https://")) else ""

class _WikivoyageRequestError(RuntimeError):
    """One Wikivoyage endpoint failed; retry_after carries a 429 hint."""

    def __init__(self, message: str, retry_after: float = 0.0):
        super().__init__(message)
        self.retry_after = max(0.0, float(retry_after))

def _wikivoyage_retry_after(response) -> float:
    """Return the response's Retry-After delay, bounded to a usable wait."""
    raw = str(response.headers.get("Retry-After") or "").strip()
    try:
        seconds = float(raw)
    except ValueError:
        seconds = 5.0
    return max(1.0, min(WIKIVOYAGE_RETRY_MAX_SECONDS, seconds))

def _wikivoyage_request_error(source: str, response) -> _WikivoyageRequestError:
    if response.status_code == 429:
        delay = _wikivoyage_retry_after(response)
        return _WikivoyageRequestError(
            f"{source} is rate-limited (HTTP 429); retry after {delay:.0f}s",
            delay,
        )
    return _WikivoyageRequestError(
        f"{source} returned HTTP {response.status_code}"
    )

def _wikivoyage_render_document(title: str) -> str:
    """Return the full article HTML through the normal CDN-cached page path."""
    try:
        response = requests.get(
            WIKIVOYAGE_RENDER_URL,
            params={"title": title, "action": "render"},
            headers={**chat.HTTP_HEADERS, "Accept": "text/html"},
            timeout=(10, 60),
        )
    except requests.RequestException as exc:
        raise _WikivoyageRequestError(f"page render failed: {exc}") from exc
    if response.status_code != 200:
        raise _wikivoyage_request_error("page render", response)
    document = response.text
    if not document.strip():
        raise _WikivoyageRequestError("page render returned an empty article")
    return document

def _wikivoyage_parse_document(title: str) -> str:
    """Return the full article HTML through the MediaWiki parse API."""
    try:
        response = requests.get(
            WIKIVOYAGE_API_URL,
            params={
                "action": "parse",
                "page": title,
                "prop": "text",
                "format": "json",
                "formatversion": 2,
                "redirects": 1,
                "disableeditsection": 1,
            },
            headers={**chat.HTTP_HEADERS, "Accept": "application/json"},
            timeout=(10, 60),
        )
    except requests.RequestException as exc:
        raise _WikivoyageRequestError(f"parse API failed: {exc}") from exc
    if response.status_code != 200:
        raise _wikivoyage_request_error("MediaWiki parse API", response)
    try:
        payload = response.json()
    except ValueError as exc:
        raise _WikivoyageRequestError(
            f"MediaWiki parse API returned invalid JSON: {exc}"
        ) from exc
    parse = payload.get("parse") if isinstance(payload, dict) else None
    if not isinstance(parse, dict):
        detail = ""
        if isinstance(payload, dict):
            error = payload.get("error")
            if isinstance(error, dict):
                detail = str(error.get("info") or "").strip()
        raise _WikivoyageRequestError(
            f"MediaWiki parse API: {detail or 'unexpected API response'}"
        )
    document = str(parse.get("text") or "")
    if not document.strip():
        raise _WikivoyageRequestError(
            "MediaWiki parse API returned an empty article"
        )
    return document

def _wikivoyage_fetch_document(title: str) -> tuple[str, bool]:
    """Fetch one complete destination article, returning (document, is_html).

    The rendered page is preferred because a /wikivoyage search has just made
    several API requests, and the parse API can answer the immediate modal
    open with HTTP 429. The API remains the fallback. If both endpoints are
    rate-limited, wait for the advertised Retry-After and try once more.
    """
    errors: list[str] = []
    for attempt in range(2):
        retry_after = 0.0
        for source, html_document, fetch in (
            ("page render", True, _wikivoyage_render_document),
            ("MediaWiki parse API", False, _wikivoyage_parse_document),
        ):
            try:
                return fetch(title), html_document
            except _WikivoyageRequestError as exc:
                errors.append(str(exc))
                retry_after = max(retry_after, exc.retry_after)
        if attempt == 0 and retry_after > 0:
            time.sleep(retry_after)
            continue
        break
    detail = errors[-1] if errors else "unexpected response"
    raise RuntimeError(f"Could not load the Wikivoyage article: {detail}")

def _wikivoyage_cached_article(key: str) -> dict | None:
    """Return a recently fetched article, or None when the cache misses."""
    now = time.time()
    with _WIKIVOYAGE_ARTICLE_CACHE_LOCK:
        entry = _WIKIVOYAGE_ARTICLE_CACHE.get(key)
        if entry is None:
            return None
        fetched_at, article = entry
        if now - fetched_at > WIKIVOYAGE_ARTICLE_CACHE_TTL:
            _WIKIVOYAGE_ARTICLE_CACHE.pop(key, None)
            return None
        return article

def _wikivoyage_store_article(key: str, article: dict) -> None:
    """Keep a small in-memory cache so reopening a card needs no request."""
    with _WIKIVOYAGE_ARTICLE_CACHE_LOCK:
        if len(_WIKIVOYAGE_ARTICLE_CACHE) >= 16:
            oldest = sorted(
                _WIKIVOYAGE_ARTICLE_CACHE.items(), key=lambda item: item[1][0]
            )[:4]
            for stale_key, _ in oldest:
                _WIKIVOYAGE_ARTICLE_CACHE.pop(stale_key, None)
        _WIKIVOYAGE_ARTICLE_CACHE[key] = (time.time(), article)

def _wikivoyage_named_entity(match: re.Match[str]) -> str:
    """Convert an HTML named entity to an XML-safe numeric reference."""
    name = match.group(1)
    if name in _WIKIVOYAGE_PREDEFINED_ENTITIES:
        return match.group(0)
    decoded = html.unescape(match.group(0))
    if decoded == match.group(0):
        return "&amp;" + name + ";"
    return "".join(f"&#{ord(character)};" for character in decoded)

def _wikivoyage_html_to_xml(document: str) -> str:
    """Make the action=render HTML parseable by the shared block walker."""
    text = re.sub(r"<!--.*?-->", "", str(document or ""), flags=re.S)
    void_pattern = "|".join(_WIKIVOYAGE_VOID_TAGS)
    text = re.sub(
        rf"<({void_pattern})(\b[^>]*?)(?<!/)>",
        r"<\1\2/>",
        text,
        flags=re.I,
    )
    text = re.sub(r"&([A-Za-z][A-Za-z0-9]+);", _wikivoyage_named_entity, text)
    text = re.sub(
        r"&(?!#\d+;|#x[0-9A-Fa-f]+;|amp;|lt;|gt;|quot;|apos;)",
        "&amp;",
        text,
    )
    return "<root>" + text + "</root>"

def _wikivoyage_article_blocks(document: str, *, html_document: bool = False) -> list[dict]:
    """Turn MediaWiki article HTML into ordered blocks for the detail modal."""
    if html_document:
        document = _wikivoyage_html_to_xml(document)
    try:
        root = ElementTree.fromstring(document)
    except ElementTree.ParseError:
        # Defensive fallback: still return readable text if the parser output
        # is ever not well-formed XML.
        text = html.unescape(re.sub(r"<[^>]+>", " ", document))
        text = " ".join(text.split())
        return [{"type": "paragraph", "text": text}] if text else []

    blocks: list[dict] = []
    seen_images: set[str] = set()
    image_count = 0

    def add_image(image, caption: str = "") -> None:
        nonlocal image_count
        if image_count >= WIKIVOYAGE_ARTICLE_IMAGE_LIMIT:
            return
        url = _wikivoyage_image_url(image)
        if not url:
            return
        key = url.split("?", 1)[0]
        if key in seen_images:
            return
        seen_images.add(key)
        image_count += 1
        alt = " ".join(str(image.get("alt") or "").split())
        blocks.append(
            {
                "type": "image",
                "url": url,
                "caption": " ".join(str(caption or "").split()),
                "alt": alt,
            }
        )

    def walk(node) -> None:
        tag = str(node.tag or "").lower()
        classes = str(node.get("class") or "").lower()
        if tag in {"style", "script", "link", "meta", "nav", "sup"}:
            return
        if tag == "div" and "mw-heading" in classes:
            heading = next(
                (
                    child
                    for child in node
                    if str(child.tag or "").lower()
                    in {"h1", "h2", "h3", "h4", "h5", "h6"}
                ),
                None,
            )
            if heading is not None:
                text = _wikivoyage_element_text(heading)
                if text:
                    blocks.append(
                        {
                            "type": "heading",
                            "level": int(str(heading.tag)[1]),
                            "text": text,
                        }
                    )
                return
        if tag in {"h1", "h2", "h3", "h4", "h5", "h6"}:
            text = _wikivoyage_element_text(node)
            if text:
                blocks.append(
                    {"type": "heading", "level": int(tag[1]), "text": text}
                )
            return
        if tag == "figure":
            caption = _wikivoyage_element_text(node.find("figcaption"))
            for image in node.iter("img"):
                add_image(image, caption)
                caption = ""
            return
        if tag == "img":
            width = str(image_size(node))
            if width.isdigit() and int(width) >= 160:
                add_image(node)
            return
        if tag == "p":
            for image in node.iter("img"):
                width = str(image_size(image))
                if width.isdigit() and int(width) >= 160:
                    add_image(image)
            text = _wikivoyage_element_text(node)
            if text:
                blocks.append({"type": "paragraph", "text": text})
            return
        if tag in {"ul", "ol"}:
            items = [
                _wikivoyage_element_text(child)
                for child in node
                if str(child.tag or "").lower() == "li"
            ]
            items = [item for item in items if item]
            if items:
                blocks.append(
                    {"type": "list", "ordered": tag == "ol", "items": items}
                )
            return
        if tag == "dl":
            for child in node:
                if str(child.tag or "").lower() not in {"dt", "dd"}:
                    continue
                text = _wikivoyage_element_text(child)
                if text:
                    blocks.append({"type": "paragraph", "text": text})
            return
        if tag == "table":
            if any(name in classes for name in _WIKIVOYAGE_SKIP_TABLE_CLASSES):
                return
            rows: list[str] = []
            for row in node.iter("tr"):
                cells = [
                    _wikivoyage_element_text(cell)
                    for cell in row
                    if str(cell.tag or "").lower() in {"td", "th"}
                ]
                cells = [cell for cell in cells if cell]
                if cells:
                    rows.append(" - ".join(cells))
            if rows:
                blocks.append(
                    {"type": "list", "ordered": False, "items": rows}
                )
            return
        for child in node:
            walk(child)

    def image_size(image) -> int | str:
        for attribute in ("data-file-width", "width"):
            value = str(image.get(attribute) or "").strip()
            if value.isdigit():
                return int(value)
        return ""

    walk(root)
    return blocks

def fetch_wikivoyage_article(url: str, title: str = "") -> dict:
    """Fetch the complete Wikivoyage article behind a search result card."""
    article_title = _wikivoyage_title_from_url(url, title)
    if not article_title:
        raise ValueError("Missing Wikivoyage article title")

    cache_key = article_title.casefold()
    cached = _wikivoyage_cached_article(cache_key)
    if cached is not None:
        return cached

    document, html_document = _wikivoyage_fetch_document(article_title)
    blocks = _wikivoyage_article_blocks(document, html_document=html_document)
    paragraphs: list[str] = []
    images: list[str] = []
    seen_paragraphs: set[str] = set()
    seen_images: set[str] = set()
    for block in blocks:
        kind = block.get("type")
        if kind == "heading":
            text = str(block.get("text") or "").strip()
            if text and text not in seen_paragraphs:
                seen_paragraphs.add(text)
                paragraphs.append(text)
        elif kind == "paragraph":
            text = str(block.get("text") or "").strip()
            if text and text not in seen_paragraphs:
                seen_paragraphs.add(text)
                paragraphs.append(text)
        elif kind == "list":
            for value in block.get("items") or []:
                text = str(value or "").strip()
                if text and text not in seen_paragraphs:
                    seen_paragraphs.add(text)
                    paragraphs.append(text)
        elif kind == "image":
            image_url = str(block.get("url") or "").strip()
            key = image_url.split("?", 1)[0]
            if image_url and key not in seen_images:
                seen_images.add(key)
                images.append(image_url)

    canonical_title = article_title
    link = WIKIVOYAGE_BASE_URL + "/wiki/" + quote(
        canonical_title.replace(" ", "_"), safe="/()_',.:!-~"
    )
    article = {
        "title": canonical_title,
        "link": link,
        "paragraphs": paragraphs,
        "images": images,
        "blocks": blocks,
    }
    _wikivoyage_store_article(cache_key, article)
    return article
