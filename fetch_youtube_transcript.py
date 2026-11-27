#!/usr/bin/env python3
"""Fetch an English YouTube transcript and summarize it with Ollama.

Usage:
    python3 fetch_youtube_transcript.py <youtube_video_hash>
    python3 fetch_youtube_transcript.py --force <youtube_video_hash>

Outputs:
    ./.chat/articles_cache/youtube_<youtube_video_hash>.transcript
    ./.chat/articles_cache/youtube_<youtube_video_hash>.title

task.third_party_ip (prompts/youtube.prompt): this pipeline uses a
third-party transcript site (youtubetotranscript.com) as its only transcript
source. The site downloads the captions from YouTube on its own network, so
rate limits or IP blocks on YouTube never apply to this pipeline. The
YouTube-specific helpers (youtube-transcript-api and yt-dlp) are deliberately
NOT used here.

The transcript page on the third-party site asks browser clients to pass a
verification check, so the page is read through three transports, tried in
order:

    1. Direct HTTP request with browser-like headers (fastest when the site
       lets the request through).
    2. Ollama's server-side web-fetch API, which reads the page from Ollama's
       cloud (requires OLLAMA_CLOUD_API_KEY, OLLAMA_API_KEY is also accepted).
    3. The local Chrome browser through its DevTools Protocol. The headful
       browser solves the site's verification check automatically. When the
       site serves its interactive "Verify you are human" checkbox instead,
       click it once in the Chrome window that pops up; the answer is stored
       in the persistent browser profile, so later runs pass automatically.
       A brief Chrome window may appear while the transcript is being read.

When the transcript returned by the site is not English, it is translated to
English with the local Ollama model. This handles videos whose only English
option is a translated caption track.

Optional environment variables:
    OLLAMA_HOST               Ollama address (default: http://localhost:11434)
    OLLAMA_YOUTUBE_MODEL      title-summary / translation model
                              (default: ornith-1.5:35b)
    OLLAMA_CLOUD_API_KEY      Ollama key enabling the server-side web-fetch
                              transport (OLLAMA_API_KEY also accepted)
    OLLAMA_WEB_FETCH_URL      web-fetch endpoint (default:
                              https://ollama.com/api/web_fetch)
    YT_BROWSER_PATH           path to a Chromium-based browser executable
                              (auto-detected by default)
    YT_CDP_PORT               fixed DevTools debugging port (random port is
                              allocated by default)
    YT_BROWSER_FETCH_TIMEOUT  how long the browser transport waits for the
                              transcript page (default: 120 seconds)
    YT_BROWSER_PROFILE_DIR   directory for the persistent browser profile
                              (default:
                              .chat/browser_profile_youtubetotranscript)
"""

import argparse
import html
import json
import os
import re
import shutil
import subprocess
import sys
import threading
import time
from pathlib import Path

try:
    import requests
except ImportError:  # pragma: no cover - dependency guard
    requests = None


def normalize_argv(argv):
    """Let positional values begin with '-' (e.g. video ID -PcBFgkr2o).

    Recognized switches are moved in front of the positional value, so
    ``--force -PcBFgkr2o`` and ``-- -PcBFgkr2o`` both work.
    """
    known_switches = {"-h", "--help", "--force", "--reuse-transcript", "--"}
    switches = [arg for arg in argv if arg in known_switches and arg != "--"]
    positionals = [arg for arg in argv if arg not in known_switches]
    if any(arg.startswith("-") for arg in positionals):
        return [*switches, "--", *positionals]
    return [*switches, *positionals]


# The YouTube-learning pipeline always uses the large answer model.
MODEL = os.environ.get("OLLAMA_YOUTUBE_MODEL", "ornith-1.5:35b")
OUTPUT_DIR = Path.cwd() / ".chat" / "articles_cache"
YOUTUBE_PREFIX = "youtube_"

OLLAMA_HOST = os.environ.get("OLLAMA_HOST", "http://localhost:11434").rstrip("/")
if not OLLAMA_HOST.startswith(("http://", "https://")):
    OLLAMA_HOST = f"http://{OLLAMA_HOST}"

# Third-party transcript site.  This site renders a plain-text transcript for
# any publicly available YouTube video and downloads the captions on its own
# network, so YouTube's own rate limiting never enters this pipeline.
TRANSCRIPT_SITE_URL = "https://youtubetotranscript.com/transcript?v={video_id}"

OLLAMA_WEB_FETCH_URL = os.environ.get(
    "OLLAMA_WEB_FETCH_URL", "https://ollama.com/api/web_fetch"
).rstrip("/")
OLLAMA_WEB_FETCH_TIMEOUT = 180

# The site's verification page masquerades as the real one for non-browser
# clients; these markers tell the transports apart.
SITE_BLOCK_MARKERS = (
    "just a moment",
    "security verification",
    "attention required",
    "verify you are human",
    "checking your browser",
)

SITE_NO_TRANSCRIPT_MARKERS = (
    "no transcript",
    "transcript not found",
    "does not have",
    "has no captions",
    "no subtitles",
)

# Segments live in one span per caption piece:  <span
# class="transcript-segment ..." data-start="8.16">…text…</span>
_SITE_SEGMENT_RE = re.compile(
    r'<span[^>]*class="[^"]*transcript-segment[^"]*"[^>]*>(.*?)</span>',
    re.DOTALL,
)

_SITE_TITLE_RE = re.compile(r"<title>(.*?)</title>", re.DOTALL)

BROWSER_FETCH_TIMEOUT = float(os.environ.get("YT_BROWSER_FETCH_TIMEOUT", 120))

BROWSER_STATE_JS = r"""(() => {
  const html = document.documentElement;
  return JSON.stringify({
    title: document.title || '',
    videoId: html.dataset.video_id || null,
    returned: html.dataset.transcript_returned ?? null,
    langs: html.dataset.current_language_code || null,
    segCount: document.querySelectorAll('span.transcript-segment').length,
    chars: document.body ? document.body.innerText.length : 0
  });
})()"""

BROWSER_EXTRACT_JS = r"""(() => JSON.stringify(
  [...document.querySelectorAll('span.transcript-segment')].map(
    (element) => (element.textContent || '').replace(/\s+/g, ' ')
  )
))()"""

# Tiny CDP client driven by node (WebSocket is a global in modern node).
BROWSER_ATTACH_JS = r"""
const args = process.argv.slice(1);
// ``node -e <script> <url> <endpoint>`` puts the first argument at index 1,
// so detect the two arguments by their shape instead of their position.
const TARGET_URL = args.find((a) => /youtubetotranscript\.com/i.test(a)) || '';
const CDP_HTTP = String(
  args.find(
    (a) => /^https?:\/\/127\.0\.0\.1:\d+/.test(a) && !/youtubetotranscript/i.test(a)
  ) || 'http://127.0.0.1:9222'
).replace(/\/+$/, '');
const TIMEOUT_MS = Number(process.env.TIMEOUT_MS || 120000);
const POLL_MS = 1500;
const CHALLENGE_RE = /just a moment|security verification|attention required|verify you are human|checking your browser/i;
const sleep = (ms) => new Promise((resolve) => setTimeout(resolve, ms));

async function json(path, method) {
  const response = await fetch(CDP_HTTP + path, { method: method || 'GET' });
  return response.json();
}

function connect(wsUrl) {
  return new Promise((resolve, reject) => {
    const ws = new WebSocket(wsUrl);
    let id = 0;
    const pending = new Map();
    ws.onopen = () => resolve({
      send(method, params = {}) {
        return new Promise((onOk, onError) => {
          const msgId = ++id;
          pending.set(msgId, { onOk, onError });
          ws.send(JSON.stringify({ id: msgId, method, params }));
        });
      },
      close() { try { ws.close(); } catch (error) {} },
    });
    ws.onerror = () => reject(new Error('websocket error'));
    ws.onmessage = (event) => {
      const msg = JSON.parse(event.data);
      if (msg.id && pending.has(msg.id)) {
        const { onOk, onError } = pending.get(msg.id);
        pending.delete(msg.id);
        if (msg.error) onError(new Error(JSON.stringify(msg.error)));
        else onOk(msg.result);
      }
    };
  });
}

(async () => {
  let page = null;
  const wantTab = (list) => (list || []).find(
    (target) => target.type === 'page'
      && /youtubetotranscript\.com/i.test(target.url || '')
  );
  for (let attempts = 0; attempts < 60 && !page; attempts += 1) {
    const list = await json('/json/list').catch(() => null);
    page = wantTab(list);
    if (!page && (list || []).some((target) => target.type === 'page')) {
      // The browser is up but its tab is still blank; reuse the first one.
      page = (list || []).find((target) => target.type === 'page');
      break;
    }
    if (!page) await sleep(500);
  }
  if (!page) { console.error('browser has no page target'); process.exit(1); }

  const cdp = await connect(page.webSocketDebuggerUrl);
  await cdp.send('Page.enable');
  if (!/youtubetotranscript\.com/i.test(page.url || '')) {
    const nav = await cdp.send('Page.navigate', { url: TARGET_URL });
    if (nav && nav.errorText) {
      console.error(`navigate failed: ${nav.errorText}`);
      process.exit(1);
    }
  }
  await cdp.send('Page.bringToFront').catch(() => {});

  const started = Date.now();
  let last = null;
  let lastClickAt = 0;
  while (Date.now() - started < TIMEOUT_MS) {
    await sleep(POLL_MS);
    const state = await cdp.send('Runtime.evaluate', {
      expression: process.env.STATE_JS,
      returnByValue: true,
    }).catch(() => null);
    const raw = state && state.result && state.result.value;
    if (!raw) continue;
    let info;
    try { info = JSON.parse(raw); } catch (error) { continue; }
    last = info;
    const challenged = CHALLENGE_RE.test(info.title || '');

    if (!challenged && Number(info.segCount) > 0) {
      const extracted = await cdp.send('Runtime.evaluate', {
        expression: process.env.EXTRACT_JS,
        returnByValue: true,
      });
      let segments;
      try { segments = JSON.parse(extracted.result.value); }
      catch (error) { console.error('could not parse transcript segments'); process.exit(1); }
      const text = segments
        .map((part) => String(part).replace(/\s+/g, ' ')
          .replace(/^\d{1,2}:\d{2}(?::\d{2})?\s*/, '').trim())
        .filter(Boolean)
        .join(' ')
        .trim();
      process.stdout.write(JSON.stringify({
        transcript: text,
        title: info.title,
        videoId: info.videoId,
        langs: info.langs,
      }));
      process.exit(0);
    }

    if (!challenged && info.returned === 'False') {
      console.error('the site reports this video has no transcript');
      process.exit(3);
    }

    if (challenged && Date.now() - lastClickAt > 8000) {
      lastClickAt = Date.now();
      // The site may show its interactive "Verify you are human" checkbox;
      // try to click it, and leave the window in the foreground so a user
      // can click it themselves when the synthesized click is rejected.
      const box = await cdp.send('Runtime.evaluate', {
        expression:
          '(() => { const widget = document.querySelector(\'input[id^="cf-chl-widget-"]\'); if (!widget) return null; let el = widget.parentElement; for (let i = 0; i < 4 && el; i++) { const b = el.getBoundingClientRect(); if (b.width > 100 && b.height > 40) return JSON.stringify({x: b.x, y: b.y, w: b.width, h: b.height}); el = el.parentElement; } return null; })()',
        returnByValue: true,
      }).catch(() => null);
      const widgetBox = box && box.result && box.result.value;
      if (widgetBox) {
        try {
          const rect = JSON.parse(widgetBox);
          const cx = Math.round(rect.x + (rect.w < 320 ? 22 : rect.w / 2));
          const cy = Math.round(rect.y + rect.h / 2);
          for (const type of ['mouseMoved', 'mousePressed', 'mouseReleased']) {
            await cdp.send('Input.dispatchMouseEvent', {
              type, x: cx, y: cy, button: 'left', clickCount: 1,
            }).catch(() => {});
          }
          if (!process.env.YTT_CLICK_NOTED) {
            process.env.YTT_CLICK_NOTED = '1';
            process.stderr.write(
              'the site asks for a browser verification; trying to click the '
              + 'checkbox automatically, or click it yourself in the Chrome '
              + 'window (needed only once)\n'
            );
          }
        } catch (error) {}
      }
    }
  }
  console.error(`browser fetch timed out; last state: ${JSON.stringify(last)}`);
  process.exit(1);
})().catch((error) => {
  console.error(String((error && error.message) || error));
  process.exit(1);
});
"""


def _log(message: str) -> None:
    print(f"[transcript] {message}", file=sys.stderr, flush=True)


def _short_error(exc: BaseException, limit: int = 500) -> str:
    """Flatten a multi-line error into one readable line."""
    text = " ".join(str(exc).split())
    return text[:limit]


def _classify_error(exc: BaseException) -> str:
    text = f"{type(exc).__name__}: {exc}".lower()
    if any(hint in text for hint in SITE_NO_TRANSCRIPT_MARKERS):
        return "no_transcript"
    if any(hint in text for hint in SITE_BLOCK_MARKERS) or "blocked" in text:
        return "blocked"
    return "transient"


def _storage_id(video_id: str) -> str:
    return f"{YOUTUBE_PREFIX}{video_id}"


def _transcript_path(video_id: str) -> Path:
    return OUTPUT_DIR / f"{_storage_id(video_id)}.transcript"


def _title_path(video_id: str) -> Path:
    return OUTPUT_DIR / f"{_storage_id(video_id)}.title"


def _site_page_title(content: str) -> str:
    match = _SITE_TITLE_RE.search(content or "")
    return " ".join(match.group(1).split()) if match else ""


def _clean_site_fragment(fragment: str) -> str:
    text = re.sub(r"<[^>]+>", " ", fragment)
    text = re.sub(r"&nbsp;", " ", text, flags=re.IGNORECASE)
    return " ".join(html.unescape(text).split())


def _parse_transcript_site_html(content: str) -> str | None:
    """Read the caption spans out of a third-party transcript page.

    The page carries one ``<span class="transcript-segment">`` per caption
    piece.  Returns None when the page holds no transcript (for example the
    verification page, or a video without captions).
    """
    text = str(content or "")
    if not text.strip():
        return None
    segments = [
        cleaned
        for cleaned in (
            _clean_site_fragment(fragment)
            for fragment in _SITE_SEGMENT_RE.findall(text)
        )
        if cleaned
    ]
    transcript = " ".join(segments).strip()
    if len(transcript) < 40:
        return None
    return transcript


def _parse_transcript_site_page(content: str) -> str | None:
    """Extract the transcript body from a third-party transcript page.

    Accepts either the server-rendered HTML or the markdown rendering of the
    page (a "## Transcript" heading followed by the caption paragraphs, then
    site chrome that must be trimmed).  Returns None when the page carries no
    transcript.
    """
    text = str(content or "")
    lowered = text.lower()
    if any(marker in lowered for marker in SITE_BLOCK_MARKERS):
        raise RuntimeError("third-party transcript site returned its verification page")
    transcript = _parse_transcript_site_html(text)
    if transcript is not None:
        return transcript
    if "## transcript" not in lowered:
        return None
    body = text[lowered.index("## transcript") + len("## transcript") :]
    for marker in ("## AI Features", "Links found:"):
        index = body.find(marker)
        if index >= 0:
            body = body[:index]
    transcript = " ".join(body.split()).strip()
    # A page with the heading but no caption body means the video has no
    # captions; a very short body means the scrape caught a partial page.
    if len(transcript) < 40:
        return None
    return transcript


def _fetch_site_with_direct_request(video_id: str) -> str:
    """Read the transcript page straight from the third-party site."""
    if requests is None:
        raise RuntimeError("requests is not installed")
    headers = {
        "User-Agent": (
            "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
            "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/131.0.0.0 Safari/537.36"
        ),
        "Accept": (
            "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8"
        ),
        "Accept-Language": "en-US,en;q=0.9",
    }
    try:
        response = requests.get(
            TRANSCRIPT_SITE_URL.format(video_id=video_id),
            headers=headers,
            timeout=(10, 60),
        )
    except requests.RequestException as exc:
        raise RuntimeError(f"direct request failed: {exc}") from exc
    if response.status_code in (403, 429, 503):
        raise RuntimeError(
            f"direct request was declined (HTTP {response.status_code})"
        )
    response.raise_for_status()
    transcript = _parse_transcript_site_page(response.text)
    if transcript is None:
        raise RuntimeError("direct request returned no transcript")
    return transcript


def _ollama_cloud_key() -> str | None:
    """Return the Ollama API key used for server-side web fetching, if any."""
    return (
        os.environ.get("OLLAMA_CLOUD_API_KEY")
        or os.environ.get("OLLAMA_API_KEY")
        or None
    )


def _fetch_site_with_webfetch(video_id: str) -> str:
    """Read the transcript page through Ollama's server-side web fetch.

    Ollama downloads the page on its own infrastructure, which succeeds
    whenever the site lets Ollama's reader through its verification check.
    """
    if requests is None:
        raise RuntimeError("requests is not installed")
    key = _ollama_cloud_key()
    if not key:
        raise RuntimeError(
            "server-side web fetch needs OLLAMA_CLOUD_API_KEY "
            "(or OLLAMA_API_KEY) to be set"
        )
    try:
        response = requests.post(
            OLLAMA_WEB_FETCH_URL,
            headers={
                "Authorization": f"Bearer {key}",
                "Content-Type": "application/json",
            },
            json={"url": TRANSCRIPT_SITE_URL.format(video_id=video_id)},
            timeout=OLLAMA_WEB_FETCH_TIMEOUT,
        )
        response.raise_for_status()
    except requests.RequestException as exc:
        raise RuntimeError(f"Ollama web fetch failed: {exc}") from exc
    content = str(response.json().get("content") or "")
    transcript = _parse_transcript_site_page(content)
    if transcript is None:
        raise RuntimeError(
            f"the site served no transcript for {video_id} through web fetch"
        )
    return transcript.strip()


_BROWSER_CANDIDATES = {
    "darwin": (
        "/Applications/Google Chrome.app/Contents/MacOS/Google Chrome",
        "/Applications/Chromium.app/Contents/MacOS/Chromium",
        "/Applications/Microsoft Edge.app/Contents/MacOS/Microsoft Edge",
        "/Applications/Brave Browser.app/Contents/MacOS/Brave Browser",
    ),
    "win32": (
        r"C:\Program Files\Google\Chrome\Application\chrome.exe",
        r"C:\Program Files (x86)\Google\Chrome\Application\chrome.exe",
        r"C:\Program Files (x86)\Microsoft\Edge\Application\msedge.exe",
    ),
    "linux": (
        "google-chrome-stable",
        "google-chrome",
        "chromium-browser",
        "chromium",
        "microsoft-edge",
        "brave-browser",
    ),
}


def _find_browser_executable() -> str:
    """Locate a Chromium-based browser executable for the CDP transport."""
    override = os.environ.get("YT_BROWSER_PATH")
    if override:
        if Path(override).exists():
            return override
        which = shutil.which(override)
        if which:
            return which
        raise RuntimeError(f"YT_BROWSER_PATH={override!r} does not exist")
    candidates = _BROWSER_CANDIDATES.get(sys.platform, _BROWSER_CANDIDATES["linux"])
    for candidate in candidates:
        if "/" in candidate or "\\" in candidate:
            if Path(candidate).exists():
                return candidate
            continue
        which = shutil.which(candidate)
        if which:
            return which
    raise RuntimeError(
        "no Chrome-compatible browser was found for the CDP transport "
        "(set YT_BROWSER_PATH to the browser executable)"
    )


class _ChromeDevTools:
    """Run one headful Chrome instance backed by a persistent profile.

    The profile directory is reused across runs so the browser keeps the
    cookies the transcript site hands out once its verification is solved;
    after that first solve the site answers directly without a challenge.
    Chrome writes the active DevTools port to ``DevToolsActivePort`` inside
    the profile, which also covers relaunching into an already-running
    instance.
    """

    def __init__(self, profile_dir: Path) -> None:
        self.executable = _find_browser_executable()
        self.profile_dir = profile_dir
        self._stderr_lines: list[str] = []
        self._devtools_port: int | None = None
        self._found = threading.Event()
        self._process: subprocess.Popen | None = None

    def _pump_stderr(self) -> None:
        process = self._process
        if process is None or process.stderr is None:
            return
        try:
            for line in process.stderr:
                text = line.decode("utf-8", errors="replace")
                self._stderr_lines.append(text)
                match = re.search(
                    r"DevTools listening on \S*127\.0\.0\.1:(\d+)/", text
                )
                if match:
                    self._devtools_port = int(match.group(1))
                    self._found.set()
        except (ValueError, OSError):
            # The pipe was closed while the process was being shut down.
            return

    def _wait_for_devtools_port(self, deadline: float) -> None:
        """Find the DevTools port from stderr or the profile's port file."""
        marker = self.profile_dir / "DevToolsActivePort"
        while time.time() < deadline:
            if self._found.is_set():
                return
            try:
                lines = marker.read_text(encoding="utf-8").split()
                port = int(lines[0])
                if port > 0:
                    self._devtools_port = port
                    self._found.set()
                    return
            except (OSError, ValueError, IndexError):
                pass
            time.sleep(0.3)

    def __enter__(self) -> "_ChromeDevTools":
        self.profile_dir.mkdir(parents=True, exist_ok=True)
        marker = self.profile_dir / "DevToolsActivePort"
        marker.unlink(missing_ok=True)
        port = os.environ.get("YT_CDP_PORT")
        argv = [
            self.executable,
            (
                f"--remote-debugging-port={int(port)}"
                if port
                else "--remote-debugging-port=0"
            ),
            "--no-first-run",
            "--no-default-browser-check",
            "--no-crash-upload",
            "--window-size=1180,860",
            f"--user-data-dir={self.profile_dir}",
        ]
        self._process = subprocess.Popen(
            argv,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.PIPE,
        )
        reader = threading.Thread(target=self._pump_stderr, daemon=True)
        reader.start()
        self._wait_for_devtools_port(time.time() + 30)
        if not self._found.is_set():
            self.close()
            detail = " ".join(
                line for line in self._stderr_lines[-3:] if line.strip()
            )
            raise RuntimeError(
                "Chrome did not expose a DevTools endpoint "
                f"({_short_error(RuntimeError(detail), 240)})"
            )
        return self

    def __exit__(self, *exc_info) -> None:
        self.close()

    @property
    def http_endpoint(self) -> str:
        return f"http://127.0.0.1:{self._devtools_port}"

    def close(self) -> None:
        process = self._process
        if process is None:
            return
        try:
            if process.poll() is None:
                process.terminate()
                try:
                    process.wait(timeout=5)
                except subprocess.TimeoutExpired:
                    process.kill()
                    process.wait(timeout=5)
        except OSError:
            pass
        finally:
            if process.stderr:
                process.stderr.close()
            self._process = None


def _browser_profile_dir() -> Path:
    """Persistent profile so solved verifications are remembered."""
    override = os.environ.get("YT_BROWSER_PROFILE_DIR")
    if override:
        return Path(override).expanduser()
    # Next to the article cache, not inside it, so cache scans never see the
    # browser profile files.
    return OUTPUT_DIR.parent / "browser_profile_youtubetotranscript"


def _fetch_site_with_browser(video_id: str) -> str:
    """Read the transcript page with the local Chrome browser.

    The headful browser solves the third-party site's verification check the
    same way a user's browser does.  The persistent profile remembers the
    verification cookie, so the very first run may ask for one checkbox
    click while every later run passes without a challenge.  Node drives
    Chrome through the DevTools Protocol.
    """
    node = shutil.which("node")
    if node is None:
        raise RuntimeError(
            "the browser transport needs node (https://nodejs.org) on PATH"
        )
    if requests is None:
        raise RuntimeError("requests is not installed")
    launch_url = TRANSCRIPT_SITE_URL.format(video_id=video_id)
    with _ChromeDevTools(_browser_profile_dir()) as devtools:
        # Wait for the browser's HTTP endpoint before handing over to node.
        deadline = time.time() + 20
        while time.time() < deadline:
            try:
                requests.get(devtools.http_endpoint + "/json/version", timeout=2)
                break
            except requests.RequestException:
                time.sleep(0.4)
        else:
            raise RuntimeError("the Chrome DevTools endpoint never came up")
        try:
            process = subprocess.run(
                [
                    node,
                    "-e",
                    BROWSER_ATTACH_JS,
                    launch_url,
                    devtools.http_endpoint,
                ],
                capture_output=True,
                text=True,
                timeout=BROWSER_FETCH_TIMEOUT + 30,
                env={
                    **os.environ,
                    "TIMEOUT_MS": str(int(BROWSER_FETCH_TIMEOUT * 1000)),
                    "STATE_JS": BROWSER_STATE_JS,
                    "EXTRACT_JS": BROWSER_EXTRACT_JS,
                },
            )
        except subprocess.TimeoutExpired as exc:
            raise RuntimeError(
                f"browser transport timed out after {BROWSER_FETCH_TIMEOUT:.0f}s"
            ) from exc
    if process.returncode == 0:
        try:
            payload = json.loads(process.stdout)
        except ValueError as exc:
            raise RuntimeError(
                f"browser transport returned unparsable data: {_short_error(exc)}"
            ) from exc
        transcript = str(payload.get("transcript") or "").strip()
        if len(transcript) < 40:
            raise RuntimeError("browser transport returned an empty transcript")
        return transcript
    if process.returncode == 3:
        raise RuntimeError(
            "the third-party transcript site reports this video has no "
            f"transcript ({_short_error(RuntimeError(process.stderr or ''), 160)})"
        )
    detail = " ".join((process.stderr or "").split())
    raise RuntimeError(
        detail[-360:] if detail else f"browser transport failed (exit {process.returncode})"
    )


# Transports that read the third-party transcript page, tried in order.
SITE_TRANSPORTS = (
    ("direct request", _fetch_site_with_direct_request),
    ("Ollama server-side web fetch", _fetch_site_with_webfetch),
    ("local Chrome (DevTools Protocol)", _fetch_site_with_browser),
)


def fetch_transcript_from_site(video_id: str) -> str:
    """Fetch the transcript through the third-party transcript site.

    Every transport reads the same page; they differ only in how the page is
    reached past the site's client verification.  Raises when no transport
    succeeds; the error lists what each transport reported.
    """
    errors: list[str] = []
    for label, transport in SITE_TRANSPORTS:
        try:
            text = transport(video_id)
            if text.strip():
                _log(f"fetched transcript from {label}")
                return text.strip()
            raise RuntimeError("the transport returned an empty transcript")
        except Exception as exc:
            errors.append(f"{label}: {_short_error(exc, 300)}")
            _log(
                f"{label} failed ({_classify_error(exc)}); "
                "trying the next transport"
            )
    raise RuntimeError(
        f"Could not read the transcript page on the third-party transcript "
        f"site for {video_id}. Try: make sure OLLAMA_CLOUD_API_KEY is set "
        "for the server-side web fetch, and that node plus Google Chrome "
        f"are installed for the local-browser transport. Details: "
        f"{'; '.join(errors)}"
    )


def _chunk_text(text: str, max_chars: int = 1600) -> list[str]:
    """Split a transcript into translation-sized sentence chunks."""
    text = " ".join(str(text or "").split())
    if not text:
        return []
    pieces = re.split(r"(?<=[。！？.!?])\s*", text)
    chunks: list[str] = []
    current = ""
    for piece in pieces:
        piece = piece.strip()
        if not piece:
            continue
        while len(piece) > max_chars:
            if current:
                chunks.append(current)
                current = ""
            chunks.append(piece[:max_chars].strip())
            piece = piece[max_chars:].strip()
        if not piece:
            continue
        if current and len(current) + 1 + len(piece) <= max_chars:
            current = f"{current} {piece}"
        else:
            if current:
                chunks.append(current)
            current = piece
    if current:
        chunks.append(current)
    return chunks


def _translate_segment(segment: str) -> str:
    """Translate one transcript segment into English with the local model."""
    if requests is None:
        raise RuntimeError(
            "requests is not installed; cannot call Ollama for translation"
        )
    prompt = (
        "Translate the following YouTube transcript segment into natural "
        "English. Translate every sentence completely. Do not summarize, "
        "shorten, explain, or add commentary. Preserve names, numbers, and "
        "examples. Return only the English translation.\n\n"
        f"<segment>\n{segment}\n</segment>\n"
    )
    payload = {
        "model": MODEL,
        "prompt": prompt,
        "stream": False,
        "think": False,
        "options": {
            "temperature": 0.1,
            "num_predict": 2600,
            "num_ctx": 16384,
        },
    }
    try:
        response = requests.post(
            f"{OLLAMA_HOST}/api/generate",
            json=payload,
            timeout=(10, 900),
        )
        response.raise_for_status()
    except requests.RequestException as exc:
        raise RuntimeError(
            f"Could not reach Ollama at {OLLAMA_HOST} for transcript "
            f"translation: {exc}"
        ) from exc
    translation = str(response.json().get("response") or "").strip()
    translation = re.sub(
        r"^(?:translation|english)\s*:\s*",
        "",
        translation,
        flags=re.IGNORECASE,
    ).strip()
    translation = translation.strip("\"'“”").strip()
    if not translation:
        raise RuntimeError("The translation model returned an empty segment")
    return translation


def _translate_transcript(text: str, language_name: str) -> str:
    """Translate a non-English transcript into English, segment by segment."""
    chunks = _chunk_text(text)
    if not chunks:
        raise RuntimeError("The source transcript is empty")
    _log(
        f"no English transcript; translating {language_name or 'the original'} "
        f"transcript in {len(chunks)} segment(s)"
    )
    translated: list[str] = []
    for index, chunk in enumerate(chunks, start=1):
        _log(f"translating segment {index}/{len(chunks)}…")
        translated.append(_translate_segment(chunk))
    result = " ".join(translated).strip()
    if not result:
        raise RuntimeError("Transcript translation produced no text")
    return result


def _is_english_language(code: str) -> bool:
    """Return True for English language codes, including en-xx variants."""
    normalized = str(code or "").strip().lower().replace("_", "-")
    return normalized == "en" or normalized.startswith("en-")


def _looks_non_english(text: str) -> bool:
    """Detect a cached transcript that is mostly a non-Latin script."""
    sample = str(text or "")[:6000]
    latin = sum(
        1 for char in sample if "a" <= char.lower() <= "z"
    )
    non_latin = sum(
        1
        for char in sample
        if char.isalpha() and ord(char) > 0x2FF
    )
    return non_latin > 20 and non_latin > latin * 0.2


def fetch_transcript(video_id: str, use_cache: bool = True) -> str:
    """Return the video's English transcript as plain text.

    A previously downloaded transcript is reused so retries and the learn
    pipeline are idempotent and do not hit the transcript site again.

    When the third-party site returns a non-English transcript, the
    original-language transcript is translated locally with Ollama. This
    handles videos whose only English option is a translated caption track.
    """
    if use_cache:
        cached = _transcript_path(video_id)
        if cached.is_file():
            try:
                text = cached.read_text(encoding="utf-8").strip()
            except OSError:
                text = ""
            if text:
                if _looks_non_english(text):
                    _log(
                        "cached transcript is not English; translating it "
                        "to English"
                    )
                    translated = _translate_transcript(
                        text, "cached"
                    )
                    try:
                        cached.write_text(
                            translated + "\n", encoding="utf-8"
                        )
                    except OSError:
                        pass
                    return translated
                _log("using cached transcript")
                return text

    transcript = fetch_transcript_from_site(video_id)
    if _looks_non_english(transcript):
        _log(
            "the third-party transcript site returned a non-English "
            "transcript; translating it to English"
        )
        return _translate_transcript(transcript, "third-party transcript site")
    return transcript


def summarize(transcript: str) -> str:
    """Ask the local Ollama model for a 20-word summary."""
    if requests is None:
        raise RuntimeError(
            "requests is not installed; cannot call Ollama for the title"
        )

    prompt = (
        "Summarize the following YouTube transcript in exactly 20 words. "
        "Return only the summary, with no quotes, labels, or explanation.\n\n"
        f"Transcript:\n{transcript}"
    )
    payload = {
        "model": MODEL,
        "prompt": prompt,
        "stream": False,
        "think": False,
        "options": {"temperature": 0.2, "num_predict": 64},
    }

    try:
        response = requests.post(
            f"{OLLAMA_HOST}/api/generate", json=payload, timeout=600
        )
        response.raise_for_status()
    except requests.RequestException as exc:
        raise RuntimeError(
            f"Could not reach Ollama at {OLLAMA_HOST}. Is it running? ({exc})"
        ) from exc

    summary = response.json().get("response", "").strip()
    summary = re.sub(r"^(summary|title)\s*:\s*", "", summary, flags=re.IGNORECASE)
    summary = summary.strip().strip("\"'“”").strip()
    summary = re.sub(r"\s+", " ", summary)

    # Enforce the 20-word limit even if the model overshoots.
    words = summary.split()
    if len(words) > 20:
        summary = " ".join(words[:20])

    if not summary:
        raise RuntimeError("Ollama returned an empty summary")
    return summary


def main() -> None:
    parser = argparse.ArgumentParser(
        description=(
            "Fetch a YouTube transcript from the third-party transcript site "
            "and summarize it into 20 words."
        )
    )
    parser.add_argument("youtube_video_hash", help="YouTube video ID, e.g. FTwxtyOEGyM")
    parser.add_argument(
        "--force",
        action="store_true",
        help="ignore an already cached .transcript file and fetch again",
    )
    parser.add_argument(
        "--reuse-transcript",
        action="store_true",
        help="use the cached transcript without fetching or rewriting it "
        "(used by the relearn pipeline)",
    )
    args = parser.parse_args(normalize_argv(sys.argv[1:]))

    video_id = args.youtube_video_hash.strip()
    if not re.fullmatch(r"[A-Za-z0-9_-]+", video_id):
        sys.exit(f"Invalid YouTube video hash: {video_id!r}")

    if args.reuse_transcript:
        if args.force:
            sys.exit(
                "Error: --reuse-transcript and --force cannot be used together"
            )
        transcript_path = _transcript_path(video_id)
        try:
            transcript = transcript_path.read_text(encoding="utf-8").strip()
        except OSError as exc:
            sys.exit(
                f"Error: --reuse-transcript requires the saved transcript at "
                f"{transcript_path} ({exc})"
            )
        if not transcript:
            sys.exit(
                f"Error: the saved transcript at {transcript_path} is empty"
            )
        print(f"Using existing transcript without modifying it: {transcript_path}")
    else:
        try:
            transcript = fetch_transcript(video_id, use_cache=not args.force)
        except Exception as exc:
            sys.exit(f"Error: {exc}")

        OUTPUT_DIR.mkdir(parents=True, exist_ok=True)
        transcript_path = _transcript_path(video_id)
        transcript_path.write_text(transcript + "\n", encoding="utf-8")
        print(f"Saved transcript: {transcript_path}")

    title_path = _title_path(video_id)

    # Reuse an existing title when re-running an already-learned video; this
    # avoids a pointless model call and keeps repeated runs fast.
    if not args.force and title_path.is_file():
        try:
            cached_summary = title_path.read_text(encoding="utf-8").strip()
        except OSError:
            cached_summary = ""
        if cached_summary:
            print(f"Title: {cached_summary}")
            return

    # Save the transcript before calling Ollama: a model/connection failure
    # must not throw away a successfully fetched transcript.
    try:
        summary = summarize(transcript)
    except Exception as exc:
        sys.exit(
            f"Error: transcript saved to {transcript_path}, but the title "
            f"summary failed: {exc}"
        )

    title_path.write_text(summary + "\n", encoding="utf-8")
    print(f"Saved title:      {title_path}")
    print(f"Title: {summary}")


if __name__ == "__main__":
    main()
