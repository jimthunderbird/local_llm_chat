#!/usr/bin/env python3
"""Search YouTube through Google News RSS.

Usage:
    python3 youtube_news_search.py --json --limit 6 -- "jensen huang"
    python3 youtube_news_search.py --progressive-json --limit 6 \\
        [--exclude VIDEO_IDS] -- "jensen huang"

The search query is sent to Google News RSS:

    https://news.google.com/rss/search?q=site:youtube.com+<keywords>
        &hl=en-US&gl=US&ceid=US:en

Only feed items whose ``<source>`` is YouTube are kept.  Google News hands
out redirect links, so each item is resolved to its real publisher URL and
only real YouTube video URLs are returned.  The result contains the video id,
title, watch URL, publish date (when the feed provides one), and the video's
maxres YouTube thumbnail.

With ``--json`` one normalized JSON object is printed per line.  With
``--progressive-json`` a single final ``{"type": "videos", ...}`` event is
printed, matching the /youtube search UI's progressive protocol.
"""

from __future__ import annotations

import argparse
import json
import os
import re
import sys
import urllib.parse
import urllib.request
from concurrent.futures import ThreadPoolExecutor
from datetime import timezone
from email.utils import parsedate_to_datetime
from pathlib import Path
from xml.etree import ElementTree

USER_AGENT = (
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
    "AppleWebKit/537.36 (KHTML, like Gecko) "
    "Chrome/125.0.0.0 Safari/537.36"
)
GOOGLE_NEWS_SEARCH_URL = "https://news.google.com/rss/search"
GOOGLE_NEWS_BATCH_URL = (
    "https://news.google.com/_/DotsSplashUi/data/batchexecute"
)
HTTP_TIMEOUT = 15
RESOLVE_WORKERS = 12
VIDEO_ID_RE = re.compile(r"^[A-Za-z0-9_-]{11}$")
ARTICLE_ID_RE = re.compile(r"/articles/([^/?#]+)")
YOUTUBE_SEARCH_PAGE_URL = "https://www.youtube.com/results"
YOUTUBE_MOBILE_SEARCH_PAGE_URL = "https://m.youtube.com/results"
# YouTube has shipped the search payload in several forms over time:
#   var ytInitialData = {...};
#   window["ytInitialData"] = {...};
#   window.ytInitialData = {...};
# Match only the assignment; the JSON itself is decoded below with
# ``JSONDecoder.raw_decode`` so a ``</script>`` inside a string, a missing
# trailing semicolon, or other markup after the object cannot break it.
YT_INITIAL_DATA_ASSIGNMENT_RE = re.compile(
    r"(?:var\s+ytInitialData\s*="
    r"|window\s*\[\s*[\"']ytInitialData[\"']\s*\]\s*="
    r"|window\.ytInitialData\s*="
    r"|(?<![\w.$])ytInitialData\s*=)",
)
TIMESTAMP_RE = re.compile(r'data-n-a-ts="(\d+)"')
SIGNATURE_RE = re.compile(r'data-n-a-sg="([^"]+)"')
# Parse YouTube's relative publish time ("3 days ago", "5mo ago", ...).
# YouTube mixes long and short units, so accept both forms and put the
# longer alternatives first ("mo" must win over "m").
PUBLISHED_AGO_RE = re.compile(
    r"(?P<value>\d[\d,]*)\s*"
    r"(?P<unit>years?|yrs?|y|months?|mos?|mo|weeks?|wks?|w|"
    r"days?|d|hours?|hrs?|hr|h|minutes?|mins?|min|m|"
    r"seconds?|secs?|sec|s)\s+ago",
    re.IGNORECASE,
)
PUBLISHED_UNIT_SECONDS = {
    "y": 365 * 86400,
    "yr": 365 * 86400,
    "year": 365 * 86400,
    "mo": 30 * 86400,
    "mon": 30 * 86400,
    "month": 30 * 86400,
    "w": 7 * 86400,
    "wk": 7 * 86400,
    "week": 7 * 86400,
    "d": 86400,
    "day": 86400,
    "h": 3600,
    "hr": 3600,
    "hour": 3600,
    "m": 60,
    "min": 60,
    "minute": 60,
    "s": 1,
    "sec": 1,
    "second": 1,
}
CACHE_PATH = Path(__file__).resolve().parent / ".chat" / "youtube_news_links.json"
CACHE_LIMIT = 5000


def _fetch(url: str, *, data: bytes | None = None,
           headers: dict[str, str] | None = None) -> str:
    request_headers = {
        "User-Agent": USER_AGENT,
        "Accept-Language": "en-US,en;q=0.9",
    }
    if headers:
        request_headers.update(headers)
    request = urllib.request.Request(
        url, data=data, headers=request_headers, method="POST" if data else "GET"
    )
    with urllib.request.urlopen(request, timeout=HTTP_TIMEOUT) as response:
        charset = response.headers.get_content_charset() or "utf-8"
        return response.read().decode(charset, errors="replace")


def _rss_url(keyword: str) -> str:
    query = urllib.parse.quote_plus(f"site:youtube.com {keyword}")
    return (
        f"{GOOGLE_NEWS_SEARCH_URL}?q={query}"
        "&hl=en-US&gl=US&ceid=US:en"
    )


def _local_tag(element: ElementTree.Element) -> str:
    return element.tag.rsplit("}", 1)[-1]


def _published_date(value: str) -> str:
    value = (value or "").strip()
    if not value:
        return ""
    try:
        parsed = parsedate_to_datetime(value)
    except (TypeError, ValueError, OverflowError):
        return ""
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed.astimezone(timezone.utc).strftime("%Y-%m-%d")


def _published_age_seconds(value: str) -> int | None:
    """Approximate a relative publish time as seconds ago.

    The YouTube search page only exposes a relative label such as
    ``6y ago`` or ``5mo ago``.  Converting it to an age gives a stable
    newest-first ordering; ``None`` is returned for undated items so they
    can be pushed to the end of the list.
    """
    text = (value or "").strip()
    if not text:
        return None
    if re.search(r"\bjust\s+now\b", text, re.IGNORECASE):
        return 0
    match = PUBLISHED_AGO_RE.search(text)
    if not match:
        return None
    try:
        count = int(match.group("value").replace(",", ""))
    except ValueError:
        return None
    unit = match.group("unit").lower()
    factor = PUBLISHED_UNIT_SECONDS.get(unit)
    if factor is None and unit.endswith("s"):
        factor = PUBLISHED_UNIT_SECONDS.get(unit[:-1])
    return count * factor if factor is not None else None


def _published_sort_key(item: dict) -> tuple[bool, int]:
    """Sort newest first, with undated videos last."""
    age = _published_age_seconds(
        str(item.get("published_text") or item.get("published") or "")
    )
    return (age is None, 0 if age is None else age)


def _is_youtube_source(source_url: str) -> bool:
    host = urllib.parse.urlparse(source_url or "").netloc.lower()
    host = host.rsplit(":", 1)[0]
    if host.startswith("www."):
        host = host[4:]
    return host in {"youtube.com", "m.youtube.com", "music.youtube.com"}


def _strip_youtube_suffix(title: str) -> str:
    title = (title or "").strip()
    return re.sub(r"\s+-\s+YouTube\s*$", "", title, flags=re.IGNORECASE).strip()


def _youtube_video_id(url: str) -> str:
    try:
        parsed = urllib.parse.urlparse(url or "")
    except ValueError:
        return ""
    host = parsed.netloc.lower().split(":", 1)[0]
    if host.startswith("www."):
        host = host[4:]
    path = parsed.path or ""
    video_id = ""
    if host == "youtu.be":
        video_id = path.strip("/").split("/", 1)[0]
    elif host in {"youtube.com", "m.youtube.com", "music.youtube.com"}:
        if path == "/watch":
            video_id = (urllib.parse.parse_qs(parsed.query).get("v") or [""])[0]
        else:
            match = re.match(
                r"^/(?:shorts|embed|live|v)/([^/?#]+)", path, re.IGNORECASE
            )
            if match:
                video_id = match.group(1)
    return video_id if VIDEO_ID_RE.fullmatch(video_id or "") else ""


def _read_cache() -> dict[str, str]:
    try:
        raw = json.loads(CACHE_PATH.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}
    if not isinstance(raw, dict):
        return {}
    return {
        str(key): str(value)
        for key, value in raw.items()
        if isinstance(key, str) and isinstance(value, str)
    }


def _write_cache(cache: dict[str, str]) -> None:
    try:
        CACHE_PATH.parent.mkdir(parents=True, exist_ok=True)
        if len(cache) > CACHE_LIMIT:
            cache = dict(list(cache.items())[-CACHE_LIMIT:])
        temporary = CACHE_PATH.with_suffix(".tmp")
        temporary.write_text(
            json.dumps(cache, ensure_ascii=False), encoding="utf-8"
        )
        os.replace(temporary, CACHE_PATH)
    except OSError:
        pass


def _resolve_google_news_link(link: str) -> str:
    """Resolve one Google News redirect link to its publisher URL."""
    direct_id = _youtube_video_id(link)
    if direct_id:
        return link

    match = ARTICLE_ID_RE.search(urllib.parse.urlparse(link).path or "")
    if not match:
        return ""
    article_id = match.group(1)

    page = _fetch(f"https://news.google.com/rss/articles/{article_id}")
    timestamp_match = TIMESTAMP_RE.search(page)
    signature_match = SIGNATURE_RE.search(page)
    if not timestamp_match or not signature_match:
        return ""

    garturlreq = [
        "garturlreq",
        [
            ["X", "X", ["X", "X"], None, None, 1, 1, "US:en", None, 1,
             None, None, None, None, None, 0, 1],
            "X",
            "X",
            1,
            [1, 1, 1],
            1,
            1,
            None,
            0,
            0,
            None,
            0,
        ],
        article_id,
        int(timestamp_match.group(1)),
        signature_match.group(1),
    ]
    inner = json.dumps(garturlreq, separators=(",", ":"))
    outer = json.dumps([[["Fbv4je", inner]]], separators=(",", ":"))
    body = urllib.parse.urlencode({"f.req": outer}).encode("utf-8")
    response = _fetch(
        GOOGLE_NEWS_BATCH_URL,
        data=body,
        headers={
            "Content-Type": "application/x-www-form-urlencoded;charset=UTF-8",
            "Accept": "*/*",
        },
    )
    text = response
    if text.startswith(")]}'"):
        text = text[4:].lstrip()
    try:
        payload = json.loads(text)
    except ValueError:
        return ""
    for entry in payload if isinstance(payload, list) else []:
        if not isinstance(entry, list) or len(entry) < 3:
            continue
        if entry[0] != "wrb.fr" or entry[1] != "Fbv4je":
            continue
        try:
            inner_payload = json.loads(entry[2])
        except (TypeError, ValueError):
            continue
        if (
            isinstance(inner_payload, list)
            and len(inner_payload) > 1
            and inner_payload[0] == "garturlres"
            and isinstance(inner_payload[1], str)
        ):
            return inner_payload[1]
    return ""


def _resolve_many(links: list[str]) -> list[str]:
    """Resolve links concurrently, using the on-disk URL cache."""
    cache = _read_cache()
    resolved: dict[str, str] = {}
    pending = []
    for link in links:
        if not link:
            continue
        cached = cache.get(link)
        if cached:
            resolved[link] = cached
            continue
        pending.append(link)

    if pending:
        with ThreadPoolExecutor(
            max_workers=min(RESOLVE_WORKERS, len(pending))
        ) as pool:
            for link, url in zip(pending, pool.map(_resolve_google_news_link, pending)):
                resolved[link] = url
                if url:
                    cache[link] = url
        _write_cache(cache)

    return [resolved.get(link, "") for link in links]


def _rss_items(keyword: str) -> list[dict]:
    body = _fetch(_rss_url(keyword), headers={"Accept": "application/rss+xml,application/xml,text/xml,*/*"})
    root = ElementTree.fromstring(body)
    items = []
    for item in root.iter():
        if _local_tag(item) != "item":
            continue
        title = ""
        link = ""
        source_url = ""
        source_name = ""
        published = ""
        for child in list(item):
            tag = _local_tag(child)
            text = (child.text or "").strip()
            if tag == "title":
                title = text
            elif tag == "link":
                link = text
            elif tag == "pubDate":
                published = _published_date(text)
            elif tag == "source":
                source_url = child.attrib.get("url", "")
                source_name = text
        if not link or not _is_youtube_source(source_url):
            continue
        if source_name and source_name.lower() != "youtube":
            continue
        title = _strip_youtube_suffix(title)
        if not title:
            continue
        items.append(
            {
                "title": title,
                "link": link,
                "published": published,
            }
        )
    # Google News RSS is ordered by relevance; the UI expects newest first.
    # ISO dates sort lexically, and undated items are pushed to the end.
    items.sort(
        key=lambda item: (bool(item["published"]), item["published"]),
        reverse=True,
    )
    return items


def search_google_news_videos(
    keyword: str,
    limit: int,
    exclude: set[str] | None = None,
) -> list[dict]:
    """Return up to ``limit`` YouTube videos from Google News RSS."""
    limit = max(1, limit)
    exclude = set(exclude or ())
    items = _rss_items(keyword)

    results: list[dict] = []
    seen: set[str] = set(exclude)
    cursor = 0
    # Resolve in batches so a "More" request does not pay for resolving the
    # whole feed again.  Excluded ids still need resolving once to be skipped.
    batch_size = max(RESOLVE_WORKERS, min(len(items), limit + len(exclude) + 4))
    while cursor < len(items) and len(results) < limit:
        batch = items[cursor:cursor + batch_size]
        cursor += len(batch)
        urls = _resolve_many([item["link"] for item in batch])
        for item, url in zip(batch, urls):
            video_id = _youtube_video_id(url)
            if not video_id or video_id in seen:
                continue
            seen.add(video_id)
            results.append(
                {
                    "id": video_id,
                    "title": item["title"],
                    "url": f"https://www.youtube.com/watch?v={video_id}",
                    "thumbnail": (
                        f"https://img.youtube.com/vi/{video_id}/"
                        "maxresdefault.jpg"
                    ),
                    "no_thumbnail": False,
                    "duration": 0,
                    "channel": "",
                    "published": item["published"],
                    "published_text": item["published"],
                    "source": "google_news",
                }
            )
            if len(results) >= limit:
                break
    return results


def _text_value(value) -> str:
    """Flatten a YouTube renderer text node into a plain string."""
    if isinstance(value, str):
        return value.strip()
    if not isinstance(value, dict):
        return ""
    simple = value.get("simpleText")
    if isinstance(simple, str):
        return simple.strip()
    runs = value.get("runs")
    if isinstance(runs, list):
        parts = []
        for run in runs:
            if isinstance(run, dict) and isinstance(run.get("text"), str):
                parts.append(run["text"])
        return "".join(parts).strip()
    return ""


def _duration_seconds(text: str) -> int:
    """Convert a YouTube duration like ``6:57`` or ``1:02:03`` to seconds."""
    pieces = [piece for piece in (text or "").strip().split(":") if piece]
    if not pieces or not all(piece.isdigit() for piece in pieces):
        return 0
    seconds = 0
    for piece in pieces:
        seconds = seconds * 60 + int(piece)
    return seconds


def _video_renderers(node):
    """Yield every ``videoRenderer`` found in a ytInitialData tree."""
    if isinstance(node, dict):
        renderer = node.get("videoRenderer")
        if isinstance(renderer, dict):
            yield renderer
        for value in node.values():
            yield from _video_renderers(value)
    elif isinstance(node, list):
        for value in node:
            yield from _video_renderers(value)


def _renderer_thumbnail(renderer: dict, video_id: str) -> str:
    thumbnail = renderer.get("thumbnail")
    if isinstance(thumbnail, dict):
        thumbnails = thumbnail.get("thumbnails")
        if isinstance(thumbnails, list):
            for entry in reversed(thumbnails):
                if isinstance(entry, dict) and entry.get("url"):
                    return str(entry["url"])
    return f"https://i.ytimg.com/vi/{video_id}/hqdefault.jpg"


def _decode_json_at(text: str, start: int):
    """Decode a JSON object beginning at or after ``start``.

    Using ``raw_decode`` instead of a regular-expression capture keeps the
    parser working when the payload contains markup-like text (for example a
    ``</script>`` inside a title) or is not followed immediately by a
    semicolon and a closing script tag.
    """
    length = len(text)
    while start < length and text[start].isspace():
        start += 1
    if start >= length or text[start] != "{":
        return None
    try:
        value, _ = json.JSONDecoder().raw_decode(text, start)
    except ValueError:
        return None
    return value if isinstance(value, dict) else None


def extract_yt_initial_data(body: str):
    """Return the ``ytInitialData`` object embedded in a YouTube page.

    YouTube has used several assignment forms over time (``var
    ytInitialData``, ``window["ytInitialData"]``, ``window.ytInitialData``,
    and a bare ``ytInitialData``).  Try each occurrence and decode the first
    one that yields a real JSON object.
    """
    for match in YT_INITIAL_DATA_ASSIGNMENT_RE.finditer(body or ""):
        data = _decode_json_at(body, match.end())
        if data is not None:
            return data
    return None


def _search_page_headers() -> dict[str, str]:
    return {
        "Accept": (
            "text/html,application/xhtml+xml,application/xml;q=0.9,"
            "*/*;q=0.8"
        ),
        "Cookie": "CONSENT=YES+1",
    }


def _fetch_search_page_data(keyword: str):
    """Fetch ytInitialData from the search page, retrying alternate pages.

    A single request can occasionally receive a consent/interstitial page or
    a slightly different desktop payload.  The desktop URL is tried first;
    the mobile endpoint (which redirects to the desktop results with the same
    data) and a cache-bypassing desktop URL are retries, so a transient
    response does not make ``/youtube advancesearch`` fail outright.
    """
    query = urllib.parse.quote_plus(keyword)
    urls = [
        f"{YOUTUBE_SEARCH_PAGE_URL}?search_query={query}&hl=en&gl=US",
        f"{YOUTUBE_SEARCH_PAGE_URL}?search_query={query}&hl=en&gl=US"
        "&persist_hl=1",
        f"{YOUTUBE_MOBILE_SEARCH_PAGE_URL}?search_query={query}"
        "&hl=en&gl=US",
    ]
    errors: list[str] = []
    for url in urls:
        try:
            body = _fetch(url, headers=_search_page_headers())
        except Exception as exc:  # network/HTTP/consent redirect failures
            errors.append(str(exc))
            continue
        data = extract_yt_initial_data(body)
        if data is not None:
            return data
        errors.append("could not read ytInitialData from the search page")
    detail = next((item for item in errors if item), "")
    raise RuntimeError(detail or "could not read ytInitialData")


def search_youtube_results_page(
    keyword: str,
    limit: int,
    exclude: set[str] | None = None,
) -> list[dict]:
    """Extract video metadata from the YouTube search results page.

    This is the ``/youtube advancesearch`` backend.  It loads
    ``https://www.youtube.com/results?search_query=<keyword>`` and reads the
    embedded ``ytInitialData`` JSON, which yields richer metadata than the
    Google News RSS source (duration, channel, and relative publish time).
    """
    limit = max(1, int(limit))
    data = _fetch_search_page_data(keyword)

    results: list[dict] = []
    seen: set[str] = set(exclude or ())
    for renderer in _video_renderers(data):
        video_id = str(renderer.get("videoId") or "")
        if not VIDEO_ID_RE.fullmatch(video_id) or video_id in seen:
            continue
        seen.add(video_id)
        title = _text_value(renderer.get("title")) or video_id
        channel = _text_value(
            renderer.get("longBylineText") or renderer.get("ownerText")
        )
        duration_text = _text_value(renderer.get("lengthText"))
        published_text = _text_value(renderer.get("publishedTimeText"))
        results.append(
            {
                "id": video_id,
                "title": title,
                "url": f"https://www.youtube.com/watch?v={video_id}",
                "thumbnail": _renderer_thumbnail(renderer, video_id),
                "no_thumbnail": False,
                "duration": _duration_seconds(duration_text),
                "channel": channel,
                "published": published_text,
                "published_text": published_text,
                "source": "youtube_search",
            }
        )
    # The page can return results in relevance order even when the upload-date
    # filter is ignored, so sort every extracted video by publish date
    # descending (undated videos last) before applying the limit.
    results.sort(key=_published_sort_key)
    return results[:limit]


def main(argv=None) -> None:
    parser = argparse.ArgumentParser(
        description="Search YouTube through Google News RSS."
    )
    parser.add_argument("keyword", help="search keyword")
    parser.add_argument(
        "-n", "--limit", type=int, default=6,
        help="number of results to return (default: 6)",
    )
    parser.add_argument(
        "--json", action="store_true",
        help="print one JSON object per video",
    )
    parser.add_argument(
        "--progressive-json", action="store_true",
        help="print one final {'type': 'videos'} JSON event",
    )
    parser.add_argument(
        "--exclude", default="", metavar="VIDEO_IDS",
        help="comma-separated video ids to omit (used by More)",
    )
    parser.add_argument(
        "--search-page", action="store_true",
        help=(
            "extract results from https://www.youtube.com/results "
            "instead of Google News RSS (used by advancesearch)"
        ),
    )
    args = parser.parse_args(argv)

    if args.limit < 1:
        parser.error("--limit must be at least 1")
    exclude = {
        video_id.strip()
        for video_id in args.exclude.split(",")
        if video_id.strip()
    }

    try:
        if args.search_page:
            videos = search_youtube_results_page(
                args.keyword, args.limit, exclude
            )
        else:
            videos = search_google_news_videos(
                args.keyword, args.limit, exclude
            )
    except Exception as exc:
        sys.exit(f"YouTube search failed: {exc}")

    if args.progressive_json:
        print(
            json.dumps(
                {"type": "videos", "items": videos, "pending": False},
                ensure_ascii=False,
            ),
            flush=True,
        )
    elif args.json:
        for video in videos:
            print(json.dumps(video, ensure_ascii=False))
    else:
        for video in videos:
            print(video["id"])


if __name__ == "__main__":
    main()
