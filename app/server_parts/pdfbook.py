"""Learn PDF books into trunks and chat with them in the reader modal."""

from __future__ import annotations

from .core import *
from .books import (
    _PDF_BOOK_READER_LOCK,
    _pdf_book_reader,
    _pdf_book_title,
    _pdf_page_paragraphs,
    pdf_book_path,
)
from .state import ChatState

PDF_BOOKS_TRUNKS_DIR = SCRIPT_DIR / ".chat" / "pdf_books" / "trunks"

PDF_LEARN_VERSION = 1

PDF_BOOK_STATE_LIMIT = 3

_PDF_LEARN_LOCKS: dict[str, threading.Lock] = {}
_PDF_LEARN_LOCKS_LOCK = threading.Lock()

PDF_CHAT_STATES: dict[str, ChatState] = {}
PDF_CHAT_ORDER: list[str] = []
PDF_CHAT_LOCK = threading.Lock()


def pdf_book_id(name: str) -> str:
    """Return a stable, filesystem-safe id for one PDF book."""
    path = pdf_book_path(name)
    if path is None:
        raise ValueError("PDF book not found")
    stem = re.sub(r"[^A-Za-z0-9._-]+", "_", path.stem).strip("._") or "book"
    digest = hashlib.sha1(path.name.encode("utf-8")).hexdigest()[:8]
    return f"pdf_{stem[:60]}_{digest}"


def _pdf_trunk_path(book_id: str, index: int) -> Path:
    return PDF_BOOKS_TRUNKS_DIR / f"{book_id}.{index}.trunk"


def _pdf_meta_path(book_id: str) -> Path:
    return PDF_BOOKS_TRUNKS_DIR / f"{book_id}.meta.json"


def _pdf_learn_lock(book_id: str) -> threading.Lock:
    with _PDF_LEARN_LOCKS_LOCK:
        lock = _PDF_LEARN_LOCKS.get(book_id)
        if lock is None:
            lock = threading.Lock()
            _PDF_LEARN_LOCKS[book_id] = lock
        return lock


def pdf_book_page_info(name: str) -> dict:
    """Return the book path, title, and page count before learning."""
    path = pdf_book_path(name)
    if path is None or not path.is_file():
        raise ValueError("PDF book not found")
    _reader, pages = _pdf_book_reader(path)
    if pages <= 0:
        raise RuntimeError("The PDF has no pages")
    return {
        "name": path.name,
        "title": _pdf_book_title(path.name),
        "pages": pages,
    }


def _pdf_clear_trunks(book_id: str) -> None:
    for old in PDF_BOOKS_TRUNKS_DIR.glob(f"{book_id}.*.trunk"):
        try:
            old.unlink()
        except OSError:
            pass
    try:
        _pdf_meta_path(book_id).unlink()
    except OSError:
        pass


def learn_pdf_book(name: str, progress=None) -> dict:
    """Split one PDF into paragraph trunks under PDF_BOOKS_TRUNKS_DIR.

    ``progress(page, total_pages, paragraphs)`` runs after each page so an SSE
    request can report how far learning has progressed. The metadata file is
    written last; its presence marks the book as fully learned.
    """
    path = pdf_book_path(name)
    if path is None or not path.is_file():
        raise ValueError("PDF book not found")
    book_id = pdf_book_id(path.name)
    lock = _pdf_learn_lock(book_id)
    if not lock.acquire(blocking=False):
        raise RuntimeError("This PDF book is already being learned.")
    try:
        PDF_BOOKS_TRUNKS_DIR.mkdir(parents=True, exist_ok=True)
        _pdf_clear_trunks(book_id)
        reader, pages = _pdf_book_reader(path)
        if pages <= 0:
            raise RuntimeError("The PDF has no pages")
        title = _pdf_book_title(path.name)
        title_xml = xml_escape(title)
        index = 0
        for page_number in range(1, pages + 1):
            with _PDF_BOOK_READER_LOCK:
                text = reader.pages[page_number - 1].extract_text() or ""
            for paragraph in _pdf_page_paragraphs(text):
                paragraph = " ".join(str(paragraph).split())
                if len(paragraph) < 20:
                    continue
                index += 1
                trunk = (
                    f"<article><title>{title_xml}</title>"
                    f"<paragraph>{xml_escape(paragraph)}</paragraph></article>"
                )
                _pdf_trunk_path(book_id, index).write_text(
                    trunk + "\n", encoding="utf-8"
                )
            if progress is not None:
                try:
                    progress(page_number, pages, index)
                except Exception:
                    pass
        if index == 0:
            raise RuntimeError("The PDF contains no readable text to learn")
        meta = {
            "version": PDF_LEARN_VERSION,
            "id": book_id,
            "name": path.name,
            "title": title,
            "pages": pages,
            "paragraphs": index,
            "learned_at": time.strftime("%Y-%m-%dT%H:%M:%S%z"),
        }
        meta_path = _pdf_meta_path(book_id)
        temporary = meta_path.with_name(meta_path.name + ".tmp")
        temporary.write_text(
            json.dumps(meta, ensure_ascii=False, indent=2) + "\n",
            encoding="utf-8",
        )
        temporary.replace(meta_path)
        forget_pdf_chat_state(path.name)
        return meta
    finally:
        lock.release()


def pdf_book_status(name: str) -> dict:
    """Return whether one PDF book has learned trunks and its metadata."""
    path = pdf_book_path(name)
    if path is None or not path.is_file():
        raise ValueError("PDF book not found")
    book_id = pdf_book_id(path.name)
    meta_path = _pdf_meta_path(book_id)
    meta: dict = {}
    try:
        raw = json.loads(meta_path.read_text(encoding="utf-8"))
        if isinstance(raw, dict):
            meta = raw
    except (OSError, ValueError, json.JSONDecodeError):
        meta = {}
    trunks = list(PDF_BOOKS_TRUNKS_DIR.glob(f"{book_id}.*.trunk"))
    learned = (
        bool(meta)
        and int(meta.get("version") or 0) == PDF_LEARN_VERSION
        and bool(trunks)
    )
    return {
        "name": path.name,
        "id": book_id,
        "title": str(meta.get("title") or _pdf_book_title(path.name)),
        "pages": int(meta.get("pages") or 0),
        "paragraphs": int(meta.get("paragraphs") or len(trunks)),
        "learned": learned,
    }


def pdf_book_chunks(name: str) -> list[chat.Chunk]:
    """Load a learned PDF book's paragraph trunks for scoped chat."""
    book_id = pdf_book_id(name)
    if not pdf_book_status(name).get("learned"):
        return []
    chunks: list[chat.Chunk] = []
    pattern = re.compile(
        rf"^{re.escape(book_id)}\.(-?\d+)\.trunk$"
    )
    try:
        paths = list(PDF_BOOKS_TRUNKS_DIR.glob(f"{book_id}.*.trunk"))
    except OSError:
        return []
    for path in paths:
        match = pattern.fullmatch(path.name)
        if not match:
            continue
        chunks.append(
            chat.Chunk(
                video_id=book_id,
                index=int(match.group(1)),
                trunk_path=path,
            )
        )
    chunks.sort(key=lambda chunk: chunk.index)
    return chunks


def get_pdf_chat_state(name: str) -> ChatState:
    """Return (creating if needed) the isolated chat session for one PDF."""
    path = pdf_book_path(name)
    if path is None:
        raise ValueError("PDF book not found")
    book_id = pdf_book_id(path.name)
    with PDF_CHAT_LOCK:
        session = PDF_CHAT_STATES.get(book_id)
        if session is not None:
            if book_id in PDF_CHAT_ORDER:
                PDF_CHAT_ORDER.remove(book_id)
            PDF_CHAT_ORDER.append(book_id)
            return session
        status = pdf_book_status(path.name)
        if not status.get("learned"):
            raise RuntimeError(
                "This PDF book has not been learned yet. Click Learn first, "
                "then chat about it."
            )
        chunks = pdf_book_chunks(path.name)
        if not chunks:
            raise RuntimeError(
                "No learned trunks were found for this PDF book."
            )
        session = ChatState(chunks=chunks)
        session.pdf_name = path.name
        session.global_chunks = chunks
        # The in-modal PDF chat must answer from this book only.
        session.research_with_news_mode = False
        PDF_CHAT_STATES[book_id] = session
        PDF_CHAT_ORDER.append(book_id)
        while len(PDF_CHAT_ORDER) > PDF_BOOK_STATE_LIMIT:
            oldest = PDF_CHAT_ORDER.pop(0)
            PDF_CHAT_STATES.pop(oldest, None)
            gc.collect()
        return session


def forget_pdf_chat_state(name: str) -> None:
    """Drop a cached PDF chat session, for example after re-learning."""
    try:
        book_id = pdf_book_id(name)
    except ValueError:
        return
    with PDF_CHAT_LOCK:
        PDF_CHAT_STATES.pop(book_id, None)
        if book_id in PDF_CHAT_ORDER:
            PDF_CHAT_ORDER.remove(book_id)
