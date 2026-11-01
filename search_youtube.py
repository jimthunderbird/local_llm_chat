#!/usr/bin/env python3
"""Search YouTube and print the matching video hashes.

Usage:
    python3 search_youtube.py <keyword> [--limit N]

Outputs one video hash per line, e.g.:
    FTwxtyOEGyM
    T594Cg4sTq4
    ...

Only videos published within the last year, at most 20 minutes long, and
with an English transcript are returned.

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
import re
import sys
import urllib.parse

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


def _search_url(keyword):
    query = urllib.parse.urlencode(
        {"search_query": keyword, "sp": SEARCH_PARAMS}
    )
    return f"https://www.youtube.com/results?{query}"


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


def search(keyword, limit=DEFAULT_LIMIT):
    """Return up to `limit` YouTube video hashes matching `keyword`.

    Results are restricted to videos published within the last year, at most
    20 minutes long, and with an English transcript available.
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
        info = ydl.extract_info(_search_url(keyword), download=False)

    hashes = []
    seen = set()
    transcript_api = YouTubeTranscriptApi()
    for entry in (info or {}).get("entries") or []:
        video_hash = entry.get("id")
        duration = entry.get("duration")
        if (
            not video_hash
            or video_hash in seen
            or not VIDEO_ID_RE.match(video_hash)
            # Skip anything that is not a plain video result (channels,
            # playlists, etc.).
            or entry.get("ie_key") != "Youtube"
            # Require a known duration; never return a video we cannot
            # verify is at most 20 minutes long.
            or duration is None
            or duration > MAX_DURATION_SECONDS
        ):
            continue
        # Only return videos whose English transcript we can actually fetch
        # later in the learn pipeline.
        if not has_english_transcript(video_hash, transcript_api):
            continue
        seen.add(video_hash)
        hashes.append(video_hash)
        if len(hashes) >= limit:
            break
    return hashes


def main(argv=None):
    parser = argparse.ArgumentParser(
        description=(
            "Search YouTube and print the matching video hashes. "
            "Only videos published within the last year, at most "
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
    args = parser.parse_args(argv)

    if args.limit < 1:
        parser.error("--limit must be at least 1")

    try:
        hashes = search(args.keyword, args.limit)
    except Exception as exc:
        sys.exit(f"YouTube search failed: {exc}")

    for video_hash in hashes:
        print(video_hash)


if __name__ == "__main__":
    main()
