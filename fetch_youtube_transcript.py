#!/usr/bin/env python3
"""Fetch an English YouTube transcript and summarize it with Ollama.

Usage:
    python3 fetch_youtube_transcript.py <youtube_video_hash>
    python3 fetch_youtube_transcript.py --force <youtube_video_hash>

Outputs:
    ./.chat/youtube_transcripts/<youtube_video_hash>.transcript
    ./.chat/youtube_transcripts/<youtube_video_hash>.title

Transcript providers, tried in order:
    1. youtube-transcript-api (fast when YouTube is not blocking this IP)
    2. yt-dlp (works through different player clients, so it still succeeds
       when the transcript API is IP-blocked)

Optional environment variables:
    YT_PROXY_URL              http(s)/socks proxy for both providers
    WEBSHARE_PROXY_USERNAME   rotating-residential proxy credentials for
    WEBSHARE_PROXY_PASSWORD   youtube-transcript-api
    YT_COOKIES_FROM_BROWSER   e.g. "chrome" for the yt-dlp fallback
    YT_COOKIES_FILE           path to a Netscape cookies.txt for yt-dlp
"""

import argparse
import html
import importlib.util
import json
import os
import random
import re
import shutil
import subprocess
import sys
import tempfile
import time
from pathlib import Path

try:
    import requests
except ImportError:  # pragma: no cover - dependency guard
    requests = None

try:
    from youtube_transcript_api import YouTubeTranscriptApi
    from youtube_transcript_api.proxies import (
        GenericProxyConfig,
        WebshareProxyConfig,
    )
except ImportError:  # pragma: no cover - yt-dlp may still be available
    YouTubeTranscriptApi = None
    GenericProxyConfig = None
    WebshareProxyConfig = None


def normalize_argv(argv):
    """Let positional values begin with '-' (e.g. video ID -PcBFgkrR2o).

    Recognized switches are moved in front of the positional value, so
    ``--force -PcBFgkrR2o`` and ``-- -PcBFgkrR2o`` both work.
    """
    known_switches = {"-h", "--help", "--force", "--"}
    switches = [arg for arg in argv if arg in known_switches and arg != "--"]
    positionals = [arg for arg in argv if arg not in known_switches]
    if any(arg.startswith("-") for arg in positionals):
        return [*switches, "--", *positionals]
    return [*switches, *positionals]


MODEL = "ornith:35b"
OUTPUT_DIR = Path.cwd() / ".chat" / "youtube_transcripts"

OLLAMA_HOST = os.environ.get("OLLAMA_HOST", "http://localhost:11434").rstrip("/")
if not OLLAMA_HOST.startswith(("http://", "https://")):
    OLLAMA_HOST = f"http://{OLLAMA_HOST}"

ENGLISH_LANGUAGES = ["en", "en-US", "en-GB"]

# YouTube rejects the transcript API from some residential/cloud IPs.  These
# player clients are handled differently by yt-dlp, so trying them in turn
# usually gets the captions even when the transcript API is blocked.
YT_DLP_CLIENTS = ("ios", "web_embedded", "mweb", "android_vr", "default")

BLOCK_HINTS = (
    "blocking requests from your ip",
    "too many requests",
    "http 429",
    "not a bot",
    "login_required",
    "captcha",
    "ipblocked",
    "requestblocked",
)

NO_TRANSCRIPT_HINTS = (
    "no english transcript",
    "no transcript",
    "transcriptsdisabled",
    "subtitles are disabled",
    "there are no subtitles",
)


def _log(message: str) -> None:
    print(f"[transcript] {message}", file=sys.stderr, flush=True)


def _short_error(exc: BaseException, limit: int = 500) -> str:
    """Flatten a multi-line library/yt-dlp error into one readable line."""
    text = " ".join(str(exc).split())
    return text[:limit]


def _classify_error(exc: BaseException) -> str:
    text = f"{type(exc).__name__}: {exc}".lower()
    if any(hint in text for hint in NO_TRANSCRIPT_HINTS):
        return "no_transcript"
    if any(hint in text for hint in BLOCK_HINTS):
        return "blocked"
    return "transient"


def _transcript_path(video_id: str) -> Path:
    return OUTPUT_DIR / f"{video_id}.transcript"


def _proxy_config():
    """Build a proxy config from environment variables, if configured."""
    if YouTubeTranscriptApi is None:
        return None

    username = os.environ.get("WEBSHARE_PROXY_USERNAME")
    password = os.environ.get("WEBSHARE_PROXY_PASSWORD")
    if username and password and WebshareProxyConfig is not None:
        return WebshareProxyConfig(
            proxy_username=username,
            proxy_password=password,
        )

    proxy_url = os.environ.get("YT_PROXY_URL")
    if proxy_url and GenericProxyConfig is not None:
        return GenericProxyConfig(http_url=proxy_url, https_url=proxy_url)

    return None


def _make_api():
    proxy_config = _proxy_config()
    if proxy_config is not None:
        return YouTubeTranscriptApi(proxy_config=proxy_config)
    return YouTubeTranscriptApi()


def _fetch_with_api(video_id: str) -> str:
    """Fetch with youtube-transcript-api, retrying only when a proxy is set.

    Without a proxy every retry leaves from the same blocked IP, so it only
    adds latency.  With a rotating proxy a fresh client per attempt can move
    to a different exit.
    """
    if YouTubeTranscriptApi is None:
        raise RuntimeError("youtube-transcript-api is not installed")

    tries = 3 if _proxy_config() is not None else 1
    last_error: BaseException | None = None

    for attempt in range(tries):
        try:
            api = _make_api()
            try:
                fetched = api.fetch(video_id, languages=ENGLISH_LANGUAGES)
            except Exception:
                # Fall back to any available English transcript (e.g.
                # auto-generated variants whose language code is not exactly
                # "en").
                available = list(api.list(video_id))
                english = [
                    transcript
                    for transcript in available
                    if transcript.language_code.lower().startswith("en")
                ]
                if not english:
                    raise RuntimeError(
                        f"No English transcript available for {video_id}"
                    )
                fetched = english[0].fetch()

            text = " ".join(
                " ".join(snippet.text.split()) for snippet in fetched
            )
            if not text.strip():
                raise RuntimeError(f"Transcript for {video_id} is empty")
            return text.strip()
        except Exception as exc:
            last_error = exc
            if attempt == tries - 1 or _classify_error(exc) != "blocked":
                raise
            delay = min(12.0, 2 ** attempt) + random.uniform(0.0, 1.5)
            _log(
                f"transcript API attempt {attempt + 1}/{tries} failed "
                f"({_classify_error(exc)}); retrying in {delay:.1f}s"
            )
            time.sleep(delay)

    assert last_error is not None
    raise last_error


def _ytdlp_command() -> list[str] | None:
    if importlib.util.find_spec("yt_dlp") is not None:
        return [sys.executable, "-m", "yt_dlp"]
    executable = shutil.which("yt-dlp")
    return [executable] if executable else None


def _parse_json3(payload: dict) -> str:
    """Convert YouTube's json3 caption format to plain text."""
    parts: list[str] = []
    for event in payload.get("events", []):
        segments = event.get("segs")
        if not segments:
            continue
        # aAppend events only mark a line break; the text lives in the
        # preceding event.
        if event.get("aAppend"):
            continue
        text = "".join(segment.get("utf8", "") for segment in segments)
        text = " ".join(text.split())
        if text:
            parts.append(text)
    return " ".join(parts)


def _parse_vtt(content: str) -> str:
    """Convert WebVTT captions to plain text (fallback for older yt-dlp)."""
    lines: list[str] = []
    for raw_line in content.splitlines():
        line = raw_line.strip()
        if not line:
            continue
        if line == "WEBVTT" or line.startswith(("Kind:", "Language:", "NOTE")):
            continue
        if "-->" in line or re.fullmatch(r"\d+", line):
            continue
        line = re.sub(r"<[^>]+>", "", line)
        line = html.unescape(line)
        line = " ".join(line.split())
        if not line:
            continue
        if lines and lines[-1] == line:
            continue
        lines.append(line)
    return " ".join(lines)


def _preferred(files: list[Path]) -> list[Path]:
    """Prefer the plain English track over en-orig/other variants."""
    return sorted(files, key=lambda path: (".en." not in path.name, path.name))


def _extract_subtitle_text(directory: Path) -> str:
    json3_files = _preferred(list(directory.glob("*.json3")))
    for path in json3_files:
        try:
            payload = json.loads(path.read_text(encoding="utf-8"))
            text = _parse_json3(payload)
        except (OSError, ValueError):
            continue
        if text.strip():
            return text.strip()

    vtt_files = _preferred(list(directory.glob("*.vtt")))
    for path in vtt_files:
        try:
            text = _parse_vtt(path.read_text(encoding="utf-8"))
        except OSError:
            continue
        if text.strip():
            return text.strip()

    return ""


def _fetch_with_ytdlp(video_id: str) -> str:
    """Fetch captions with yt-dlp, trying player clients that dodge IP blocks."""
    command = _ytdlp_command()
    if command is None:
        raise RuntimeError("yt-dlp is not installed")

    last_error = "no English captions were found"
    proxy = os.environ.get("YT_PROXY_URL")
    cookies_browser = os.environ.get("YT_COOKIES_FROM_BROWSER")
    cookies_file = os.environ.get("YT_COOKIES_FILE")

    for client in YT_DLP_CLIENTS:
        with tempfile.TemporaryDirectory(prefix="yt-transcript-") as tmp:
            output_template = str(Path(tmp) / "%(id)s.%(ext)s")
            argv = [
                *command,
                "--no-warnings",
                "--no-playlist",
                "--ignore-no-formats-error",
                "--skip-download",
                "--write-subs",
                "--write-auto-subs",
                "--sub-langs",
                "en.*",
                "--sub-format",
                "json3/vtt",
                "-o",
                output_template,
            ]
            if client != "default":
                argv.extend(
                    ["--extractor-args", f"youtube:player_client={client}"]
                )
            if proxy:
                argv.extend(["--proxy", proxy])
            if cookies_browser:
                argv.extend(["--cookies-from-browser", cookies_browser])
            if cookies_file:
                argv.extend(["--cookies", cookies_file])
            argv.append(f"https://www.youtube.com/watch?v={video_id}")

            try:
                result = subprocess.run(
                    argv,
                    capture_output=True,
                    text=True,
                    timeout=180,
                )
            except subprocess.TimeoutExpired:
                last_error = f"yt-dlp timed out (client={client})"
                continue
            except OSError as exc:
                raise RuntimeError(f"could not run yt-dlp: {exc}") from exc

            text = _extract_subtitle_text(Path(tmp))
            if text:
                _log(f"fetched with yt-dlp (client={client})")
                return text

            details = " ".join(
                (result.stderr or result.stdout or "").split()
            )
            last_error = (
                details[-500:]
                if details
                else f"yt-dlp returned no captions (client={client})"
            )
            time.sleep(random.uniform(0.2, 0.8))

    raise RuntimeError(f"yt-dlp could not fetch an English transcript: {last_error}")


def fetch_transcript(video_id: str, use_cache: bool = True) -> str:
    """Return the video's English transcript as plain text.

    A previously downloaded transcript is reused so retries and the learn
    pipeline are idempotent and do not hit YouTube again.
    """
    if use_cache:
        cached = _transcript_path(video_id)
        if cached.is_file():
            try:
                text = cached.read_text(encoding="utf-8").strip()
            except OSError:
                text = ""
            if text:
                _log("using cached transcript")
                return text

    errors: list[str] = []

    try:
        text = _fetch_with_api(video_id)
        _log("fetched with youtube-transcript-api")
        return text
    except Exception as exc:
        errors.append(
            f"youtube-transcript-api: {_classify_error(exc)} "
            f"({_short_error(exc, 240)})"
        )
        _log(
            f"youtube-transcript-api failed ({_classify_error(exc)}); "
            "trying yt-dlp"
        )

    try:
        text = _fetch_with_ytdlp(video_id)
        return text
    except Exception as exc:
        errors.append(f"yt-dlp: {_short_error(exc, 360)}")
        _log(f"yt-dlp failed ({_short_error(exc, 240)})")

    raise RuntimeError(
        f"Could not fetch an English transcript for {video_id}. "
        "YouTube may be blocking this network; set YT_PROXY_URL (or "
        "WEBSHARE_PROXY_USERNAME/WEBSHARE_PROXY_PASSWORD), or "
        "YT_COOKIES_FROM_BROWSER=chrome as a last resort, then retry. "
        f"Details: {'; '.join(errors)}"
    )


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
        description="Fetch a YouTube transcript and summarize it into 20 words."
    )
    parser.add_argument("youtube_video_hash", help="YouTube video ID, e.g. FTwxtyOEGyM")
    parser.add_argument(
        "--force",
        action="store_true",
        help="ignore an already cached .transcript file and fetch again",
    )
    args = parser.parse_args(normalize_argv(sys.argv[1:]))

    video_id = args.youtube_video_hash.strip()
    if not re.fullmatch(r"[A-Za-z0-9_-]+", video_id):
        sys.exit(f"Invalid YouTube video hash: {video_id!r}")

    try:
        transcript = fetch_transcript(video_id, use_cache=not args.force)
    except Exception as exc:
        sys.exit(f"Error: {exc}")

    OUTPUT_DIR.mkdir(parents=True, exist_ok=True)
    transcript_path = _transcript_path(video_id)
    title_path = OUTPUT_DIR / f"{video_id}.title"
    transcript_path.write_text(transcript + "\n", encoding="utf-8")
    print(f"Saved transcript: {transcript_path}")

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
