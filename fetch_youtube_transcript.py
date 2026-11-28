#!/usr/bin/env python3
"""Fetch an English YouTube transcript and summarize it with Ollama.

Usage:
    python3 fetch_youtube_transcript.py <youtube_video_hash>
    python3 fetch_youtube_transcript.py --force <youtube_video_hash>

Outputs:
    ./.chat/articles_cache/youtube_<youtube_video_hash>.transcript
    ./.chat/articles_cache/youtube_<youtube_video_hash>.title

The transcript source is NoteGPT's public YouTube transcript endpoint. It is
the only source this pipeline uses, per prompts/youtube.prompt. A fresh
anonymous UUID cookie is sent with each request because NoteGPT may accept
only one transcript per anonymous visitor. The response contains the video's
available caption tracks; the English track is preferred and a non-English
track is translated locally with Ollama.

Optional environment variables:
    OLLAMA_HOST               Ollama address (default: http://localhost:11434)
    OLLAMA_YOUTUBE_MODEL      title-summary / translation model
                              (default: ornith-1.5:35b)
"""

import argparse
import os
import re
import sys
import uuid
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

# Error hints from NoteGPT and YouTube. They let the caller distinguish a
# missing transcript from an IP/rate-limit refusal in the log message.
LIBRARY_NO_TRANSCRIPT_HINTS = (
    "no english transcript",
    "no transcript",
    "transcriptsdisabled",
    "subtitles are disabled",
    "there are no subtitles",
)

LIBRARY_BLOCK_HINTS = (
    "blocking requests from your ip",
    "too many requests",
    "http 429",
    "not a bot",
    "login_required",
    "captcha",
    "ipblocked",
    "requestblocked",
    "sign in to confirm",
    "bot check",
)


def _log(message: str) -> None:
    print(f"[transcript] {message}", file=sys.stderr, flush=True)


def _short_error(exc: BaseException, limit: int = 500) -> str:
    """Flatten a multi-line error into one readable line."""
    text = " ".join(str(exc).split())
    return text[:limit]


def _classify_error(exc: BaseException) -> str:
    text = f"{type(exc).__name__}: {exc}".lower()
    if any(hint in text for hint in LIBRARY_NO_TRANSCRIPT_HINTS):
        return "no_transcript"
    if any(hint in text for hint in LIBRARY_BLOCK_HINTS):
        return "blocked"
    if "blocked" in text:
        return "blocked"
    return "transient"


def _storage_id(video_id: str) -> str:
    return f"{YOUTUBE_PREFIX}{video_id}"


def _transcript_path(video_id: str) -> Path:
    return OUTPUT_DIR / f"{_storage_id(video_id)}.transcript"


def _title_path(video_id: str) -> Path:
    return OUTPUT_DIR / f"{_storage_id(video_id)}.title"


NOTEGPT_TRANSCRIPT_URL = (
    "https://notegpt.io/api/v2/video-transcript-v2"
)


def _notegpt_data(video_id: str) -> dict:
    """Request NoteGPT and return its validated ``data`` object."""
    if requests is None:
        raise RuntimeError(
            "requests is not installed; cannot call NoteGPT"
        )
    response = requests.get(
        NOTEGPT_TRANSCRIPT_URL,
        params={"platform": "youtube", "video_id": video_id},
        headers={
            "Accept": "application/json, text/plain, */*",
            "Origin": "https://notegpt.io",
            "Referer": "https://notegpt.io/youtube-transcript-generator",
            "User-Agent": (
                "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
                "AppleWebKit/537.36 (KHTML, like Gecko) "
                "Chrome/124.0 Safari/537.36"
            ),
        },
        cookies={"anonymous_user_id": str(uuid.uuid4())},
        timeout=(10, 90),
    )
    response.raise_for_status()
    try:
        payload = response.json()
    except ValueError as exc:
        raise RuntimeError(
            "NoteGPT returned a non-JSON response (likely a security checkpoint)"
        ) from exc
    data = payload.get("data") if isinstance(payload, dict) else None
    if not isinstance(data, dict) or str(payload.get("code")) != "100000":
        message = payload.get("message") if isinstance(payload, dict) else ""
        raise RuntimeError(
            "NoteGPT could not return a transcript"
            + (f": {message}" if message else "")
        )
    return data


def _notegpt_tracks(data: dict) -> tuple[str, dict, str, bool]:
    """Pick the English caption track and describe its language."""
    transcripts = data.get("transcripts")
    if not isinstance(transcripts, dict) or not transcripts:
        raise RuntimeError("NoteGPT returned no caption tracks")
    language_keys = list(transcripts)
    language_key = next(
        (
            key
            for key in language_keys
            if str(key).lower().replace("_", "-").startswith("en")
        ),
        language_keys[0],
    )
    tracks = transcripts.get(language_key)
    if not isinstance(tracks, dict):
        raise RuntimeError("NoteGPT returned an unusable caption track")
    language_name = ""
    for language in data.get("language_code") or []:
        if not isinstance(language, dict):
            continue
        code = str(language.get("code") or "")
        if code == language_key or (
            code.startswith("en") and language_key.startswith("en")
        ):
            language_name = str(language.get("name") or "")
            break
    non_english = not str(language_key).lower().replace(
        "_", "-"
    ).startswith("en")
    return (
        language_key,
        tracks,
        language_name or language_key,
        non_english,
    )


def _fetch_with_notegpt(video_id: str) -> tuple[str, str, bool]:
    """Fetch captions from NoteGPT, the only transcript source.

    NoteGPT serves the caption tracks that YouTube exposes, behind an
    anonymous UUID cookie. The returned tuple is
    ``(text, language_name, is_non_english)`` so a non-English track can go
    through the existing local translation step.
    """
    data = _notegpt_data(video_id)
    _language_key, tracks, language_name, non_english = _notegpt_tracks(data)
    for track_name in ("default", "auto", "custom"):
        segments = tracks.get(track_name)
        if not isinstance(segments, list) or not segments:
            continue
        text = " ".join(
            str(segment.get("text") or "").strip()
            for segment in segments
            if isinstance(segment, dict)
        )
        text = " ".join(text.split())
        if text:
            return text, language_name, non_english
    raise RuntimeError("NoteGPT returned empty transcript text")


def _timed_seconds(value) -> float | None:
    """Parse NoteGPT's ``HH:MM:SS`` / ``MM:SS`` timestamp into seconds."""
    text = str(value or "").strip()
    if not text:
        return None
    parts = text.split(":")
    if len(parts) > 3:
        return None
    try:
        numbers = [float(part) for part in parts]
    except ValueError:
        return None
    seconds = 0.0
    for number in numbers:
        seconds = seconds * 60 + number
    return seconds


def fetch_timed_transcript(video_id: str) -> dict:
    """Return NoteGPT's caption segments with start/end times.

    The result is suitable for syncing the transcript with an embedded
    player. It uses the same English track as ``_fetch_with_notegpt`` and
    normalizes each segment to a contiguous time window so the browser can
    highlight exactly one portion at a time.
    """
    data = _notegpt_data(video_id)
    _language_key, tracks, language_name, non_english = _notegpt_tracks(data)
    for track_name in ("default", "auto", "custom"):
        raw_segments = tracks.get(track_name)
        if not isinstance(raw_segments, list) or not raw_segments:
            continue
        parsed: list[dict] = []
        for raw_segment in raw_segments:
            if not isinstance(raw_segment, dict):
                continue
            text = " ".join(str(raw_segment.get("text") or "").split())
            if not text:
                continue
            start = _timed_seconds(raw_segment.get("start"))
            if start is None:
                continue
            end = _timed_seconds(raw_segment.get("end"))
            parsed.append({"start": start, "end": end, "text": text})
        if not parsed:
            continue
        parsed.sort(
            key=lambda item: (
                item["start"],
                item["end"] if item["end"] is not None else item["start"],
            )
        )
        segments: list[dict] = []
        for index, item in enumerate(parsed):
            start = float(item["start"])
            next_start = (
                float(parsed[index + 1]["start"])
                if index + 1 < len(parsed)
                else None
            )
            raw_end = item["end"]
            if next_start is not None and next_start > start:
                end = next_start
            elif raw_end is not None and float(raw_end) > start:
                end = float(raw_end)
            else:
                end = start + 2.0
            segments.append(
                {
                    "start": round(start, 3),
                    "end": round(end, 3),
                    "text": item["text"],
                }
            )
        return {
            "video_id": video_id,
            "language": language_name,
            "source": "NoteGPT",
            "track": track_name,
            "non_english": non_english,
            "segments": segments,
        }
    raise RuntimeError("NoteGPT returned no timed transcript segments")


def _fetch_transcript_source(video_id: str) -> tuple[str, str, bool]:
    """Fetch the transcript from NoteGPT, the only supported source.

    Returns ``(text, language_name, is_non_english)``. The caller translates
    the text when NoteGPT only supplied a non-English caption track.
    """
    try:
        text, language_name, non_english = _fetch_with_notegpt(video_id)
    except Exception as exc:
        raise RuntimeError(
            "NoteGPT could not fetch a transcript for "
            f"{video_id}: {_classify_error(exc)} ({_short_error(exc, 500)})"
        ) from exc
    _log(
        "fetched transcript with NoteGPT"
        + (f" ({language_name})" if language_name else "")
        + ("; translating it to English" if non_english else "")
    )
    return text, f"NoteGPT ({language_name})", non_english


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
    pipeline are idempotent and do not hit NoteGPT again.

    NoteGPT is the only transcript source, per prompts/youtube.prompt. When it
    returns a non-English track, the text is translated locally with Ollama.
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

    transcript, language_name, non_english = _fetch_transcript_source(
        video_id
    )

    if non_english:
        return _translate_transcript(transcript, language_name)
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
            "Fetch a YouTube transcript with NoteGPT and summarize it into "
            "20 words."
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
