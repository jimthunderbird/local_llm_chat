"""Shared imports, paths, command catalogue, and small cross-feature helpers."""



from __future__ import annotations



import argparse
import ast
import base64
import codecs
import csv
import gc
import hashlib
import html
import importlib.util
import io
import json
import math
import os
import queue
import re
import selectors
import shlex
import shutil
import signal
import subprocess
import sys
import tempfile
import threading
import wave
import zipfile
import time
import unicodedata
from datetime import datetime, timedelta, timezone
from email.utils import parsedate_to_datetime
from concurrent.futures import ThreadPoolExecutor, as_completed
from contextlib import redirect_stderr, redirect_stdout
from functools import lru_cache
from html.parser import HTMLParser
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Callable, NamedTuple
from urllib.parse import parse_qs, quote, quote_plus, unquote, urlparse
from xml.sax.saxutils import escape as xml_escape
from xml.etree import ElementTree



# Place modals use trafilatura to turn a place's linked website into readable
# paragraphs. It is optional so the rest of the server still starts when the
# package has not been installed yet; only the "From Website" section is then
# shown with an explanatory error.
try:
    import trafilatura
except ImportError:  # pragma: no cover - optional dependency
    trafilatura = None



# The package lives in ./app/server_parts; the repository root holds chat.py

# and its data directories. Resolve both and make the root importable.

APP_DIR = Path(__file__).resolve().parent.parent

SCRIPT_DIR = APP_DIR.parent

if str(SCRIPT_DIR) not in sys.path:

    sys.path.insert(0, str(SCRIPT_DIR))



import chat
import deep_research
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

# English Corner: /settings english_words_to_learn <range> selects the
# frequency band whose words are highlighted in news and article readers. The
# band names and range syntax come from the English Corner word finder, so the
# UI and the CLI accept exactly the same options. The setting is small and

WEB_COMMANDS = {
    **chat.COMMANDS,
    "/help": "show commands and keyboard shortcuts",
    "/compact": "summarize older conversation turns and keep only a short recent tail",
    "/youtube": "YouTube commands: learn <hash|url> | relearn <hash|url> | "
    "learn_topic <topic> | learn_playlist <playlist> | search <keyword> | bookmark list",
    "/news": "list the latest news from wsj, cnn, cbslite, nprtext, yahoo_finance, medium, kqed, sfgate, sfbayview, mission_local, richmond_sunset, techcrunch, krasia, wccftech, or hackernews (optionally /news [source ...] [count]); /news search <keywords> [website] searches Google News, optionally only that website",
    "/mdn": "search MDN Web Docs by keywords: /mdn <keywords>",
    "/britanica": "search Encyclopædia Britannica by keywords: /britanica <keywords>",
    "/googlenews": "search Google News for the latest stories: /googlenews <keywords>",
    "/gkg": "search the latest GDELT 2.0 Global Knowledge Graph news: /gkg <keyword>[:<location>] (the location is geocoded and searched within 15 km; for example /gkg restaurant:san francisco, ca)",
    "/howstuffworks": "search HowStuffWorks articles by keywords: /howstuffworks <keywords>",
    "/wikivoyage": "search Wikivoyage destinations by keywords: /wikivoyage <keywords>",
    "/map": "geocode a location with OpenStreetMap and list nearby Wikipedia articles: /map <location>",
    "/game": "play a map explorer game: /game lists the games; /game <game_name> starts one",
    "/medium": "list the latest stories from the Medium author in ~/.medium/username (optionally /medium [count])",
    "/ai_news_podcast": "show the top five stories from the last 12 hours and create a two-voice AI news podcast",
    "/gutenberg": "search Project Gutenberg books: /gutenberg search <keywords>; click a book to read it and ask questions",
    "/openlibrary": "search inside Open Library books: /openlibrary search <keywords>",
    "/internet_archive": "search Internet Archive digitized texts and catalogue metadata: /internet_archive <keywords>",
    "/article": "open an article URL in the reader popup or use a local article file as context (/article <url|path>; --simple disables the local-file evidence loop)",
    "/mathtutor": "upload Microsoft Word test papers and practice similar math problems with detailed answers",
    "/changelog": "list completed-task summaries from .logs (newest first); click one to read it",
    "/stackoverflow": "search Stack Overflow: /stackoverflow tag <tag> or /stackoverflow search <keywords>",
    "/reddit": "search Reddit posts by keywords: /reddit <keywords>; also searches related subreddits; click a post to read it",
    "/tablelog": "japan food and restaurant guide for travelers - search Tabelog restaurant pages by keywords: /tablelog <keywords>",
    "/stock": "show a stock symbol profile, financial details, and related news articles: /stock <stock_symbol>",
    "/robinhood": "show your Robinhood holdings, pending orders, and recent transactions for a symbol through the Robinhood MCP server: /robinhood <stock_symbol>",
    "/zillow": "show nearby for-sale and rental listings from Zillow's official MCP server: /zillow <latitude,longitude> [radius_miles]",
    "/moonbit": "ask about the MoonBit programming language using the official docs; /moonbit off returns to normal chat",
    "/sat_1600": "ask an SAT question using the 1600.now question bank and local model; /sat_1600 off returns to normal chat",
    "/deepresearch": "run a bounded multi-round research report on <topic>: /deepresearch <topic>",
    "/research_with_the_news": "research a topic from recent news, Wikipedia, or a doctor's hospital website, then keep doing so for each follow-up: /research_with_the_news <topic>; /research_with_the_news on; /research_with_the_news off",
    "/anthem": "search the Anthem Provider Directory for doctors by name, specialty, hospital, or location; each result opens a provider profile; follow-up questions keep the provider context: /anthem <question>; /anthem off",
    "/united_healthcare": "search the UnitedHealthcare provider directory by NPI, doctor name, specialty, or hospital; each result opens a provider profile; /united_healthcare <question>; /united_healthcare off",
    "/write_a_book": "ask for every book setting (model, directory, words, chapters, sub-questions, articles per sub-question), then research recent news and write a book: /write_a_book [<subject>]",
    "/books": "list the books written by /write_a_book in .chat/books: /books [keyword]",
    "/pdf_books": "list the PDF books in .chat/pdf_books and read them page by page",
    "/settings": "set English Corner options: /settings english_words_to_learn <range> (3k, 5k, 10k, 20k, 30k, 50k, 100k, or a range such as 10k..50k; default 10k..50k; off disables highlighting)",
}

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

ARTICLES_CACHE_DIR = chat.ARTICLES_DIR
NEWS_CACHE_DIR = ARTICLES_CACHE_DIR
NEWS_CACHE_LOCK = threading.Lock()
NEWS_ID_PATTERN = re.compile(r"news_[A-Za-z0-9]+")

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

try:
    import requests
except ImportError as exc:  # pragma: no cover - dependency guard
    sys.exit(f"Missing dependency: {exc.name}. Install with: pip install requests")
