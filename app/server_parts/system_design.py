"""System Design and Architecture repo reading mode.

The ``/system_design_and_architecture`` command lists the topics published in
https://github.com/puncsky/system-design-and-architecture and lets the browser
open each English Markdown chapter in a reader modal. The repository files are
fetched from raw.githubusercontent.com and cached under ``.chat`` so the command
also works when GitHub is briefly unreachable.
"""

from __future__ import annotations

from .core import *

SYSTEM_DESIGN_REPO_URL = (
    "https://github.com/puncsky/system-design-and-architecture"
)
SYSTEM_DESIGN_OWNER = "puncsky"
SYSTEM_DESIGN_REPOSITORY = "system-design-and-architecture"
SYSTEM_DESIGN_BRANCH = "main"
SYSTEM_DESIGN_RAW_ROOT = (
    f"https://raw.githubusercontent.com/{SYSTEM_DESIGN_OWNER}/"
    f"{SYSTEM_DESIGN_REPOSITORY}/{SYSTEM_DESIGN_BRANCH}"
)
SYSTEM_DESIGN_SUMMARY_URL = f"{SYSTEM_DESIGN_RAW_ROOT}/SUMMARY.md"
SYSTEM_DESIGN_BLOB_ROOT = (
    f"{SYSTEM_DESIGN_REPO_URL}/blob/{SYSTEM_DESIGN_BRANCH}"
)
SYSTEM_DESIGN_CACHE_DIR = (
    SCRIPT_DIR / ".chat" / "system_design_and_architecture"
)
SYSTEM_DESIGN_SUMMARY_CACHE = SYSTEM_DESIGN_CACHE_DIR / "SUMMARY.md"
SYSTEM_DESIGN_SUMMARY_TTL_SECONDS = 6 * 60 * 60
SYSTEM_DESIGN_REQUEST_TIMEOUT = 25

SYSTEM_DESIGN_LOCK = threading.Lock()
SYSTEM_DESIGN_TOPIC_CACHE: dict[str, dict] = {}

_SYSTEM_DESIGN_MARKDOWN_LINK_RE = re.compile(
    r"\[([^\]]+)\]\(([^)\s]+\.md)(#[^)\s]*)?\)"
)
_SYSTEM_DESIGN_IMAGE_RE = re.compile(
    r"!\[([^\]]*)\]\(([^)\s]+)(\s+[\"'][^\"']*[\"'])?\)"
)
_SYSTEM_DESIGN_HTML_IMAGE_RE = re.compile(
    r"(<img\b[^>]*?\bsrc=[\"'])([^\"']+)([\"'])",
    re.IGNORECASE,
)
_SYSTEM_DESIGN_SAFE_PATH_RE = re.compile(
    r"^(?:[A-Za-z0-9._-]+/)*[A-Za-z0-9._-]+\.md$"
)


def _system_design_normalize_path(path: str) -> str:
    """Return a safe repository-relative Markdown path or raise ValueError."""
    value = str(path or "").strip().replace("\\", "/")
    while value.startswith("./"):
        value = value[2:]
    if value.startswith("/"):
        value = value.lstrip("/")
    if not value or ".." in value.split("/"):
        raise ValueError("Invalid System Design topic path")
    if _SYSTEM_DESIGN_SAFE_PATH_RE.fullmatch(value) is None:
        raise ValueError("Invalid System Design topic path")
    return value


def _system_design_fetch(url: str) -> bytes:
    """Download one repository file with a descriptive error on failure."""
    try:
        response = requests.get(
            url,
            timeout=SYSTEM_DESIGN_REQUEST_TIMEOUT,
            headers={
                "User-Agent": "local-llm-chat-system-design/1.0",
                "Accept": "text/plain, */*",
            },
        )
    except requests.RequestException as exc:
        raise RuntimeError(f"Could not fetch {url}: {exc}") from exc
    if response.status_code != 200:
        raise RuntimeError(
            f"Could not fetch {url}: HTTP {response.status_code}"
        )
    return response.content


def _system_design_summary_text(force: bool = False) -> str:
    """Return SUMMARY.md from the network, falling back to the local cache."""
    with SYSTEM_DESIGN_LOCK:
        cache_is_fresh = False
        if SYSTEM_DESIGN_SUMMARY_CACHE.is_file():
            age = time.time() - SYSTEM_DESIGN_SUMMARY_CACHE.stat().st_mtime
            cache_is_fresh = age < SYSTEM_DESIGN_SUMMARY_TTL_SECONDS
            if not force and cache_is_fresh:
                try:
                    return SYSTEM_DESIGN_SUMMARY_CACHE.read_text(
                        encoding="utf-8"
                    )
                except OSError:
                    pass
        try:
            data = _system_design_fetch(SYSTEM_DESIGN_SUMMARY_URL)
            text = data.decode("utf-8", errors="replace")
        except RuntimeError:
            if SYSTEM_DESIGN_SUMMARY_CACHE.is_file():
                try:
                    return SYSTEM_DESIGN_SUMMARY_CACHE.read_text(
                        encoding="utf-8"
                    )
                except OSError:
                    pass
            raise
        SYSTEM_DESIGN_CACHE_DIR.mkdir(parents=True, exist_ok=True)
        SYSTEM_DESIGN_SUMMARY_CACHE.write_text(text, encoding="utf-8")
        return text


def _system_design_clean_title(value: str) -> str:
    """Turn a Markdown link label into a plain title."""
    text = re.sub(r"[`*_]", "", str(value or "")).strip()
    return " ".join(text.split())


def _system_design_topics_from_summary(text: str) -> list[dict]:
    """Parse the SUMMARY.md headings and chapter links into topic records."""
    topics: list[dict] = []
    seen: set[str] = set()
    group = "Topics"
    for raw_line in str(text or "").splitlines():
        heading = re.match(r"^\s*#{1,6}\s+(.*?)\s*$", raw_line)
        if heading:
            group = _system_design_clean_title(heading.group(1)) or group
            continue
        link = re.search(r"\[([^\]]+)\]\(([^)]+\.md)\)", raw_line)
        if link is None:
            continue
        try:
            path = _system_design_normalize_path(unquote(link.group(2)))
        except ValueError:
            continue
        if path in seen:
            continue
        seen.add(path)
        title = _system_design_clean_title(link.group(1)) or path
        topics.append(
            {
                "title": title,
                "path": path,
                "group": group,
                "url": f"{SYSTEM_DESIGN_BLOB_ROOT}/{quote(path, safe='/')}",
                "raw_url": f"{SYSTEM_DESIGN_RAW_ROOT}/{quote(path, safe='/')}",
            }
        )
    return topics


def system_design_topics(force: bool = False) -> list[dict]:
    """Return every topic in the repository's English SUMMARY.md."""
    text = _system_design_summary_text(force=force)
    topics = _system_design_topics_from_summary(text)
    if not topics:
        raise RuntimeError(
            "The System Design and Architecture SUMMARY.md listed no topics."
        )
    return topics


def _system_design_repository_url(path: str) -> str:
    return f"{SYSTEM_DESIGN_BLOB_ROOT}/{quote(path, safe='/')}"


def _system_design_split_front_matter(markdown: str) -> tuple[str, str]:
    """Return (body, front-matter title) for a repository Markdown file."""
    text = str(markdown or "")
    if text.startswith("\ufeff"):
        text = text[1:]
    match = re.match(
        r"^---[ \t]*\r?\n(.*?)\r?\n---[ \t]*(?:\r?\n|$)",
        text,
        re.DOTALL,
    )
    if match is None:
        return text, ""
    title = ""
    title_match = re.search(
        r"^title:\s*(.+?)\s*$", match.group(1), re.MULTILINE
    )
    if title_match:
        title = _system_design_clean_title(
            title_match.group(1).strip().strip("\"'")
        )
    return text[match.end():], title


def _system_design_html_to_markdown(markdown: str) -> str:
    """Convert the simple HTML used by the repo README into Markdown.

    Chapters are mostly Markdown, but the introduction is an HTML page with
    centered headings, links, and badge images. The browser's Markdown reader
    does not execute raw HTML, so convert that small tag vocabulary while
    leaving fenced code blocks untouched.
    """
    parts = re.split(r"(```[\s\S]*?```)", str(markdown or ""))
    for index in range(0, len(parts), 2):
        value = parts[index]

        def html_image(match: re.Match) -> str:
            attrs = match.group(1)
            source = re.search(
                r"\bsrc\s*=\s*[\"']([^\"']+)[\"']", attrs, re.I
            )
            if source is None:
                return ""
            alt = re.search(
                r"\balt\s*=\s*[\"']([^\"']*)[\"']", attrs, re.I
            )
            alt_text = alt.group(1) if alt else ""
            return f"\n![{alt_text}]({source.group(1)})\n"

        def html_link(match: re.Match) -> str:
            href, label = match.group(1), match.group(2)
            label = re.sub(r"<[^>]+>", "", label).strip()
            return f"[{label}]({href})"

        value = re.sub(
            r"<img\b([^>]*)>", html_image, value, flags=re.IGNORECASE
        )
        value = re.sub(
            r"<a\b[^>]*\bhref\s*=\s*[\"']([^\"']+)[\"'][^>]*>"
            r"(.*?)</a>",
            html_link,
            value,
            flags=re.IGNORECASE | re.DOTALL,
        )
        value = re.sub(
            r"<h([1-6])\b[^>]*>(.*?)</h\1>",
            lambda match: (
                "\n"
                + "#" * int(match.group(1))
                + " "
                + re.sub(r"<[^>]+>", "", match.group(2)).strip()
                + "\n"
            ),
            value,
            flags=re.IGNORECASE | re.DOTALL,
        )
        value = re.sub(
            r"</?(?:b|strong)\b[^>]*>", "**", value, flags=re.IGNORECASE
        )
        value = re.sub(
            r"</?(?:i|em)\b[^>]*>", "*", value, flags=re.IGNORECASE
        )
        value = re.sub(
            r"<br\s*/?>", "\n", value, flags=re.IGNORECASE
        )
        value = re.sub(
            r"</?(?:p|div|center|table|thead|tbody|tr|td|th|ul|ol|li|"
            r"details|summary|section|article|figure|figcaption|span|font|"
            r"small|sup|sub)\b[^>]*>",
            "\n",
            value,
            flags=re.IGNORECASE,
        )
        # Drop any remaining raw tags so the reader never shows literal HTML.
        value = re.sub(r"<[^>]+>", "", value)
        parts[index] = value
    return "".join(parts)


def _system_design_rewrite_links(markdown: str, path: str) -> str:
    """Make relative chapter links and image sources absolute.

    GitHub serves Markdown with paths relative to the chapter file, while the
    reader modal renders the text directly. Images therefore point at the raw
    file host, and chapter links point at the repository's Blob view.
    """
    directory = posixpath.dirname(path)

    def resolve(target: str) -> str:
        if re.match(r"^(?:[a-z]+:)?//", target, re.IGNORECASE):
            return target
        if target.startswith("#"):
            return target
        clean = target.split("#", 1)[0].split("?", 1)[0]
        relative = posixpath.normpath(posixpath.join(directory, clean))
        if relative.startswith("../") or relative == "..":
            return target
        return relative

    def markdown_link(match: re.Match) -> str:
        label, target, fragment = (
            match.group(1),
            match.group(2),
            match.group(3) or "",
        )
        relative = resolve(target)
        if relative == target:
            return match.group(0)
        return (
            f"[{label}]({_system_design_repository_url(relative)}{fragment})"
        )

    def markdown_image(match: re.Match) -> str:
        alt, target, title = match.group(1), match.group(2), match.group(3) or ""
        relative = resolve(target)
        if relative == target:
            return match.group(0)
        source = f"{SYSTEM_DESIGN_RAW_ROOT}/{quote(relative, safe='/')}"
        return f"![{alt}]({source}{title})"

    def html_image(match: re.Match) -> str:
        prefix, target, suffix = match.group(1), match.group(2), match.group(3)
        relative = resolve(target)
        if relative == target:
            return match.group(0)
        source = f"{SYSTEM_DESIGN_RAW_ROOT}/{quote(relative, safe='/')}"
        return f"{prefix}{source}{suffix}"

    markdown = _SYSTEM_DESIGN_IMAGE_RE.sub(markdown_image, markdown)
    markdown = _SYSTEM_DESIGN_HTML_IMAGE_RE.sub(html_image, markdown)
    markdown = _SYSTEM_DESIGN_MARKDOWN_LINK_RE.sub(markdown_link, markdown)
    return markdown


def system_design_topic(path: str) -> dict:
    """Return one chapter's English Markdown, cached under .chat."""
    normalized = _system_design_normalize_path(path)
    with SYSTEM_DESIGN_LOCK:
        cached = SYSTEM_DESIGN_TOPIC_CACHE.get(normalized)
        if cached is not None:
            return dict(cached)
        cache_path = SYSTEM_DESIGN_CACHE_DIR / normalized
        markdown = ""
        if cache_path.is_file():
            try:
                markdown = cache_path.read_text(encoding="utf-8")
            except OSError:
                markdown = ""
        if not markdown:
            raw_url = (
                f"{SYSTEM_DESIGN_RAW_ROOT}/{quote(normalized, safe='/')}"
            )
            data = _system_design_fetch(raw_url)
            markdown = data.decode("utf-8", errors="replace")
            cache_path.parent.mkdir(parents=True, exist_ok=True)
            cache_path.write_text(markdown, encoding="utf-8")
        body, front_matter_title = _system_design_split_front_matter(markdown)
        title_match = re.search(r"^\s*#\s+(.+?)\s*$", body, re.MULTILINE)
        html_title_match = re.search(
            r"<h1\b[^>]*>(.*?)</h1>", body, re.IGNORECASE | re.DOTALL
        )
        title = front_matter_title
        if not title and title_match:
            title = _system_design_clean_title(title_match.group(1))
        if not title and html_title_match:
            title = _system_design_clean_title(
                re.sub(r"<[^>]+>", "", html_title_match.group(1))
            )
        if not title:
            title = normalized.rsplit("/", 1)[-1].removesuffix(".md")
        rendered_markdown = _system_design_html_to_markdown(body)
        result = {
            "path": normalized,
            "title": title,
            "markdown": _system_design_rewrite_links(
                rendered_markdown, normalized
            ),
            "url": _system_design_repository_url(normalized),
            "raw_url": (
                f"{SYSTEM_DESIGN_RAW_ROOT}/{quote(normalized, safe='/')}"
            ),
            "repo_url": SYSTEM_DESIGN_REPO_URL,
        }
        SYSTEM_DESIGN_TOPIC_CACHE[normalized] = result
        return dict(result)


# Cross-component imports are placed after the definitions so a
# feature module can depend on another feature module safely.
import posixpath
