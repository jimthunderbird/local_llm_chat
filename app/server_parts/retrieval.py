"""Global corpus loading, news/article learning, and chunk helpers."""

from __future__ import annotations

from .core import *

# Learned articles live beside the transcript corpus. An article is learned
# once, then its content and retrieval trunks are reused on disk and included
# in the global BM25 corpus. NEWS_CACHE_DIR is kept as the compatibility name
# for the news-specific callers.
ARTICLES_CACHE_DIR = chat.ARTICLES_DIR

NEWS_CACHE_DIR = ARTICLES_CACHE_DIR

NEWS_CACHE_LOCK = threading.Lock()

NEWS_ID_PATTERN = re.compile(r"news_[A-Za-z0-9]+")

class ClientDisconnected(Exception):
    """Raised when the browser closes an SSE connection mid-stream."""

def capture_call(func, *args, **kwargs):
    """Call ``func`` and also return anything it printed to stdout/stderr."""
    buffer = io.StringIO()
    try:
        with redirect_stdout(buffer), redirect_stderr(buffer):
            result = func(*args, **kwargs)
        return result, buffer.getvalue().strip(), None
    except SystemExit as exc:
        return None, buffer.getvalue().strip(), str(exc)

def load_chunks_captured(**kwargs):
    """Run chat.load_chunks without leaking prints; return (chunks, output, error)."""
    return capture_call(chat.load_chunks, **kwargs)

def load_all_chunks_captured():
    """Load the global corpus without leaking prints."""
    return capture_call(chat.load_all_chunks)

def chunk_is_news(chunk: chat.Chunk) -> bool:
    """Return True when a chunk belongs to a learned news story."""
    return bool(NEWS_ID_PATTERN.fullmatch(chunk.video_id))

def _read_news_meta(news_id: str) -> dict:
    """Load one learned story's metadata (title, link, paragraphs, ...)."""
    if not NEWS_ID_PATTERN.fullmatch(news_id):
        return {}
    try:
        data = json.loads(
            (NEWS_CACHE_DIR / f"{news_id}.json").read_text(encoding="utf-8")
        )
    except (OSError, json.JSONDecodeError):
        return {}
    return data if isinstance(data, dict) else {}

def news_is_learned(news_id: str) -> bool:
    """A story is learned once its metadata and overview trunk exist."""
    if not NEWS_ID_PATTERN.fullmatch(news_id):
        return False
    return (
        (NEWS_CACHE_DIR / f"{news_id}.json").is_file()
        and (NEWS_CACHE_DIR / f"{news_id}.0.trunk").is_file()
    )

def news_title(news_id: str) -> str:
    """Return a learned story's title, or an empty string."""
    meta = _read_news_meta(news_id)
    return str(meta.get("title") or "").strip()

def news_link(news_id: str) -> str:
    """Return a learned story's original article URL, or an empty string."""
    meta = _read_news_meta(news_id)
    return str(meta.get("link") or "").strip()

def chunk_title(chunk: chat.Chunk) -> str:
    """Return the title for a transcript or news chunk."""
    if chunk_is_news(chunk):
        return news_title(chunk.video_id) or chunk.video_id
    return chat.video_title(chunk.video_id)

def chunk_url(chunk: chat.Chunk) -> str:
    """Return the source URL for a transcript or news chunk."""
    if chunk_is_news(chunk):
        return news_link(chunk.video_id)
    return YOUTUBE_URL_TEMPLATE.format(video_id=chunk.video_id)

def news_key_for_item(payload: dict) -> str:
    """Return the stable identity used to cache one news story."""
    link = str(payload.get("link") or "").strip()
    if link:
        # A fragment is a client-side anchor, not a different article.
        return link.split("#", 1)[0].strip()
    title = str(payload.get("title") or "").strip()
    if not title:
        raise ValueError("The news story has no title or link")
    return "\0".join(
        [
            title,
            str(payload.get("published") or "").strip(),
            str(payload.get("source") or "").strip(),
        ]
    )

def news_id_for_item(payload: dict) -> str:
    """Return the content-addressed id for a news story."""
    key = news_key_for_item(payload)
    return "news_" + hashlib.sha1(key.encode("utf-8")).hexdigest()[:16]

def _news_clean_paragraphs(values) -> list[str]:
    """Normalize the paragraphs sent by the browser, dropping duplicates."""
    paragraphs: list[str] = []
    seen: set[str] = set()
    if not isinstance(values, list):
        return paragraphs
    for value in values:
        text = " ".join(str(value or "").split())
        if not text or text in seen:
            continue
        seen.add(text)
        paragraphs.append(text)
    return paragraphs

def _news_trunk(title: str, paragraph: str) -> str:
    return (
        f"<article><title>{xml_escape(title)}</title>"
        f"<paragraph>{xml_escape(paragraph)}</paragraph></article>\n"
    )

def learn_news_article(payload: dict) -> dict:
    """Store one news story and build the learned retrieval trunks.

    The original article content and its learned representation both live in
    ``.chat/articles_cache``: a JSON record plus a Markdown copy of the story,
    and one trunk per paragraph. A compact overview trunk (index 0) makes
    broad questions such as "what is this story about?" retrievable.
    """
    title = " ".join(str(payload.get("title") or "").split())
    if not title:
        raise ValueError("The news story has no title")
    paragraphs = _news_clean_paragraphs(payload.get("paragraphs"))
    summary = " ".join(str(payload.get("summary") or "").split())
    if not paragraphs:
        if summary:
            paragraphs = [summary]
        else:
            raise ValueError("The news story has no readable paragraphs")

    # The result list already assigned a stable id from the feed entry. Reuse
    # it so a learned story keeps the same id when its modal fetches and
    # rewrites the link (for example a Google News wrapper resolving to the
    # publisher's canonical URL). Otherwise the next list request would offer
    # Learn again for an already learned story.
    requested_id = str(payload.get("news_id") or "").strip()
    news_id = (
        requested_id
        if NEWS_ID_PATTERN.fullmatch(requested_id)
        else news_id_for_item(payload)
    )
    link = str(payload.get("link") or "").strip()
    source = str(payload.get("source") or "").strip()
    section = str(payload.get("section") or "").strip()
    published = str(payload.get("published") or "").strip()
    images = [
        str(url).strip()
        for url in (payload.get("images") or [])
        if str(url).strip()
    ][:12]
    meta = {
        "id": news_id,
        "title": title,
        "link": link,
        "source": source,
        "section": section,
        "published": published,
        "summary": summary,
        "paragraphs": paragraphs,
        "images": images,
        "learned_at": time.time(),
    }

    content_lines = [f"# {title}", ""]
    details = [part for part in (source, section) if part]
    if details:
        content_lines.extend([" · ".join(details), ""])
    if published:
        content_lines.extend([published, ""])
    if link:
        content_lines.extend([link, ""])
    if summary and summary != paragraphs[0]:
        content_lines.extend([summary, ""])
    for paragraph in paragraphs:
        content_lines.extend([paragraph, ""])
    content = "\n".join(content_lines).rstrip() + "\n"

    NEWS_CACHE_DIR.mkdir(parents=True, exist_ok=True)
    with NEWS_CACHE_LOCK:
        # Re-learning replaces the old generated trunks instead of appending.
        for old in NEWS_CACHE_DIR.glob(f"{news_id}.*.trunk"):
            try:
                old.unlink()
            except OSError:
                pass
        (NEWS_CACHE_DIR / f"{news_id}.json").write_text(
            json.dumps(meta, ensure_ascii=False, indent=2) + "\n",
            encoding="utf-8",
        )
        (NEWS_CACHE_DIR / f"{news_id}.md").write_text(
            content, encoding="utf-8"
        )
        (NEWS_CACHE_DIR / f"{news_id}.title").write_text(
            title + "\n", encoding="utf-8"
        )
        overview = summary or paragraphs[0]
        (NEWS_CACHE_DIR / f"{news_id}.0.trunk").write_text(
            _news_trunk(title, overview), encoding="utf-8"
        )
        learned_chunks = [
            chat.Chunk(
                video_id=news_id,
                index=0,
                trunk_path=NEWS_CACHE_DIR / f"{news_id}.0.trunk",
            )
        ]
        for index, paragraph in enumerate(paragraphs, start=1):
            trunk_path = NEWS_CACHE_DIR / f"{news_id}.{index}.trunk"
            trunk_path.write_text(
                _news_trunk(title, paragraph), encoding="utf-8"
            )
            learned_chunks.append(
                chat.Chunk(video_id=news_id, index=index, trunk_path=trunk_path)
            )

    # Store the article's trunks and search entries together in the SQLite
    # FTS5 index at .chat/articles_cache/bm25_index_fts.db. The next server
    # start therefore has no re-tokenization work for this article.
    try:
        chat.index_chunks_in_sqlite(learned_chunks, corpus_key="global")
    except Exception:
        # Learning the article on disk succeeded; the index is rebuilt lazily
        # on the next question if SQLite is temporarily unavailable.
        pass

    return {
        "id": news_id,
        "learned": True,
        "title": title,
        "link": link,
        "chunks": len(paragraphs) + 1,
    }

def source_payload(sources):
    """Serialize source chunks for the browser."""
    payload = []
    for score, (chunk, value) in enumerate(sources, start=1):
        payload.append(
            {
                "rank": score,
                "label": chunk.label,
                "video_id": chunk.video_id,
                "index": chunk.index,
                "score": value,
                "title": chunk_title(chunk),
                "kind": "news" if chunk_is_news(chunk) else "video",
                "url": chunk_url(chunk),
            }
        )
    return payload


# Cross-component imports are placed after the definitions so a
# feature module can depend on another feature module safely.
from .answer import YOUTUBE_URL_TEMPLATE
