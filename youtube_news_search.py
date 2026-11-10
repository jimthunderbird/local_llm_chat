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
only real YouTube video URLs are returned.  The result intentionally contains
only the video id, title, and watch URL (plus a publish date when the feed
provides one); no thumbnail or duration metadata is needed.

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
TIMESTAMP_RE = re.compile(r'data-n-a-ts="(\d+)"')
SIGNATURE_RE = re.compile(r'data-n-a-sg="([^"]+)"')
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
                    "thumbnail": "",
                    "no_thumbnail": True,
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
    args = parser.parse_args(argv)

    if args.limit < 1:
        parser.error("--limit must be at least 1")
    exclude = {
        video_id.strip()
        for video_id in args.exclude.split(",")
        if video_id.strip()
    }

    try:
        videos = search_google_news_videos(args.keyword, args.limit, exclude)
    except Exception as exc:
        sys.exit(f"Google News YouTube search failed: {exc}")

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
