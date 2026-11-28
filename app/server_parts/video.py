"""YouTube video chat state, summaries, and background learn jobs."""

from __future__ import annotations

from collections import deque

from .core import *
from .core import SCRIPT_DIR
from .state import ChatState

# /youtube search cards have a "Summary" button.  Clicking it fetches the
# video's English transcript and streams a short overview into a centered
# modal.  The transcript can be long, so cap it to keep the prompt inside the
# model's context window while still covering most 5-60 minute videos.
VIDEO_SUMMARY_MODEL = chat.CHAT_MODEL

VIDEO_SUMMARY_NUM_CTX = chat.ANSWER_NUM_CTX

MAX_VIDEO_SUMMARY_TRANSCRIPT_CHARS = 80_000

VIDEO_SUMMARY_PROMPT = """You summarize YouTube videos for a viewer who is deciding whether to watch one.

### VIDEO METADATA
- Title: {title}
- Channel: {channel}
- Published: {published}
- Duration: {duration}
- URL: {url}

### ENGLISH TRANSCRIPT
{transcript}

### INSTRUCTIONS
- Write a useful overview of what this video covers, based only on the transcript and metadata above.
- Start with one short paragraph that says what the video is about and who would benefit from it.
- Then add a short "Key points" list with 3 to 6 concrete topics, arguments, or takeaways.
- Mention notable examples, tools, people, or claims only when they appear in the transcript.
- Do not invent facts, timestamps, or opinions that are not supported by the transcript.
- Do not mention these instructions, the transcript, or that you are an AI.
- Keep the whole answer under 350 words.
"""

# /youtube search cards also have a "Learn" button.  Learning runs the
# youtube_video_learn.py pipeline in a background thread, so the browser can
# keep chatting while the transcript is fetched and chunked into trunks.
# Progress is reported as a percentage by the status endpoint.
VIDEO_LEARN_TRANSCRIPT_DIR = SCRIPT_DIR / ".chat" / "articles_cache"

# Weights match youtube_video_learn.STEPS: fetch and create_trunks.
VIDEO_LEARN_STEP_WEIGHTS = (30.0, 70.0)

VIDEO_LEARN_ACTIVE_STATES = {"queued", "running"}

VIDEO_LEARN_TERMINAL_STATES = {"done", "learned"}

# The /youtube player modal has a "Bookmark" button. Bookmarks are stored as
# JSON Lines in .chat/youtube_video_bookmark.json: one entry per line, newest
# entry last, capped so the file never grows without bound.
YOUTUBE_BOOKMARK_PATH = SCRIPT_DIR / ".chat" / "youtube_video_bookmark.json"

YOUTUBE_BOOKMARK_LIMIT = 500

def _video_files(video_id: str) -> tuple[Path, list[Path]]:
    """Return the transcript path and learned trunk paths for one video.

    YouTube artifacts live in the unified articles cache and carry a
    ``youtube_`` prefix, so they can share the directory with learned news
    stories and web articles.
    """
    storage_id = f"{chat.YOUTUBE_PREFIX}{video_id}"
    transcript = VIDEO_LEARN_TRANSCRIPT_DIR / f"{storage_id}.transcript"
    trunks = list(VIDEO_LEARN_TRANSCRIPT_DIR.glob(f"{storage_id}.*.trunk"))
    return transcript, trunks

def video_has_transcript(video_id: str) -> bool:
    """Whether a video's downloaded transcript is available."""
    if not re.fullmatch(r"[A-Za-z0-9_-]{11}", video_id):
        return False
    transcript, _trunks = _video_files(video_id)
    return transcript.is_file()

def video_timed_transcript_path(video_id: str) -> Path:
    """Where a video's timestamped transcript is cached."""
    return VIDEO_LEARN_TRANSCRIPT_DIR / (
        f"{chat.YOUTUBE_PREFIX}{video_id}.timed.json"
    )


def _estimated_video_timeline(
    video_id: str, transcript: str, duration: float
) -> dict:
    """Build an approximate timeline when NoteGPT has no cached timings.

    The transcript is split into sentence-sized chunks and each chunk gets a
    share of the video duration proportional to its character count. The
    result is labelled ``estimated`` so the UI can tell the viewer that the
    highlighting is approximate.
    """
    if duration <= 0:
        raise RuntimeError(
            "Timed transcript is unavailable and the video duration is unknown."
        )
    text = " ".join(str(transcript or "").split())
    if not text:
        raise RuntimeError("The stored transcript is empty.")
    sentences = [
        part.strip()
        for part in re.split(r"(?<=[.!?])\s+", text)
        if part.strip()
    ]
    chunks: list[str] = []
    current = ""
    for sentence in sentences:
        if current and len(current) + 1 + len(sentence) > 220:
            chunks.append(current)
            current = sentence
        else:
            current = f"{current} {sentence}".strip()
    if current:
        chunks.append(current)
    if not chunks:
        chunks = [text]
    total_chars = sum(len(chunk) for chunk in chunks) or 1
    segments: list[dict] = []
    consumed = 0
    start = 0.0
    for chunk in chunks:
        consumed += len(chunk)
        end = round(duration * consumed / total_chars, 3)
        if end <= start:
            end = min(duration, start + 1.0)
        segments.append(
            {"start": round(start, 3), "end": end, "text": chunk}
        )
        start = end
    if segments:
        segments[-1]["end"] = round(max(duration, segments[-1]["end"]), 3)
    return {
        "video_id": video_id,
        "language": "English",
        "source": "estimated",
        "track": "stored transcript",
        "segments": segments,
    }


def video_transcript_timeline(video_id: str, duration: float = 0.0) -> dict:
    """Return timestamped transcript segments for a learned video.

    The NoteGPT timing is cached next to the transcript after the first
    request, so switching the realtime transcript on again is instant and does
    not call the network. A stale cache is refreshed when the transcript file
    is newer. If NoteGPT cannot provide English timings, the locally stored
    English transcript is split across the known video duration and marked as
    estimated.
    """
    if not re.fullmatch(r"[A-Za-z0-9_-]{11}", video_id):
        raise ValueError("Missing or invalid YouTube video id")
    transcript_path, _trunks = _video_files(video_id)
    if not transcript_path.is_file():
        raise ValueError(
            "This video has no locally stored transcript yet. Learn it first."
        )
    timed_path = video_timed_transcript_path(video_id)
    try:
        cache_is_current = (
            timed_path.is_file()
            and timed_path.stat().st_mtime >= transcript_path.stat().st_mtime
        )
    except OSError:
        cache_is_current = False
    if cache_is_current:
        try:
            cached = json.loads(timed_path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            cached = None
        if (
            isinstance(cached, dict)
            and not cached.get("non_english")
            and isinstance(cached.get("segments"), list)
            and cached["segments"]
        ):
            return cached

    try:
        import fetch_youtube_transcript as transcript_module

        timeline = transcript_module.fetch_timed_transcript(video_id)
    except Exception:
        timeline = None
    if timeline is not None and not timeline.get("non_english"):
        try:
            timed_path.write_text(
                json.dumps(timeline, ensure_ascii=False),
                encoding="utf-8",
            )
        except OSError:
            pass
        return timeline

    try:
        transcript = transcript_path.read_text(encoding="utf-8")
    except OSError as exc:
        raise RuntimeError(
            f"Could not read the stored transcript: {exc}"
        ) from exc
    return _estimated_video_timeline(video_id, transcript, float(duration))


def video_is_learned(video_id: str) -> bool:
    """A video is learned once its RAG trunks exist.

    This intentionally mirrors ``news_is_learned``: chat only needs the
    learned trunks, not a particular transcript or rewrite file. Requiring
    every intermediate file made chats fail after the learning pipeline was
    migrated even though the trunks were present.
    """
    if not re.fullmatch(r"[A-Za-z0-9_-]{11}", video_id):
        return False
    _transcript, trunks = _video_files(video_id)
    return bool(trunks)

def video_is_chat_ready(video_id: str) -> bool:
    """Whether the Chat button can open a usable video session.

    A transcript without trunks is still chat-ready because
    ``get_video_chat_state`` can rebuild the trunks locally before opening.
    """
    return video_is_learned(video_id) or video_has_transcript(video_id)

def ensure_video_trunks(video_id: str) -> bool:
    """Rebuild missing trunks from an existing transcript, if possible.

    Learning is a two-step subprocess pipeline. If it was interrupted after
    the transcript was saved -- or an older release left only a transcript --
    chat can repair the local, offline chunking step without re-downloading.
    """
    if video_is_learned(video_id):
        return True
    if not video_has_transcript(video_id):
        return False
    try:
        import create_trunks

        create_trunks.build_video_trunks(
            video_id, output_dir=VIDEO_LEARN_TRANSCRIPT_DIR
        )
    except (ImportError, OSError, ValueError) as exc:
        raise RuntimeError(
            f"Found the transcript for {video_id!r}, but could not rebuild "
            f"its learned trunks: {exc}"
        ) from exc
    return video_is_learned(video_id)

# The "Chat" button inside a /youtube search video modal keeps its own
# conversation scoped to that one transcript. It must not disturb the main
# chat's transcript scope, so each video gets a lightweight ChatState with
# only its own trunks loaded.
VIDEO_CHAT_STATES: dict[str, ChatState] = {}

VIDEO_CHAT_LOCK = threading.Lock()

def get_video_chat_state(video_id: str) -> ChatState:
    """Return (creating if needed) the isolated chat session for a video."""
    with VIDEO_CHAT_LOCK:
        session = VIDEO_CHAT_STATES.get(video_id)
        if session is not None:
            return session
        # Use the same disk-backed readiness check as the status endpoint.
        # If a transcript exists but its trunks were lost or never finished,
        # rebuild them before declaring the video unlearned.
        if not ensure_video_trunks(video_id):
            raise RuntimeError(
                "This video has not been learned yet. Click Learn first, then "
                "chat about its transcript."
            )
        chunks, _printed, error = load_chunks_captured(video_ids={video_id})
        if error or not chunks:
            raise RuntimeError(
                error
                or f"No trunk files found for transcript {video_id!r}."
            )
        # The transcript scope supplies its own corpus; language detection
        # still decides whether an official language documentation answer is
        # appropriate for a code question.
        session = ChatState(chunks=chunks)
        session.transcript = video_id
        session.global_chunks = chunks
        # A video-modal chat is always scoped to that transcript. It must not
        # inherit the main chat's Online/research mode or route questions to
        # unrelated news research.
        session.research_with_news_mode = False
        VIDEO_CHAT_STATES[video_id] = session
        return session

def forget_video_chat_state(video_id: str) -> None:
    """Drop a cached session, for example after the video is re-learned."""
    with VIDEO_CHAT_LOCK:
        VIDEO_CHAT_STATES.pop(video_id, None)

# The "Chat" button inside a /news story modal keeps its own conversation
# scoped to that one learned story. It is deliberately separate from the main
# chat and from the video chat sessions.
NEWS_CHAT_STATES: dict[str, ChatState] = {}

NEWS_CHAT_LOCK = threading.Lock()

def get_news_chat_state(news_id: str) -> ChatState:
    """Return (creating if needed) the isolated chat session for a story."""
    if not NEWS_ID_PATTERN.fullmatch(news_id):
        raise RuntimeError("Missing or invalid news story id")
    with NEWS_CHAT_LOCK:
        session = NEWS_CHAT_STATES.get(news_id)
        if session is not None:
            return session
        if not news_is_learned(news_id):
            raise RuntimeError(
                "This story has not been learned yet. Click Learn first, then "
                "chat about it."
            )
        chunks = chat.load_news_chunks({news_id})
        if not chunks:
            raise RuntimeError(
                f"No learned trunks found for news story {news_id!r}."
            )
        session = ChatState(chunks=chunks)
        session.transcript = None
        session.news_id = news_id
        session.global_chunks = chunks
        # The story-modal chat must answer only from this learned article.
        # ChatState defaults to Online mode, which would route the question
        # to news research and ignore the scoped article corpus.
        session.research_with_news_mode = False
        NEWS_CHAT_STATES[news_id] = session
        return session

def forget_news_chat_state(news_id: str) -> None:
    """Drop a cached news chat session, for example after re-learning."""
    with NEWS_CHAT_LOCK:
        NEWS_CHAT_STATES.pop(news_id, None)

def refresh_news_corpus(state: ChatState, news_id: str) -> None:
    """Make a newly learned story part of the global corpus immediately."""
    forget_news_chat_state(news_id)
    try:
        with state.lock:
            chat.clear_bm25_cache()
            state.cache.clear()
            chunks, _printed, error = load_all_chunks_captured()
            if error or chunks is None:
                return
            state.global_chunks = chunks
            if (
                state.article_path is not None
                or state.transcript is not None
                or state.news_id is not None
            ):
                return
            state.replace_chunks(chunks)
    except Exception:
        # Learning succeeded even if the in-memory corpus could not refresh;
        # /refresh or the next server start can retry.
        pass

# Each open Project Gutenberg book gets its own article-scoped ChatState. The
# plaintext is split into the same section-aware paragraph trunks used by
# /article, so retrieval sends only a few relevant paragraphs to the model
# instead of the whole book. Keep only a few books resident to bound memory;
# the downloaded plaintext and .article_cache indexes stay on disk for reuse.
GUTENBERG_BOOK_STATES: dict[str, ChatState] = {}

GUTENBERG_BOOK_STATE_ORDER: list[str] = []

GUTENBERG_BOOK_STATE_LOCK = threading.Lock()

def get_gutenberg_chat_state(
    book_id: str, *, text_url: str = "", title: str = ""
) -> ChatState:
    """Return (creating if needed) the isolated RAG session for one book."""
    if not GUTENBERG_BOOK_ID_RE.fullmatch(book_id):
        raise RuntimeError("Missing or invalid Project Gutenberg book id")
    with GUTENBERG_BOOK_STATE_LOCK:
        session = GUTENBERG_BOOK_STATES.get(book_id)
        if session is not None:
            if book_id in GUTENBERG_BOOK_STATE_ORDER:
                GUTENBERG_BOOK_STATE_ORDER.remove(book_id)
            GUTENBERG_BOOK_STATE_ORDER.append(book_id)
            return session

        catalog = GUTENBERG_CATALOG_CACHE.get(book_id) or {}
        book_title = (title or catalog.get("title") or f"Book {book_id}").strip()
        _plaintext, rag_path = ensure_gutenberg_book(
            book_id,
            text_url=text_url or str(catalog.get("text_url") or ""),
            title=book_title,
        )
        chunks, loaded_title, structure = _load_article_chunks(rag_path)
        if not chunks:
            raise RuntimeError("The book has no readable paragraphs.")
        session = ChatState(chunks=chunks)
        session.transcript = None
        session.article_path = str(rag_path)
        session.article_title = loaded_title or book_title
        session.article_structure = structure
        session.article_iterative = True
        session.global_chunks = chunks
        # A book-modal chat stays on the book's own article context.
        session.research_with_news_mode = False
        GUTENBERG_BOOK_STATES[book_id] = session
        GUTENBERG_BOOK_STATE_ORDER.append(book_id)

        while len(GUTENBERG_BOOK_STATE_ORDER) > GUTENBERG_BOOK_STATE_LIMIT:
            oldest = GUTENBERG_BOOK_STATE_ORDER.pop(0)
            GUTENBERG_BOOK_STATES.pop(oldest, None)
            gc.collect()
        return session

def _reconcile_video_learn_job(job: dict) -> dict:
    """Reconcile a terminal job snapshot with what is actually on disk.

    The browser decides whether to show Learn or Chat from this snapshot.
    A job that says "done" after its files were moved or removed must not
    keep advertising a chat session that the server will reject.
    """
    snapshot = dict(job)
    video_id = str(snapshot.get("id") or "")
    if (
        snapshot.get("status") in VIDEO_LEARN_TERMINAL_STATES
        and not video_is_chat_ready(video_id)
    ):
        message = (
            "Learned files are missing for this video. Learn it again to "
            "rebuild its transcript and trunks."
        )
        snapshot.update(
            status="error",
            learned=False,
            message=message,
            error=message,
        )
    elif snapshot.get("status") in VIDEO_LEARN_TERMINAL_STATES:
        # Trunks may have been rebuilt after the job finished.
        snapshot["learned"] = True
    return snapshot

def _video_learn_jobs_snapshot(
    state: ChatState, video_id: str | None = None
) -> list[dict] | dict | None:
    """Return a thread-safe copy of one job or every known job."""
    with state.video_learn_lock:
        if video_id is not None:
            job = state.video_learn_jobs.get(video_id)
            return _reconcile_video_learn_job(job) if job is not None else None
        return [
            _reconcile_video_learn_job(job)
            for job in state.video_learn_jobs.values()
        ]

def _update_video_learn_job(
    state: ChatState, video_id: str, **fields
) -> None:
    with state.video_learn_lock:
        job = state.video_learn_jobs.get(video_id)
        if job is not None:
            job.update(fields)

def start_video_learn_job(
    state: ChatState, video_id: str, title: str = "", relearn: bool = False
) -> tuple[str, dict | None]:
    """Start a background learn job, or report that one already exists.

    ``relearn`` deliberately ignores an already-learned video: it removes the
    generated title/trunks first, then rebuilds them from the saved
    transcript.
    """
    if not relearn and video_is_learned(video_id):
        return "learned", None

    with state.video_learn_lock:
        existing = state.video_learn_jobs.get(video_id)
        if existing is not None and existing.get("status") in VIDEO_LEARN_ACTIVE_STATES:
            return "running", dict(existing)
        job = {
            "id": video_id,
            "title": title,
            "status": "queued",
            "progress": 0.0,
            "step": "queued",
            "message": "Queued for relearning" if relearn else "Queued for learning",
            "error": "",
            "learned": False,
            "relearn": relearn,
            "started_at": time.time(),
            "finished_at": None,
        }
        state.video_learn_jobs[video_id] = job
        snapshot = dict(job)

    threading.Thread(
        target=_run_video_learn_job,
        args=(state, video_id, relearn),
        name=f"video-learn-{video_id}",
        daemon=True,
    ).start()
    return "started", snapshot

def _run_video_learn_job(
    state: ChatState, video_id: str, relearn: bool = False
) -> None:
    """Run youtube_video_learn.steps_for() and publish progress to the UI."""
    _update_video_learn_job(
        state,
        video_id,
        status="running",
        progress=0.0,
        step="relearning" if relearn else "starting",
        message="Starting relearn pipeline" if relearn else "Starting learn pipeline",
    )
    try:
        import youtube_video_learn as learn

        if relearn:
            _update_video_learn_job(
                state,
                video_id,
                step="relearning",
                message="Removing previous title and trunks",
            )
            removed = learn.remove_generated_artifacts(video_id)
            _update_video_learn_job(
                state,
                video_id,
                message=(
                    f"Removed {len(removed)} generated file(s); "
                    "the transcript is unchanged"
                ),
            )

        steps = list(learn.steps_for(relearn))
        if len(steps) == len(VIDEO_LEARN_STEP_WEIGHTS):
            weights = list(VIDEO_LEARN_STEP_WEIGHTS)
        else:
            weights = [100.0 / len(steps)] * len(steps) if steps else []

        progress = 0.0
        for index, (script, script_args) in enumerate(steps):
            start = progress
            end = min(100.0, start + weights[index])
            _update_video_learn_job(
                state,
                video_id,
                step=script,
                progress=round(start, 1),
                message=f"Running {script}",
            )

            argv = [arg.replace("<hash>", video_id) for arg in script_args]
            process = subprocess.Popen(
                [sys.executable, "-u", str(SCRIPT_DIR / script), *argv],
                cwd=SCRIPT_DIR,
                stdout=subprocess.PIPE,
                stderr=subprocess.STDOUT,
                text=True,
                bufsize=1,
            )
            assert process.stdout is not None
            captured: deque[str] = deque(maxlen=80)
            try:
                for line in process.stdout:
                    clean = line.strip()
                    if not clean:
                        continue
                    captured.append(clean)
                    _update_video_learn_job(
                        state, video_id, message=clean[:200]
                    )
                returncode = process.wait()
            finally:
                if process.poll() is None:
                    process.kill()
                    process.wait()

            if returncode != 0:
                import youtube_video_learn as learn

                raise RuntimeError(
                    f"{script} failed for video {video_id} with exit code "
                    f"{returncode}. The step's own output explains why:\n"
                    f"{learn.step_error_detail(captured)}"
                )
            progress = end
            _update_video_learn_job(
                state,
                video_id,
                progress=round(progress, 1),
                message=f"Finished {script}",
            )

        if not video_is_learned(video_id):
            raise RuntimeError(
                "Learning finished but no trunk files were written; the "
                "transcript and learned corpus may be in different "
                "directories."
            )
        _update_video_learn_job(
            state,
            video_id,
            status="done",
            progress=100.0,
            step="done",
            message="Video learned",
            learned=True,
            finished_at=time.time(),
        )
        _refresh_knowledge_after_learn(state, video_id)
    except Exception as exc:
        _update_video_learn_job(
            state,
            video_id,
            status="error",
            message="Learn failed",
            error=str(exc),
            finished_at=time.time(),
        )

def _refresh_knowledge_after_learn(state: ChatState, video_id: str) -> None:
    """Make a newly learned video available without restarting the server."""
    # A re-learn can change the transcript's trunks, so drop any in-modal chat
    # session that still holds the old chunks.
    forget_video_chat_state(video_id)
    try:
        with state.lock:
            chat.clear_bm25_cache()
            state.cache.clear()
            chunks, _printed, error = load_all_chunks_captured()
            if error or chunks is None:
                return
            state.global_chunks = chunks
            # Persist the new trunks and merge the FTS5 segments now, while
            # this background learn job is already off the question path.
            try:
                chat.index_chunks_in_sqlite(
                    chunks, corpus_key="global", prune=True
                )
            except Exception:
                pass
            if state.article_path is not None or state.transcript is not None:
                return
            # A new video is immediately part of the global corpus; no
            # membership check or classification pass is needed.
            state.replace_chunks(chunks)
    except Exception:
        # A failed cache refresh must not turn a successful learn into an
        # error; /refresh and the next server start can retry it.
        pass


# Cross-component imports are placed after the definitions so a
# feature module can depend on another feature module safely.
from .article import GUTENBERG_CATALOG_CACHE, _load_article_chunks
from .command_config import GUTENBERG_BOOK_ID_RE, GUTENBERG_BOOK_STATE_LIMIT
from .library import ensure_gutenberg_book
from .retrieval import NEWS_ID_PATTERN, load_all_chunks_captured, load_chunks_captured, news_is_learned
