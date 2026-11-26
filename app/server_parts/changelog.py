"""Completed-task summary pages under .logs."""

from __future__ import annotations

from .core import *

# ---- /changelog ------------------------------------------------------
def changelog_directory() -> Path | None:
    """Return the folder holding completed-task summary pages."""
    for candidate in (LOGS_DIR, SCRIPT_DIR / ".log"):
        if candidate.is_dir():
            return candidate
    return None

def _changelog_label(name: str, timestamp: str) -> tuple[str, str, str]:
    """Split a summary file name into its prompt, slug, and readable title."""
    label = name[: -len(".summary.html")]
    if timestamp and label.endswith("-" + timestamp):
        label = label[: -(len(timestamp) + 1)]
    prompt, _, slug = label.partition("-")
    title = " ".join(part for part in slug.split("-") if part).strip()
    if not title:
        title = label.replace("-", " ").strip() or name
    return prompt or "task", slug, title

def list_changelog_entries() -> list[dict]:
    """List completed-task summary pages, newest first."""
    directory = changelog_directory()
    if directory is None:
        return []
    entries: list[dict] = []
    for path in directory.iterdir():
        try:
            if not path.is_file():
                continue
            name = path.name
            if CHANGELOG_FILE_RE.fullmatch(name) is None:
                continue
            stat = path.stat()
        except OSError:
            continue
        timestamp_match = CHANGELOG_TIMESTAMP_RE.search(name)
        timestamp = timestamp_match.group(1) if timestamp_match else ""
        if timestamp:
            display_time = (
                f"{timestamp[:4]}-{timestamp[5:7]}-{timestamp[8:10]} "
                f"{timestamp[11:13]}:{timestamp[14:16]}:{timestamp[17:19]}"
            )
        else:
            display_time = time.strftime(
                "%Y-%m-%d %H:%M:%S", time.localtime(stat.st_mtime)
            )
        prompt, slug, title = _changelog_label(name, timestamp)
        entries.append(
            {
                "name": name,
                "title": title,
                "prompt": prompt,
                "slug": slug,
                "timestamp": display_time,
                "sort_key": timestamp or time.strftime(
                    "%Y-%m-%d-%H-%M-%S", time.localtime(stat.st_mtime)
                ),
                "mtime": stat.st_mtime,
                "size": stat.st_size,
                "url": f"/api/changelog_file?name={quote(name)}",
            }
        )
    entries.sort(key=lambda item: (item["sort_key"], item["mtime"]), reverse=True)
    return entries

def changelog_file_path(name: str) -> Path | None:
    """Resolve one summary page without allowing it to escape the log folder."""
    directory = changelog_directory()
    if directory is None:
        return None
    name = str(name or "").strip()
    if not name or name != Path(name).name:
        return None
    if CHANGELOG_FILE_RE.fullmatch(name) is None:
        return None
    root = directory.resolve()
    path = (directory / name).resolve()
    try:
        path.relative_to(root)
    except ValueError:
        return None
    return path if path.is_file() else None


# Cross-component imports are placed after the definitions so a
# feature module can depend on another feature module safely.
from .core import CHANGELOG_FILE_RE, CHANGELOG_TIMESTAMP_RE, LOGS_DIR, SCRIPT_DIR
