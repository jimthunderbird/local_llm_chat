#!/usr/bin/env python3
"""List every video hash in a YouTube playlist.

Usage:
    python3 get_videos_in_playlist.py <youtube_playlist_id>
    python3 get_videos_in_playlist.py https://www.youtube.com/playlist?list=PL...

Outputs one 11-character YouTube video hash per line, e.g.:
    FTwxtyOEGyM
    T594Cg4sTq4
    ...

Videos are printed in playlist order with duplicates removed.  Videos whose
``.chat/youtube_transcripts/<hash>.transcript`` file already exists are skipped, as
are playlist entries that are not videos (e.g. nested playlists or unavailable
items).
"""

import argparse
import re
import sys
import urllib.parse
from pathlib import Path

try:
    import yt_dlp
except ImportError as exc:  # pragma: no cover - dependency guard
    sys.exit(
        f"Missing dependency: {exc.name}. "
        "Install with: pip install yt-dlp"
    )

VIDEO_ID_RE = re.compile(r"^[A-Za-z0-9_-]{11}$")
LIST_PARAM_RE = re.compile(r"[?&]list=([^&#]+)")
TRANSCRIPT_DIR = Path(__file__).resolve().parent / ".chat" / "youtube_transcripts"


def has_transcript(video_hash: str) -> bool:
    """Return True if the video's transcript has already been downloaded."""
    return (TRANSCRIPT_DIR / f"{video_hash}.transcript").is_file()


def normalize_argv(argv):
    """Let positional values begin with '-' (playlist IDs can contain '-')."""
    if argv and argv[0].startswith("-") and argv[0] not in {"-h", "--help", "--"}:
        return ["--", *argv]
    return argv


def extract_playlist_id(value: str) -> str:
    """Extract a YouTube playlist ID from a bare ID or playlist URL.

    Accepts values such as ``PLBCF2DAC6FFB574DE`` and URLs like
    ``https://www.youtube.com/playlist?list=PL...`` (extra parameters such as
    ``&index=2`` are ignored).
    """
    value = value.strip()
    if not value:
        raise ValueError("Playlist ID is empty.")

    # A bare playlist ID (no scheme, no path, no query string).
    if "/" not in value and "?" not in value and "&" not in value and "#" not in value:
        return value

    match = LIST_PARAM_RE.search(value)
    if match:
        playlist_id = urllib.parse.unquote(match.group(1)).strip()
        if playlist_id:
            return playlist_id

    # Tolerate URLs without a scheme (e.g. youtube.com/playlist?list=...).
    parsed = urllib.parse.urlparse(value if "://" in value else "https://" + value)
    playlist_id = urllib.parse.parse_qs(parsed.query).get("list", [""])[0].strip()
    if playlist_id:
        return playlist_id

    raise ValueError(
        f"Could not extract a YouTube playlist ID from {value!r}. "
        "Expected a playlist ID or a playlist URL containing '?list=<id>'."
    )


def get_videos(playlist_id: str) -> list[str]:
    """Return all video hashes in `playlist_id`, in playlist order."""
    options = {
        "quiet": True,
        "no_warnings": True,
        "skip_download": True,
        "extract_flat": "in_playlist",
    }
    url = (
        "https://www.youtube.com/playlist?"
        + urllib.parse.urlencode({"list": playlist_id})
    )

    with yt_dlp.YoutubeDL(options) as ydl:
        info = ydl.extract_info(url, download=False)

    hashes = []
    seen = set()
    for entry in (info or {}).get("entries") or []:
        if not entry:
            continue
        video_hash = entry.get("id")
        if (
            not video_hash
            or not VIDEO_ID_RE.fullmatch(video_hash)
            or video_hash in seen
            # Skip non-video entries (channels, nested playlists, ...).
            or entry.get("ie_key") not in (None, "Youtube")
            # Skip videos we have already downloaded a transcript for.
            or has_transcript(video_hash)
        ):
            continue
        seen.add(video_hash)
        hashes.append(video_hash)
    return hashes


def main(argv=None):
    parser = argparse.ArgumentParser(
        description=(
            "Print all YouTube video hashes in a playlist, one per line."
        )
    )
    parser.add_argument(
        "youtube_playlist_id",
        help=(
            "YouTube playlist ID or URL, e.g. PLBCF2DAC6FFB574DE or "
            "https://www.youtube.com/playlist?list=PLBCF2DAC6FFB574DE"
        ),
    )
    args = parser.parse_args(normalize_argv(sys.argv[1:]))

    try:
        playlist_id = extract_playlist_id(args.youtube_playlist_id)
    except ValueError as exc:
        sys.exit(str(exc))

    try:
        hashes = get_videos(playlist_id)
    except Exception as exc:
        sys.exit(f"Could not fetch playlist {playlist_id}: {exc}")

    for video_hash in hashes:
        print(video_hash)


if __name__ == "__main__":
    main()
