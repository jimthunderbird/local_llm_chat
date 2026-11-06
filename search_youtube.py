#!/usr/bin/env python3
"""Search YouTube and print the matching video hashes.

Usage:
    python3 search_youtube.py <keyword> [--limit N]
    python3 search_youtube.py <keyword> --json [--limit N] \\
        [--min-duration SECONDS] [--max-duration SECONDS] \\
        [--any-date] [--no-transcript-check] [--sort-recent] \\
        [--progressive-json] [--exclude VIDEO_IDS]

Outputs one video hash per line, e.g.:
    FTwxtyOEGyM
    T594Cg4sTq4
    ...

With ``--json`` it prints one JSON object per line instead, including the
title, duration, channel, thumbnail, watch URL, and (with ``--sort-recent``)
the relative publication age.  That mode powers ``/youtube search``.

``--sort-recent`` returns the newest matching videos first.  The fast path
uses YouTube's InnerTube search response, which already includes relative
publish times (``"3 weeks ago"``), so it no longer fetches every watch page
just to rank the results.  English-caption checks are limited to a shortlist
and run in parallel with a deadline.

By default only videos published within the last year, at most 20 minutes
long, and with an English transcript are returned.

Searching prefers YouTube's InnerTube endpoint because it returns title,
duration, channel, thumbnail, and relative publish time in one request.  A
yt-dlp flat-search fallback is kept for when InnerTube is unavailable or the
response shape changes.

YouTube does not expose a "rolling 12 months" search parameter directly, so
we use its built-in "This year" upload-date filter combined with the "Videos
only" result-type filter.  YouTube applies that date filter to a rolling one-
year window, which is exactly the requirement here.
"""

import argparse
import json
import re
import sys
import urllib.parse
from concurrent.futures import (
    ThreadPoolExecutor,
    TimeoutError as FuturesTimeoutError,
    as_completed,
)
from datetime import datetime, timezone

try:
    import yt_dlp
    from youtube_transcript_api import YouTubeTranscriptApi
except ImportError as exc:  # pragma: no cover - dependency guard
    sys.exit(
        f"Missing dependency: {exc.name}. "
        "Install with: pip install yt-dlp youtube-transcript-api"
    )

try:
    import requests
except ImportError:  # pragma: no cover - optional fast path
    requests = None

DEFAULT_LIMIT = 10

# Only return videos that are at most 20 minutes long (in seconds).
MAX_DURATION_SECONDS = 20 * 60

# Ask YouTube for a small candidate pool (about two search pages for a normal
# request) instead of the old 100-item pool.  InnerTube returns ~20 videos per
# page, so the shortlist is filled without paying for several continuations.
CANDIDATE_MULTIPLIER = 3
MIN_CANDIDATES = 24
MAX_CANDIDATES = 120

# English-caption lookups are network-bound.  Check only the candidates that
# could plausibly fill the page, in parallel, with a short deadline.
CAPTION_SHORTLIST_MULTIPLIER = 2
MIN_CAPTION_SHORTLIST = 12
CAPTION_CHECK_DEADLINE = 2.5
CAPTION_CHECK_WORKERS = 16

# Fallback only: flat yt-dlp results have no publish time, so recent mode
# still needs a bounded watch-page lookup if InnerTube fails.
DATE_ENRICH_LIMIT = 16
DATE_ENRICH_WORKERS = 8

# YouTube search ``sp`` parameter.
#
# The value is a base64-encoded protobuf:
#   field 2 (filters) = {
#       field 1 (upload date) = 5   -> "This year" (rolling 1-year window)
#       field 2 (result type) = 1   -> Videos only
#   }
# i.e. protobuf bytes 12 04 08 05 10 01 -> "EgQIBRAB".
SEARCH_PARAMS = "EgQIBRAB"

# "Videos only", with no upload-date filter.
VIDEOS_ONLY_PARAMS = "EgIQAQ=="

INNERTUBE_SEARCH_URL = (
    "https://www.youtube.com/youtubei/v1/search?prettyPrint=false"
)
INNERTUBE_CONTEXT = {
    "client": {
        "clientName": "WEB",
        "clientVersion": "2.20240401.00.00",
        "hl": "en",
        "gl": "US",
    }
}
INNERTUBE_TIMEOUT = 6
INNERTUBE_HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
        "AppleWebKit/537.36 (KHTML, like Gecko) "
        "Chrome/125.0.0.0 Safari/537.36"
    ),
    "Accept-Language": "en-US,en;q=0.9",
    "Content-Type": "application/json",
    "X-YouTube-Client-Name": "1",
    "X-YouTube-Client-Version": "2.20240401.00.00",
}

VIDEO_ID_RE = re.compile(r"^[A-Za-z0-9_-]{11}$")


def _search_url(keyword, within_year=True):
    params = {"search_query": keyword}
    if within_year:
        params["sp"] = SEARCH_PARAMS
    query = urllib.parse.urlencode(params)
    return f"https://www.youtube.com/results?{query}"


def _thumbnail_url(entry, video_id):
    """Pick the largest thumbnail yt-dlp reported, with a stable fallback."""
    best_url = ""
    best_width = -1
    for thumbnail in entry.get("thumbnails") or []:
        url = str(thumbnail.get("url") or "").strip()
        if not url:
            continue
        width = thumbnail.get("width")
        width = width if isinstance(width, (int, float)) else 0
        if width > best_width:
            best_url = url
            best_width = width
    return best_url or f"https://i.ytimg.com/vi/{video_id}/mqdefault.jpg"


def english_transcript_status(video_id, api=None):
    """Return True / False / None for a video's English transcript.

    ``True`` means an English track is listed, ``False`` means YouTube
    explicitly reports that no suitable track exists, and ``None`` means the
    lookup could not be completed (network error, blocked IP, timeout, ...).
    Keeping the third state lets callers avoid treating a transient failure as
    "this video has no captions".
    """
    api = api or YouTubeTranscriptApi()
    try:
        transcripts = api.list(video_id)
        return any(
            transcript.language_code.lower().startswith("en")
            for transcript in transcripts
        )
    except Exception as exc:
        name = type(exc).__name__.lower()
        if any(
            token in name
            for token in (
                "transcriptsdisabled",
                "notranscriptfound",
                "videounavailable",
                "no_transcript",
                "no_transcripts",
            )
        ):
            return False
        return None


def has_english_transcript(video_id, api=None):
    """Return True only when an English transcript was positively verified."""
    return english_transcript_status(video_id, api) is True


def _upload_date_from_info(info):
    """Return ``(upload_date, timestamp, published)`` from yt-dlp metadata."""
    timestamp = info.get("timestamp")
    if not isinstance(timestamp, (int, float)):
        timestamp = info.get("release_timestamp")
    if not isinstance(timestamp, (int, float)):
        timestamp = None

    upload_date = str(info.get("upload_date") or "").strip()
    if not re.fullmatch(r"\d{8}", upload_date):
        upload_date = ""

    published = ""
    if upload_date:
        published = f"{upload_date[:4]}-{upload_date[4:6]}-{upload_date[6:]}"
    elif timestamp is not None:
        published = datetime.fromtimestamp(
            timestamp, tz=timezone.utc
        ).strftime("%Y-%m-%d")
        upload_date = published.replace("-", "")

    return upload_date, int(timestamp) if timestamp is not None else None, published


def _fetch_upload_date(video_id):
    """Look up one video's upload date without downloading it."""
    options = {
        "quiet": True,
        "no_warnings": True,
        "skip_download": True,
        "noplaylist": True,
    }
    try:
        with yt_dlp.YoutubeDL(options) as ydl:
            info = ydl.extract_info(
                f"https://www.youtube.com/watch?v={video_id}", download=False
            )
        return video_id, _upload_date_from_info(info or {})
    except Exception:
        # A missing date should not discard an otherwise valid search result.
        return video_id, ("", None, "")


def _enrich_upload_dates(videos, limit=DATE_ENRICH_LIMIT):
    """Fallback: attach exact upload dates to a small batch of candidates."""
    targets = videos[:limit]
    if not targets:
        return

    dates = {}
    workers = min(DATE_ENRICH_WORKERS, len(targets))
    with ThreadPoolExecutor(max_workers=workers) as pool:
        futures = [pool.submit(_fetch_upload_date, video["id"]) for video in targets]
        for future in as_completed(futures):
            video_id, value = future.result()
            dates[video_id] = value

    for video in targets:
        upload_date, timestamp, published = dates.get(
            video["id"], ("", None, "")
        )
        video["upload_date"] = upload_date
        video["timestamp"] = timestamp or video.get("timestamp")
        video["published"] = published


# ---------------------------------------------------------------------------
# Fast path: YouTube's InnerTube search response
# ---------------------------------------------------------------------------

_UNIT_SECONDS = {
    "second": 1,
    "minute": 60,
    "hour": 3600,
    "day": 86400,
    "week": 7 * 86400,
    "month": 30 * 86400,
    "year": 365 * 86400,
}
_RELATIVE_TIME_RE = re.compile(
    r"(?:streamed|premiered|uploaded)?\s*"
    r"(\d+)\s+(second|minute|hour|day|week|month|year)s?\s+ago",
    re.IGNORECASE,
)


def _walk(node, key):
    """Yield every value stored under `key` anywhere in a JSON tree."""
    if isinstance(node, dict):
        for child_key, value in node.items():
            if child_key == key:
                yield value
            yield from _walk(value, key)
    elif isinstance(node, list):
        for item in node:
            yield from _walk(item, key)


def _runs_text(node):
    """Flatten YouTube's ``{simpleText}`` / ``{runs: [...]}`` text objects."""
    if not isinstance(node, dict):
        return ""
    if "simpleText" in node:
        return str(node.get("simpleText") or "")
    return "".join(
        str(run.get("text") or "")
        for run in node.get("runs") or []
        if isinstance(run, dict)
    )


def _parse_duration_text(text):
    """Convert ``13:59`` / ``1:05:53`` to seconds."""
    text = str(text or "").strip()
    if not text:
        return None
    try:
        parts = [int(part) for part in text.split(":")]
    except ValueError:
        return None
    if not parts:
        return None
    seconds = 0
    for part in parts:
        seconds = seconds * 60 + part
    return seconds


def _relative_age_seconds(text):
    """Convert ``3 weeks ago`` / ``yesterday`` to an approximate age."""
    text = str(text or "").strip().lower()
    if not text:
        return None
    if text == "today":
        return 0
    if text == "yesterday":
        return 86400
    match = _RELATIVE_TIME_RE.search(text)
    if not match:
        return None
    return int(match.group(1)) * _UNIT_SECONDS[match.group(2)]


def _relative_timestamp(text, now=None):
    """Approximate Unix timestamp for a relative publish label."""
    age = _relative_age_seconds(text)
    if age is None:
        return None
    now = now or datetime.now(timezone.utc)
    return int(now.timestamp()) - age


def _innertube_request(session, body):
    response = session.post(
        INNERTUBE_SEARCH_URL,
        json=body,
        timeout=INNERTUBE_TIMEOUT,
    )
    response.raise_for_status()
    return response.json()


def _innertube_page(session, keyword, params):
    """Return ``(videos, continuation_token)`` for the first search page."""
    data = _innertube_request(
        session,
        {
            "context": INNERTUBE_CONTEXT,
            "query": keyword,
            "params": params,
        },
    )
    return _parse_innertube_videos(data), _next_continuation(data)


def _innertube_continuation(session, token):
    """Return ``(videos, next_continuation_token)`` for one more page."""
    data = _innertube_request(
        session,
        {
            "context": INNERTUBE_CONTEXT,
            "continuation": token,
        },
    )
    return _parse_innertube_videos(data), _next_continuation(data)


def _next_continuation(data):
    for command in _walk(data, "continuationCommand"):
        token = command.get("token") if isinstance(command, dict) else None
        if token:
            return str(token)
    return ""


def _parse_innertube_videos(data):
    """Extract normalized video dicts from an InnerTube search response."""
    now = datetime.now(timezone.utc)
    videos = []
    for renderer in _walk(data, "videoRenderer"):
        if not isinstance(renderer, dict):
            continue
        video_id = str(renderer.get("videoId") or "").strip()
        if not VIDEO_ID_RE.match(video_id):
            continue
        duration = _parse_duration_text(_runs_text(renderer.get("lengthText")))
        if duration is None:
            # Live streams and upcoming premieres have no fixed length.
            continue
        thumbnails = renderer.get("thumbnail", {}).get("thumbnails") or []
        thumbnail = ""
        if thumbnails:
            thumbnail = str(
                (thumbnails[-1] or {}).get("url") or ""
            ).strip()
        published_text = re.sub(
            r"^(?:streamed|premiered|uploaded)\s+",
            "",
            _runs_text(renderer.get("publishedTimeText")).strip(),
            flags=re.IGNORECASE,
        )
        channel = (
            _runs_text(renderer.get("ownerText"))
            or _runs_text(renderer.get("longBylineText"))
        ).strip()
        videos.append(
            {
                "id": video_id,
                "title": _runs_text(renderer.get("title")).strip(),
                "duration": duration,
                "channel": channel,
                "thumbnail": thumbnail
                or f"https://i.ytimg.com/vi/{video_id}/mqdefault.jpg",
                "published_text": published_text,
                "timestamp": _relative_timestamp(published_text, now),
                "url": f"https://www.youtube.com/watch?v={video_id}",
            }
        )
    return videos


def _duration_ok(video, min_duration, max_duration):
    duration = video.get("duration")
    return (
        isinstance(duration, (int, float))
        and min_duration <= duration <= max_duration
    )


def _search_candidates_innertube(
    keyword,
    *,
    candidate_limit,
    min_duration,
    max_duration,
    enough,
    within_year,
    exclude=frozenset(),
):
    """Fetch a small, filtered candidate pool via InnerTube.

    Returns ``None`` when the fast path is unavailable so the caller can fall
    back to yt-dlp.  Fetching stops as soon as enough duration-eligible
    candidates are available, which normally means one search page.
    """
    if requests is None:
        return None

    params = SEARCH_PARAMS if within_year else VIDEOS_ONLY_PARAMS
    session = requests.Session()
    session.headers.update(INNERTUBE_HEADERS)

    videos = []
    seen = set()
    token = ""
    fetched = 0
    try:
        while True:
            if token:
                page, token = _innertube_continuation(session, token)
            else:
                page, token = _innertube_page(session, keyword, params)
            if not page:
                break
            for video in page:
                video_id = video["id"]
                if video_id in seen or video_id in exclude:
                    continue
                seen.add(video_id)
                if _duration_ok(video, min_duration, max_duration):
                    videos.append(video)
            fetched += len(page)
            if len(videos) >= enough or fetched >= candidate_limit or not token:
                break
        if not videos and not fetched:
            return None
        return videos[:candidate_limit]
    except Exception:
        return None
    finally:
        session.close()


def _search_candidates_ytdlp(
    keyword,
    *,
    candidate_limit,
    min_duration,
    max_duration,
    within_year,
    exclude=frozenset(),
):
    """Fallback candidate search using yt-dlp's flat playlist extraction."""
    options = {
        "quiet": True,
        "no_warnings": True,
        "skip_download": True,
        "extract_flat": True,
        "playlistend": candidate_limit,
    }
    with yt_dlp.YoutubeDL(options) as ydl:
        info = ydl.extract_info(
            _search_url(keyword, within_year=within_year), download=False
        )

    videos = []
    seen = set()
    for entry in (info or {}).get("entries") or []:
        video_id = entry.get("id")
        duration = entry.get("duration")
        if (
            not video_id
            or video_id in seen
            or video_id in exclude
            or not VIDEO_ID_RE.match(video_id)
            # Skip anything that is not a plain video result (channels,
            # playlists, etc.).
            or entry.get("ie_key") != "Youtube"
            # Require a known duration; never return a video we cannot verify
            # is inside the requested window.
            or duration is None
            or duration < min_duration
            or duration > max_duration
        ):
            continue
        seen.add(video_id)
        videos.append(
            {
                "id": video_id,
                "title": str(entry.get("title") or "").strip(),
                "duration": int(duration),
                "channel": str(
                    entry.get("channel") or entry.get("uploader") or ""
                ).strip(),
                "thumbnail": _thumbnail_url(entry, video_id),
                "published_text": "",
                "timestamp": None,
                "url": f"https://www.youtube.com/watch?v={video_id}",
            }
        )
        if len(videos) >= candidate_limit:
            break
    return videos


def _verify_captions(videos, api, deadline_s=CAPTION_CHECK_DEADLINE):
    """Return ``{video_id: True/False/None}`` within a wall-clock deadline."""
    results = {video["id"]: None for video in videos}
    if not videos:
        return results

    workers = min(CAPTION_CHECK_WORKERS, len(videos))
    pool = ThreadPoolExecutor(max_workers=workers)
    futures = {
        pool.submit(english_transcript_status, video["id"], api): video["id"]
        for video in videos
    }
    try:
        for future in as_completed(futures, timeout=deadline_s):
            video_id = futures[future]
            try:
                results[video_id] = future.result()
            except Exception:
                results[video_id] = None
    except FuturesTimeoutError:
        pass
    finally:
        pool.shutdown(wait=False, cancel_futures=True)
    return results


def _with_english_transcripts(videos, limit, api, deadline_s=CAPTION_CHECK_DEADLINE):
    """Return videos positively verified to have English captions.

    Only a shortlist is checked.  If it cannot fill the page, one more batch
    is checked with the same deadline; the old unbounded pool scan is gone.
    """
    if not videos or limit <= 0:
        return []

    batch_size = min(
        len(videos),
        max(limit * CAPTION_SHORTLIST_MULTIPLIER, MIN_CAPTION_SHORTLIST),
    )
    kept = []
    start = 0
    while start < len(videos):
        batch = videos[start : start + batch_size]
        statuses = _verify_captions(batch, api, deadline_s=deadline_s)
        for video in batch:
            if statuses.get(video["id"]) is True:
                kept.append(video)
                if len(kept) >= limit:
                    return kept
        start += batch_size
        # Bound the scan: normally one batch, occasionally two when filters
        # removed many candidates.
        if start >= batch_size * 2:
            break
    return kept


def _sort_newest_first(videos):
    """Sort newest first, leaving unknown publish times in relevance order."""
    indexed = list(enumerate(videos))
    indexed.sort(
        key=lambda pair: (
            pair[1].get("timestamp") is not None,
            pair[1].get("timestamp") or 0,
            -pair[0],
        ),
        reverse=True,
    )
    return [video for _, video in indexed]


def search_videos(
    keyword,
    limit=DEFAULT_LIMIT,
    *,
    min_duration=0,
    max_duration=MAX_DURATION_SECONDS,
    require_english_transcript=True,
    within_year=True,
    sort_recent=False,
    on_candidates=None,
    exclude=None,
):
    """Return up to `limit` video metadata dicts matching `keyword`.

    Durations are given in seconds.  By default results are restricted to
    videos published within the last year, at most 20 minutes long, and with
    an English transcript available.  ``/youtube search`` relaxes the date
    restriction and uses a 5-60 minute duration window; it still requires
    English captions for every returned video.

    With ``sort_recent`` the results are ordered newest first using the
    relative publish time already present in the search response.  When an
    English transcript is required, only a shortlist is checked, in parallel,
    with a deadline.

    ``on_candidates`` is an optional callback used by progressive callers.  It
    receives the ordered, duration-filtered candidates before caption
    verification so a UI can paint cards immediately.

    ``exclude`` is a set of already-known video ids.  They are filtered out
    while the candidate pool is collected, so caption checks are spent only
    on genuinely new videos (this is what makes the UI "More" button cheap).
    """
    exclude = frozenset(exclude or ())
    candidate_limit = min(
        max(limit * CANDIDATE_MULTIPLIER, MIN_CANDIDATES)
        + len(exclude),
        MAX_CANDIDATES,
    )
    enough = max(
        limit * CAPTION_SHORTLIST_MULTIPLIER,
        MIN_CAPTION_SHORTLIST,
    )

    videos = _search_candidates_innertube(
        keyword,
        candidate_limit=candidate_limit,
        min_duration=min_duration,
        max_duration=max_duration,
        enough=enough,
        within_year=within_year,
        exclude=exclude,
    )
    if videos is None:
        videos = _search_candidates_ytdlp(
            keyword,
            candidate_limit=candidate_limit,
            min_duration=min_duration,
            max_duration=max_duration,
            within_year=within_year,
            exclude=exclude,
        )
        if sort_recent and videos:
            _enrich_upload_dates(videos)

    if sort_recent:
        videos = _sort_newest_first(videos)

    if on_candidates is not None:
        on_candidates(list(videos[:limit]))

    if require_english_transcript:
        api = YouTubeTranscriptApi()
        videos = _with_english_transcripts(videos, limit, api)

    return videos[:limit]


def search(keyword, limit=DEFAULT_LIMIT):
    """Return up to `limit` YouTube video hashes matching `keyword`.

    Results are restricted to videos published within the last year, at most
    20 minutes long, and with an English transcript available.
    """
    return [
        video["id"]
        for video in search_videos(
            keyword,
            limit,
            min_duration=0,
            max_duration=MAX_DURATION_SECONDS,
            require_english_transcript=True,
            within_year=True,
        )
    ]


def main(argv=None):
    parser = argparse.ArgumentParser(
        description=(
            "Search YouTube and print the matching video hashes. "
            "By default only videos published within the last year, at most "
            "20 minutes long, and with an English transcript are returned."
        )
    )
    parser.add_argument("keyword", help="search keyword")
    parser.add_argument(
        "-n",
        "--limit",
        type=int,
        default=DEFAULT_LIMIT,
        help=f"number of results to return (default: {DEFAULT_LIMIT})",
    )
    parser.add_argument(
        "--json",
        action="store_true",
        help="print one JSON object per video instead of only the video hash",
    )
    parser.add_argument(
        "--min-duration",
        type=int,
        default=0,
        metavar="SECONDS",
        help="minimum video duration in seconds (default: 0)",
    )
    parser.add_argument(
        "--max-duration",
        type=int,
        default=MAX_DURATION_SECONDS,
        metavar="SECONDS",
        help=(
            "maximum video duration in seconds "
            f"(default: {MAX_DURATION_SECONDS})"
        ),
    )
    parser.add_argument(
        "--any-date",
        action="store_true",
        help="do not restrict results to the last year",
    )
    parser.add_argument(
        "--no-transcript-check",
        action="store_true",
        help="skip the English-transcript availability check",
    )
    parser.add_argument(
        "--sort-recent",
        action="store_true",
        help=(
            "include publish times and return the most recently published "
            "videos first"
        ),
    )
    parser.add_argument(
        "--progressive-json",
        action="store_true",
        help=(
            "emit a candidate event before caption verification, then a final "
            "event (used by the /youtube search UI)"
        ),
    )
    parser.add_argument(
        "--exclude",
        default="",
        metavar="VIDEO_IDS",
        help=(
            "comma-separated video ids to omit from candidates and results "
            "(used by the /youtube More button)"
        ),
    )
    args = parser.parse_args(argv)

    if args.limit < 1:
        parser.error("--limit must be at least 1")
    if args.min_duration < 0:
        parser.error("--min-duration cannot be negative")
    if args.max_duration < args.min_duration:
        parser.error("--max-duration must be greater than or equal to --min-duration")

    exclude = {
        video_id.strip()
        for video_id in args.exclude.split(",")
        if video_id.strip()
    }

    def emit_candidates(candidates):
        if not candidates or not args.progressive_json:
            return
        print(
            json.dumps(
                {
                    "type": "candidates",
                    "items": candidates,
                    "pending": True,
                },
                ensure_ascii=False,
            ),
            flush=True,
        )

    try:
        videos = search_videos(
            args.keyword,
            args.limit,
            min_duration=args.min_duration,
            max_duration=args.max_duration,
            require_english_transcript=not args.no_transcript_check,
            within_year=not args.any_date,
            sort_recent=args.sort_recent,
            on_candidates=emit_candidates,
            exclude=exclude,
        )
    except Exception as exc:
        sys.exit(f"YouTube search failed: {exc}")

    if args.progressive_json:
        print(
            json.dumps(
                {
                    "type": "videos",
                    "items": videos,
                    "pending": False,
                },
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
