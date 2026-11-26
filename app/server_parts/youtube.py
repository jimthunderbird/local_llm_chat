"""YouTube bookmarks and search-result helpers."""

from __future__ import annotations

from .core import *

def read_youtube_bookmarks() -> list[dict]:
    """Return bookmarked videos in file order (oldest first)."""
    try:
        lines = YOUTUBE_BOOKMARK_PATH.read_text(encoding="utf-8").splitlines()
    except OSError:
        return []
    entries: list[dict] = []
    for line in lines:
        line = line.strip()
        if not line:
            continue
        try:
            entry = json.loads(line)
        except json.JSONDecodeError:
            continue
        if isinstance(entry, dict) and entry.get("id"):
            entries.append(entry)
    return entries

def write_youtube_bookmarks(entries: list[dict]) -> None:
    """Atomically rewrite the bookmark file, one JSON entry per line."""
    YOUTUBE_BOOKMARK_PATH.parent.mkdir(parents=True, exist_ok=True)
    content = "".join(
        json.dumps(entry, ensure_ascii=False) + "\n" for entry in entries
    )
    temp_path = YOUTUBE_BOOKMARK_PATH.with_suffix(".json.tmp")
    temp_path.write_text(content, encoding="utf-8")
    temp_path.replace(YOUTUBE_BOOKMARK_PATH)

def add_youtube_bookmark(payload: dict) -> dict:
    """Append one bookmark, moving an existing entry for the same video."""
    video_id = str(payload.get("id") or "").strip()
    if not re.fullmatch(r"[A-Za-z0-9_-]{11}", video_id):
        raise ValueError("Missing or invalid YouTube video id")
    try:
        duration = max(0, int(payload.get("duration") or 0))
    except (TypeError, ValueError):
        duration = 0
    entry = {
        "id": video_id,
        "thumbnail": str(payload.get("thumbnail") or "").strip(),
        "title": str(payload.get("title") or "").strip(),
        "date": str(payload.get("date") or "").strip(),
        "duration": duration,
    }
    entries = [
        existing
        for existing in read_youtube_bookmarks()
        if existing.get("id") != video_id
    ]
    entries.append(entry)
    entries = entries[-YOUTUBE_BOOKMARK_LIMIT:]
    write_youtube_bookmarks(entries)
    return entry

def format_youtube_duration(seconds: int) -> str:
    """Format a duration in seconds as m:ss or h:mm:ss."""
    if seconds <= 0:
        return ""
    hours, remainder = divmod(seconds, 3600)
    minutes, secs = divmod(remainder, 60)
    if hours:
        return f"{hours}:{minutes:02d}:{secs:02d}"
    return f"{minutes}:{secs:02d}"


# Cross-component imports are placed after the definitions so a
# feature module can depend on another feature module safely.
from .video import YOUTUBE_BOOKMARK_LIMIT, YOUTUBE_BOOKMARK_PATH
