#!/usr/bin/env python3
"""Fetch an English YouTube transcript and summarize it with Ollama.

Usage:
    python3 fetch_youtube_transcript.py <youtube_video_hash>
    python3 fetch_youtube_transcript.py --force <youtube_video_hash>

Outputs:
    ./.chat/articles_cache/youtube_<youtube_video_hash>.transcript
    ./.chat/articles_cache/youtube_<youtube_video_hash>.title

Transcript providers, tried in order:
    1. youtube-transcript-api (fast when YouTube is not blocking this IP)
    2. yt-dlp (works through different player clients, so it still succeeds
       when the transcript API is IP-blocked)
    3. An original-language caption track translated to English with the
       local Ollama model. This handles videos whose only English option is
       a YouTube translated track (for example en-zh) that YouTube refuses to
       serve.

Optional environment variables:
    YT_PROXY_URL              http(s)/socks proxy for both providers
    WEBSHARE_PROXY_USERNAME   rotating-residential proxy credentials for
    WEBSHARE_PROXY_PASSWORD   youtube-transcript-api
    YT_COOKIES_FROM_BROWSER   e.g. "chrome" for the yt-dlp fallback
    YT_COOKIES_FILE           path to a Netscape cookies.txt for yt-dlp
    OLLAMA_YOUTUBE_MODEL      title-summary model (default: ornith-1.5:35b)
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


def _storage_id(video_id: str) -> str:
    return f"{YOUTUBE_PREFIX}{video_id}"


def _transcript_path(video_id: str) -> Path:
    return OUTPUT_DIR / f"{_storage_id(video_id)}.transcript"


def _title_path(video_id: str) -> Path:
    return OUTPUT_DIR / f"{_storage_id(video_id)}.title"


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

def _fetch_with_api_any(video_id: str) -> tuple[str, str, str]:
    """Fetch any available transcript, preferring English.

    Returns ``(text, language_code, language_name)``.  When the video has no
    English track, the caller translates the original-language transcript.
    """
    if YouTubeTranscriptApi is None:
        raise RuntimeError("youtube-transcript-api is not installed")

    tries = 3 if _proxy_config() is not None else 1
    last_error: BaseException | None = None

    for attempt in range(tries):
        try:
            api = _make_api()
            transcripts = list(api.list(video_id))
            if not transcripts:
                raise RuntimeError(f"No transcripts are available for {video_id}")

            english = [
                transcript
                for transcript in transcripts
                if str(transcript.language_code).lower().startswith("en")
            ]
            ordered = english + [
                transcript
                for transcript in transcripts
                if transcript not in english
            ]
            chosen = None
            fetched = None
            for transcript in ordered:
                try:
                    fetched = transcript.fetch()
                except Exception as exc:
                    last_error = exc
                    continue
                chosen = transcript
                break
            if chosen is None or fetched is None:
                if last_error is not None:
                    raise last_error
                raise RuntimeError(f"Could not fetch a transcript for {video_id}")

            text = " ".join(
                " ".join(snippet.text.split()) for snippet in fetched
            )
            if not text.strip():
                raise RuntimeError(f"Transcript for {video_id} is empty")
            return (
                text.strip(),
                str(chosen.language_code),
                str(getattr(chosen, "language", "") or chosen.language_code),
            )
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


def _ytdlp_original_languages(video_id: str) -> list[str]:
    """Return original (non-translated) subtitle languages from yt-dlp."""
    command = _ytdlp_command()
    if command is None:
        return []
    argv = [
        *command,
        "--no-warnings",
        "--no-playlist",
        "--list-subs",
        f"https://www.youtube.com/watch?v={video_id}",
    ]
    try:
        result = subprocess.run(
            argv,
            capture_output=True,
            text=True,
            timeout=120,
        )
    except (subprocess.TimeoutExpired, OSError):
        return []
    languages: list[str] = []
    for line in (result.stdout or "").splitlines():
        if " from " in line.casefold():
            # Translated tracks such as "en-zh English from Chinese" are
            # derived from an original track; translate the original locally
            # instead of relying on YouTube's translated caption endpoint.
            continue
        match = re.match(r"^([A-Za-z0-9_-]+)\s{2,}\S", line)
        if not match:
            continue
        code = match.group(1)
        if code.casefold() in {
            "language",
            "available",
            "automatic",
            "subtitles",
            "format",
        }:
            continue
        if code not in languages:
            languages.append(code)
    return languages


def _fetch_with_ytdlp_languages(
    video_id: str,
    sub_langs: str,
    *,
    clients: tuple[str, ...] | None = None,
    label: str = "English",
) -> str:
    """Fetch captions for one language pattern with yt-dlp."""
    command = _ytdlp_command()
    if command is None:
        raise RuntimeError("yt-dlp is not installed")

    last_error = f"no {label} captions were found"
    proxy = os.environ.get("YT_PROXY_URL")
    cookies_browser = os.environ.get("YT_COOKIES_FROM_BROWSER")
    cookies_file = os.environ.get("YT_COOKIES_FILE")
    for client in clients or YT_DLP_CLIENTS:
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
                sub_langs,
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
                _log(f"fetched {label} captions with yt-dlp (client={client})")
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

    raise RuntimeError(
        f"yt-dlp could not fetch a {label} transcript: {last_error}"
    )


def _fetch_with_ytdlp(video_id: str) -> str:
    """Fetch English captions with yt-dlp, trying alternate player clients."""
    return _fetch_with_ytdlp_languages(video_id, "en.*")


def _fetch_with_ytdlp_any(video_id: str) -> tuple[str, str, str]:
    """Fetch an original-language transcript when English is unavailable."""
    languages = _ytdlp_original_languages(video_id)
    if not languages:
        raise RuntimeError(
            "yt-dlp found no original-language subtitle tracks"
        )
    for language in languages:
        try:
            text = _fetch_with_ytdlp_languages(
                video_id,
                language,
                clients=("visionos", "android_vr", "default"),
                label=language,
            )
        except Exception as exc:
            _log(
                f"yt-dlp could not fetch {language} captions: "
                f"{_short_error(exc, 240)}"
            )
            continue
        if text.strip():
            return text.strip(), language, language
    raise RuntimeError(
        "yt-dlp could not download any original-language caption track"
    )


def fetch_transcript(video_id: str, use_cache: bool = True) -> str:
    """Return the video's English transcript as plain text.

    A previously downloaded transcript is reused so retries and the learn
    pipeline are idempotent and do not hit YouTube again.

    When the video has no English caption track, the original-language
    transcript is fetched and translated locally with Ollama. This handles
    videos whose only English option is a YouTube translated track (for
    example ``en-zh``) that YouTube's caption endpoint refuses to serve.
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

    errors: list[str] = []

    try:
        text, language_code, language_name = _fetch_with_api_any(video_id)
        if _is_english_language(language_code):
            _log(
                "fetched English transcript with youtube-transcript-api "
                f"({language_code})"
            )
            return text
        _log(
            f"youtube-transcript-api returned {language_name or language_code}; "
            "translating it to English"
        )
        return _translate_transcript(
            text, language_name or language_code
        )
    except Exception as exc:
        errors.append(
            f"youtube-transcript-api: {_classify_error(exc)} "
            f"({_short_error(exc, 300)})"
        )
        _log(
            f"youtube-transcript-api failed ({_classify_error(exc)}); "
            "trying yt-dlp"
        )

    try:
        text = _fetch_with_ytdlp(video_id)
        _log("fetched English transcript with yt-dlp")
        return text
    except Exception as exc:
        errors.append(f"yt-dlp English: {_short_error(exc, 360)}")
        _log(f"yt-dlp English failed ({_short_error(exc, 240)})")

    try:
        text, language_code, language_name = _fetch_with_ytdlp_any(
            video_id
        )
        if _is_english_language(language_code):
            _log(
                "fetched English transcript with yt-dlp "
                f"({language_code})"
            )
            return text
        _log(
            f"yt-dlp returned {language_name or language_code}; "
            "translating it to English"
        )
        return _translate_transcript(
            text, language_name or language_code
        )
    except Exception as exc:
        errors.append(
            f"yt-dlp original language: {_short_error(exc, 360)}"
        )
        _log(
            "yt-dlp original-language fallback failed "
            f"({_short_error(exc, 240)})"
        )

    raise RuntimeError(
        f"Could not fetch an English transcript for {video_id}. The "
        "pipeline also tried translating the original-language transcript "
        f"with the local {MODEL} model. YouTube may be blocking this "
        "network; set YT_PROXY_URL (or "
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
