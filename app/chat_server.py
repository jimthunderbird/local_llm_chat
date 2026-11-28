#!/usr/bin/env python3
"""HTTP API server for the browser front end in app/chat.html.

The server reuses chat.py for chunk loading, BM25 retrieval, prompt building,
Ollama streaming, transcript/article scoping, history, /memory,
/status, /sources, /benchmark, /youtube, /news, /mdn, /britanica,
/stackoverflow, /reddit, /tablelog, /stock, /googlenews, /gkg, /medium,
/gutenberg, /openlibrary, /internet_archive, /changelog, /wikivoyage, /map,
/game, /system_design_and_architecture, /wiki,
/wiki_article, /deepresearch, /write_a_book, /books, /pdf_books, /robinhood,
/article, /dict, /dictionary_history, and /ask. The browser is a
pure HTML/CSS/JavaScript single-page app served from ./app/chat.html.

Every question searches the one global trunk corpus. Tags are no longer a
user-facing scope; the corpus is never partitioned by topic.

Usage:
    python3 app/chat_server.py [--host 127.0.0.1] [--port 8765]
                                  [--max-context-tokens N] [--progressive]

Endpoints:
    GET  /                       app/chat.html
    GET  /api/state              current chat state + command list + history
    GET  /api/english_words      ?range=<10k|20k|30k|50k|100k|range> the
                                  English Corner vocabulary used to highlight
                                  words in article readers
    GET  /api/autocomplete       ?kind=commands|games|transcripts|paths|
                                  stackoverflow_subcommands&q=...
    GET  /api/wiki?word=...      JSON Wikipedia summary
    GET  /api/transcripts?q=...  ag title search results
    GET  /api/changelog_file     ?name=<summary.html> one completed-task page
                                  used by /changelog's detail modal
    GET  /api/system_design_topic ?path=<en/chapter.md> one English chapter
                                  from puncsky/system-design-and-architecture
                                  for the /system_design_and_architecture
                                  reader modal, with images made absolute
    GET  /api/book_file          ?name=<book.html> one book written by
                                  /write_a_book
    GET  /api/pdf_book_file      ?name=<book.pdf> one PDF from .chat/pdf_books
    GET  /api/pdf_book_page      ?name=<book.pdf>&page=N one page of PDF text
                                  for the page-by-page reader, served as an HTML page
    POST /api/pdf_books/download  {slug} download one Manning free ebook into
                                  .chat/pdf_books and return its reader card
    GET  /api/gutenberg_book     ?id=<book_id>&url=<plaintext_url>&title=...
                                  the cached plaintext used by the book modal
    POST /api/ask                SSE: question or slash-command stream
                                  (an optional `image` data URL attaches one
                                  PNG/JPEG/WebP/GIF and answers visually.
                                  Answers include a per-paragraph
                                  source-video annotation event. A question that
                                  asks for Rust, Go, or PHP code uses the
                                  official documentation for that language and
                                  consults it before any local code generation.
                                  Explanatory language questions do not load
                                  language documentation. A "source" event says
                                  whether the currently streaming answer is
                                  coming from the local Ollama model or the
                                  official docs. /news streams "news" events
                                  from CNN, Yahoo Finance,
                                  KQED, SFGATE, SF Bay View, Mission Local,
                                  Richmond Review, and TechCrunch.
                                  /stackoverflow tag|search also streams a
                                  /news-style result list.)
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
    POST /api/news_learn         store one /news story or loaded Reddit post
                                  and build its learned trunks in
                                  .chat/articles_cache
    GET  /api/news_status        ?id=<news_id> whether one story is learned
    GET  /api/news_item          ?id=<news_id> full learned story for the modal
    GET  /api/news_article       ?url=<article_url> fetch a story body for the
                                  detail modal when the list feed had no body
    GET  /api/news_open          ?url=<article_url> redirect to the publisher,
                                  resolving a Google News wrapper first
    GET  /api/reddit_article     ?url=<post_url> fetch a Reddit post body and
                                  comments for the /reddit detail modal
    GET  /api/stock_reddit       ?symbol=<ticker> fetch unique Reddit posts from
                                  r/stock and r/<ticker> for the /stock modal
    GET  /api/map_geocode        ?q=<address>&limit=<n> address autocomplete
                                  and coordinate lookup for /map's To location tab
                                  optional viewbox=<west>,<south>,<east>,<north>,
                                  bounded=1, and countrycodes=<cc[,cc]>
    GET  /api/game_geocode       ?game=<game_name>&q=<place or address>&limit=<n>
                                  combined place and address autocomplete
                                  constrained to one game's exploration
                                  boundary for the /game Set Current Location
                                  dropdown
    GET  /api/map_restaurants    ?lat=<latitude>&lon=<longitude> fetch nearby
                                  restaurants from the Overpass API for /map
    GET  /api/game_nearby        ?lat=<latitude>&lon=<longitude> fetch nearby
                                  attractions and nearby restaurants for the
                                  /game explorer map
    GET  /api/game_place         ?url=<osm_url>&name=<name> fetch one explorer
                                  marker's tags, geolocated Wikipedia extract, and
                                  trafilatura-extracted website text for its
                                  right-click place modal
    GET  /api/game_place_news    ?lat=<latitude>&lon=<longitude> reverse-geocode
                                  the place name with Nominatim and list its
                                  recent Google News stories for the explorer
                                  place modal's Local News section
    GET  /api/game_place_properties ?lat=<latitude>&lon=<longitude> list nearby
                                  for-sale and rental properties from the
                                  Zillow MCP server for the place modal
    POST /api/game_current_location
                                  {"game":..., "latitude":..., "longitude":...}
                                  persist the explorer's chosen starting point
                                  so the next /game opens there
    GET  /api/driving_instructions
                                  ?from_lat=<lat>&from_lon=<lon>
                                  &to_lat=<lat>&to_lon=<lon> driving route and
                                  turn-by-turn steps from the OSRM demo server
    GET  /api/osm_restaurant     ?type=<node|way|relation>&id=<osm_id>
                                  fetch one restaurant's cuisine, address, and
                                  opening hours from the Overpass API for the
                                  /map restaurant modal (no API key needed)
    GET  /api/yelp_restaurant    ?lat=<latitude>&lon=<longitude>&name=<name>
                                  optional Yelp Fusion v3 enrichment for a /map
                                  restaurant modal (used only when configured)
    GET  /api/stock_summary      ?symbol=<ticker> fetch a company summary for a
                                  /stock holding, shown in its own modal
    GET  /api/stackoverflow_answers
                                  ?id=<question_id> fetch a question's answers
                                  for the /stackoverflow detail modal
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
                                  the background for a /youtube search card;
                                  {"relearn": true} rebuilds the title and
                                  trunks from the saved transcript
    GET  /api/video_learn_status current background learn progress (all jobs,
                                  or ?id=<video_id> for one)
    GET  /api/video_transcript_timeline
                                  ?id=<video_id>&duration=<seconds> timestamped
                                  transcript segments for the video modal's
                                  Realtime Transcript switch, cached beside the
                                  learned transcript
    POST /api/gutenberg_chat     SSE: answer a question about one Project
                                  Gutenberg book in a session isolated from
                                  the main chat
"""

# The feature implementation lives in app/server_parts/ (one module per
# aspect). This file only composes the HTTP handler and starts the process.

from __future__ import annotations

import sys
from pathlib import Path

_APP_DIR = Path(__file__).resolve().parent
if str(_APP_DIR) not in sys.path:
    sys.path.insert(0, str(_APP_DIR))

from server_parts.core import *  # noqa: F401,F403
from server_parts.english_corner import *  # noqa: F401,F403
from server_parts.news_research import *  # noqa: F401,F403
from server_parts.docs import *  # noqa: F401,F403
from server_parts.stock import *  # noqa: F401,F403
from server_parts.podcast import *  # noqa: F401,F403
from server_parts.books import *  # noqa: F401,F403
from server_parts.retrieval import *  # noqa: F401,F403
from server_parts.tts import *  # noqa: F401,F403
from server_parts.news import *  # noqa: F401,F403
from server_parts.robinhood import *  # noqa: F401,F403
from server_parts.zillow import *  # noqa: F401,F403
from server_parts.stackoverflow import *  # noqa: F401,F403
from server_parts.wikivoyage import *  # noqa: F401,F403
from server_parts.map import *  # noqa: F401,F403
from server_parts.game import *  # noqa: F401,F403
from server_parts.changelog import *  # noqa: F401,F403
from server_parts.mathtutor import *  # noqa: F401,F403
from server_parts.command_config import *  # noqa: F401,F403
from server_parts.translation import *  # noqa: F401,F403
from server_parts.code import *  # noqa: F401,F403
from server_parts.video import *  # noqa: F401,F403
from server_parts.image import *  # noqa: F401,F403
from server_parts.youtube import *  # noqa: F401,F403
from server_parts.config import *  # noqa: F401,F403
from server_parts.programming import *  # noqa: F401,F403
from server_parts.github import *  # noqa: F401,F403
from server_parts.programming_docs import *  # noqa: F401,F403
from server_parts.conversation import *  # noqa: F401,F403
from server_parts.answer import *  # noqa: F401,F403
from server_parts.article import *  # noqa: F401,F403
from server_parts.library import *  # noqa: F401,F403
from server_parts.directory_anthem import *  # noqa: F401,F403
from server_parts.directory_uhc import *  # noqa: F401,F403
from server_parts.state import *  # noqa: F401,F403
from server_parts.reddit import *  # noqa: F401,F403
from server_parts.handler_core import HandlerCoreMixin
from server_parts.handler_ask import HandlerAskMixin
from server_parts.handler_directory import HandlerDirectoryMixin
from server_parts.handler_research import HandlerResearchMixin
from server_parts.handler_books import HandlerBooksMixin
from server_parts.handler_youtube import HandlerYoutubeMixin
from server_parts.handler_commands_a import HandlerCommandsAMixin
from server_parts.handler_commands_b import HandlerCommandsBMixin
from server_parts.handler_commands_c import HandlerCommandsCMixin
from server_parts.handler_moonbit import HandlerMoonbitMixin
from server_parts.handler_sat import HandlerSatMixin
from server_parts.handler_system_design import HandlerSystemDesignMixin
from server_parts.article import _iterative_paragraph_chunks
from server_parts.tts import _keepalive_watchdog, _prewarm_tts


class ChatRequestHandler(
    HandlerCoreMixin,
    HandlerAskMixin,
    HandlerDirectoryMixin,
    HandlerResearchMixin,
    HandlerBooksMixin,
    HandlerYoutubeMixin,
    HandlerCommandsAMixin,
    HandlerCommandsBMixin,
    HandlerCommandsCMixin,
    HandlerMoonbitMixin,
    HandlerSatMixin,
    HandlerSystemDesignMixin,
    BaseHTTPRequestHandler,
):
    """One HTTP handler per request; SSE responses stream from Ollama."""

    server_version = "YTLocalChat/1.0"
    protocol_version = "HTTP/1.1"

    # Injected in main().
    state: ChatState

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


def _chat_server_pids(
    ps_output: str, *, current_pid: int, parent_pid: int
) -> list[int]:
    """Return PIDs of other running chat_server.py processes.

    ``ps_output`` is the output of ``ps -axo pid=,ppid=,command=``.  The
    current process and its parent are protected so a wrapper shell or a test
    harness whose command line happens to mention the script is never killed.
    """
    pids: list[int] = []
    protected = {current_pid, parent_pid}
    for line in ps_output.splitlines():
        fields = line.strip().split(None, 2)
        if len(fields) != 3:
            continue
        pid_text, ppid_text, command = fields
        try:
            pid = int(pid_text)
            ppid = int(ppid_text)
        except ValueError:
            continue
        if pid in protected:
            continue
        lowered = command.lower()
        if "chat_server.py" not in lowered and "app.chat_server" not in lowered:
            continue
        # Only stop actual Python interpreters running the server, not an
        # editor, pager, grep, or other tool whose arguments mention it.
        tokens = lowered.split()
        executable = os.path.basename(tokens[0])
        is_python = executable.startswith(("python", "pypy"))
        if not is_python and executable in {"env", "uv", "poetry", "pipenv"}:
            is_python = any(
                os.path.basename(token).startswith(("python", "pypy"))
                for token in tokens[1:]
            )
        if not is_python:
            continue
        pids.append(pid)
    return sorted(set(pids))


def _pid_is_running(pid: int) -> bool:
    """Whether ``pid`` is still running (a zombie no longer counts)."""
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    except OSError:
        return False
    try:
        result = subprocess.run(
            ["ps", "-p", str(pid), "-o", "stat="],
            capture_output=True,
            text=True,
            timeout=2,
            check=False,
        )
    except (OSError, subprocess.SubprocessError):
        return True
    state = result.stdout.strip()
    if not state:
        return False
    return not state.startswith("Z")


def _stop_existing_chat_server() -> None:
    """Stop a previously started chat_server.py before binding the port."""
    try:
        result = subprocess.run(
            ["ps", "-axo", "pid=,ppid=,command="],
            capture_output=True,
            text=True,
            timeout=5,
            check=False,
        )
    except (OSError, subprocess.SubprocessError) as exc:
        print(f"Could not inspect running chat servers: {exc}", flush=True)
        return
    pids = _chat_server_pids(
        result.stdout,
        current_pid=os.getpid(),
        parent_pid=os.getppid(),
    )
    if not pids:
        return

    print(
        "Stopping existing chat server process(es): "
        + ", ".join(str(pid) for pid in pids),
        flush=True,
    )
    for sig, timeout in ((signal.SIGTERM, 8.0), (signal.SIGKILL, 2.0)):
        signaled: list[int] = []
        for pid in pids:
            try:
                os.kill(pid, sig)
            except ProcessLookupError:
                continue
            except OSError as exc:
                print(f"Could not stop chat server PID {pid}: {exc}", flush=True)
                continue
            signaled.append(pid)
        pids = signaled
        deadline = time.monotonic() + timeout
        while pids and time.monotonic() < deadline:
            pids = [pid for pid in pids if _pid_is_running(pid)]
            if pids:
                time.sleep(0.1)
        if not pids:
            break

    if pids:
        print(
            "Warning: existing chat server process(es) still running: "
            + ", ".join(str(pid) for pid in pids),
            flush=True,
        )
    else:
        print("Existing chat server stopped.", flush=True)


def main() -> None:
    args = parse_args()
    if not CHAT_HTML.is_file():
        sys.exit(f"Missing front end: {CHAT_HTML}")

    # A second instance would compete for the port and, worse, load the same
    # large Ollama models twice. Stop a prior instance before doing any warmup
    # or corpus loading so a restart always replaces the old server.
    _stop_existing_chat_server()

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
    try:
        # Build the paragraph-only retrieval list as well. The first question
        # otherwise paid for article heading annotation and filtering before
        # its first retrieval round.
        _iterative_paragraph_chunks(state.chunks)
    except Exception:
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
