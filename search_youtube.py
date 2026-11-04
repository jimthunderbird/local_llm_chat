#!/usr/bin/env python3
"""Search YouTube and print the matching video hashes.

Usage:
    python3 search_youtube.py <keyword> [--limit N]
    python3 search_youtube.py <keyword> --json [--limit N] \\
        [--min-duration SECONDS] [--max-duration SECONDS] \\
        [--any-date] [--no-transcript-check] [--sort-recent]

Outputs one video hash per line, e.g.:
    FTwxtyOEGyM
    T594Cg4sTq4
    ...

With ``--json`` it prints one JSON object per line instead, including the
title, duration, channel, thumbnail, watch URL, and (with ``--sort-recent``)
the upload date.  That mode powers ``/youtube search``.

``--sort-recent`` returns the newest matching videos first.  Candidates are
enriched with upload dates, then checked (in parallel batches) for an English
transcript until ``--limit`` videos are found.  This keeps the
transcript-verification cost bounded while guaranteeing that every returned
video has English captions.

By default only videos published within the last year, at most 20 minutes
long, and with an English transcript are returned.

Searching uses yt-dlp instead of ``youtube-search-python``: its
``VideosSearch`` API currently breaks on modern YouTube responses, while
yt-dlp is actively maintained and also gives us access to YouTube's own
upload-date search filter.

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
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime, timezone

try:
    import yt_dlp
    from youtube_transcript_api import YouTubeTranscriptApi
except ImportError as exc:  # pragma: no cover - dependency guard
    sys.exit(
        f"Missing dependency: {exc.name}. "
        "Install with: pip install yt-dlp youtube-transcript-api"
    )

DEFAULT_LIMIT = 10

# Only return videos that are at most 20 minutes long (in seconds).
MAX_DURATION_SECONDS = 20 * 60

# Since long videos are filtered out after searching, ask YouTube for a
# larger candidate pool so we can still fill ``limit`` short videos.
CANDIDATE_MULTIPLIER = 10
MIN_CANDIDATES = 100

# Flat search results do not include upload dates.  When the caller asks for
# the most recent videos, enrich the best duration-eligible candidates with a
# lightweight metadata lookup and then sort by upload time.  The cap keeps the
# extra requests bounded while leaving plenty of candidates to choose from.
DATE_ENRICH_LIMIT = 40
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


def has_english_transcript(video_id, api=None):
    """Return True when YouTube lists an English transcript for `video_id`.

    Both manually uploaded and auto-generated English tracks (whose language
    codes may be ``en``, ``en-US``, ``en-GB``, etc.) count.  Any failure to
    inspect the track list (disabled transcripts, unavailable video, network
    error, ...) is treated as "no English transcript" so the caller only ever
    returns videos we could verify.
    """
    api = api or YouTubeTranscriptApi()
    try:
        transcripts = api.list(video_id)
        return any(
            transcript.language_code.lower().startswith("en")
            for transcript in transcripts
        )
    except Exception:
        return False


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
    """Attach upload dates to the first `limit` videos, in parallel."""
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
        video["timestamp"] = timestamp
        video["published"] = published


def _with_english_transcripts(videos, limit):
    """Return the first `limit` videos that have an English transcript.

    Transcript lookups are network-bound and can fail slowly, so they are
    checked in parallel batches.  Processing newest-first means we normally
    only inspect a handful of candidates before the result list is full.
    """
    if not videos or limit <= 0:
        return []

    kept = []
    batch_size = max(limit * 2, 12)
    for start in range(0, len(videos), batch_size):
        batch = videos[start : start + batch_size]
        workers = min(DATE_ENRICH_WORKERS, len(batch))
        with ThreadPoolExecutor(max_workers=workers) as pool:
            results = list(
                pool.map(
                    lambda video: has_english_transcript(video["id"]),
                    batch,
                )
            )
        for video, has_transcript in zip(batch, results):
            if has_transcript:
                kept.append(video)
                if len(kept) >= limit:
                    return kept
    return kept


def search_videos(
    keyword,
    limit=DEFAULT_LIMIT,
    *,
    min_duration=0,
    max_duration=MAX_DURATION_SECONDS,
    require_english_transcript=True,
    within_year=True,
    sort_recent=False,
):
    """Return up to `limit` video metadata dicts matching `keyword`.

    Durations are given in seconds.  By default results are restricted to
    videos published within the last year, at most 20 minutes long, and with
    an English transcript available.  ``/youtube search`` relaxes the date
    restriction and uses a 5-40 minute duration window; it still requires
    English captions for every returned video.

    With ``sort_recent`` the results are enriched with upload dates and
    ordered newest first.  When an English transcript is required, the newest
    candidates are transcript-checked before the final list is returned.
    """
    candidate_limit = max(limit * CANDIDATE_MULTIPLIER, MIN_CANDIDATES)
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
    transcript_api = YouTubeTranscriptApi() if require_english_transcript else None
    for entry in (info or {}).get("entries") or []:
        video_id = entry.get("id")
        duration = entry.get("duration")
        if (
            not video_id
            or video_id in seen
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
        # Recent-mode transcript checks happen after the candidates are
        # ordered newest-first, so only the results we actually need are
        # verified.  The normal relevance-ordered path checks as it goes and
        # can stop as soon as it has enough videos.
        if (
            require_english_transcript
            and not sort_recent
            and not has_english_transcript(video_id, transcript_api)
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
                "url": f"https://www.youtube.com/watch?v={video_id}",
            }
        )
        if len(videos) >= limit and not sort_recent:
            break

    if sort_recent:
        if videos:
            _enrich_upload_dates(videos)
            # Newest first; keep original relevance order as the tie-breaker
            # and leave videos with unknown dates at the end.
            indexed = list(enumerate(videos))
            indexed.sort(
                key=lambda pair: (pair[1].get("timestamp") or 0, -pair[0]),
                reverse=True,
            )
            videos = [video for _, video in indexed]
        if require_english_transcript:
            videos = _with_english_transcripts(videos, limit)
        return videos[:limit]

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
            "include upload dates and return the most recently published "
            "videos first"
        ),
    )
    args = parser.parse_args(argv)

    if args.limit < 1:
        parser.error("--limit must be at least 1")
    if args.min_duration < 0:
        parser.error("--min-duration cannot be negative")
    if args.max_duration < args.min_duration:
        parser.error("--max-duration must be greater than or equal to --min-duration")

    try:
        videos = search_videos(
            args.keyword,
            args.limit,
            min_duration=args.min_duration,
            max_duration=args.max_duration,
            require_english_transcript=not args.no_transcript_check,
            within_year=not args.any_date,
            sort_recent=args.sort_recent,
        )
    except Exception as exc:
        sys.exit(f"YouTube search failed: {exc}")

    if args.json:
        for video in videos:
            print(json.dumps(video, ensure_ascii=False))
    else:
        for video in videos:
            print(video["id"])


if __name__ == "__main__":
    main()
