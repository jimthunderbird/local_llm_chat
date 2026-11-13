#!/usr/bin/env python3
"""Rewrite a YouTube transcript into self-contained knowledge chunks with Ollama.

Usage:
    python3 rewrite.py <youtube_video_hash>

Reads:
    ./.chat/articles_cache/youtube_<youtube_video_hash>.transcript

Writes:
    ./.chat/articles_cache/youtube_<youtube_video_hash>.rewrite
"""

import argparse
import json
import os
import re
import sys
from pathlib import Path

try:
    import requests
except ImportError as exc:  # pragma: no cover - dependency guard
    sys.exit(f"Missing dependency: {exc.name}. Install with: pip install requests")

from paragraph_utils import split_paragraphs

def normalize_argv(argv):
    """Let positional values begin with '-' (e.g. video ID -PcBFgkrR2o)."""
    if argv and argv[0].startswith("-") and argv[0] not in {"-h", "--help", "--"}:
        return ["--", *argv]
    return argv


# The YouTube-learning pipeline always uses the large answer model.
MODEL = os.environ.get("OLLAMA_YOUTUBE_MODEL", "ornith-1.5:35b")
OUTPUT_DIR = Path.cwd() / ".chat" / "articles_cache"
YOUTUBE_PREFIX = "youtube_"

OLLAMA_HOST = os.environ.get("OLLAMA_HOST", "http://localhost:11434").rstrip("/")
if not OLLAMA_HOST.startswith(("http://", "https://")):
    OLLAMA_HOST = f"http://{OLLAMA_HOST}"

PROMPT = """You are building a knowledge base from a YouTube transcript.
Rewrite the transcript into a set of detailed, self-contained knowledge paragraphs.

Requirements:
- Extract ALL factual details, explanations, examples, numbers, names, places,
  recommendations, and caveats from the transcript. Do not invent information.
- Output ONLY the rewritten paragraphs. No title, preamble, summary, commentary,
  bullet points, numbering, or markdown formatting.
- Separate paragraphs with exactly one blank line.
- Each paragraph must be short (roughly 40-80 words) and express ONE distinct
  piece of knowledge.
- Each paragraph must make sense on its own, because it will be retrieved
  independently in a RAG system. Resolve pronouns and references: name the
  subject explicitly instead of saying "it", "this place", "he", or "the video".
- Keep the original meaning and useful specifics. Remove filler, greetings,
  sponsorship reads, and channel chatter.
- Write in clear, plain English.

Transcript:
{transcript}
"""


def rewrite_transcript(transcript: str) -> str:
    """Ask Ollama to turn the transcript into self-contained knowledge chunks."""
    payload = {
        "model": MODEL,
        "prompt": PROMPT.format(transcript=transcript),
        "stream": True,
        "think": False,
        "options": {
            "temperature": 0.3,
            "num_ctx": 32768,
            "num_predict": 8192,
            "repeat_penalty": 1.05,
        },
    }

    chunks = []
    try:
        with requests.post(
            f"{OLLAMA_HOST}/api/generate",
            json=payload,
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
                    raise RuntimeError(f"Ollama error: {event['error']}")
                token = event.get("response", "")
                if token:
                    # Stream the rewritten content to the terminal as it arrives.
                    print(token, end="", flush=True)
                    chunks.append(token)
                if event.get("done"):
                    break
    except requests.RequestException as exc:
        raise RuntimeError(
            f"Could not reach Ollama at {OLLAMA_HOST}. Is it running? ({exc})"
        ) from exc

    print(flush=True)
    rewritten = "".join(chunks).strip()
    if not rewritten:
        raise RuntimeError("Ollama returned an empty rewrite")
    return normalize_output(rewritten)


def normalize_output(text: str) -> str:
    """Clean up model formatting so the result is plain blank-line paragraphs."""
    # Drop accidental markdown code fences.
    text = re.sub(r"^```[a-zA-Z]*\s*", "", text)
    text = re.sub(r"\s*```$", "", text)

    # Remove a leading meta line such as "Here is the rewritten transcript:".
    text = re.sub(
        r"^(here (is|are)[^\n]*|rewritten (transcript|content)[^\n]*):?\s*\n+",
        "",
        text,
        flags=re.IGNORECASE,
    )

    # Strip markdown list markers, headings, and numbering from line starts.
    lines = []
    for line in text.splitlines():
        line = re.sub(r"^\s{0,3}#{1,6}\s*", "", line)
        line = re.sub(r"^\s*[-*+]\s+", "", line)
        line = re.sub(r"^\s*\d+[.)]\s+", "", line)
        lines.append(line.rstrip())
    text = "\n".join(lines)

    # Collapse runs of blank lines into a single paragraph separator.
    text = re.sub(r"\n\s*\n+", "\n\n", text)
    text = re.sub(r"[ \t]+", " ", text)

    # Reflow line-separated output and split overlong blocks so the saved
    # rewrite itself consists of short, RAG-ready paragraphs.
    return "\n\n".join(split_paragraphs(text))


def main() -> None:
    parser = argparse.ArgumentParser(
        description="Rewrite a YouTube transcript into RAG-ready knowledge chunks."
    )
    parser.add_argument("youtube_video_hash", help="YouTube video ID, e.g. FTwxtyOEGyM")
    args = parser.parse_args(normalize_argv(sys.argv[1:]))

    video_id = args.youtube_video_hash.strip()
    if not re.fullmatch(r"[A-Za-z0-9_-]+", video_id):
        sys.exit(f"Invalid YouTube video hash: {video_id!r}")

    storage_id = f"{YOUTUBE_PREFIX}{video_id}"
    transcript_path = OUTPUT_DIR / f"{storage_id}.transcript"
    if not transcript_path.is_file():
        sys.exit(
            f"Transcript not found: {transcript_path}\n"
            f"Run: python3 fetch_youtube_transcript.py {video_id}"
        )

    transcript = transcript_path.read_text(encoding="utf-8").strip()
    if not transcript:
        sys.exit(f"Transcript is empty: {transcript_path}")

    try:
        rewritten = rewrite_transcript(transcript)
    except Exception as exc:
        sys.exit(f"Error: {exc}")

    rewrite_path = OUTPUT_DIR / f"{storage_id}.rewrite"
    rewrite_path.write_text(rewritten + "\n", encoding="utf-8")

    paragraphs = [p for p in rewritten.split("\n\n") if p.strip()]
    print(f"Saved rewrite: {rewrite_path}")
    print(f"Knowledge chunks: {len(paragraphs)}")


if __name__ == "__main__":
    main()
