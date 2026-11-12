#!/usr/bin/env python3
"""HTTP API server for the browser front end in app/chat.html.

The server reuses chat.py for chunk loading, BM25 retrieval, prompt building,
Ollama streaming, transcript/article scoping, history, /memory,
/status, /sources, /benchmark, /youtube, /news, /mdn, /googlenews, /medium,
/gutenberg, /openlibrary, /changelog, /wikivoyage, /wiki, /wiki_article,
/article, /dict, and /ask. The browser is a
pure HTML/CSS/JavaScript single-page app served from ./app/chat.html.

Every question searches the one global trunk corpus. Tags are no longer a
user-facing scope; the corpus is never partitioned by topic.

Usage:
    python3 app/chat_server.py [--host 127.0.0.1] [--port 8765]
                                  [--max-context-tokens N] [--progressive]

Endpoints:
    GET  /                       app/chat.html
    GET  /api/state              current chat state + command list + history
    GET  /api/autocomplete       ?kind=commands|transcripts|paths&q=...
    GET  /api/wiki?word=...      JSON Wikipedia summary
    GET  /api/transcripts?q=...  ag title search results
    GET  /api/changelog_file     ?name=<summary.html> one completed-task page
                                  used by /changelog's detail modal
    GET  /api/gutenberg_book     ?id=<book_id>&url=<plaintext_url>&title=...
                                  the cached plaintext used by the book modal
    POST /api/ask                SSE: question or slash-command stream
                                  (an optional `image` data URL attaches one
                                  PNG/JPEG/WebP/GIF and answers visually.
                                  Answers include a per-paragraph
                                  source-video annotation event. A question that
                                  names Rust, Go, or PHP uses the official
                                  documentation for that language; a code question
                                  consults the documentation first, before any
                                  local code generation. A language-feature
                                  explanation instead combines related official
                                  documentation excerpts with the retrieved
                                  transcript trunks. A "source" event says
                                  whether the currently streaming answer is
                                  coming from the local Ollama model or the
                                  official docs. /news streams "news" events
                                  from CNN, Yahoo Finance,
                                  KQED, SFGATE, SF Bay View, Mission Local,
                                  Richmond Review, and TechCrunch).
    POST /api/dict               SSE: dictionary + latest-answer usage stream
    POST /api/translate          SSE: paragraph-by-paragraph Simplified Chinese
                                  translation of an answer
    POST /api/tts                one article paragraph -> local text-to-speech
                                  WAV (used by the news modal's read-aloud icon);
                                  an optional voice selects a TTS voice
    POST /api/ai_news_podcast    SSE: draft a 2000-word HOST/GUEST script from
                                  the /ai_news_podcast stories and return the
                                  two male voices used by /api/tts
    POST /api/explain_line       SSE: explain one line of a code snippet from a
                                  Go, Rust, or PHP
    POST /api/snippet_question   SSE: answer a free-form question about a whole
                                  code snippet
    POST /api/news_learn         store one /news story and build its learned
                                  trunks in .chat/articles_cache
    GET  /api/news_status        ?id=<news_id> whether one story is learned
    GET  /api/news_item          ?id=<news_id> full learned story for the modal
    GET  /api/news_article       ?url=<article_url> fetch a story body for the
                                  detail modal when the list feed had no body
    GET  /api/wikivoyage_article ?url=<article_url> fetch a complete Wikivoyage
                                  article (headings, text, lists, and figures)
                                  for the /wikivoyage detail modal
    POST /api/news_chat          SSE: answer a question from one learned news
                                  story in a session isolated from the main chat
    POST /api/video_summary      SSE: fetch a YouTube video's English transcript
                                  and stream an AI summary for the /youtube
                                  search result modal
    POST /api/video_chat         SSE: answer a question from one learned video's
                                  transcript inside the video modal, using a
                                  session isolated from the main chat
    POST /api/video_learn        start the youtube_video_learn.py pipeline in
                                  the background for a /youtube search card
    GET  /api/video_learn_status current background learn progress (all jobs,
                                  or ?id=<video_id> for one)
    POST /api/gutenberg_chat     SSE: answer a question about one Project
                                  Gutenberg book in a session isolated from
                                  the main chat
"""

from __future__ import annotations

import argparse
import base64
import codecs
import gc
import hashlib
import html
import importlib.util
import io
import json
import math
import os
import re
import selectors
import shutil
import subprocess
import sys
import tempfile
import threading
import wave
import zipfile
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from contextlib import redirect_stderr, redirect_stdout
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Callable, NamedTuple
from urllib.parse import parse_qs, quote, quote_plus, unquote, urlparse
from xml.sax.saxutils import escape as xml_escape
from xml.etree import ElementTree

try:
    import requests
except ImportError as exc:  # pragma: no cover - dependency guard
    sys.exit(f"Missing dependency: {exc.name}. Install with: pip install requests")

# The server lives in ./app, while chat.py and its data directories live in the
# repository root. Resolve both and make the root importable when run as a script.
APP_DIR = Path(__file__).resolve().parent
SCRIPT_DIR = APP_DIR.parent
if str(SCRIPT_DIR) not in sys.path:
    sys.path.insert(0, str(SCRIPT_DIR))

import chat
import sqlite_bm25
from paragraph_utils import split_paragraphs
from doc_answer_cleanup import (
    CodeFenceRepairFilter,
    InsufficientContextFilter,
    repair_code_fences,
    strip_leading_insufficient_context,
)

CHAT_HTML = APP_DIR / "chat.html"
MAX_REQUEST_BYTES = 20 * 1024 * 1024
PATH_COMPLETION_LIMIT = 50

# /changelog lists the task summary pages written by the prompt workflow.
# Those pages live in .logs at the repository root; .log is accepted too so an
# older checkout with that spelling still works. File names are trusted only
# when they match this pattern, and the resolver never leaves the log folder.
LOGS_DIR = SCRIPT_DIR / ".logs"
CHANGELOG_FILE_RE = re.compile(
    r"^[A-Za-z0-9][A-Za-z0-9_.-]*\.summary\.html$"
)
CHANGELOG_TIMESTAMP_RE = re.compile(
    r"(\d{4}-\d{2}-\d{2}-\d{2}-\d{2}-\d{2})\.summary\.html$"
)

WEB_COMMANDS = {
    **chat.COMMANDS,
    "/help": "show commands and keyboard shortcuts",
    "/youtube": "YouTube commands: learn <hash|url> | learn_topic <topic> | "
    "learn_playlist <playlist> | search <keyword> | bookmark list",
    "/news": "list the latest news from wsj, cnn, cbslite, nprtext, yahoo_finance, medium, kqed, sfgate, sfbayview, mission_local, richmond_sunset, techcrunch, krasia, wccftech, or hackernews (optionally /news [source ...] [count]); /news search <keywords> [website] searches Google News, optionally only that website",
    "/mdn": "search MDN Web Docs by keywords: /mdn <keywords>",
    "/googlenews": "search Google News for the latest stories: /googlenews <keywords>",
    "/howstuffworks": "search HowStuffWorks articles by keywords: /howstuffworks <keywords>",
    "/wikivoyage": "search Wikivoyage destinations by keywords: /wikivoyage <keywords>",
    "/medium": "list the latest stories from the Medium author in ~/.medium/username (optionally /medium [count])",
    "/ai_news_podcast": "show the top five stories from the last 12 hours and create a two-voice AI news podcast",
    "/gutenberg": "search Project Gutenberg books: /gutenberg search <keywords>; click a book to read it and ask questions",
    "/openlibrary": "search inside Open Library books: /openlibrary search <keywords>",
    "/article": "use one local article file as context; iterative evidence is the default (--simple disables it)",
    "/mathtutor": "upload Microsoft Word test papers and practice similar math problems with detailed answers",
    "/changelog": "list completed-task summaries from .logs (newest first); click one to read it",
}

# News sources accepted by /news and completed by news_fetch.php.
NEWS_SOURCES = {
    "wsj": "The Wall Street Journal",
    "cnn": "CNN News",
    "cbslite": "CBC Lite",
    "nprtext": "NPR (Text)",
    "yahoo_finance": "Yahoo Finance",
    "medium": "Medium",
    "kqed": "KQED News",
    "sfgate": "SFGATE Bay Area News",
    "sfbayview": "SF Bay View",
    "mission_local": "Mission Local",
    "richmond_sunset": "Richmond Review",
    "techcrunch": "TechCrunch",
    "krasia": "KrASIA",
    "wccftech": "Wccftech",
    "hackernews": "Hacker News",
}

NEWS_SOURCE_USAGE = "|".join(NEWS_SOURCES)

# /news search <keywords> <website> restricts a Google News search to one
# site. Accept a bare hostname (howstuffworks.com), a www host, or a full URL;
# the site: operator uses the host plus an optional path.
NEWS_SEARCH_WEBSITE_RE = re.compile(
    r"^(?:https?://)?(?P<host>(?:[a-z0-9](?:[a-z0-9-]*[a-z0-9])?\.)+"
    r"[a-z]{2,63})(?P<path>/[^\s]*)?$",
    re.IGNORECASE,
)
# A trailing token such as "node.js" or "notes.md" is far more likely to be a
# keyword than a website, so these file-like endings only count as a site when
# the user is explicit (a scheme, www., or a path).
NEWS_SEARCH_FILE_EXTENSIONS = {
    "js", "jsx", "ts", "tsx", "py", "rb", "go", "rs", "php", "java",
    "c", "h", "cpp", "cs", "sh", "md", "html", "htm", "css", "json",
    "xml", "yaml", "yml", "toml", "csv", "sql", "txt", "ipynb", "pdf",
    "zip", "tar", "gz", "jpg", "jpeg", "png", "gif", "svg", "webp",
    "mp3", "mp4", "wav",
}


def split_news_search_website(keywords: str) -> tuple[str, str]:
    """Split a trailing website off a /news search query.

    ``/news search <keywords> <website>`` searches Google News with a
    ``site:<website>`` restriction. Only the final token is considered, and
    only when at least one keyword precedes it, so ordinary searches (and a
    lone ``node.js`` keyword) are left untouched.

    Returns ``(keywords, website)`` where ``website`` is an empty string when
    no site was requested.
    """
    stripped = keywords.strip()
    parts = stripped.split()
    if len(parts) < 2:
        return stripped, ""

    candidate = parts[-1]
    match = NEWS_SEARCH_WEBSITE_RE.match(candidate)
    if match is None:
        return stripped, ""

    host = match.group("host")
    path = (match.group("path") or "").rstrip("/")
    extension = host.rsplit(".", 1)[-1].lower()
    explicit = (
        candidate.lower().startswith(("http://", "https://", "www."))
        or "/" in candidate
    )
    if extension in NEWS_SEARCH_FILE_EXTENSIONS and not explicit:
        return stripped, ""

    return " ".join(parts[:-1]), host + path

# /mdn searches the public MDN Web Docs search API and renders the matches as
# a /news-style result list. Each result opens in the existing story modal so
# readers can translate, read aloud, or follow the original documentation link.
MDN_SEARCH_URL = "https://developer.mozilla.org/api/v1/search"
MDN_DOCS_BASE_URL = "https://developer.mozilla.org"
MDN_LOCALE = "en-US"
MDN_RESULT_LIMIT = 10
MDN_HIGHLIGHT_LIMIT = 3

# /ai_news_podcast lists the five newest stories from the last 12 hours and
# turns their full article bodies into a two-host podcast script, read aloud
# with two distinct male voices.
AI_NEWS_PODCAST_QUERY = "when:12h news"
# latest_news_url is https://news.google.com/rss/search?q=when:12h&... ;
# Google News returns an empty feed for the bare time operator, so the command
# keeps the same 12-hour window and adds the word "news" to receive stories.
AI_NEWS_PODCAST_LIMIT = 5
AI_NEWS_PODCAST_MODEL = chat.CHAT_MODEL
AI_NEWS_PODCAST_SOURCE_CHARS = 24_000
AI_NEWS_PODCAST_NUM_PREDICT = 5_000
AI_NEWS_PODCAST_NUM_CTX = chat.ANSWER_NUM_CTX
AI_NEWS_PODCAST_PROMPT = """You are the head writer for a daily AI news podcast.

Below is the source material: up to five current news stories with their
title, source, summary, and full article paragraphs.

Write an engaging podcast episode of approximately 2000 words. It must sound
like two male co-hosts talking together: HOST leads the show and GUEST adds
analysis and context. Discuss every story in the source material, in order,
and never invent facts that are absent from the source.

Formatting rules (follow them exactly):
- Put exactly one speaker label at the start of every line: HOST: or GUEST:.
- Every spoken turn starts with one of those two labels and a space.
- Alternate naturally between HOST and GUEST; do not write stage directions,
  markdown, headings, bullet points, timestamps, or sound-effect notes.
- Do not include an episode title or a closing summary section.
- Aim for roughly 2000 words of spoken dialogue.

Source material:
{source}

Podcast script:
"""


def _module_available(name: str) -> bool:
    """Return True when an optional Python package is importable."""
    try:
        return importlib.util.find_spec(name) is not None
    except (ImportError, ValueError):
        return False


def _clean_voice_name(voice: str) -> str:
    """Keep a user-supplied voice name safe for a subprocess argument."""
    value = " ".join(str(voice or "").split())
    if len(value) > 80:
        value = value[:80].strip()
    return value.replace("\x00", "")


def _podcast_voice_defaults() -> dict[str, str]:
    """Pick two distinct male voices for the local TTS engine.

    Kokoro uses its two American male voices, while the macOS ``say`` and
    espeak fallbacks use male voices that ship with those engines. Environment
    overrides make it easy to choose different voices without code changes.
    """
    if _module_available("kokoro") and _module_available("numpy"):
        host, guest = "am_michael", "am_adam"
    elif shutil.which("say"):
        host, guest = "Daniel", "Fred"
    elif shutil.which("espeak-ng") or shutil.which("espeak"):
        host, guest = "en-us+m3", "en-us+m4"
    else:
        host, guest = "", ""
    return {
        "host": _clean_voice_name(
            os.environ.get("PODCAST_HOST_VOICE", host)
        ),
        "guest": _clean_voice_name(
            os.environ.get("PODCAST_GUEST_VOICE", guest)
        ),
    }

# Learned articles live beside the transcript corpus. An article is learned
# once, then its content and retrieval trunks are reused on disk and included
# in the global BM25 corpus. NEWS_CACHE_DIR is kept as the compatibility name
# for the news-specific callers.
ARTICLES_CACHE_DIR = chat.ARTICLES_DIR
NEWS_CACHE_DIR = ARTICLES_CACHE_DIR
NEWS_CACHE_LOCK = threading.Lock()
NEWS_ID_PATTERN = re.compile(r"news_[A-Za-z0-9]+")

# The news modal's read-aloud button sends one article paragraph at a time to
# /api/tts. The local engine returns a small WAV so playback starts as soon as
# the first paragraph is ready while the browser prefetches the next one.
# research/text_to_speech.txt uses Kokoro; it is used when installed, with the
# operating system's voice as a fast fallback (macOS `say`, Linux espeak).
TTS_CACHE_DIR = SCRIPT_DIR / ".chat" / "tts_cache"
TTS_ENGINE = os.environ.get("TTS_ENGINE", "auto").strip().lower()
TTS_MAX_CHARS = 12_000
TTS_CACHE_LOCK = threading.Lock()
TTS_KEY_LOCKS: dict[str, threading.Lock] = {}
KOKORO_PIPELINE = None
KOKORO_PIPELINE_LOCK = threading.Lock()
KOKORO_SYNTH_LOCK = threading.Lock()


def _tts_voice(override: str = "") -> str:
    return _clean_voice_name(override) or os.environ.get("TTS_VOICE", "").strip()


def _tts_cache_path(text: str, voice: str = "") -> Path:
    voice = _tts_voice(voice) or os.environ.get("KOKORO_VOICE", "").strip()
    digest = hashlib.sha256(
        f"{TTS_ENGINE}\0{voice}\0{text}".encode("utf-8")
    ).hexdigest()
    return TTS_CACHE_DIR / f"{digest}.wav"


def _tts_key_lock(key: str) -> threading.Lock:
    with TTS_CACHE_LOCK:
        return TTS_KEY_LOCKS.setdefault(key, threading.Lock())


def _synthesize_kokoro(text: str, out_path: Path, voice: str = "") -> None:
    """Render one paragraph with the local Kokoro pipeline from the research."""
    global KOKORO_PIPELINE
    try:
        import numpy as np  # type: ignore
        from kokoro import KPipeline  # type: ignore
    except Exception as exc:  # pragma: no cover - optional dependency
        raise RuntimeError("kokoro is not installed") from exc

    with KOKORO_PIPELINE_LOCK:
        if KOKORO_PIPELINE is None:
            lang_code = os.environ.get("KOKORO_LANG_CODE", "a")
            KOKORO_PIPELINE = KPipeline(lang_code=lang_code)
        pipeline = KOKORO_PIPELINE

    voice = _tts_voice(voice) or os.environ.get("KOKORO_VOICE", "af_heart")
    parts = []
    # The pipeline owns a single model instance; serialize inference when the
    # browser prefetches the next paragraph concurrently.
    with KOKORO_SYNTH_LOCK:
        for _graphemes, _phonemes, audio in pipeline(text, voice=voice):
            samples = np.asarray(audio, dtype=np.float32).reshape(-1)
            if samples.size:
                parts.append(samples)
    if not parts:
        raise RuntimeError("Kokoro produced no audio")
    samples = np.concatenate(parts) if len(parts) > 1 else parts[0]
    pcm = (np.clip(samples, -1.0, 1.0) * 32767.0).astype("<i2")
    with wave.open(str(out_path), "wb") as wav:
        wav.setnchannels(1)
        wav.setsampwidth(2)
        wav.setframerate(24_000)
        wav.writeframes(pcm.tobytes())


def _synthesize_macos(text: str, out_path: Path, voice: str = "") -> None:
    executable = shutil.which("say")
    if not executable:
        raise RuntimeError("macOS 'say' is not available")
    command = [
        executable,
        "-o",
        str(out_path),
        "--file-format=WAVE",
        "--data-format=LEI16@22050",
    ]
    voice = _tts_voice(voice)
    if voice:
        command.extend(["-v", voice])
    command.append(text)
    subprocess.run(
        command,
        check=True,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.PIPE,
        timeout=120,
    )


def _synthesize_espeak(text: str, out_path: Path, voice: str = "") -> None:
    executable = shutil.which("espeak-ng") or shutil.which("espeak")
    if not executable:
        raise RuntimeError("espeak-ng/espeak is not available")
    command = [executable, "-w", str(out_path)]
    voice = _tts_voice(voice)
    if voice:
        command.extend(["-v", voice])
    command.append(text)
    subprocess.run(
        command,
        check=True,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.PIPE,
        timeout=120,
    )


def _synthesize_to_file(text: str, out_path: Path, voice: str = "") -> None:
    engine = TTS_ENGINE
    errors: list[str] = []

    if engine in {"auto", "kokoro"}:
        try:
            _synthesize_kokoro(text, out_path, voice)
            return
        except Exception as exc:
            errors.append(f"kokoro: {exc}")
            if engine == "kokoro":
                raise RuntimeError("; ".join(errors)) from exc

    if engine in {"auto", "say"}:
        if sys.platform == "darwin" or engine == "say":
            try:
                _synthesize_macos(text, out_path, voice)
                return
            except Exception as exc:
                errors.append(f"say: {exc}")

    if engine in {"auto", "espeak"}:
        try:
            _synthesize_espeak(text, out_path, voice)
            return
        except Exception as exc:
            errors.append(f"espeak: {exc}")

    detail = "; ".join(errors) or f"unknown TTS_ENGINE {engine!r}"
    raise RuntimeError(f"No local text-to-speech engine is available ({detail})")


def _prewarm_tts() -> None:
    """Load the local TTS model in the background so the first click is fast."""
    if os.environ.get("TTS_PREWARM", "1").strip() == "0":
        return
    try:
        synthesize_speech("Text to speech is ready.")
    except Exception:
        # TTS is optional; a missing engine must not affect chat startup.
        pass


def _keepalive_watchdog() -> None:
    """Refresh Ollama's keep-alive timer without generating a real answer.

    Ollama evicts a model after ``OLLAMA_KEEP_ALIVE`` without traffic. Any
    request refreshes that timer, so a one-token generation every few minutes
    keeps the single chat model resident. The request uses the same
    ``num_ctx`` as the real calls, so it never triggers a context-size reload.
    """
    while True:
        time.sleep(KEEPALIVE_WATCHDOG_INTERVAL_SECONDS)
        try:
            response = requests.post(
                f"{chat.OLLAMA_HOST}/api/generate",
                json={
                    "model": chat.CHAT_MODEL,
                    "prompt": "",
                    "stream": False,
                    "think": False,
                    "keep_alive": chat.OLLAMA_KEEP_ALIVE,
                    "options": {
                        "num_predict": 1,
                        "num_ctx": chat.ANSWER_NUM_CTX,
                    },
                },
                timeout=(2, 60),
            )
            response.close()
        except requests.RequestException:
            # The watchdog is best-effort; a stopped Ollama simply retries.
            pass


def synthesize_speech(text: str, voice: str = "") -> bytes:
    """Return a cached WAV for one article paragraph, synthesizing if needed."""
    text = " ".join(str(text or "").split())
    if not text:
        raise ValueError("Missing text to read aloud")
    if len(text) > TTS_MAX_CHARS:
        raise ValueError(
            f"That paragraph is too long to read aloud (limit "
            f"{TTS_MAX_CHARS:,} characters)."
        )

    TTS_CACHE_DIR.mkdir(parents=True, exist_ok=True)
    cache_path = _tts_cache_path(text, voice)
    with _tts_key_lock(cache_path.name):
        try:
            cached = cache_path.read_bytes()
            if len(cached) > 44:
                return cached
        except OSError:
            pass

        temp_path = cache_path.with_name(
            f".{cache_path.stem}.{os.getpid()}.{threading.get_ident()}.tmp.wav"
        )
        try:
            _synthesize_to_file(text, temp_path, voice)
            if not temp_path.exists() or temp_path.stat().st_size <= 44:
                raise RuntimeError("The text-to-speech engine produced no audio")
            os.replace(temp_path, cache_path)
            return cache_path.read_bytes()
        finally:
            try:
                temp_path.unlink()
            except OSError:
                pass

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


def _mdn_plain_text(value: object) -> str:
    """Turn one MDN summary or highlighted snippet into readable text."""
    if isinstance(value, list):
        return "\n\n".join(
            text for text in (_mdn_plain_text(item) for item in value) if text
        )
    text = html.unescape(re.sub(r"<[^>]*>", "", str(value or "")))
    text = re.sub(r"[ \t]+", " ", text)
    text = re.sub(r"\n{3,}", "\n\n", text)
    return text.strip()


def search_mdn_docs(keyword: str, limit: int = MDN_RESULT_LIMIT) -> list[dict]:
    """Search MDN Web Docs and return normalized documentation result cards."""
    keyword = " ".join(str(keyword or "").split())
    if not keyword:
        return []
    try:
        response = requests.get(
            MDN_SEARCH_URL,
            params={"q": keyword, "locale": MDN_LOCALE},
            headers={**chat.HTTP_HEADERS, "Accept": "application/json"},
            timeout=(10, 30),
        )
        response.raise_for_status()
        data = response.json()
    except (requests.RequestException, ValueError) as exc:
        raise RuntimeError(f"MDN search failed: {exc}") from exc

    documents = data.get("documents") if isinstance(data, dict) else None
    if not isinstance(documents, list):
        raise RuntimeError("MDN returned an unexpected response.")

    results: list[dict] = []
    for document in documents[: max(1, int(limit))]:
        if not isinstance(document, dict):
            continue
        mdn_url = str(document.get("mdn_url") or "").strip()
        title = _mdn_plain_text(document.get("title")) or "(untitled)"
        summary = _mdn_plain_text(document.get("summary"))
        if not mdn_url and not summary:
            continue
        link = (
            MDN_DOCS_BASE_URL + mdn_url
            if mdn_url.startswith("/")
            else mdn_url
        )
        passages: list[str] = []
        highlight = document.get("highlight")
        bodies = highlight.get("body") if isinstance(highlight, dict) else None
        if isinstance(bodies, list):
            for raw in bodies[:MDN_HIGHLIGHT_LIMIT]:
                passage = _mdn_plain_text(raw)
                if (
                    passage
                    and passage not in passages
                    and passage.casefold() not in summary.casefold()
                ):
                    passages.append(passage)
        results.append(
            {
                "title": title,
                "link": link,
                "summary": summary,
                "paragraphs": passages,
            }
        )
    return results


# /wikivoyage searches the Wikivoyage REST API and renders the destinations as
# a /news-style result list. Each card carries the destination description,
# summary extract, coordinates, and a link to the full Wikivoyage article.
WIKIVOYAGE_SEARCH_URL = "https://en.wikivoyage.org/w/rest.php/v1/search/page"
WIKIVOYAGE_SUMMARY_URL = (
    "https://en.wikivoyage.org/api/rest_v1/page/summary/{title}"
)
WIKIVOYAGE_BASE_URL = "https://en.wikivoyage.org"
WIKIVOYAGE_RESULT_LIMIT = 10
WIKIVOYAGE_SUMMARY_WORKERS = 8


def _wikivoyage_plain_text(value: object) -> str:
    """Turn a Wikivoyage excerpt, extract, or description into plain text."""
    text = html.unescape(re.sub(r"<[^>]*>", "", str(value or "")))
    text = re.sub(r"[ \t]+", " ", text)
    text = re.sub(r"\n{3,}", "\n\n", text)
    return text.strip()


def _wikivoyage_page_link(page: dict, summary: dict) -> str:
    """Return the canonical Wikivoyage article URL for one search page."""
    content_urls = summary.get("content_urls")
    if isinstance(content_urls, dict):
        desktop = content_urls.get("desktop")
        if isinstance(desktop, dict):
            page_url = str(desktop.get("page") or "").strip()
            if page_url:
                return page_url
    key = str(page.get("key") or page.get("title") or "").strip()
    if not key:
        return ""
    return (
        WIKIVOYAGE_BASE_URL
        + "/wiki/"
        + quote(key.replace(" ", "_"), safe="/()_',.:!-~")
    )


def _wikivoyage_summary(key: str) -> dict:
    """Fetch one destination summary, returning an empty dict on failure."""
    if not key:
        return {}
    try:
        response = requests.get(
            WIKIVOYAGE_SUMMARY_URL.format(
                title=quote(key.replace(" ", "_"), safe="")
            ),
            headers={**chat.HTTP_HEADERS, "Accept": "application/json"},
            timeout=(10, 30),
        )
        response.raise_for_status()
        data = response.json()
    except (requests.RequestException, ValueError):
        return {}
    return data if isinstance(data, dict) else {}


def search_wikivoyage_destinations(
    keyword: str, limit: int = WIKIVOYAGE_RESULT_LIMIT
) -> list[dict]:
    """Search Wikivoyage and return normalized destination result cards."""
    keyword = " ".join(str(keyword or "").split())
    if not keyword:
        return []
    try:
        response = requests.get(
            WIKIVOYAGE_SEARCH_URL,
            params={"q": keyword, "limit": max(1, int(limit))},
            headers={**chat.HTTP_HEADERS, "Accept": "application/json"},
            timeout=(10, 30),
        )
        response.raise_for_status()
        data = response.json()
    except (requests.RequestException, ValueError) as exc:
        raise RuntimeError(f"Wikivoyage search failed: {exc}") from exc

    pages = data.get("pages") if isinstance(data, dict) else None
    if not isinstance(pages, list):
        raise RuntimeError("Wikivoyage returned an unexpected response.")
    pages = [page for page in pages if isinstance(page, dict)][: max(1, int(limit))]

    summaries: dict[int, dict] = {}
    if pages:
        with ThreadPoolExecutor(
            max_workers=min(WIKIVOYAGE_SUMMARY_WORKERS, len(pages))
        ) as executor:
            futures = {
                executor.submit(
                    _wikivoyage_summary,
                    str(page.get("key") or page.get("title") or "").strip(),
                ): index
                for index, page in enumerate(pages)
            }
            for future in as_completed(futures):
                try:
                    summaries[futures[future]] = future.result()
                except Exception:  # pragma: no cover - defensive fallback
                    summaries[futures[future]] = {}

    results: list[dict] = []
    for index, page in enumerate(pages):
        summary = summaries.get(index) or {}
        title = _wikivoyage_plain_text(
            summary.get("title") or page.get("title")
        ) or "(untitled)"
        description = _wikivoyage_plain_text(
            summary.get("description") or page.get("description")
        )
        excerpt = _wikivoyage_plain_text(page.get("excerpt"))
        extract = _wikivoyage_plain_text(summary.get("extract")) or excerpt
        link = _wikivoyage_page_link(page, summary)
        if not link and not extract:
            continue

        paragraphs: list[str] = []
        if extract:
            paragraphs.append(extract)
        coordinates = summary.get("coordinates")
        if isinstance(coordinates, dict):
            latitude = coordinates.get("lat")
            longitude = coordinates.get("lon")
            if isinstance(latitude, (int, float)) and isinstance(
                longitude, (int, float)
            ):
                paragraphs.append(
                    f"Coordinates: {latitude:.6g}, {longitude:.6g}"
                )

        card_summary = description
        if extract and extract != description:
            card_summary = f"{description} — {extract}" if description else extract

        thumbnail = page.get("thumbnail") or summary.get("thumbnail") or {}
        image_url = ""
        if isinstance(thumbnail, dict):
            image_url = str(
                thumbnail.get("source") or thumbnail.get("url") or ""
            ).strip()
            if image_url.startswith("//"):
                image_url = "https:" + image_url

        results.append(
            {
                "title": title,
                "link": link,
                "description": description,
                "summary": card_summary,
                "paragraphs": paragraphs,
                "images": [image_url] if image_url else [],
            }
        )
    return results


# The detail modal uses the MediaWiki parse API so a destination opens as the
# complete article, not just the search API's short overview extract. The
# returned HTML is well-formed XML, which lets us keep headings, paragraphs,
# lists, and figures in their original reading order.
WIKIVOYAGE_API_URL = "https://en.wikivoyage.org/w/api.php"
# The normal page-render endpoint is CDN-cached and is not subject to the
# MediaWiki API's tight per-IP request limit. A /wikivoyage search already
# makes one search request plus a summary request per card, so opening a card
# immediately afterwards through action=parse could return HTTP 429 and leave
# the modal showing only the short search extract.
WIKIVOYAGE_RENDER_URL = WIKIVOYAGE_BASE_URL + "/w/index.php"
WIKIVOYAGE_RETRY_MAX_SECONDS = 35.0
WIKIVOYAGE_ARTICLE_CACHE_TTL = 3600.0
_WIKIVOYAGE_ARTICLE_CACHE: dict[str, tuple[float, dict]] = {}
_WIKIVOYAGE_ARTICLE_CACHE_LOCK = threading.Lock()
_WIKIVOYAGE_VOID_TAGS = (
    "area",
    "base",
    "br",
    "col",
    "embed",
    "hr",
    "img",
    "input",
    "link",
    "meta",
    "param",
    "source",
    "track",
    "wbr",
)
_WIKIVOYAGE_PREDEFINED_ENTITIES = {"amp", "apos", "gt", "lt", "quot"}
WIKIVOYAGE_ARTICLE_IMAGE_LIMIT = 80
_WIKIVOYAGE_SKIP_TABLE_CLASSES = (
    "infobox",
    "routebox",
    "article-status",
    "navbox",
    "metadata",
    "noprint",
)


def _wikivoyage_title_from_url(url: str, title: str = "") -> str:
    """Return the article title encoded in a Wikivoyage URL."""
    fallback = " ".join(str(title or "").split())
    parsed = urlparse(str(url or "").strip())
    path = unquote(parsed.path or "")
    if path.startswith("/wiki/"):
        candidate = path[len("/wiki/") :].replace("_", " ").strip()
        candidate = candidate.split("#", 1)[0].strip()
        if candidate and not candidate.lower().startswith("special:"):
            return candidate
    query_title = (parse_qs(parsed.query).get("title") or [""])[0].strip()
    if query_title:
        return query_title
    return fallback


def _wikivoyage_element_text(element) -> str:
    """Return normalized plain text for one parsed HTML element.

    MediaWiki pages embed their templatestyles in <style> elements inside the
    article body. Those rules are presentation, not article text, so exclude
    them (and edit links) from headings, paragraphs, listings, and tables.
    """
    if element is None:
        return ""
    parts: list[str] = []

    def collect(node) -> None:
        tag = str(node.tag or "").lower()
        if tag in {"style", "script", "sup", "nav"}:
            return
        if node.text:
            parts.append(str(node.text))
        for child in node:
            collect(child)
            if child.tail:
                parts.append(str(child.tail))

    collect(element)
    return " ".join("".join(parts).split())


def _wikivoyage_image_url(image) -> str:
    """Return the largest available absolute URL for one article image."""
    sources: list[str] = []
    srcset = str(image.get("srcset") or "").strip()
    # Wikimedia map URLs contain commas in the path (osm-intl,10,...), so a
    # plain split on every comma truncates them to a broken URL. srcset
    # candidates are only separated by a comma followed by the next URL.
    for candidate in re.split(r",\s*(?=https?://)", srcset):
        parts = candidate.strip().split()
        if parts:
            sources.append(parts[0])
    source = str(image.get("src") or "").strip()
    if source:
        sources.append(source)
    if not sources:
        return ""
    source = html.unescape(sources[0]).strip()
    if source.startswith("//"):
        source = "https:" + source
    return source if source.startswith(("http://", "https://")) else ""


class _WikivoyageRequestError(RuntimeError):
    """One Wikivoyage endpoint failed; retry_after carries a 429 hint."""

    def __init__(self, message: str, retry_after: float = 0.0):
        super().__init__(message)
        self.retry_after = max(0.0, float(retry_after))


def _wikivoyage_retry_after(response) -> float:
    """Return the response's Retry-After delay, bounded to a usable wait."""
    raw = str(response.headers.get("Retry-After") or "").strip()
    try:
        seconds = float(raw)
    except ValueError:
        seconds = 5.0
    return max(1.0, min(WIKIVOYAGE_RETRY_MAX_SECONDS, seconds))


def _wikivoyage_request_error(source: str, response) -> _WikivoyageRequestError:
    if response.status_code == 429:
        delay = _wikivoyage_retry_after(response)
        return _WikivoyageRequestError(
            f"{source} is rate-limited (HTTP 429); retry after {delay:.0f}s",
            delay,
        )
    return _WikivoyageRequestError(
        f"{source} returned HTTP {response.status_code}"
    )


def _wikivoyage_render_document(title: str) -> str:
    """Return the full article HTML through the normal CDN-cached page path."""
    try:
        response = requests.get(
            WIKIVOYAGE_RENDER_URL,
            params={"title": title, "action": "render"},
            headers={**chat.HTTP_HEADERS, "Accept": "text/html"},
            timeout=(10, 60),
        )
    except requests.RequestException as exc:
        raise _WikivoyageRequestError(f"page render failed: {exc}") from exc
    if response.status_code != 200:
        raise _wikivoyage_request_error("page render", response)
    document = response.text
    if not document.strip():
        raise _WikivoyageRequestError("page render returned an empty article")
    return document


def _wikivoyage_parse_document(title: str) -> str:
    """Return the full article HTML through the MediaWiki parse API."""
    try:
        response = requests.get(
            WIKIVOYAGE_API_URL,
            params={
                "action": "parse",
                "page": title,
                "prop": "text",
                "format": "json",
                "formatversion": 2,
                "redirects": 1,
                "disableeditsection": 1,
            },
            headers={**chat.HTTP_HEADERS, "Accept": "application/json"},
            timeout=(10, 60),
        )
    except requests.RequestException as exc:
        raise _WikivoyageRequestError(f"parse API failed: {exc}") from exc
    if response.status_code != 200:
        raise _wikivoyage_request_error("MediaWiki parse API", response)
    try:
        payload = response.json()
    except ValueError as exc:
        raise _WikivoyageRequestError(
            f"MediaWiki parse API returned invalid JSON: {exc}"
        ) from exc
    parse = payload.get("parse") if isinstance(payload, dict) else None
    if not isinstance(parse, dict):
        detail = ""
        if isinstance(payload, dict):
            error = payload.get("error")
            if isinstance(error, dict):
                detail = str(error.get("info") or "").strip()
        raise _WikivoyageRequestError(
            f"MediaWiki parse API: {detail or 'unexpected API response'}"
        )
    document = str(parse.get("text") or "")
    if not document.strip():
        raise _WikivoyageRequestError(
            "MediaWiki parse API returned an empty article"
        )
    return document


def _wikivoyage_fetch_document(title: str) -> tuple[str, bool]:
    """Fetch one complete destination article, returning (document, is_html).

    The rendered page is preferred because a /wikivoyage search has just made
    several API requests, and the parse API can answer the immediate modal
    open with HTTP 429. The API remains the fallback. If both endpoints are
    rate-limited, wait for the advertised Retry-After and try once more.
    """
    errors: list[str] = []
    for attempt in range(2):
        retry_after = 0.0
        for source, html_document, fetch in (
            ("page render", True, _wikivoyage_render_document),
            ("MediaWiki parse API", False, _wikivoyage_parse_document),
        ):
            try:
                return fetch(title), html_document
            except _WikivoyageRequestError as exc:
                errors.append(str(exc))
                retry_after = max(retry_after, exc.retry_after)
        if attempt == 0 and retry_after > 0:
            time.sleep(retry_after)
            continue
        break
    detail = errors[-1] if errors else "unexpected response"
    raise RuntimeError(f"Could not load the Wikivoyage article: {detail}")


def _wikivoyage_cached_article(key: str) -> dict | None:
    """Return a recently fetched article, or None when the cache misses."""
    now = time.time()
    with _WIKIVOYAGE_ARTICLE_CACHE_LOCK:
        entry = _WIKIVOYAGE_ARTICLE_CACHE.get(key)
        if entry is None:
            return None
        fetched_at, article = entry
        if now - fetched_at > WIKIVOYAGE_ARTICLE_CACHE_TTL:
            _WIKIVOYAGE_ARTICLE_CACHE.pop(key, None)
            return None
        return article


def _wikivoyage_store_article(key: str, article: dict) -> None:
    """Keep a small in-memory cache so reopening a card needs no request."""
    with _WIKIVOYAGE_ARTICLE_CACHE_LOCK:
        if len(_WIKIVOYAGE_ARTICLE_CACHE) >= 16:
            oldest = sorted(
                _WIKIVOYAGE_ARTICLE_CACHE.items(), key=lambda item: item[1][0]
            )[:4]
            for stale_key, _ in oldest:
                _WIKIVOYAGE_ARTICLE_CACHE.pop(stale_key, None)
        _WIKIVOYAGE_ARTICLE_CACHE[key] = (time.time(), article)


def _wikivoyage_named_entity(match: re.Match[str]) -> str:
    """Convert an HTML named entity to an XML-safe numeric reference."""
    name = match.group(1)
    if name in _WIKIVOYAGE_PREDEFINED_ENTITIES:
        return match.group(0)
    decoded = html.unescape(match.group(0))
    if decoded == match.group(0):
        return "&amp;" + name + ";"
    return "".join(f"&#{ord(character)};" for character in decoded)


def _wikivoyage_html_to_xml(document: str) -> str:
    """Make the action=render HTML parseable by the shared block walker."""
    text = re.sub(r"<!--.*?-->", "", str(document or ""), flags=re.S)
    void_pattern = "|".join(_WIKIVOYAGE_VOID_TAGS)
    text = re.sub(
        rf"<({void_pattern})(\b[^>]*?)(?<!/)>",
        r"<\1\2/>",
        text,
        flags=re.I,
    )
    text = re.sub(r"&([A-Za-z][A-Za-z0-9]+);", _wikivoyage_named_entity, text)
    text = re.sub(
        r"&(?!#\d+;|#x[0-9A-Fa-f]+;|amp;|lt;|gt;|quot;|apos;)",
        "&amp;",
        text,
    )
    return "<root>" + text + "</root>"


def _wikivoyage_article_blocks(document: str, *, html_document: bool = False) -> list[dict]:
    """Turn MediaWiki article HTML into ordered blocks for the detail modal."""
    if html_document:
        document = _wikivoyage_html_to_xml(document)
    try:
        root = ElementTree.fromstring(document)
    except ElementTree.ParseError:
        # Defensive fallback: still return readable text if the parser output
        # is ever not well-formed XML.
        text = html.unescape(re.sub(r"<[^>]+>", " ", document))
        text = " ".join(text.split())
        return [{"type": "paragraph", "text": text}] if text else []

    blocks: list[dict] = []
    seen_images: set[str] = set()
    image_count = 0

    def add_image(image, caption: str = "") -> None:
        nonlocal image_count
        if image_count >= WIKIVOYAGE_ARTICLE_IMAGE_LIMIT:
            return
        url = _wikivoyage_image_url(image)
        if not url:
            return
        key = url.split("?", 1)[0]
        if key in seen_images:
            return
        seen_images.add(key)
        image_count += 1
        alt = " ".join(str(image.get("alt") or "").split())
        blocks.append(
            {
                "type": "image",
                "url": url,
                "caption": " ".join(str(caption or "").split()),
                "alt": alt,
            }
        )

    def walk(node) -> None:
        tag = str(node.tag or "").lower()
        classes = str(node.get("class") or "").lower()
        if tag in {"style", "script", "link", "meta", "nav", "sup"}:
            return
        if tag == "div" and "mw-heading" in classes:
            heading = next(
                (
                    child
                    for child in node
                    if str(child.tag or "").lower()
                    in {"h1", "h2", "h3", "h4", "h5", "h6"}
                ),
                None,
            )
            if heading is not None:
                text = _wikivoyage_element_text(heading)
                if text:
                    blocks.append(
                        {
                            "type": "heading",
                            "level": int(str(heading.tag)[1]),
                            "text": text,
                        }
                    )
                return
        if tag in {"h1", "h2", "h3", "h4", "h5", "h6"}:
            text = _wikivoyage_element_text(node)
            if text:
                blocks.append(
                    {"type": "heading", "level": int(tag[1]), "text": text}
                )
            return
        if tag == "figure":
            caption = _wikivoyage_element_text(node.find("figcaption"))
            for image in node.iter("img"):
                add_image(image, caption)
                caption = ""
            return
        if tag == "img":
            width = str(image_size(node))
            if width.isdigit() and int(width) >= 160:
                add_image(node)
            return
        if tag == "p":
            for image in node.iter("img"):
                width = str(image_size(image))
                if width.isdigit() and int(width) >= 160:
                    add_image(image)
            text = _wikivoyage_element_text(node)
            if text:
                blocks.append({"type": "paragraph", "text": text})
            return
        if tag in {"ul", "ol"}:
            items = [
                _wikivoyage_element_text(child)
                for child in node
                if str(child.tag or "").lower() == "li"
            ]
            items = [item for item in items if item]
            if items:
                blocks.append(
                    {"type": "list", "ordered": tag == "ol", "items": items}
                )
            return
        if tag == "dl":
            for child in node:
                if str(child.tag or "").lower() not in {"dt", "dd"}:
                    continue
                text = _wikivoyage_element_text(child)
                if text:
                    blocks.append({"type": "paragraph", "text": text})
            return
        if tag == "table":
            if any(name in classes for name in _WIKIVOYAGE_SKIP_TABLE_CLASSES):
                return
            rows: list[str] = []
            for row in node.iter("tr"):
                cells = [
                    _wikivoyage_element_text(cell)
                    for cell in row
                    if str(cell.tag or "").lower() in {"td", "th"}
                ]
                cells = [cell for cell in cells if cell]
                if cells:
                    rows.append(" — ".join(cells))
            if rows:
                blocks.append(
                    {"type": "list", "ordered": False, "items": rows}
                )
            return
        for child in node:
            walk(child)

    def image_size(image) -> int | str:
        for attribute in ("data-file-width", "width"):
            value = str(image.get(attribute) or "").strip()
            if value.isdigit():
                return int(value)
        return ""

    walk(root)
    return blocks


def fetch_wikivoyage_article(url: str, title: str = "") -> dict:
    """Fetch the complete Wikivoyage article behind a search result card."""
    article_title = _wikivoyage_title_from_url(url, title)
    if not article_title:
        raise ValueError("Missing Wikivoyage article title")

    cache_key = article_title.casefold()
    cached = _wikivoyage_cached_article(cache_key)
    if cached is not None:
        return cached

    document, html_document = _wikivoyage_fetch_document(article_title)
    blocks = _wikivoyage_article_blocks(document, html_document=html_document)
    paragraphs: list[str] = []
    images: list[str] = []
    seen_paragraphs: set[str] = set()
    seen_images: set[str] = set()
    for block in blocks:
        kind = block.get("type")
        if kind == "heading":
            text = str(block.get("text") or "").strip()
            if text and text not in seen_paragraphs:
                seen_paragraphs.add(text)
                paragraphs.append(text)
        elif kind == "paragraph":
            text = str(block.get("text") or "").strip()
            if text and text not in seen_paragraphs:
                seen_paragraphs.add(text)
                paragraphs.append(text)
        elif kind == "list":
            for value in block.get("items") or []:
                text = str(value or "").strip()
                if text and text not in seen_paragraphs:
                    seen_paragraphs.add(text)
                    paragraphs.append(text)
        elif kind == "image":
            image_url = str(block.get("url") or "").strip()
            key = image_url.split("?", 1)[0]
            if image_url and key not in seen_images:
                seen_images.add(key)
                images.append(image_url)

    canonical_title = article_title
    link = WIKIVOYAGE_BASE_URL + "/wiki/" + quote(
        canonical_title.replace(" ", "_"), safe="/()_',.:!-~"
    )
    article = {
        "title": canonical_title,
        "link": link,
        "paragraphs": paragraphs,
        "images": images,
        "blocks": blocks,
    }
    _wikivoyage_store_article(cache_key, article)
    return article


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


# ---- /mathtutor ------------------------------------------------------
# A mathtutor session owns the Word test papers the student uploaded and the
# practice problems generated from them. Everything persists on disk so the
# session survives a server restart:
#   .chat/mathtutor/uploaded_word_docs/<session>_<filename>
#   .chat/mathtutor/sessions/<unixtimestamp>.context.json
MATHUTOR_DIR = SCRIPT_DIR / ".chat" / "mathtutor"
MATHUTOR_UPLOAD_DIR = MATHUTOR_DIR / "uploaded_word_docs"
MATHUTOR_SESSIONS_DIR = MATHUTOR_DIR / "sessions"
MATHUTOR_LOCK = threading.RLock()
MATHUTOR_SESSION_ID_RE = re.compile(r"\d{10,16}")
MATHUTOR_MAX_FILES = 10
MATHUTOR_MAX_FILE_BYTES = 8 * 1024 * 1024
MATHUTOR_MAX_TOTAL_BYTES = 12 * 1024 * 1024
# The tutor generates practice problems five at a time. The upload creates the
# first five; the browser asks for another five when the student reaches the
# end of the current set (problem offset divisible by five).
MATHUTOR_PROBLEMS_PER_BATCH = 5
MATHUTOR_GENERATION_ATTEMPTS = 5
MATHUTOR_GENERATION_TOKENS = 5_000
MATHUTOR_SOURCE_CHAR_LIMIT = 16_000
# The mathtutor uses the same single local LFM3 model as every other path.
# Thinking stays disabled through chat.generate_text / stream_answer defaults.
MATHUTOR_MODEL = chat.CHAT_MODEL


def _docx_text_from_bytes(data: bytes) -> str:
    """Extract readable text from a .docx without requiring python-docx."""
    try:
        with zipfile.ZipFile(io.BytesIO(data)) as archive:
            try:
                document_xml = archive.read("word/document.xml")
            except KeyError as exc:
                raise ValueError(
                    "The Word document has no word/document.xml part"
                ) from exc
    except zipfile.BadZipFile as exc:
        raise ValueError("The .docx file is not a valid Word document") from exc

    try:
        root = ElementTree.fromstring(document_xml)
    except ElementTree.ParseError as exc:
        raise ValueError("The Word document XML could not be parsed") from exc

    parts: list[str] = []

    def walk(element: ElementTree.Element) -> None:
        tag = element.tag if isinstance(element.tag, str) else ""
        local = tag.rsplit("}", 1)[-1] if tag else ""
        if local == "p":
            if parts and not parts[-1].endswith("\n"):
                parts.append("\n")
            for child in element:
                walk(child)
            parts.append("\n")
            return
        if local == "t":
            parts.append(element.text or "")
            return
        if local == "tab":
            parts.append("\t")
            return
        if local in {"br", "cr"}:
            parts.append("\n")
            return
        for child in element:
            walk(child)

    walk(root)
    text = "".join(parts)
    text = re.sub(r"[ \t]+\n", "\n", text)
    text = re.sub(r"\n{3,}", "\n\n", text)
    return text.strip()


def _legacy_doc_text_from_bytes(data: bytes) -> str:
    """Use an installed converter for the old binary .doc format."""
    temp_path = ""
    try:
        with tempfile.NamedTemporaryFile(suffix=".doc", delete=False) as handle:
            handle.write(data)
            temp_path = handle.name
        commands = []
        textutil = shutil.which("textutil")
        if textutil:
            commands.append([textutil, "-convert", "txt", "-stdout", temp_path])
        antiword = shutil.which("antiword")
        if antiword:
            commands.append([antiword, temp_path])
        catdoc = shutil.which("catdoc")
        if catdoc:
            commands.append([catdoc, temp_path])
        errors: list[str] = []
        for command in commands:
            try:
                result = subprocess.run(
                    command,
                    check=True,
                    stdout=subprocess.PIPE,
                    stderr=subprocess.PIPE,
                    timeout=60,
                )
                text = result.stdout.decode("utf-8", errors="replace").strip()
                if text:
                    return text
            except (OSError, subprocess.SubprocessError) as exc:
                errors.append(f"{Path(command[0]).name}: {exc}")
        raise ValueError(
            "Could not read the .doc file. Save it as .docx and upload it again"
            + (f" ({'; '.join(errors)})" if errors else "")
        )
    finally:
        if temp_path:
            try:
                os.unlink(temp_path)
            except OSError:
                pass


def _mathtutor_extract_document_text(name: str, data: bytes) -> str:
    suffix = Path(name).suffix.lower()
    if suffix == ".docx" or data[:2] == b"PK":
        text = _docx_text_from_bytes(data)
    elif suffix == ".doc":
        text = _legacy_doc_text_from_bytes(data)
    else:
        raise ValueError(
            f"Unsupported file type {suffix or '(none)'}; upload .docx or .doc"
        )
    text = re.sub(r"\n{3,}", "\n\n", text).strip()
    if not text:
        raise ValueError(f"No readable text was found in {name}")
    return text


def _mathtutor_session_path(session_id: str) -> Path:
    if not MATHUTOR_SESSION_ID_RE.fullmatch(str(session_id or "")):
        raise ValueError("Invalid mathtutor session id")
    return MATHUTOR_SESSIONS_DIR / f"{session_id}.context.json"


def _mathtutor_public_session(session: dict) -> dict:
    """Return the browser-safe view (reference answers stay server-side)."""
    documents = []
    for document in session.get("documents", []):
        documents.append(
            {
                "name": str(document.get("name", "")),
                "bytes": int(document.get("bytes", 0) or 0),
                "characters": int(document.get("characters", 0) or 0),
            }
        )
    problems = []
    for problem in session.get("problems", []):
        problems.append(
            {
                "id": int(problem.get("id", 0) or 0),
                "topic": str(problem.get("topic", "")),
                "question": str(problem.get("question", "")),
            }
        )
    return {
        "id": str(session.get("id", "")),
        "created_at": float(session.get("created_at", 0) or 0),
        "updated_at": float(session.get("updated_at", 0) or 0),
        "documents": documents,
        "problems": problems,
    }


def _mathtutor_load_session(session_id: str) -> dict:
    path = _mathtutor_session_path(session_id)
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except FileNotFoundError as exc:
        raise ValueError("Mathtutor session not found") from exc
    except (OSError, json.JSONDecodeError) as exc:
        raise ValueError(f"Could not read mathtutor session: {exc}") from exc
    if not isinstance(data, dict):
        raise ValueError("Malformed mathtutor session")
    data.setdefault("documents", [])
    data.setdefault("problems", [])
    data.setdefault("history", [])
    return data


def _mathtutor_latest_session() -> dict | None:
    try:
        paths = list(MATHUTOR_SESSIONS_DIR.glob("*.context.json"))
    except OSError:
        return None

    def sort_key(path: Path) -> int:
        stem = path.name.split(".", 1)[0]
        return int(stem) if stem.isdigit() else 0

    for path in sorted(paths, key=sort_key, reverse=True):
        session_id = path.name.split(".", 1)[0]
        try:
            return _mathtutor_load_session(session_id)
        except ValueError:
            continue
    return None


def _mathtutor_save_session(session: dict) -> None:
    MATHUTOR_SESSIONS_DIR.mkdir(parents=True, exist_ok=True)
    session["updated_at"] = time.time()
    path = _mathtutor_session_path(str(session.get("id", "")))
    temp_path = path.with_name(f".{path.name}.{os.getpid()}.tmp")
    try:
        temp_path.write_text(
            json.dumps(session, ensure_ascii=False, indent=2),
            encoding="utf-8",
        )
        os.replace(temp_path, path)
    finally:
        try:
            temp_path.unlink()
        except OSError:
            pass


def _mathtutor_source_text(session: dict, limit: int) -> str:
    chunks: list[str] = []
    remaining = limit
    for document in session.get("documents", []):
        if remaining <= 0:
            break
        text = str(document.get("text", "")).strip()
        if not text:
            continue
        name = str(document.get("name", "test paper"))
        header = f"### {name}\n"
        take = max(0, remaining - len(header))
        if take <= 0:
            break
        chunks.append(header + text[:take])
        remaining -= len(header) + min(len(text), take)
    return "\n\n".join(chunks)


def _mathtutor_parse_problems(raw: str, limit: int | None = None) -> list[dict]:
    """Parse the model's JSON problem list, tolerating prose around it."""
    text = re.sub(r"^\s*```(?:json)?\s*", "", str(raw or "").strip(), flags=re.I)
    text = re.sub(r"\s*```\s*$", "", text)
    candidates = [text]
    start = text.find("[")
    end = text.rfind("]")
    if start >= 0 and end > start:
        candidates.append(text[start : end + 1])
    start = text.find("{")
    end = text.rfind("}")
    if start >= 0 and end > start:
        candidates.append(text[start : end + 1])

    payload = None
    for candidate in candidates:
        try:
            payload = json.loads(candidate)
            break
        except json.JSONDecodeError:
            continue

    items: list = []
    if isinstance(payload, dict):
        payload = payload.get("problems") or payload.get("questions") or []
    if isinstance(payload, list):
        items = payload
    if not items:
        # Recover complete objects from a truncated array (common when the
        # model is asked for many problems). raw_decode keeps nested LaTeX
        # braces inside strings intact.
        decoder = json.JSONDecoder()
        index = 0
        while True:
            start = text.find("{", index)
            if start < 0:
                break
            try:
                item, end = decoder.raw_decode(text, start)
            except json.JSONDecodeError:
                index = start + 1
                continue
            if isinstance(item, dict):
                items.append(item)
            index = end

    problems: list[dict] = []
    seen: set[str] = set()
    for item in items:
        if not isinstance(item, dict):
            continue
        question = str(item.get("question") or "").strip()
        answer = str(item.get("answer") or "").strip()
        topic = str(item.get("topic") or "Practice").strip()
        if not question or not answer or question in seen:
            continue
        seen.add(question)
        problems.append(
            {
                "id": len(problems) + 1,
                "topic": topic or "Practice",
                "question": question,
                "answer": answer,
            }
        )
        if limit is not None and len(problems) >= limit:
            break
    if not problems:
        raise ValueError("The model returned no usable practice problems")
    for index, problem in enumerate(problems, start=1):
        problem["id"] = index
    return problems


def _mathtutor_generate_problems(
    session: dict,
    count: int = MATHUTOR_PROBLEMS_PER_BATCH,
    start_id: int = 1,
) -> list[dict]:
    source = _mathtutor_source_text(session, MATHUTOR_SOURCE_CHAR_LIMIT)
    if not source:
        raise ValueError("The uploaded test papers contain no readable text")

    problems: list[dict] = []
    # Avoid questions already in the session as well as ones generated in this
    # batch, so repeated "Next question" requests never duplicate problems.
    seen: set[str] = {
        str(problem.get("question", ""))
        for problem in session.get("problems", [])
        if isinstance(problem, dict)
    }
    last_error = ""
    attempts = 0
    while len(problems) < count and attempts < MATHUTOR_GENERATION_ATTEMPTS:
        attempts += 1
        remaining = count - len(problems)
        batch_size = remaining
        avoid_block = ""
        existing = [
            problem
            for problem in session.get("problems", [])
            if isinstance(problem, dict)
        ] + problems
        if existing:
            avoid_list = "\n".join(
                f"- {problem.get('question', '')}" for problem in existing
            )
            avoid_block = (
                "\nDo NOT repeat or paraphrase any of these already generated problems:\n"
                f"{avoid_list}\n"
            )
        prompt = f"""You are an expert math teacher. The student uploaded test papers from their course.
Study the papers and create exactly {batch_size} NEW practice problems that test the same skills, methods, and difficulty level. Do not copy a problem verbatim.
{avoid_block}
<test_papers>
{source}
</test_papers>

Return ONLY a valid JSON array containing all {batch_size} problems (no prose, no markdown fences). Each element must be an object with exactly these string fields:
  "topic": a short topic label,
  "question": the complete practice problem, including all numbers and wording,
  "answer": a complete worked reference answer with the final result.
Use LaTeX inside $...$ for math when helpful.
"""
        for generation_prompt, temperature in (
            (prompt, 0.35),
            (prompt + "\nIMPORTANT: output JSON only.", 0.1),
        ):
            try:
                raw = chat.generate_text(
                    generation_prompt,
                    model=MATHUTOR_MODEL,
                    temperature=temperature,
                    num_predict=MATHUTOR_GENERATION_TOKENS,
                    num_ctx=chat.ANSWER_NUM_CTX,
                )
            except RuntimeError as exc:
                last_error = str(exc)
                break
            try:
                batch = _mathtutor_parse_problems(raw, limit=batch_size)
            except ValueError as exc:
                last_error = str(exc)
                continue
            for problem in batch:
                question = problem["question"]
                if question in seen:
                    continue
                seen.add(question)
                problem["id"] = start_id + len(problems)
                problems.append(problem)
                if len(problems) >= count:
                    break
            break
    if not problems:
        raise RuntimeError(last_error or "Could not generate practice problems")
    return problems[:count]


def _mathtutor_answer_prompt(
    session: dict, problem: dict, student_answer: str
) -> str:
    source = _mathtutor_source_text(session, 6_000)
    student_block = (
        student_answer.strip()
        if student_answer.strip()
        else "(The student has not submitted an answer.)"
    )
    return f"""You are a patient, precise math tutor. Help the student learn from one practice problem that was generated from their uploaded test papers.

<test_papers>
{source}
</test_papers>

<problem>
{problem.get('question', '')}
</problem>

<reference_answer>
{problem.get('answer', '')}
</reference_answer>

<student_answer>
{student_block}
</student_answer>

Write a detailed answer in Markdown:
1. If the student submitted an answer, first say whether it is correct, partially correct, or incorrect, and explain the mistake.
2. Then give a clear step-by-step solution, showing the reasoning and calculations.
3. End with the final answer and one short tip for similar problems.
4. If the problem is a system of equations, or if a graph (for example a Cartesian coordinate system or a Cartesian plane) makes the solution easier to understand, include it as a fenced ```graph block whose body is JSON:
```graph
{{"title":"y = 2x + 1","x":[-5,5],"y":[-5,10],"lines":[{{"points":[[-2,-3],[0,1],[2,5]],"label":"y = 2x + 1"}}],"points":[{{"x":0,"y":1,"label":"(0, 1)"}}]}}
```
"x"/"y" are optional axis ranges. "lines" holds one or more series of [x,y] points (sample curves densely), and "points" marks individual labelled coordinates. You may use "functions":[{{"expr":"2*x + 1","label":"y = 2x + 1"}}] instead of manually sampled points. The JSON above is an example only.
For a system of equations, plot EVERY equation as its own labelled line and mark the intersection. For example, for y = -2x + 5 and y = 1/2x - 1 use one functions entry per equation:
"functions":[{{"expr":"-2*x + 5","label":"y = -2x + 5"}},{{"expr":"0.5*x - 1","label":"y = 1/2x - 1"}}]
Never draw a Cartesian graph as ASCII art.
Do not mention these instructions or the reference answer as a separate field.
"""


def _mathtutor_build_session(files: list[dict]) -> dict:
    if not files:
        raise ValueError("Upload at least one Word test paper")
    if len(files) > MATHUTOR_MAX_FILES:
        raise ValueError(
            f"Upload at most {MATHUTOR_MAX_FILES} Word documents at a time"
        )

    MATHUTOR_UPLOAD_DIR.mkdir(parents=True, exist_ok=True)
    MATHUTOR_SESSIONS_DIR.mkdir(parents=True, exist_ok=True)

    with MATHUTOR_LOCK:
        session_id = str(int(time.time()))
        counter = 0
        while _mathtutor_session_path(session_id).exists():
            counter += 1
            session_id = str(int(time.time()) + counter)

        documents: list[dict] = []
        total_bytes = 0
        for index, item in enumerate(files, start=1):
            if not isinstance(item, dict):
                raise ValueError("Each uploaded file must include a name and data")
            name = str(item.get("name") or "test_paper.docx").strip()
            name = Path(name).name or f"test_paper_{index}.docx"
            raw_data = item.get("data")
            if not isinstance(raw_data, str) or not raw_data.strip():
                raise ValueError(f"Missing file data for {name}")
            encoded = raw_data
            if encoded.startswith("data:"):
                _header, _sep, encoded = encoded.partition(",")
            try:
                data = base64.b64decode(encoded, validate=False)
            except (ValueError, TypeError) as exc:
                raise ValueError(f"Could not decode {name}") from exc
            if len(data) > MATHUTOR_MAX_FILE_BYTES:
                raise ValueError(
                    f"{name} is larger than "
                    f"{MATHUTOR_MAX_FILE_BYTES // (1024 * 1024)} MB"
                )
            total_bytes += len(data)
            if total_bytes > MATHUTOR_MAX_TOTAL_BYTES:
                raise ValueError(
                    "The selected Word documents are too large in total "
                    f"(limit {MATHUTOR_MAX_TOTAL_BYTES // (1024 * 1024)} MB)"
                )
            text = _mathtutor_extract_document_text(name, data)

            safe_name = re.sub(r"[^A-Za-z0-9._-]+", "_", name)
            stored_name = f"{session_id}_{index}_{safe_name}"
            stored_path = MATHUTOR_UPLOAD_DIR / stored_name
            stored_path.write_bytes(data)
            documents.append(
                {
                    "name": name,
                    "stored_name": stored_name,
                    "path": str(stored_path),
                    "bytes": len(data),
                    "characters": len(text),
                    "text": text,
                }
            )

        session = {
            "id": session_id,
            "created_at": time.time(),
            "updated_at": time.time(),
            "documents": documents,
            "problems": [],
            "history": [],
        }
        try:
            session["problems"] = _mathtutor_generate_problems(session)
        except Exception:
            # Do not leave uploaded files behind when problem generation fails.
            for document in documents:
                try:
                    Path(str(document.get("path", ""))).unlink()
                except OSError:
                    pass
            raise
        _mathtutor_save_session(session)
        return session


def _mathtutor_append_history(
    session: dict, problem: dict, student_answer: str, model_answer: str
) -> None:
    history = session.setdefault("history", [])
    history.append(
        {
            "problem_id": int(problem.get("id", 0) or 0),
            "question": str(problem.get("question", "")),
            "student_answer": student_answer,
            "model_answer": model_answer,
            "at": time.time(),
        }
    )
    del history[:-100]
    _mathtutor_save_session(session)


COMMAND_ARGUMENTS = {
    "/youtube",
    "/ask",
    "/wiki",
    "/wiki_article",
    "/article",
    "/dict",
    "/depth",
    "/benchmark",
    "/news",
    "/mdn",
    "/googlenews",
    "/howstuffworks",
    "/medium",
    "/gutenberg",
    "/openlibrary",
}

# Subcommands grouped under /gutenberg and /openlibrary.
GUTENBERG_SUBCOMMANDS = {
    "search": "search Project Gutenberg by title, author, or keyword",
}

OPENLIBRARY_SUBCOMMANDS = {
    "search": "search inside Open Library books by keyword",
}

# Project Gutenberg search and per-book chat. /gutenberg search mirrors the
# /news search flow: it asks news_fetch.php for a Google News RSS search
# restricted to site:gutenberg.org, resolves each Google News redirect to its
# gutenberg.org page, and turns the matching ebook links into book cards. The
# Open Library search API and the Project Gutenberg catalogue search page stay
# as a fallback when the news feed has no usable book links. The chosen
# plaintext is downloaded once and cached under .chat. Book chat reuses the
# /article section-aware RAG pipeline, so a multi-megabyte novel never goes
# into the model's context at once.
OPENLIBRARY_SEARCH_URL = "https://openlibrary.org/search.json"
OPENLIBRARY_INSIDE_SEARCH_URL = "https://openlibrary.org/search/inside.json"
OPENLIBRARY_BOOK_PAGE_URL = "https://openlibrary.org{key}"
OPENLIBRARY_WORK_JSON_URL = "https://openlibrary.org{key}.json"
OPENLIBRARY_SEARCH_FIELDS = (
    "key,title,author_name,first_publish_year,language,subject,"
    "id_project_gutenberg,cover_i,cover_edition_key,edition_count,ebook_access"
)
OPENLIBRARY_RESULT_LIMIT = 20
OPENLIBRARY_METADATA_SEARCH_LIMIT = 60
OPENLIBRARY_HIGHLIGHT_LIMIT = 3
OPENLIBRARY_DESCRIPTION_LIMIT = 520
OPENLIBRARY_DESCRIPTION_CACHE: dict[str, str] = {}
OPENLIBRARY_DESCRIPTION_CACHE_LOCK = threading.Lock()
GUTENBERG_SEARCH_URL = "https://www.gutenberg.org/ebooks/search/"
GUTENBERG_SEARCH_BLOCK_RE = re.compile(
    r'<li class="booklink">(.*?)</li>', re.IGNORECASE | re.DOTALL
)
GUTENBERG_SEARCH_LINK_RE = re.compile(
    r'<a class="link" href="/ebooks/(\d+)"', re.IGNORECASE
)
GUTENBERG_SEARCH_TITLE_RE = re.compile(
    r'<span class="title">(.*?)</span>', re.IGNORECASE | re.DOTALL
)
GUTENBERG_SEARCH_AUTHOR_RE = re.compile(
    r'<span class="subtitle">(.*?)</span>', re.IGNORECASE | re.DOTALL
)
GUTENBERG_SEARCH_EXTRA_RE = re.compile(
    r'<span class="extra">(.*?)</span>', re.IGNORECASE | re.DOTALL
)
GUTENBERG_SEARCH_DOWNLOADS_RE = re.compile(r"([\d,]+)\s+downloads", re.IGNORECASE)
GUTENBERG_NEWS_SITE_QUERY = "site:gutenberg.org"
GUTENBERG_NEWS_BOOK_LINK_RE = re.compile(
    r"gutenberg\.org/(?:"
    r"ebooks/(?P<ebook>\d+)/?(?:[?#]|$)"
    r"|cache/epub/(?P<cache>\d+)/"
    r"|files/(?P<files>\d+)/"
    r")",
    re.IGNORECASE,
)
GUTENBERG_NEWS_TITLE_SUFFIX_RE = re.compile(
    r"\s*[-–—|]\s*Project Gutenberg\s*$", re.IGNORECASE
)
GUTENBERG_NEWS_BY_RE = re.compile(
    r"^(?P<title>.+)\s+by\s+(?P<authors>.+)$", re.IGNORECASE
)
GUTENBERG_BOOK_URL = "https://www.gutenberg.org/ebooks/{book_id}"
GUTENBERG_BOOK_CACHE_DIR = SCRIPT_DIR / ".chat" / "gutenberg_cache"
GUTENBERG_BOOK_RESULT_LIMIT = 12
GUTENBERG_BOOK_STATE_LIMIT = 3
GUTENBERG_TEXT_MAX_BYTES = 30 * 1024 * 1024
GUTENBERG_START_MARKER_RE = re.compile(
    r"\*\*\*\s*START OF (?:THE|THIS) PROJECT GUTENBERG EBOOK.*?\*\*\*",
    re.IGNORECASE | re.DOTALL,
)
GUTENBERG_END_MARKER_RE = re.compile(
    r"\*\*\*\s*END OF (?:THE|THIS) PROJECT GUTENBERG EBOOK.*?\*\*\*",
    re.IGNORECASE | re.DOTALL,
)
GUTENBERG_OLD_END_RE = re.compile(
    r"\n\s*End of (?:the )?Project Gutenberg(?:'s)?.*\Z",
    re.IGNORECASE | re.DOTALL,
)
GUTENBERG_CHAPTER_RE = re.compile(
    r"^\s*(?:CHAPTER|BOOK|PART|VOLUME|ACT|SCENE)\s+"
    r"(?:[IVXLCDM]+|\d+)\b[^\n]*$",
    re.IGNORECASE,
)
GUTENBERG_BOOK_ID_RE = re.compile(r"\d{1,8}")

# Subcommands grouped under /youtube. Kept in display order for Tab
# completion and the /help panel.
YOUTUBE_SUBCOMMANDS = {
    "learn": "fetch one transcript and chunk it into RAG trunks",
    "learn_topic": "search YouTube for <topic> and learn the top 3 videos",
    "learn_playlist": "learn every video in a YouTube playlist",
    "search": "load the YouTube search-results page for <keyword> and show matching videos with channel, duration, and publish date",
    "transcript": "use one transcript by hash/URL, or search titles with ag",
    "search_transcript": "search transcript titles with ag and pick a video",
    "bookmark": "list bookmarked videos, latest first: /youtube bookmark list",
}

# /youtube search shows six cards at a time. The "More" button below the
# cards asks /api/youtube_more for the next six from the same cached search.
# Search itself reads YouTube's own results page, so the cards also carry the
# channel, duration, and relative publish date.
YOUTUBE_SEARCH_PAGE_SIZE = 6
# One YouTube results page normally contains about twenty videoRenderer
# entries.  Fetching all of them at once would be slower than painting the
# first six, so they are cached and revealed by the "More" button.
YOUTUBE_SEARCH_PAGE_RESULTS = 20
# Cache at most this many results per keyword, even after many "More" clicks.
YOUTUBE_SEARCH_MAX_RESULTS = YOUTUBE_SEARCH_PAGE_RESULTS
# After the first page is painted, quietly fill the cache with the remaining
# videos from the results page.  The "More" button then reads from memory
# instead of paying for another search.
YOUTUBE_SEARCH_PREFETCH_RESULTS = YOUTUBE_SEARCH_PAGE_RESULTS
# How long a "More" request will wait for that prefetch before doing its own
# search.  A prefetch normally finishes in a few seconds.
YOUTUBE_PREFETCH_WAIT_SECONDS = 8.0

# The browser's "中文" button translates a finished answer paragraph by
# paragraph. The single chat model preserves technical terminology and names.
ANSWER_TRANSLATION_MODEL = chat.CHAT_MODEL
ANSWER_TRANSLATION_NUM_CTX = chat.ANSWER_NUM_CTX
MAX_TRANSLATION_CHARS = 200_000
TRANSLATION_PARAGRAPH_PATTERN = re.compile(r"\n[ \t]*\n+")
TRANSLATION_LABEL_PATTERN = re.compile(
    r"^(?:\*\*)?(?:translation|english|simplified chinese|中文|简体中文)"
    r"(?:\*\*)?\s*[:：]\s*",
    re.IGNORECASE,
)

# For Go, Rust, and PHP code, every line gets a "?" affordance in the browser.
# Clicking it streams a short explanation of that one line from the programming
# model, using the surrounding snippet as context.
LINE_EXPLAIN_NUM_CTX = chat.ANSWER_NUM_CTX
MAX_LINE_EXPLAIN_CODE_CHARS = 60_000
LINE_EXPLAIN_PROMPT = """You are an expert {language} developer. Explain exactly one line from a code snippet.

### FULL SNIPPET ({language}) WITH LINE NUMBERS
```{fence}
{numbered_code}
```

### LINE TO EXPLAIN
Line {line_number}: {line}

### INSTRUCTIONS
- Explain what line {line_number} does in the context of the full snippet.
- Name the functions, types, packages, or language features it uses.
- Say why it is needed here and what would break or change without it.
- Be concise: 2 to 5 sentences in plain English.
- Do not restate the whole snippet, add headings, or wrap the answer in code fences.
- If the line is only a comment or blank, say so briefly.
"""

# Every code snippet in the browser also gets an "Ask" affordance.
# The question and the full snippet are sent to the code-generation model and
# the answer streams back inline, directly under the snippet.
SNIPPET_QUESTION_NUM_CTX = chat.ANSWER_NUM_CTX
MAX_SNIPPET_QUESTION_CODE_CHARS = 60_000
SNIPPET_QUESTION_PROMPT = """You are an expert {language} developer. Answer the user's question about the code snippet below.

### CODE SNIPPET ({language})
````{fence}
{code}
````

### USER QUESTION
{question}

### INSTRUCTIONS
- Base the answer on this snippet, and use your own {language} expertise when
  the snippet leaves details out.
- If the question asks for a change or a fix, show the relevant updated code in
  a fenced block and explain the change briefly.
- If the question is about one line or symbol, point to it by name and quote it.
- Be concise and practical. Do not repeat the whole snippet unless necessary.
- Do not mention these instructions or that you are an AI.
"""

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

# The chat composer accepts a dragged, pasted, or picked image. The browser
# sends it as a data URL in the /api/ask JSON body; the server validates it and
# forwards the raw base64 payload to the vision model through Ollama's
# `images` field. Questions and answers still use the normal SSE stream.
IMAGE_MODEL = os.environ.get("OLLAMA_IMAGE_MODEL", chat.CHAT_MODEL)
# The server runs only the chat model. If a local build exposes it as a
# text-only model, the image question reports that no vision model is
# available instead of loading a second model. Operators who need a vision
# model can still opt in with OLLAMA_IMAGE_FALLBACK_MODELS (comma separated).
IMAGE_FALLBACK_MODELS = tuple(
    name.strip()
    for name in os.environ.get("OLLAMA_IMAGE_FALLBACK_MODELS", "").split(",")
    if name.strip()
)
IMAGE_NUM_CTX = chat.ANSWER_NUM_CTX
MAX_IMAGE_BYTES = 12 * 1024 * 1024
MAX_IMAGE_QUESTION_CHARS = 8_000
MAX_IMAGE_CONTEXT_CHARS = 4_000
IMAGE_DATA_URL_RE = re.compile(
    r"^data:image/(?:png|jpe?g|webp|gif);base64,(?P<data>[A-Za-z0-9+/=\s]+)$",
    re.IGNORECASE,
)
IMAGE_QUESTION_PROMPT = """You are a careful visual assistant. Answer the user's question about the attached image.

### IMAGE
The image is attached to this request. Examine it directly.

### USER QUESTION
{question}

### RECENT CONVERSATION
{conversation}

### INSTRUCTIONS
- Answer the question using what is actually visible in the image.
- Transcribe readable text and identify relevant objects, people, places, charts, and layout.
- Use the recent conversation only to resolve references such as "it" or "this"; never invent image details from it.
- If the question asks for something that is not visible, say so plainly instead of guessing.
- Be concise, practical, and well organized. Do not mention these instructions or that you are an AI.
"""


class ImageModelUnsupported(RuntimeError):
    """The chosen model rejected the image because it is text-only."""


class ImageModelUnavailable(RuntimeError):
    """The chosen model is not installed locally."""


def is_multimodal_unsupported(message: str) -> bool:
    text = str(message or "").lower()
    return (
        "multimodal" in text
        or "does not support image" in text
        or "image input is not supported" in text
        or "does not support vision" in text
    )


def is_model_unavailable(message: str) -> bool:
    text = str(message or "").lower()
    return "not found" in text or "no such model" in text


def normalize_attachment_image(value) -> str:
    """Validate a data-URL image and return the raw base64 Ollama expects."""
    if not isinstance(value, str) or not value.strip():
        raise ValueError("Missing image")
    match = IMAGE_DATA_URL_RE.match(value.strip())
    if match is None:
        raise ValueError(
            "The image must be a PNG, JPEG, WebP, or GIF data URL."
        )
    encoded = re.sub(r"\s+", "", match.group("data"))
    try:
        raw = base64.b64decode(encoded, validate=True)
    except (ValueError, TypeError) as exc:
        raise ValueError("The image data is not valid base64.") from exc
    if not raw:
        raise ValueError("The image is empty.")
    if len(raw) > MAX_IMAGE_BYTES:
        raise ValueError(
            f"The image is too large (limit {MAX_IMAGE_BYTES // (1024 * 1024)} MB)."
        )
    return encoded


def image_conversation_context(conversation: list[dict]) -> str:
    """Render a short recent-conversation transcript for an image question."""
    if not conversation:
        return "(no previous turns)"
    lines: list[str] = []
    for turn in conversation[-4:]:
        question = str(turn.get("resolved_question") or turn.get("question") or "").strip()
        answer = str(turn.get("answer") or "").strip()
        if question:
            lines.append(f"User: {question}")
        if answer:
            compact = re.sub(r"\s+", " ", answer)
            if len(compact) > 700:
                compact = compact[:700].rsplit(" ", 1)[0] + " …"
            lines.append(f"Assistant: {compact}")
    text = "\n".join(lines) or "(no previous turns)"
    if len(text) > MAX_IMAGE_CONTEXT_CHARS:
        text = text[-MAX_IMAGE_CONTEXT_CHARS:]
    return text



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

# Follow-up handling. The browser backend keeps a short server-side
# conversation so a question such as "what does he think about AI?" or
# "when does it open" can be rewritten into a standalone retrieval question
# before BM25 runs. A completely unrelated question starts a fresh context.
CONVERSATION_MAX_TURNS = 8
CONVERSATION_ANSWER_CHARS = 900
# One model handles follow-up rewriting as well; keeping it identical to the
# answer model avoids loading a second model for a short classification call.
CONVERSATION_MODEL = chat.CHAT_MODEL

# Ollama unloads a model after OLLAMA_KEEP_ALIVE without traffic, which makes
# the first question after an idle gap pay the multi-second model load. A tiny
# request every few minutes refreshes that timer. Set
# OLLAMA_KEEPALIVE_WATCHDOG=0 to disable it, or OLLAMA_KEEPALIVE_INTERVAL to
# change the interval in seconds (minimum 60).
KEEPALIVE_WATCHDOG_ENABLED = os.environ.get(
    "OLLAMA_KEEPALIVE_WATCHDOG", "1"
).strip().lower() not in {"0", "false", "no", "off"}
try:
    KEEPALIVE_WATCHDOG_INTERVAL_SECONDS = max(
        60, int(os.environ.get("OLLAMA_KEEPALIVE_INTERVAL", "300"))
    )
except ValueError:
    KEEPALIVE_WATCHDOG_INTERVAL_SECONDS = 300

# Code-generation uses the same single local LFM3 model as chat, per the
# single-model requirement. Every code snippet the browser produces uses it.
CODE_GENERATION_MODEL = chat.CHAT_MODEL

# For Go, Rust, and PHP, every chat answer and every generated code sample
# uses the dedicated programming model instead of the general transcript chat
# model. Keeping it in one constant makes the model visible in the status bar,
# the benchmark line, and the answer-source badges.
PROGRAMMING_MODEL = CODE_GENERATION_MODEL

# Coding mode. Questions that ask for code samples about a programming
# language feature, structure, or architecture are answered by the dedicated
# code-generation model without its reasoning (thinking) mode. The regular
# chat model remains the default for everything else.
CODING_MODE_MODEL = CODE_GENERATION_MODEL
CODING_MODE_PROMPT_PREFIX = """### CODING MODE (highest priority)
The user is asking for a code sample about a programming-language feature,
structure, or architecture. Answer as an experienced software engineer.
- You MAY use your own programming knowledge in addition to the retrieved
  context. If the context is irrelevant or incomplete, do not refuse; still
  provide a correct, self-contained example.
- This overrides the retrieval template's "Missing Data" rule: when the
  user asks for code, never answer that the provided context is insufficient.
  Generate the code from your own knowledge and use whatever excerpts are
  available as hints.
- Show complete, runnable code in fenced blocks with the language tag
  (for example ```python).
- Briefly explain the key idea, then the code, then any complexity or
  trade-offs that matter.
- Do not invent citations or mention chunk IDs.

"""

# GitHub code fallback. When coding mode produces no usable answer, the
# server searches GitHub's public API and constructs an answer from two
# related code snippets. GitHub's code-search endpoint requires
# authentication; when GITHUB_TOKEN/GH_TOKEN is not set, the fallback searches
# repositories and reads source files through the unauthenticated core API.
GITHUB_API_BASE = "https://api.github.com"
GITHUB_RAW_BASE = "https://raw.githubusercontent.com"
GITHUB_TOKEN = (
    os.environ.get("GITHUB_TOKEN") or os.environ.get("GH_TOKEN") or ""
).strip()
GITHUB_HEADERS = {
    "Accept": "application/vnd.github+json",
    "User-Agent": "youtube-local-chat/1.0",
    "X-GitHub-Api-Version": "2022-11-28",
}
GITHUB_TIMEOUT = (4, 12)
GITHUB_TOTAL_TIMEOUT = 25.0
GITHUB_SNIPPET_LIMIT = 2
GITHUB_SNIPPET_MAX_LINES = 80
GITHUB_SNIPPET_MAX_CHARS = 6000
GITHUB_REPOSITORY_LIMIT = 6
GITHUB_MAX_QUERY_TERMS = 8
GITHUB_MAX_FILE_BYTES = 300_000

# Official documentation is used when a question names Go, Rust, or PHP.
# The language is detected from the question itself instead of a tag, so
# every question searches the one global corpus.
GO_LANGUAGE = "Go"
RUST_LANGUAGE = "Rust"
PHP_LANGUAGE = "PHP"
GOLANG_DOC_SEARCH_TIMEOUT = 900
RUST_DOC_SEARCH_TIMEOUT = 900
PHP_DOC_SEARCH_TIMEOUT = 900

# The browser shows a live "answer source" badge while an answer streams. For
# documentation-backed languages the answer starts in the local Ollama model
# and may then be replaced or extended by the official language documentation,
# so the server emits a "source" SSE event at each transition.
SOURCE_OLLAMA = "ollama"
SOURCE_DOCUMENTATION = "documentation"
SOURCE_MERGED = "merged"
DOCUMENTATION_LANGUAGES = {GO_LANGUAGE, RUST_LANGUAGE, PHP_LANGUAGE}

# Documentation-backed languages also pin the language of every generated code
# sample, even when the question does not name a language again.
LANGUAGE_CODE_FENCES = {
    GO_LANGUAGE: "go",
    RUST_LANGUAGE: "rust",
    PHP_LANGUAGE: "php",
}

# The programming_docs_map from ./prompts/programming.prompt. When a question
# is about a programming language or one of its features, the server loads
# related excerpts from that language's official documentation in addition to
# the transcript trunk context. Every entry names the documentation script
# whose cached index and chunker are reused, the reference URL to cite, and
# the local knowledge-note directory that supplements the docs.
PROGRAMMING_DOCS_MAP = {
    GO_LANGUAGE: {
        "reference": "https://go.dev/doc/",
        "source_label": "go.dev/doc",
        "module": "golang_doc_search",
        "knowledge_tag": "golang",
    },
    RUST_LANGUAGE: {
        "reference": "https://doc.rust-lang.org/reference",
        "source_label": "doc.rust-lang.org",
        "module": "rust_doc_search",
        "knowledge_tag": "rust",
    },
    PHP_LANGUAGE: {
        "reference": "https://www.php.net/manual/en/",
        "source_label": "www.php.net/manual/en",
        "module": "php_doc_search",
        "knowledge_tag": "php",
    },
}

# Bound the documentation that is folded into a normal chat prompt. The
# excerpts are a reference next to the trunks, not a replacement for them.
PROGRAMMING_DOCS_TOP_K = 6
PROGRAMMING_DOCS_CHUNKS_PER_PAGE = 2
PROGRAMMING_DOCS_MAX_CHARS = 6_000
_PROGRAMMING_DOCS_INDEX_CACHE: dict[str, tuple] = {}
_PROGRAMMING_DOCS_INDEX_LOCK = threading.Lock()


def normalize_language(language: str | None) -> str:
    """Return the canonical display name for a programming language."""
    raw = (language or "").strip()
    aliases = {
        "go": GO_LANGUAGE,
        "golang": GO_LANGUAGE,
        "rust": RUST_LANGUAGE,
        "php": PHP_LANGUAGE,
    }
    return aliases.get(raw.lower(), raw)


def language_uses_official_docs(language: str | None) -> bool:
    """Return True when code answers must come from official docs."""
    return normalize_language(language) in DOCUMENTATION_LANGUAGES


def language_chat_model(language: str | None) -> str:
    """Return the answer-generation model for ``language``."""
    return (
        PROGRAMMING_MODEL
        if language_uses_official_docs(language)
        else chat.CHAT_MODEL
    )


def language_coding_model(language: str | None) -> str:
    """Return the code-generation model for ``language``."""
    return (
        PROGRAMMING_MODEL
        if language_uses_official_docs(language)
        else CODING_MODE_MODEL
    )


def language_code_fence(language: str | None) -> str:
    """Return the Markdown fence language for ``language`` code samples."""
    return LANGUAGE_CODE_FENCES.get(normalize_language(language), "")


def language_coding_directive(language: str | None) -> str:
    """Build the language requirement for generated code."""
    language = normalize_language(language)
    if not language_uses_official_docs(language):
        return ""
    fence = language_code_fence(language) or language.lower()
    return f"""### LANGUAGE REQUIREMENT (active language: {language})
The user is working in {language}. Every code sample, snippet, command, and
API reference in the answer MUST be written in {language}.
- Use complete, idiomatic {language} and fence every code block as
  ```{fence}.
- If the question names another programming language or does not name any
  language, still answer in {language}; never emit code in another language.
- Use only the standard library and documented {language} APIs unless the
  retrieved context explicitly names a dependency.

"""


def coding_prompt_prefix(language: str | None) -> str:
    """Return the coding-mode prompt for the detected language."""
    return CODING_MODE_PROMPT_PREFIX + language_coding_directive(language)


def github_code_fallback_allowed(language: str | None) -> bool:
    """Return False when GitHub code search must not be used."""
    return not language_uses_official_docs(language)

# GitHub's search API takes a language name ("Python"), not the spelling used
# in a question ("python3"). Frameworks map to the language they are written
# in, and the extensions are used to pick source files in repository fallback.
GITHUB_LANGUAGE_MAP: dict[str, tuple[str, tuple[str, ...]]] = {
    "python": ("Python", (".py",)),
    "python3": ("Python", (".py",)),
    "javascript": ("JavaScript", (".js", ".jsx", ".mjs", ".cjs")),
    "typescript": ("TypeScript", (".ts", ".tsx")),
    "node": ("JavaScript", (".js", ".mjs", ".cjs")),
    "node.js": ("JavaScript", (".js", ".mjs", ".cjs")),
    "deno": ("TypeScript", (".ts", ".js")),
    "java": ("Java", (".java",)),
    "c++": ("C++", (".cpp", ".cc", ".cxx", ".hpp", ".h")),
    "cpp": ("C++", (".cpp", ".cc", ".cxx", ".hpp", ".h")),
    "c#": ("C#", (".cs",)),
    "csharp": ("C#", (".cs",)),
    ".net": ("C#", (".cs",)),
    "dotnet": ("C#", (".cs",)),
    "golang": ("Go", (".go",)),
    "rust": ("Rust", (".rs",)),
    "ruby": ("Ruby", (".rb",)),
    "php": ("PHP", (".php",)),
    "swift": ("Swift", (".swift",)),
    "kotlin": ("Kotlin", (".kt", ".kts")),
    "scala": ("Scala", (".scala",)),
    "haskell": ("Haskell", (".hs",)),
    "elixir": ("Elixir", (".ex", ".exs")),
    "erlang": ("Erlang", (".erl",)),
    "clojure": ("Clojure", (".clj",)),
    "lua": ("Lua", (".lua",)),
    "perl": ("Perl", (".pl", ".pm")),
    "objective-c": ("Objective-C", (".m", ".mm")),
    "dart": ("Dart", (".dart",)),
    "flutter": ("Dart", (".dart",)),
    "julia": ("Julia", (".jl",)),
    "fortran": ("Fortran", (".f90", ".f95", ".f")),
    "cobol": ("COBOL", (".cob", ".cbl")),
    "assembly": ("Assembly", (".asm", ".s")),
    "asm": ("Assembly", (".asm", ".s")),
    "sql": ("SQL", (".sql",)),
    "mysql": ("SQL", (".sql",)),
    "postgres": ("SQL", (".sql",)),
    "postgresql": ("SQL", (".sql",)),
    "sqlite": ("SQL", (".sql",)),
    "bash": ("Shell", (".sh", ".bash")),
    "shell": ("Shell", (".sh", ".bash")),
    "zsh": ("Shell", (".sh", ".zsh")),
    "powershell": ("PowerShell", (".ps1",)),
    "html": ("HTML", (".html", ".htm")),
    "css": ("CSS", (".css",)),
    "sass": ("CSS", (".scss", ".sass")),
    "less": ("CSS", (".less",)),
    "react": ("JavaScript", (".js", ".jsx", ".ts", ".tsx")),
    "reactjs": ("JavaScript", (".js", ".jsx", ".ts", ".tsx")),
    "vue": ("JavaScript", (".vue", ".js", ".ts")),
    "vuejs": ("JavaScript", (".vue", ".js", ".ts")),
    "angular": ("TypeScript", (".ts",)),
    "svelte": ("JavaScript", (".svelte", ".js", ".ts")),
    "next.js": ("JavaScript", (".js", ".jsx", ".ts", ".tsx")),
    "nuxt": ("JavaScript", (".js", ".ts", ".vue")),
    "django": ("Python", (".py",)),
    "flask": ("Python", (".py",)),
    "fastapi": ("Python", (".py",)),
    "rails": ("Ruby", (".rb",)),
    "laravel": ("PHP", (".php",)),
    "spring": ("Java", (".java",)),
    "express": ("JavaScript", (".js", ".ts")),
    "pandas": ("Python", (".py",)),
    "numpy": ("Python", (".py",)),
    "pytorch": ("Python", (".py",)),
    "tensorflow": ("Python", (".py",)),
    "jax": ("Python", (".py",)),
    "sklearn": ("Python", (".py",)),
    "scikit-learn": ("Python", (".py",)),
    "tailwind": ("CSS", (".css", ".html")),
    "tailwindcss": ("CSS", (".css", ".html")),
    "webpack": ("JavaScript", (".js",)),
    "vite": ("JavaScript", (".js", ".ts")),
    "babel": ("JavaScript", (".js",)),
    "jest": ("JavaScript", (".js", ".ts")),
    "pytest": ("Python", (".py",)),
    "android": ("Kotlin", (".kt", ".java")),
    "ios": ("Swift", (".swift",)),
    "unity": ("C#", (".cs",)),
    "unreal": ("C++", (".cpp", ".h")),
    "opengl": ("C++", (".cpp", ".h")),
    "vulkan": ("C++", (".cpp", ".h")),
    "webgl": ("JavaScript", (".js",)),
    "wasm": ("WebAssembly", (".wat", ".wasm")),
}
GITHUB_DEFAULT_EXTENSIONS = tuple(
    sorted(
        {
            extension
            for _language, extensions in GITHUB_LANGUAGE_MAP.values()
            for extension in extensions
        }
    )
)
# Filler and code-request words add no signal to a GitHub query. Technical
# nouns such as "class", "function", and "data structure" are deliberately
# kept because they are often the most useful search terms.
GITHUB_QUERY_STOPWORDS = frozenset(chat.FUZZY_ANCHOR_STOPWORDS) | frozenset(
    "i you he she it we they me him her us them my your his its our their "
    "code sample samples snippet snippets example examples demo demonstrate "
    "write show give provide generate create implement build debug refactor "
    "fix explain teach walkthrough walkthrough want need want like "
    "using use used implementation program script".split()
)
# The main language pattern uses \b, which cannot match names that end in a
# symbol ("C++" and "C#"), nor a leading dot (".NET"). This supplementary
# pattern keeps those common languages usable for GitHub queries.
GITHUB_EXTRA_LANGUAGE_PATTERN = re.compile(
    r"(?<![A-Za-z0-9_])(?:c\+\+|c#|\.net)(?![A-Za-z0-9_])",
    re.IGNORECASE,
)

# Words that name a programming language, runtime, or framework. A language
# mention alone is not enough: the question must also ask for code/design
# help or name a programming structure.
PROGRAMMING_LANGUAGE_PATTERN = re.compile(
    r"\b(?:"
    r"python|python3|javascript|typescript|node(?:\.js)?|deno|"
    r"java|c\+\+|cpp|c#|csharp|\.net|dotnet|golang|rust|ruby|"
    r"php|swift|kotlin|scala|haskell|elixir|erlang|clojure|lua|perl|"
    r"objective-c|dart|flutter|julia|fortran|cobol|assembly|asm|"
    r"sql|mysql|postgres(?:ql)?|sqlite|mongodb|redis|"
    r"bash|shell|zsh|powershell|html|css|sass|less|"
    r"react|reactjs|vue|vuejs|angular|svelte|next\.js|nuxt|"
    r"django|flask|fastapi|rails|laravel|spring|express|"
    r"graphql|grpc|docker|kubernetes|terraform|git|"
    r"pandas|numpy|pytorch|tensorflow|jax|sklearn|scikit-learn|"
    r"tailwind(?:css)?|webpack|vite|babel|jest|pytest|"
    r"android|ios|unity|unreal|opengl|vulkan|webgl|wasm"
    r")\b",
    re.IGNORECASE,
)

# Programming structures, algorithms, paradigms, and architecture terms that
# are commonly the subject of a code sample. These only trigger coding mode
# together with an explicit code request or a named programming language, so
# generic uses of words such as "stack" or "architecture" do not match.
PROGRAMMING_CONCEPT_PATTERN = re.compile(
    r"\b(?:"
    r"linked\s+list|doubly\s+linked\s+list|binary\s+(?:tree|search\s+tree)|"
    r"b-?tree|trie|hash\s+(?:map|table|set)|stack|queue|deque|heap|"
    r"priority\s+queue|graph(?:\s+(?:traversal|algorithm))?|"
    r"depth[-\s]first\s+search|breadth[-\s]first\s+search|dfs|bfs|"
    r"dynamic\s+programming|memoization|recursion|recursive|backtracking|"
    r"greedy\s+algorithm|sorting\s+algorithm|binary\s+search|"
    r"quicksort|quick\s+sort|merge\s+sort|heapsort|heap\s+sort|"
    r"bubble\s+sort|insertion\s+sort|selection\s+sort|"
    r"polymorphism|inheritance|encapsulation|abstraction|"
    r"time\s+complexity|space\s+complexity|big[-\s]?o|data\s+structure|"
    r"design\s+pattern|singleton|factory\s+pattern|observer\s+pattern|"
    r"decorator|dependency\s+injection|dependency\s+inversion|"
    r"factory|builder|adapter|strategy|repository\s+pattern|mvc|mvvm|"
    r"event\s+loop|closure|promise|async(?:hronous)?|await|callback|"
    r"coroutine|thread(?:ing|s)?|concurrency|mutex|semaphore|race\s+condition|"
    r"garbage\s+collection|memory\s+leak|pointer|reference\s+count|"
    r"unit\s+test|integration\s+test|test[-\s]driven|mock(?:ing)?|"
    r"regular\s+expression|regex|orm|crud|rest\s+api|api\s+endpoint|"
    r"microservice|monolith|serverless|event[-\s]driven|message\s+queue|"
    r"pub(?:lish)?/?sub(?:scribe)?|load\s+balanc(?:er|ing)|cach(?:e|ing)|"
    r"database\s+index|schema|migration|transaction|acid|"
    r"object[-\s]oriented|functional\s+programming|generics?|interface|"
    r"abstract\s+class|class|function|method|module|package|library|"
    r"framework|compiler|interpreter|virtual\s+machine|bytecode|"
    r"frontend|backend|full[-\s]stack|architecture|system\s+design|"
    r"scalab(?:le|ility)|distributed\s+system|consensus|raft|paxos"
    r")\b",
    re.IGNORECASE,
)

# Unambiguous programming concepts. A question such as "what is dependency
# injection?" is a coding question even without an explicit language, while a
# generic word such as "stack" or "architecture" needs a code request or a
# named language before coding mode is used.
STRONG_PROGRAMMING_CONCEPT_PATTERN = re.compile(
    r"\b(?:"
    r"linked\s+list|doubly\s+linked\s+list|binary\s+(?:tree|search\s+tree)|"
    r"b-?tree|trie|hash\s+(?:map|table|set)|priority\s+queue|deque|"
    r"depth[-\s]first\s+search|breadth[-\s]first\s+search|dfs|bfs|"
    r"dynamic\s+programming|memoization|recursion|recursive|backtracking|"
    r"greedy\s+algorithm|sorting\s+algorithm|binary\s+search|"
    r"quicksort|quick\s+sort|merge\s+sort|heapsort|heap\s+sort|"
    r"bubble\s+sort|insertion\s+sort|selection\s+sort|"
    r"polymorphism|inheritance|encapsulation|abstraction|"
    r"time\s+complexity|space\s+complexity|big[-\s]?o|data\s+structure|"
    r"design\s+pattern|singleton|factory\s+pattern|observer\s+pattern|"
    r"dependency\s+injection|dependency\s+inversion|"
    r"repository\s+pattern|mvc|mvvm|"
    r"event\s+loop|closure|promise|async(?:hronous)?|await|callback|"
    r"coroutine|mutex|semaphore|race\s+condition|"
    r"garbage\s+collection|memory\s+leak|reference\s+count|"
    r"unit\s+test|integration\s+test|test[-\s]driven|mock(?:ing)?|"
    r"regular\s+expression|regex|orm|crud|rest\s+api|api\s+endpoint|"
    r"microservice|monolith|serverless|event[-\s]driven|message\s+queue|"
    r"pub(?:lish)?/?sub(?:scribe)?|load\s+balanc(?:er|ing)|"
    r"database\s+index|acid|object[-\s]oriented|functional\s+programming|"
    r"generics?|compiler|interpreter|virtual\s+machine|bytecode|"
    r"system\s+design|distributed\s+system|consensus|raft|paxos"
    r")\b",
    re.IGNORECASE,
)

# Language features and mechanics that are the subject of a programming
# question even when the question does not ask for code. Together with the
# concept patterns above, this lets a follow-up such as "how does ownership
# work?" keep consulting the Rust documentation from the previous turn.
PROGRAMMING_FEATURE_PATTERN = re.compile(
    r"\b(?:"
    r"ownership|borrow(?:ing|ed|s)?|borrow\s+checker|lifetimes?|"
    r"trait(?:s)?|macros?|goroutines?|channels?|defer|generics?|"
    r"pattern\s+matching|type\s+inference|memory\s+safety|"
    r"move\s+semantics|smart\s+pointers?|destructors?|drop\s+check|"
    r"async\s+runtime|iterators?|structs?|enums?|methods?|"
    r"error\s+handling|exceptions?|reflection|metaprogramming|"
    r"operator\s+overloading|zero[-\s]cost|compile[-\s]time"
    r")\b",
    re.IGNORECASE,
)

# Opinions and recommendations about a technology are not code requests,
# even when they mention a programming concept.
CODING_OPINION_PATTERN = re.compile(
    r"\b(?:what\s+do\s+you\s+think|do\s+you\s+(?:like|prefer|recommend)|"
    r"should\s+(?:i|we)\s+(?:use|choose|learn)|is\s+(?:it|\w+)\s+(?:worth|better)|"
    r"pros\s+and\s+cons|versus|vs\.?|compare|recommend(?:ation)?|"
    r"your\s+opinion|opinions?\s+on)\b",
    re.IGNORECASE,
)

CHINESE_CODING_PATTERN = re.compile(
    r"(?:代码|示例|例子|代码片段|函数|类|实现|编程|程序|架构|算法|"
    r"数据结构|写一个|写个|如何实现|怎么做|用\s*\w+\s*(?:写|实现))"
)

# Explicit requests for code / implementation help. Kept separate from the
# concept list so an ordinary question containing "class" or "function" is
# not misclassified.
CODE_REQUEST_PATTERN = re.compile(
    r"(?:\b(?:show|write|give|provide|generate|create|implement|build|code|"
    r"debug|refactor|fix)\b.{0,60}\b(?:code|sample|snippet|example|function|"
    r"method|class|program|script|implementation|algorithm|data\s+structure|"
    r"query|api|endpoint|architecture|design)\b"
    r"|\b(?:code|sample|example|demo)\s+(?:code|sample|example|snippet|"
    r"implementation|program|for\s+(?:a|an|the))\b"
    r"|\bhow\s+(?:do|can|should|would|to)\s+(?:i|we|you)?\s*(?:implement|"
    r"build|write|code|create|structure|architect|design)\b"
    r"|\b(?:implement|implementing|code|program|architect)\s+(?:a|an|the|this|"
    r"that|my|our)\b"
    r"|\b(?:in|using|with)\s+(?:python|javascript|typescript|java|c\+\+|go|"
    r"rust|ruby|php|swift|kotlin|sql|bash|react|node(?:\.js)?)\b"
    r"|\b(?:explain|teach|demonstrate|walk\s+me\s+through)\b.{0,40}"
    r"\b(?:how\s+to|code|implementation|syntax|language\s+feature)\b"
    r")",
    re.IGNORECASE,
)

# Explanation questions about a language feature ("what is ownership in
# Rust?", "how does the borrow checker work?") are not code requests even
# though CODE_REQUEST_PATTERN's broad "in rust" alternative matches them. The
# server uses these patterns to keep such questions in the normal answer path,
# where the language documentation is combined with the transcript context.
PROGRAMMING_EXPLANATION_PATTERN = re.compile(
    r"^\s*(?:what\s+(?:is|are|does|do)|why\s+(?:is|are|does|do)|"
    r"how\s+(?:does|do|is|are)|explain|describe|tell\s+me\s+about|define|"
    r"definition\s+of|difference\s+between|when\s+(?:should|would|do)\s+"
    r"(?:i|we)\s+use|walk\s+me\s+through)\b",
    re.IGNORECASE,
)
EXPLICIT_CODE_REQUEST_PATTERN = re.compile(
    r"(?:\b(?:show|write|give|provide|generate|create|implement|build|code|"
    r"debug|refactor|fix)\b.{0,60}\b(?:code|sample|snippet|example|function|"
    r"method|class|program|script|implementation|algorithm|data\s+structure|"
    r"query|api|endpoint|architecture|design)\b"
    r"|\b(?:code|sample|example|demo)\s+(?:code|sample|example|snippet|"
    r"implementation|program|for\s+(?:a|an|the))\b"
    r"|\bhow\s+(?:do|can|should|would|to)\s+(?:i|we|you)?\s*(?:implement|"
    r"build|write|code|create|structure|architect|design)\b"
    r"|\b(?:implement|implementing|code|program|architect)\s+(?:a|an|the|this|"
    r"that|my|our)\b)",
    re.IGNORECASE,
)


def programming_explanation_question(question: str) -> bool:
    """Return True for an explanatory question about a language feature."""
    text = " ".join(str(question or "").split())
    return bool(text and PROGRAMMING_EXPLANATION_PATTERN.search(text))


def explicit_code_request(question: str) -> bool:
    """Return True when the question explicitly asks for code or a program."""
    text = " ".join(str(question or "").split())
    return bool(
        text
        and (
            EXPLICIT_CODE_REQUEST_PATTERN.search(text)
            or CHINESE_CODING_PATTERN.search(question)
        )
    )

# A pasted code snippet is itself a strong signal that the question is a
# coding question, even when it is phrased as "why does this fail?".
CODE_SYNTAX_PATTERN = re.compile(
    r"(?:^|\n)\s*(?:def|class|function|const|let|var|import|from|"
    r"#include|public|private|protected|static|void|int|SELECT|CREATE|"
    r"INSERT|UPDATE|fn|func|package|using|async|await)\b"
    r"|=>|::|\{\s*\n|;\s*$",
    re.MULTILINE,
)


def question_requests_coding(question: str) -> bool:
    """Return True when a question asks for a programming code sample.

    Detection is deliberately conservative: a named language is only enough
    when the question also asks for code/design help or names a programming
    concept. Explicit code requests, pasted code, and language+concept
    questions are all handled without an extra model call.
    """
    text = " ".join(str(question or "").split())
    if not text:
        return False
    if CHINESE_CODING_PATTERN.search(question):
        return True
    if CODE_SYNTAX_PATTERN.search(question):
        return True
    if CODE_REQUEST_PATTERN.search(text):
        return True
    has_language = bool(PROGRAMMING_LANGUAGE_PATTERN.search(text))
    has_concept = bool(PROGRAMMING_CONCEPT_PATTERN.search(text))
    has_strong_concept = bool(STRONG_PROGRAMMING_CONCEPT_PATTERN.search(text))
    is_opinion = bool(CODING_OPINION_PATTERN.search(text))
    if is_opinion:
        return False
    return has_strong_concept or (has_language and has_concept)


def github_query_terms(
    question: str,
) -> tuple[list[str], str | None, tuple[str, ...] | None]:
    """Return GitHub search terms, language, and source extensions.

    The language is inferred from the programming-language names already used
    by coding-mode detection. Frameworks map to their implementation language
    (React -> JavaScript, Django -> Python). The returned terms remove the
    question's filler words and the language name so the query stays focused.
    """
    text = " ".join(str(question or "").split())
    language: str | None = None
    extensions: tuple[str, ...] | None = None
    raw_language = ""
    language_matches = list(GITHUB_EXTRA_LANGUAGE_PATTERN.finditer(text))
    language_matches.extend(PROGRAMMING_LANGUAGE_PATTERN.finditer(text))
    for match in sorted(language_matches, key=lambda item: item.start()):
        token = match.group(0).lower()
        mapped = GITHUB_LANGUAGE_MAP.get(token)
        if mapped is not None:
            language, extensions = mapped
            raw_language = token
            break

    language_tokens = set(chat.tokenize(raw_language))
    terms: list[str] = []
    for token in chat.tokenize(text):
        if len(token) < 2 or token.isdigit():
            continue
        if token in GITHUB_QUERY_STOPWORDS or token in language_tokens:
            continue
        if token not in terms:
            terms.append(token)
    return terms[:GITHUB_MAX_QUERY_TERMS], language, extensions


def detect_code_language(question: str, fallback: str = "") -> str:
    """Return the programming language named by a question, if any.

    The GitHub language map already recognizes Rust, PHP, and "golang" as
    well as languages that are not documentation-backed. Only Go, Rust, and
    PHP are returned because those are the languages with an official-docs
    answer path. ``fallback`` keeps the previous language for a follow-up
    such as "now add error handling" when the new question does not name one.
    """
    language = detect_explicit_language(question)
    if language:
        return language if language in DOCUMENTATION_LANGUAGES else ""
    return fallback if fallback in DOCUMENTATION_LANGUAGES else ""


def detect_explicit_language(question: str) -> str:
    """Return any programming language explicitly named by a question.

    Unlike :func:`detect_code_language`, this also returns languages such as
    Python or JavaScript that have no official-docs integration. Callers use
    that to avoid treating a follow-up in a previous Rust conversation as a
    Rust question when the new question clearly switches to another language.
    """
    text = " ".join(str(question or "").split())
    _terms, language, _extensions = github_query_terms(text)
    if language:
        return language
    if re.search(
        r"\b(?:in|with|using)\s+go\b|\bgo\s+(?:program|code|script|cli|"
        r"function|package|module|programming)\b",
        text,
        re.IGNORECASE,
    ):
        return GO_LANGUAGE
    return ""


def programming_feature_question(question: str) -> bool:
    """Return True when a question is about a language or language feature.

    This is broader than coding-mode detection: a question such as "how does
    ownership work?" asks about a Rust feature without requesting code. It is
    used only to decide whether a previously mentioned language should still
    supply its official documentation context.
    """
    text = " ".join(str(question or "").split())
    if not text:
        return False
    return bool(
        question_requests_coding(question)
        or CODE_REQUEST_PATTERN.search(text)
        or CHINESE_CODING_PATTERN.search(question)
        or PROGRAMMING_CONCEPT_PATTERN.search(text)
        or STRONG_PROGRAMMING_CONCEPT_PATTERN.search(text)
        or PROGRAMMING_FEATURE_PATTERN.search(text)
    )


def programming_docs_language(question: str, fallback: str = "") -> str:
    """Return the documentation-backed language for a language question.

    An explicitly named language wins. When the question does not name one,
    the previous programming language is reused only when the new question is
    clearly about a language or one of its features. This prevents an
    unrelated question from inheriting stale documentation context.
    """
    explicit = normalize_language(detect_explicit_language(question))
    if explicit:
        return explicit if explicit in PROGRAMMING_DOCS_MAP else ""
    if not programming_feature_question(question):
        return ""
    previous = normalize_language(fallback)
    return previous if previous in PROGRAMMING_DOCS_MAP else ""


def _github_headers(text_match: bool = False) -> dict[str, str]:
    headers = dict(GITHUB_HEADERS)
    if text_match:
        # Ask the code-search endpoint for the matched fragments, not just
        # file paths. Without this header every result would need a second
        # request to download the file.
        headers["Accept"] = "application/vnd.github.text-match+json"
    if GITHUB_TOKEN:
        headers["Authorization"] = f"Bearer {GITHUB_TOKEN}"
    return headers


def _github_snippet_language(path: str) -> str:
    suffix = Path(path).suffix.lower()
    for language, extensions in GITHUB_LANGUAGE_MAP.values():
        if suffix in extensions:
            return language
    return ""


def _github_trim_snippet(text: str, terms: list[str]) -> str:
    """Trim a source file or search fragment down to a readable snippet."""
    normalized = str(text or "").replace("\r\n", "\n").replace("\r", "\n")
    if not normalized.strip():
        return ""
    all_lines = normalized.split("\n")
    if len(all_lines) > GITHUB_SNIPPET_MAX_LINES:
        lowered = [line.lower() for line in all_lines]
        anchor: int | None = None
        for term in terms:
            needle = term.lower()
            if not needle:
                continue
            for index, line in enumerate(lowered):
                if needle in line:
                    anchor = index
                    break
            if anchor is not None:
                break
        start = max(0, (anchor if anchor is not None else 0) - 6)
        # Move the start to a nearby declaration/import so the snippet begins
        # at a natural boundary instead of in the middle of an expression.
        for index in range(start, min(len(all_lines), start + 12)):
            if re.match(
                r"\s*(?:def|class|function|func|fn|pub\s+fn|public|private|"
                r"protected|static|void|int|const|let|var|#include|import|"
                r"from|package|using|SELECT|CREATE)\b",
                all_lines[index],
            ):
                start = index
                break
        end = min(len(all_lines), start + GITHUB_SNIPPET_MAX_LINES)
        snippet = "\n".join(all_lines[start:end]).strip()
        if start > 0:
            snippet = "...\n" + snippet
        if end < len(all_lines):
            snippet += "\n..."
    else:
        snippet = normalized.strip()

    if len(snippet) > GITHUB_SNIPPET_MAX_CHARS:
        snippet = snippet[:GITHUB_SNIPPET_MAX_CHARS].rstrip()
        snippet += "\n... (truncated)"
    return snippet


def _github_raw_file(repo: str, branch: str | None, path: str) -> str:
    """Download a text source file from raw.githubusercontent.com."""
    if not repo or not path:
        return ""
    ref = branch or "HEAD"
    url = (
        f"{GITHUB_RAW_BASE}/{quote(repo, safe='/')}/"
        f"{quote(ref, safe='/')}/{quote(path, safe='/')}"
    )
    try:
        response = requests.get(
            url,
            headers={"User-Agent": GITHUB_HEADERS["User-Agent"]},
            timeout=GITHUB_TIMEOUT,
        )
    except requests.RequestException:
        return ""
    if response.status_code != 200:
        return ""
    if len(response.content) > GITHUB_MAX_FILE_BYTES:
        return ""
    # Skip binaries such as images or compiled artifacts.
    if b"\x00" in response.content:
        return ""
    return response.text


def _github_repo_code_paths(
    repo: str,
    branch: str,
    extensions: tuple[str, ...] | None,
    terms: list[str],
    deadline: float,
) -> list[str]:
    """List candidate source files in a repository, best match first."""
    if time.monotonic() >= deadline:
        return []
    url = (
        f"{GITHUB_API_BASE}/repos/{quote(repo, safe='/')}/git/trees/"
        f"{quote(branch or 'HEAD', safe='')}"
    )
    try:
        response = requests.get(
            url,
            params={"recursive": "1"},
            headers=_github_headers(),
            timeout=GITHUB_TIMEOUT,
        )
    except requests.RequestException:
        return []
    if response.status_code != 200:
        return []
    try:
        data = response.json()
    except ValueError:
        return []
    tree = data.get("tree")
    if not isinstance(tree, list):
        return []

    suffixes = extensions or GITHUB_DEFAULT_EXTENSIONS
    normalized_terms = [
        re.sub(r"[^a-z0-9]+", "", term.lower()) for term in terms if term
    ]
    scored: list[tuple[int, int, str]] = []
    for item in tree:
        if not isinstance(item, dict) or item.get("type") != "blob":
            continue
        path = str(item.get("path") or "")
        if not path:
            continue
        if Path(path).suffix.lower() not in suffixes:
            continue
        try:
            size = int(item.get("size") or 0)
        except (TypeError, ValueError):
            size = 0
        if size > GITHUB_MAX_FILE_BYTES:
            continue
        lowered = path.lower()
        normalized_path = re.sub(
            r"[^a-z0-9]+", "", Path(path).with_suffix("").as_posix().lower()
        )
        score = sum(
            2
            for term in normalized_terms
            if term and term in normalized_path
        )
        if re.search(
            r"(?:^|/)(?:tests?|vendor|node_modules|examples?|docs?)/",
            lowered,
        ):
            score -= 1
        scored.append((score, -len(path), path))

    scored.sort(reverse=True)
    matching = [path for score, _length, path in scored if score > 0]
    if matching:
        return matching[:5]
    return [path for _score, _length, path in scored[:3]]


def _github_code_search_snippets(
    query: str,
    language: str | None,
    extensions: tuple[str, ...] | None,
    terms: list[str],
    limit: int,
    deadline: float,
) -> list[dict]:
    """Search GitHub code (requires a token) and return matched fragments."""
    if not GITHUB_TOKEN or limit <= 0 or time.monotonic() >= deadline:
        return []
    search_query = f"{query} in:file"
    if language:
        search_query += f" language:{language}"
    try:
        response = requests.get(
            f"{GITHUB_API_BASE}/search/code",
            params={"q": search_query, "per_page": max(limit * 4, 10)},
            headers=_github_headers(text_match=True),
            timeout=GITHUB_TIMEOUT,
        )
    except requests.RequestException:
        return []
    if response.status_code != 200:
        return []
    try:
        data = response.json()
    except ValueError:
        return []

    snippets: list[dict] = []
    seen: set[str] = set()
    for item in data.get("items") or []:
        if len(snippets) >= limit:
            break
        if not isinstance(item, dict):
            continue
        repository = item.get("repository") or {}
        repo = str(repository.get("full_name") or "")
        path = str(item.get("path") or "")
        url = str(item.get("html_url") or "")
        if not url or url in seen:
            continue
        pieces: list[str] = []
        for match in item.get("text_matches") or []:
            if not isinstance(match, dict):
                continue
            fragment = str(match.get("fragment") or "")
            if fragment.strip():
                pieces.append(fragment)
        code = _github_trim_snippet("\n...\n".join(pieces), terms)
        if not code and repo and path:
            code = _github_trim_snippet(
                _github_raw_file(repo, None, path), terms
            )
        if not code:
            continue
        seen.add(url)
        snippets.append(
            {
                "repo": repo or "GitHub",
                "path": path or "(code search result)",
                "url": url,
                "language": language or _github_snippet_language(path),
                "code": code,
            }
        )
    return snippets


def _github_repository_search_snippets(
    query: str,
    language: str | None,
    extensions: tuple[str, ...] | None,
    terms: list[str],
    limit: int,
    deadline: float,
) -> list[dict]:
    """Unauthenticated fallback: find repositories and read source files."""
    if limit <= 0 or time.monotonic() >= deadline:
        return []
    search_query = query
    if language:
        search_query += f" language:{language}"
    try:
        response = requests.get(
            f"{GITHUB_API_BASE}/search/repositories",
            params={
                "q": search_query,
                "sort": "stars",
                "order": "desc",
                "per_page": GITHUB_REPOSITORY_LIMIT,
            },
            headers=_github_headers(),
            timeout=GITHUB_TIMEOUT,
        )
    except requests.RequestException:
        return []
    if response.status_code != 200:
        return []
    try:
        data = response.json()
    except ValueError:
        return []

    snippets: list[dict] = []
    seen: set[str] = set()
    for repo_data in data.get("items") or []:
        if len(snippets) >= limit or time.monotonic() >= deadline:
            break
        if not isinstance(repo_data, dict):
            continue
        repo = str(repo_data.get("full_name") or "")
        branch = str(repo_data.get("default_branch") or "HEAD")
        if not repo:
            continue
        paths = _github_repo_code_paths(
            repo, branch, extensions, terms, deadline
        )
        for path in paths:
            if len(snippets) >= limit or time.monotonic() >= deadline:
                break
            url = f"https://github.com/{repo}/blob/{branch}/{path}"
            if url in seen:
                continue
            code = _github_trim_snippet(
                _github_raw_file(repo, branch, path), terms
            )
            if not code:
                continue
            seen.add(url)
            snippets.append(
                {
                    "repo": repo,
                    "path": path,
                    "url": url,
                    "language": language or _github_snippet_language(path),
                    "code": code,
                }
            )
            # One snippet per repository keeps the two sources diverse.
            break
    return snippets


def github_code_snippets(
    question: str,
    limit: int = GITHUB_SNIPPET_LIMIT,
    log: Callable[[str], None] | None = None,
    language_guard: str | None = None,
) -> list[dict]:
    """Return up to ``limit`` GitHub code snippets related to ``question``.

    GitHub's code-search API needs a token. With one, matched fragments come
    back directly. Without one, repository search plus the public git-tree and
    raw-content endpoints provide the same fallback using only anonymous
    requests. Results are deduplicated by URL.

    ``language_guard`` is a hard safety guard: Go, Rust, and PHP questions
    never reach GitHub, even if a future caller forgets to check first.
    """

    def emit(message: str) -> None:
        if log is not None:
            log(message)

    if not github_code_fallback_allowed(language_guard):
        emit(
            f"[github: code search disabled for {language_guard}; "
            "official language documentation is used instead]"
        )
        return []

    terms, language, extensions = github_query_terms(question)
    if not terms:
        if not language:
            return []
        terms = [language.lower()]

    deadline = time.monotonic() + GITHUB_TOTAL_TIMEOUT
    query_candidates: list[str] = []
    for count in (len(terms), min(3, len(terms)), 1):
        candidate = " ".join(terms[:count]).strip()
        if candidate and candidate not in query_candidates:
            query_candidates.append(candidate)

    snippets: list[dict] = []
    seen: set[str] = set()
    for query in query_candidates[:2]:
        if len(snippets) >= limit or time.monotonic() >= deadline:
            break
        label = query + (f" ({language})" if language else "")
        emit(f"[github: searching code for {label!r}]")
        found = _github_code_search_snippets(
            query,
            language,
            extensions,
            terms,
            limit - len(snippets),
            deadline,
        )
        if found:
            if len(found) < limit - len(snippets):
                found.extend(
                    _github_repository_search_snippets(
                        query,
                        language,
                        extensions,
                        terms,
                        limit - len(snippets) - len(found),
                        deadline,
                    )
                )
        else:
            emit("[github: code search unavailable; searching repositories]")
            found = _github_repository_search_snippets(
                query,
                language,
                extensions,
                terms,
                limit - len(snippets),
                deadline,
            )
        for snippet in found:
            url = str(snippet.get("url") or "")
            if url and url in seen:
                continue
            if url:
                seen.add(url)
            snippets.append(snippet)
            if len(snippets) >= limit:
                break
    return snippets[:limit]


def format_github_code_answer(snippets: list[dict]) -> str:
    """Build a Markdown answer from the retrieved GitHub code snippets."""
    lines = [
        "I could not find a complete answer in the local knowledge base, so I "
        "searched GitHub for related code. Here are the most relevant "
        "snippets:",
        "",
    ]
    for number, snippet in enumerate(snippets, start=1):
        repo = str(snippet.get("repo") or "GitHub")
        path = str(snippet.get("path") or "")
        url = str(snippet.get("url") or "")
        label = f"{repo}/{path}" if path else repo
        if url:
            lines.append(f"### Snippet {number}: [{label}]({url})")
        else:
            lines.append(f"### Snippet {number}: {label}")
        lines.append("")
        language = str(snippet.get("language") or "").strip()
        lines.append(f"```{language}" if language else "```")
        lines.append(str(snippet.get("code") or "").strip())
        lines.append("```")
        lines.append("")
    lines.append(
        "These snippets are starting points; adapt them to your project's "
        "types, imports, and error handling."
    )
    return "\n".join(lines).strip() + "\n"


def golang_package_docs_request(question: str) -> bool:
    """Return True for a Go code-sample question that names or implies a package.

    ``golang_doc_search.py`` already performs this check with the keyword
    model, but the server needs a cheap, deterministic way to route questions
    such as "how do I use sync.WaitGroup" into coding mode and the Go
    documentation path. Package detection and the code-sample regex are
    imported from the script so both layers agree on what counts as a Go
    package mention; ``candidate_packages`` also maps concept words such as
    "iterator" to the related ``iter`` package.
    """
    try:
        import golang_doc_search as go_docs
    except ImportError:
        return False
    text = " ".join(str(question or "").split())
    if not text:
        return False
    wants_code = bool(
        go_docs.CODE_SAMPLE_RE.search(text)
        or CODE_REQUEST_PATTERN.search(text)
    )
    if not wants_code:
        return False
    return bool(go_docs.candidate_packages(text))


def rust_crate_docs_request(question: str) -> bool:
    """Return True for a Rust code-sample question that names a crate.

    ``rust_doc_search.py`` performs the same check with the keyword model,
    but the server needs a cheap, deterministic way to route questions such
    as "how do I use serde to deserialize JSON" into coding mode and the Rust
    documentation path. The script's code-sample regex and crate detector are
    imported so both layers agree on what counts as a crate mention.
    """
    try:
        import rust_doc_search as rust_docs
    except ImportError:
        return False
    text = " ".join(str(question or "").split())
    if not text:
        return False
    wants_code = bool(
        rust_docs.CODE_SAMPLE_RE.search(text)
        or CODE_REQUEST_PATTERN.search(text)
    )
    if not wants_code:
        return False
    return bool(rust_docs.candidate_crates(text))


def rust_code_docs_request(question: str) -> bool:
    """Return True for any Rust code-sample question.

    Rust code samples must come from the official Rust documentation
    (doc.rust-lang.org and docs.rs), never from the GitHub
    code-search fallback. This check does not require a crate name, so a
    question such as "show me how to iterate over a HashMap" is routed to
    ``rust_doc_search.py`` as well.
    """
    try:
        import rust_doc_search as rust_docs
    except ImportError:
        return False
    text = " ".join(str(question or "").split())
    if not text:
        return False
    return bool(
        rust_docs.CODE_SAMPLE_RE.search(text)
        or CODE_REQUEST_PATTERN.search(text)
        or CHINESE_CODING_PATTERN.search(question)
    )


def php_symbol_docs_request(question: str) -> bool:
    """Return True for a PHP code-sample question that names a symbol.

    ``php_doc_search.py`` performs the same check with the keyword model,
    but the server needs a cheap, deterministic way to route questions such
    as "show me code sample using array_map" into coding mode and the PHP
    manual path. The script's code-sample regex and symbol detector are
    imported so both layers agree on what counts as a PHP symbol mention.
    """
    try:
        import php_doc_search as php_docs
    except ImportError:
        return False
    text = " ".join(str(question or "").split())
    if not text:
        return False
    wants_code = bool(
        php_docs.CODE_SAMPLE_RE.search(text)
        or CODE_REQUEST_PATTERN.search(text)
    )
    if not wants_code:
        return False
    return bool(php_docs.candidate_symbols(text))


def documentation_code_request(language: str | None, question: str) -> bool:
    """Return True when a documentation-backed language asks for code.

    The language documentation scripts use their own ``CODE_SAMPLE_RE``,
    which recognises requests such as "write a CLI" that the generic server
    detector may miss. Mirroring that check here guarantees every code
    request in Go, Rust, or PHP enters coding mode.
    """
    normalized = normalize_language(language)
    if not language_uses_official_docs(normalized):
        return False
    text = " ".join(str(question or "").split())
    if not text:
        return False
    if CODE_REQUEST_PATTERN.search(text) or CHINESE_CODING_PATTERN.search(
        question
    ):
        return True
    try:
        if normalized == GO_LANGUAGE:
            import golang_doc_search as docs
        elif normalized == RUST_LANGUAGE:
            import rust_doc_search as docs
        else:
            import php_doc_search as docs
    except ImportError:
        return False
    return bool(docs.CODE_SAMPLE_RE.search(text))


def _run_doc_search(
    script_name: str,
    question: str,
    *,
    timeout: float,
    log_prefix: str,
    start_message: str,
    success_message: str,
    model: str = PROGRAMMING_MODEL,
    log: Callable[[str], None] | None = None,
    emit: Callable[[str], None] | None = None,
) -> tuple[str | None, str]:
    """Run an official documentation search script and stream its stdout.

    The script prints the documentation answer incrementally. ``emit`` is
    called with each decoded stdout chunk as soon as it arrives, so the HTTP
    handler can forward it to the browser without waiting for the whole
    subprocess to finish. Progress lines from stderr are forwarded through
    ``log`` in the same way.

    Returns ``(answer, "")`` on success and ``(error_answer, error)`` on
    failure. The returned text includes the Sources section printed by the
    script; when a run fails after producing partial output, that partial
    answer is returned together with the error.
    """
    script = SCRIPT_DIR / script_name
    if not script.is_file():
        return None, f"missing script: {script}"

    if log is not None:
        log(f"[{log_prefix}: {start_message}]")
    try:
        process = subprocess.Popen(
            [sys.executable, "-u", str(script), question, "--model", model],
            cwd=SCRIPT_DIR,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            bufsize=0,
        )
    except OSError as exc:
        return None, f"could not run {script_name}: {exc}"

    assert process.stdout is not None and process.stderr is not None
    selector = selectors.DefaultSelector()
    selector.register(process.stdout, selectors.EVENT_READ, "stdout")
    selector.register(process.stderr, selectors.EVENT_READ, "stderr")
    decoder = codecs.getincrementaldecoder("utf-8")("replace")
    fence_filter = CodeFenceRepairFilter()
    answer_parts: list[str] = []
    stderr_text = ""
    stderr_line_buffer = ""
    deadline = time.monotonic() + timeout
    timed_out = False
    returncode: int | None = None

    def deliver_stdout(text: str) -> None:
        """Repair a stdout chunk before it is stored or streamed."""
        repaired = fence_filter.feed(text)
        if not repaired:
            return
        answer_parts.append(repaired)
        if emit is not None:
            emit(repaired)

    try:
        while selector.get_map():
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                timed_out = True
                try:
                    process.kill()
                except OSError:
                    pass
                break
            events = selector.select(timeout=min(remaining, 1.0))
            for key, _mask in events:
                chunk = os.read(key.fileobj.fileno(), 4096)
                if not chunk:
                    selector.unregister(key.fileobj)
                    continue
                if key.data == "stdout":
                    text = decoder.decode(chunk)
                    if not text:
                        continue
                    deliver_stdout(text)
                else:
                    decoded = chunk.decode("utf-8", "replace")
                    stderr_text += decoded
                    stderr_line_buffer += decoded
                    while "\n" in stderr_line_buffer:
                        line, stderr_line_buffer = stderr_line_buffer.split(
                            "\n", 1
                        )
                        line = line.strip()
                        if (
                            line
                            and not line.startswith("Warning:")
                            and log is not None
                        ):
                            log(f"[{log_prefix}: {line}]")

        tail = decoder.decode(b"", final=True)
        if tail:
            deliver_stdout(tail)
        fence_tail = fence_filter.flush()
        if fence_tail:
            answer_parts.append(fence_tail)
            if emit is not None:
                emit(fence_tail)
        trailing_line = stderr_line_buffer.strip()
        if (
            trailing_line
            and not trailing_line.startswith("Warning:")
            and log is not None
        ):
            log(f"[{log_prefix}: {trailing_line}]")

        if not timed_out:
            try:
                returncode = process.wait(timeout=5)
            except subprocess.TimeoutExpired:
                timed_out = True
                try:
                    process.kill()
                except OSError:
                    pass
                returncode = process.wait()
        else:
            returncode = process.wait()
    except BaseException:
        # Includes ClientDisconnected raised by the SSE emitter. Always reap
        # the child so a closed browser tab does not leave the model running.
        try:
            process.kill()
        except OSError:
            pass
        try:
            process.wait(timeout=5)
        except subprocess.TimeoutExpired:
            pass
        raise
    finally:
        selector.close()
        for stream in (process.stdout, process.stderr):
            try:
                stream.close()
            except OSError:
                pass

    answer = "".join(answer_parts).strip()
    if timed_out:
        return (answer or None), f"{script_name} timed out"
    if returncode != 0:
        lines = stderr_text.strip().splitlines()
        reason = lines[-1].strip() if lines else ""
        return (answer or None), reason or f"exit status {returncode}"
    if not answer:
        return None, f"{script_name} returned an empty answer"

    if log is not None:
        log(f"[{log_prefix}: {success_message}]")
    return answer, ""


def _load_programming_docs_index(
    language: str,
) -> tuple[object, list[dict], object | None] | None:
    """Load the cached official-docs index for ``language``.

    The documentation scripts already crawl and cache their language docs.
    Reusing that cache avoids a second crawler and guarantees the excerpts
    shown in a chat answer come from the same reference source described by
    ``PROGRAMMING_DOCS_MAP``. Returns ``None`` when the language is not in the
    map or the cache has not been built yet; the caller then keeps the normal
    transcript-only answer path.
    """
    normalized = normalize_language(language)
    descriptor = PROGRAMMING_DOCS_MAP.get(normalized)
    if descriptor is None:
        return None
    try:
        module = importlib.import_module(descriptor["module"])
    except ImportError:
        return None

    cache_dir = Path(module.DEFAULT_CACHE_DIR).expanduser()
    try:
        cache_file = module.cache_path(cache_dir)
        cache_mtime = cache_file.stat().st_mtime
    except (AttributeError, OSError):
        return None

    with _PROGRAMMING_DOCS_INDEX_LOCK:
        cached = _PROGRAMMING_DOCS_INDEX_CACHE.get(normalized)
        if (
            cached is not None
            and cached[0] == cache_mtime
            and cached[1] is module
        ):
            return module, cached[2], cached[3]

    args = argparse.Namespace(
        cache_dir=str(cache_dir),
        offline=True,
        refresh=False,
        max_pages=getattr(module, "DEFAULT_MAX_PAGES", 250),
    )
    try:
        with redirect_stdout(io.StringIO()), redirect_stderr(io.StringIO()):
            pages = module.get_pages(args)
    except SystemExit:
        return None
    except Exception:
        return None
    if not pages:
        return None

    try:
        chunks = module.chunk_pages(pages)
    except Exception:
        return None
    if not chunks:
        return None

    # Curated notes in ./knowledge/<tag> supplement the official manual, just
    # as they do when the documentation script answers by itself.
    try:
        from knowledge_docs import knowledge_chunks

        chunks.extend(
            knowledge_chunks(
                descriptor["knowledge_tag"], module.chunk_pages
            )
        )
    except Exception:
        pass

    bm25 = None
    bm25_class = getattr(module, "BM25Okapi", None)
    if bm25_class is not None:
        try:
            bm25 = bm25_class(
                [
                    module.tokenize(
                        f"{chunk.get('text', '')} {chunk.get('title', '')}"
                    )
                    for chunk in chunks
                ]
            )
        except Exception:
            bm25 = None

    with _PROGRAMMING_DOCS_INDEX_LOCK:
        _PROGRAMMING_DOCS_INDEX_CACHE[normalized] = (
            cache_mtime,
            module,
            chunks,
            bm25,
        )
    return module, chunks, bm25


def _rank_programming_docs(
    module,
    chunks: list[dict],
    bm25,
    question: str,
    limit: int,
) -> list[tuple[float, dict]]:
    """Rank documentation excerpts for ``question`` with the docs tokenizer."""
    keywords = chat.tokenize(question) or [question]
    if bm25 is None:
        try:
            return module.rank_chunks(question, keywords, chunks, limit)
        except Exception:
            return []

    query_text = " ".join(keywords * 2) + " " + question
    query_tokens = module.tokenize(query_text) or [question.lower()]
    try:
        scores = [float(score) for score in bm25.get_scores(query_tokens)]
    except Exception:
        return []
    try:
        from knowledge_docs import boost_knowledge_scores

        boost_knowledge_scores(chunks, scores)
    except Exception:
        pass

    order = sorted(
        range(len(chunks)), key=lambda index: scores[index], reverse=True
    )
    selected: list[tuple[float, dict]] = []
    per_page: dict[str, int] = {}
    seen_text: set[str] = set()
    for index in order:
        if len(selected) >= limit:
            break
        score = scores[index]
        if score <= 0:
            continue
        chunk = chunks[index]
        url = str(chunk.get("url") or "")
        if (
            not chunk.get("knowledge")
            and per_page.get(url, 0) >= PROGRAMMING_DOCS_CHUNKS_PER_PAGE
        ):
            continue
        fingerprint = str(chunk.get("text") or "")[:160]
        if fingerprint in seen_text:
            continue
        seen_text.add(fingerprint)
        per_page[url] = per_page.get(url, 0) + 1
        selected.append((score, chunk))
    return selected


def programming_docs_context(
    language: str, question: str
) -> tuple[str, dict]:
    """Build an official-documentation reference block for a chat prompt.

    The block is added next to the normal transcript context, so the model
    can use the official manual for syntax, APIs, and language semantics while
    still grounding the rest of the answer in the retrieved trunks.
    """
    normalized = normalize_language(language)
    descriptor = PROGRAMMING_DOCS_MAP.get(normalized)
    if descriptor is None:
        return "", {}
    loaded = _load_programming_docs_index(normalized)
    if loaded is None:
        return "", {
            "language": normalized,
            "source": descriptor["reference"],
            "excerpts": 0,
            "error": "no cached documentation index",
        }

    module, chunks, bm25 = loaded
    selected = _rank_programming_docs(
        module,
        chunks,
        bm25,
        question,
        PROGRAMMING_DOCS_TOP_K,
    )
    if not selected:
        return "", {
            "language": normalized,
            "source": descriptor["reference"],
            "excerpts": 0,
            "error": "no relevant documentation excerpt",
        }

    parts: list[str] = []
    used_chars = 0
    for number, (_score, chunk) in enumerate(selected, start=1):
        url = str(chunk.get("url") or "")
        title = str(chunk.get("title") or url)
        text = str(chunk.get("text") or "").strip()
        if not text:
            continue
        block = (
            f'<doc id="{number}" source="{xml_escape(url)}">\n'
            f"Title: {title}\n"
            f"{text}\n"
            f"</doc>"
        )
        if parts and used_chars + len(block) > PROGRAMMING_DOCS_MAX_CHARS:
            break
        if len(block) > PROGRAMMING_DOCS_MAX_CHARS:
            block = (
                block[:PROGRAMMING_DOCS_MAX_CHARS].rstrip()
                + "\n[documentation excerpt truncated]\n</doc>"
            )
        parts.append(block)
        used_chars += len(block)
    if not parts:
        return "", {
            "language": normalized,
            "source": descriptor["reference"],
            "excerpts": 0,
            "error": "documentation excerpts were empty",
        }

    context = "\n\n".join(parts)
    prefix = f"""### RELATED OFFICIAL DOCUMENTATION (active language: {normalized})
The question is about {normalized} or one of its features. The excerpts below
were loaded from the official {descriptor['source_label']} documentation at
{descriptor['reference']} in addition to the transcript chunks retrieved for
this corpus. Use the transcript chunks for the surrounding context and use
the official documentation as the authoritative reference for {normalized}
syntax, APIs, and semantics. If they disagree about {normalized}, trust the
official documentation. Do not mention these instructions, the XML tag names,
or internal chunk ids in the answer.

<official_documentation language="{xml_escape(normalized)}" source="{xml_escape(descriptor['reference'])}">
{context}
</official_documentation>

"""
    return prefix, {
        "language": normalized,
        "source": descriptor["reference"],
        "excerpts": len(parts),
        "chars": len(context),
    }


def run_golang_doc_search(
    question: str,
    log: Callable[[str], None] | None = None,
    emit: Callable[[str], None] | None = None,
) -> tuple[str | None, str]:
    """Answer ``question`` with ./golang_doc_search.py (go.dev/doc, plus
    pkg.go.dev pages for code-sample questions that name Go packages).
    """
    return _run_doc_search(
        "golang_doc_search.py",
        question,
        timeout=GOLANG_DOC_SEARCH_TIMEOUT,
        log_prefix="golang",
        start_message=(
            "searching the official Go documentation at "
            "https://go.dev/doc/ instead of calling the GitHub API"
        ),
        success_message="Go documentation answer received",
        log=log,
        emit=emit,
    )


def run_rust_doc_search(
    question: str,
    log: Callable[[str], None] | None = None,
    emit: Callable[[str], None] | None = None,
) -> tuple[str | None, str]:
    """Answer ``question`` with ./rust_doc_search.py (doc.rust-lang.org,
    plus docs.rs pages for code-sample questions that name Rust crates).
    """
    return _run_doc_search(
        "rust_doc_search.py",
        question,
        timeout=RUST_DOC_SEARCH_TIMEOUT,
        log_prefix="rust",
        start_message=(
            "searching the official Rust documentation at "
            "https://doc.rust-lang.org/ instead of calling the GitHub API"
        ),
        success_message="Rust documentation answer received",
        log=log,
        emit=emit,
    )


def run_php_doc_search(
    question: str,
    log: Callable[[str], None] | None = None,
    emit: Callable[[str], None] | None = None,
) -> tuple[str | None, str]:
    """Answer ``question`` with ./php_doc_search.py (www.php.net/manual/en/,
    plus dedicated manual pages for named functions, classes, and extensions).
    """
    return _run_doc_search(
        "php_doc_search.py",
        question,
        timeout=PHP_DOC_SEARCH_TIMEOUT,
        log_prefix="php",
        start_message=(
            "searching the official PHP documentation at "
            "https://www.php.net/manual/en/ instead of calling the GitHub API"
        ),
        success_message="PHP documentation answer received",
        log=log,
        emit=emit,
    )


def merge_doc_answer(
    chat_answer: str | None, doc_answer: str, heading: str
) -> str:
    """Merge the local knowledge answer with an official docs answer."""
    doc_answer = doc_answer.strip()
    local_answer = (chat_answer or "").strip()
    if not local_answer or answer_is_insufficient(local_answer):
        return doc_answer + "\n"
    # The local answer already contains the documentation text; do not repeat
    # the whole documentation response.
    if doc_answer and doc_answer.lower() in local_answer.lower():
        return local_answer + "\n"
    return (
        f"{local_answer}\n\n"
        "---\n\n"
        f"### {heading}\n\n"
        f"{doc_answer}\n"
    )


def merge_golang_doc_answer(
    chat_answer: str | None, doc_answer: str
) -> str:
    """Merge the local knowledge answer with the go.dev/doc answer."""
    return merge_doc_answer(
        chat_answer,
        doc_answer,
        "Go documentation reference (go.dev/doc)",
    )


def merge_rust_doc_answer(
    chat_answer: str | None, doc_answer: str
) -> str:
    """Merge the local knowledge answer with the doc.rust-lang.org answer."""
    return merge_doc_answer(
        chat_answer,
        doc_answer,
        "Rust documentation reference (doc.rust-lang.org)",
    )


def merge_php_doc_answer(
    chat_answer: str | None, doc_answer: str
) -> str:
    """Merge the local knowledge answer with the www.php.net answer."""
    return merge_doc_answer(
        chat_answer,
        doc_answer,
        "PHP documentation reference (www.php.net/manual/en)",
    )


def documentation_descriptor(language: str | None) -> dict | None:
    """Return the official-documentation configuration for a language.

    Go, Rust, and PHP code answers must be grounded in go.dev/doc,
    doc.rust-lang.org, or www.php.net/manual/en/ before the general local
    model is allowed to generate code.
    """
    normalized = normalize_language(language)
    if normalized == GO_LANGUAGE:
        return {
            "name": "go",
            "source_label": "go.dev/doc",
            "source_name": "Go documentation (go.dev/doc)",
            "source_url": "https://go.dev/doc/",
            "heading": "Go documentation reference (go.dev/doc)",
            "runner": run_golang_doc_search,
            "merge": merge_golang_doc_answer,
        }
    if normalized == RUST_LANGUAGE:
        return {
            "name": "rust",
            "source_label": "doc.rust-lang.org",
            "source_name": "Rust documentation (doc.rust-lang.org)",
            "source_url": "https://doc.rust-lang.org/",
            "heading": "Rust documentation reference (doc.rust-lang.org)",
            "runner": run_rust_doc_search,
            "merge": merge_rust_doc_answer,
        }
    if normalized == PHP_LANGUAGE:
        return {
            "name": "php",
            "source_label": "www.php.net/manual/en",
            "source_name": "PHP documentation (www.php.net/manual/en)",
            "source_url": "https://www.php.net/manual/en/",
            "heading": (
                "PHP documentation reference (www.php.net/manual/en)"
            ),
            "runner": run_php_doc_search,
            "merge": merge_php_doc_answer,
        }
    return None


# Official documentation answers are also used as a reference for local code
# generation when the excerpts are incomplete. Keep that reference bounded so
# it cannot crowd out the model's answer window.
DOCUMENTATION_REFERENCE_MAX_CHARS = 8000
DOCUMENTATION_INSUFFICIENT_PATTERN = re.compile(
    r"(?:excerpts?\s+(?:do|does|did)\s+not\s+contain"
    r"|(?:indexed\s+)?[\w.]+\s+documentation\s+(?:excerpts?\s+)?"
    r"(?:does|do|did)\s+not\s+contain"
    r"|(?:cannot|can't|unable\s+to)\s+(?:provide|give|write)\s+"
    r"(?:a\s+)?(?:complete|full)\s+answer"
    r"|not\s+contain\s+the\s+information\s+needed"
    r"|(?:based\s+solely|only)\s+on\s+the\s+provided\s+excerpts)",
    re.IGNORECASE,
)

# A fenced code sample is a strong signal that a documentation answer is
# complete even when it opens with a caveat about the indexed excerpts. The
# docs assistants often write "the excerpts do not contain X" and then still
# provide a full working sample, so the caveat alone must not classify the
# answer as a refusal and trigger a second, duplicate generation.
ANSWER_CODE_FENCE_PATTERN = re.compile(
    r"```[^\n]*\n(.*?)```", re.DOTALL
)


def answer_has_code_sample(answer: str | None) -> bool:
    """Return True when an answer contains a substantive fenced code sample."""
    text = str(answer or "")
    for match in ANSWER_CODE_FENCE_PATTERN.finditer(text):
        body = match.group(1)
        lines = [line for line in body.splitlines() if line.strip()]
        if len(lines) >= 3 or len(body.strip()) >= 120:
            return True
    return False


def documentation_answer_is_insufficient(answer: str | None) -> bool:
    """Return True when an official-docs answer is a refusal.

    The documentation scripts use wording such as "the indexed Go
    documentation excerpts do not contain ...", which differs from the
    transcript prompt's refusal phrase. Coding-mode answers are rescued by
    the local model when either form appears.
    """
    if not answer or not answer.strip():
        return True
    # A code sample means the answer is usable even if a caveat sentence
    # mentions that the indexed excerpts were incomplete. Discarding it would
    # clear text that has already streamed to the browser and regenerate it.
    if answer_has_code_sample(answer):
        return False
    if answer_is_insufficient(answer):
        return True
    text = " ".join(answer.split())
    return bool(DOCUMENTATION_INSUFFICIENT_PATTERN.search(text[:800]))


def documentation_reference_excerpt(answer: str) -> str:
    """Trim a docs answer for use as a local-model code-generation reference."""
    text = (answer or "").strip()
    if not text:
        return ""
    # The scripts end with a plain "Sources:" list. It is useful to the reader
    # but not to the model, so leave it out of the prompt reference.
    lines = text.splitlines()
    for index in range(len(lines) - 1, -1, -1):
        if lines[index].strip().lower() == "sources:":
            text = "\n".join(lines[:index]).rstrip()
            break
    if len(text) > DOCUMENTATION_REFERENCE_MAX_CHARS:
        text = (
            text[:DOCUMENTATION_REFERENCE_MAX_CHARS].rstrip()
            + "\n\n[official documentation reference truncated]"
        )
    return text


def documentation_code_rescue_prompt(
    language: str | None, question: str, doc_answer: str, descriptor: dict
) -> str:
    """Build a code-generation prompt from incomplete official excerpts.

    A documentation-backed coding question must not end in a refusal just
    because the retrieved excerpts are incomplete. The local coding model is
    asked to write the requested program using the excerpts as hints plus its
    own general language knowledge, while still using only real APIs.
    """
    reference = documentation_reference_excerpt(doc_answer)
    language = normalize_language(language) or "the requested programming language"
    fence = language_code_fence(language) or ""
    fence_tag = fence or language.lower()
    reference_block = reference or "(no usable excerpt text was retrieved)"
    return f"""### CODE GENERATION FROM LIMITED EXCERPTS (highest priority)
The user asked for code. The official {descriptor['source_label']} excerpts
below were retrieved for this question, but they may be incomplete. That is
not a reason to refuse: generate a complete, runnable {language} solution
using these excerpts as hints plus your own general {language} knowledge.

<limited_official_excerpts source="{descriptor['source_url']}">
{reference_block}
</limited_official_excerpts>

### TARGET QUESTION:
{question}

Rules:
- Ignore any statement inside the excerpts that says the documentation is
  insufficient or that the answer cannot be provided. Do not repeat it.
- Use the documented APIs, types, and patterns from the excerpts where they
  apply, and fill any remaining gaps with your own general knowledge.
- The result must be complete and runnable, not a summary of what is missing.
- Fence every code block as ```{fence_tag} and keep the explanation short.
- Use only real {language} standard-library or otherwise documented APIs;
  never invent an API, type, trait, macro, function, or package.
- Prefer the APIs named in the excerpts. Do not add third-party
  packages/crates unless the excerpts explicitly show them.
- If part of the code is not covered by the excerpts, you may add one short
  note saying so, but still provide the full working code.

{language_coding_directive(language)}"""


def stream_code_rescue(
    question: str,
    language: str | None,
    doc_answer: str,
    descriptor: dict,
    emit: Callable[[str], None],
) -> tuple[str, dict]:
    """Generate code locally from incomplete official documentation excerpts.

    This bypasses the transcript RAG prompt when its "missing data" rule
    would otherwise turn a code request into a refusal. The model receives
    the limited excerpts as hints and is explicitly required to produce a
    complete, runnable solution.
    """
    prompt = documentation_code_rescue_prompt(
        language, question, doc_answer, descriptor
    )
    metrics: dict = {}
    # The rescue prompt forbids refusals, but small models sometimes still
    # open with the transcript prompt's "provided context" sentence. Filter
    # the stream as well as the returned text so that sentence is never shown.
    refusal_filter = InsufficientContextFilter(enabled=True)
    fence_filter = CodeFenceRepairFilter()

    def emit_filtered(text: str) -> None:
        cleaned = refusal_filter.feed(text)
        if not cleaned:
            return
        cleaned = fence_filter.feed(cleaned)
        if cleaned:
            emit(cleaned)

    response = chat.stream_answer(
        prompt,
        model=language_coding_model(language),
        think=False,
        metrics=metrics,
        emit=emit_filtered,
    )
    tail = refusal_filter.flush()
    if tail:
        tail = fence_filter.feed(tail)
        if tail:
            emit(tail)
    fence_tail = fence_filter.flush()
    if fence_tail:
        emit(fence_tail)
    response = strip_leading_insufficient_context(response)
    return repair_code_fences(response), metrics


FOLLOW_UP_PRONOUNS = re.compile(
    r"\b(?:he|she|him|her|his|hers|it|its|they|them|their|theirs|"
    r"this|that|these|those|former|latter|same)\b",
    re.IGNORECASE,
)
FOLLOW_UP_OPENERS = re.compile(
    r"^\s*(?:and|also|but|so|then|what about|how about|what else|"
    r"anything else|why|how so|really|tell me more|more|continue|go on|"
    r"elaborate|expand|which one)\b",
    re.IGNORECASE,
)

INSUFFICIENT_ANSWER_PATTERN = re.compile(
    r"(?:"
    r"(?:the\s+)?(?:provided|available|given\s+)?context\s+"
    r"(?:does\s+not|doesn't|did\s+not|didn't)\s+"
    r"(?:contain|include|provide)\s+"
    r"(?:(?:sufficient|enough|any)\s+(?:information|details?|data|evidence)"
    r"|(?:the\s+)?(?:information|details?|data|evidence)\s+"
    r"(?:needed|required)\s+to\s+answer"
    r"|(?:information|details?|data|evidence)\s+(?:about|regarding|on)\s+"
    r"(?:this|the|that)\s+(?:question|topic|request))"
    r"|insufficient\s+(?:information|context|data|evidence)"
    r"|not\s+(?:enough|sufficient)\s+(?:information|context|data|evidence)"
    r"|(?:there\s+is|there's)\s+no\s+(?:relevant\s+)?"
    r"(?:information|context|data|details?)"
    r"|no\s+(?:relevant\s+)?(?:information|details?|data)\s+"
    r"(?:is\s+)?(?:available|provided|found)\s+in\s+the\s+"
    r"(?:provided\s+)?context"
    r"|(?:i\s+)?(?:do\s+not|don't)\s+have\s+(?:enough|sufficient)\s+"
    r"(?:information|context|data)"
    r"|(?:based\s+on\s+)?(?:the\s+)?(?:provided|available|given\s+)?"
    r"context\s*,?\s*(?:i\s+)?(?:cannot|can't|am\s+unable\s+to)\s+"
    r"(?:answer|determine)"
    r"|(?:i\s+)?(?:cannot|can't|am\s+unable\s+to)\s+(?:answer|determine)\s+"
    r"(?:this|the|that)\s+question\s*(?:[,;:]?\s*)?"
    r"(?:from|in|using|based\s+on)?\s*(?:the\s+)?"
    r"(?:provided|available|given\s+)?context"
    r"|(?:the\s+)?(?:provided|available|given\s+)?context\s+"
    r"(?:is|seems)\s+(?:insufficient|not\s+enough|too\s+limited)"
    r"|(?:i\s+)?(?:cannot|can't|am\s+unable\s+to)\s+(?:answer|determine)\s+"
    r"(?:this|the|that)\s+question\s*[.!]?\s*$"
    r")",
    re.IGNORECASE,
)


def answer_is_insufficient(answer: str | None) -> bool:
    """Return True when an answer is empty or reports missing context.

    Refusals are normally the first sentence of the answer. For long answers
    only the opening is inspected, so a valid answer that mentions a missing
    detail later on is not mistaken for a refusal.
    """
    if not answer or not answer.strip():
        return True
    # Answers that contain a real code sample are not refusals, even when
    # their opening sentence notes that the retrieved context was partial.
    if answer_has_code_sample(answer):
        return False
    # Collapse whitespace so line-wrapped refusals still match the patterns.
    text = " ".join(answer.split())
    if len(text) > 1200:
        text = text[:600]
    return bool(INSUFFICIENT_ANSWER_PATTERN.search(text))


def attach_documentation_answer(
    first_response: str | None,
    first_sources,
    first_stats: dict,
    *,
    name: str,
    source_label: str,
    source_name: str,
    source_url: str,
    heading: str,
    runner: Callable[..., tuple[str | None, str]],
    merge: Callable[[str | None, str], str],
    question: str,
    emit_delta: Callable[[str], None],
    emit_reset: Callable[[str], None],
    emit_log: Callable[[str], None],
    emit_source: Callable[[dict], None],
):
    """Stream an official documentation answer and merge it with the local one.

    Shared by the Go, Rust, and PHP documentation paths. ``runner`` streams
    the documentation answer through the supplied callbacks; ``merge``
    combines it with the answer already produced from the local knowledge
    base.
    """
    local_answer_usable = bool(
        first_response and first_response.strip()
    ) and not answer_is_insufficient(first_response)
    doc_stream_started = False

    def announce(source: str, label: str, detail: str) -> None:
        emit_source(
            {
                "source": source,
                "label": label,
                "url": source_url,
                "detail": detail,
            }
        )

    def emit_doc_delta(text: str) -> None:
        nonlocal doc_stream_started
        if not doc_stream_started:
            doc_stream_started = True
            announce(
                SOURCE_DOCUMENTATION,
                source_name,
                "streaming the authoritative answer from the official "
                f"documentation at {source_url}",
            )
            if local_answer_usable:
                # The local answer is already on screen and streams above
                # this point; only add the reference divider.
                emit_delta(f"\n\n---\n\n### {heading}\n\n")
            elif first_response and first_response.strip():
                # Drop an unusable/refusal local answer as soon as the
                # documentation answer starts arriving.
                emit_reset(f"{name} documentation merge")
        emit_delta(text)

    doc_answer, doc_error = runner(
        question,
        log=emit_log,
        emit=emit_doc_delta,
    )
    if not doc_answer:
        emit_log(
            f"[{name}: documentation search unavailable: {doc_error}; "
            "keeping the local answer]"
        )
        return first_response, first_sources, first_stats

    response = merge(first_response, doc_answer)
    # Keep video annotations only for the part that came from the local
    # transcript context.
    sources = first_sources if local_answer_usable else []
    stats = dict(first_stats)
    stats[f"{name}_doc_fallback"] = {
        "used": True,
        "doc_chars": len(doc_answer),
        "local_answer_used": local_answer_usable,
        "streamed": doc_stream_started,
    }
    if doc_error:
        emit_log(
            f"[{name}: the documentation stream ended early: {doc_error}]"
        )
    if local_answer_usable:
        announce(
            SOURCE_MERGED,
            f"local LLM + {source_name}",
            "the final answer combines the local Ollama answer with the "
            f"official documentation at {source_url}",
        )
    emit_log(
        f"[{name}: merged the {source_label} answer with the local "
        "knowledge answer]"
    )
    return response, sources, stats


def split_translation_paragraphs(text: str) -> list[str]:
    """Split an answer into paragraph-sized blocks for streaming translation."""
    normalized = text.replace("\r\n", "\n").replace("\r", "\n").strip()
    if not normalized:
        return []
    return [
        block.strip("\n")
        for block in TRANSLATION_PARAGRAPH_PATTERN.split(normalized)
        if block.strip()
    ]


class TranslationPrefixFilter:
    """Strip a leading model label such as ``Translation:`` while streaming."""

    def __init__(self) -> None:
        self._buffer = ""
        self._ready = False

    def feed(self, token: str) -> str:
        if self._ready:
            return token
        self._buffer += token
        stripped = self._buffer.lstrip()
        if not stripped:
            return ""
        # Wait briefly so a label split across tokens can be removed as a
        # whole. Short paragraphs are released by flush().
        if "\n" not in stripped and len(stripped) < 16:
            return ""
        self._ready = True
        return TRANSLATION_LABEL_PATTERN.sub("", stripped, count=1)

    def flush(self) -> str:
        if self._ready:
            return ""
        self._ready = True
        return TRANSLATION_LABEL_PATTERN.sub(
            "", self._buffer.lstrip(), count=1
        )


CONVERSATION_REWRITE_PROMPT = """Decide whether the latest user question depends on the conversation, then rewrite it as a standalone question.

<conversation>
{conversation}
</conversation>

<latest_question>
{question}
</latest_question>

Rules:
- If the latest question is a follow-up that refers to the conversation, rewrite it as a standalone question.
- Replace "he", "she", "him", "her", "his", or "hers" with the person named in the conversation.
- Replace "it" or "its" with the thing or topic being discussed.
- Replace "they" or "them" with the people or things being discussed.
- Replace "this", "that", "these", or "those" with the specific thing or things they point to.
- If the latest question is a short or elliptical follow-up (for example "why?", "and his wife?", "what about that?", or "what are the opening hours?"), turn it into a complete question about the relevant topic in the conversation.
- A question is a follow-up only when it refers to the conversation or asks for another detail about the same subject. For example, after a conversation about Nijo Market, "what are the opening hours?" and "is there parking?" are follow-ups, but "how do I cook pasta?" is not.
- If the latest question is already a complete standalone question, or it introduces a different subject or topic that is not part of the conversation (a new place, person, product, or event), output exactly UNRELATED.
- Never add a conversation entity to a question that did not refer to the conversation or ask about the same subject. For example, do not turn "what is the weather in Tokyo?" into a question about the conversation's subject.
- Keep the user's intent, wording, language, and constraints.
- Use only names and topics stated in the conversation. Do not answer the question and do not add outside facts.
Output only the standalone question on one line, or the single word UNRELATED, with no quotes, labels, or explanation.

Standalone question:"""

CONVERSATION_UNRELATED = "unrelated"
# A short question is ambiguous: it may be an elliptical follow-up even when it
# contains no pronoun. Content words are counted without the words that almost
# every question uses. Longer questions carry enough topic words to be treated
# as standalone, which avoids a model call on every turn.
FOLLOW_UP_MAX_CONTENT_WORDS = 6
CONVERSATION_STOPWORDS = frozenset(
    "a an the and or but if then than that this these those there here "
    "what which who whom whose when where why how is are was were be been "
    "being do does did done have has had can could should would will may "
    "might must shall to of in on at for from with about into over after "
    "before under between during any some all more most other another "
    "such only just also very much many few own same so not no nor too "
    "i me my we us you your he him his she her it its they them their "
    "theirs please tell give show list find get".split()
)


class ConversationResolution(NamedTuple):
    """Result of interpreting one question against the recent conversation."""

    question: str
    is_follow_up: bool = False
    is_unrelated: bool = False


def conversation_content_terms(text: str) -> set[str]:
    """Return the topic-bearing terms of a question or answer."""
    return {
        token
        for token in chat.tokenize(text)
        if len(token) > 1 and token not in CONVERSATION_STOPWORDS
    }


def conversation_overlap_ratio(question: str, turns: list[dict]) -> float:
    """Return the strongest topic overlap between a question and recent questions.

    The ratio is measured against the smaller of the two questions, so a single
    generic shared word (for example "market" in an unrelated question) does
    not dominate a longer question.
    """
    if not turns:
        return 0.0
    question_terms = conversation_content_terms(question)
    if not question_terms:
        return 1.0
    best = 0.0
    for turn in turns[-CONVERSATION_MAX_TURNS:]:
        asked = str(
            turn.get("resolved_question") or turn.get("question") or ""
        )
        asked_terms = conversation_content_terms(asked)
        if not asked_terms:
            continue
        shared = question_terms & asked_terms
        if not shared:
            continue
        best = max(
            best, len(shared) / min(len(question_terms), len(asked_terms))
        )
    return best


def question_relates_to_conversation(question: str, turns: list[dict]) -> bool:
    """Return True when a question shares a substantial topic with recent turns.

    The comparison deliberately uses the previous *questions*, not the previous
    answers. Answers are long, so almost any new question shares some word with
    them (for example a Nijo Market answer mentions "Japan", which would make a
    later visa question look related). A question starts a fresh context unless
    it shares more than half of the smaller side of the comparison with one of
    the last asked questions, which keeps genuine same-topic questions (for
    example "is nijo market open on sunday?" after "where is nijo market?") in
    the same context while a different subject refreshes it.
    """
    return conversation_overlap_ratio(question, turns) > 0.5


def is_unrelated_response(response: str) -> bool:
    """Return True when the conversation model flags a new topic."""
    text = chat.THINK_PATTERN.sub("", response).strip().strip('"\'`“”')
    normalized = re.sub(r"[^a-z]+", " ", text.lower()).strip()
    return bool(normalized) and normalized.split()[0] == CONVERSATION_UNRELATED


def normalize_question_for_comparison(text: str) -> str:
    """Fold wording that does not change a question's meaning.

    Case, punctuation, and articles are ignored so a model that only re-adds
    "a" or "the" (for example "what is git worktree?" -> "What is a Git
    worktree?") is not mistaken for a rewritten follow-up. That mistake kept
    a stale conversation alive and sent the new topic to the previous topic's
    context.
    """
    tokens = re.findall(r"[a-z0-9]+", text.lower())
    return " ".join(token for token in tokens if token not in {"a", "an", "the"})


def question_needs_context(question: str, turns: list[dict]) -> bool:
    """Return True when ``question`` may refer to an earlier turn."""
    if not turns:
        return False
    text = question.strip()
    if not text:
        return False
    if FOLLOW_UP_PRONOUNS.search(text) or FOLLOW_UP_OPENERS.search(text):
        return True
    # Very short or elliptical questions such as "why?", "how so?", or
    # "what are the opening hours?" usually depend on the previous turn even
    # when they contain no explicit pronoun. Longer questions have enough
    # topic words to stand on their own and skip the model call.
    return len(conversation_content_terms(text)) <= FOLLOW_UP_MAX_CONTENT_WORDS


def format_conversation(turns: list[dict]) -> str:
    """Format recent turns for the follow-up rewrite prompt."""
    lines: list[str] = []
    for turn in turns[-CONVERSATION_MAX_TURNS:]:
        asked = " ".join(str(turn.get("question", "")).split())
        resolved = " ".join(str(turn.get("resolved_question", "")).split())
        if resolved and resolved != asked:
            asked = f"{asked} (resolved: {resolved})"
        answer = " ".join(str(turn.get("answer", "")).split())
        if len(answer) > CONVERSATION_ANSWER_CHARS:
            answer = answer[:CONVERSATION_ANSWER_CHARS].rstrip() + "..."
        lines.append(f"User: {asked}")
        lines.append(f"Assistant: {answer}")
    return "\n".join(lines)


def clean_standalone_question(response: str) -> str | None:
    """Extract one clean standalone question from a model response."""
    text = chat.THINK_PATTERN.sub("", response).strip()
    text = re.sub(
        r"^(?:standalone|rewritten|search|new)\s+question\s*[:：]\s*",
        "",
        text,
        flags=re.IGNORECASE,
    )
    text = re.sub(r"^(?:question|q)\s*[:：]\s*", "", text, flags=re.IGNORECASE)
    lines = [line.strip() for line in text.splitlines() if line.strip()]
    if not lines:
        return None
    text = " ".join(lines[0].split()).strip('"\'`“”')
    if not text or text.lower() in {"none", "n/a", "unchanged"}:
        return None
    if len(text) > 600:
        return None
    return text


def rewrite_follow_up(
    question: str,
    turns: list[dict],
) -> ConversationResolution | None:
    """Resolve references in a follow-up or detect a completely new topic."""
    if not turns:
        return None
    prompt = CONVERSATION_REWRITE_PROMPT.format(
        conversation=format_conversation(turns),
        question=question,
    )
    unchanged: str | None = None
    for model in dict.fromkeys((CONVERSATION_MODEL, chat.EXPANSION_MODEL)):
        try:
            response = chat.generate_text(
                prompt,
                model=model,
                temperature=0.0,
                num_predict=256,
                num_ctx=chat.ANSWER_NUM_CTX,
                stop=["\n\n", "</latest_question>"],
            )
        except RuntimeError:
            continue
        if is_unrelated_response(response):
            # A model may call a standalone question on the same subject a new
            # topic. Keep the conversation when the question still shares its
            # subject with an earlier question; there is nothing to rewrite,
            # but a later follow-up must still be able to refer back to it.
            if question_relates_to_conversation(question, turns):
                return ConversationResolution(question, is_follow_up=False)
            return ConversationResolution(
                question, is_follow_up=False, is_unrelated=True
            )
        cleaned = clean_standalone_question(response)
        if not cleaned:
            continue
        if (
            normalize_question_for_comparison(cleaned)
            != normalize_question_for_comparison(question)
        ):
            return ConversationResolution(cleaned, is_follow_up=True)
        unchanged = cleaned
    if unchanged is not None:
        # The model returned the question unchanged. It is a follow-up only
        # when it still shares a topic with the recent questions; otherwise it
        # is a fresh question and the stale context must be dropped.
        related = question_relates_to_conversation(question, turns)
        return ConversationResolution(
            question, is_follow_up=related, is_unrelated=not related
        )
    # No conversation model was available. Fall back to a lexical decision so
    # an unrelated question still gets a fresh context.
    related = question_relates_to_conversation(question, turns)
    return ConversationResolution(
        question, is_follow_up=related, is_unrelated=not related
    )


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


def fetch_news_article(url: str, title: str = "") -> dict:
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
        timeout=180,
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
        "paragraphs": paragraphs,
        "images": images,
    }


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

    news_id = news_id_for_item(payload)
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


# ---------------------------------------------------------------------------
# Paragraph -> source video annotations
#
# The browser shows a small YouTube icon beside every answer paragraph. The
# answer model is not asked to emit citations (the prompt explicitly forbids
# them), so the mapping is recovered after the fact: each rendered answer
# block is matched against the trunk text that was actually used as context.
# The splitter mirrors renderMarkdown() in chat.html so block indexes line up
# exactly with the browser's <p>/<li>/<hN>/<blockquote> elements.
# ---------------------------------------------------------------------------
YOUTUBE_URL_TEMPLATE = "https://www.youtube.com/watch?v={video_id}"

ANSWER_BLOCK_START_RE = re.compile(
    r"^(?:#{1,6}\s|```|>\s?|(?:[-*+]|\d+[.)])\s+|-{3,}\s*$|\*{3,}\s*$)"
)
ANSWER_HEADING_RE = re.compile(r"^(#{1,6})\s+(.*)$")
ANSWER_LIST_RE = re.compile(r"^\s*([-*+]|\d+[.)])\s+(.*)$")
ANSWER_QUOTE_RE = re.compile(r"^\s*>\s?")
ANSWER_HR_RE = re.compile(r"^\s*(?:-{3,}|\*{3,}|_{3,})\s*$")
ANSWER_FENCE_RE = re.compile(r"^\s*```")

# Small words carry little source signal and would make every paragraph match
# the same common trunk. They are ignored when scoring block/source overlap.
ANNOTATION_STOPWORDS = frozenset(
    {
        "the", "and", "for", "that", "this", "with", "from", "are",
        "was", "were", "have", "has", "had", "not", "but", "you",
        "your", "they", "their", "them", "his", "her", "its", "our",
        "who", "what", "when", "where", "which", "how", "why", "can",
        "could", "would", "should", "may", "might", "will", "also",
        "into", "about", "more", "than", "then", "such", "some", "any",
        "all", "one", "two", "three", "very", "just", "there", "these",
        "those", "been", "being", "does", "did", "doing", "use", "used",
        "using", "including", "include", "includes", "other", "only",
    }
)


def split_answer_blocks(text: str) -> list[str]:
    """Split an answer into blocks in the same order renderMarkdown emits.

    Code fences and horizontal rules produce no annotatable block, matching
    the browser renderer. List items are separate blocks; blockquote lines are
    concatenated because the browser renders them with <br> (whose textContent
    has no separating space).
    """
    normalized = text.replace("\r\n", "\n").replace("\r", "\n")
    lines = normalized.split("\n")
    blocks: list[str] = []
    i = 0
    while i < len(lines):
        line = lines[i]
        if not line.strip():
            i += 1
            continue

        if ANSWER_FENCE_RE.match(line):
            i += 1
            while i < len(lines) and not ANSWER_FENCE_RE.match(lines[i]):
                i += 1
            if i < len(lines):
                i += 1
            continue

        if ANSWER_HR_RE.match(line):
            i += 1
            continue

        heading = ANSWER_HEADING_RE.match(line)
        if heading:
            blocks.append(heading.group(2).strip())
            i += 1
            continue

        if ANSWER_QUOTE_RE.match(line):
            quote_lines: list[str] = []
            while i < len(lines) and ANSWER_QUOTE_RE.match(lines[i]):
                quote_lines.append(ANSWER_QUOTE_RE.sub("", lines[i], count=1))
                i += 1
            blocks.append("".join(quote_lines).strip())
            continue

        list_match = ANSWER_LIST_RE.match(line)
        if list_match:
            ordered = list_match.group(1)[0].isdigit()
            while i < len(lines):
                item_match = ANSWER_LIST_RE.match(lines[i])
                if not item_match:
                    break
                if item_match.group(1)[0].isdigit() != ordered:
                    break
                blocks.append(item_match.group(2).strip())
                i += 1
            continue

        paragraph_lines = [line]
        i += 1
        while (
            i < len(lines)
            and lines[i].strip()
            and not ANSWER_BLOCK_START_RE.match(lines[i])
        ):
            paragraph_lines.append(lines[i])
            i += 1
        blocks.append(" ".join(paragraph_lines).strip())

    return [block for block in blocks if block]


def _annotation_tokens(text: str) -> set[str]:
    return {
        token
        for token in chat.tokenize(text)
        if len(token) > 2 and token not in ANNOTATION_STOPWORDS
    }


def build_answer_annotations(
    answer: str, sources: list[tuple[chat.Chunk, float]]
) -> list[dict]:
    """Map each answer block to the source it most likely came from.

    Scoring is the cosine similarity between IDF-weighted token sets of the
    answer block and each trunk (plus its title). Normalizing by the source
    length matters because a long transcript paragraph shares many common
    words with any answer, while a short, precise news paragraph can be the
    actual source. A learned news story that is nearly as good a lexical match
    as the best transcript is preferred, so a story read with /news keeps its
    news icon even when a video paraphrases the same reporting. When a block
    shares no vocabulary with any trunk (for example a Chinese answer
    synthesized from English context), it falls back to the top-ranked trunk
    so the paragraph still gets a useful source link.
    """
    if not answer or not sources:
        return []
    blocks = split_answer_blocks(answer)
    if not blocks:
        return []

    source_rows: list[tuple[int, chat.Chunk, str, set[str]]] = []
    for rank, (chunk, _score) in enumerate(sources, start=1):
        title = chunk_title(chunk)
        tokens = _annotation_tokens(
            f"{title}\n{chat.paragraph_text(chunk)}"
        )
        source_rows.append((rank, chunk, title, tokens))

    doc_freq: dict[str, int] = {}
    for _rank, _chunk, _title, tokens in source_rows:
        for token in tokens:
            doc_freq[token] = doc_freq.get(token, 0) + 1
    total = len(source_rows)
    weights = {
        token: math.log((total + 1) / (freq + 0.5)) + 0.5
        for token, freq in doc_freq.items()
    }

    annotations: list[dict] = []
    for index, block in enumerate(blocks):
        tokens = _annotation_tokens(block)
        block_weight = sum(weights.get(token, 0.5) for token in tokens) or 1.0

        best: tuple[int, chat.Chunk, str, float] | None = None
        best_news: tuple[int, chat.Chunk, str, float] | None = None
        for rank, chunk, title, source_tokens in source_rows:
            overlap = tokens & source_tokens
            if not overlap:
                continue
            overlap_weight = sum(
                weights.get(token, 0.5) for token in overlap
            )
            source_weight = (
                sum(weights.get(token, 0.5) for token in source_tokens)
                or 1.0
            )
            # Cosine similarity, so a short exact source is not outscored by
            # a long paragraph that merely repeats many common words.
            score = overlap_weight / math.sqrt(
                block_weight * source_weight
            )
            if best is None or score > best[3] + 1e-9:
                best = (rank, chunk, title, score)
            if chunk_is_news(chunk) and (
                best_news is None or score > best_news[3] + 1e-9
            ):
                best_news = (rank, chunk, title, score)

        # Learned news is an explicit source the user chose to keep, so give
        # it the citation when it is essentially as close as the best match.
        if (
            best_news is not None
            and (best is None or best_news[3] >= best[3] * 0.75)
        ):
            best = best_news

        if best is None:
            chunk = sources[0][0]
            best = (1, chunk, chunk_title(chunk), 0.0)

        rank, chunk, title, score = best
        annotations.append(
            {
                "index": index,
                "video_id": chunk.video_id,
                "chunk_index": chunk.index,
                "title": title or chunk.video_id,
                "url": chunk_url(chunk),
                "kind": "news" if chunk_is_news(chunk) else "video",
                "rank": rank,
                "score": round(score, 4),
            }
        )
    return annotations


# Articles loaded with /article are split into the same one-knowledge-piece
# paragraphs that the transcript pipeline uses. The generated trunk files live
# outside ./.chat/articles_cache so they never become corpus videos or appear in
# transcript autocomplete.
ARTICLE_CACHE_DIR = APP_DIR / ".article_cache"
ARTICLE_HTML_BLOCK_RE = re.compile(
    r"<\s*(script|style|noscript|svg|head)\b[^>]*>.*?<\s*/\s*\1\s*>",
    re.IGNORECASE | re.DOTALL,
)
ARTICLE_HTML_BREAK_RE = re.compile(
    r"<\s*/?\s*(?:br|p|div|li|ul|ol|h[1-6]|tr|td|th|section|article|"
    r"blockquote|pre)\b[^>]*>",
    re.IGNORECASE,
)
ARTICLE_HTML_TAG_RE = re.compile(r"<[^>]+>")
ARTICLE_MARKDOWN_HEADING_RE = re.compile(
    r"^\s{0,3}#{1,6}\s+(?P<title>.+?)\s*#*\s*$"
)
ARTICLE_HTML_HEADING_RE = re.compile(
    r"<\s*h[1-6]\b[^>]*>(?P<heading>.*?)<\s*/\s*h[1-6]\s*>",
    re.IGNORECASE | re.DOTALL,
)
ARTICLE_SUMMARY_HEADING_RE = re.compile(
    r"^\s*(?:\d+(?:\.\d+)*[.)]?\s*)?"
    r"(?:tl\s*;?\s*dr|summary|overview|abstract|executive summary)\b",
    re.IGNORECASE,
)
ARTICLE_NUMBERED_HEADING_RE = re.compile(
    r"^\s*\d+(?:\.\d+)*[.)]?\s+[A-Za-z]"
)
ARTICLE_OVERVIEW_REFERENCE_RE = re.compile(
    r"\b(?:this|the)\s+(?:article|document|doc|file|page|report|paper|book|novel)\b",
    re.IGNORECASE,
)
# A bare deictic ("what is this about?") refers to the whole article. The
# lookahead excludes cases where "this" is a determiner for another noun
# ("what is this item tag about?"), which is a question about one concept.
ARTICLE_BARE_DEICTIC_RE = re.compile(
    r"\b(?:this|it)\b(?=\s*(?:$|[,.;:!?]|"
    r"(?:about|all|in|with|for|on|by|to|of|and|or)\b))",
    re.IGNORECASE,
)
ARTICLE_OVERVIEW_INTENT_RE = re.compile(
    r"\b(?:about|summari[sz]e|summary|overview|tl\s*;?\s*dr|main|"
    r"topics?|discuss(?:es|ed)?|cover(?:s|ed)?|describe(?:s|d)?|purpose|"
    r"what is|what's|what does)\b",
    re.IGNORECASE,
)
ARTICLE_BROAD_OVERVIEW_RE = re.compile(
    r"\bwhat(?:'s|\s+is|\s+was)?\s+(?:this|it)\s+(?:all\s+)?about\b"
    r"|\b(?:summari[sz]e|overview|tl\s*;?\s*dr)\s+(?:this|it)\b"
    r"|\b(?:tell|show)\s+me\s+(?:about\s+)?(?:this|it)\b"
    r"|\bdescribe\s+(?:this|it)\b"
    r"|\b(?:main|key|top|core)\s+(?:recommendations?|takeaways?|"
    r"points?|findings?|conclusions?)\b",
    re.IGNORECASE,
)
ARTICLE_DETAILED_OVERVIEW_RE = re.compile(
    r"\b(?:detail(?:ed)?|in[- ]depth|comprehensive|thorough|long|extended|"
    r"elaborate|exhaustive)\b",
    re.IGNORECASE,
)
ARTICLE_WORD_COUNT_RE = re.compile(
    r"\b(\d[\d,]*)\s*[- ]?(?:words?|word\s+answer)\b",
    re.IGNORECASE,
)
ARTICLE_OVERVIEW_OUTLINE_CHARS = 1400
ARTICLE_OVERVIEW_SUMMARY_CHARS = 2400
ARTICLE_DIGEST_CHARS = 16000
ARTICLE_DIGEST_MIN_SECTION_CHARS = 300
ARTICLE_DIGEST_MAX_SECTION_CHARS = 1400
ARTICLE_OVERVIEW_INDEX = 0
ARTICLE_DIGEST_INDEX = -1
ARTICLE_TOP_SECTION_RE = re.compile(r"^\s*\d+[.)]?\s+\S")
ARTICLE_SUBSECTION_RE = re.compile(r"^\s*\d+\.\d+")

# Structure-aware article routing. The cache directory is content-addressed, so
# these generated artifacts can live beside the paragraph trunks and be reused
# until the article bytes change.
ARTICLE_META_VERSION = 4
ARTICLE_META_FILENAME = "article_structure.json"
ARTICLE_SECTION_SUMMARY_CHARS = 1400
ARTICLE_SECTION_SUMMARY_INDEX_BASE = -100
ARTICLE_SECTION_DEPTH = 12
ARTICLE_SECTION_CONTEXT_TOKENS = 8192
ARTICLE_NARROW_CONTEXT_TOKENS = 6144
ARTICLE_ITERATIVE_MAX_ROUNDS = 4
ARTICLE_ITERATIVE_EVIDENCE_TOKENS = 16000
ARTICLE_ITERATIVE_BATCH = 4
ARTICLE_ITERATIVE_CANDIDATES = 50
ARTICLE_ROUTER_MODEL = chat.FILTER_MODEL
# Normal transcript questions reuse the /article iterative evidence loop.
# YouTube transcripts are shorter per paragraph, but the global corpus spans
# many videos, so the same bounded-round design applies. Enumeration questions
# need wider coverage.
YOUTUBE_ITERATIVE_MAX_ROUNDS = 3
YOUTUBE_ITERATIVE_ENUMERATIVE_ROUNDS = 5
YOUTUBE_ITERATIVE_EVIDENCE_TOKENS = 16000
YOUTUBE_ITERATIVE_BATCH = 4
YOUTUBE_ITERATIVE_ENUMERATIVE_BATCH = 6
YOUTUBE_ITERATIVE_CANDIDATES = 50
YOUTUBE_ITERATIVE_PROMPT_PREFIX = """### RETRIEVED EVIDENCE
The context below was gathered in several retrieval rounds from the knowledge
base and is sufficient for the complete question. Answer every part of the
question, including lists, comparisons, and enumerations, using this evidence.
Do not say that the context is insufficient; if a detail is genuinely absent,
answer the supported parts and omit the unsupported detail.

"""
# A learned news story, a single transcript, or a book modal is a small,
# explicitly-scoped corpus. If the question has no lexical match in that scope,
# BM25 can return nothing and the answer stream ends silently (the article
# modal then looks broken). In that case use the scope's reading-order text as
# context, up to the normal context budget, instead of refusing with no output.
SCOPED_CONTEXT_FALLBACK_PROMPT_PREFIX = """### SCOPED SOURCE
The question did not lexically match any single paragraph, so the context
below is the available text of the active source in reading order. Use this
source to answer. If the requested detail is genuinely absent, say briefly
that this source does not cover it; never return an empty answer.

"""
ARTICLE_ENUMERATIVE_RE = re.compile(
    r"\b(?:list|all|every|each|enumerate|compare|comparison|across\s+the\s+"
    r"(?:article|document)|types?|kinds?|levels?|categories)\b",
    re.IGNORECASE,
)
ARTICLE_SECTION_INTENT_RE = re.compile(
    r"\b(?:explain|describe|walk\s+me\s+through|how\s+(?:does|do|is|are)|"
    r"workflow|process|section|chapter|part\s+\d+|why\s+(?:does|do|is|are))\b",
    re.IGNORECASE,
)
ARTICLE_ROUTER_PROMPT = """Classify a question about one long article.

Choose exactly one label:
- OVERVIEW: the question asks for a summary of the whole article.
- SECTION: the question is mainly about one section. Also name its number.
- NARROW: the question asks for a specific fact from somewhere in the article.

Article sections:
{sections}

Question: {question}

Reply with one line in exactly one of these forms:
OVERVIEW
SECTION <number>
NARROW
"""
ARTICLE_SUFFICIENCY_PROMPT = """You are checking whether retrieved excerpts are enough to answer a question.

Question: {question}

Excerpts:
{excerpts}

Can every part of the question be answered with a specific fact or quote from
these excerpts? Reply in exactly this format:
VERDICT: YES or NO
EVIDENCE: [excerpt number] (required when the verdict is YES)
MISSING: what is still missing (short; write NONE when nothing is missing)
"""


class ArticleRoute(NamedTuple):
    """Routing decision for one question about the active article."""

    label: str
    section: int | None = None
    iterative: bool = False
    source: str = "regex"


def article_requested_word_count(question: str) -> int | None:
    """Return the requested answer length ("in 1000 words"), if any."""
    match = ARTICLE_WORD_COUNT_RE.search(question)
    if not match:
        return None
    try:
        words = int(match.group(1).replace(",", ""))
    except ValueError:
        return None
    return words if 30 <= words <= 4000 else None


def article_overview_question(question: str) -> bool:
    """Return True for questions about the article as a whole.

    These questions need the document title, outline, and opening/summary
    section rather than the normal BM25 paragraphs. Without this branch a
    broad question such as "what is this article about?" matches generic
    words ("what", "this") in unrelated paragraphs and the answer model sees
    no useful overview.

    Phrasings that use a bare deictic ("what is this about, in 1000 words")
    count as well because this helper is only consulted in article mode.
    """
    if ARTICLE_BROAD_OVERVIEW_RE.search(question):
        return True
    if not ARTICLE_OVERVIEW_INTENT_RE.search(question):
        return False
    if ARTICLE_OVERVIEW_REFERENCE_RE.search(question):
        return True
    if ARTICLE_BARE_DEICTIC_RE.search(question):
        return True
    return False


def _clean_article_heading(raw: str) -> str:
    text = ARTICLE_HTML_TAG_RE.sub(" ", raw)
    return " ".join(html.unescape(text).split())


def _article_headings(raw_text: str, text: str) -> list[str]:
    """Collect HTML or Markdown headings in document order."""
    headings: list[str] = []
    seen: set[str] = set()

    def add(heading: str) -> None:
        heading = " ".join(heading.split())
        key = heading.casefold()
        if heading and len(heading) <= 200 and key not in seen:
            seen.add(key)
            headings.append(heading)

    heading_source = ARTICLE_HTML_BLOCK_RE.sub("\n", raw_text)
    for raw in ARTICLE_HTML_HEADING_RE.findall(heading_source):
        add(_clean_article_heading(raw))
    for line in text.splitlines():
        match = ARTICLE_MARKDOWN_HEADING_RE.match(line)
        if match:
            add(match.group("title"))
    return headings


def _article_heading_like(paragraph: str) -> bool:
    """Return True for short, heading-shaped lines (including nav entries)."""
    if len(paragraph) > 120:
        return False
    if ARTICLE_NUMBERED_HEADING_RE.match(paragraph):
        return True
    return (
        len(paragraph.split()) <= 12
        and not paragraph.endswith((".", "!", "?", ";", ","))
    )


def _article_summary_paragraphs(paragraphs: list[str]) -> list[str]:
    """Return the paragraphs under a TL;DR/summary/overview heading."""
    normalized = [" ".join(paragraph.split()) for paragraph in paragraphs]
    matches = [
        index
        for index, paragraph in enumerate(normalized)
        if len(paragraph) <= 120
        and ARTICLE_SUMMARY_HEADING_RE.match(paragraph)
    ]
    if not matches:
        return []
    # An article often repeats "TL;DR" in its table of contents. Prefer the
    # numbered section heading ("1. TL;DR") when one exists.
    start = next(
        (
            index
            for index in matches
            if ARTICLE_NUMBERED_HEADING_RE.match(normalized[index])
        ),
        matches[0],
    )

    result: list[str] = []
    char_count = 0
    for paragraph in normalized[start + 1 :]:
        if not paragraph:
            continue
        # Skip outline/navigation entries between a summary link and the real
        # summary body (common in HTML pages with a table of contents).
        if not result and _article_heading_like(paragraph):
            continue
        if result and _article_heading_like(paragraph):
            break
        result.append(paragraph)
        char_count += len(paragraph)
        if len(result) >= 12 or char_count >= ARTICLE_OVERVIEW_SUMMARY_CHARS:
            break
    return result


def _article_opening_paragraphs(paragraphs: list[str]) -> list[str]:
    """Fallback overview text when the article has no summary section."""
    result: list[str] = []
    char_count = 0
    for paragraph in paragraphs[1:]:
        paragraph = " ".join(paragraph.split())
        if not paragraph:
            continue
        if len(paragraph) <= 120 and (
            ARTICLE_NUMBERED_HEADING_RE.match(paragraph)
            or (
                len(paragraph.split()) <= 10
                and not paragraph.endswith((".", "!", "?", ";"))
            )
        ):
            continue
        result.append(paragraph)
        char_count += len(paragraph)
        if len(result) >= 6 or char_count >= ARTICLE_OVERVIEW_SUMMARY_CHARS:
            break
    return result


def _article_overview(
    title: str, headings: list[str], paragraphs: list[str]
) -> str:
    """Build an always-in-context overview trunk for a loaded article.

    It contains the title, a compact outline, and the TL;DR/summary section
    (or the opening paragraphs when there is no explicit summary). The normal
    paragraph trunks remain unchanged, so follow-up questions still use BM25
    over the full article.
    """
    summary = _article_summary_paragraphs(paragraphs)
    if not summary:
        summary = _article_opening_paragraphs(paragraphs)

    parts = [
        f"Article title: {title}",
        "Document overview: this article summarizes the main topics, "
        "purpose, and summary of the document.",
    ]
    if headings:
        outline: list[str] = []
        outline_chars = 0
        for heading in headings[:60]:
            line = f"- {heading}"
            if outline_chars + len(line) > ARTICLE_OVERVIEW_OUTLINE_CHARS:
                break
            outline.append(line)
            outline_chars += len(line) + 1
        if outline:
            parts.append("Article outline:")
            parts.extend(outline)
    if summary:
        parts.append("Article summary:")
        parts.extend(summary)
    return "\n".join(parts)


def _article_digest(
    title: str,
    headings: list[str],
    paragraphs: list[str],
    max_chars: int = ARTICLE_DIGEST_CHARS,
) -> str:
    """Build a bounded, article-wide digest for overview questions.

    The overview alone (title, outline, TL;DR) is often under ~1,000 tokens,
    which is not enough for a request such as "what is this about, in 1000
    words". This digest keeps the overview and adds the first substantive
    content from every major section, spread across the whole document and
    capped by a character budget. It is used as one synthetic trunk so BM25
    cannot replace it with unrelated generic-word matches.
    """
    overview = _article_overview(title, headings, paragraphs)
    parts = [overview]
    used = len(overview)
    remaining = max_chars - used
    if remaining < 300:
        return overview

    normalized = [" ".join(paragraph.split()) for paragraph in paragraphs]
    anchors = [
        index
        for index, paragraph in enumerate(normalized)
        if ARTICLE_TOP_SECTION_RE.match(paragraph)
        and not ARTICLE_SUBSECTION_RE.match(paragraph)
    ]
    if len(anchors) < 3:
        anchors = [
            index
            for index, paragraph in enumerate(normalized)
            if _article_heading_like(paragraph)
        ]
    anchors = sorted(set(anchors))

    section_budget = ARTICLE_DIGEST_MIN_SECTION_CHARS
    if anchors:
        section_budget = max(
            ARTICLE_DIGEST_MIN_SECTION_CHARS,
            min(
                ARTICLE_DIGEST_MAX_SECTION_CHARS,
                remaining // len(anchors),
            ),
        )

    for position, start in enumerate(anchors):
        if used >= max_chars:
            break
        end = anchors[position + 1] if position + 1 < len(anchors) else len(
            normalized
        )
        block: list[str] = []
        block_chars = 0
        for index in range(start, end):
            paragraph = normalized[index]
            if not paragraph:
                continue
            # Keep subsection headings as structure, but avoid tiny
            # navigation fragments and inline code noise.
            if len(paragraph) < 20 and index != start:
                continue
            if (
                block_chars + len(paragraph) > section_budget
                and block
            ):
                break
            block.append(paragraph)
            block_chars += len(paragraph) + 1
            if block_chars >= section_budget:
                break
        if block:
            section = "\n".join(block)
            parts.append(section)
            used += len(section) + 2

    if not anchors:
        # No usable headings: sample substantive paragraphs evenly across the
        # document so the digest still represents its whole span.
        content_indexes = [
            index
            for index, paragraph in enumerate(normalized)
            if len(paragraph) >= 80 and not _article_heading_like(paragraph)
        ]
        if content_indexes:
            slots = max(1, remaining // 400)
            step = max(1, len(content_indexes) // slots)
            for index in content_indexes[::step]:
                paragraph = normalized[index]
                if used + len(paragraph) + 2 > max_chars:
                    break
                parts.append(paragraph)
                used += len(paragraph) + 2
                if used >= max_chars:
                    break

    digest = "\n\n".join(parts)
    return digest[:max_chars]


def _normalize_article_text(text: str) -> str:
    return " ".join(text.split())


def _article_section_map(
    paragraphs: list[str], headings: list[str], title: str
) -> list[dict]:
    """Record each paragraph's nearest preceding heading and section span.

    Headings are matched against the paragraphs produced by the normal
    HTML/Markdown extractor. Numbered headings are also accepted as a fallback
    for documents whose markup does not use ``<h1>``-``<h6>`` tags. The result
    is persisted next to the article trunks, so parent-section expansion does
    not need to re-parse the document for every question.
    """
    heading_keys = {
        _normalize_article_text(heading).casefold()
        for heading in headings
        if _normalize_article_text(heading)
    }
    sections: list[dict] = []
    current: dict | None = None

    for index, paragraph in enumerate(paragraphs, start=1):
        normalized = _normalize_article_text(paragraph)
        # Markdown headings keep their ``##`` marker in the paragraph text,
        # while _article_headings() stores the heading without it. Compare
        # both spellings so Markdown sections (and Gutenberg chapter
        # headings promoted to Markdown) are recognized.
        heading_text = ARTICLE_MARKDOWN_HEADING_RE.sub(
            lambda match: match.group("title"), normalized
        )
        is_heading = bool(normalized) and (
            normalized.casefold() in heading_keys
            or heading_text.casefold() in heading_keys
        )
        if (
            not is_heading
            and len(normalized) <= 160
            and ARTICLE_NUMBERED_HEADING_RE.match(normalized)
        ):
            is_heading = True

        if is_heading:
            current = {
                "number": len(sections) + 1,
                "heading": heading_text or normalized,
                "heading_index": index,
                "start": index,
                "end": index,
            }
            sections.append(current)
        elif current is None:
            current = {
                "number": len(sections) + 1,
                "heading": title or "Introduction",
                "heading_index": 0,
                "start": index,
                "end": index,
            }
            sections.append(current)
        else:
            current["end"] = index

    return sections


def _article_section_summary(
    section: dict, paragraphs: list[str]
) -> str:
    """Return a fast, deterministic extractive summary for one section."""
    heading_index = int(section.get("heading_index") or 0)
    start = heading_index + 1 if heading_index else int(section["start"])
    end = int(section["end"])
    chosen: list[str] = []
    used = 0
    for index in range(start, end + 1):
        if index < 1 or index > len(paragraphs):
            continue
        paragraph = _normalize_article_text(paragraphs[index - 1])
        if not paragraph or _article_heading_like(paragraph):
            continue
        if len(paragraph) < 24 and chosen:
            continue
        chosen.append(paragraph)
        used += len(paragraph) + 1
        if used >= ARTICLE_SECTION_SUMMARY_CHARS:
            break
    if not chosen:
        return ""
    summary = "\n".join(chosen)
    if len(summary) > ARTICLE_SECTION_SUMMARY_CHARS:
        summary = summary[:ARTICLE_SECTION_SUMMARY_CHARS].rsplit(" ", 1)[0]
    return summary


def _article_build_structure(
    video_id: str,
    title: str,
    headings: list[str],
    paragraphs: list[str],
) -> dict:
    """Build and return the persisted article structure/summary tree."""
    sections = _article_section_map(paragraphs, headings, title)
    for section in sections:
        section["summary"] = _article_section_summary(section, paragraphs)

    paragraph_map: dict[int, dict] = {}
    for section in sections:
        for index in range(int(section["start"]), int(section["end"]) + 1):
            paragraph_map[index] = {
                "index": index,
                "heading": section["heading"],
                "section": section["number"],
                "section_start": section["start"],
                "section_end": section["end"],
            }

    return {
        "version": ARTICLE_META_VERSION,
        "video_id": video_id,
        "title": title,
        "headings": headings,
        "overview": _article_overview(title, headings, paragraphs),
        "digest": _article_digest(title, headings, paragraphs),
        "sections": sections,
        "paragraph_map": [
            paragraph_map[index] for index in sorted(paragraph_map)
        ],
    }


def _article_structure_is_valid(
    structure: object, video_id: str, paragraph_count: int
) -> bool:
    if not isinstance(structure, dict):
        return False
    if structure.get("version") != ARTICLE_META_VERSION:
        return False
    if structure.get("video_id") != video_id:
        return False
    if not isinstance(structure.get("overview"), str):
        return False
    if not isinstance(structure.get("digest"), str):
        return False
    if not isinstance(structure.get("sections"), list):
        return False
    paragraph_map = structure.get("paragraph_map")
    return isinstance(paragraph_map, list) and len(paragraph_map) == paragraph_count


def _load_article_structure(
    cache_dir: Path, video_id: str, paragraph_count: int
) -> dict | None:
    meta_path = cache_dir / ARTICLE_META_FILENAME
    try:
        structure = json.loads(meta_path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    if not _article_structure_is_valid(structure, video_id, paragraph_count):
        return None
    return structure


def _save_article_structure(cache_dir: Path, structure: dict) -> None:
    persisted = {
        key: value
        for key, value in structure.items()
        if not key.startswith("_")
    }
    try:
        (cache_dir / ARTICLE_META_FILENAME).write_text(
            json.dumps(persisted, ensure_ascii=False, indent=2),
            encoding="utf-8",
        )
    except OSError:
        # The structure is already in memory; a read-only cache must not stop
        # /article from working.
        pass


def _article_add_runtime_indexes(structure: dict) -> dict:
    structure["_section_lookup"] = {
        section["number"]: section for section in structure.get("sections", [])
    }
    structure["_paragraph_lookup"] = {
        item["index"]: item for item in structure.get("paragraph_map", [])
    }
    return structure


def _article_heading_tokens(text: str) -> set[str]:
    return {
        token
        for token in chat.tokenize(text)
        if not token.isdigit() and token not in chat.FUZZY_ANCHOR_STOPWORDS
    }


def _article_match_section(question: str, structure: dict | None) -> int | None:
    """Return the section whose heading best matches the question, if any."""
    if not structure:
        return None
    question_text = question.casefold()
    question_tokens = _article_heading_tokens(question)
    best: tuple[float, int] | None = None
    for section in structure.get("sections", []):
        if int(section.get("end", 0)) <= int(
            section.get("heading_index") or section.get("start", 0)
        ):
            continue
        heading = str(section.get("heading", ""))
        heading_key = heading.casefold().strip()
        if not heading_key:
            continue
        heading_tokens = _article_heading_tokens(heading)
        score = 0.0
        if len(heading_key) >= 4 and heading_key in question_text:
            score = 1.5
        elif heading_tokens:
            overlap = question_tokens & heading_tokens
            coverage = len(overlap) / len(heading_tokens)
            if len(overlap) >= 1 and coverage >= 0.6:
                score = coverage + min(0.4, len(overlap) / 10)
        if score <= 0:
            continue
        if best is None or score > best[0]:
            best = (score, int(section["number"]))
    return best[1] if best is not None else None


def _article_classify_with_model(
    question: str, structure: dict | None
) -> ArticleRoute | None:
    """Ask the small model to disambiguate an uncertain article question."""
    if not structure:
        return None
    usable = [
        section
        for section in structure.get("sections", [])
        if int(section.get("end", 0))
        > int(section.get("heading_index") or section.get("start", 0))
    ]
    if len(usable) < 2:
        return None
    lines = []
    for section in usable[:80]:
        heading = str(section.get("heading", "")).splitlines()[0][:160]
        lines.append(f"{section['number']}. {heading}")
    prompt = ARTICLE_ROUTER_PROMPT.format(
        sections="\n".join(lines), question=question
    )
    try:
        response = chat.generate_text(
            prompt,
            model=ARTICLE_ROUTER_MODEL,
            temperature=0.0,
            num_predict=32,
            num_ctx=chat.ANSWER_NUM_CTX,
            stop=["\n"],
        )
    except RuntimeError:
        return None
    cleaned = chat.THINK_PATTERN.sub("", response).strip().upper()
    if cleaned.startswith("OVERVIEW"):
        return ArticleRoute("overview", source="model")
    match = re.search(r"SECTION\s+(\d+)", cleaned)
    if match:
        number = int(match.group(1))
        if number in structure.get("_section_lookup", {}):
            return ArticleRoute("section", number, source="model")
    if "NARROW" in cleaned:
        return ArticleRoute("narrow", source="model")
    return None


def article_question_route(
    question: str,
    structure: dict | None,
    iterative_default: bool = True,
) -> ArticleRoute:
    """Route an article question to overview, section, or narrow retrieval.

    The existing regex rules remain the final fallback. The small model is only
    consulted when the question looks section-oriented but no heading matches;
    a failed or unparseable model response therefore behaves exactly like the
    previous ``article_overview_question`` implementation.
    """
    if article_overview_question(question):
        return ArticleRoute("overview", source="regex")

    enumerative = bool(ARTICLE_ENUMERATIVE_RE.search(question))
    iterative = iterative_default or enumerative
    section = _article_match_section(question, structure)
    if section is not None and not enumerative:
        return ArticleRoute("section", section, False, "heading")

    if (
        section is None
        and not enumerative
        and structure
        and ARTICLE_SECTION_INTENT_RE.search(question)
    ):
        classified = _article_classify_with_model(question, structure)
        if classified is not None:
            if classified.label == "overview":
                return classified
            if classified.label == "section":
                return ArticleRoute(
                    "section", classified.section, False, "model"
                )
            if classified.label == "narrow":
                return ArticleRoute("narrow", None, iterative, "model")
    return ArticleRoute("narrow", None, iterative, "regex")


def _article_section_summary_index(section_number: int) -> int:
    return ARTICLE_SECTION_SUMMARY_INDEX_BASE - section_number


def _article_source_section(
    structure: dict | None, chunk_index: int
) -> dict | None:
    if not structure:
        return None
    section_lookup = structure.get("_section_lookup", {})
    if chunk_index <= ARTICLE_SECTION_SUMMARY_INDEX_BASE:
        return section_lookup.get(
            ARTICLE_SECTION_SUMMARY_INDEX_BASE - chunk_index
        )
    item = structure.get("_paragraph_lookup", {}).get(chunk_index)
    if not item:
        return None
    return section_lookup.get(item["section"])


def _make_article_expansion(
    structure: dict | None,
    chunks: list[chat.Chunk],
    budget: int,
    log: Callable[[str], None],
) -> tuple[Callable, dict]:
    """Return a parent-section/neighbor expansion callback for chat.answer."""
    by_index = {chunk.index: chunk for chunk in chunks}
    stats: dict = {"summaries": 0, "neighbors": 0, "added": 0}

    def prepare(
        selected: list[tuple[chat.Chunk, float]],
        _raw_selected: list[tuple[chat.Chunk, float]],
    ) -> list[tuple[chat.Chunk, float]]:
        if not structure:
            return selected
        result: list[tuple[chat.Chunk, float]] = []
        seen: set[int] = set()
        used = 0

        def add(chunk: chat.Chunk | None, score: float) -> bool:
            nonlocal used
            if chunk is None or chunk.index in seen:
                return False
            text = chat.paragraph_text(chunk)
            if not text:
                return False
            tokens = chat.estimate_tokens(text) + 16
            if result and used + tokens > budget:
                return False
            seen.add(chunk.index)
            result.append((chunk, score))
            used += tokens
            return True

        # Parent summaries first, so the model sees the heading that gives each
        # paragraph its meaning. Limit this to the strongest few matches.
        summary_sections: list[int] = []
        for chunk, _score in selected[:4]:
            item = structure.get("_paragraph_lookup", {}).get(chunk.index)
            if item and item["section"] not in summary_sections:
                summary_sections.append(item["section"])
        for number in summary_sections:
            summary_chunk = by_index.get(_article_section_summary_index(number))
            if add(summary_chunk, 0.0):
                stats["summaries"] += 1

        for chunk, score in selected:
            add(chunk, score)

        for chunk, _score in selected:
            item = structure.get("_paragraph_lookup", {}).get(chunk.index)
            if not item:
                continue
            # Neighbor expansion is deliberately local: one paragraph on each
            # side inside the same section, never the whole section.
            for neighbor_index in (chunk.index - 1, chunk.index + 1):
                neighbor_item = structure.get("_paragraph_lookup", {}).get(
                    neighbor_index
                )
                if not neighbor_item or neighbor_item["section"] != item["section"]:
                    continue
                if add(by_index.get(neighbor_index), 0.0):
                    stats["neighbors"] += 1

        stats["added"] = max(0, len(result) - len(selected))
        if stats["added"]:
            log(
                "[article: parent-section expansion added "
                f"{stats['summaries']} section summary(ies) and "
                f"{stats['neighbors']} neighbor paragraph(s)]"
            )
        return result or selected

    return prepare, stats


def _article_excerpts_for_check(
    evidence: list[tuple[chat.Chunk, float]], max_chars: int = 6000
) -> str:
    lines: list[str] = []
    used = 0
    for number, (chunk, _score) in enumerate(evidence, start=1):
        text = _normalize_article_text(chat.paragraph_text(chunk))
        if not text:
            continue
        snippet = text
        remaining = max_chars - used
        if remaining <= 0:
            break
        if len(snippet) > remaining:
            snippet = snippet[:remaining].rsplit(" ", 1)[0]
        lines.append(f"[{number}] {snippet}")
        used += len(snippet) + 8
    return "\n".join(lines)


def _article_sufficiency_check(
    question: str, evidence: list[tuple[chat.Chunk, float]]
) -> dict:
    """Ask the small model whether the gathered evidence answers the question."""
    excerpts = _article_excerpts_for_check(evidence)
    if not excerpts:
        return {"sufficient": False, "missing": ""}
    prompt = ARTICLE_SUFFICIENCY_PROMPT.format(
        question=question, excerpts=excerpts
    )
    try:
        response = chat.generate_text(
            prompt,
            model=ARTICLE_ROUTER_MODEL,
            temperature=0.0,
            num_predict=64,
            num_ctx=chat.ANSWER_NUM_CTX,
            stop=["\n\n"],
        )
    except RuntimeError:
        return {"sufficient": False, "missing": ""}
    cleaned = chat.THINK_PATTERN.sub("", response)
    verdict = re.search(r"VERDICT\s*:\s*(YES|NO)", cleaned, re.IGNORECASE)
    citation = re.search(r"EVIDENCE\s*:[^\n]*\d", cleaned, re.IGNORECASE)
    missing_match = re.search(r"MISSING\s*:\s*(.+)", cleaned, re.IGNORECASE)
    missing = missing_match.group(1).strip() if missing_match else ""
    if missing.upper().startswith("NONE"):
        missing = ""
    sufficient = bool(
        verdict
        and verdict.group(1).upper() == "YES"
        and citation is not None
    )
    return {"sufficient": sufficient, "missing": missing[:300]}


_PARAGRAPH_CHUNKS_CACHE: dict[int, tuple[list[chat.Chunk], list[chat.Chunk]]] = {}


def _is_article_heading_chunk(chunk: chat.Chunk) -> bool:
    """Return True when a learned-article chunk is just a section heading.

    Article trunks preserve headings as their own paragraph (Wikivoyage's
    ``Eat``, ``Sleep``, ``By plane``, and so on). A heading is a structural
    label, not a knowledge paragraph, and BM25's length normalization gives
    such a tiny document a very high score. For example, "what can i eat in
    Izumo" ranked the one-word ``Eat`` heading far above the soba and
    restaurant paragraphs it introduces, so the answer never contained them.
    Keep headings out of paragraph retrieval and let the body paragraphs,
    which repeat the useful section vocabulary, answer the question.
    """
    text = " ".join(chat.paragraph_text(chunk).split())
    return bool(text) and _article_heading_like(text)


def _iterative_paragraph_chunks(chunks: list[chat.Chunk]) -> list[chat.Chunk]:
    """Return a stable paragraph-only list for BM25 indexing.

    ``get_bm25_index`` caches by list identity. Building a fresh
    ``[chunk for chunk in chunks if chunk.index > 0]`` list on every question
    defeated that cache and rebuilt the whole corpus index (~2 seconds) each
    time. Keep one filtered list per source corpus instead. Standalone article
    headings are excluded because they are section labels, not evidence.
    """
    if not any(chunk.index <= 0 for chunk in chunks):
        # YouTube corpora contain only paragraph chunks, so no copy is needed.
        return chunks
    key = id(chunks)
    entry = _PARAGRAPH_CHUNKS_CACHE.get(key)
    if entry is not None and entry[0] is chunks:
        return entry[1]
    # Annotate article paragraphs with their enclosing section heading before
    # dropping the heading chunks. The heading then contributes to BM25 for
    # the section body without becoming evidence itself. For example, the
    # "Eat" heading makes the Izumo soba and Hara paragraphs match "what can i
    # eat in Izumo" even though those paragraphs do not repeat the word
    # "eat".
    chat.annotate_section_headings(chunks)
    filtered = [
        chunk
        for chunk in chunks
        if chunk.index > 0 and not _is_article_heading_chunk(chunk)
    ]
    _PARAGRAPH_CHUNKS_CACHE[key] = (chunks, filtered)
    while len(_PARAGRAPH_CHUNKS_CACHE) > 4:
        _PARAGRAPH_CHUNKS_CACHE.pop(next(iter(_PARAGRAPH_CHUNKS_CACHE)))
    return filtered


def _evidence_covers_question(
    question: str, evidence: list[tuple[chat.Chunk, float]]
) -> bool:
    """Cheap check: does the gathered evidence mention every question term?

    The small-model sufficiency check costs more than the rest of a simple
    retrieval round. When the evidence already contains every content word
    from the question, there is no reason to ask the model. A miss (including
    a synonym or a misspelling) still falls back to the model, so this is a
    conservative fast path. Heading-only chunks do not count: a question word
    that appears only in the label ``Eat`` is not evidence that the gathered
    paragraphs describe what to eat.
    """
    if not evidence:
        return False
    question_terms = {
        token
        for token in chat.tokenize(question)
        if len(token) > 1 and token not in chat.FUZZY_ANCHOR_STOPWORDS
    }
    if not question_terms:
        return False
    substantive = [
        chunk
        for chunk, _score in evidence
        if not _is_article_heading_chunk(chunk)
    ]
    if not substantive:
        return False
    evidence_text = " ".join(
        chat.paragraph_text(chunk) for chunk in substantive
    )
    evidence_terms = set(chat.tokenize(evidence_text))
    return question_terms.issubset(evidence_terms)


def _iterative_evidence(
    question: str,
    chunks: list[chat.Chunk],
    budget: int,
    log: Callable[[str], None],
    *,
    label: str,
    structure: dict | None = None,
    max_rounds: int = 4,
    batch: int = 4,
    candidate_limit: int = 50,
    require_sufficient: bool = False,
    accept_insufficient: bool = False,
    apply_filter: bool = True,
    check_every: int = 1,
) -> list[tuple[chat.Chunk, float]]:
    """Gather evidence in small rounds, stopping when it is sufficient.

    This is the local analogue of ``chat_gradual_context.py``'s loop: retrieve
    a few new paragraphs, ask the small model whether they answer the question,
    and search again with the missing terms. It is capped by both the number of
    rounds and a hard evidence-token budget. ``/article`` adds section summaries
    through ``structure``.
    """
    seen: set[tuple[str, int]] = set()
    evidence: list[tuple[chat.Chunk, float]] = []
    used = 0
    search_question = question
    sufficient = False
    paragraph_chunks = _iterative_paragraph_chunks(chunks)
    # The original question does not change between rounds, so expand it once.
    # Later rounds append the sufficiency checker's ``missing`` terms to the
    # BM25 query, which already gives those terms full weight; expanding the
    # whole question again only repeated a small-model call. ``expand_query``
    # is also cached, so /article and normal retrieval reuse this result.
    try:
        expansion = chat.expand_query(question)
    except RuntimeError:
        expansion = ""
    filter_done = False
    previous_missing = ""

    for round_number in range(1, max_rounds + 1):
        if used >= budget:
            break
        ranked = chat.top_chunks(
            search_question,
            paragraph_chunks,
            limit=candidate_limit,
            expansion=expansion,
        )
        candidates = [
            item
            for item in ranked
            if (item[0].video_id, item[0].index) not in seen
        ]
        if not candidates:
            break

        # Run the small relevance filter once, when the candidate pool is in
        # its effective range, instead of on every round.
        if (
            apply_filter
            and not filter_done
            and chat.FILTER_MIN_CANDIDATES
            <= len(candidates)
            <= chat.FILTER_MAX_CANDIDATES
        ):
            filter_done = True
            filtered = chat.filter_candidates(
                search_question, candidates[: chat.FILTER_MAX_CANDIDATES]
            )
            if filtered:
                filtered_ids = {id(chunk) for chunk, _score in filtered}
                candidates = filtered + [
                    item for item in candidates
                    if id(item[0]) not in filtered_ids
                ]

        added = 0
        # Gather a wider first batch. A sufficiency check is the most
        # expensive step in the loop, so front-loading a few extra paragraphs
        # often answers the question after one check instead of two or three.
        round_batch = batch * 2 if round_number == 1 else batch
        for chunk, score in candidates:
            key = (chunk.video_id, chunk.index)
            if key in seen:
                continue
            tokens = chat.estimate_tokens(chat.paragraph_text(chunk)) + 16
            if evidence and used + tokens > budget:
                continue
            evidence.append((chunk, score))
            seen.add(key)
            used += tokens
            added += 1
            if added >= round_batch:
                break
        if not added:
            break
        log(
            f"[{label} iterative: round {round_number}/{max_rounds}, "
            f"added {added} paragraph(s), evidence ~{used:,} token(s)]"
        )
        # Fast path: for a simple question, evidence that already contains all
        # of the question's content words is sufficient by inspection. This
        # skips a small-model call on most direct fact questions.
        if not chat.LIST_QUERY_PATTERN.search(question) and (
            _evidence_covers_question(question, evidence)
        ):
            sufficient = True
            log(
                f"[{label} iterative: evidence covers the question terms; "
                "stopping]"
            )
            break
        # Checking sufficiency is the expensive step. Enumerative questions
        # need several rounds of coverage, so checking every other round
        # avoids repeating the small-model call while still stopping as soon
        # as the growing evidence is judged sufficient (or at the last round).
        if (
            check_every > 1
            and round_number % check_every != 0
            and round_number != max_rounds
        ):
            continue
        verdict = _article_sufficiency_check(question, evidence)
        if verdict["sufficient"]:
            sufficient = True
            log(f"[{label} iterative: evidence is sufficient; stopping]")
            break
        if verdict["missing"]:
            missing_key = verdict["missing"].strip().lower()
            if round_number > 1 and missing_key == previous_missing:
                # The last round did not change what the model says is
                # missing, so another identical search will not help.
                log(
                    f"[{label} iterative: missing terms did not change; "
                    "stopping early]"
                )
                break
            previous_missing = missing_key
            search_question = f"{question} {verdict['missing']}"

    if require_sufficient and not sufficient:
        if not accept_insufficient:
            # The gathered paragraphs never answered the question. Fall back
            # to the normal retrieval path instead of forcing unrelated
            # evidence into the prompt.
            log(
                f"[{label} iterative: evidence stayed insufficient after "
                f"{max_rounds} round(s); using normal retrieval]"
            )
            return []
        # The checker is intentionally conservative (especially for list
        # questions). We already have question-ranked paragraphs, so use them
        # and let the answer model decide. Falling back to a second BM25 pass
        # repeated roughly two seconds of work and replaced broader evidence
        # with a much smaller context.
        log(
            f"[{label} iterative: checker still wanted more after "
            f"{max_rounds} round(s); using the {len(evidence)} gathered "
            "paragraph(s)]"
        )

    if not evidence or not structure:
        return evidence

    # Add one parent summary per section represented in the evidence, keeping
    # the final context in document order for coherent synthesis.
    final: list[tuple[chat.Chunk, float]] = []
    seen_summaries: set[int] = set()
    by_index = {chunk.index: chunk for chunk in chunks}
    for chunk, score in sorted(evidence, key=lambda item: item[0].index):
        item = structure.get("_paragraph_lookup", {}).get(chunk.index)
        if item and item["section"] not in seen_summaries:
            seen_summaries.add(item["section"])
            summary_chunk = by_index.get(
                _article_section_summary_index(item["section"])
            )
            if summary_chunk is not None:
                final.append((summary_chunk, 0.0))
        final.append((chunk, score))

    limited: list[tuple[chat.Chunk, float]] = []
    total = 0
    for chunk, score in final:
        tokens = chat.estimate_tokens(chat.paragraph_text(chunk)) + 16
        if limited and total + tokens > budget:
            continue
        limited.append((chunk, score))
        total += tokens
    return limited


def _article_iterative_evidence(
    question: str,
    chunks: list[chat.Chunk],
    structure: dict | None,
    budget: int,
    log: Callable[[str], None],
) -> list[tuple[chat.Chunk, float]]:
    """Run iterative evidence gathering for the active article."""
    return _iterative_evidence(
        question,
        chunks,
        budget,
        log,
        label="article",
        structure=structure,
        max_rounds=ARTICLE_ITERATIVE_MAX_ROUNDS,
        batch=ARTICLE_ITERATIVE_BATCH,
        candidate_limit=ARTICLE_ITERATIVE_CANDIDATES,
    )


def _scoped_context_fallback(
    chunks: list[chat.Chunk],
    budget: int,
) -> list[chat.Chunk]:
    """Return a scoped corpus in reading order within the context budget.

    This is the last line of defence for an isolated session (a learned news
    story, one transcript, or one book). A question can have no BM25 overlap
    with an otherwise useful article; returning no context made the modal's
    chat appear to do nothing at all. Keeping the overview/first chunks plus
    as many following chunks as fit gives the answer model real source text
    while still respecting the context window.
    """
    if not chunks:
        return []
    if budget is None or budget <= 0:
        budget = YOUTUBE_ITERATIVE_EVIDENCE_TOKENS
    ordered = sorted(chunks, key=lambda chunk: (chunk.index, chunk.video_id))
    selected: list[chat.Chunk] = []
    for chunk in ordered:
        trial = selected + [chunk]
        context = chat.build_context(trial)
        if selected and chat.estimate_tokens(context) > budget:
            continue
        selected = trial
        if chat.estimate_tokens(context) >= budget:
            break
    return selected


def _youtube_iterative_evidence(
    question: str,
    chunks: list[chat.Chunk],
    budget: int,
    log: Callable[[str], None],
    depth: int | None = None,
    cache: dict | None = None,
) -> list[tuple[chat.Chunk, float]]:
    """Iteratively gather YouTube transcript evidence for a normal question.

    ``/article`` already answers by retrieving a few paragraphs at a time and
    asking the small model whether the evidence is sufficient. All transcript
    questions use the same loop here: the global knowledge base is searched in
    rounds, and retrieval stops as soon as the gathered paragraphs can answer
    the whole question. List/enumeration questions get more rounds and a larger
    budget because they need broader coverage.

    ``cache`` stores the evidence for the current question so repeated scopes
    can reuse it instead of rerunning expansion, BM25, and the small-model
    sufficiency loop.
    """
    enumerative = bool(chat.LIST_QUERY_PATTERN.search(question))
    effective_budget = budget
    if effective_budget <= 0:
        effective_budget = YOUTUBE_ITERATIVE_EVIDENCE_TOKENS
    if enumerative:
        effective_budget = max(effective_budget, chat.LIST_CONTEXT_TOKENS)
    candidate_limit = (
        depth if depth is not None and depth > 0 else YOUTUBE_ITERATIVE_CANDIDATES
    )
    # Keep the generic relevance filter to remove off-topic BM25 matches.
    base_apply_filter = not enumerative
    cache_key = (
        question,
        id(chunks),
        effective_budget,
        candidate_limit,
        enumerative,
        base_apply_filter,
    )
    base = cache.get(cache_key) if cache is not None else None
    if base is None:
        base = _iterative_evidence(
            question,
            chunks,
            effective_budget,
            log,
            label="youtube",
            max_rounds=(
                YOUTUBE_ITERATIVE_ENUMERATIVE_ROUNDS
                if enumerative
                else YOUTUBE_ITERATIVE_MAX_ROUNDS
            ),
            batch=(
                YOUTUBE_ITERATIVE_ENUMERATIVE_BATCH
                if enumerative
                else YOUTUBE_ITERATIVE_BATCH
            ),
            candidate_limit=candidate_limit,
            require_sufficient=True,
            # A conservative "not sufficient" verdict must not throw away
            # question-ranked evidence and trigger a second retrieval pass.
            accept_insufficient=True,
            apply_filter=base_apply_filter,
            check_every=2 if enumerative else 1,
        )
        if cache is not None:
            cache[cache_key] = base

    return base


def _decode_article_bytes(data: bytes) -> str:
    """Decode a local article without assuming a single text encoding."""
    if data.startswith((b"\xff\xfe", b"\xfe\xff")):
        return data.decode("utf-16")
    for encoding in ("utf-8-sig", "utf-8", "cp1252", "latin-1"):
        try:
            return data.decode(encoding)
        except UnicodeDecodeError:
            continue
    return data.decode("utf-8", errors="replace")


def _article_title(text: str, path: Path) -> str:
    """Return a short title for the article, falling back to its filename."""
    for line in text.splitlines():
        line = line.strip()
        if not line:
            continue
        heading = ARTICLE_MARKDOWN_HEADING_RE.match(line)
        if heading:
            line = heading.group("title").strip()
        if line:
            return line[:200]
    return path.stem


def _load_article_chunks(
    path: Path,
) -> tuple[list[chat.Chunk], str, dict]:
    """Split one article into a cached summary tree and paragraph trunks.

    The path and bytes are content-hashed, so reloading an unchanged article
    reuses the generated structure: document overview, digest, per-section
    extractive summaries, and the section span for every paragraph. Paragraph
    trunks remain the retrieval source of truth; the summaries are synthetic
    context used by the article router.
    """
    data = path.read_bytes()
    raw_text = _decode_article_bytes(data)
    text = raw_text
    if path.suffix.lower() in {".html", ".htm", ".xhtml"}:
        text = ARTICLE_HTML_BLOCK_RE.sub("\n", text)
        text = ARTICLE_HTML_BREAK_RE.sub("\n\n", text)
        text = ARTICLE_HTML_TAG_RE.sub(" ", text)
        text = html.unescape(text)
    text = text.replace("\r\n", "\n").replace("\r", "\n").strip()
    if not text:
        raise ValueError("the article is empty")

    headings = _article_headings(raw_text, text)
    title = _article_title(text, path)
    paragraphs = split_paragraphs(text)
    if not paragraphs:
        raise ValueError("the article contains no readable paragraphs")

    digest = hashlib.sha1(
        str(path).encode("utf-8") + b"\0" + data
    ).hexdigest()[:16]
    video_id = f"article_{digest}"
    cache_dir = ARTICLE_CACHE_DIR / video_id
    cache_dir.mkdir(parents=True, exist_ok=True)
    for old_trunk in cache_dir.glob("*.trunk"):
        old_trunk.unlink()

    # The summary tree is expensive to build and is a pure function of the
    # article bytes, so load it from the content-addressed cache when valid.
    structure = _load_article_structure(
        cache_dir, video_id, len(paragraphs)
    )
    if structure is None:
        structure = _article_build_structure(
            video_id, title, headings, paragraphs
        )
        _save_article_structure(cache_dir, structure)
    _article_add_runtime_indexes(structure)
    overview = str(structure["overview"])
    digest_text = str(structure["digest"])

    # Make the title part of the first paragraph so a broad question such as
    # "what is this about?" still retrieves the article heading. Do this after
    # the section map is built, so paragraph indexes stay aligned.
    if title and title.casefold() not in paragraphs[0].casefold():
        paragraphs[0] = f"{title}\n\n{paragraphs[0]}"

    # Chunk 0 is a compact document overview (title + outline + TL;DR). It
    # gives article-wide questions useful context even when their generic
    # wording ("what is this article about?") has no good BM25 match among
    # thousands of paragraphs.
    overview_trunk = (
        f"<article><title>{xml_escape(title)}</title>"
        f"<paragraph>{xml_escape(overview)}</paragraph></article>"
    )
    overview_path = cache_dir / f"{video_id}.0.trunk"
    overview_path.write_text(overview_trunk + "\n", encoding="utf-8")
    chunks: list[chat.Chunk] = [
        chat.Chunk(video_id=video_id, index=0, trunk_path=overview_path)
    ]

    # Chunk -1 is a broader digest used only for article-wide questions. It
    # is excluded from ordinary BM25 retrieval (see _run_question) so its
    # many headings and terms cannot crowd out question-specific paragraphs.
    digest_trunk = (
        f"<article><title>{xml_escape(title)}</title>"
        f"<paragraph>{xml_escape(digest_text)}</paragraph></article>"
    )
    digest_path = cache_dir / f"{video_id}.-1.trunk"
    digest_path.write_text(digest_trunk + "\n", encoding="utf-8")
    chunks.append(
        chat.Chunk(video_id=video_id, index=-1, trunk_path=digest_path)
    )

    # Per-section summaries are cached in the structure file. Each one becomes
    # a synthetic trunk with a unique negative index so it can be attached as
    # parent context without entering normal BM25 retrieval.
    for section in structure["sections"]:
        summary = str(section.get("summary") or "")
        if not summary:
            continue
        summary_index = _article_section_summary_index(int(section["number"]))
        summary_text = f"{section['heading']}\n{summary}"
        summary_trunk = (
            f"<article><title>{xml_escape(title)}</title>"
            f"<paragraph>{xml_escape(summary_text)}</paragraph></article>"
        )
        summary_path = (
            cache_dir / f"{video_id}.section-{section['number']}.trunk"
        )
        summary_path.write_text(summary_trunk + "\n", encoding="utf-8")
        chunks.append(
            chat.Chunk(
                video_id=video_id,
                index=summary_index,
                trunk_path=summary_path,
            )
        )

    for index, paragraph in enumerate(paragraphs, start=1):
        trunk = (
            f"<article><title>{xml_escape(title)}</title>"
            f"<paragraph>{xml_escape(paragraph)}</paragraph></article>"
        )
        trunk_path = cache_dir / f"{video_id}.{index}.trunk"
        trunk_path.write_text(trunk + "\n", encoding="utf-8")
        chunks.append(
            chat.Chunk(video_id=video_id, index=index, trunk_path=trunk_path)
        )
    # Persist the article's trunks and inverted index together, matching the
    # news learning path. Unchanged chunks are skipped by the file signature.
    try:
        chat.index_chunks_in_sqlite(chunks)
    except Exception:
        pass
    return chunks, title, structure


# The /gutenberg command and its two-column book modal. The catalog search is
# intentionally small and stateless; the per-book chat state is created lazily
# the first time a question is asked, so opening the modal costs only the
# plaintext download. Each book gets at most one on-disk copy and one in-memory
# article index.
GUTENBERG_CATALOG_CACHE: dict[str, dict] = {}
GUTENBERG_DOWNLOAD_LOCKS: dict[str, threading.Lock] = {}
GUTENBERG_DOWNLOAD_LOCK = threading.Lock()


def _first_gutenberg_id(values) -> str:
    """Return the canonical numeric id from an Open Library identifier list."""
    if not isinstance(values, list):
        values = [values]
    for value in values:
        text = str(value or "").strip()
        if GUTENBERG_BOOK_ID_RE.fullmatch(text):
            return str(int(text))
    return ""


def _gutenberg_openlibrary_card(doc: dict) -> dict | None:
    """Normalize one Open Library work into a Gutenberg book card."""
    if not isinstance(doc, dict):
        return None
    book_id = _first_gutenberg_id(doc.get("id_project_gutenberg"))
    if not book_id:
        return None
    title = str(doc.get("title") or f"Book {book_id}").strip()
    authors = ", ".join(
        str(name).strip()
        for name in (doc.get("author_name") or [])
        if str(name).strip()
    )
    subjects = [
        str(value).strip()
        for value in (doc.get("subject") or [])
        if str(value).strip()
    ][:4]
    languages = [
        str(value).strip()
        for value in (doc.get("language") or [])
        if str(value).strip()
    ]
    try:
        first_publish_year = int(doc.get("first_publish_year") or 0)
    except (TypeError, ValueError):
        first_publish_year = 0
    return {
        "id": book_id,
        "title": title,
        "authors": authors,
        "languages": languages,
        "subjects": subjects,
        "first_publish_year": first_publish_year,
        "download_count": 0,
        "url": GUTENBERG_BOOK_URL.format(book_id=book_id),
        "text_url": f"{GUTENBERG_BOOK_URL.format(book_id=book_id)}.txt.utf-8",
        "source": "openlibrary",
    }


def _gutenberg_search_card(block: str) -> dict | None:
    """Normalize one result block from the Project Gutenberg search page."""
    link = GUTENBERG_SEARCH_LINK_RE.search(block)
    if link is None:
        return None
    book_id = str(int(link.group(1)))
    title_match = GUTENBERG_SEARCH_TITLE_RE.search(block)
    author_match = GUTENBERG_SEARCH_AUTHOR_RE.search(block)
    extra_match = GUTENBERG_SEARCH_EXTRA_RE.search(block)
    title = html.unescape(re.sub(r"<[^>]+>", "", title_match.group(1))).strip() if title_match else ""
    authors = html.unescape(re.sub(r"<[^>]+>", "", author_match.group(1))).strip() if author_match else ""
    downloads = 0
    if extra_match:
        extra_text = html.unescape(re.sub(r"<[^>]+>", "", extra_match.group(1)))
        downloads_match = GUTENBERG_SEARCH_DOWNLOADS_RE.search(extra_text)
        if downloads_match:
            downloads = int(downloads_match.group(1).replace(",", ""))
    if not title:
        title = f"Book {book_id}"
    return {
        "id": book_id,
        "title": title,
        "authors": authors,
        "languages": [],
        "subjects": [],
        "first_publish_year": 0,
        "download_count": downloads,
        "url": GUTENBERG_BOOK_URL.format(book_id=book_id),
        "text_url": f"{GUTENBERG_BOOK_URL.format(book_id=book_id)}.txt.utf-8",
        "source": "gutenberg.org",
    }


_NUMBER_ONES = (
    "zero",
    "one",
    "two",
    "three",
    "four",
    "five",
    "six",
    "seven",
    "eight",
    "nine",
    "ten",
    "eleven",
    "twelve",
    "thirteen",
    "fourteen",
    "fifteen",
    "sixteen",
    "seventeen",
    "eighteen",
    "nineteen",
)
_NUMBER_TENS = (
    "",
    "",
    "twenty",
    "thirty",
    "forty",
    "fifty",
    "sixty",
    "seventy",
    "eighty",
    "ninety",
)
_GUTENBERG_STOPWORDS = {
    "a",
    "an",
    "and",
    "as",
    "at",
    "by",
    "for",
    "from",
    "in",
    "of",
    "on",
    "or",
    "the",
    "to",
    "with",
}


def _number_to_words(value: int) -> str:
    """Render a non-negative integer as lowercase English words."""
    if value < 0 or value > 999999999:
        return str(value)

    def under_thousand(number: int) -> list[str]:
        words: list[str] = []
        if number >= 100:
            words.extend((_NUMBER_ONES[number // 100], "hundred"))
            number %= 100
        if number >= 20:
            words.append(_NUMBER_TENS[number // 10])
            number %= 10
        if number or not words:
            words.append(_NUMBER_ONES[number])
        return words

    if value == 0:
        return "zero"
    parts: list[str] = []
    for divisor, label in ((1000000, "million"), (1000, "thousand")):
        if value >= divisor:
            parts.extend(under_thousand(value // divisor))
            parts.append(label)
            value %= divisor
    if value:
        parts.extend(under_thousand(value))
    return " ".join(parts)


def _year_to_words(value: int) -> str:
    """Render a year-like number the way book titles usually spell it."""
    if not 1100 <= value <= 2099:
        return ""
    high, low = divmod(value, 100)
    if low == 0:
        return f"{_number_to_words(high)} hundred"
    return f"{_number_to_words(high)} {_number_to_words(low)}"


def _gutenberg_query_variants(keyword: str) -> list[str]:
    """Return catalogue queries for a free-form book search.

    Project Gutenberg's catalogue search is literal: ``the 39 steps`` does not
    match ``The Thirty-Nine Steps`` even though a reader expects it to. Build a
    few spelling variants (digits as words, year-style numbers, and the query
    without a leading article) so titles written either way remain findable.
    """
    base = " ".join(str(keyword or "").split())
    if not base:
        return []
    variants: list[str] = []
    seen: set[str] = set()

    def add(value: str) -> None:
        value = " ".join(value.split())
        key = value.casefold()
        if value and key not in seen:
            seen.add(key)
            variants.append(value)

    add(base)
    add(
        re.sub(
            r"\b\d{1,6}\b",
            lambda match: _number_to_words(int(match.group(0))),
            base,
        )
    )
    add(
        re.sub(
            r"\b\d{4}\b",
            lambda match: _year_to_words(int(match.group(0)))
            or match.group(0),
            base,
        )
    )
    for value in list(variants):
        add(re.sub(r"^(?:the|a|an)\s+", "", value, flags=re.IGNORECASE))
    return variants[:6]


def _gutenberg_tokens(text: str) -> list[str]:
    text = html.unescape(str(text or "")).casefold()
    return [token for token in re.split(r"[^a-z0-9]+", text) if token]


def _gutenberg_query_term_sets(keyword: str) -> list[set[str]]:
    term_sets: list[set[str]] = []
    seen: set[frozenset[str]] = set()
    for variant in _gutenberg_query_variants(keyword):
        terms = {
            token
            for token in _gutenberg_tokens(variant)
            if token not in _GUTENBERG_STOPWORDS
        }
        if not terms:
            continue
        frozen = frozenset(terms)
        if frozen not in seen:
            seen.add(frozen)
            term_sets.append(terms)
    return term_sets


def _gutenberg_card_relevance(card: dict, keyword: str) -> float:
    """Score a book card against the query and its spelling variants.

    The catalogue can return loosely related books for a literal query (for
    example other John Buchan novels for ``the 39 steps``). Ranking by title
    and author coverage keeps the exact title ahead of those results.
    """
    title_tokens = _gutenberg_tokens(str(card.get("title") or ""))
    author_tokens = _gutenberg_tokens(str(card.get("authors") or ""))
    title_set = set(title_tokens)
    author_set = set(author_tokens)
    title_text = " ".join(title_tokens)
    variants = _gutenberg_query_variants(keyword)
    variant_texts = {" ".join(_gutenberg_tokens(value)) for value in variants}
    best = 0.0
    for terms in _gutenberg_query_term_sets(keyword):
        title_coverage = len(terms & title_set) / len(terms)
        author_coverage = len(terms & author_set) / len(terms)
        score = title_coverage * 6.0 + author_coverage * 1.5
        if terms <= title_set:
            score += 3.0
        if title_text in variant_texts:
            score += 4.0
        best = max(best, score)
    return best


def _combine_gutenberg_cards(
    keyword: str,
    *groups: list[dict],
    limit: int = GUTENBERG_BOOK_RESULT_LIMIT,
) -> list[dict]:
    """Merge book cards from all sources, deduplicate, and rank them."""
    by_id: dict[str, dict] = {}
    order: list[str] = []
    for group in groups:
        for card in group or []:
            if not isinstance(card, dict):
                continue
            book_id = str(card.get("id") or "")
            if not book_id:
                continue
            existing = by_id.get(book_id)
            if existing is None:
                by_id[book_id] = dict(card)
                order.append(book_id)
            else:
                _merge_gutenberg_news_card(existing, card)
    cards = [by_id[book_id] for book_id in order]
    ranked = sorted(
        enumerate(cards),
        key=lambda pair: (-_gutenberg_card_relevance(pair[1], keyword), pair[0]),
    )
    return [card for _index, card in ranked[: max(1, limit)]]


def _openlibrary_search_params(keyword: str) -> dict:
    """Build Open Library search parameters from a free-form query.

    A query such as ``the time machine by h g wells`` maps to the documented
    ``?title=...&author=...`` form. Everything else uses the general ``q``
    search so keywords and subjects still work.
    """
    match = re.match(
        r"^(?P<title>.+?)\s+by\s+(?P<author>.+)$", keyword.strip(), re.IGNORECASE
    )
    if match:
        title = match.group("title").strip()
        author = match.group("author").strip()
        if title and author and len(title) <= 240 and len(author) <= 240:
            return {"title": title, "author": author}
    return {"q": keyword}


def _openlibrary_plain_text(value: object, limit: int = 0) -> str:
    """Flatten an Open Library text field into readable plain text."""
    if isinstance(value, dict):
        value = value.get("value") or value.get("description") or ""
    if isinstance(value, (list, tuple)):
        parts = [_openlibrary_plain_text(item) for item in value]
        value = " ".join(part for part in parts if part)
    text = str(value or "")
    text = text.replace("{{{", "").replace("}}}", "")
    text = re.sub(r"<[^>]+>", " ", text)
    text = html.unescape(text)
    text = " ".join(text.split())
    if limit > 0 and len(text) > limit:
        shortened = text[:limit].rsplit(" ", 1)[0].rstrip(" ,;:-")
        text = (shortened or text[:limit].rstrip()) + "…"
    return text


def _openlibrary_key(value: object) -> str:
    """Return a normalized Open Library key such as /works/OL123W."""
    key = str(value or "").strip()
    if not key:
        return ""
    if key.startswith(("http://", "https://")):
        path = urlparse(key).path
        key = path or ""
    if key and not key.startswith("/"):
        key = "/" + key
    return key


def _openlibrary_first_text(value: object) -> str:
    if isinstance(value, (list, tuple)):
        for item in value:
            text = _openlibrary_plain_text(item)
            if text:
                return text
        return ""
    return _openlibrary_plain_text(value)


def _openlibrary_related_text(hit: dict, keyword: str) -> list[str]:
    """Return the strongest full-text snippets for one search hit."""
    highlight = hit.get("highlight") if isinstance(hit, dict) else None
    raw_snippets = highlight.get("text") if isinstance(highlight, dict) else None
    if not isinstance(raw_snippets, list):
        return []
    terms = {
        token
        for token in re.split(r"[^a-z0-9]+", keyword.casefold())
        if len(token) > 1
    }
    snippets: list[tuple[float, int, str]] = []
    seen: set[str] = set()
    for index, raw in enumerate(raw_snippets):
        text = _openlibrary_plain_text(raw, limit=520)
        if not text or text in seen:
            continue
        seen.add(text)
        tokens = {
            token
            for token in re.split(r"[^a-z0-9]+", text.casefold())
            if len(token) > 1
        }
        coverage = len(terms & tokens) / len(terms) if terms else 0.0
        snippets.append((-coverage, index, text))
    snippets.sort()
    return [text for _coverage, _index, text in snippets[:OPENLIBRARY_HIGHLIGHT_LIMIT]]


def _openlibrary_metadata_description(
    authors: str,
    first_publish_year: int,
    publisher: str,
    subjects: list[str],
) -> str:
    parts: list[str] = []
    if authors:
        parts.append(f"By {authors}")
    if first_publish_year > 0:
        parts.append(f"first published {first_publish_year}")
    if publisher:
        parts.append(f"published by {publisher}")
    if subjects:
        parts.append(f"subjects: {', '.join(subjects[:3])}")
    return " · ".join(parts)


def _openlibrary_doc_card(doc: dict, keyword: str) -> dict | None:
    """Normalize one Open Library catalogue work into a book card.

    Catalogue results carry no full-text excerpts, but they are the only place
    where a title/series phrase such as "the rust programming language" is
    matched as a whole. They are merged with the inside-search hits so books
    about the complete phrase outrank books that merely mention its words.
    """
    if not isinstance(doc, dict):
        return None
    title = str(doc.get("title") or "").strip()
    if not title:
        return None
    work_key = _openlibrary_key(doc.get("key"))
    if not work_key:
        return None
    authors = ", ".join(
        str(name).strip()
        for name in (doc.get("author_name") or [])
        if str(name).strip()
    )
    subjects = [
        str(value).strip()
        for value in (doc.get("subject") or [])
        if str(value).strip()
    ][:5]
    languages = [
        str(value).strip()
        for value in (doc.get("language") or [])
        if str(value).strip()
    ]
    try:
        first_publish_year = int(doc.get("first_publish_year") or 0)
    except (TypeError, ValueError):
        first_publish_year = 0
    try:
        download_count = int(doc.get("download_count") or 0)
    except (TypeError, ValueError):
        download_count = 0
    return {
        "id": work_key.lstrip("/"),
        "title": title,
        "authors": authors,
        "description": _openlibrary_metadata_description(
            authors, first_publish_year, "", subjects
        ),
        "related_text": [],
        "languages": languages,
        "subjects": subjects,
        "first_publish_year": first_publish_year,
        "download_count": download_count,
        "url": OPENLIBRARY_BOOK_PAGE_URL.format(key=work_key),
        "text_url": "",
        "source": "openlibrary",
        "work_key": work_key,
    }


def _openlibrary_hit_card(hit: dict, keyword: str) -> dict | None:
    """Normalize one Open Library full-text search hit into a book card."""
    if not isinstance(hit, dict):
        return None
    edition = hit.get("edition") if isinstance(hit.get("edition"), dict) else {}
    fields = hit.get("fields") if isinstance(hit.get("fields"), dict) else {}

    title = _openlibrary_first_text(
        edition.get("title") or fields.get("meta_title") or ""
    )
    if not title:
        return None

    authors = ", ".join(
        str(author.get("name") or "").strip()
        for author in (edition.get("authors") or [])
        if isinstance(author, dict) and str(author.get("name") or "").strip()
    )
    if not authors:
        authors = _openlibrary_first_text(fields.get("meta_creator"))

    publisher = _openlibrary_first_text(fields.get("meta_publisher"))
    subjects = [
        _openlibrary_plain_text(value)
        for value in (fields.get("meta_subject") or [])
        if _openlibrary_plain_text(value)
    ][:5]

    first_publish_year = 0
    for value in (
        _openlibrary_first_text(fields.get("meta_year")),
        str(edition.get("publish_date") or ""),
        _openlibrary_first_text(fields.get("meta_date")),
    ):
        match = re.search(r"\b(\d{4})\b", value)
        if match:
            first_publish_year = int(match.group(1))
            break

    work_key = _openlibrary_key(edition.get("work_key"))
    edition_key = _openlibrary_key(edition.get("key"))
    page_key = work_key or edition_key
    card_id = (page_key or str(hit.get("_id") or "")).lstrip("/")
    if not card_id:
        return None
    url = (
        OPENLIBRARY_BOOK_PAGE_URL.format(key=page_key)
        if page_key
        else f"https://openlibrary.org/search/inside?q={quote(keyword)}"
    )
    description = _openlibrary_metadata_description(
        authors, first_publish_year, publisher, subjects
    )
    return {
        "id": card_id,
        "title": title,
        "authors": authors,
        "description": description,
        "related_text": _openlibrary_related_text(hit, keyword),
        "languages": [],
        "subjects": subjects,
        "first_publish_year": first_publish_year,
        "download_count": 0,
        "url": url,
        "text_url": "",
        "source": "openlibrary",
        "work_key": work_key,
    }


def _openlibrary_work_description(work_key: str) -> str:
    """Fetch a work description, caching both hits and misses in memory."""
    work_key = _openlibrary_key(work_key)
    if not work_key.startswith("/works/"):
        return ""
    with OPENLIBRARY_DESCRIPTION_CACHE_LOCK:
        if work_key in OPENLIBRARY_DESCRIPTION_CACHE:
            return OPENLIBRARY_DESCRIPTION_CACHE[work_key]
    description = ""
    try:
        response = requests.get(
            OPENLIBRARY_WORK_JSON_URL.format(key=work_key),
            headers={**chat.HTTP_HEADERS, "Accept": "application/json"},
            timeout=(4, 10),
        )
        response.raise_for_status()
        data = response.json()
        if isinstance(data, dict):
            description = _openlibrary_plain_text(
                data.get("description") or data.get("first_sentence") or "",
                limit=OPENLIBRARY_DESCRIPTION_LIMIT,
            )
    except (requests.RequestException, ValueError):
        description = ""
    with OPENLIBRARY_DESCRIPTION_CACHE_LOCK:
        OPENLIBRARY_DESCRIPTION_CACHE[work_key] = description
    return description


def _openlibrary_attach_descriptions(cards: list[dict]) -> None:
    """Add real work descriptions in parallel, keeping metadata fallbacks."""
    pending = [
        (card, str(card.get("work_key") or ""))
        for card in cards
        if str(card.get("work_key") or "").startswith("/works/")
    ]
    if not pending:
        return
    with ThreadPoolExecutor(max_workers=min(8, len(pending))) as executor:
        futures = {
            executor.submit(_openlibrary_work_description, work_key): card
            for card, work_key in pending
        }
        for future in as_completed(futures):
            card = futures[future]
            try:
                description = future.result()
            except Exception:
                description = ""
            if description:
                card["description"] = description
    for card in cards:
        card.pop("work_key", None)


def _openlibrary_card_relevance(card: dict, keyword: str) -> float:
    """Score a book card against the whole query phrase and its variants.

    A full-text hit can match every word separately (for "the rust programming
    language" that includes books about rust the metal or the fungal disease),
    while the catalogue result matches the phrase as a title. Title/author
    coverage and an exact phrase match therefore dominate, and excerpt coverage
    only breaks ties between otherwise similar cards.
    """
    title_tokens = set(_gutenberg_tokens(str(card.get("title") or "")))
    author_tokens = set(_gutenberg_tokens(str(card.get("authors") or "")))
    subject_tokens = set(_gutenberg_tokens(" ".join(card.get("subjects") or [])))
    excerpt_token_sets = [
        set(_gutenberg_tokens(text))
        for text in (card.get("related_text") or [])
    ]
    description_tokens = set(_gutenberg_tokens(str(card.get("description") or "")))
    title_text = " ".join(sorted(title_tokens))
    variant_texts = {
        " ".join(sorted(_gutenberg_tokens(value)))
        for value in _gutenberg_query_variants(keyword)
    }
    best = 0.0
    for terms in _gutenberg_query_term_sets(keyword):
        if not terms:
            continue
        total = len(terms)
        title_coverage = len(terms & title_tokens) / total
        author_coverage = len(terms & author_tokens) / total
        score = title_coverage * 8.0 + author_coverage * 2.0
        if terms <= title_tokens:
            score += 4.0
        if title_text in variant_texts:
            score += 6.0
        if excerpt_token_sets:
            excerpt_coverage = max(
                len(terms & tokens) / total for tokens in excerpt_token_sets
            )
            score += excerpt_coverage * 2.0
        score += len(terms & description_tokens) / total * 1.0
        score += len(terms & subject_tokens) / total * 1.0
        best = max(best, score)
    return best


def _combine_openlibrary_cards(
    keyword: str,
    *groups: list[dict],
    limit: int = OPENLIBRARY_RESULT_LIMIT,
) -> list[dict]:
    """Merge inside-search and catalogue cards, then rank by phrase match."""
    by_id: dict[str, dict] = {}
    order: list[str] = []
    for group in groups:
        for card in group or []:
            if not isinstance(card, dict):
                continue
            card_id = str(card.get("id") or "")
            if not card_id:
                continue
            existing = by_id.get(card_id)
            if existing is None:
                by_id[card_id] = dict(card)
                order.append(card_id)
                continue
            if len(str(card.get("description") or "")) > len(
                str(existing.get("description") or "")
            ):
                existing["description"] = card["description"]
            for field in ("authors", "title"):
                if not existing.get(field) and card.get(field):
                    existing[field] = card[field]
            if not existing.get("first_publish_year") and card.get("first_publish_year"):
                existing["first_publish_year"] = card["first_publish_year"]
            if not existing.get("work_key") and card.get("work_key"):
                existing["work_key"] = card["work_key"]
            for field in ("languages", "subjects"):
                merged = list(existing.get(field) or [])
                for value in card.get(field) or []:
                    if value not in merged:
                        merged.append(value)
                existing[field] = merged
            merged_text = list(existing.get("related_text") or [])
            for snippet in card.get("related_text") or []:
                if snippet not in merged_text:
                    merged_text.append(snippet)
            existing["related_text"] = merged_text[:OPENLIBRARY_HIGHLIGHT_LIMIT]
    cards = [by_id[card_id] for card_id in order]
    ranked = sorted(
        enumerate(cards),
        key=lambda pair: (-_openlibrary_card_relevance(pair[1], keyword), pair[0]),
    )
    return [card for _index, card in ranked[: max(1, limit)]]


def _fetch_openlibrary_inside(keyword: str, limit: int) -> list[dict]:
    """Fetch and normalize the full-text (inside) search hits."""
    try:
        response = requests.get(
            OPENLIBRARY_INSIDE_SEARCH_URL,
            params={"q": keyword, "limit": max(20, limit * 3)},
            headers={**chat.HTTP_HEADERS, "Accept": "application/json"},
            timeout=(10, 40),
        )
        response.raise_for_status()
        data = response.json()
    except (requests.RequestException, ValueError) as exc:
        raise RuntimeError(f"Open Library search failed: {exc}") from exc

    hits = data.get("hits") if isinstance(data, dict) else None
    raw_hits = hits.get("hits") if isinstance(hits, dict) else None
    if not isinstance(raw_hits, list):
        raise RuntimeError("Open Library returned an unexpected response.")

    cards: list[dict] = []
    for hit in raw_hits:
        card = _openlibrary_hit_card(hit, keyword)
        if card is not None:
            cards.append(card)
    return cards


def _fetch_openlibrary_metadata(keyword: str, limit: int) -> list[dict]:
    """Fetch catalogue works whose title/author matches the whole query."""
    params = _openlibrary_search_params(keyword)
    params["fields"] = OPENLIBRARY_SEARCH_FIELDS
    params["limit"] = max(20, limit * 3)
    try:
        response = requests.get(
            OPENLIBRARY_SEARCH_URL,
            params=params,
            headers={**chat.HTTP_HEADERS, "Accept": "application/json"},
            timeout=(10, 30),
        )
        response.raise_for_status()
        data = response.json()
    except (requests.RequestException, ValueError) as exc:
        raise RuntimeError(f"Open Library catalogue search failed: {exc}") from exc
    docs = data.get("docs") if isinstance(data, dict) else None
    if not isinstance(docs, list):
        return []
    cards: list[dict] = []
    for doc in docs:
        card = _openlibrary_doc_card(doc, keyword)
        if card is not None:
            cards.append(card)
    return cards


def search_openlibrary_inside(
    keyword: str, limit: int = OPENLIBRARY_RESULT_LIMIT
) -> list[dict]:
    """Search Open Library books and return cards for the whole query.

    Full-text (inside) hits supply matching passages, while the catalogue
    search supplies works whose title/author matches the complete phrase (for
    example "the rust programming language"). Both sources are merged, ranked
    by phrase/title relevance, and the remaining work descriptions are fetched
    in parallel.
    """
    keyword = " ".join(str(keyword or "").split())
    if not keyword:
        return []
    limit = max(1, min(int(limit), 50))

    inside_cards: list[dict] = []
    metadata_cards: list[dict] = []
    errors: list[str] = []
    with ThreadPoolExecutor(max_workers=2) as executor:
        futures = {
            executor.submit(_fetch_openlibrary_inside, keyword, limit): "inside",
            executor.submit(_fetch_openlibrary_metadata, keyword, limit): "metadata",
        }
        for future in as_completed(futures):
            try:
                cards = future.result()
            except RuntimeError as exc:
                errors.append(str(exc))
                continue
            if futures[future] == "inside":
                inside_cards = cards
            else:
                metadata_cards = cards
    if not inside_cards and not metadata_cards and errors:
        raise RuntimeError("; ".join(errors))

    cards = _combine_openlibrary_cards(
        keyword, metadata_cards, inside_cards, limit=limit
    )
    _openlibrary_attach_descriptions(cards)
    for card in cards:
        if not card.get("description"):
            card["description"] = f"Open Library match for {keyword!r}."
    return cards


def _search_gutenberg_openlibrary(
    keyword: str, limit: int = GUTENBERG_BOOK_RESULT_LIMIT
) -> list[dict]:
    """Resolve Gutenberg text identifiers through Open Library's search API."""
    params = _openlibrary_search_params(keyword)
    params["fields"] = OPENLIBRARY_SEARCH_FIELDS
    # Open Library returns works, and not every work has a linked Gutenberg
    # identifier. Ask for a wider page so there are enough usable cards left
    # after filtering.
    params["limit"] = max(40, max(1, limit) * 5)
    try:
        response = requests.get(
            OPENLIBRARY_SEARCH_URL,
            params=params,
            headers=chat.HTTP_HEADERS,
            timeout=(10, 30),
        )
        response.raise_for_status()
        data = response.json()
    except (requests.RequestException, ValueError) as exc:
        raise RuntimeError(f"Open Library search failed: {exc}") from exc
    docs = data.get("docs") if isinstance(data, dict) else None
    if not isinstance(docs, list):
        raise RuntimeError("Open Library returned an unexpected response.")
    items: list[dict] = []
    seen: set[str] = set()
    for doc in docs:
        card = _gutenberg_openlibrary_card(doc)
        if card is None or card["id"] in seen:
            continue
        seen.add(card["id"])
        GUTENBERG_CATALOG_CACHE[card["id"]] = card
        items.append(card)
        if len(items) >= max(1, limit):
            break
    return items


def _search_gutenberg_gutenberg_org(
    keyword: str, limit: int = GUTENBERG_BOOK_RESULT_LIMIT
) -> list[dict]:
    """Search the Project Gutenberg catalogue page for one query.

    Open Library is authoritative for identifiers it has linked, but some
    public-domain works (for example early H. G. Wells editions) have no
    `id_project_gutenberg` value yet. The catalogue search page still returns
    their ebook numbers, so parse those cards without another third-party API.
    """
    try:
        response = requests.get(
            GUTENBERG_SEARCH_URL,
            params={"query": keyword},
            headers=chat.HTTP_HEADERS,
            timeout=(5, 20),
        )
        response.raise_for_status()
        page = response.text
    except requests.RequestException as exc:
        raise RuntimeError(f"Project Gutenberg search failed: {exc}") from exc
    items: list[dict] = []
    seen: set[str] = set()
    for block in GUTENBERG_SEARCH_BLOCK_RE.findall(page):
        card = _gutenberg_search_card(block)
        if card is None or card["id"] in seen:
            continue
        seen.add(card["id"])
        GUTENBERG_CATALOG_CACHE[card["id"]] = card
        items.append(card)
        if len(items) >= max(1, limit):
            break
    return items


def search_gutenberg_books(keyword: str, limit: int = GUTENBERG_BOOK_RESULT_LIMIT) -> list[dict]:
    """Search for Project Gutenberg books and return normalized book cards.

    Open Library is the primary catalog: each matching work contributes its
    ``id_project_gutenberg`` identifier. Because not every work is linked to a
    Gutenberg text there yet, the Project Gutenberg catalogue search fills in
    the missing cards. Every catalogue spelling variant is consulted, and the
    merged results are ranked against the original query so an exact title is
    not pushed out by loosely related matches.
    """
    keyword = keyword.strip()
    if not keyword:
        return []
    limit = max(1, limit)
    items: list[dict] = []
    errors: list[str] = []

    try:
        items.extend(_search_gutenberg_openlibrary(keyword, limit))
    except RuntimeError as exc:
        errors.append(str(exc))

    # Search the catalogue even when Open Library returned a full page: its
    # literal search misses digit/word title spellings, so a variant may hold
    # the exact book while the other sources only returned related works.
    for variant in _gutenberg_query_variants(keyword):
        try:
            items.extend(
                _search_gutenberg_gutenberg_org(
                    variant, max(limit, GUTENBERG_BOOK_RESULT_LIMIT)
                )
            )
        except RuntimeError as exc:
            errors.append(str(exc))

    merged = _combine_gutenberg_cards(keyword, items, limit=limit)
    if not merged and errors:
        raise RuntimeError("; ".join(errors))
    return merged


def _gutenberg_news_book_id(link: str) -> str:
    """Extract a Gutenberg ebook id from a resolved Google News link."""
    match = GUTENBERG_NEWS_BOOK_LINK_RE.search(link or "")
    if match is None:
        return ""
    value = next((group for group in match.groups() if group), "")
    return str(int(value)) if value else ""


def _gutenberg_card_from_news_item(item: dict) -> dict | None:
    """Turn one resolved Google News story into a normalized book card.

    Google News indexes the public Gutenberg ebook pages, including their
    ``/ebooks/<id>``, ``/cache/epub/<id>/`` and ``/files/<id>/`` URLs. Only
    those links are books; category, author-search and site-homepage hits are
    ignored. Titles commonly look like "Persuasion by Jane Austen - Project
    Gutenberg", so the site suffix and author are split out when present.
    """
    if not isinstance(item, dict):
        return None
    book_id = _gutenberg_news_book_id(str(item.get("link") or ""))
    if not book_id:
        return None

    title = html.unescape(str(item.get("title") or "")).strip()
    title = GUTENBERG_NEWS_TITLE_SUFFIX_RE.sub("", title).strip()
    authors = ""
    by_match = GUTENBERG_NEWS_BY_RE.match(title)
    if by_match is not None:
        title = by_match.group("title").strip()
        authors = by_match.group("authors").strip()
    if not title:
        title = f"Book {book_id}"

    images = item.get("images")
    cover_url = ""
    if isinstance(images, list):
        cover_url = next(
            (str(value).strip() for value in images if str(value).strip()),
            "",
        )

    return {
        "id": book_id,
        "title": title,
        "authors": authors,
        "languages": [],
        "subjects": [],
        "first_publish_year": 0,
        "download_count": 0,
        "url": GUTENBERG_BOOK_URL.format(book_id=book_id),
        "text_url": f"{GUTENBERG_BOOK_URL.format(book_id=book_id)}.txt.utf-8",
        "cover_url": cover_url,
        "source": "google_news",
    }


def _merge_gutenberg_news_card(existing: dict, incoming: dict) -> bool:
    """Fill gaps on a duplicate card (for example its missing author).

    The same ebook can appear through both its reading page and its canonical
    ``/ebooks/<id>`` page, and only one of those titles may include the author.
    Returns True when the existing card was changed.
    """
    changed = False
    for key in (
        "authors",
        "languages",
        "subjects",
        "first_publish_year",
        "download_count",
        "cover_url",
    ):
        if not existing.get(key) and incoming.get(key):
            existing[key] = incoming[key]
            changed = True
    return changed


def search_gutenberg_books_google_news(
    keyword: str,
    limit: int = GUTENBERG_BOOK_RESULT_LIMIT,
    on_item: Callable[[list[dict]], None] | None = None,
) -> list[dict]:
    """Search Gutenberg books through the Google News RSS mechanism.

    This deliberately reuses ``news_fetch.php``'s dynamic googlenews source so
    the search behaves exactly like ``/news search``: the query is restricted
    with ``site:gutenberg.org``, Google News redirect links are resolved by the
    existing reader, and each NDJSON ``item`` event becomes a book card. The
    callback receives the accumulated cards as they arrive so the browser can
    render results progressively.
    """
    keyword = keyword.strip()
    if not keyword:
        return []
    limit = max(1, limit)
    script = SCRIPT_DIR / "news_fetch.php"
    if not script.is_file():
        raise RuntimeError(f"missing {script.name}")

    query = f"{GUTENBERG_NEWS_SITE_QUERY} {keyword}"
    # Some hits are category or search pages rather than books, so request
    # more feed items than the number of cards we ultimately want.
    argv = [
        "php",
        str(script),
        "--json",
        "--source",
        "googlenews",
        "--search",
        query,
        "--limit",
        str(max(limit * 2, 20)),
    ]
    try:
        process = subprocess.Popen(
            argv,
            cwd=SCRIPT_DIR,
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            text=True,
            bufsize=1,
        )
    except OSError as exc:
        raise RuntimeError(f"could not run {script.name}: {exc}") from exc

    assert process.stdout is not None
    cards: dict[str, dict] = {}
    stopped = False
    try:
        for raw_line in process.stdout:
            line = raw_line.strip()
            if not line:
                continue
            try:
                payload = json.loads(line)
            except json.JSONDecodeError:
                continue
            if not isinstance(payload, dict) or payload.get("type") != "item":
                continue
            card = _gutenberg_card_from_news_item(payload)
            if card is None:
                continue
            existing = cards.get(card["id"])
            if existing is not None:
                if on_item is not None and _merge_gutenberg_news_card(
                    existing, card
                ):
                    on_item(list(cards.values()))
                continue
            if len(cards) >= limit:
                stopped = True
                break
            cards[card["id"]] = card
            GUTENBERG_CATALOG_CACHE[card["id"]] = card
            if on_item is not None:
                on_item(list(cards.values()))
        if stopped:
            process.terminate()
            try:
                process.wait(timeout=5)
            except subprocess.TimeoutExpired:
                process.kill()
                process.wait()
        else:
            process.wait()
    except BaseException:
        process.terminate()
        try:
            process.wait(timeout=5)
        except subprocess.TimeoutExpired:
            process.kill()
            process.wait()
        raise
    return list(cards.values())[:limit]


def _clean_gutenberg_plaintext(raw_text: str) -> str:
    """Remove the Project Gutenberg header/footer from one downloaded book."""
    text = raw_text.replace("\r\n", "\n").replace("\r", "\n").lstrip("\ufeff")
    start = GUTENBERG_START_MARKER_RE.search(text)
    if start is not None:
        text = text[start.end():]
    end = GUTENBERG_END_MARKER_RE.search(text)
    if end is not None:
        text = text[:end.start()]
    text = GUTENBERG_OLD_END_RE.sub("", text)
    return text.strip() + "\n"


def _gutenberg_rag_text(text: str, title: str) -> str:
    """Make a book-friendly source for the existing article/RAG splitter.

    Project Gutenberg files use hard-wrapped prose and plain ``CHAPTER I``
    lines. Promote those lines to Markdown headings so the article structure
    builder can produce section summaries, and put the title first so overview
    questions retrieve a useful document heading.
    """
    lines: list[str] = []
    for line in text.splitlines():
        stripped = line.strip()
        if stripped and GUTENBERG_CHAPTER_RE.match(stripped):
            lines.append(f"\n## {stripped}\n")
        else:
            lines.append(line)
    body = "\n".join(lines).strip()
    if title and title.casefold() not in body[:500].casefold():
        body = f"# {title}\n\n{body}"
    return body + "\n"


def _gutenberg_book_paths(book_id: str) -> tuple[Path, Path]:
    GUTENBERG_BOOK_CACHE_DIR.mkdir(parents=True, exist_ok=True)
    return (
        GUTENBERG_BOOK_CACHE_DIR / f"{book_id}.txt",
        GUTENBERG_BOOK_CACHE_DIR / f"{book_id}.rag.txt",
    )


def _gutenberg_download_lock(book_id: str) -> threading.Lock:
    with GUTENBERG_DOWNLOAD_LOCK:
        lock = GUTENBERG_DOWNLOAD_LOCKS.get(book_id)
        if lock is None:
            lock = threading.Lock()
            GUTENBERG_DOWNLOAD_LOCKS[book_id] = lock
        return lock


def ensure_gutenberg_book(
    book_id: str,
    text_url: str = "",
    title: str = "",
) -> tuple[str, Path]:
    """Download (once) and return a book's cleaned plaintext and text path."""
    if not GUTENBERG_BOOK_ID_RE.fullmatch(book_id):
        raise ValueError("Missing or invalid Project Gutenberg book id")
    text_path, rag_path = _gutenberg_book_paths(book_id)
    if text_path.is_file() and rag_path.is_file():
        try:
            return text_path.read_text(encoding="utf-8"), rag_path
        except OSError:
            pass

    catalog = GUTENBERG_CATALOG_CACHE.get(book_id) or {}
    text_url = (text_url or catalog.get("text_url") or "").strip()
    title = (title or catalog.get("title") or "").strip()
    if not text_url:
        # This URL pattern redirects to the available plaintext edition for
        # nearly every English-language Gutenberg book.
        text_url = f"https://www.gutenberg.org/ebooks/{book_id}.txt.utf-8"

    with _gutenberg_download_lock(book_id):
        if text_path.is_file() and rag_path.is_file():
            return text_path.read_text(encoding="utf-8"), rag_path
        try:
            response = requests.get(
                text_url,
                headers={**chat.HTTP_HEADERS, "Accept": "text/plain"},
                stream=True,
                timeout=(10, 90),
                allow_redirects=True,
            )
            response.raise_for_status()
            chunks = bytearray()
            for block in response.iter_content(chunk_size=256 * 1024):
                if not block:
                    continue
                chunks.extend(block)
                if len(chunks) > GUTENBERG_TEXT_MAX_BYTES:
                    raise RuntimeError(
                        "Book plaintext is too large to cache "
                        f"(limit {GUTENBERG_TEXT_MAX_BYTES:,} bytes)."
                    )
        except requests.RequestException as exc:
            raise RuntimeError(f"Could not download the book: {exc}") from exc

        raw_text = _decode_article_bytes(bytes(chunks))
        text = _clean_gutenberg_plaintext(raw_text)
        if not text.strip():
            raise RuntimeError("The downloaded book has no readable text.")
        if not title:
            title = next(
                (line.strip() for line in text.splitlines() if line.strip()),
                f"Book {book_id}",
            )[:200]

        text_path.parent.mkdir(parents=True, exist_ok=True)
        temp_text = text_path.with_name(text_path.name + ".tmp")
        temp_rag = rag_path.with_name(rag_path.name + ".tmp")
        temp_text.write_text(text, encoding="utf-8")
        temp_rag.write_text(_gutenberg_rag_text(text, title), encoding="utf-8")
        temp_text.replace(text_path)
        temp_rag.replace(rag_path)
        return text, rag_path


class ChatState:
    """The server-side equivalent of chat.py's interactive session state."""

    def __init__(
        self,
        max_context_tokens: int = chat.MAX_CONTEXT_TOKENS,
        progressive: bool = False,
        chunks: list[chat.Chunk] | None = None,
    ) -> None:
        self.lock = threading.RLock()
        self.max_context_tokens = max_context_tokens
        self.progressive = progressive
        self.depth: int | None = chat.DEFAULT_DEPTH
        # The language of the most recent code question, when one was named.
        # It lets a follow-up such as "now add error handling" keep using the
        # same official documentation.
        self.code_language: str = ""
        self.transcript: str | None = None
        # Set on an isolated in-modal session for one learned news story.
        self.news_id: str | None = None
        self.article_path: str | None = None
        self.article_title: str = ""
        self.article_structure: dict | None = None
        self.article_iterative = False
        # The /mathtutor session the browser is currently using. Sessions are
        # also discoverable on disk, so a server restart resumes the latest
        # uploaded test papers.
        self.mathtutor_session_id: str | None = None
        self.cache = chat.AnswerCache()
        # /youtube search results are reused by the "More" button instead of
        # rerunning the (network-bound) YouTube search from scratch.
        self.youtube_search_cache: dict[str, list[dict]] = {}
        # In-progress background cache growth, keyed by search keyword.
        # The More handler waits on the event instead of starting a duplicate
        # search for the same keyword.
        self.youtube_prefetch: dict[str, threading.Event] = {}
        # Background /youtube search card "Learn" jobs, keyed by video id.
        self.video_learn_jobs: dict[str, dict] = {}
        self.video_learn_lock = threading.Lock()
        self.latest_answer: str | None = None
        self.latest_sources: list[tuple[chat.Chunk, float]] = []
        self.latest_benchmark: dict | None = None
        self.benchmark_mode = False
        self.conversation: list[dict] = []
        self.conversation_revision = 0
        chat.HISTORY_FILE.parent.mkdir(parents=True, exist_ok=True)
        chat.HISTORY_FILE.touch(exist_ok=True)
        if chunks is None:
            chunks, _printed, error = load_all_chunks_captured()
            if error or chunks is None:
                raise SystemExit(error or "No trunks found in the global corpus")
        self.chunks = chunks
        # The global corpus stays loaded even while /transcript or /article uses
        # a narrower in-memory scope.
        # An explicitly supplied corpus (the in-video chat) is also used as the
        # fallback corpus.
        self.global_chunks = chunks
        self.history: list[str] = self.read_history()
        self.started_at = __import__("time").time()

    # ---- history -----------------------------------------------------
    def read_history(self) -> list[str]:
        try:
            lines = chat.HISTORY_FILE.read_text(
                encoding="utf-8", errors="replace"
            ).splitlines()
        except OSError:
            return []
        return [line for line in lines if line.strip()]

    def append_history(self, entry: str) -> None:
        entry = entry.replace("\n", " ").strip()
        if not entry:
            return
        try:
            with chat.HISTORY_FILE.open("a", encoding="utf-8") as handle:
                handle.write(entry + "\n")
        except OSError:
            return
        if not self.history or self.history[-1] != entry:
            self.history.append(entry)
            if len(self.history) > 2000:
                self.history = self.history[-2000:]

    # ---- conversation ------------------------------------------------
    def reset_conversation(self) -> None:
        self.conversation.clear()
        self.conversation_revision += 1

    def resolve_question(self, question: str) -> ConversationResolution:
        """Interpret a question against the recent conversation.

        A related follow-up is rewritten into a standalone retrieval question.
        A question that is completely unrelated to the previous turns starts a
        fresh context, so stale names and pronouns cannot leak into later
        retrieval.
        """
        if not self.conversation:
            return ConversationResolution(question)
        if not question_needs_context(question, self.conversation):
            overlap = conversation_overlap_ratio(question, self.conversation)
            if overlap > 0.5:
                return ConversationResolution(question)
            if overlap == 0.0:
                # No shared topic at all: confidently a fresh question.
                self.reset_conversation()
                return ConversationResolution(
                    question, is_follow_up=False, is_unrelated=True
                )
            # Partial overlap is ambiguous: ask the conversation model instead
            # of guessing, so a longer follow-up is not dropped and a longer
            # unrelated question does not inherit stale context.
        resolution = rewrite_follow_up(question, self.conversation)
        if resolution is None:
            return ConversationResolution(question)
        if resolution.is_unrelated:
            self.reset_conversation()
        return resolution

    def record_turn(
        self, question: str, resolved_question: str, answer: str
    ) -> None:
        self.conversation.append(
            {
                "question": question,
                "resolved_question": resolved_question,
                "answer": answer,
            }
        )
        if len(self.conversation) > CONVERSATION_MAX_TURNS:
            del self.conversation[:-CONVERSATION_MAX_TURNS]
        self.conversation_revision += 1

    # ---- state -------------------------------------------------------
    def replace_chunks(
        self,
        chunks: list[chat.Chunk],
        article_path: str | None = None,
        article_title: str = "",
        article_structure: dict | None = None,
        article_iterative: bool = False,
    ) -> None:
        """Install a new knowledge scope and drop the current conversation.

        Switching transcripts or articles starts a fresh chain of questions.
        Passing an ``article_path`` enters article mode; every other caller
        leaves article mode.
        """
        self.chunks = chunks
        self.article_path = article_path
        self.article_title = article_title
        self.article_structure = article_structure
        self.article_iterative = bool(article_iterative and article_path)
        chat.clear_bm25_cache()
        self.reset_conversation()
        gc.collect()

    @property
    def depth_label(self) -> str:
        if self.depth is None:
            return "auto"
        return str(self.depth)

    def snapshot(self, include_history: bool = False) -> dict:
        all_ids = {chunk.video_id for chunk in self.chunks}
        news_ids = {video_id for video_id in all_ids if NEWS_ID_PATTERN.fullmatch(video_id)}
        video_ids = all_ids - news_ids
        data = {
            "transcript": self.transcript,
            "depth": self.depth,
            "depth_label": self.depth_label,
            "default_depth": chat.DEFAULT_DEPTH,
            "max_context_tokens": self.max_context_tokens,
            "progressive": self.progressive,
            "chunks": len(self.chunks),
            "videos": len(video_ids),
            "news": len(news_ids),
            "code_language": self.code_language or "",
            "scope": (
                f"news {self.news_id!r}"
                if self.news_id is not None
                else (
                    f"article {self.article_path!r}"
                    if self.article_path is not None
                    else (
                        f"transcript {self.transcript!r}"
                        if self.transcript is not None
                        else "global corpus (all learned trunks)"
                    )
                )
            ),
            "article": self.article_path,
            "article_title": self.article_title,
            "article_iterative": self.article_iterative,
            "article_sections": len(
                (self.article_structure or {}).get("sections", [])
            ),
            "models": {
                "chat": language_chat_model(self.code_language),
                "coding": language_coding_model(self.code_language),
                "line_explain": language_coding_model(self.code_language),
                "expansion": chat.EXPANSION_MODEL,
                "filter": chat.FILTER_MODEL,
                "translation": chat.TRANSLATION_MODEL,
                "conversation": CONVERSATION_MODEL,
                "dict": chat.DICT_MODEL,
            },
            "benchmark_mode": self.benchmark_mode,
            "has_answer": self.latest_answer is not None,
            "source_count": len(self.latest_sources),
            "history_count": len(self.history),
            "conversation_turns": len(self.conversation),
            "conversation_max_turns": CONVERSATION_MAX_TURNS,
            "commands": [
                {"name": name, "description": description}
                for name, description in sorted(WEB_COMMANDS.items())
            ],
            "argument_commands": sorted(COMMAND_ARGUMENTS),
            "news_sources": [
                {"name": name, "label": label}
                for name, label in sorted(NEWS_SOURCES.items())
            ],
        }
        if include_history:
            data["history"] = self.history[-1000:]
        return data

    def cache_key(
        self,
        question: str,
        answer_language: str | None,
        conversation_revision: int | None = None,
        language: str | None = None,
        coding_mode: bool = False,
        prompt_variant: str = "",
    ) -> tuple:
        key = chat.make_cache_key(
            self.transcript,
            self.depth,
            self.max_context_tokens,
            answer_language,
            question,
            self.progressive,
        )
        if conversation_revision is not None:
            # Context-dependent questions must not hit an answer cached for a
            # different "he", "that book", etc.
            key += (conversation_revision,)
        if self.article_path is not None:
            # Article answers depend on the section map and route, not only on
            # the scope that happened to be active when /article was entered.
            key += (
                "article",
                self.article_path,
                (self.article_structure or {}).get("video_id", ""),
                self.article_iterative,
            )
        if self.news_id is not None:
            key += ("news", self.news_id)
        if coding_mode:
            # The coding model and prompt differ from the regular RAG answer.
            key += ("coding", language_coding_model(language))
        else:
            # The model differs when a documentation-backed language is
            # active.
            key += ("model", language_chat_model(language))
        if prompt_variant:
            # Documentation-first answers use the official docs as prompt
            # context, so they must not reuse a plain coding-mode answer.
            key += (prompt_variant,)
        return key


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
    state: ChatState, video_id: str, title: str = ""
) -> tuple[str, dict | None]:
    """Start a background learn job, or report that one already exists."""
    if video_is_learned(video_id):
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
            "message": "Queued for learning",
            "error": "",
            "learned": False,
            "started_at": time.time(),
            "finished_at": None,
        }
        state.video_learn_jobs[video_id] = job
        snapshot = dict(job)

    threading.Thread(
        target=_run_video_learn_job,
        args=(state, video_id),
        name=f"video-learn-{video_id}",
        daemon=True,
    ).start()
    return "started", snapshot


def _run_video_learn_job(state: ChatState, video_id: str) -> None:
    """Run youtube_video_learn.STEPS and publish progress to the UI."""
    _update_video_learn_job(
        state,
        video_id,
        status="running",
        progress=0.0,
        step="starting",
        message="Starting learn pipeline",
    )
    try:
        import youtube_video_learn as learn

        steps = list(learn.STEPS)
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
            try:
                for line in process.stdout:
                    clean = line.strip()
                    if not clean:
                        continue
                    _update_video_learn_job(
                        state, video_id, message=clean[:200]
                    )
                returncode = process.wait()
            finally:
                if process.poll() is None:
                    process.kill()
                    process.wait()

            if returncode != 0:
                raise RuntimeError(
                    f"{script} failed with exit code {returncode}"
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
                chat.index_chunks_in_sqlite(chunks, corpus_key="global")
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


class ChatRequestHandler(BaseHTTPRequestHandler):
    """One HTTP handler per request; SSE responses stream from Ollama."""

    server_version = "YTLocalChat/1.0"
    protocol_version = "HTTP/1.1"

    # Injected in main().
    state: ChatState

    # ---- basic helpers ------------------------------------------------
    def log_message(self, fmt, *args):
        # Keep the terminal quiet; errors still surface through SSE.
        if args and str(args[0]).startswith("4"):
            super().log_message(fmt, *args)

    def _send_json(self, payload: dict, status: int = 200) -> None:
        data = json.dumps(payload, ensure_ascii=False).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(data)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        try:
            self.wfile.write(data)
        except (BrokenPipeError, ConnectionResetError):
            pass

    def _send_error_json(self, message: str, status: int = 400) -> None:
        self._send_json({"error": message}, status)

    def _send_wav(self, data: bytes) -> None:
        self.send_response(200)
        self.send_header("Content-Type", "audio/wav")
        self.send_header("Content-Length", str(len(data)))
        self.send_header("Cache-Control", "private, max-age=86400")
        self.send_header("X-Content-Type-Options", "nosniff")
        self.end_headers()
        try:
            self.wfile.write(data)
        except (BrokenPipeError, ConnectionResetError):
            pass

    def _read_json(self) -> dict | None:
        try:
            length = int(self.headers.get("Content-Length", "0"))
        except ValueError:
            self._send_error_json("Invalid Content-Length", 400)
            return None
        if length > MAX_REQUEST_BYTES:
            self._send_error_json("Request body too large", 413)
            return None
        raw = self.rfile.read(length) if length else b""
        if not raw:
            return {}
        try:
            data = json.loads(raw.decode("utf-8"))
        except (UnicodeDecodeError, json.JSONDecodeError):
            self._send_error_json("Request body must be valid JSON", 400)
            return None
        return data if isinstance(data, dict) else None

    def _begin_sse(self) -> None:
        self.send_response(200)
        self.send_header("Content-Type", "text/event-stream; charset=utf-8")
        self.send_header("Cache-Control", "no-cache, no-store, must-revalidate")
        self.send_header("X-Accel-Buffering", "no")
        self.send_header("Connection", "close")
        self.end_headers()
        self.close_connection = True

    def _sse(self, event: str, data) -> None:
        payload = json.dumps(data, ensure_ascii=False)
        message = f"event: {event}\ndata: {payload}\n\n".encode("utf-8")
        try:
            self.wfile.write(message)
            self.wfile.flush()
        except (BrokenPipeError, ConnectionResetError, OSError) as exc:
            raise ClientDisconnected() from exc

    def _sse_text(self, event: str, text: str) -> None:
        if text:
            self._sse(event, {"text": text})

    def _sse_answer_source(
        self,
        *,
        language: str = "",
        source: str,
        label: str,
        model: str = "",
        url: str = "",
        detail: str = "",
    ) -> None:
        """Tell the browser where the currently streaming answer comes from."""
        self._sse(
            "source",
            {
                "language": language,
                "source": source,
                "label": label,
                "model": model,
                "url": url,
                "detail": detail,
            },
        )
        self._sse_text("log", f"[answer source: {label}]")

    def _sse_doc_source(self, payload: dict) -> None:
        self._sse("source", payload)
        label = payload.get("label") or payload.get("source") or "unknown"
        self._sse_text("log", f"[answer source: {label}]")

    # ---- GET ----------------------------------------------------------
    def do_OPTIONS(self) -> None:  # noqa: N802 - BaseHTTPRequestHandler API
        self.send_response(204)
        self.send_header("Access-Control-Allow-Origin", "*")
        self.send_header("Access-Control-Allow-Methods", "GET, POST, OPTIONS")
        self.send_header("Access-Control-Allow-Headers", "Content-Type")
        self.end_headers()

    def do_GET(self) -> None:  # noqa: N802 - BaseHTTPRequestHandler API
        parsed = urlparse(self.path)
        path = parsed.path
        if path in {"/", "/index.html", "/chat.html", "/app/chat.html"}:
            self._serve_chat_html()
            return
        if path == "/favicon.ico":
            self.send_response(204)
            self.end_headers()
            return
        if path == "/api/state":
            with self.state.lock:
                self._send_json(self.state.snapshot(include_history=True))
            return
        if path == "/api/video_learn_status":
            params = parse_qs(parsed.query)
            video_id = (params.get("id") or [""])[0].strip()
            if video_id and not re.fullmatch(r"[A-Za-z0-9_-]{11}", video_id):
                self._send_error_json("Invalid YouTube video id", 400)
                return
            if video_id:
                job = _video_learn_jobs_snapshot(self.state, video_id)
                if job is not None:
                    self._send_json(job)
                    return
                # Match the disk-backed readiness rule used by
                # get_video_chat_state. A transcript-only video is also
                # chat-ready because the chat request rebuilds its trunks.
                learned = video_is_chat_ready(video_id)
                self._send_json(
                    {
                        "id": video_id,
                        "status": "learned" if learned else "idle",
                        "progress": 100.0 if learned else 0.0,
                        "learned": learned,
                    }
                )
                return
            self._send_json(
                {"jobs": _video_learn_jobs_snapshot(self.state) or []}
            )
            return
        if path == "/api/news_status":
            params = parse_qs(parsed.query)
            news_id = (params.get("id") or [""])[0].strip()
            if not NEWS_ID_PATTERN.fullmatch(news_id):
                self._send_error_json("Missing or invalid news story id", 400)
                return
            self._send_json(
                {
                    "id": news_id,
                    "learned": news_is_learned(news_id),
                    "title": news_title(news_id),
                }
            )
            return
        if path == "/api/news_item":
            params = parse_qs(parsed.query)
            news_id = (params.get("id") or [""])[0].strip()
            if not NEWS_ID_PATTERN.fullmatch(news_id):
                self._send_error_json("Missing or invalid news story id", 400)
                return
            meta = _read_news_meta(news_id)
            if not meta:
                self._send_error_json("News story not found", 404)
                return
            item = dict(meta)
            item["id"] = news_id
            item["news_id"] = news_id
            item["learned"] = news_is_learned(news_id)
            item["complete"] = True
            self._send_json(item)
            return
        if path == "/api/news_article":
            params = parse_qs(parsed.query)
            url = (params.get("url") or [""])[0].strip()
            title = (params.get("title") or [""])[0].strip()
            try:
                article = fetch_news_article(url, title)
            except ValueError as exc:
                self._send_error_json(str(exc), 400)
                return
            except (OSError, subprocess.SubprocessError, RuntimeError) as exc:
                self._send_error_json(str(exc), 502)
                return
            self._send_json(article)
            return
        if path == "/api/wikivoyage_article":
            params = parse_qs(parsed.query)
            url = (params.get("url") or [""])[0].strip()
            title = (params.get("title") or [""])[0].strip()
            try:
                article = fetch_wikivoyage_article(url, title)
            except ValueError as exc:
                self._send_error_json(str(exc), 400)
                return
            except RuntimeError as exc:
                self._send_error_json(str(exc), 502)
                return
            self._send_json(article)
            return
        if path == "/api/autocomplete":
            params = parse_qs(parsed.query)
            kind = (params.get("kind") or ["commands"])[0]
            query = (params.get("q") or [""])[0]
            self._send_json({"items": self._autocomplete(kind, query)})
            return
        if path == "/api/changelog_file":
            params = parse_qs(parsed.query)
            name = (params.get("name") or [""])[0]
            self._send_changelog_file(name)
            return
        if path == "/api/wiki":
            params = parse_qs(parsed.query)
            word = (params.get("word") or [""])[0].strip()
            self._wiki_json(word)
            return
        if path == "/api/transcripts":
            params = parse_qs(parsed.query)
            keyword = (params.get("q") or [""])[0].strip()
            self._transcript_search_json(keyword)
            return
        if path == "/api/gutenberg_book":
            params = parse_qs(parsed.query)
            book_id = (params.get("id") or [""])[0].strip()
            text_url = (params.get("url") or [""])[0].strip()
            title = (params.get("title") or [""])[0].strip()
            self._gutenberg_book_json(book_id, text_url, title)
            return
        self._send_error_json("Not found", 404)

    def _serve_chat_html(self) -> None:
        try:
            data = CHAT_HTML.read_bytes()
        except OSError as exc:
            self._send_error_json(f"Cannot read {CHAT_HTML}: {exc}", 500)
            return
        self.send_response(200)
        self.send_header("Content-Type", "text/html; charset=utf-8")
        self.send_header("Content-Length", str(len(data)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        try:
            self.wfile.write(data)
        except (BrokenPipeError, ConnectionResetError):
            pass

    def _send_changelog_file(self, name: str) -> None:
        """Serve one completed-task summary page to the changelog modal."""
        path = changelog_file_path(name)
        if path is None:
            self._send_error_json("Changelog entry not found", 404)
            return
        try:
            data = path.read_bytes()
        except OSError as exc:
            self._send_error_json(f"Cannot read changelog entry: {exc}", 500)
            return
        self.send_response(200)
        self.send_header("Content-Type", "text/html; charset=utf-8")
        self.send_header("Content-Length", str(len(data)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        try:
            self.wfile.write(data)
        except (BrokenPipeError, ConnectionResetError):
            pass

    def _autocomplete(self, kind: str, query: str) -> list[dict]:
        query = query.strip()
        if kind == "commands":
            items = []
            for name, description in sorted(WEB_COMMANDS.items()):
                if name.startswith(query or "/"):
                    items.append({"value": name, "description": description})
            return items

        if kind == "youtube_subcommands":
            lowered = query.lower()
            return [
                {"value": name, "description": description}
                for name, description in YOUTUBE_SUBCOMMANDS.items()
                if name.startswith(lowered)
            ]

        if kind == "gutenberg_subcommands":
            lowered = query.lower()
            return [
                {"value": name, "description": description}
                for name, description in GUTENBERG_SUBCOMMANDS.items()
                if name.startswith(lowered)
            ]

        if kind == "openlibrary_subcommands":
            lowered = query.lower()
            return [
                {"value": name, "description": description}
                for name, description in OPENLIBRARY_SUBCOMMANDS.items()
                if name.startswith(lowered)
            ]

        if kind == "youtube_bookmark_actions":
            lowered = query.lower()
            actions = {
                "list": "list bookmarked videos, latest first",
            }
            return [
                {"value": name, "description": description}
                for name, description in actions.items()
                if name.startswith(lowered)
            ]

        if kind in {"news_sources", "news_subcommands"}:
            lowered = query.lower()
            items = []
            if kind == "news_subcommands" and "search".startswith(lowered):
                items.append(
                    {
                        "value": "search",
                        "description": "search Google News by keywords",
                    }
                )
            items.extend(
                {"value": name, "description": label}
                for name, label in sorted(NEWS_SOURCES.items())
                if name.startswith(lowered)
            )
            return items

        if kind == "paths":
            return self._path_completions(query)

        if kind in {"transcripts", "transcript_titles"}:
            if kind == "transcript_titles" and query:
                matches = chat.search_transcript_titles(query)
                available = set(chat.available_video_ids())
                return [
                    {
                        "value": match.video_id,
                        "description": match.title,
                    }
                    for match in matches
                    if match.video_id in available
                ][:30]

            available = chat.available_video_ids()
            lowered = query.lower()
            items = []
            for video_id in available:
                title = chat.video_title(video_id)
                if (
                    not lowered
                    or lowered in video_id.lower()
                    or lowered in title.lower()
                ):
                    items.append(
                        {"value": video_id, "description": title or video_id}
                    )
                    if len(items) >= 30:
                        break
            return items

        return []

    def _path_completions(self, query: str) -> list[dict]:
        """Complete a local filesystem path for commands such as /article.

        The typed text is treated the way a shell would read it: the part up
        to the last slash selects the directory to list and the remainder is
        the name prefix.  ``~`` is expanded for reading the directory while
        the returned value keeps the user's spelling, so completing ``~/do``
        yields ``~/documents/`` instead of an absolute path.
        """
        raw = query or ""
        if raw == "~":
            prefix, tail = "~/", ""
        elif "/" in raw:
            head, tail = raw.rsplit("/", 1)
            prefix = head + "/"
        else:
            prefix, tail = "", raw

        if prefix:
            directory = Path(os.path.expanduser(prefix))
        else:
            directory = Path.cwd()
        if not directory.is_absolute():
            directory = Path.cwd() / directory

        # Hidden entries only appear once the user starts typing a dot.
        show_hidden = tail.startswith(".")
        try:
            entries = list(directory.iterdir())
        except OSError:
            return []

        directories: list[dict] = []
        files: list[dict] = []
        for entry in entries:
            name = entry.name
            if not name.startswith(tail):
                continue
            if name.startswith(".") and not show_hidden:
                continue
            try:
                is_dir = entry.is_dir()
            except OSError:
                is_dir = False
            value = prefix + name + ("/" if is_dir else "")
            if is_dir:
                directories.append({"value": value, "description": "dir"})
            else:
                try:
                    size = entry.stat().st_size
                    description = f"{size:,} bytes"
                except OSError:
                    description = "file"
                files.append({"value": value, "description": description})

        def sort_key(item: dict) -> tuple[str, str]:
            return (item["value"].rsplit("/", 1)[-1].lower(), item["value"])

        directories.sort(key=sort_key)
        files.sort(key=sort_key)
        return (directories + files)[:PATH_COMPLETION_LIMIT]

    def _wiki_json(self, word: str) -> None:
        if not word:
            self._send_error_json("Usage: /wiki <word>", 400)
            return
        url = chat.WIKI_SUMMARY_URL.format(word=quote(word, safe=""))
        try:
            response = requests.get(
                url, headers=chat.HTTP_HEADERS, timeout=(10, 30)
            )
        except requests.RequestException as exc:
            self._send_error_json(f"Wikipedia lookup failed: {exc}", 502)
            return
        if response.status_code == 404:
            self._send_error_json(f"Wikipedia: no page found for {word!r}.", 404)
            return
        try:
            response.raise_for_status()
            data = response.json()
        except (requests.RequestException, ValueError) as exc:
            self._send_error_json(f"Wikipedia lookup failed: {exc}", 502)
            return
        if not isinstance(data, dict):
            self._send_error_json("Wikipedia returned an unexpected response.", 502)
            return
        page = ((data.get("content_urls") or {}).get("desktop") or {}).get(
            "page"
        )
        self._send_json(
            {
                "word": word,
                "title": data.get("title") or word,
                "extract": data.get("extract") or data.get("description") or "",
                "source": page or "",
            }
        )

    def _transcript_search_json(self, keyword: str) -> None:
        if not keyword:
            self._send_error_json("Usage: /youtube transcript <keyword>", 400)
            return
        matches = chat.search_transcript_titles(keyword)
        available = set(chat.available_video_ids())
        self._send_json(
            {
                "items": [
                    {
                        "video_id": match.video_id,
                        "title": match.title,
                        "has_trunks": match.video_id in available,
                    }
                    for match in matches
                ]
            }
        )

    def _gutenberg_book_json(
        self, book_id: str, text_url: str = "", title: str = ""
    ) -> None:
        """Return one book's cached plaintext for the two-column modal."""
        if not GUTENBERG_BOOK_ID_RE.fullmatch(book_id):
            self._send_error_json(
                "Usage: /api/gutenberg_book?id=<book_id>", 400
            )
            return
        try:
            text, _rag_path = ensure_gutenberg_book(
                book_id, text_url=text_url, title=title
            )
        except (ValueError, RuntimeError) as exc:
            self._send_error_json(str(exc), 502)
            return
        catalog = GUTENBERG_CATALOG_CACHE.get(book_id) or {}
        resolved_title = (
            title
            or str(catalog.get("title") or "")
            or next(
                (line.strip() for line in text.splitlines() if line.strip()),
                f"Book {book_id}",
            )[:200]
        )
        self._send_json(
            {
                "id": book_id,
                "title": resolved_title,
                "authors": str(catalog.get("authors") or ""),
                "languages": catalog.get("languages") or [],
                "subjects": catalog.get("subjects") or [],
                "download_count": int(catalog.get("download_count") or 0),
                "url": GUTENBERG_BOOK_URL.format(book_id=book_id),
                "text_url": text_url
                or str(catalog.get("text_url") or ""),
                "text": text,
                "characters": len(text),
            }
        )

    # ---- POST ---------------------------------------------------------
    def do_POST(self) -> None:  # noqa: N802 - BaseHTTPRequestHandler API
        parsed = urlparse(self.path)
        if parsed.path == "/api/ask":
            payload = self._read_json()
            if payload is None:
                return
            question = str(payload.get("question", "")).strip()
            raw_image = payload.get("image")
            image: str | None = None
            if raw_image:
                try:
                    image = normalize_attachment_image(raw_image)
                except ValueError as exc:
                    self._send_error_json(str(exc), 400)
                    return
            if not question and image is None:
                self._send_error_json("Missing question", 400)
                return
            if image is not None and question.startswith("/"):
                self._send_error_json(
                    "Images can only be attached to a question, not a command",
                    400,
                )
                return
            self._handle_ask(question, image=image)
            return
        if parsed.path == "/api/dict":
            payload = self._read_json()
            if payload is None:
                return
            word = str(payload.get("word", "")).strip()
            answer = payload.get("answer")
            self._handle_dict(word, answer if isinstance(answer, str) else None)
            return
        if parsed.path == "/api/translate":
            payload = self._read_json()
            if payload is None:
                return
            text = payload.get("text")
            if not isinstance(text, str) or not text.strip():
                self._send_error_json("Missing text to translate", 400)
                return
            self._handle_translate(text)
            return
        if parsed.path == "/api/tts":
            payload = self._read_json()
            if payload is None:
                return
            text = payload.get("text")
            if not isinstance(text, str) or not text.strip():
                self._send_error_json("Missing text to read aloud", 400)
                return
            voice = str(payload.get("voice") or "")
            try:
                self._send_wav(synthesize_speech(text, voice=voice))
            except ValueError as exc:
                self._send_error_json(str(exc), 400)
            except (OSError, subprocess.SubprocessError, RuntimeError) as exc:
                self._send_error_json(
                    f"Could not read the text aloud: {exc}", 502
                )
            return
        if parsed.path == "/api/ai_news_podcast":
            payload = self._read_json()
            if payload is None:
                return
            self._handle_ai_news_podcast(payload)
            return
        if parsed.path == "/api/explain_line":
            payload = self._read_json()
            if payload is None:
                return
            self._handle_explain_line(payload)
            return
        if parsed.path == "/api/snippet_question":
            payload = self._read_json()
            if payload is None:
                return
            self._handle_snippet_question(payload)
            return
        if parsed.path == "/api/video_learn":
            payload = self._read_json()
            if payload is None:
                return
            video_id = str(payload.get("id") or "").strip()
            if not re.fullmatch(r"[A-Za-z0-9_-]{11}", video_id):
                self._send_error_json("Missing or invalid YouTube video id", 400)
                return
            title = str(payload.get("title") or "").strip()
            status, job = start_video_learn_job(
                self.state, video_id, title=title
            )
            response = {"id": video_id, "status": status}
            if status == "learned":
                response.update(progress=100.0, learned=True)
            else:
                response["job"] = job
            self._send_json(response)
            return
        if parsed.path == "/api/news_learn":
            payload = self._read_json()
            if payload is None:
                return
            try:
                result = learn_news_article(payload)
            except ValueError as exc:
                self._send_error_json(str(exc), 400)
                return
            except OSError as exc:
                self._send_error_json(
                    f"Could not store the story: {exc}", 500
                )
                return
            refresh_news_corpus(self.state, result["id"])
            self._send_json(result)
            return
        if parsed.path == "/api/news_chat":
            payload = self._read_json()
            if payload is None:
                return
            news_id = str(payload.get("id") or "").strip()
            if not NEWS_ID_PATTERN.fullmatch(news_id):
                self._send_error_json("Missing or invalid news story id", 400)
                return
            question = str(payload.get("question") or "").strip()
            if not question:
                self._send_error_json("Missing question", 400)
                return
            self._handle_news_chat(
                news_id, question, reset=bool(payload.get("reset"))
            )
            return
        if parsed.path == "/api/video_summary":
            payload = self._read_json()
            if payload is None:
                return
            self._handle_video_summary(payload)
            return
        if parsed.path == "/api/video_chat":
            payload = self._read_json()
            if payload is None:
                return
            video_id = str(payload.get("id") or "").strip()
            if not re.fullmatch(r"[A-Za-z0-9_-]{11}", video_id):
                self._send_error_json("Missing or invalid YouTube video id", 400)
                return
            question = str(payload.get("question") or "").strip()
            if not question:
                self._send_error_json("Missing question", 400)
                return
            self._handle_video_chat(
                video_id, question, reset=bool(payload.get("reset"))
            )
            return
        if parsed.path == "/api/youtube_bookmark":
            payload = self._read_json()
            if payload is None:
                return
            try:
                entry = add_youtube_bookmark(payload)
            except ValueError as exc:
                self._send_error_json(str(exc), 400)
                return
            self._send_json({"ok": True, "bookmark": entry})
            return
        if parsed.path == "/api/youtube_more":
            payload = self._read_json()
            if payload is None:
                return
            keyword = str(payload.get("keyword") or "").strip()
            raw_exclude = payload.get("exclude")
            exclude = (
                {str(value) for value in raw_exclude}
                if isinstance(raw_exclude, list)
                else set()
            )
            self._handle_youtube_more(keyword, exclude)
            return
        if parsed.path == "/api/gutenberg_chat":
            payload = self._read_json()
            if payload is None:
                return
            book_id = str(payload.get("id") or "").strip()
            if not GUTENBERG_BOOK_ID_RE.fullmatch(book_id):
                self._send_error_json(
                    "Missing or invalid Project Gutenberg book id", 400
                )
                return
            question = str(payload.get("question") or "").strip()
            if not question:
                self._send_error_json("Missing question", 400)
                return
            self._handle_gutenberg_chat(
                book_id,
                question,
                title=str(payload.get("title") or "").strip(),
                text_url=str(payload.get("url") or "").strip(),
                reset=bool(payload.get("reset")),
            )
            return
        if parsed.path == "/api/mathtutor_upload":
            payload = self._read_json()
            if payload is None:
                return
            raw_files = payload.get("files")
            if not isinstance(raw_files, list) or not raw_files:
                self._send_error_json("Upload at least one Word document", 400)
                return
            try:
                session = _mathtutor_build_session(raw_files)
            except ValueError as exc:
                self._send_error_json(str(exc), 400)
                return
            except RuntimeError as exc:
                self._send_error_json(
                    f"Could not generate practice problems: {exc}", 502
                )
                return
            except OSError as exc:
                self._send_error_json(
                    f"Could not store the uploaded documents: {exc}", 500
                )
                return
            with self.state.lock:
                self.state.mathtutor_session_id = str(session["id"])
            self._send_json({"session": _mathtutor_public_session(session)})
            return
        if parsed.path == "/api/mathtutor_more":
            payload = self._read_json()
            if payload is None:
                return
            session_id = str(payload.get("id") or "").strip()
            try:
                with MATHUTOR_LOCK:
                    session = _mathtutor_load_session(session_id)
                    start_id = len(session.get("problems", [])) + 1
                    added = _mathtutor_generate_problems(
                        session,
                        count=MATHUTOR_PROBLEMS_PER_BATCH,
                        start_id=start_id,
                    )
                    session.setdefault("problems", []).extend(added)
                    _mathtutor_save_session(session)
            except ValueError as exc:
                self._send_error_json(str(exc), 400)
                return
            except RuntimeError as exc:
                self._send_error_json(
                    f"Could not generate more practice problems: {exc}", 502
                )
                return
            except OSError as exc:
                self._send_error_json(
                    f"Could not update the mathtutor session: {exc}", 500
                )
                return
            with self.state.lock:
                self.state.mathtutor_session_id = str(session.get("id", ""))
            self._send_json(
                {
                    "session": _mathtutor_public_session(session),
                    "added": len(added),
                }
            )
            return
        if parsed.path == "/api/mathtutor_answer":
            payload = self._read_json()
            if payload is None:
                return
            session_id = str(payload.get("id") or "").strip()
            try:
                index = int(payload.get("index", -1))
            except (TypeError, ValueError):
                index = -1
            student_answer = str(payload.get("answer") or "").strip()
            self._handle_mathtutor_answer(session_id, index, student_answer)
            return
        self._send_error_json("Not found", 404)

    # ---- SSE: ask / commands -----------------------------------------
    def _handle_ask(
        self, question: str, image: str | None = None
    ) -> None:
        state = self.state
        with state.lock:
            state.append_history(question)
            self._begin_sse()
            try:
                self._sse("meta", state.snapshot())
                if image is not None:
                    self._run_image_question(question, image)
                elif question.startswith("/"):
                    self._run_command(question)
                else:
                    self._run_question(question)
                self._sse(
                    "done",
                    {
                        "state": state.snapshot(),
                        "benchmark": (
                            chat.format_benchmark_line(state.latest_benchmark)
                            if state.latest_benchmark is not None
                            else ""
                        ),
                    },
                )
            except ClientDisconnected:
                pass
            except Exception as exc:  # Keep the browser informed on failure.
                try:
                    self._sse("error", {"message": str(exc)})
                    self._sse("done", {"state": state.snapshot()})
                except ClientDisconnected:
                    pass

    def _handle_video_chat(
        self, video_id: str, question: str, reset: bool = False
    ) -> None:
        """Stream an answer from one video's transcript inside the video modal.

        This is the in-modal equivalent of ``/youtube transcript <url>``
        followed by a question, but it runs against a dedicated session so the
        main chat's conversation and scope are left untouched.
        """
        try:
            session = get_video_chat_state(video_id)
        except RuntimeError as exc:
            self._begin_sse()
            try:
                self._sse("error", {"message": str(exc)})
                self._sse("done", {})
            except ClientDisconnected:
                pass
            return

        previous_state = self.state
        self.state = session
        try:
            with session.lock:
                if reset:
                    session.reset_conversation()
                    session.cache.clear()
                    session.latest_answer = None
                    session.latest_sources = []
                    session.latest_benchmark = None
                self._begin_sse()
                try:
                    self._sse("meta", session.snapshot())
                    self._run_question(question)
                    self._sse(
                        "done",
                        {
                            "state": session.snapshot(),
                            "benchmark": (
                                chat.format_benchmark_line(
                                    session.latest_benchmark
                                )
                                if session.latest_benchmark is not None
                                else ""
                            ),
                        },
                    )
                except ClientDisconnected:
                    pass
                except Exception as exc:  # Keep the modal informed.
                    try:
                        self._sse("error", {"message": str(exc)})
                        self._sse("done", {"state": session.snapshot()})
                    except ClientDisconnected:
                        pass
        finally:
            self.state = previous_state

    def _handle_news_chat(
        self, news_id: str, question: str, reset: bool = False
    ) -> None:
        """Stream an answer from one learned news story in its modal."""
        try:
            session = get_news_chat_state(news_id)
        except RuntimeError as exc:
            self._begin_sse()
            try:
                self._sse("error", {"message": str(exc)})
                self._sse("done", {})
            except ClientDisconnected:
                pass
            return

        previous_state = self.state
        self.state = session
        try:
            with session.lock:
                if reset:
                    session.reset_conversation()
                    session.cache.clear()
                    session.latest_answer = None
                    session.latest_sources = []
                    session.latest_benchmark = None
                self._begin_sse()
                try:
                    self._sse("meta", session.snapshot())
                    self._run_question(question)
                    self._sse(
                        "done",
                        {
                            "state": session.snapshot(),
                            "benchmark": (
                                chat.format_benchmark_line(
                                    session.latest_benchmark
                                )
                                if session.latest_benchmark is not None
                                else ""
                            ),
                        },
                    )
                except ClientDisconnected:
                    pass
                except Exception as exc:
                    try:
                        self._sse("error", {"message": str(exc)})
                        self._sse("done", {"state": session.snapshot()})
                    except ClientDisconnected:
                        pass
        finally:
            self.state = previous_state

    def _handle_gutenberg_chat(
        self,
        book_id: str,
        question: str,
        *,
        title: str = "",
        text_url: str = "",
        reset: bool = False,
    ) -> None:
        """Stream an answer from one Project Gutenberg book.

        The book is split into article-style paragraph trunks on first use and
        searched with the same iterative, section-aware RAG loop as /article.
        A dedicated session keeps the main chat's scope and conversation
        untouched while the modal is open.
        """
        try:
            session = get_gutenberg_chat_state(
                book_id, text_url=text_url, title=title
            )
        except (RuntimeError, ValueError) as exc:
            self._begin_sse()
            try:
                self._sse("error", {"message": str(exc)})
                self._sse("done", {})
            except ClientDisconnected:
                pass
            return

        previous_state = self.state
        self.state = session
        try:
            with session.lock:
                if reset:
                    session.reset_conversation()
                    session.cache.clear()
                    session.latest_answer = None
                    session.latest_sources = []
                    session.latest_benchmark = None
                self._begin_sse()
                try:
                    self._sse("meta", session.snapshot())
                    self._run_question(question)
                    self._sse(
                        "done",
                        {
                            "state": session.snapshot(),
                            "benchmark": (
                                chat.format_benchmark_line(
                                    session.latest_benchmark
                                )
                                if session.latest_benchmark is not None
                                else ""
                            ),
                        },
                    )
                except ClientDisconnected:
                    pass
                except Exception as exc:  # Keep the book modal informed.
                    try:
                        self._sse("error", {"message": str(exc)})
                        self._sse("done", {"state": session.snapshot()})
                    except ClientDisconnected:
                        pass
        finally:
            self.state = previous_state

    def _handle_mathtutor_answer(
        self, session_id: str, index: int, student_answer: str = ""
    ) -> None:
        """Stream a worked solution for one uploaded-paper practice problem."""
        self._begin_sse()
        session = None
        problem = None
        try:
            session = _mathtutor_load_session(session_id)
            problems = session.get("problems", [])
            if index < 0 or index >= len(problems):
                raise ValueError("Invalid practice problem index")
            problem = problems[index]
            if not isinstance(problem, dict):
                raise ValueError("Malformed practice problem")

            self._sse(
                "mathtutor_answer_start",
                {
                    "id": str(session.get("id", "")),
                    "index": index,
                    "problem_id": int(problem.get("id", 0) or 0),
                },
            )
            prompt = _mathtutor_answer_prompt(session, problem, student_answer)
            response = chat.stream_answer(
                prompt,
                model=MATHUTOR_MODEL,
                emit=lambda text: self._sse_text("delta", text),
            )
            if response:
                with MATHUTOR_LOCK:
                    # Reload before recording: another answer request may
                    # have updated the same session in the meantime.
                    try:
                        latest = _mathtutor_load_session(session_id)
                    except ValueError:
                        latest = session
                    _mathtutor_append_history(
                        latest, problem, student_answer, response
                    )
            self._sse(
                "mathtutor_answer_done",
                {
                    "id": str(session.get("id", "")),
                    "index": index,
                    "text": response,
                },
            )
            self._sse("done", {})
        except ClientDisconnected:
            pass
        except (ValueError, RuntimeError, OSError) as exc:
            try:
                self._sse("error", {"message": str(exc)})
                self._sse("done", {})
            except ClientDisconnected:
                pass

    def _run_image_question(self, question: str, image: str) -> None:
        """Answer a question about one attached image with the vision model.

        Image questions deliberately bypass transcript retrieval: the answer
        comes from the picture and, for pronoun-heavy follow-ups, the recent
        conversation. The usual ``delta``/``done`` SSE events keep the browser
        rendering it like any other answer.
        """
        state = self.state
        original_question = (question or "Describe this image in detail.").strip()
        if len(original_question) > MAX_IMAGE_QUESTION_CHARS:
            raise ValueError(
                "The question is too long "
                f"(limit {MAX_IMAGE_QUESTION_CHARS:,} characters)."
            )
        resolved_question = original_question
        if state.conversation:
            resolution = state.resolve_question(original_question)
            if resolution.is_unrelated:
                self._sse_text(
                    "log",
                    "[conversation: the question is unrelated to the previous "
                    "turns; starting a fresh context]",
                )
            elif resolution.question != original_question:
                self._sse_text(
                    "log",
                    "[conversation: interpreting the follow-up as: "
                    f"{resolution.question}]",
                )
            resolved_question = resolution.question

        self._sse(
            "mode",
            {"model": IMAGE_MODEL, "label": "image question"},
        )
        self._sse_text("log", "[image: examining the attached image]")
        prompt = IMAGE_QUESTION_PROMPT.format(
            question=resolved_question,
            conversation=image_conversation_context(state.conversation),
        )

        models: list[str] = []
        for name in (IMAGE_MODEL, *IMAGE_FALLBACK_MODELS):
            if name and name not in models:
                models.append(name)
        answer = ""
        for index, model in enumerate(models):
            if index:
                self._sse(
                    "mode",
                    {"model": model, "label": "image question (vision)"},
                )
            try:
                answer = self._stream_image_answer(model, prompt, image)
                break
            except (ImageModelUnsupported, ImageModelUnavailable) as exc:
                if index == len(models) - 1:
                    if isinstance(exc, ImageModelUnsupported):
                        raise RuntimeError(
                            "No installed Ollama model can read images. Pull a "
                            "vision model or set OLLAMA_IMAGE_MODEL to one that is "
                            "installed."
                        ) from exc
                    raise RuntimeError(
                        "No local image model is available. Install a vision "
                        "model or set OLLAMA_IMAGE_MODEL."
                    ) from exc
                reason = (
                    "cannot read images"
                    if isinstance(exc, ImageModelUnsupported)
                    else "is not installed"
                )
                self._sse_text(
                    "log",
                    f"[image: the selected model {reason}; retrying with the "
                    "next local vision model]",
                )

        state.latest_answer = answer or None
        state.latest_sources = []
        state.record_turn(original_question, resolved_question, answer)

    def _stream_image_answer(
        self, model: str, prompt: str, image: str
    ) -> str:
        """Stream one vision model's answer to an image question."""
        payload = {
            "model": model,
            "prompt": prompt,
            "images": [image],
            "stream": True,
            "think": False,
            "keep_alive": chat.OLLAMA_KEEP_ALIVE,
            "options": {
                "temperature": 0.2,
                "num_ctx": IMAGE_NUM_CTX,
            },
        }
        answer_parts: list[str] = []
        try:
            with requests.post(
                f"{chat.OLLAMA_HOST}/api/generate",
                json=payload,
                stream=True,
                timeout=(10, 900),
            ) as response:
                if response.status_code >= 400:
                    detail = (response.text or "")[:800]
                    if is_multimodal_unsupported(detail):
                        raise ImageModelUnsupported(detail)
                    if response.status_code == 404 or is_model_unavailable(detail):
                        raise ImageModelUnavailable(detail)
                    response.raise_for_status()
                for line in response.iter_lines(decode_unicode=True):
                    if not line:
                        continue
                    try:
                        event = json.loads(line)
                    except json.JSONDecodeError:
                        continue
                    error = event.get("error")
                    if error:
                        if not answer_parts and is_multimodal_unsupported(error):
                            raise ImageModelUnsupported(str(error))
                        if not answer_parts and is_model_unavailable(error):
                            raise ImageModelUnavailable(str(error))
                        raise RuntimeError(f"Ollama error: {error}")
                    token = event.get("response", "")
                    if token:
                        answer_parts.append(token)
                        self._sse_text("delta", token)
                    if event.get("done"):
                        break
        except requests.HTTPError as exc:
            detail = ""
            if exc.response is not None:
                detail = exc.response.text or ""
            if is_multimodal_unsupported(detail):
                raise ImageModelUnsupported(detail) from exc
            if exc.response is not None and exc.response.status_code == 404:
                raise ImageModelUnavailable(detail) from exc
            raise RuntimeError(
                f"Image model request failed: {detail or exc}"
            ) from exc
        except requests.RequestException as exc:
            raise RuntimeError(
                f"Could not reach Ollama at {chat.OLLAMA_HOST}. "
                f"Is it running? ({exc})"
            ) from exc
        return "".join(answer_parts).strip()

    def _run_question(self, question: str) -> None:
        state = self.state
        article_mode = state.article_path is not None
        # Iterative evidence is reused within this question.
        iterative_cache: dict = {}
        answer_language: str | None = None
        retrieval_question = question
        if chat.contains_simplified_chinese(question):
            answer_language = "Simplified Chinese"
            self._sse_text(
                "log",
                "[Simplified Chinese detected; translating the question "
                "into English for BM25 retrieval and answering directly "
                "in Simplified Chinese]",
            )
            try:
                retrieval_question = chat.translate_to_english(question)
            except RuntimeError:
                # chat.answer() retries the translation and reports its own
                # warning if this pre-translation failed.
                retrieval_question = question

        context_sensitive = False
        if state.conversation:
            resolution = state.resolve_question(retrieval_question)
            if resolution.is_unrelated:
                self._sse_text(
                    "log",
                    "[conversation: the question is unrelated to the "
                    "previous turns; starting a fresh context]",
                )
            if resolution.question != retrieval_question:
                self._sse_text(
                    "log",
                    "[conversation: interpreting the follow-up as: "
                    f"{resolution.question}]",
                )
                retrieval_question = resolution.question
            context_sensitive = resolution.is_follow_up

        article_route: ArticleRoute | None = None
        article_prepare_selected: Callable | None = None
        article_prepare_stats: dict = {}
        article_attempt_depth: int | None = state.depth
        article_attempt_budget = state.max_context_tokens
        article_use_all_chunks = False
        article_prompt_prefix: str | None = None
        article_cache_variant = ""
        article_overview_mode = False
        if article_mode:
            article_route = article_question_route(
                retrieval_question,
                state.article_structure,
                iterative_default=state.article_iterative,
            )
            route_label = article_route.label
            if article_route.section is not None:
                route_label += f" {article_route.section}"
            if article_route.iterative:
                route_label += " + iterative"
            self._sse_text(
                "log",
                f"[article router: {route_label} ({article_route.source})]",
            )

        paragraph_chunks = (
            _iterative_paragraph_chunks(state.chunks)
            if article_mode
            else state.chunks
        )
        answer_chunks = (
            paragraph_chunks if article_mode else state.chunks
        )
        if article_route is not None:
            if article_route.label == "section" and article_route.section is not None:
                section = (state.article_structure or {}).get(
                    "_section_lookup", {}
                ).get(article_route.section)
                if section is None:
                    article_route = ArticleRoute(
                        "narrow", None, False, "section-missing"
                    )
                else:
                    answer_chunks = [
                        chunk
                        for chunk in paragraph_chunks
                        if int(section["start"]) <= chunk.index <= int(section["end"])
                    ]
                    if not answer_chunks:
                        answer_chunks = paragraph_chunks
                    article_attempt_depth = ARTICLE_SECTION_DEPTH
                    article_attempt_budget = ARTICLE_SECTION_CONTEXT_TOKENS
                    article_cache_variant = (
                        f"article-section:{article_route.section}"
                    )
                    article_prompt_prefix = (
                        "### ARTICLE SECTION\n"
                        f"The context is focused on the article section "
                        f"'{section.get('heading', '')}'. It includes the "
                        "section summary and neighboring paragraphs. Prefer "
                        "the specific paragraphs for facts and use the "
                        "section summary only for orientation. Ground every "
                        "claim in this context; if a detail is absent, omit "
                        "it rather than refusing.\n\n"
                    )
                    article_prepare_selected, article_prepare_stats = (
                        _make_article_expansion(
                            state.article_structure,
                            state.chunks,
                            ARTICLE_SECTION_CONTEXT_TOKENS,
                            lambda text: self._sse_text("log", text),
                        )
                    )
            elif article_route.iterative:
                evidence = _article_iterative_evidence(
                    retrieval_question,
                    state.chunks,
                    state.article_structure,
                    ARTICLE_ITERATIVE_EVIDENCE_TOKENS,
                    lambda text: self._sse_text("log", text),
                )
                if evidence:
                    answer_chunks = [chunk for chunk, _score in evidence]
                    article_use_all_chunks = True
                    article_attempt_depth = state.depth
                    article_attempt_budget = ARTICLE_ITERATIVE_EVIDENCE_TOKENS
                    article_cache_variant = "article-iterative"
                    article_prompt_prefix = (
                        "### ARTICLE EVIDENCE\n"
                        "The context below was gathered in several retrieval "
                        "rounds across the whole article, in document order; "
                        "it includes section summaries. Answer the complete "
                        "question, including enumerations and comparisons, "
                        "using every relevant part of this evidence. Do not "
                        "say that the context is insufficient; if one detail "
                        "is genuinely absent, answer the supported parts and "
                        "omit the unsupported detail.\n\n"
                    )
                else:
                    article_route = ArticleRoute(
                        "narrow", None, False, "iterative-empty"
                    )
                    self._sse_text(
                        "log",
                        "[article iterative: no new evidence found; using the "
                        "normal narrow retrieval path]",
                    )
            if article_route.label == "narrow" and not article_route.iterative:
                article_attempt_depth = state.depth
                article_attempt_budget = ARTICLE_NARROW_CONTEXT_TOKENS
                article_cache_variant = "article-narrow"
                article_prompt_prefix = (
                    "### ARTICLE CONTEXT\n"
                    "The context was retrieved from one article and includes "
                    "parent-section summaries and neighboring paragraphs. "
                    "Use the specific paragraphs for facts and the section "
                    "summary only for orientation. Ground every claim in the "
                    "context; if a specific detail is absent, omit it rather "
                    "than refusing.\n\n"
                )
                article_prepare_selected, article_prepare_stats = (
                    _make_article_expansion(
                        state.article_structure,
                        state.chunks,
                        ARTICLE_NARROW_CONTEXT_TOKENS,
                        lambda text: self._sse_text("log", text),
                    )
                )

        article_overview_mode = bool(
            article_route is not None and article_route.label == "overview"
        )
        article_overview_prefix: str | None = None
        article_overview_context_ready = False
        if article_overview_mode:
            digest_chunks = [
                chunk
                for chunk in state.chunks
                if chunk.index == ARTICLE_DIGEST_INDEX
            ]
            overview_chunks = [
                chunk
                for chunk in state.chunks
                if chunk.index == ARTICLE_OVERVIEW_INDEX
            ]
            requested_words = (
                article_requested_word_count(question)
                or article_requested_word_count(retrieval_question)
            )
            detailed = bool(
                ARTICLE_DETAILED_OVERVIEW_RE.search(question)
                or ARTICLE_DETAILED_OVERVIEW_RE.search(retrieval_question)
            )
            if digest_chunks and (requested_words or detailed):
                # The digest contains the title, outline, TL;DR, and
                # representative content from each major section. Use it as
                # the sole candidate so BM25 cannot replace it with unrelated
                # paragraphs that merely share generic words such as
                # "what"/"this"/"about".
                answer_chunks = digest_chunks[:1]
                context_kind = "article digest"
                context_detail = (
                    "its title, outline, TL;DR/summary, and representative "
                    "content from each major section"
                )
            elif overview_chunks:
                # Concise overview questions only need the compact overview;
                # the digest is reserved for long-form/detailed requests.
                answer_chunks = overview_chunks[:1]
                context_kind = "document overview"
                context_detail = "its title, outline, and TL;DR/summary"
            else:
                context_kind = ""
                context_detail = ""

            if context_kind:
                article_overview_context_ready = True
                if requested_words:
                    length_instruction = (
                        "Target length: approximately "
                        f"{requested_words:,} words. Write a detailed, "
                        "well-structured answer with headings where they "
                        "help. Develop the points supported by the context "
                        "instead of padding with unsupported facts. "
                    )
                elif detailed:
                    length_instruction = (
                        "Write a detailed, well-structured answer with "
                        "headings where they help. "
                    )
                else:
                    length_instruction = (
                        "Answer the user's question with a concise, "
                        "well-organized summary. "
                    )
                article_overview_prefix = (
                    "### ARTICLE OVERVIEW\n"
                    f"The context below is the {context_kind} of the entire "
                    f"article: {context_detail}. "
                    + length_instruction
                    + "Ground every claim in this context. If a specific "
                    "detail is absent, omit it rather than refusing; never "
                    "say that the context is insufficient.\n\n"
                )
                self._sse_text(
                    "log",
                    f"[article: answering from the {context_kind} "
                    "(title, outline, summary, and representative "
                    "sections)]",
                )

        detected_language = detect_code_language(retrieval_question)
        if (
            not detected_language
            and programming_feature_question(retrieval_question)
        ):
            # A follow-up such as "now add error handling" or "how does
            # ownership work?" may not repeat the language. Keep the previous
            # one only when the new question is about a language or feature.
            detected_language = state.code_language
        if detected_language:
            state.code_language = detected_language
            self._sse("meta", state.snapshot())
        code_language = detected_language

        # A question about a programming language or one of its features gets
        # related official-documentation excerpts next to the transcript
        # context. This uses the programming_docs_map and the documentation
        # index that the per-language search script already cached, so no
        # second crawler or extra model call is needed.
        programming_docs_prefix = ""
        programming_docs_stats: dict = {}
        if not article_mode and not explicit_code_request(retrieval_question):
            docs_language = programming_docs_language(
                retrieval_question, code_language
            )
            if docs_language:
                (
                    programming_docs_prefix,
                    programming_docs_stats,
                ) = programming_docs_context(
                    docs_language, retrieval_question
                )
                if programming_docs_prefix:
                    self._sse_text(
                        "log",
                        "[programming docs: loaded "
                        f"{programming_docs_stats['excerpts']} related "
                        f"{docs_language} excerpt(s) from "
                        f"{PROGRAMMING_DOCS_MAP[docs_language]['source_label']} "
                        "in addition to the transcript context]",
                    )
        golang_package_docs = (
            not article_mode
            and code_language == GO_LANGUAGE
            and golang_package_docs_request(retrieval_question)
        )
        rust_crate_docs = (
            not article_mode
            and code_language == RUST_LANGUAGE
            and rust_crate_docs_request(retrieval_question)
        )
        rust_code_docs = (
            not article_mode
            and code_language == RUST_LANGUAGE
            and rust_code_docs_request(retrieval_question)
        )
        php_symbol_docs = (
            not article_mode
            and code_language == PHP_LANGUAGE
            and php_symbol_docs_request(retrieval_question)
        )
        documentation_code = (
            not article_mode
            and documentation_code_request(code_language, retrieval_question)
        )
        coding_mode = (
            question_requests_coding(retrieval_question)
            or golang_package_docs
            or rust_crate_docs
            or rust_code_docs
            or php_symbol_docs
            or documentation_code
        )
        if (
            coding_mode
            and programming_docs_prefix
            and programming_explanation_question(retrieval_question)
            and not explicit_code_request(retrieval_question)
        ):
            # "What is ownership in Rust?" is an explanation, not a request
            # for a program. Keep the official documentation excerpts, but
            # answer from the normal prompt so the retrieved trunk context is
            # used alongside the docs instead of taking the docs-first
            # code-generation path.
            coding_mode = False
            documentation_code = False
            golang_package_docs = False
            rust_crate_docs = False
            rust_code_docs = False
            php_symbol_docs = False
            self._sse_text(
                "log",
                "[programming question: explanation detected; combining the "
                "official documentation excerpts with the transcript "
                "context instead of generating code]",
            )
        # Go, Rust, and PHP answer with the dedicated programming model for
        # both chat and code generation.
        coding_model = language_coding_model(code_language)
        chat_model = language_chat_model(code_language)
        if golang_package_docs:
            self._sse_text(
                "log",
                "[go: code-sample question references or implies a Go "
                "package; pkg.go.dev package documentation will be searched]",
            )
        elif documentation_code and code_language == GO_LANGUAGE:
            self._sse_text(
                "log",
                "[go: code-sample question detected; searching the "
                "official Go documentation and answering in Go]",
            )
        if rust_crate_docs:
            self._sse_text(
                "log",
                "[rust: code-sample question references a Rust crate; "
                "docs.rs crate documentation will be searched]",
            )
        elif rust_code_docs:
            self._sse_text(
                "log",
                "[rust: code-sample question detected; searching the official "
                "Rust documentation instead of GitHub]",
            )
        if php_symbol_docs:
            self._sse_text(
                "log",
                "[php: code-sample question references a PHP function, "
                "class, or extension; the dedicated manual page will be "
                "searched]",
            )
        elif documentation_code and code_language == PHP_LANGUAGE:
            self._sse_text(
                "log",
                "[php: code-sample question detected; searching the "
                "official PHP manual and answering in PHP]",
            )
        if coding_mode:
            mode_label = (
                f"{code_language} coding mode"
                if code_language
                else "coding mode"
            )
            self._sse(
                "mode",
                {
                    "mode": "coding",
                    "model": coding_model,
                    "think": False,
                    "language": code_language or "",
                    "label": mode_label,
                },
            )
            language_note = (
                f"; all generated code will be {code_language}"
                if code_language
                else ""
            )
            self._sse_text(
                "log",
                "[coding mode: code/architecture question detected; using "
                f"{coding_model} without thinking{language_note}]",
            )

        def attempt(
            chunks: list[chat.Chunk],
            scope_label: str,
            report_errors: bool = True,
            prompt_prefix_override: str | None = None,
            cache_variant: str = "",
            use_all_chunks: bool = False,
            depth_override: int | None = None,
            max_context_tokens_override: int | None = None,
            prepare_selected: Callable | None = None,
            prepare_stats: dict | None = None,
        ):
            """Answer once from the supplied corpus scope."""
            attempt_stats: dict = {}
            # A documentation-backed language must use that language for any
            # code in the answer, not only for answers classified as coding
            # questions. A normal explanation may still include a snippet.
            prompt_prefix = (
                prompt_prefix_override
                if prompt_prefix_override is not None
                else (
                    coding_prompt_prefix(code_language)
                    if coding_mode
                    else language_coding_directive(code_language)
                )
            )
            if programming_docs_prefix:
                # Keep the coding-mode rules first (they are the highest
                # priority), then add the documentation reference next to the
                # retrieved transcript context.
                prompt_prefix = prompt_prefix + programming_docs_prefix
            # /article gathers evidence in small rounds and stops as soon as
            # the small model says the gathered paragraphs are sufficient.
            # Apply the same loop to normal transcript questions. Article mode
            # already did this above; coding questions keep their
            # documentation-first path. A cached iterative answer is reused
            # without repeating the retrieval rounds.
            iterative_eligible = (
                not article_mode and not use_all_chunks and not coding_mode
            )
            if iterative_eligible:
                iterative_variant = (
                    f"{cache_variant}+youtube-iterative"
                    if cache_variant
                    else "youtube-iterative"
                )
                iterative_cached = state.cache.get(
                    state.cache_key(
                        retrieval_question,
                        answer_language,
                        conversation_revision=(
                            state.conversation_revision
                            if context_sensitive
                            else None
                        ),
                        language=code_language,
                        coding_mode=coding_mode,
                        prompt_variant=iterative_variant,
                    )
                ) is not None
                if iterative_cached:
                    cache_variant = iterative_variant
                else:
                    evidence_budget = (
                        max_context_tokens_override
                        if max_context_tokens_override is not None
                        else state.max_context_tokens
                    )
                    evidence = _youtube_iterative_evidence(
                        retrieval_question,
                        chunks,
                        evidence_budget,
                        lambda text: self._sse_text("log", text),
                        depth=(
                            depth_override
                            if depth_override is not None
                            else state.depth
                        ),
                        cache=iterative_cache,
                    )
                    if evidence:
                        chunks = [chunk for chunk, _score in evidence]
                        use_all_chunks = True
                        cache_variant = iterative_variant
                        prompt_prefix = (
                            YOUTUBE_ITERATIVE_PROMPT_PREFIX + prompt_prefix
                        )
                    elif state.news_id is not None or state.transcript is not None:
                        # The scoped corpus is small and there is no BM25
                        # match. Answer from its reading-order text rather
                        # than ending the stream with no message at all.
                        scoped_chunks = _scoped_context_fallback(
                            chunks, evidence_budget
                        )
                        if scoped_chunks:
                            chunks = scoped_chunks
                            use_all_chunks = True
                            cache_variant = (
                                f"{cache_variant}+scoped-context-fallback"
                                if cache_variant
                                else "scoped-context-fallback"
                            )
                            prompt_prefix = (
                                SCOPED_CONTEXT_FALLBACK_PROMPT_PREFIX
                                + prompt_prefix
                            )
                            self._sse_text(
                                "log",
                                "[no matching paragraph in this scoped "
                                "source; using its reading-order text as "
                                "context]",
                            )
            emit_delta = lambda text: self._sse("delta", {"text": text})
            docs_backed = language_uses_official_docs(code_language)
            answer_filter = InsufficientContextFilter(enabled=docs_backed)
            fence_filter = CodeFenceRepairFilter(enabled=docs_backed)

            def emit_answer_delta(text: str) -> None:
                cleaned = answer_filter.feed(text)
                if not cleaned:
                    return
                cleaned = fence_filter.feed(cleaned)
                if cleaned:
                    emit_delta(cleaned)

            try:
                response, sources = chat.answer(
                    retrieval_question,
                    chunks,
                    depth=(
                        depth_override
                        if depth_override is not None
                        else state.depth
                    ),
                    max_context_tokens=(
                        max_context_tokens_override
                        if max_context_tokens_override is not None
                        else state.max_context_tokens
                    ),
                    answer_language=answer_language,
                    cache=state.cache,
                    cache_key=state.cache_key(
                        retrieval_question,
                        answer_language,
                        conversation_revision=(
                            state.conversation_revision
                            if context_sensitive
                            else None
                        ),
                        language=code_language,
                        coding_mode=coding_mode,
                        prompt_variant=cache_variant,
                    ),
                    stats=attempt_stats,
                    progressive=state.progressive,
                    prompt_prefix=prompt_prefix,
                    model=(
                        language_coding_model(code_language)
                        if coding_mode
                        else language_chat_model(code_language)
                    ),
                    think=False,
                    use_all_chunks=use_all_chunks,
                    prepare_selected=prepare_selected,
                    emit=emit_answer_delta,
                    log=lambda text: self._sse_text("log", text),
                )
            except RuntimeError as exc:
                if report_errors:
                    self._sse("error", {"message": str(exc)})
                return None, [], attempt_stats, str(exc)
            except Exception as exc:  # Keep unexpected failures visible.
                message = f"{type(exc).__name__}: {exc}"
                if report_errors:
                    self._sse("error", {"message": message})
                return None, [], attempt_stats, message
            tail = answer_filter.flush()
            if tail:
                tail = fence_filter.feed(tail)
                if tail:
                    emit_delta(tail)
            fence_tail = fence_filter.flush()
            if fence_tail:
                emit_delta(fence_tail)
            if docs_backed and response is not None:
                # Keep the returned text in sync with what was streamed: the
                # refusal is already suppressed and malformed continuation
                # fences are already repaired, so neither can reappear in the
                # saved answer, annotations, or a documentation merge.
                response = repair_code_fences(
                    strip_leading_insufficient_context(response)
                )
            if prepare_stats is not None and prepare_stats.get("added"):
                attempt_stats["article_expansion"] = dict(prepare_stats)
            if programming_docs_stats:
                attempt_stats["programming_docs"] = dict(
                    programming_docs_stats
                )
            return response, sources or [], attempt_stats, None

        scope_label = (
            f"article {state.article_path!r}"
            if article_mode
            else (
                f"transcript {state.transcript!r}"
                if state.transcript is not None
                else "global corpus"
            )
        )
        documentation_language = (
            None
            if article_mode
            else (
                code_language
                if language_uses_official_docs(code_language)
                else None
            )
        )
        local_model = coding_model if coding_mode else chat_model
        documentation_first_done = False
        first_response: str | None = None
        first_sources: list[tuple[chat.Chunk, float]] = []
        first_stats: dict = {}
        response: str | None = None
        sources: list[tuple[chat.Chunk, float]] = []
        stats: dict = {}

        # A Go, Rust, or PHP code question must consult the official
        # documentation before the general local model generates any code.
        # The docs answer is streamed first; only then is the local coding
        # model called, with that answer inserted in its prompt as the
        # authoritative reference. This prevents a code sample being invented
        # before the corresponding documentation has been retrieved.
        if documentation_language and coding_mode:
            documentation_first_done = True
            descriptor = documentation_descriptor(documentation_language)
            assert descriptor is not None
            self._sse_answer_source(
                language=documentation_language,
                source=SOURCE_DOCUMENTATION,
                label=(
                    f"official docs {descriptor['source_label']} via "
                    f"{coding_model}"
                ),
                model=coding_model,
                url=descriptor["source_url"],
                detail=(
                    "the official documentation is retrieved before any "
                    "local code generation"
                ),
            )
            self._sse_text(
                "log",
                f"[docs-first: consulting {descriptor['source_label']} before "
                f"{coding_model} generates code]",
            )
            doc_answer, doc_error = descriptor["runner"](
                retrieval_question,
                log=lambda text: self._sse_text("log", text),
                emit=lambda text: self._sse_text("delta", text),
            )

            doc_incomplete = bool(doc_answer) and (
                coding_mode
                and documentation_answer_is_insufficient(doc_answer)
            )
            if doc_answer and doc_incomplete:
                # The excerpts were too thin for the documentation assistant,
                # which streamed a refusal. A code request must not end there:
                # clear the refusal and let the local coding model write the
                # program from the limited excerpts plus its own knowledge.
                self._sse_text(
                    "log",
                    f"[docs-first: the {descriptor['source_label']} excerpts "
                    "were incomplete; asking the local coding model to "
                    "generate the code from the limited excerpts instead of "
                    "refusing]",
                )
                self._sse_answer_source(
                    language=documentation_language,
                    source=SOURCE_OLLAMA,
                    label=f"local LLM {coding_model} (code rescue)",
                    model=coding_model,
                    detail=(
                        "the official documentation excerpts were incomplete; "
                        "the local model is generating the requested code "
                        "from those excerpts and its own programming knowledge"
                    ),
                )
                self._sse(
                    "reset",
                    {"reason": "documentation answer was incomplete"},
                )
                rescue_response, rescue_metrics = stream_code_rescue(
                    retrieval_question,
                    documentation_language,
                    doc_answer,
                    descriptor,
                    emit=lambda text: self._sse("delta", {"text": text}),
                )
                if rescue_response and rescue_response.strip():
                    response = rescue_response + "\n"
                    sources = []
                    stats = dict(rescue_metrics)
                    stats["documentation_first"] = {
                        "used": True,
                        "source": descriptor["source_url"],
                        "doc_chars": len(doc_answer),
                        "doc_insufficient": True,
                        "local_code_rescue": True,
                        "error": doc_error,
                    }
                    first_response = response
                    first_sources = []
                    first_stats = stats
                else:
                    # Extremely unlikely: the rescue model returned nothing.
                    # Restore the documentation answer rather than leaving the
                    # screen empty.
                    self._sse_text(
                        "log",
                        "[docs-first: the local code rescue returned no text; "
                        "keeping the documentation answer]",
                    )
                    response = doc_answer + "\n"
                    self._sse_text("delta", response)
                    sources = []
                    stats = {
                        "documentation_first": {
                            "used": True,
                            "source": descriptor["source_url"],
                            "doc_chars": len(doc_answer),
                            "doc_insufficient": True,
                            "local_code_rescue": False,
                            "error": doc_error,
                        }
                    }
                    first_response = response
                    first_sources = []
                    first_stats = stats
            elif doc_answer:
                # The official documentation answer is streamed as the final
                # answer. The local model is not asked for a second
                # "Complementary notes from the local transcript context"
                # section: the documentation already contains the code and
                # explanation, and the extra section only duplicates it.
                stats = {
                    "documentation_first": {
                        "used": True,
                        "source": descriptor["source_url"],
                        "doc_chars": len(doc_answer),
                        "streamed_before_local": True,
                        "complementary_notes_removed": True,
                        "error": doc_error,
                    }
                }
                response = doc_answer + "\n"
                sources = []
                self._sse_answer_source(
                    language=documentation_language,
                    source=SOURCE_DOCUMENTATION,
                    label=(
                        f"official docs {descriptor['source_label']} via "
                        f"{coding_model}"
                    ),
                    model=coding_model,
                    url=descriptor["source_url"],
                    detail=(
                        "the answer comes from the official documentation; "
                        "no additional local complement is appended"
                    ),
                )
                first_response = response
                first_sources = []
                first_stats = stats
            else:
                self._sse_text(
                    "log",
                    f"[docs-first: documentation unavailable: {doc_error}; "
                    "falling back to the local coding model]",
                )
                self._sse_answer_source(
                    language=documentation_language,
                    source=SOURCE_OLLAMA,
                    label=f"local LLM {local_model}",
                    model=local_model,
                    detail=(
                        "the official documentation was unavailable; the "
                        "local model is answering as a fallback"
                    ),
                )
                (
                    first_response,
                    first_sources,
                    first_stats,
                    error,
                ) = attempt(
                    answer_chunks,
                    scope_label,
                    prompt_prefix_override=(
                        article_overview_prefix
                        if article_overview_prefix is not None
                        else article_prompt_prefix
                    ),
                    cache_variant=article_cache_variant,
                    use_all_chunks=(
                        article_overview_context_ready
                        or article_use_all_chunks
                    ),
                    depth_override=article_attempt_depth,
                    max_context_tokens_override=article_attempt_budget,
                    prepare_selected=article_prepare_selected,
                    prepare_stats=article_prepare_stats,
                )
                if error:
                    return
                response, sources, stats = (
                    first_response,
                    first_sources,
                    first_stats,
                )

        if not documentation_first_done:
            if documentation_language:
                self._sse_answer_source(
                    language=documentation_language,
                    source=SOURCE_OLLAMA,
                    label=f"local LLM {local_model}",
                    model=local_model,
                    detail=(
                        "the answer is being generated by the local Ollama "
                        "model from the transcript knowledge base"
                    ),
                )
            first_response, first_sources, first_stats, error = attempt(
                answer_chunks,
                scope_label,
                prompt_prefix_override=(
                    article_overview_prefix
                    if article_overview_prefix is not None
                    else article_prompt_prefix
                ),
                cache_variant=article_cache_variant,
                use_all_chunks=(
                    article_overview_context_ready or article_use_all_chunks
                ),
                depth_override=article_attempt_depth,
                max_context_tokens_override=article_attempt_budget,
                prepare_selected=article_prepare_selected,
                prepare_stats=article_prepare_stats,
            )
            if error:
                return
            response, sources, stats = (
                first_response,
                first_sources,
                first_stats,
            )

        # For non-coding questions in a documentation-backed language, the
        # local knowledge base still answers first and the official docs are
        # consulted when it cannot. Coding questions already took the
        # documentation-first path above, so they are never retried here.
        local_answer_insufficient = answer_is_insufficient(first_response)
        documentation = None
        if (
            not documentation_first_done
            and documentation_language
            and (coding_mode or local_answer_insufficient)
        ):
            documentation = documentation_descriptor(documentation_language)

        if documentation is not None:
            if not coding_mode:
                self._sse_text(
                    "log",
                    "the local knowledge base could not answer; consulting "
                    f"{documentation['source_label']} with {coding_model}",
                )
            response, sources, stats = attach_documentation_answer(
                first_response,
                first_sources,
                first_stats,
                question=retrieval_question,
                emit_delta=lambda text: self._sse_text("delta", text),
                emit_reset=lambda reason: self._sse(
                    "reset", {"reason": reason}
                ),
                emit_log=lambda text: self._sse_text("log", text),
                emit_source=self._sse_doc_source,
                **documentation,
            )
        # For every other language, an unusable coding answer falls back to
        # GitHub's public API and an answer built from code snippets. Go,
        # Rust, and PHP never take this path: their code samples come from
        # the official documentation above.
        elif (
            coding_mode
            and not article_mode
            and github_code_fallback_allowed(code_language)
            and answer_is_insufficient(first_response)
        ):
            self._sse_text(
                "log",
                "[coding mode: no usable answer; searching GitHub for "
                "related code snippets]",
            )
            snippets = github_code_snippets(
                retrieval_question,
                limit=GITHUB_SNIPPET_LIMIT,
                log=lambda text: self._sse_text("log", text),
                language_guard=code_language,
            )
            if snippets:
                if first_response and first_response.strip():
                    self._sse("reset", {"reason": "github code fallback"})
                response = format_github_code_answer(snippets)
                self._sse_text("delta", response)
                sources = []
                stats = dict(first_stats)
                stats["github_fallback"] = {
                    "snippets": len(snippets),
                    "sources": [
                        {
                            "repo": snippet.get("repo"),
                            "path": snippet.get("path"),
                            "url": snippet.get("url"),
                        }
                        for snippet in snippets
                    ],
                }
                self._sse_text(
                    "log",
                    f"[github fallback: using {len(snippets)} code "
                    "snippet(s)]",
                )
            else:
                self._sse_text(
                    "log",
                    "[github fallback: no related code snippets found]",
                )

        if response is None:
            # An isolated modal must never finish with an empty answer. The
            # scoped fallback above normally prevents this, but a model or
            # upstream failure can still produce no text; make that visible.
            if state.news_id is not None:
                message = (
                    "I could not produce an answer from this learned story. "
                    "Try rephrasing the question or learning the article again."
                )
                self._sse_text("delta", message)
                response = message
                sources = []
                stats = dict(stats or {})
            elif state.transcript is not None:
                message = (
                    "I could not produce an answer from this transcript. "
                    "Try rephrasing the question or learning the video again."
                )
                self._sse_text("delta", message)
                response = message
                sources = []
                stats = dict(stats or {})
            else:
                return
        if documentation_language and response is not None:
            # Final safety net: whatever path produced the answer (local,
            # documentation merge, or code rescue), never persist or annotate
            # a leading "provided context is insufficient" refusal.
            response = strip_leading_insufficient_context(response)
        state.latest_answer = response
        state.latest_sources = sources
        stats["coding_mode"] = coding_mode
        stats["model"] = coding_model if coding_mode else chat_model
        state.latest_benchmark = stats
        annotations = build_answer_annotations(response, sources)
        if article_mode:
            # Article paragraphs have no YouTube source. Keep the paragraph
            # mapping for the UI but suppress the video icon/link and add the
            # nearest cached section heading as a precise citation.
            for item in annotations:
                item["video_id"] = ""
                item["url"] = ""
                title = state.article_title or "article"
                section = _article_source_section(
                    state.article_structure, int(item.get("chunk_index", -1))
                )
                heading = str(section.get("heading", "")) if section else ""
                item["title"] = (
                    f"{title} — {heading}" if heading else title
                )
        self._sse("annotations", {"items": annotations})
        state.record_turn(question, retrieval_question, response)
        if state.benchmark_mode:
            self._sse_text("log", chat.format_benchmark_line(stats))

    def _run_command(self, text: str) -> None:
        name, _, argument = text.partition(" ")
        name = name.lower()
        argument = argument.strip()

        if name in {"/exit", "/quit", "/q"}:
            self._sse_text("output", "\nBye. Close the browser tab to exit.\n")
        elif name == "/help":
            self._command_help()
        elif name == "/memory":
            report = chat.index_memory_report(
                self.state.chunks, article=self.state.article_path
            )
            self._sse_text("output", "\n" + report + "\n")
        elif name == "/refresh":
            self._command_refresh()
        elif name == "/youtube":
            self._command_youtube(argument)
        elif name == "/ask":
            self._command_ask(argument)
        elif name == "/news":
            self._command_news(argument)
        elif name == "/mdn":
            self._command_mdn(argument)
        elif name == "/googlenews":
            self._command_googlenews(argument)
        elif name == "/howstuffworks":
            self._command_howstuffworks(argument)
        elif name == "/wikivoyage":
            self._command_wikivoyage(argument)
        elif name == "/medium":
            self._command_medium(argument)
        elif name == "/ai_news_podcast":
            self._command_ai_news_podcast(argument)
        elif name == "/gutenberg":
            self._command_gutenberg(argument)
        elif name == "/openlibrary":
            self._command_openlibrary(argument)
        elif name == "/changelog":
            self._command_changelog(argument)
        elif name == "/wiki":
            self._command_wiki(argument)
        elif name == "/wiki_article":
            self._command_wiki_article(argument)
        elif name == "/article":
            self._command_article(argument)
        elif name == "/mathtutor":
            self._command_mathtutor(argument)
        elif name == "/dict":
            self._command_dict(argument)
        elif name == "/depth":
            self._command_depth(argument)
        elif name == "/status":
            state = self.state
            report = chat.chat_status_report(
                state.transcript,
                state.chunks,
                state.depth,
                state.max_context_tokens,
                len(state.cache),
                article=state.article_path,
            )
            report += (
                f"\n  conversation: {len(state.conversation)} turn(s) "
                f"(kept: last {CONVERSATION_MAX_TURNS})"
            )
            self._sse_text("output", "\n" + report + "\n")
        elif name == "/sources":
            self._command_sources()
        elif name == "/benchmark":
            self._command_benchmark(argument)
        else:
            self._sse_text(
                "output",
                f"\nUnknown command: {name}. Use /help to see the available "
                "commands.\n",
            )

    def _command_help(self) -> None:
        lines = ["", "Commands:"]
        for name, description in sorted(WEB_COMMANDS.items()):
            lines.append(f"  {name:<20} {description}")
        lines.extend(
            [
                "",
                "Keyboard:",
                "  Esc          normal mode        i / a / A     insert",
                "  h / l / 0 / $                 move left / right / start / end",
                "  w / b                        move by word",
                "  x / dw / dd / D              delete char / word / line / to end",
                "  v / V                        visual / visual-line mode",
                "  u                            undo",
                "  Tab                          complete command or transcript",
                "  Up / Down, or k / j          previous / next history entry",
                "  Click any word in an answer  open a streaming /dict popup",
                "",
            ]
        )
        self._sse_text("output", "\n".join(lines))

    def _command_refresh(self) -> None:
        state = self.state
        if state.article_path is not None:
            self._refresh_article(state.article_path)
            return
        new_chunks, printed, error = load_all_chunks_captured()
        if error or new_chunks is None:
            self._sse_text(
                "output",
                f"\nCannot refresh the knowledge corpus: {error}\n",
            )
            return
        state.global_chunks = new_chunks
        state.replace_chunks(new_chunks)
        state.transcript = None
        state.cache.clear()
        self._sse_text(
            "output",
            "\n" + (printed + "\n" if printed else "")
            + chat.index_memory_report(new_chunks, build_index=False)
            + "\nGlobal corpus refreshed.\n",
        )
        self._sse("meta", state.snapshot())

    def _command_transcript(
        self, argument: str, direct: bool = True, title_only: bool = False
    ) -> None:
        state = self.state
        available = set(chat.available_video_ids())
        if not available:
            self._sse_text(
                "output",
                f"\nNo transcripts found in {chat.OUTPUT_DIR}. Use /youtube learn first.\n",
            )
            return
        if not argument:
            self._sse_text(
                "output",
                "\nUsage: /youtube transcript <hash|URL|keyword> "
                "(Tab completes ids and titles)\n",
            )
            return

        video_id = None if title_only else chat.extract_transcript_video_id(argument)
        if video_id is not None:
            if video_id not in available:
                self._sse_text(
                    "output",
                    f"\nNo trunk files for transcript {video_id!r}. "
                    "Use /youtube learn first.\n",
                )
                return
            self._activate_transcript(video_id)
            return

        matches = chat.search_transcript_titles(argument)
        usable = [match for match in matches if match.video_id in available]
        if not usable:
            if matches:
                self._sse_text(
                    "output",
                    f"\nFound {len(matches)} title match(es) for "
                    f"{argument!r}, but none have trunks. Use /youtube learn first.\n",
                )
            else:
                self._sse_text(
                    "output", f"\nNo transcript titles matched {argument!r}.\n"
                )
            return
        if len(usable) == 1:
            self._activate_transcript(usable[0].video_id)
            return

        lines = [f"\nFound {len(usable)} transcript(s) for {argument!r}:"]
        for index, match in enumerate(usable, start=1):
            lines.append(f"  {index:>4}. {match.video_id}  {match.title}")
        lines.append(
            "Select one with /youtube transcript <video_id> or click a result below.\n"
        )
        self._sse_text("output", "\n".join(lines))
        self._sse(
            "choices",
            {
                "kind": "transcript",
                "items": [
                    {
                        "value": match.video_id,
                        "description": match.title,
                    }
                    for match in usable
                ],
            },
        )

    def _refresh_article(self, article_path: str) -> None:
        """Re-read the active article and rebuild its paragraph trunks."""
        state = self.state
        path = Path(article_path)
        try:
            new_chunks, title, structure = _load_article_chunks(path)
        except (OSError, ValueError) as exc:
            self._sse_text(
                "output",
                f"\nCannot refresh article {article_path!r}: {exc}\n",
            )
            return
        state.replace_chunks(
            new_chunks,
            article_path=article_path,
            article_title=title,
            article_structure=structure,
            article_iterative=state.article_iterative,
        )
        state.cache.clear()
        self._sse_text(
            "output",
            "\n"
            + chat.index_memory_report(
                new_chunks, build_index=False, article=article_path
            )
            + f"\nNow answering from article {article_path!r} "
            f"({sum(1 for chunk in new_chunks if chunk.index > 0)} "
            "paragraph(s)).\n",
        )
        self._sse("meta", state.snapshot())

    def _activate_transcript(self, video_id: str) -> None:
        state = self.state
        new_chunks, printed, error = load_chunks_captured(video_ids={video_id})
        if error or new_chunks is None:
            self._sse_text(
                "output",
                f"\nCannot switch to transcript {video_id!r}: {error}\n",
            )
            return
        state.replace_chunks(new_chunks)
        state.transcript = video_id
        state.cache.clear()
        self._sse_text(
            "output",
            "\n" + (printed + "\n" if printed else "")
            + chat.index_memory_report(new_chunks, build_index=False)
            + f"\nNow answering only from transcript {video_id!r}.\n",
        )
        self._sse("meta", state.snapshot())

    def _command_youtube(self, argument: str) -> None:
        """Dispatch /youtube <subcommand> [argument]."""
        subcommand, _, rest = argument.partition(" ")
        subcommand = subcommand.strip().lower()
        rest = rest.strip()
        if not subcommand:
            lines = [
                "\nUsage: /youtube <subcommand> ...",
                "",
            ]
            for name, description in YOUTUBE_SUBCOMMANDS.items():
                lines.append(f"  /youtube {name:<14} {description}")
            self._sse_text("output", "\n".join(lines) + "\n")
            return
        if subcommand == "learn":
            self._command_learn(rest)
        elif subcommand == "learn_topic":
            self._command_learn_topic(rest)
        elif subcommand == "learn_playlist":
            self._command_learn_playlist(rest)
        elif subcommand == "search":
            self._command_youtube_search(rest)
        elif subcommand == "transcript":
            self._command_transcript(rest, direct=False)
        elif subcommand == "search_transcript":
            self._command_transcript(rest, direct=False, title_only=True)
        elif subcommand == "bookmark":
            self._command_youtube_bookmark(rest)
        else:
            self._sse_text(
                "output",
                f"\nUnknown /youtube subcommand: {subcommand!r}. "
                "Use /youtube to see the available subcommands.\n",
            )

    def _command_youtube_bookmark(self, argument: str) -> None:
        """List the videos saved with the player modal's Bookmark button."""
        action = argument.strip().lower()
        if action != "list":
            self._sse_text(
                "output",
                "\nUsage: /youtube bookmark list\n"
                "Use the Bookmark button in a video player to save a video.\n",
            )
            return
        entries = read_youtube_bookmarks()
        if not entries:
            self._sse_text(
                "output",
                "\nNo bookmarked videos yet. Open a /youtube search video and "
                "click Bookmark.\n",
            )
            return
        # Render the bookmarks with the same cards as /youtube search so a
        # saved video can be played, summarized, or learned in place.  The
        # entries are stored oldest-first; show the newest first.
        videos: list[dict] = []
        for entry in reversed(entries):
            video_id = str(entry.get("id") or "")
            if not re.fullmatch(r"[A-Za-z0-9_-]{11}", video_id):
                continue
            try:
                duration = max(0, int(entry.get("duration") or 0))
            except (TypeError, ValueError):
                duration = 0
            date = str(entry.get("date") or "").strip()
            videos.append(
                {
                    "id": video_id,
                    "title": str(entry.get("title") or video_id),
                    "thumbnail": str(entry.get("thumbnail") or ""),
                    "duration": duration,
                    "upload_date": date,
                    "published": date,
                    "published_text": date,
                    "url": f"https://www.youtube.com/watch?v={video_id}",
                }
            )
        if not videos:
            self._sse_text(
                "output", "\nNo valid bookmarked videos found.\n"
            )
            return
        self._sse_text(
            "output",
            f"\nBookmarked videos ({len(videos)}, latest first):\n",
        )
        self._sse(
            "videos",
            {
                "keyword": "",
                "items": videos,
                "has_more": False,
                "pending": False,
                "heading": "Bookmarked videos",
                "note": (
                    f"{len(videos)} saved video"
                    f"{'' if len(videos) == 1 else 's'} · latest first"
                ),
            },
        )

    def _command_youtube_search(self, argument: str) -> None:
        """Search YouTube's own results page and show video cards.

        Usage: /youtube search "<keyword>"
        The command loads ``https://www.youtube.com/results?search_query=
        <keyword>`` and extracts the video metadata embedded in the page.
        Each card carries the watch url, title, channel, duration, thumbnail,
        and relative publish date.  Each card opens a centered player, and its
        Summary/Learn buttons keep working from the video id.  The fetched
        result page is cached so the "More" button can reveal the remaining
        videos without another search.
        """
        keyword = argument.strip()
        if len(keyword) >= 2 and keyword[0] == keyword[-1] and keyword[0] in "\"'":
            keyword = keyword[1:-1].strip()
        if not keyword:
            self._sse_text(
                "output", '\nUsage: /youtube search "<keyword>"\n'
            )
            return

        page_url = (
            "https://www.youtube.com/results?search_query="
            + quote_plus(keyword)
        )
        self._sse_text(
            "output",
            f"\n/youtube search: loading {page_url}\n",
        )
        # Fetch one extra result so the UI knows whether a "More" button is
        # useful without making a second search just to find out.
        candidate_state: dict[str, object] = {"items": [], "has_more": False}

        def on_candidates(items: list[dict], has_more: bool) -> None:
            if not items:
                return
            candidate_state["items"] = items
            candidate_state["has_more"] = has_more
            # Paint the cards immediately if a future backend streams
            # candidates before the final result set.
            self._sse(
                "videos",
                {
                    "keyword": keyword,
                    "items": items[:YOUTUBE_SEARCH_PAGE_SIZE],
                    "has_more": has_more,
                    "pending": True,
                },
            )

        try:
            results = self._cached_youtube_search(
                keyword,
                YOUTUBE_SEARCH_PAGE_SIZE + 1,
                on_candidates=on_candidates,
            )
        except RuntimeError as exc:
            self._sse_text("output", f"\n/youtube search: {exc}\n")
            return

        videos = results[:YOUTUBE_SEARCH_PAGE_SIZE]
        if not videos:
            pending_items = list(candidate_state["items"])[
                :YOUTUBE_SEARCH_PAGE_SIZE
            ]
            if pending_items:
                self._sse_text(
                    "output",
                    "\n/youtube search: showing the matching videos found so "
                    "far.\n",
                )
                self._sse(
                    "videos",
                    {
                        "keyword": keyword,
                        "items": pending_items,
                        "has_more": bool(candidate_state["has_more"]),
                        "pending": False,
                        "captions_unverified": True,
                    },
                )
                self._merge_youtube_cache(
                    keyword, list(candidate_state["items"])
                )
                self._start_youtube_prefetch(keyword)
                return
            self._sse_text(
                "output",
                "\n/youtube search: no YouTube videos were found for "
                f"{keyword!r}.\n",
            )
            return

        self._sse_text(
            "output",
            f"\n/youtube search: {len(videos)} video(s) for {keyword!r}. "
            "Click a video to play it here, or use Summary for an AI overview.\n",
        )
        self._sse(
            "videos",
            {
                "keyword": keyword,
                "items": videos,
                "has_more": len(results) > YOUTUBE_SEARCH_PAGE_SIZE,
                "pending": False,
                "heading": "YouTube videos · search page",
                "note": (
                    f"{len(videos)} result"
                    f"{'' if len(videos) == 1 else 's'} · "
                    "youtube.com/results"
                ),
            },
        )
        if len(results) > YOUTUBE_SEARCH_PAGE_SIZE:
            # Warm the remaining cards from the same results page while the
            # user is still looking at the first one.
            self._start_youtube_prefetch(keyword)

    def _youtube_search_command(
        self,
        keyword: str,
        limit: int,
        progressive: bool,
        exclude: set[str] | None = None,
    ) -> list[str]:
        command = [
            sys.executable,
            "-u",
            str(SCRIPT_DIR / "youtube_news_search.py"),
            "--search-page",
            "--progressive-json" if progressive else "--json",
            "--limit",
            str(limit),
        ]
        if exclude:
            # Attach the value with ``=`` instead of passing it as a separate
            # argument.  YouTube ids may start with ``-`` (for example
            # ``-PcBFgkrR2o``), and argparse would otherwise mistake the
            # comma-separated exclusion list for an option and fail with
            # "argument --exclude: expected one argument".
            command.append("--exclude=" + ",".join(sorted(exclude)))
        command.extend(["--", keyword])
        return command

    def _normalize_youtube_items(
        self, raw_items: list[dict], limit: int
    ) -> list[dict]:
        videos = []
        seen = set()
        for video in raw_items or []:
            if not isinstance(video, dict):
                continue
            video_id = str(video.get("id") or "")
            if (
                not re.fullmatch(r"[A-Za-z0-9_-]{11}", video_id)
                or video_id in seen
            ):
                continue
            seen.add(video_id)
            videos.append(
                {
                    "id": video_id,
                    "title": str(video.get("title") or "").strip(),
                    "duration": int(video.get("duration") or 0),
                    "channel": str(video.get("channel") or "").strip(),
                    "thumbnail": str(video.get("thumbnail") or "").strip(),
                    "no_thumbnail": bool(video.get("no_thumbnail")),
                    "upload_date": str(video.get("upload_date") or "").strip(),
                    "published": str(video.get("published") or "").strip(),
                    "published_text": str(
                        video.get("published_text") or ""
                    ).strip(),
                    "timestamp": int(video.get("timestamp") or 0),
                    "url": str(video.get("url") or "").strip()
                    or f"https://www.youtube.com/watch?v={video_id}",
                }
            )
            if len(videos) >= limit:
                break
        return videos

    def _parse_youtube_json_lines(
        self, output: str, limit: int
    ) -> list[dict]:
        raw_items = []
        for line in output.splitlines():
            line = line.strip()
            if not line:
                continue
            try:
                video = json.loads(line)
            except json.JSONDecodeError:
                continue
            if not isinstance(video, dict):
                continue
            if video.get("type") == "videos":
                raw_items = video.get("items") or []
            elif video.get("id"):
                # Legacy one-object-per-line output.
                raw_items.append(video)
        return self._normalize_youtube_items(raw_items, limit)

    def _run_youtube_search(
        self,
        keyword: str,
        limit: int,
        on_candidates: Callable[[list[dict], bool], None] | None = None,
        exclude: set[str] | None = None,
        fallback_to_candidates: bool = False,
        status: dict[str, bool] | None = None,
    ) -> list[dict]:
        """Run youtube_news_search.py and return normalized video metadata.

        The search script reads YouTube's own search-results page and extracts
        each video's id, title, watch URL, thumbnail, channel, duration, and
        relative publish date.  ``exclude`` omits already-known video ids so a
        "More" request only returns genuinely new results.

        ``on_candidates`` is retained for the progressive caller interface,
        although the results-page search emits one final result set.  The
        ``fallback_to_candidates`` and ``status`` arguments are kept for cache
        prefetch callers.
        """
        progressive = on_candidates is not None or fallback_to_candidates
        command = self._youtube_search_command(
            keyword, limit, progressive, exclude=exclude
        )

        if not progressive:
            try:
                result = subprocess.run(
                    command,
                    cwd=SCRIPT_DIR,
                    stdout=subprocess.PIPE,
                    stderr=subprocess.PIPE,
                    text=True,
                    timeout=180,
                )
            except subprocess.TimeoutExpired as exc:
                raise RuntimeError("YouTube search timed out.") from exc
            except OSError as exc:
                raise RuntimeError(
                    f"could not run youtube_news_search.py: {exc}"
                ) from exc

            if result.returncode != 0:
                detail = (result.stderr or result.stdout or "").strip()
                raise RuntimeError(
                    "YouTube search failed" + (f": {detail}" if detail else ".")
                )
            return self._parse_youtube_json_lines(result.stdout, limit)

        try:
            process = subprocess.Popen(
                command,
                cwd=SCRIPT_DIR,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                text=True,
            )
        except OSError as exc:
            raise RuntimeError(
                f"could not run youtube_news_search.py: {exc}"
            ) from exc

        stderr_lines: list[str] = []

        def drain_stderr() -> None:
            if process.stderr is None:
                return
            try:
                for line in process.stderr:
                    stderr_lines.append(line)
            except Exception:
                pass

        stderr_thread = threading.Thread(target=drain_stderr, daemon=True)
        stderr_thread.start()
        timed_out = threading.Event()

        def kill_process() -> None:
            timed_out.set()
            try:
                process.kill()
            except OSError:
                pass

        timer = threading.Timer(180, kill_process)
        timer.start()
        final_videos: list[dict] = []
        candidate_videos: list[dict] = []
        try:
            if process.stdout is not None:
                for line in process.stdout:
                    line = line.strip()
                    if not line:
                        continue
                    try:
                        payload = json.loads(line)
                    except json.JSONDecodeError:
                        continue
                    if not isinstance(payload, dict):
                        continue
                    event_type = payload.get("type")
                    if event_type == "candidates":
                        items = self._normalize_youtube_items(
                            payload.get("items") or [], limit
                        )
                        if items:
                            candidate_videos = items
                            if on_candidates is not None:
                                on_candidates(
                                    items,
                                    len(items) > YOUTUBE_SEARCH_PAGE_SIZE,
                                )
                    elif event_type == "videos":
                        final_videos = self._normalize_youtube_items(
                            payload.get("items") or [], limit
                        )
                    elif payload.get("id"):
                        candidate_videos.append(payload)
            returncode = process.wait()
        finally:
            timer.cancel()
            if process.poll() is None:
                try:
                    process.kill()
                except OSError:
                    pass
                process.wait()
            stderr_thread.join(timeout=1)

        if timed_out.is_set():
            raise RuntimeError("YouTube search timed out.")
        if returncode != 0:
            detail = "".join(stderr_lines).strip()
            raise RuntimeError(
                "YouTube search failed" + (f": {detail}" if detail else ".")
            )
        if final_videos:
            if status is not None:
                status["captions_unverified"] = False
            return final_videos
        # The final event was empty: usually every caption lookup timed out or
        # was blocked.  A normal /youtube search keeps the candidate cards it
        # already emitted and labels them as unverified.  "More" and prefetch
        # have no such callback, so they explicitly ask for the candidates.
        if fallback_to_candidates and candidate_videos:
            if status is not None:
                status["captions_unverified"] = True
            return candidate_videos[:limit]
        if status is not None:
            status["captions_unverified"] = False
        return []

    def _cached_youtube_search(
        self,
        keyword: str,
        limit: int,
        on_candidates: Callable[[list[dict], bool], None] | None = None,
    ) -> list[dict]:
        """Return up to `limit` results, growing a per-keyword cache on demand.

        YouTube's recent-first ordering is stable, so asking for a larger
        limit returns the earlier page unchanged followed by new videos. The
        cache therefore lets the "More" button fetch just one larger result
        set and append the unseen tail.
        """
        limit = max(1, min(limit, YOUTUBE_SEARCH_MAX_RESULTS))
        with self.state.lock:
            cached = self.state.youtube_search_cache.get(keyword)
            if cached is not None and len(cached) >= limit:
                return list(cached[:limit])

        videos = self._run_youtube_search(
            keyword, limit, on_candidates=on_candidates
        )
        with self.state.lock:
            existing = self.state.youtube_search_cache.get(keyword)
            if existing is not None and len(existing) >= len(videos):
                videos = existing
            elif videos:
                self.state.youtube_search_cache[keyword] = videos
        return list(videos[:limit])

    def _youtube_cache_items(self, keyword: str) -> list[dict]:
        with self.state.lock:
            return list(self.state.youtube_search_cache.get(keyword) or [])

    def _merge_youtube_cache(
        self, keyword: str, videos: list[dict]
    ) -> list[dict]:
        """Append unseen videos to the keyword cache and return the cache."""
        with self.state.lock:
            existing = list(
                self.state.youtube_search_cache.get(keyword) or []
            )
            seen = {video["id"] for video in existing}
            for video in videos:
                if video["id"] in seen:
                    continue
                seen.add(video["id"])
                existing.append(video)
            if existing:
                self.state.youtube_search_cache[keyword] = existing
            return list(existing)

    def _grow_youtube_cache(self, keyword: str, target: int) -> list[dict]:
        """Fetch only the videos needed to reach ``target`` cached results.

        Known ids are passed as ``--exclude`` so the results-page search only
        returns videos that are not already cached.
        """
        target = max(1, min(target, YOUTUBE_SEARCH_MAX_RESULTS))
        cached = self._youtube_cache_items(keyword)
        if len(cached) >= target:
            return cached
        exclude = {video["id"] for video in cached}
        videos = self._run_youtube_search(
            keyword,
            target - len(cached),
            exclude=exclude,
            fallback_to_candidates=True,
        )
        return self._merge_youtube_cache(keyword, videos)

    def _start_youtube_prefetch(self, keyword: str) -> None:
        """Warm the cache for the next "More" clicks in the background."""
        if not keyword:
            return
        with self.state.lock:
            cached = self.state.youtube_search_cache.get(keyword) or []
            target = min(
                max(
                    YOUTUBE_SEARCH_PREFETCH_RESULTS,
                    len(cached) + YOUTUBE_SEARCH_PAGE_SIZE * 3,
                ),
                YOUTUBE_SEARCH_MAX_RESULTS,
            )
            if len(cached) >= target or keyword in self.state.youtube_prefetch:
                return
            # Grow in a few small steps.  The first step covers the next
            # "More" page quickly; later steps keep filling the cache while
            # the user reads the cards.
            step = YOUTUBE_SEARCH_PAGE_SIZE * 2
            targets = []
            next_target = min(len(cached) + step + 1, target)
            while next_target < target:
                targets.append(next_target)
                next_target = min(next_target + step, target)
            targets.append(target)
            event = threading.Event()
            self.state.youtube_prefetch[keyword] = event

        def prefetch() -> None:
            try:
                for step_target in targets:
                    self._grow_youtube_cache(keyword, step_target)
            except Exception:
                # Prefetch is opportunistic; a failure just means "More" will
                # perform a normal search when the user asks for it.
                pass
            finally:
                with self.state.lock:
                    if self.state.youtube_prefetch.get(keyword) is event:
                        self.state.youtube_prefetch.pop(keyword, None)
                event.set()

        threading.Thread(target=prefetch, daemon=True).start()

    def _handle_youtube_more(self, keyword: str, exclude: set[str]) -> None:
        """Return the next page of a previous /youtube search as JSON."""
        if not keyword:
            self._send_error_json('Usage: /youtube search "<keyword>"', 400)
            return

        page_size = YOUTUBE_SEARCH_PAGE_SIZE
        cached = self._youtube_cache_items(keyword)
        unseen = [video for video in cached if video["id"] not in exclude]

        # A background prefetch may already be filling the cache.  Poll it for
        # a short while so we can return as soon as this page is ready, rather
        # than launching a duplicate YouTube search.
        if len(unseen) < page_size:
            with self.state.lock:
                event = self.state.youtube_prefetch.get(keyword)
            deadline = time.monotonic() + YOUTUBE_PREFETCH_WAIT_SECONDS
            while event is not None and time.monotonic() < deadline:
                event.wait(timeout=0.15)
                cached = self._youtube_cache_items(keyword)
                unseen = [
                    video for video in cached if video["id"] not in exclude
                ]
                if len(unseen) >= page_size or event.is_set():
                    break
            if len(unseen) < page_size:
                with self.state.lock:
                    event = self.state.youtube_prefetch.get(keyword)
                if event is not None:
                    event.wait(timeout=0.5)
                    cached = self._youtube_cache_items(keyword)
                    unseen = [
                        video for video in cached
                        if video["id"] not in exclude
                    ]

        if len(unseen) >= page_size:
            items = unseen[:page_size]
            self._send_json(
                {
                    "keyword": keyword,
                    "items": items,
                    "has_more": len(unseen) > page_size,
                }
            )
            self._start_youtube_prefetch(keyword)
            return

        # The cache is short: fetch only the missing, unseen videos.  Asking
        # for one extra result keeps `has_more` accurate.  The results-page
        # search skips known ids and returns only unseen videos.
        missing = page_size - len(unseen) + 1
        known = set(exclude) | {video["id"] for video in cached}
        status: dict[str, bool] = {}
        try:
            results = self._run_youtube_search(
                keyword,
                missing,
                exclude=known,
                fallback_to_candidates=True,
                status=status,
            )
        except RuntimeError as exc:
            self._send_error_json(f"/youtube search: {exc}", 502)
            return

        cached = self._merge_youtube_cache(keyword, results)
        unseen = [video for video in cached if video["id"] not in exclude]
        items = unseen[:page_size]
        self._send_json(
            {
                "keyword": keyword,
                "items": items,
                "has_more": len(unseen) > page_size,
                "captions_unverified": bool(
                    status.get("captions_unverified")
                ),
            }
        )
        self._start_youtube_prefetch(keyword)

    def _command_learn(self, argument: str) -> None:
        if not argument:
            self._sse_text(
                "output", "\nUsage: /youtube learn <youtube_video_hash|url>\n"
            )
            return
        try:
            import youtube_video_learn as learn
        except ImportError as exc:
            self._sse_text(
                "output", f"\n/youtube learn is unavailable: cannot import "
                f"youtube_video_learn.py ({exc})\n"
            )
            return
        try:
            video_id = learn.extract_video_hash(argument)
        except ValueError as exc:
            self._sse_text("output", f"\n/youtube learn: {exc}\n")
            return

        self._sse_text("output", f"\n/youtube learn: {video_id}\n")
        if not self._learn_video(video_id, label="/youtube learn"):
            return
        self._finish_learn([video_id], label="/youtube learn")

    def _command_learn_topic(self, argument: str) -> None:
        topic = argument.strip()
        if not topic:
            self._sse_text(
                "output", "\nUsage: /youtube learn_topic <topic>\n"
            )
            return

        self._sse_text(
            "output", f"\n/youtube learn_topic: searching YouTube for {topic!r}...\n"
        )
        try:
            result = subprocess.run(
                [
                    sys.executable,
                    "-u",
                    str(SCRIPT_DIR / "search_youtube.py"),
                    "--limit",
                    "3",
                    "--",
                    topic,
                ],
                cwd=SCRIPT_DIR,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                text=True,
                timeout=600,
            )
        except subprocess.TimeoutExpired:
            self._sse_text(
                "output", "\n/youtube learn_topic: YouTube search timed out.\n"
            )
            return
        except OSError as exc:
            self._sse_text(
                "output", f"\n/youtube learn_topic: could not run search_youtube.py: {exc}\n"
            )
            return

        if result.stderr.strip():
            self._sse_text(
                "output",
                result.stderr if result.stderr.endswith("\n") else result.stderr + "\n",
            )
        if result.returncode != 0:
            self._sse_text(
                "output",
                f"\n/youtube learn_topic: search_youtube.py failed with exit code "
                f"{result.returncode}.\n",
            )
            return

        video_ids = []
        for line in result.stdout.splitlines():
            video_id = line.strip()
            if (
                re.fullmatch(r"[A-Za-z0-9_-]{11}", video_id)
                and video_id not in video_ids
            ):
                video_ids.append(video_id)
        video_ids = video_ids[:3]

        if not video_ids:
            self._sse_text(
                "output",
                "\n/youtube learn_topic: no videos with an English transcript were "
                "found for this topic.\n",
            )
            return

        self._sse_text(
            "output",
            f"\n/youtube learn_topic: learning {len(video_ids)} video(s): "
            f"{', '.join(video_ids)}\n",
        )
        learned = []
        for video_id in video_ids:
            self._sse_text("output", f"\n/youtube learn_topic: {video_id}\n")
            if self._learn_video(video_id, label="/youtube learn_topic"):
                learned.append(video_id)
            else:
                self._sse_text(
                    "output",
                    f"\n/youtube learn_topic: could not learn {video_id}; continuing "
                    "with any remaining videos.\n",
                )

        if not learned:
            self._sse_text(
                "output", "\n/youtube learn_topic: no videos could be learned.\n"
            )
            return
        self._finish_learn(learned, label="/youtube learn_topic")

    def _command_learn_playlist(self, argument: str) -> None:
        """Learn every video in a YouTube playlist."""
        playlist = argument.strip()
        if not playlist:
            self._sse_text(
                "output",
                "\nUsage: /youtube learn_playlist <youtube_playlist_id|url>\n",
            )
            return

        self._sse_text(
            "output",
            f"\n/youtube learn_playlist: fetching videos from {playlist!r}...\n",
        )
        try:
            result = subprocess.run(
                [
                    sys.executable,
                    "-u",
                    str(SCRIPT_DIR / "get_videos_in_playlist.py"),
                    playlist,
                ],
                cwd=SCRIPT_DIR,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                text=True,
                timeout=600,
            )
        except subprocess.TimeoutExpired:
            self._sse_text(
                "output",
                "\n/youtube learn_playlist: playlist fetch timed out.\n",
            )
            return
        except OSError as exc:
            self._sse_text(
                "output",
                "\n/youtube learn_playlist: could not run "
                f"get_videos_in_playlist.py: {exc}\n",
            )
            return

        if result.stderr.strip():
            self._sse_text(
                "output",
                result.stderr
                if result.stderr.endswith("\n")
                else result.stderr + "\n",
            )
        if result.returncode != 0:
            self._sse_text(
                "output",
                "\n/youtube learn_playlist: get_videos_in_playlist.py failed with "
                f"exit code {result.returncode}.\n",
            )
            return

        video_ids: list[str] = []
        for line in result.stdout.splitlines():
            video_id = line.strip()
            if (
                re.fullmatch(r"[A-Za-z0-9_-]{11}", video_id)
                and video_id not in video_ids
            ):
                video_ids.append(video_id)

        if not video_ids:
            self._sse_text(
                "output",
                "\n/youtube learn_playlist: the playlist contains no videos.\n",
            )
            return

        total = len(video_ids)
        self._sse_text(
            "output",
            f"\n/youtube learn_playlist: learning {total} video(s) from the "
            "playlist.\n",
        )
        learned = []
        for index, video_id in enumerate(video_ids, start=1):
            self._sse_text(
                "output",
                f"\n/youtube learn_playlist: [{index}/{total}] {video_id}\n",
            )
            if self._learn_video(video_id, label="/youtube learn_playlist"):
                learned.append(video_id)
            else:
                self._sse_text(
                    "output",
                    f"\n/youtube learn_playlist: could not learn {video_id}; "
                    "continuing with any remaining videos.\n",
                )

        if not learned:
            self._sse_text(
                "output",
                "\n/youtube learn_playlist: no videos could be learned.\n",
            )
            return
        self._finish_learn(learned, label="/youtube learn_playlist")

    def _learn_video(self, video_id: str, label: str = "/youtube learn") -> bool:
        """Run the youtube_video_learn.py pipeline for one video."""
        try:
            import youtube_video_learn as learn
        except ImportError as exc:
            self._sse_text(
                "output",
                f"\n{label} is unavailable: cannot import "
                f"youtube_video_learn.py ({exc})\n",
            )
            return False

        for script, script_args in learn.STEPS:
            argv = [arg.replace("<hash>", video_id) for arg in script_args]
            if not self._run_learn_step(script, argv, label=label):
                return False
        return True

    def _run_learn_step(
        self, script: str, argv: list[str], label: str = "/youtube learn"
    ) -> bool:
        """Run one learn pipeline step, streaming its output."""
        self._sse_text(
            "output", f"\n=== {script} {' '.join(argv)} ===\n"
        )
        process = None
        try:
            process = subprocess.Popen(
                [
                    sys.executable,
                    "-u",
                    str(SCRIPT_DIR / script),
                    *argv,
                ],
                cwd=SCRIPT_DIR,
                stdout=subprocess.PIPE,
                stderr=subprocess.STDOUT,
                text=True,
                bufsize=1,
            )
            assert process.stdout is not None
            try:
                for line in process.stdout:
                    self._sse_text("output", line)
                returncode = process.wait()
            except ClientDisconnected:
                # The browser cancelled the stream; do not leave the
                # learn pipeline running in the background.
                process.terminate()
                try:
                    process.wait(timeout=5)
                except subprocess.TimeoutExpired:
                    process.kill()
                    process.wait()
                raise
        except OSError as exc:
            self._sse_text(
                "output", f"\n{label}: could not run {script}: {exc}\n"
            )
            return False
        if returncode != 0:
            self._sse_text(
                "output",
                f"\n{label} aborted: {script} failed with exit code "
                f"{returncode}\n",
            )
            return False
        return True

    def _finish_learn(self, video_ids: list[str], label: str = "/youtube learn") -> None:
        """Invalidate caches and refresh the global corpus."""
        state = self.state
        chat.clear_bm25_cache()
        state.cache.clear()
        for video_id in video_ids:
            self._sse_text(
                "output",
                f"\n{label}: {video_id} added to the knowledge base.\n",
            )

        new_chunks = printed = error = None
        reloaded = False

        # Keep the global corpus current even when an article or transcript
        # scope is active, so a later /refresh sees the new video.
        global_chunks, _global_printed, global_error = load_all_chunks_captured()
        if not global_error and global_chunks is not None:
            state.global_chunks = global_chunks
            # Index the newly learned trunks and compact the FTS5 segments
            # here, during the learn command rather than on the next question.
            try:
                chat.index_chunks_in_sqlite(
                    global_chunks, corpus_key="global"
                )
            except Exception:
                pass

        if state.article_path is not None:
            self._sse_text(
                "output",
                f"\nStill answering from article {state.article_path!r}; "
                "the loaded article context is unchanged.\n",
            )
        elif state.transcript is not None:
            if state.transcript in video_ids:
                new_chunks, printed, error = load_chunks_captured(
                    video_ids={state.transcript}
                )
                reloaded = True
            else:
                self._sse_text(
                    "output",
                    f"\nStill answering only from transcript "
                    f"{state.transcript!r}; the loaded trunks are unchanged.\n",
                )
        else:
            # A newly learned video belongs to the global corpus immediately,
            # without a membership check or classifier pass.
            new_chunks, printed, error = global_chunks, _global_printed, global_error
            reloaded = True

        if reloaded:
            if error or new_chunks is None:
                self._sse_text(
                    "output",
                    f"\n{label}: could not reload the active scope: {error}\n",
                )
            else:
                state.global_chunks = new_chunks
                state.replace_chunks(new_chunks)
                self._sse_text(
                    "output",
                    "\n" + (printed + "\n" if printed else "")
                    + chat.index_memory_report(
                        new_chunks, build_index=False
                    )
                    + "\n",
                )
        self._sse("meta", state.snapshot())

    def _command_news(self, argument: str) -> None:
        """Stream the latest news stories to the browser.

        Usage: /news [SOURCE ...] [number-of-stories]
               /news search <keywords> [website]
        Each SOURCE is one of wsj, cnn, cbslite, nprtext, yahoo_finance, medium,
        kqed, sfgate, sfbayview, mission_local, richmond_sunset, techcrunch,
        krasia, wccftech, or hackernews.
        Several sources may be selected.
        Without a source, every supported source is shown as its own section.
        "search <keywords> [website]" is the same as /googlenews <keywords>,
        except a trailing website restricts the search to that site, for
        example: /news search black holes howstuffworks.com. It renders the
        matches exactly like a /news story list.
        """
        stripped = argument.strip()
        search_match = re.match(r"(?i)^search(?:\s+(.*))?$", stripped)
        if search_match:
            keywords = (search_match.group(1) or "").strip()
            if not keywords:
                self._sse_text(
                    "output",
                    "\nUsage: /news search <keywords> [website]\n"
                    "Example: /news search jensen huang\n"
                    "Example: /news search black holes howstuffworks.com\n",
                )
                return
            keywords, website = split_news_search_website(keywords)
            query = f"site:{website} {keywords}" if website else keywords
            self._stream_news_events(
                "news_fetch.php",
                "/news",
                ["--source", "googlenews", "--search", query],
            )
            return

        argv: list[str] = []
        sources: list[str] = []
        for part in argument.replace(",", " ").split():
            lowered = part.lower()
            if part.isdigit():
                if int(part) < 1:
                    self._sse_text(
                        "output",
                        f"\nUsage: /news [{NEWS_SOURCE_USAGE}] ... "
                        "[number-of-stories]\n",
                    )
                    return
                argv.extend(["--limit", part])
            elif lowered in NEWS_SOURCES:
                if lowered not in sources:
                    sources.append(lowered)
            else:
                self._sse_text(
                    "output",
                    f"\nUsage: /news [{NEWS_SOURCE_USAGE}] ... "
                    "[number-of-stories]\n",
                )
                return
        for source in sources:
            argv.extend(["--source", source])
        self._stream_news_events("news_fetch.php", "/news", argv)

    def _command_mdn(self, argument: str) -> None:
        """Search MDN Web Docs and stream the matches as a /news-style list.

        Usage: /mdn <keywords>
        The public MDN search API supplies the titles, summaries, and matching
        snippets. The browser reuses the /news story-list and detail modal, so
        a result can be translated, read aloud, or opened on MDN.
        """
        keyword = " ".join(argument.split())
        if not keyword:
            self._sse_text(
                "output",
                "\nUsage: /mdn <keywords>\n"
                "Example: /mdn fetch api\n"
                "Example: /mdn javascript promises\n",
            )
            return

        self._sse_text(
            "log", f"[mdn: searching developer.mozilla.org for {keyword!r}]"
        )
        try:
            results = search_mdn_docs(keyword)
        except RuntimeError as exc:
            self._sse_text("error", f"\n/mdn: {exc}\n")
            return

        self._sse(
            "news",
            {
                "type": "start",
                "source": "MDN Web Docs",
                "feed": MDN_SEARCH_URL,
                "sources": ["MDN Web Docs"],
                "docs": True,
            },
        )
        for index, result in enumerate(results, 1):
            self._sse(
                "news",
                {
                    "type": "item",
                    "index": index,
                    "title": result["title"],
                    "link": result["link"],
                    "source": "MDN Web Docs",
                    "summary": result["summary"],
                    "docs": True,
                },
            )
            paragraphs = [
                text
                for text in [result["summary"], *result["paragraphs"]]
                if text
            ]
            seen: set[str] = set()
            for text in paragraphs:
                if text in seen:
                    continue
                seen.add(text)
                self._sse(
                    "news",
                    {"type": "paragraph", "index": index, "text": text},
                )
            self._sse("news", {"type": "item_end", "index": index})
        self._sse("news", {"type": "done"})
        if not results:
            self._sse_text(
                "output", f"\n/mdn: no documentation matched {keyword!r}.\n"
            )

    def _command_wikivoyage(self, argument: str) -> None:
        """Search Wikivoyage destinations and stream a /news-style list.

        Usage: /wikivoyage <keywords>
        The Wikivoyage search API supplies the destination cards, and each
        destination summary adds its overview extract and coordinates. The
        browser reuses the /news result list and detail modal, so a card can
        be read, translated, learned, chatted with, and opened on Wikivoyage.
        """
        keyword = " ".join(argument.split())
        if not keyword:
            self._sse_text(
                "output",
                "\nUsage: /wikivoyage <keywords>\n"
                "Example: /wikivoyage Kyoto\n"
                "Example: /wikivoyage hiking in Japan\n",
            )
            return

        self._sse_text(
            "log", f"[wikivoyage: searching en.wikivoyage.org for {keyword!r}]"
        )
        try:
            results = search_wikivoyage_destinations(keyword)
        except RuntimeError as exc:
            self._sse_text("error", f"\n/wikivoyage: {exc}\n")
            return

        self._sse(
            "news",
            {
                "type": "start",
                "source": "Wikivoyage",
                "feed": WIKIVOYAGE_SEARCH_URL,
                "sources": ["Wikivoyage"],
                "docs": True,
                "docs_label": "Wikivoyage",
            },
        )
        for index, result in enumerate(results, 1):
            # A destination article can be learned exactly like a news story,
            # so attach the same stable id and learned status that /news items
            # carry. The browser then adds the Learn/Chat rail to the modal.
            news_id = news_id_for_item(result)
            self._sse(
                "news",
                {
                    "type": "item",
                    "index": index,
                    "title": result["title"],
                    "link": result["link"],
                    "source": "Wikivoyage",
                    "summary": result["summary"],
                    "images": result["images"],
                    "docs": True,
                    "docs_label": "Wikivoyage",
                    "learnable": True,
                    "news_id": news_id,
                    "learned": news_is_learned(news_id),
                    "article_api": "/api/wikivoyage_article",
                },
            )
            seen: set[str] = set()
            for text in result["paragraphs"]:
                if text in seen:
                    continue
                seen.add(text)
                self._sse(
                    "news", {"type": "paragraph", "index": index, "text": text}
                )
            self._sse("news", {"type": "item_end", "index": index})
        self._sse("news", {"type": "done"})
        if not results:
            self._sse_text(
                "output", f"\n/wikivoyage: no destinations matched {keyword!r}.\n"
            )

    def _command_changelog(self, argument: str) -> None:
        """List the completed-task summary pages, newest first.

        Usage: /changelog
        Every entry is one .logs/*.summary.html page. The browser renders the
        list and asks /api/changelog_file for the selected page when its row is
        clicked, so the modal receives the original HTML document.
        """
        entries = list_changelog_entries()
        self._sse(
            "changelog",
            {
                "type": "start",
                "directory": str(changelog_directory() or LOGS_DIR),
                "count": len(entries),
            },
        )
        for index, entry in enumerate(entries, 1):
            self._sse(
                "changelog",
                {"type": "item", "index": index, **entry},
            )
        self._sse("changelog", {"type": "done"})
        if not entries:
            self._sse_text(
                "output",
                "\nNo completed-task summaries were found in .logs.\n",
            )

    def _command_googlenews(self, argument: str) -> None:
        """Search Google News RSS for the latest stories matching keywords.

        Usage: /googlenews <keywords>
        news_fetch.php creates a dynamic googlenews source for the query, so
        the browser reuses the /news story-list and detail rendering.
        """
        keywords = argument.strip()
        if not keywords:
            self._sse_text(
                "output",
                "\nUsage: /googlenews <keywords>\n"
                "Example: /googlenews open source llm\n",
            )
            return
        self._stream_news_events(
            "news_fetch.php",
            "/googlenews",
            ["--source", "googlenews", "--search", keywords],
        )

    def _command_howstuffworks(self, argument: str) -> None:
        """Search HowStuffWorks articles by keywords.

        Usage: /howstuffworks <keywords>
        The command reuses the /news search pipeline: news_fetch.php searches
        Google News RSS, with the query restricted to
        site:howstuffworks.com. Its result list and detail modal therefore
        support Learn, Chat, and read-aloud exactly like a /news story. Full
        article bodies are fetched on demand when a story is opened.
        """
        keywords = argument.strip()
        if not keywords:
            self._sse_text(
                "output",
                "\nUsage: /howstuffworks <keywords>\n"
                "Example: /howstuffworks how do black holes work\n",
            )
            return
        self._stream_news_events(
            "news_fetch.php",
            "/howstuffworks",
            [
                "--source",
                "googlenews",
                "--search",
                f"site:howstuffworks.com {keywords}",
            ],
        )

    def _command_medium(self, argument: str) -> None:
        """Stream the latest stories from the configured Medium author.

        Usage: /medium [number-of-stories]
        The author handle is read from ~/.medium/username; news_fetch.php
        fetches https://medium.com/feed/@<handle> through its medium_author
        source and the browser reuses the /news story-list and detail rendering.
        """
        username = medium_username()
        if not username:
            self._sse_text(
                "output",
                "\n/medium: write your Medium handle (for example "
                "@yourname) to ~/.medium/username first.\n",
            )
            return

        argv: list[str] = ["--source", "medium_author"]
        for part in argument.replace(",", " ").split():
            if part.isdigit():
                if int(part) < 1:
                    self._sse_text(
                        "output", "\nUsage: /medium [number-of-stories]\n"
                    )
                    return
                argv.extend(["--limit", part])
            else:
                self._sse_text(
                    "output", "\nUsage: /medium [number-of-stories]\n"
                )
                return
        self._stream_news_events("news_fetch.php", "/medium", argv)

    def _command_ai_news_podcast(self, argument: str) -> None:
        """List the five newest stories and offer a two-voice podcast.

        Usage: /ai_news_podcast
        The list is the same Google News RSS query as the prompt's
        latest_news_url (the last twelve hours). The browser adds a Podcast
        button under the five story links; using it drafts a host/guest script
        from the full article bodies and reads it with two male voices.
        """
        if argument.strip():
            self._sse_text(
                "output",
                "\nUsage: /ai_news_podcast\n"
                "Shows the top five stories from the last 12 hours and adds a "
                "podcast button underneath them.\n",
            )
            return
        self._stream_news_events(
            "news_fetch.php",
            "/ai_news_podcast",
            [
                "--source",
                "googlenews",
                "--search",
                AI_NEWS_PODCAST_QUERY,
                "--limit",
                str(AI_NEWS_PODCAST_LIMIT),
            ],
            podcast=True,
        )

    def _command_gutenberg(self, argument: str) -> None:
        """Search Project Gutenberg; the browser renders clickable book cards.

        Usage: /gutenberg search <keywords>
        Clicking a card opens a two-column modal: the book's plaintext on the
        left and an isolated RAG chat about that book on the right.
        """
        subcommand, _, rest = argument.strip().partition(" ")
        keyword = rest.strip()
        if subcommand.lower() != "search" or not keyword:
            self._sse_text(
                "output",
                "\nUsage: /gutenberg search <keywords>\n"
                "Search Project Gutenberg by title, author, or keyword, then "
                "click a book to read it and ask questions.\n",
            )
            return
        heading = f"Project Gutenberg · {keyword}"
        self._sse_text(
            "log",
            f"[gutenberg: searching Google News for "
            f"{GUTENBERG_NEWS_SITE_QUERY} {keyword!r}]",
        )

        def publish(cards: list[dict]) -> None:
            self._sse(
                "books",
                {
                    "keyword": keyword,
                    "heading": heading,
                    "items": cards,
                },
            )

        items: list[dict] = []
        try:
            items = search_gutenberg_books_google_news(keyword, on_item=publish)
        except RuntimeError as exc:
            self._sse_text(
                "log", f"[gutenberg: Google News search failed: {exc}]"
            )

        # Google News searches full text and can fill a page with loosely
        # related books; the catalogue search is literal, so augment every
        # search instead of using it only when the news feed is empty. Query
        # variants (for example ``39`` -> ``thirty nine``) let the catalogue
        # find an exact title such as The Thirty-Nine Steps.
        try:
            items = _combine_gutenberg_cards(
                keyword,
                items,
                search_gutenberg_books(keyword),
                limit=GUTENBERG_BOOK_RESULT_LIMIT,
            )
        except RuntimeError as exc:
            self._sse_text(
                "log", f"[gutenberg: catalogue search failed: {exc}]"
            )

        # Google News may have published partial results while it streamed;
        # this final event replaces them with the merged, relevance-ranked
        # list so an exact catalogue hit is not hidden behind loose news hits.
        publish(items)

    def _command_openlibrary(self, argument: str) -> None:
        """Search inside Open Library books and stream clickable book cards.

        Usage: /openlibrary search <keywords>
        Open Library's inside-search endpoint matches the query against the
        full text of digitized books. Each card shows the title, authors,
        catalogue description, and the passages where the query matched. The
        card links to the Open Library record; unlike /gutenberg these books
        are not downloaded into the local reader.
        """
        subcommand, _, rest = argument.strip().partition(" ")
        keyword = rest.strip()
        if subcommand.lower() != "search" or not keyword:
            self._sse_text(
                "output",
                "\nUsage: /openlibrary search <keywords>\n"
                "Example: /openlibrary search rust borrow checker\n",
            )
            return

        heading = f"Open Library · {keyword}"
        self._sse_text(
            "log", f"[openlibrary: searching inside books for {keyword!r}]"
        )
        try:
            items = search_openlibrary_inside(keyword)
        except RuntimeError as exc:
            self._sse_text("error", f"\n{exc}\n")
            return

        self._sse(
            "books",
            {
                "keyword": keyword,
                "heading": heading,
                "items": items,
            },
        )
        if not items:
            self._sse_text(
                "output",
                f"\n/openlibrary: no books matched {keyword!r}.\n",
            )

    def _stream_news_events(
        self,
        script_name: str,
        command: str,
        extra_argv: list[str],
        podcast: bool = False,
    ) -> int:
        """Stream a news fetcher's NDJSON events as `news` SSE events.

        news_fetch.php emits one JSON event per line; relaying each one lets
        the title list appear story by story while the feeds are still being
        downloaded. List requests run with --lazy so publisher article bodies
        are not downloaded up front; the detail modal fetches the selected
        story on demand through /api/news_article.
        """
        script = SCRIPT_DIR / script_name
        if not script.exists():
            self._sse_text(
                "output", f"\n{command}: missing {script.name}.\n"
            )
            return 0

        argv = ["php", str(script), "--json", "--lazy", *extra_argv]
        log_prefix = command.lstrip("/")
        process = None
        item_count = 0
        try:
            process = subprocess.Popen(
                argv,
                cwd=SCRIPT_DIR,
                stdout=subprocess.PIPE,
                stderr=subprocess.STDOUT,
                text=True,
                bufsize=1,
            )
            assert process.stdout is not None
            try:
                for raw_line in process.stdout:
                    line = raw_line.strip()
                    if not line:
                        continue
                    try:
                        payload = json.loads(line)
                    except json.JSONDecodeError:
                        self._sse_text("log", f"[{log_prefix}: {line}]")
                        continue
                    if not isinstance(payload, dict):
                        continue
                    if payload.get("type") == "item":
                        item_count += 1
                        # Attach the stable learned-story id and status so the
                        # browser can offer Learn or Chat without a second
                        # request for every story in the feed.
                        try:
                            news_id = news_id_for_item(payload)
                        except ValueError:
                            news_id = ""
                        payload["news_id"] = news_id
                        payload["learned"] = bool(
                            news_id and news_is_learned(news_id)
                        )
                    if podcast:
                        # Mark every event so the browser can add the podcast
                        # action underneath this particular story list.
                        payload["ai_news_podcast"] = True
                    self._sse("news", payload)
                returncode = process.wait()
            except ClientDisconnected:
                process.terminate()
                try:
                    process.wait(timeout=5)
                except subprocess.TimeoutExpired:
                    process.kill()
                    process.wait()
                raise
        except OSError as exc:
            self._sse_text(
                "output", f"\n{command}: could not run {script.name}: {exc}\n"
            )
            return 0

        if returncode != 0:
            self._sse_text(
                "output",
                f"\n{command}: {script.name} exited with code {returncode}.\n",
            )
        elif item_count == 0:
            self._sse_text(
                "output", f"\n{command}: no stories were found in the feed.\n"
            )
        return item_count

    def _command_ask(self, argument: str) -> None:
        """Answer a quick, self-contained question without retrieval."""
        question = argument.strip()
        if not question:
            self._sse_text("output", "\nUsage: /ask <question>\n")
            return
        stats: dict = {}
        started = time.monotonic()
        try:
            response = chat.answer_simple_question(
                question,
                metrics=stats,
                emit=lambda text: self._sse_text("delta", text),
            )
        except RuntimeError as exc:
            self._sse_text("output", f"\nQuick answer failed: {exc}\n")
            return
        if response:
            self.state.latest_answer = response
            self.state.latest_sources = []
            stats.update(
                {
                    "cached": False,
                    "model": chat.SIMPLE_ANSWER_MODEL,
                    "retrieval": 0.0,
                    "expansion": 0.0,
                    "bm25": 0.0,
                    "filter": 0.0,
                    "candidates": 0,
                    "trunks": 0,
                    "context_build": 0.0,
                    "prompt_build": 0.0,
                    "context_tokens": 0,
                    "prefill": stats.get("prompt_eval_duration", 0.0),
                    "load": stats.get("load_duration", 0.0),
                    "end_to_end_ttft": stats.get("request_ttft", 0.0),
                    "prompt_tokens": stats.get("prompt_eval_count", 0),
                    "prompt_tokens_cached": stats.get(
                        "prompt_eval_cached_count", 0
                    ),
                    "response_tokens": stats.get("eval_count", 0),
                    "total": time.monotonic() - started,
                }
            )
            self.state.latest_benchmark = stats

    def _command_wiki(self, argument: str) -> None:
        question = argument.strip()
        if not question:
            self._sse_text("output", "\nUsage: /wiki <question>\n")
            return
        response = chat.answer_wiki_question(
            question,
            emit=lambda text: self._sse("delta", {"text": text}),
            log=lambda text: self._sse_text("log", text),
        )
        if response:
            self.state.latest_answer = response
            self.state.latest_sources = []

    def _command_wiki_article(self, argument: str) -> None:
        question = argument.strip()
        if not question:
            self._sse_text("output", "\nUsage: /wiki_article <question>\n")
            return
        response = chat.print_wiki_full_articles(
            question,
            emit=lambda text: self._sse("delta", {"text": text}),
            log=lambda text: self._sse_text("log", text),
        )
        if response:
            self.state.latest_answer = response
            self.state.latest_sources = []

    def _command_article(self, argument: str) -> None:
        """Use one local article file as the complete context for the chat."""
        state = self.state
        raw_path = argument.strip()
        simple = bool(
            re.search(r"(?:^|\s)--(?:simple|fast)\b", raw_path)
        )
        iterative = not simple or bool(
            re.search(r"(?:^|\s)--(?:iterative|deep)\b", raw_path)
        )
        raw_path = re.sub(
            r"(?:^|\s)--(?:iterative|deep|simple|fast)\b", " ", raw_path
        ).strip()
        if not raw_path:
            self._sse_text(
                "output",
                "\nUsage: /article [--simple] <path_to_an_article>\n"
                "The file becomes the only context; follow-up questions keep "
                "the article scope until /youtube transcript is used.\n"
                "The capped multi-round evidence loop is enabled by default "
                "(--simple or --fast disables it).\n",
            )
            return
        if (
            len(raw_path) >= 2
            and raw_path[0] == raw_path[-1]
            and raw_path[0] in {"'", '"'}
        ):
            raw_path = raw_path[1:-1]
        path = Path(raw_path).expanduser()
        if not path.is_absolute():
            path = SCRIPT_DIR / path
        path = path.resolve()
        if not path.is_file():
            self._sse_text(
                "output", f"\nArticle not found: {path}\n"
            )
            return
        try:
            new_chunks, title, structure = _load_article_chunks(path)
        except (OSError, ValueError) as exc:
            self._sse_text(
                "output",
                f"\nCannot use article {path}: {exc}\n",
            )
            return
        # /article replaces the transcript scope for subsequent questions.
        state.transcript = None
        state.replace_chunks(
            new_chunks,
            article_path=str(path),
            article_title=title,
            article_structure=structure,
            article_iterative=iterative,
        )
        state.cache.clear()
        self._sse_text(
            "output",
            "\n"
            + chat.index_memory_report(
                new_chunks, build_index=False, article=str(path)
            )
            + f"\nNow answering from article {str(path)!r} "
            f"({sum(1 for chunk in new_chunks if chunk.index > 0)} "
            "paragraph(s), "
            f"{len(structure.get('sections', []))} section(s)). Existing "
            "conversation context was cleared; follow-up questions stay on "
            "this article."
            + (
                " Iterative evidence mode is enabled.\n"
                if state.article_iterative
                else " Fast single-pass retrieval mode is enabled.\n"
            ),
        )
        self._sse("meta", state.snapshot())

    def _command_mathtutor(self, argument: str) -> None:
        """Open the Word-test-paper tutor, resuming the latest session."""
        state = self.state
        session = None
        if state.mathtutor_session_id:
            try:
                session = _mathtutor_load_session(state.mathtutor_session_id)
            except ValueError:
                session = None
        if session is None:
            session = _mathtutor_latest_session()
        if session is not None:
            state.mathtutor_session_id = str(session.get("id", ""))
        self._sse(
            "mathtutor",
            {
                "session": (
                    _mathtutor_public_session(session) if session else None
                )
            },
        )
        if session is None:
            self._sse_text(
                "output",
                "\nMath Tutor: upload one or more Microsoft Word test papers "
                "(.docx or .doc) below to generate similar practice problems.\n",
            )
        else:
            self._sse_text(
                "output",
                f"\nMath Tutor: resumed session {session.get('id')} with "
                f"{len(session.get('problems', []))} practice problem(s).\n",
            )

    def _command_dict(self, argument: str) -> None:
        word = argument.strip()
        if not word:
            self._sse_text("output", "\nUsage: /dict <word>\n")
            return
        self._stream_dictionary(word, self.state.latest_answer)

    def _command_sources(self) -> None:
        sources = self.state.latest_sources
        if not sources:
            self._sse_text(
                "output", "\nNo sources available yet. Ask a question first.\n"
            )
            return
        lines = ["", "Sources for the latest answer:"]
        for index, (chunk, score) in enumerate(sources, start=1):
            label = chunk.label
            if self.state.article_path is not None:
                section = _article_source_section(
                    self.state.article_structure, chunk.index
                )
                if section is not None:
                    heading = str(section.get("heading", ""))
                    if heading:
                        label += f" ({heading})"
            lines.append(f"  [Chunk {index}] {label} (bm25 {score:.4f})")
        self._sse_text("output", "\n".join(lines) + "\n")

    def _command_benchmark(self, argument: str) -> None:
        state = self.state
        value = argument.strip().lower()
        if value in {"", "last", "show"}:
            if state.latest_benchmark is None:
                self._sse_text(
                    "output",
                    "\nNo benchmark data yet. Ask a question first, or use "
                    "/benchmark on to show metrics after each answer.\n"
                    + chat.memory_usage_line()
                    + "\n",
                )
            else:
                self._sse_text(
                    "output",
                    "\n" + chat.format_benchmark_line(state.latest_benchmark) + "\n",
                )
            return
        if value in {"on", "enable", "enabled"}:
            state.benchmark_mode = True
            message = (
                "\nBenchmark display: on (metrics shown after each answer).\n"
            )
            if state.latest_benchmark is not None:
                message += chat.format_benchmark_line(state.latest_benchmark) + "\n"
            else:
                message += chat.memory_usage_line() + "\n"
            self._sse_text("output", message)
            self._sse("meta", state.snapshot())
            return
        if value in {"off", "disable", "disabled"}:
            state.benchmark_mode = False
            self._sse_text("output", "\nBenchmark display: off.\n")
            self._sse("meta", state.snapshot())
            return
        self._sse_text(
            "output",
            "\nUsage: /benchmark [on|off] (no argument shows the latest "
            "answer's metrics)\n",
        )

    def _command_depth(self, argument: str) -> None:
        state = self.state
        value = argument.strip().lower()
        if not value:
            current = "auto" if state.depth is None else str(state.depth)
            self._sse_text(
                "output",
                f"\nRetrieval depth: {current}\n"
                "Usage: /depth <number of trunks> | /depth auto\n"
                f"Default: {chat.DEFAULT_DEPTH} top BM25 matching trunks; "
                "/depth auto enables adaptive retrieval.\n",
            )
            return
        if value in {"auto", "adaptive", "0"}:
            state.depth = None
            state.cache.clear()
            self._sse_text(
                "output",
                "\nRetrieval depth: auto (BM25 score elbow, token budget).\n",
            )
            self._sse("meta", state.snapshot())
            return
        if value in {"default", "reset"}:
            state.depth = chat.DEFAULT_DEPTH
            state.cache.clear()
            self._sse_text(
                "output",
                f"\nRetrieval depth: top {state.depth} BM25 matching trunk(s) "
                "(default).\n",
            )
            self._sse("meta", state.snapshot())
            return
        try:
            new_depth = int(value)
        except ValueError:
            new_depth = 0
        if new_depth < 1:
            self._sse_text(
                "output",
                f"\nInvalid depth: {argument!r}. Use a positive integer, "
                "'auto', or 'default'.\n",
            )
            return
        state.depth = new_depth
        state.cache.clear()
        self._sse_text(
            "output",
            f"\nRetrieval depth: top {new_depth} BM25 matching trunk(s).\n",
        )
        self._sse("meta", state.snapshot())

    # ---- SSE: answer translation -------------------------------------
    def _handle_translate(self, text: str) -> None:
        """Translate a finished answer into Simplified Chinese paragraph by paragraph."""
        self._begin_sse()
        try:
            if len(text) > MAX_TRANSLATION_CHARS:
                raise RuntimeError(
                    f"Answer is too long to translate (limit "
                    f"{MAX_TRANSLATION_CHARS:,} characters)."
                )
            paragraphs = split_translation_paragraphs(text)
            if not paragraphs:
                raise RuntimeError("There is no answer text to translate.")

            self._sse(
                "start",
                {
                    "model": ANSWER_TRANSLATION_MODEL,
                    "paragraphs": len(paragraphs),
                },
            )
            for index, paragraph in enumerate(paragraphs):
                if index:
                    self._sse_text("delta", "\n\n")
                self._stream_translation_paragraph(paragraph)
                self._sse(
                    "paragraph",
                    {"index": index + 1, "total": len(paragraphs)},
                )
            self._sse("done", {"paragraphs": len(paragraphs)})
        except ClientDisconnected:
            pass
        except Exception as exc:
            try:
                self._sse("error", {"message": f"Translation failed: {exc}"})
                self._sse("done", {})
            except ClientDisconnected:
                pass

    # ---- SSE: AI news podcast ---------------------------------------
    def _handle_ai_news_podcast(self, payload: dict) -> None:
        """Draft a 2000-word host/guest podcast from current news stories.

        The browser sends the five stories from /ai_news_podcast. Missing
        article bodies are fetched on demand here so the script is grounded in
        the full news content, not just the feed headlines. The script streams
        back as ``delta`` events; ``start`` carries the two male voices the
        browser should use with /api/tts.
        """
        self._begin_sse()
        try:
            stories = self._ai_news_podcast_stories(payload)
            if not stories:
                raise RuntimeError(
                    "No article content was available for the podcast."
                )
            source = self._ai_news_podcast_source(stories)
            voices = _podcast_voice_defaults()
            self._sse(
                "start",
                {
                    "model": AI_NEWS_PODCAST_MODEL,
                    "voices": voices,
                    "stories": len(stories),
                    "target_words": 2000,
                },
            )
            prompt = AI_NEWS_PODCAST_PROMPT.format(source=source)
            request = {
                "model": AI_NEWS_PODCAST_MODEL,
                "prompt": prompt,
                "stream": True,
                "think": False,
                "keep_alive": chat.OLLAMA_KEEP_ALIVE,
                "options": {
                    "temperature": 0.55,
                    "num_ctx": AI_NEWS_PODCAST_NUM_CTX,
                    "num_predict": AI_NEWS_PODCAST_NUM_PREDICT,
                },
            }
            text_parts: list[str] = []
            try:
                with requests.post(
                    f"{chat.OLLAMA_HOST}/api/generate",
                    json=request,
                    stream=True,
                    timeout=(10, 1800),
                ) as response:
                    response.raise_for_status()
                    for line in response.iter_lines(decode_unicode=True):
                        if not line:
                            continue
                        try:
                            event = json.loads(line)
                        except json.JSONDecodeError:
                            continue
                        if event.get("error"):
                            raise RuntimeError(
                                f"Ollama error: {event['error']}"
                            )
                        token = event.get("response", "")
                        if token:
                            text_parts.append(token)
                            self._sse_text("delta", token)
                        if event.get("done"):
                            break
            except requests.RequestException as exc:
                raise RuntimeError(
                    f"Could not reach Ollama at {chat.OLLAMA_HOST}. "
                    f"Is it running? ({exc})"
                ) from exc
            script = "".join(text_parts).strip()
            if not script:
                raise RuntimeError(
                    "The podcast model returned an empty script."
                )
            self._sse(
                "done",
                {
                    "stories": len(stories),
                    "words": len(script.split()),
                },
            )
        except ClientDisconnected:
            pass
        except Exception as exc:
            try:
                self._sse("error", {"message": f"Podcast failed: {exc}"})
                self._sse("done", {})
            except ClientDisconnected:
                pass

    def _ai_news_podcast_stories(self, payload: dict) -> list[dict]:
        """Normalize the browser's story list and fill in missing bodies."""
        raw_stories = payload.get("stories")
        if not isinstance(raw_stories, list) or not raw_stories:
            raw_stories = [payload]
        stories: list[dict] = []
        for raw in raw_stories[:AI_NEWS_PODCAST_LIMIT]:
            if not isinstance(raw, dict):
                continue
            title = str(raw.get("title") or "").strip()
            link = str(raw.get("link") or "").strip()
            summary = str(raw.get("summary") or "").strip()
            paragraphs = [
                str(value).strip()
                for value in (raw.get("paragraphs") or [])
                if str(value).strip()
            ]
            if not paragraphs and re.match(
                r"^https?://", link, re.IGNORECASE
            ):
                try:
                    article = fetch_news_article(link, title)
                except (RuntimeError, ValueError, OSError):
                    article = {}
                paragraphs = list(article.get("paragraphs") or [])
                link = str(article.get("link") or link).strip()
            if not paragraphs and summary:
                paragraphs = [summary]
            if not title and not paragraphs:
                continue
            stories.append(
                {
                    "title": title or "Untitled story",
                    "source": str(raw.get("source") or "").strip(),
                    "link": link,
                    "summary": summary,
                    "paragraphs": paragraphs,
                }
            )
        return stories

    def _ai_news_podcast_source(self, stories: list[dict]) -> str:
        """Build the bounded source block that grounds the podcast script."""
        blocks: list[str] = []
        for index, story in enumerate(stories, start=1):
            lines = [
                f"STORY {index}",
                f"Title: {story['title']}",
            ]
            if story.get("source"):
                lines.append(f"Source: {story['source']}")
            if story.get("summary"):
                lines.append(f"Summary: {story['summary']}")
            if story.get("link"):
                lines.append(f"Link: {story['link']}")
            lines.append("Article:")
            lines.extend(story.get("paragraphs") or [])
            blocks.append("\n".join(lines))
        return "\n\n".join(blocks)[:AI_NEWS_PODCAST_SOURCE_CHARS]

    def _stream_translation_paragraph(self, paragraph: str) -> None:
        """Stream one paragraph's Simplified Chinese translation."""
        payload = {
            "model": ANSWER_TRANSLATION_MODEL,
            "prompt": chat.ANSWER_TRANSLATION_PROMPT.format(text=paragraph),
            "stream": True,
            "think": False,
            "keep_alive": chat.OLLAMA_KEEP_ALIVE,
            "options": {
                "temperature": 0.1,
                "num_ctx": ANSWER_TRANSLATION_NUM_CTX,
            },
        }
        prefix_filter = TranslationPrefixFilter()
        try:
            with requests.post(
                f"{chat.OLLAMA_HOST}/api/generate",
                json=payload,
                stream=True,
                timeout=(10, 900),
            ) as response:
                response.raise_for_status()
                for line in response.iter_lines(decode_unicode=True):
                    if not line:
                        continue
                    try:
                        event = json.loads(line)
                    except json.JSONDecodeError:
                        continue
                    if event.get("error"):
                        raise RuntimeError(
                            f"Ollama error: {event['error']}"
                        )
                    token = event.get("response", "")
                    if token:
                        cleaned = prefix_filter.feed(token)
                        if cleaned:
                            self._sse_text("delta", cleaned)
                    if event.get("done"):
                        break
        except requests.RequestException as exc:
            raise RuntimeError(
                f"Could not reach Ollama at {chat.OLLAMA_HOST}. "
                f"Is it running? ({exc})"
            ) from exc

        trailing = prefix_filter.flush()
        if trailing:
            self._sse_text("delta", trailing)

    # ---- SSE: single code-line explanation ---------------------------
    def _handle_explain_line(self, payload: dict) -> None:
        """Explain one line of a Go, Rust, or PHP code snippet."""
        language = normalize_language(
            str(payload.get("language") or self.state.code_language or "")
        )
        code = payload.get("code")
        try:
            line_number = int(payload.get("line") or 0)
        except (TypeError, ValueError):
            line_number = 0

        if not language_uses_official_docs(language):
            self._send_error_json(
                "Line explanations are available for Go, Rust, and PHP.",
                400,
            )
            return
        if not isinstance(code, str) or not code.strip():
            self._send_error_json("Missing code snippet", 400)
            return
        if len(code) > MAX_LINE_EXPLAIN_CODE_CHARS:
            self._send_error_json(
                f"Code snippet is too long to explain "
                f"(limit {MAX_LINE_EXPLAIN_CODE_CHARS:,} characters).",
                413,
            )
            return
        code_lines = code.splitlines()
        if line_number < 1 or line_number > len(code_lines):
            self._send_error_json(
                f"Line {line_number} is outside the code snippet "
                f"(1-{len(code_lines)}).",
                400,
            )
            return

        fence = language_code_fence(language) or "text"
        model = language_coding_model(language)
        self._begin_sse()
        try:
            self._sse(
                "start",
                {
                    "model": model,
                    "language": language,
                    "line": line_number,
                },
            )
            self._stream_line_explanation(
                code_lines, line_number, language, fence, model
            )
            self._sse("done", {"line": line_number})
        except ClientDisconnected:
            pass
        except Exception as exc:
            try:
                self._sse("error", {"message": f"Line explanation failed: {exc}"})
                self._sse("done", {})
            except ClientDisconnected:
                pass

    def _stream_line_explanation(
        self,
        code_lines: list[str],
        line_number: int,
        language: str,
        fence: str,
        model: str,
    ) -> None:
        numbered_code = "\n".join(
            f"{index:>4} | {line}"
            for index, line in enumerate(code_lines, start=1)
        )
        prompt = LINE_EXPLAIN_PROMPT.format(
            language=language,
            fence=fence,
            numbered_code=numbered_code,
            line_number=line_number,
            line=code_lines[line_number - 1],
        )
        payload = {
            "model": model,
            "prompt": prompt,
            "stream": True,
            "think": False,
            "keep_alive": chat.OLLAMA_KEEP_ALIVE,
            "options": {
                "temperature": 0.2,
                "num_ctx": LINE_EXPLAIN_NUM_CTX,
            },
        }
        try:
            with requests.post(
                f"{chat.OLLAMA_HOST}/api/generate",
                json=payload,
                stream=True,
                timeout=(10, 900),
            ) as response:
                response.raise_for_status()
                for line in response.iter_lines(decode_unicode=True):
                    if not line:
                        continue
                    try:
                        event = json.loads(line)
                    except json.JSONDecodeError:
                        continue
                    if event.get("error"):
                        raise RuntimeError(f"Ollama error: {event['error']}")
                    token = event.get("response", "")
                    if token:
                        self._sse_text("delta", token)
                    if event.get("done"):
                        break
        except requests.RequestException as exc:
            raise RuntimeError(
                f"Could not reach Ollama at {chat.OLLAMA_HOST}. "
                f"Is it running? ({exc})"
            ) from exc

    # ---- SSE: ask a question about a whole code snippet -----------------
    def _handle_snippet_question(self, payload: dict) -> None:
        """Answer a free-form question about one code snippet."""
        code = payload.get("code")
        question = str(payload.get("question") or "").strip()
        raw_language = str(
            payload.get("language") or self.state.code_language or ""
        ).strip()

        if not question:
            self._send_error_json("Missing question", 400)
            return
        if not isinstance(code, str) or not code.strip():
            self._send_error_json("Missing code snippet", 400)
            return
        if len(code) > MAX_SNIPPET_QUESTION_CODE_CHARS:
            self._send_error_json(
                f"Code snippet is too long to ask about "
                f"(limit {MAX_SNIPPET_QUESTION_CODE_CHARS:,} characters).",
                413,
            )
            return

        unsupported = ("", "default", "text", "plaintext")
        language = (
            "programming"
            if raw_language.lower() in unsupported
            else raw_language
        )
        fence = re.sub(r"[^A-Za-z0-9_+.#-]", "", raw_language) or "text"
        model = CODE_GENERATION_MODEL

        self._begin_sse()
        try:
            self._sse(
                "start",
                {"model": model, "language": language},
            )
            self._stream_snippet_question(
                code, question, language, fence, model
            )
            self._sse("done", {})
        except ClientDisconnected:
            pass
        except Exception as exc:
            try:
                self._sse(
                    "error",
                    {"message": f"Snippet answer failed: {exc}"},
                )
                self._sse("done", {})
            except ClientDisconnected:
                pass

    def _stream_snippet_question(
        self,
        code: str,
        question: str,
        language: str,
        fence: str,
        model: str,
    ) -> None:
        prompt = SNIPPET_QUESTION_PROMPT.format(
            language=language,
            fence=fence,
            code=code,
            question=question,
        )
        payload = {
            "model": model,
            "prompt": prompt,
            "stream": True,
            "think": False,
            "keep_alive": chat.OLLAMA_KEEP_ALIVE,
            "options": {
                "temperature": 0.2,
                "num_ctx": SNIPPET_QUESTION_NUM_CTX,
            },
        }
        try:
            with requests.post(
                f"{chat.OLLAMA_HOST}/api/generate",
                json=payload,
                stream=True,
                timeout=(10, 900),
            ) as response:
                response.raise_for_status()
                for line in response.iter_lines(decode_unicode=True):
                    if not line:
                        continue
                    try:
                        event = json.loads(line)
                    except json.JSONDecodeError:
                        continue
                    if event.get("error"):
                        raise RuntimeError(f"Ollama error: {event['error']}")
                    token = event.get("response", "")
                    if token:
                        self._sse_text("delta", token)
                    if event.get("done"):
                        break
        except requests.RequestException as exc:
            raise RuntimeError(
                f"Could not reach Ollama at {chat.OLLAMA_HOST}. "
                f"Is it running? ({exc})"
            ) from exc

    # ---- SSE: YouTube video summary ----------------------------------
    def _handle_video_summary(self, payload: dict) -> None:
        """Fetch one video's transcript and stream an AI summary."""
        video_id = str(payload.get("id") or "").strip()
        if not re.fullmatch(r"[A-Za-z0-9_-]{11}", video_id):
            self._send_error_json("Missing or invalid YouTube video id", 400)
            return

        title = str(payload.get("title") or "").strip() or video_id
        channel = str(payload.get("channel") or "").strip()
        published = str(payload.get("published") or "").strip()
        url = str(payload.get("url") or "").strip() or (
            f"https://www.youtube.com/watch?v={video_id}"
        )
        try:
            duration = int(payload.get("duration") or 0)
        except (TypeError, ValueError):
            duration = 0

        self._begin_sse()
        try:
            self._sse(
                "start",
                {
                    "id": video_id,
                    "title": title,
                    "channel": channel,
                    "published": published,
                    "duration": duration,
                    "url": url,
                },
            )
            # Import lazily so a missing youtube-transcript-api dependency
            # only breaks this endpoint, not the whole chat server.
            try:
                import fetch_youtube_transcript as transcript_module
            except ImportError as exc:
                raise RuntimeError(
                    "youtube-transcript-api is required for video summaries "
                    f"({exc})"
                ) from exc

            transcript = transcript_module.fetch_transcript(video_id)
            if not transcript.strip():
                raise RuntimeError("the video has an empty transcript")
            self._stream_video_summary(
                title=title,
                channel=channel,
                published=published,
                duration=duration,
                url=url,
                transcript=transcript,
            )
            self._sse("done", {"id": video_id})
        except ClientDisconnected:
            pass
        except Exception as exc:
            try:
                self._sse("error", {"message": f"Video summary failed: {exc}"})
                self._sse("done", {"id": video_id})
            except ClientDisconnected:
                pass

    def _stream_video_summary(
        self,
        *,
        title: str,
        channel: str,
        published: str,
        duration: int,
        url: str,
        transcript: str,
    ) -> None:
        """Stream the model's summary of one YouTube transcript."""
        transcript = transcript.strip()
        if len(transcript) > MAX_VIDEO_SUMMARY_TRANSCRIPT_CHARS:
            transcript = transcript[:MAX_VIDEO_SUMMARY_TRANSCRIPT_CHARS].rsplit(
                " ", 1
            )[0]
            transcript += "\n\n[Transcript truncated because of its length.]"

        prompt = VIDEO_SUMMARY_PROMPT.format(
            title=title,
            channel=channel or "unknown",
            published=published or "unknown",
            duration=(
                f"{duration // 60}:{duration % 60:02d}" if duration > 0 else "unknown"
            ),
            url=url,
            transcript=transcript,
        )
        payload = {
            "model": VIDEO_SUMMARY_MODEL,
            "prompt": prompt,
            "stream": True,
            "think": False,
            "keep_alive": chat.OLLAMA_KEEP_ALIVE,
            "options": {
                "temperature": 0.2,
                "num_ctx": VIDEO_SUMMARY_NUM_CTX,
            },
        }
        try:
            with requests.post(
                f"{chat.OLLAMA_HOST}/api/generate",
                json=payload,
                stream=True,
                timeout=(10, 900),
            ) as response:
                response.raise_for_status()
                for line in response.iter_lines(decode_unicode=True):
                    if not line:
                        continue
                    try:
                        event = json.loads(line)
                    except json.JSONDecodeError:
                        continue
                    if event.get("error"):
                        raise RuntimeError(f"Ollama error: {event['error']}")
                    token = event.get("response", "")
                    if token:
                        self._sse_text("delta", token)
                    if event.get("done"):
                        break
        except requests.RequestException as exc:
            raise RuntimeError(
                f"Could not reach Ollama at {chat.OLLAMA_HOST}. "
                f"Is it running? ({exc})"
            ) from exc

    # ---- SSE: dictionary popup ---------------------------------------
    def _handle_dict(self, word: str, answer: str | None) -> None:
        self._begin_sse()
        try:
            if not word:
                self._sse_text("delta", "Usage: /dict <word>\n")
            else:
                context = answer if answer is not None else self.state.latest_answer
                self._stream_dictionary(word, context)
            self._sse("done", {"word": word})
        except ClientDisconnected:
            pass
        except Exception as exc:
            try:
                self._sse("error", {"message": str(exc)})
                self._sse("done", {"word": word})
            except ClientDisconnected:
                pass

    def _stream_dictionary(self, word: str, latest_answer: str | None) -> None:
        # The popup shows exactly: word, IPA, part of speech, and the
        # contextual meaning. Keep the prompt and the streamed output limited
        # to those fields (no quote, model banner, or follow-up usage section).
        context = chat.dictionary_surrounding_context(word, latest_answer)
        prompt = chat.DICT_POPUP_PROMPT.format(
            word=word,
            context=(context or "(no previous answer available)"),
        )
        try:
            chat.stream_answer(
                prompt,
                model=chat.DICT_MODEL,
                emit=lambda text: self._sse("delta", {"text": text}),
            )
        except RuntimeError as exc:
            self._sse("error", {"message": f"Dictionary lookup failed: {exc}"})


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="HTTP backend for the app/chat.html browser console."
    )
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", type=int, default=8765)
    parser.add_argument(
        "--max-context-tokens",
        type=int,
        default=chat.MAX_CONTEXT_TOKENS,
        metavar="N",
        help="estimated-token budget for adaptive retrieval",
    )
    parser.add_argument(
        "--progressive",
        action="store_true",
        help="stream a fast first pass, then append an expansion pass",
    )
    parser.add_argument(
        "--no-prewarm",
        action="store_true",
        help="do not warm the Ollama models at startup",
    )
    return parser.parse_args(argv)


def main() -> None:
    args = parse_args()
    if not CHAT_HTML.is_file():
        sys.exit(f"Missing front end: {CHAT_HTML}")

    # Warm the models while the corpus is being loaded, then wait for the
    # warmup to finish before accepting requests. Serving during prewarm made
    # the first question compete with a 20+ GB model load, which more than
    # doubled its time to first token.
    prewarm_thread: threading.Thread | None = None
    if not args.no_prewarm:
        prewarm_thread = threading.Thread(target=chat.prewarm, daemon=True)
        prewarm_thread.start()
        print("Warming Ollama models...", flush=True)

    try:
        state = ChatState(
            max_context_tokens=args.max_context_tokens,
            progressive=args.progressive,
        )
    except SystemExit as exc:
        sys.exit(f"Cannot start chat server: {exc}")

    if prewarm_thread is not None:
        # Do not block startup forever if Ollama is slow or unavailable; the
        # first real request still works and will warm the model itself.
        prewarm_thread.join(timeout=120)
        if prewarm_thread.is_alive():
            print(
                "Ollama warmup is still running; starting anyway.",
                flush=True,
            )
        else:
            print("Ollama models are ready.", flush=True)

    # Transcript/article/book FTS tables describe sources that are no longer
    # open after a restart. They are cheap to rebuild, so drop them and return
    # their pages to the filesystem instead of letting the database grow
    # forever. This runs before the global index is opened so VACUUM can take
    # the exclusive lock it needs.
    try:
        sqlite_bm25.drop_transient_corpora(
            chat.BM25_DB_PATH, vacuum=True
        )
    except Exception:
        pass

    # Build the BM25 index before accepting requests. Otherwise the first
    # question pays the index-construction cost for the whole corpus. The
    # startup corpus is the full global one, so this is also the safe place to
    # prune DB rows whose trunk files were deleted. Merge FTS5 segments after
    # the sync so the first MATCH scans an optimized b-tree.
    try:
        startup_index = chat.get_bm25_index(state.chunks, prune=True)
        startup_index.bm25.optimize()
        startup_index.bm25.vacuum()
    except Exception:
        # Index maintenance is best-effort; a later question rebuilds lazily.
        pass

    ChatRequestHandler.state = state
    server = ThreadingHTTPServer((args.host, args.port), ChatRequestHandler)
    server.daemon_threads = True
    address = f"http://{args.host}:{args.port}/"
    all_ids = {chunk.video_id for chunk in state.chunks}
    news_count = sum(1 for video_id in all_ids if NEWS_ID_PATTERN.fullmatch(video_id))
    source_summary = f"({len(all_ids) - news_count} videos)"
    if news_count:
        source_summary = (
            f"({len(all_ids) - news_count} videos, "
            f"{news_count} articles)"
        )
    print(
        f"Loaded {len(state.chunks)} trunks into the global corpus "
        f"{source_summary}.",
        flush=True,
    )
    print(f"YouTube local chat server running at {address}", flush=True)
    print("Press Ctrl+C to stop.", flush=True)
    # Warm the optional text-to-speech engine (Kokoro when installed) after the
    # chat server is ready. The news read-aloud button then responds quickly
    # on its first paragraph instead of paying the model-load cost.
    threading.Thread(target=_prewarm_tts, daemon=True).start()
    if KEEPALIVE_WATCHDOG_ENABLED:
        threading.Thread(target=_keepalive_watchdog, daemon=True).start()
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        print("\nStopping chat server.")
    finally:
        server.server_close()


if __name__ == "__main__":
    main()
