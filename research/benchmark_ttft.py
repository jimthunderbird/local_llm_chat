#!/usr/bin/env python3
"""Regression TTFT benchmark for chat.py.

Usage:
    python3 research/benchmark_ttft.py
    python3 research/benchmark_ttft.py --show-answers
    python3 research/benchmark_ttft.py --depth 30 --max-context-tokens 4096

The script calls the same retrieval and streaming path as the chatbot, with
the answer text suppressed unless --show-answers is given. It reports median
end-to-end TTFT, prompt prefill, prompt tokens, retrieval time, and context
size across the regression question set. It never uses the answer cache, and
it reports Ollama's cached-prefix count so cache effects are visible.
"""

from __future__ import annotations

import argparse
import contextlib
import io
import statistics
import sys
import uuid
from pathlib import Path

SCRIPT_DIR = Path(__file__).resolve().parent
PROJECT_DIR = SCRIPT_DIR.parent
sys.path.insert(0, str(PROJECT_DIR))

import chat  # noqa: E402  (path setup must happen first)

DEFAULT_QUESTIONS = [
    "list me all levels of wagyu in details",
    "what time does nijo market open",
    "where is nijo market",
    "i want to get 90-day long-stay visa, what are the requirements",
    "show me list of airports",
    "show me a list of small towns",
    "i want to buy electronics in japan, give me a recommendation in 600 words",
    "can i do left turn on red light",
    "我想获得90天长期签证，需要什么条件",
]


def parse_depth(value: str) -> int | None:
    if value.strip().lower() in {"auto", "default", "0"}:
        return None
    return int(value)


def main() -> None:
    parser = argparse.ArgumentParser(description="Benchmark chat.py TTFT.")
    parser.add_argument(
        "--depth",
        default="auto",
        help="auto or an explicit number of trunks (default: auto)",
    )
    parser.add_argument(
        "--max-context-tokens",
        type=int,
        default=chat.MAX_CONTEXT_TOKENS,
        help=f"auto retrieval budget (default: {chat.MAX_CONTEXT_TOKENS})",
    )
    parser.add_argument(
        "--question",
        action="append",
        default=[],
        help="benchmark only this question (repeatable)",
    )
    parser.add_argument(
        "--show-answers",
        action="store_true",
        help="print each full answer after its metrics row",
    )
    parser.add_argument(
        "--no-packed-embeddings",
        action="store_true",
        help="use individual JSON embeddings instead of .chat/packs",
    )
    parser.add_argument(
        "--progressive",
        action="store_true",
        help="benchmark the optional two-stage progressive streaming path",
    )
    parser.add_argument(
        "--no-cache-bust",
        action="store_true",
        help="allow Ollama prefix-cache reuse (default: prepend a unique "
        "nonce so prefill measurements are real)",
    )
    args = parser.parse_args()

    chat.USE_PACKED_EMBEDDINGS = not args.no_packed_embeddings
    depth = parse_depth(args.depth)
    questions = args.question or DEFAULT_QUESTIONS

    chunks = chat.load_embeddings()
    print(
        f"Benchmark: global corpus, depth="
        f"{'auto' if depth is None else depth}, "
        f"budget={args.max_context_tokens:,}, chunks={len(chunks):,}, "
        f"questions={len(questions)}\n"
    )

    rows = []
    for number, question in enumerate(questions, start=1):
        stats: dict = {}
        output = io.StringIO()
        # A per-question nonce at the very start of the prompt defeats Ollama's
        # prefix cache, so prompt_eval_duration reflects real prefill work.
        prompt_prefix = ""
        if not args.no_cache_bust:
            prompt_prefix = f"<benchmark-nonce>{uuid.uuid4()}</benchmark-nonce>\n"
        with contextlib.redirect_stdout(output):
            try:
                response, sources = chat.answer(
                    question,
                    chunks,
                    depth=depth,
                    max_context_tokens=args.max_context_tokens,
                    stats=stats,
                    progressive=args.progressive,
                    prompt_prefix=prompt_prefix,
                )
            except Exception as exc:  # keep benchmarking the remaining questions
                print(
                    f"{number:>2}. ERROR {question!r}: {exc}",
                    file=sys.stderr,
                )
                continue

        rows.append(stats)
        print(
            f"{number:>2}. TTFT {stats.get('end_to_end_ttft', 0):5.2f} s | "
            f"prefill {stats.get('prefill', 0):4.2f} s | "
            f"retrieval {stats.get('retrieval', 0):4.2f} s | "
            f"prompt {stats.get('prompt_tokens', 0):5,} tok "
            f"(cached {stats.get('prompt_tokens_cached', 0):4,}) | "
            f"context {stats.get('context_tokens', 0):5,} est | "
            f"trunks {stats.get('trunks', 0):2} | "
            f"total {stats.get('total', 0):5.2f} s | {question}"
        )
        if args.show_answers:
            print(f"\n--- answer ({len(response or ''):,} chars) ---")
            print(response)
            print("--- end answer ---\n")

    if not rows:
        sys.exit("No benchmark rows collected.")

    print("\nMedian over {} question(s):".format(len(rows)))
    for label, key, unit in (
        ("end-to-end TTFT", "end_to_end_ttft", "s"),
        ("request TTFT", "request_ttft", "s"),
        ("prompt prefill", "prefill", "s"),
        ("retrieval", "retrieval", "s"),
        ("prompt tokens", "prompt_tokens", "tok"),
        ("prompt tokens cached", "prompt_tokens_cached", "tok"),
        ("context estimated", "context_tokens", "tok"),
        ("trunks", "trunks", ""),
        ("total answer time", "total", "s"),
    ):
        values = [row.get(key, 0) for row in rows]
        median = statistics.median(values)
        if unit == "s":
            print(f"  {label:22} {median:7.2f} s")
        else:
            print(f"  {label:22} {median:7,.0f} {unit}".rstrip())


if __name__ == "__main__":
    main()
