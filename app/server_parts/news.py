"""News search queries, article fetches, and the Medium feed."""

from __future__ import annotations

from .core import *

# /medium reads the Medium author handle from this file and asks
# news_fetch.php for that author's RSS feed (https://medium.com/feed/@handle).
MEDIUM_USERNAME_PATH = Path.home() / ".medium" / "username"

def medium_username() -> str:
    """Return the configured Medium handle, without a leading '@'."""
    try:
        value = MEDIUM_USERNAME_PATH.read_text(encoding="utf-8").strip()
    except OSError:
        return ""
    return value.lstrip("@").strip()

def fetch_news_article(
    url: str, title: str = "", timeout: float = 180
) -> dict:
    """Fetch one article body on demand for the news detail modal.

    The list feed uses the same fetcher, but a reader mirror can transiently
    rate-limit a burst of stories. Retrying on demand keeps the detail modal
    usable instead of showing an empty article until the failure cache expires.
    The headline lets the fetcher find a syndicated full-text copy when the
    original publisher is paywalled or bot-challenged.
    """
    url = str(url or "").strip()
    if not re.match(r"^https?://", url, re.IGNORECASE):
        raise ValueError("Missing or invalid article URL")
    # Resolve a Google News wrapper before asking news_fetch.php for the body.
    # This avoids downloading through a URL that Google may answer with its
    # "automated queries" block, and makes the returned canonical link the
    # publisher URL that the modal can offer as "Open article".
    if GOOGLE_NEWS_ARTICLE_RE.match(url):
        resolved = resolve_google_news_url(url)
        if resolved:
            url = resolved
    command = [
        "php",
        str(SCRIPT_DIR / "news_fetch.php"),
        "--json",
        "--article",
        url,
    ]
    title = str(title or "").strip()
    if title:
        command.extend(["--title", title])
    result = subprocess.run(
        command,
        cwd=SCRIPT_DIR,
        capture_output=True,
        text=True,
        timeout=timeout,
    )
    payload = None
    for line in reversed(result.stdout.splitlines()):
        line = line.strip()
        if not line.startswith("{"):
            continue
        try:
            candidate = json.loads(line)
        except json.JSONDecodeError:
            continue
        if isinstance(candidate, dict) and "paragraphs" in candidate:
            payload = candidate
            break
    if payload is None:
        detail = (result.stderr or result.stdout or "").strip()
        detail = detail.splitlines()[-1] if detail else "no article data"
        raise RuntimeError(f"Could not load the article: {detail}")

    paragraphs = [
        str(paragraph).strip()
        for paragraph in (payload.get("paragraphs") or [])
        if str(paragraph).strip()
    ]
    images = [
        str(image).strip()
        for image in (payload.get("images") or [])
        if str(image).strip()
    ]
    return {
        "link": str(payload.get("link") or url).strip(),
        "title": str(payload.get("title") or "").strip(),
        "paragraphs": paragraphs,
        "images": images,
    }

def _article_title_from_url(url: str) -> str:
    """Return a readable fallback headline from an article URL."""
    parsed = urlparse(str(url or "").strip())
    slug = unquote(parsed.path.rstrip("/").rsplit("/", 1)[-1])
    slug = re.sub(r"\.(?:html?|php|aspx?)$", "", slug, flags=re.IGNORECASE)
    slug = re.sub(r"[-_+]+", " ", slug).strip()
    if not slug:
        return parsed.netloc or "Article"
    return slug[:1].upper() + slug[1:]


# Cross-component imports are placed after the definitions so a
# feature module can depend on another feature module safely.
from .core import SCRIPT_DIR
from .reddit import GOOGLE_NEWS_ARTICLE_RE, resolve_google_news_url
