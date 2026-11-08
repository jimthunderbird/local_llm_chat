#!/usr/bin/env python3
"""Interactive RAG chatbot over the YouTube transcript embeddings.

Usage:
    python3 chat_gradual_context.py

At startup, every ./.chat/youtube_transcripts/<youtube_video_hash>.<index>.embedding
file is loaded into memory and paired with its
./.chat/youtube_transcripts/<youtube_video_hash>.<index>.trunk file. Every
question searches the whole global corpus; there is no tag scope.

For each question:
    1. Embed the question with Ollama model embeddinggemma-2:570m.
    2. Cosine-rank every stored embedding and keep a shortlist of the best
       candidates.
    3. Re-rank that shortlist using only each trunk's paragraph text (not the
       shared title), so one video's title cannot crowd out more relevant
       chunks from other videos.
    4. Normalize the question's spelling and word boundaries before retrieval
       (for example "redlight" -> "red light"), so equivalent questions
       retrieve the same context.
    5. Grow the context gradually from that ranking, three trunks at a time:
         a. Add the next 3 new trunks. For list/exhaustive questions, gather
            a minimum number of trunks before the first check so the checker
            can see whether the list is complete.
         b. Ask qwen3:1.7b to build a small checklist of what a complete
            answer requires and to state whether the accumulated context
            contains enough of that checklist.
         c. Stop when the checklist is covered or the context cap is reached.
    6. Stream the answer from Ollama model ornith:35b.

Simplified Chinese questions are translated into English before retrieval;
answers are translated back into Simplified Chinese while they stream.

Commands:
    /memory            Show memory used by the loaded embeddings.
    /refresh           Reload the global embedding corpus.
    /transcript <hash|keyword>
                       Use one video's transcript (reloads embeddings).
    /search_transcript <keyword>
                       Alias for /transcript <keyword>.
    /youtube learn <hash|url>
                       Fetch, rewrite, chunk, and embed a video.
    /wiki <word>       Look up a Wikipedia summary.
    /dict <word>       Show English dictionary definitions for <word>.
    /depth <n|auto>    Set the maximum gradually gathered trunks.
    /status            Show the current chat status.
    /sources           Show the source list for the latest answer.
    /exit, /quit, /q   Leave the chatbot.
"""

import argparse
import gc
import json
import math
import os
import re
import subprocess
import sys
from collections import Counter
from dataclasses import dataclass
from pathlib import Path
from urllib.parse import quote

try:
    import requests
except ImportError as exc:  # pragma: no cover - dependency guard
    sys.exit(f"Missing dependency: {exc.name}. Install with: pip install requests")

try:
    from prompt_toolkit import PromptSession
    from prompt_toolkit.application import get_app
    from prompt_toolkit.completion import Completer, Completion
    from prompt_toolkit.filters import vi_insert_mode
    from prompt_toolkit.history import History
    from prompt_toolkit.key_binding import KeyBindings
    from prompt_toolkit.key_binding.vi_state import InputMode
except ImportError as exc:  # pragma: no cover - dependency guard
    sys.exit(
        f"Missing dependency: {exc.name}. Install with: "
        "python3 -m pip install --user prompt_toolkit"
    )

EMBED_MODEL = "embeddinggemma-2:570m"
CHAT_MODEL = "qwen3:1.7b"
# Local model used to translate Simplified Chinese questions/answers.
TRANSLATION_MODEL = "qwen3:1.7b"
# Local model used by /dict to generate definitions.
DICT_MODEL = "gemma3:1b"
MIN_SIMILARITY = 0.75
FALLBACK_TOP_N = 10
# Default maximum number of gradually gathered trunks (see /depth).
DEFAULT_DEPTH = 100
# Number of title-influenced candidates re-ranked on paragraph text alone.
RERANK_CANDIDATES = 100
# Gradual-context retrieval adds this many new trunks before each
# sufficiency check, and never collects more than this many trunks.
GRADUAL_BATCH_SIZE = 3
GRADUAL_MAX_CONTEXT = 100
# Context window requested from Ollama for answer generation.
ANSWER_NUM_CTX = 32768
# Rough token estimate: about four ASCII characters or one non-ASCII
# character (for example CJK) per token.
CHARS_PER_TOKEN = 4
# The 1.7B sufficiency checker can call a partial enumeration sufficient after
# seeing one introductory sentence (for example, a video title that says
# "ten levels" plus details for only two of them). Before trusting its
# verdict, gather at least this many trunks for questions that ask for an
# exhaustive list, and a smaller amount for ordinary list requests.
GRADUAL_MIN_EXHAUSTIVE = 50
GRADUAL_MIN_LIST = 20
EXHAUSTIVE_PATTERN = re.compile(
    r"\b(all|every|each|complete|full|levels?|types?|kinds?|grades?|"
    r"categories|rankings?)\b",
    re.IGNORECASE,
)
LIST_PATTERN = re.compile(
    r"\b(list|lists|enumerate|examples?|options?|requirements?|"
    r"recommendations?|ways?|steps?)\b",
    re.IGNORECASE,
)
SCRIPT_DIR = Path(__file__).resolve().parent
OUTPUT_DIR = Path.cwd() / ".chat" / "youtube_transcripts"
CHAT_DIR = Path.cwd() / ".chat"
HISTORY_FILE = CHAT_DIR / "question_history.txt"

OLLAMA_HOST = os.environ.get("OLLAMA_HOST", "http://localhost:11434").rstrip("/")
if not OLLAMA_HOST.startswith(("http://", "https://")):
    OLLAMA_HOST = f"http://{OLLAMA_HOST}"

EMBEDDING_FILE = re.compile(
    r"^(?P<video>[A-Za-z0-9_-]+)\.(?P<index>\d+)\.embedding$"
)
PARAGRAPH_PATTERN = re.compile(
    r"<paragraph>(.*?)</paragraph>", re.DOTALL | re.IGNORECASE
)
# Alphabetic words used for question normalization and compound-word splitting.
WORD_PATTERN = re.compile(r"[A-Za-z]+")
# Cache of word frequencies for the currently loaded chunks. The cache key is
# the tuple of chunk labels, so switching transcripts invalidates it.
_WORD_COUNTS_KEY: tuple[str, ...] | None = None
_WORD_COUNTS: Counter[str] = Counter()
# Output of `ag --vimgrep`: <path>:<line>:<column>:<matching text>.
AG_MATCH_PATTERN = re.compile(
    r"^(?P<path>.+?):\d+:\d+:(?P<title>.*)$"
)

WIKI_SUMMARY_URL = "https://en.wikipedia.org/api/rest_v1/page/summary/{word}"
HTTP_HEADERS = {
    "User-Agent": "article-vector-strategy-chat/1.0 (local RAG chatbot)",
    "Accept": "application/json",
}

PROMPT = """<context>{context}</context>
based on <context>, perform the instruction or answer the following question: {question}

If the sources conflict, state the general rule and the specific exception, then
answer according to the exception. If the sources agree, answer directly without
discussing conflicts."""

SUFFICIENCY_PROMPT = """<context>{context}</context>

Question or instruction: {question}

Part 1: List every item or fact the question asks for that is explicitly
present in the context. If the question calls for an exhaustive list (for
example it uses "all", "every", "levels", "types", or "kinds"), also list the
expected items that are absent from the context.
Part 2: Is the context sufficient for a complete and accurate answer? End with
exactly one line: "VERDICT: YES" or "VERDICT: NO".

Do not answer the question itself. Do not use outside knowledge."""

DICT_USAGE_PROMPT = """The following is the latest answer in this conversation:
<latest_answer>
{latest_answer}
</latest_answer>

The word "{word}" appears in that answer. Explain how it is used there
(its part of speech and grammatical role, if clear) and what it means in
that specific context. Quote the relevant phrase or sentence, then give the
contextual meaning. Keep the explanation concise."""

DICT_PROMPT = """Provide a concise English dictionary entry for the word or phrase
"{word}". Include the part of speech, a clear definition, and one short
example sentence. If the input is not a valid English word or phrase, say so
plainly instead of inventing a meaning. Keep the answer under 120 words."""

QUESTION_TRANSLATION_PROMPT = """Translate the following Simplified Chinese question into natural English.
Preserve names, titles, technical terms, and the intent of the question exactly.
Output only the English translation, with no explanation or notes.

<question>
{question}
</question>"""

ANSWER_TRANSLATION_PROMPT = """Translate the following English text into Simplified Chinese.
Translate every ordinary English word, but leave proper nouns in their original
Latin spelling. Never transliterate place names or invent Chinese names.

Examples:
English: "Takayama is a small town." -> Chinese: "Takayama 是一个小镇。"
English: "I visited Okutama and Hinohara." -> Chinese: "我参观了 Okutama 和 Hinohara。"
English: "1. Kyoto" -> Chinese: "1. Kyoto"

Preserve Markdown formatting and lists. Output only the Simplified Chinese
translation, with no explanation or notes.

<text>
{text}
</text>"""

HAN_PATTERN = re.compile(r"[\u3400-\u4dbf\u4e00-\u9fff]")
KANA_PATTERN = re.compile(r"[\u3040-\u30ff]")
THINK_PATTERN = re.compile(
    r"<think(?:ing)?>.*?</think(?:ing)?>", re.DOTALL | re.IGNORECASE
)

# Characters that only occur in Simplified Chinese. Used to tell Simplified
# from Traditional Chinese without pulling in a full conversion library.
SIMPLIFIED_ONLY_CHARS = frozenset(
    "这个们说国学时会来对开关门问题吗么无电话见现实发东马鸟龙书车长风吹飞"
    "买卖钱银铁错别认识语讲谁请谢让该还过进远边运达选迟适遗邮邻释针钟钢键"
    "镇闭闻间闷闹阁阅队阶阳阴陆陈险随隐难雾静韩页顶项顺须顽顾顿预领颇颈频"
    "颗颜愿类显饭饮饲饱饰馆驾驶验骑骗鱼鸡鸣鸦鸭鸽鹅鹰麦黄齐齿龄龟爱亿仪价"
    "众优传伤侦侧侨债倾仅从伞伟伪杰备儿两册冻净准凉减凑汤温测济涛湾湿灯争"
    "尔墙独获环产画当疗尽监盘碍礼税稳穷竞笔节范筑简签篮筹粮纪约红级纯纸紧"
    "线练组细终结绝统丝经绿维网绵编缘县总织继续"
)

# Characters that only occur in Traditional Chinese (the counterparts of the
# simplified characters above).
TRADITIONAL_ONLY_CHARS = frozenset(
    "這個們說國學會時來對開關門問題嗎麼無電話見現實發東馬鳥龍書車長風飛"
    "買賣錢銀鐵錯別認識語講誰請謝讓該還過進遠邊運達選遲適遺郵鄰釋針鐘鋼鍵"
    "鎮閉聞間悶鬧閣閱隊階陽陰陸陳險隨隱難霧靜韓頁頂項順須頑顧頓預領頗頸頻"
    "顆顏願類顯飯飲飼飽飾館駕駛驗騎騙魚雞鳴鴉鴨鴿鵝鷹麥黃齊齒齡龜愛億儀價"
    "眾優傳傷偵側僑債傾僅從傘偉偽傑備兒兩冊凍淨準涼減湊湯溫測濟濤灣濕燈爭"
    "爾牆獨獲環產畫當療盡監盤礙禮稅穩窮競筆節範築簡簽籃籌糧紀約紅級純紙緊"
    "線練組細終結絕統絲經綠維網綿編緣縣總織繼續"
)

# Sentence groups are translated while the English answer is still streaming.
TRANSLATION_MIN_CHARS = 40
TRANSLATION_MAX_CHARS = 400
TRANSLATION_BOUNDARY = re.compile(
    r"[.!?。！？](?:[\"'”’)\]]*)\s*"
)
LIST_PREFIX = re.compile(r"^(\s*(?:\d+[.)]|[-*+]))\s+")

# Every chatbot command, with the help text shown next to its completion.
COMMANDS = {
    "/memory": "show memory used by the loaded embeddings",
    "/refresh": "reload the global embedding corpus",
    "/transcript": "use one transcript by hash/URL or search titles with ag",
    "/search_transcript": "alias for /transcript <keyword> (ag title search)",
    "/youtube": "YouTube video commands: /youtube learn <hash|url>",
    "/wiki": "look up a Wikipedia summary for <word>",
    "/dict": "show English dictionary definitions for <word> (local Ollama) and explain its use and meaning in the latest answer",
    "/depth": "set the maximum number of gradually gathered trunks (or 'auto')",
    "/status": "show the current chat status",
    "/sources": "show the source list for the latest answer",
    "/exit": "leave the chatbot",
    "/quit": "leave the chatbot",
    "/q": "leave the chatbot",
}

# Subcommands grouped under /youtube.
YOUTUBE_SUBCOMMANDS = {
    "learn": "fetch, rewrite, chunk, and embed one video",
}


@dataclass
class Chunk:
    """One embedded knowledge trunk."""

    video_id: str
    index: int
    embedding: list[float]
    trunk_path: Path

    @property
    def label(self) -> str:
        return f"{self.video_id}.{self.index}"


@dataclass(frozen=True)
class TranscriptMatch:
    """A title-file match returned by /transcript and /search_transcript."""

    video_id: str
    title: str


def load_embeddings(video_ids: set[str] | None = None) -> list[Chunk]:
    """Load embeddings and pair them with their trunk files.

    By default every learned video is loaded. When ``video_ids`` is given
    (the /transcript command), only those videos are loaded.
    """
    if not OUTPUT_DIR.is_dir():
        sys.exit(f"Embedding directory not found: {OUTPUT_DIR}")

    if video_ids is None:
        selected_video_ids = set(available_video_ids())
        scope = "the global corpus"
        outside_scope = "outside the global corpus"
    else:
        selected_video_ids = set(video_ids)
        scope = "transcript " + ", ".join(sorted(selected_video_ids))
        outside_scope = "from other transcripts"

    chunks: list[Chunk] = []
    expected_dim: int | None = None
    skipped = 0
    filtered = 0

    paths = sorted(
        OUTPUT_DIR.glob("*.embedding"),
        key=lambda p: (p.name.split(".", 1)[0], p.name),
    )
    for path in paths:
        match = EMBEDDING_FILE.fullmatch(path.name)
        if not match:
            continue
        if match.group("video") not in selected_video_ids:
            filtered += 1
            continue

        trunk_path = path.with_name(path.name[: -len(".embedding")] + ".trunk")
        if not trunk_path.is_file():
            print(f"Warning: missing trunk for {path.name}, skipping.", file=sys.stderr)
            skipped += 1
            continue

        try:
            embedding = json.loads(path.read_text(encoding="utf-8"))
            if not isinstance(embedding, list) or not embedding:
                raise ValueError("empty or non-list embedding")
            embedding = [float(value) for value in embedding]
        except (OSError, ValueError, TypeError, json.JSONDecodeError) as exc:
            print(f"Warning: cannot read {path.name}: {exc}, skipping.", file=sys.stderr)
            skipped += 1
            continue

        if expected_dim is None:
            expected_dim = len(embedding)
        elif len(embedding) != expected_dim:
            print(
                f"Warning: {path.name} has {len(embedding)} dimensions, "
                f"expected {expected_dim}, skipping.",
                file=sys.stderr,
            )
            skipped += 1
            continue

        # Normalize once so cosine similarity is a simple dot product.
        norm = math.sqrt(math.fsum(value * value for value in embedding))
        if norm == 0.0:
            print(f"Warning: {path.name} is a zero vector, skipping.", file=sys.stderr)
            skipped += 1
            continue

        chunks.append(
            Chunk(
                video_id=match.group("video"),
                index=int(match.group("index")),
                embedding=[value / norm for value in embedding],
                trunk_path=trunk_path,
            )
        )

    if not chunks:
        sys.exit(
            f"No embeddings found for {scope} in {OUTPUT_DIR}\n"
            "Run: python3 youtube_video_learn.py <youtube_video_hash>"
        )

    details = []
    if skipped:
        details.append(f"{skipped} skipped")
    if filtered and video_ids is None:
        details.append(f"{filtered} {outside_scope}")
    suffix = f" ({', '.join(details)})" if details else ""
    print(f"Loaded {len(chunks)} embeddings for {scope}{suffix}.")
    return chunks


def format_bytes(num: float) -> str:
    """Format a byte count for the memory report."""
    value = float(num)
    for unit in ("B", "KiB", "MiB", "GiB", "TiB"):
        if abs(value) < 1024.0 or unit == "TiB":
            return f"{value:.1f} {unit}"
        value /= 1024.0
    return f"{value:.1f} TiB"


def embedding_memory_report(chunks: list[Chunk]) -> str:
    """Describe how much memory the loaded embeddings occupy."""
    if not chunks:
        return "Embedding memory: no embeddings loaded."

    dims = len(chunks[0].embedding)
    values = sum(len(chunk.embedding) for chunk in chunks)
    # Python stores every float as a separate object (~24 bytes) plus an
    # 8-byte pointer in the list. This is the in-process footprint.
    python_bytes = sum(
        sys.getsizeof(chunk.embedding) + len(chunk.embedding) * sys.getsizeof(0.0)
        for chunk in chunks
    )
    packed_bytes = values * 4  # what the same vectors cost as float32

    lines = [
        f"Embedding memory: {len(chunks):,} chunks x {dims} dims "
        f"= {values:,} values",
        f"  ~{format_bytes(python_bytes)} as Python float objects "
        f"({format_bytes(packed_bytes)} if packed as float32)",
    ]
    try:
        import resource

        rss = resource.getrusage(resource.RUSAGE_SELF).ru_maxrss
        if sys.platform != "darwin":  # Linux reports KiB, macOS reports bytes
            rss *= 1024
        lines.append(f"  process peak RSS: {format_bytes(rss)}")
    except (ImportError, OSError):  # pragma: no cover - non-Unix platforms
        pass
    return "\n".join(lines)


def chat_status_report(
    transcript: str | None,
    chunks: list[Chunk],
    depth: int | None,
) -> str:
    """Describe the chatbot's current configuration and loaded data."""
    video_ids = {chunk.video_id for chunk in chunks}
    if transcript is not None:
        scope = f"single transcript {transcript!r}"
    else:
        scope = "global corpus (all learned trunks)"

    cap = retrieval_cap(depth)
    retrieval = (
        f"gradual: up to {cap} trunk(s), "
        f"{GRADUAL_BATCH_SIZE} new trunk(s) per sufficiency check"
    )
    if depth is None:
        retrieval += " (default cap)"

    lines = [
        "Chat status:",
        f"  models:      {CHAT_MODEL} (chat), {EMBED_MODEL} (embeddings), "
        f"{TRANSLATION_MODEL} (translation)",
        f"  Ollama host: {OLLAMA_HOST}",
        f"  scope:       {scope}",
        f"  loaded:      {len(chunks):,} chunks from "
        f"{len(video_ids):,} video(s)",
        f"  retrieval:   {retrieval}",
        f"  history:     {history_count(HISTORY_FILE):,} saved entries",
    ]
    return "\n".join(lines)


class CommandCompleter(Completer):
    """Complete chatbot commands when the first word starts with '/'."""

    def get_completions(self, document, complete_event):
        text = document.text_before_cursor
        if not text.startswith("/"):
            return
        # Commands are always the first word; once an argument is typed,
        # /transcript gets completions.
        if any(ch.isspace() for ch in text):
            name, _, argument = text.partition(" ")
            prefix = argument.lstrip()
            if name.lower() == "/transcript":
                for video_id in available_video_ids():
                    if video_id.startswith(prefix):
                        yield Completion(video_id, start_position=-len(prefix))
            elif name.lower() == "/youtube":
                for subcommand, description in YOUTUBE_SUBCOMMANDS.items():
                    if subcommand.startswith(prefix):
                        yield Completion(
                            subcommand,
                            start_position=-len(prefix),
                            display=subcommand,
                            display_meta=description,
                        )
            return
        for command in sorted(COMMANDS):
            if command.startswith(text):
                yield Completion(
                    command,
                    start_position=-len(text),
                    display=command,
                    display_meta=COMMANDS[command],
                )


def available_video_ids() -> list[str]:
    """Return video ids that have embeddings in ./.chat/youtube_transcripts."""
    try:
        entries = list(OUTPUT_DIR.glob("*.embedding"))
    except OSError:
        return []
    video_ids = set()
    for entry in entries:
        match = EMBEDDING_FILE.fullmatch(entry.name)
        if match:
            video_ids.add(match.group("video"))
    return sorted(video_ids)


class VideoCompleter(Completer):
    """Tab-complete video ids that have embeddings."""

    def get_completions(self, document, complete_event):
        prefix = document.text_before_cursor
        for video_id in available_video_ids():
            if video_id.startswith(prefix):
                yield Completion(video_id, start_position=-len(prefix))


class TranscriptMatchCompleter(Completer):
    """Tab-complete video ids from the current /transcript matches."""

    def __init__(self, matches: list[TranscriptMatch]) -> None:
        self.matches = matches

    def get_completions(self, document, complete_event):
        prefix = document.text_before_cursor.strip()
        for match in self.matches:
            if match.video_id.startswith(prefix):
                yield Completion(
                    match.video_id,
                    start_position=-len(prefix),
                    display=f"{match.video_id}  {match.title}",
                    display_meta=match.title,
                )


def prompt_for_video_id() -> str:
    """Prompt for a transcript video id, tab-completing available ones."""
    video_ids = available_video_ids()
    if not video_ids:
        sys.exit(f"No transcripts with embeddings found in {OUTPUT_DIR}")

    print(f"{len(video_ids)} transcripts available (Tab to complete).")
    session = PromptSession(
        completer=VideoCompleter(),
        complete_while_typing=True,
        multiline=False,
    )
    while True:
        try:
            video_id = session.prompt("Transcript: ").strip()
        except (EOFError, KeyboardInterrupt):
            print()
            sys.exit("No transcript selected.")
        if not video_id:
            continue
        if video_id in video_ids:
            return video_id
        print(
            f"Unknown transcript: {video_id!r}. "
            "Type a video id with embeddings, or use /youtube learn first."
        )


class PlainFileHistory(History):
    """One chat entry per line in ./.chat/question_history.txt.

    Both questions and slash commands are stored so Up/Down can navigate
    back and forth through the complete session history.
    """

    def __init__(self, path: Path) -> None:
        super().__init__()
        self.path = path
        try:
            self.path.parent.mkdir(parents=True, exist_ok=True)
            self.path.touch(exist_ok=True)
        except OSError as exc:
            print(f"Warning: cannot create {self.path}: {exc}", file=sys.stderr)

    def append_string(self, string: str) -> None:
        if not string.strip():
            return
        super().append_string(string)

    def load_history_strings(self):
        """Yield the most recent entry first, as prompt_toolkit expects."""
        try:
            lines = self.path.read_text(
                encoding="utf-8", errors="replace"
            ).splitlines()
        except OSError:
            return
        for line in reversed(lines):
            if line.strip():
                yield line

    def store_string(self, string: str) -> None:
        try:
            with self.path.open("a", encoding="utf-8") as handle:
                handle.write(string.replace("\n", " ") + "\n")
        except OSError as exc:
            print(
                f"Warning: cannot append to {self.path}: {exc}", file=sys.stderr
            )


def make_prompt_session(history: PlainFileHistory) -> PromptSession:
    """Create a vi-mode prompt with history and a mode-aware toolbar."""
    bindings = KeyBindings()

    @bindings.add("up", filter=vi_insert_mode)
    def _history_up(event) -> None:
        event.current_buffer.auto_up(count=event.arg)

    @bindings.add("down", filter=vi_insert_mode)
    def _history_down(event) -> None:
        event.current_buffer.auto_down(count=event.arg)

    @bindings.add("tab")
    def _complete(event) -> None:
        """Tab completes commands; repeated Tab cycles the matches."""
        buffer = event.current_buffer
        if buffer.complete_state:
            buffer.complete_next()
        else:
            buffer.start_completion(select_first=False)

    def bottom_toolbar() -> str:
        app = get_app()
        buffer = app.current_buffer
        if buffer.selection_state is not None:
            mode = (
                "VISUAL-LINE"
                if buffer.selection_state.type.name == "LINES"
                else "VISUAL"
            )
        elif app.vi_state.input_mode in {
            InputMode.INSERT,
            InputMode.INSERT_MULTIPLE,
            InputMode.REPLACE,
            InputMode.REPLACE_SINGLE,
        }:
            mode = "INSERT"
        else:
            mode = "NORMAL"
        return (
            f" {mode}  |  Esc normal  i insert  "
            "v/V visual  dd/dw delete  u undo  Tab complete  Up/Down history "
        )

    session = PromptSession(
        history=history,
        vi_mode=True,
        key_bindings=bindings,
        completer=CommandCompleter(),
        bottom_toolbar=bottom_toolbar,
        complete_while_typing=False,
        multiline=False,
    )

    # After typing a command that takes an argument, append a space so the
    # word can be entered immediately. This uses the insert event (not the
    # text-changed event) so deleting characters never triggers it again.
    def _auto_space(buffer) -> None:
        if buffer.text in {
            "/transcript",
            "/search_transcript",
            "/youtube",
            "/youtube learn",
            "/wiki",
            "/dict",
            "/depth",
        }:
            buffer.cursor_position = len(buffer.text)
            buffer.insert_text(" ", fire_event=False)

    session.default_buffer.on_text_insert += _auto_space
    return session


def history_count(path: Path) -> int:
    try:
        return sum(
            1
            for line in path.read_text(
                encoding="utf-8", errors="replace"
            ).splitlines()
            if line.strip()
        )
    except OSError:
        return 0


def create_embedding(content: str) -> list[float]:
    """Embed one text with Ollama's embedding endpoint."""
    return create_embeddings([content])[0]


def create_embeddings(contents: list[str]) -> list[list[float]]:
    """Embed one or more texts with Ollama in a single request."""
    if not contents:
        return []

    payload = {"model": EMBED_MODEL, "input": contents}
    try:
        response = requests.post(
            f"{OLLAMA_HOST}/api/embed",
            json=payload,
            timeout=(10, 300),
        )
        response.raise_for_status()
        data = response.json()
    except requests.RequestException as exc:
        raise RuntimeError(
            f"Could not reach Ollama at {OLLAMA_HOST}. Is it running? ({exc})"
        ) from exc

    if data.get("error"):
        raise RuntimeError(f"Ollama error: {data['error']}")

    embeddings = data.get("embeddings")
    if not embeddings or len(embeddings) != len(contents):
        raise RuntimeError("Ollama returned an unexpected number of embeddings")
    if any(not embedding for embedding in embeddings):
        raise RuntimeError("Ollama returned an empty embedding")
    return [[float(value) for value in embedding] for embedding in embeddings]


def paragraph_text(chunk: Chunk) -> str:
    """Return only the paragraph body of a trunk, excluding its shared title."""
    try:
        content = chunk.trunk_path.read_text(encoding="utf-8")
    except OSError:
        return ""
    match = PARAGRAPH_PATTERN.search(content)
    return match.group(1).strip() if match else content.strip()


def corpus_word_counts(chunks: list[Chunk]) -> Counter[str]:
    """Return word frequencies for the loaded trunks (cached per chunk set)."""
    global _WORD_COUNTS_KEY, _WORD_COUNTS

    key = tuple(chunk.label for chunk in chunks)
    if _WORD_COUNTS_KEY == key:
        return _WORD_COUNTS

    counts: Counter[str] = Counter()
    for chunk in chunks:
        try:
            content = chunk.trunk_path.read_text(
                encoding="utf-8", errors="replace"
            )
        except OSError:
            continue
        counts.update(word.lower() for word in WORD_PATTERN.findall(content))

    _WORD_COUNTS_KEY = key
    _WORD_COUNTS = counts
    return counts


def split_compound_word(word: str, counts: Counter[str]) -> str:
    """Insert a space into an unknown compound word using corpus vocabulary.

    A missing space such as ``redlight`` changes the embedding of an otherwise
    identical question (``red light``), which can change retrieval and even
    flip the final answer. If the word itself is unknown but can be split into
    two words that both occur in the corpus, use that split. Proper nouns and
    genuinely unknown words are left untouched.
    """
    lower = word.lower()
    if lower in counts or len(lower) < 6:
        return word

    best_score = 0
    best_split: tuple[str, str] | None = None
    # Require parts of at least three characters so that short proper nouns
    # (Osaka, Kobe, Nara) are not split into accidental word fragments.
    for index in range(3, len(lower) - 2):
        left, right = lower[:index], lower[index:]
        if counts[left] < 2 or counts[right] < 2:
            continue
        # Prefer a split whose parts are both common, which avoids accidental
        # splits of names into rare fragments.
        score = counts[left] * counts[right]
        if score > best_score:
            best_score = score
            best_split = (left, right)

    if best_split is None:
        return word
    return " ".join(best_split)


def normalize_question_text(question: str, chunks: list[Chunk]) -> str:
    """Normalize spelling and word boundaries before embedding the question."""
    counts = corpus_word_counts(chunks)
    normalized = WORD_PATTERN.sub(
        lambda match: split_compound_word(match.group(0), counts), question
    )
    normalized = re.sub(r"\s+", " ", normalized).strip()
    return normalized or question


def rerank_on_paragraphs(
    question_embedding: list[float],
    candidates: list[tuple[Chunk, float]],
) -> list[tuple[Chunk, float]]:
    """Re-score shortlisted chunks using paragraph-only embeddings.

    The stored embeddings cover the whole trunk, including a title shared by
    every chunk of a video. That title can dominate cosine similarity and make
    one video fill the whole context, hiding a chunk from another video that
    actually answers the question. Re-embedding just the paragraphs removes
    that bias. If Ollama cannot embed them, keep the original ranking.
    """
    texts = [paragraph_text(chunk) for chunk, _ in candidates]
    indexed_texts = [
        (index, text) for index, text in enumerate(texts) if text
    ]
    if not indexed_texts:
        return candidates

    try:
        embeddings = create_embeddings([text for _, text in indexed_texts])
    except RuntimeError as exc:
        print(f"Warning: paragraph re-ranking unavailable: {exc}", file=sys.stderr)
        return candidates

    embedding_by_index = {
        index: embedding
        for (index, _), embedding in zip(indexed_texts, embeddings)
    }
    question_norm = math.sqrt(
        math.fsum(value * value for value in question_embedding)
    )
    reranked: list[tuple[Chunk, float]] = []
    for index, (chunk, fallback_score) in enumerate(candidates):
        embedding = embedding_by_index.get(index)
        if embedding is None:
            reranked.append((chunk, fallback_score))
            continue
        norm = math.sqrt(math.fsum(value * value for value in embedding))
        if norm == 0.0:
            reranked.append((chunk, fallback_score))
            continue
        score = math.fsum(
            q * value for q, value in zip(question_embedding, embedding)
        ) / (question_norm * norm)
        reranked.append((chunk, score))

    reranked.sort(key=lambda item: item[1], reverse=True)
    return reranked


def top_chunks(
    question_embedding: list[float],
    chunks: list[Chunk],
    min_similarity: float = MIN_SIMILARITY,
    fallback_top_n: int = FALLBACK_TOP_N,
    limit: int | None = None,
) -> list[tuple[Chunk, float]]:
    """Return (chunk, cosine similarity) pairs above the threshold, best first.

    The stored embeddings are used only to build a shortlist; final scores come
    from paragraph-only embeddings. When fewer than ``fallback_top_n`` chunks
    clear the threshold, return the best ``fallback_top_n`` chunks instead so
    the model always gets enough context. This includes any chunks above the
    threshold, since they always rank ahead of the rest.

    When ``limit`` is set (the /depth command), return exactly the best
    ``limit`` chunks, bypassing the threshold/fallback rule.
    """
    if len(question_embedding) != len(chunks[0].embedding):
        raise RuntimeError(
            f"Question embedding has {len(question_embedding)} dimensions, "
            f"but stored embeddings have {len(chunks[0].embedding)}."
        )

    norm = math.sqrt(math.fsum(value * value for value in question_embedding))
    if norm == 0.0:
        raise RuntimeError("Question embedding is a zero vector")

    scored = [
        (
            math.fsum(q * value for q, value in zip(question_embedding, chunk.embedding))
            / norm,
            chunk,
        )
        for chunk in chunks
    ]
    scored.sort(key=lambda item: item[0], reverse=True)

    # Titles are shared by every chunk of a video, so the initial ranking can
    # be dominated by a title rather than the paragraph. Re-rank a shortlist on
    # paragraph text alone before applying the similarity threshold.
    shortlist = [(chunk, score) for score, chunk in scored[:RERANK_CANDIDATES]]
    reranked = rerank_on_paragraphs(question_embedding, shortlist)

    if limit is not None:
        return reranked[:limit]

    matches = [
        (chunk, score)
        for chunk, score in reranked
        if score > min_similarity
    ]
    if len(matches) >= fallback_top_n:
        return matches
    return reranked[:fallback_top_n]


def build_context(chunks: list[Chunk]) -> str:
    """Concatenate the retrieved trunk contents."""
    contents = []
    for chunk in chunks:
        try:
            contents.append(chunk.trunk_path.read_text(encoding="utf-8").strip())
        except OSError as exc:
            print(f"Warning: cannot read {chunk.trunk_path.name}: {exc}", file=sys.stderr)
    return "\n\n".join(contents)


def estimate_tokens(text: str) -> int:
    """Return a rough token count for ``text`` without a tokenizer.

    ASCII text averages roughly four characters per token, while each CJK
    character is usually its own token. This is intentionally an estimate;
    it is used only for the context-size report.
    """
    ascii_chars = sum(1 for char in text if ord(char) < 128)
    other_chars = len(text) - ascii_chars
    return (ascii_chars + CHARS_PER_TOKEN - 1) // CHARS_PER_TOKEN + other_chars


def contains_simplified_chinese(text: str) -> bool:
    """Return True when ``text`` looks like Simplified Chinese.

    Japanese text (which uses kana), English-only text, and text that leans
    Traditional Chinese (more traditional-only than simplified-only
    characters) are excluded. Text made only of shared Han characters, such
    as ``你好``, is treated as Simplified Chinese.
    """
    if KANA_PATTERN.search(text) or not HAN_PATTERN.search(text):
        return False
    simplified = sum(char in SIMPLIFIED_ONLY_CHARS for char in text)
    traditional = sum(char in TRADITIONAL_ONLY_CHARS for char in text)
    return simplified >= traditional


def clean_translation(text: str) -> str:
    """Strip model chatter, thinking blocks, labels, and wrapping quotes."""
    text = THINK_PATTERN.sub("", text).strip()
    text = re.sub(
        r"^(?:translation|english|simplified chinese|简体中文|中文)\s*[:：]\s*",
        "",
        text,
        flags=re.IGNORECASE,
    )
    text = text.strip()
    if len(text) >= 2 and text[0] in '"“' and text[-1] in '"”':
        text = text[1:-1].strip()
    return text


def generate_text(
    prompt: str,
    model: str = TRANSLATION_MODEL,
    num_ctx: int = 8192,
) -> str:
    """Send a prompt to Ollama and return the complete (non-streamed) reply."""
    payload = {
        "model": model,
        "prompt": prompt,
        "stream": False,
        "think": False,
        "options": {
            "temperature": 0.1,
            "num_ctx": num_ctx,
        },
    }
    try:
        response = requests.post(
            f"{OLLAMA_HOST}/api/generate",
            json=payload,
            timeout=(10, 300),
        )
        response.raise_for_status()
        data = response.json()
    except requests.RequestException as exc:
        raise RuntimeError(
            f"Could not reach Ollama at {OLLAMA_HOST}. Is it running? ({exc})"
        ) from exc

    if data.get("error"):
        raise RuntimeError(f"Ollama error: {data['error']}")
    return str(data.get("response", ""))


def translate_to_english(text: str, model: str = TRANSLATION_MODEL) -> str:
    """Translate a Simplified Chinese question into English."""
    translation = clean_translation(
        generate_text(
            QUESTION_TRANSLATION_PROMPT.format(question=text), model=model
        )
    )
    if not translation:
        raise RuntimeError("The translation model returned an empty answer")
    return translation


def translate_to_chinese(text: str, model: str = TRANSLATION_MODEL) -> str:
    """Translate English text into Simplified Chinese."""
    translation = clean_translation(
        generate_text(ANSWER_TRANSLATION_PROMPT.format(text=text), model=model)
    )
    if not translation:
        raise RuntimeError("The translation model returned an empty answer")
    return translation


def pop_translation_segment(buffer: str, final: bool = False) -> tuple[str, str]:
    """Split off the next piece of streamed text that can be translated alone."""
    if final:
        return buffer, ""

    # Prefer line boundaries: list items and short lines translate much better
    # when they are not folded into the next paragraph. Wait for one more
    # character after the newline so trailing blank lines join this segment.
    newline = buffer.find("\n")
    if 0 <= newline < TRANSLATION_MAX_CHARS:
        end = newline + 1
        while end < len(buffer) and buffer[end] in "\r\n":
            end += 1
        if end < len(buffer):
            return buffer[:end], buffer[end:]

    if len(buffer) < TRANSLATION_MIN_CHARS:
        return "", buffer

    for match in TRANSLATION_BOUNDARY.finditer(buffer):
        # If the boundary is the very last character we have received, wait
        # for the next token so trailing spaces/newlines join this segment
        # instead of leaking into the next translation.
        if match.end() == len(buffer):
            break
        if match.end() >= TRANSLATION_MIN_CHARS:
            return buffer[: match.end()], buffer[match.end() :]

    if len(buffer) >= TRANSLATION_MAX_CHARS:
        cut = buffer.rfind(" ", 0, TRANSLATION_MAX_CHARS)
        if cut < TRANSLATION_MIN_CHARS:
            cut = TRANSLATION_MAX_CHARS
        return buffer[:cut], buffer[cut:]
    return "", buffer


def stream_answer(
    prompt: str,
    model: str = CHAT_MODEL,
    translate_answer: bool = False,
) -> str:
    """Send the prompt to Ollama and stream the generated answer to stdout.

    When ``translate_answer`` is true the English text is not printed.
    Instead, completed sentence groups are translated with TRANSLATION_MODEL
    and printed as Simplified Chinese while the answer is still streaming.
    The full English answer is always returned.
    """
    payload = {
        "model": model,
        "prompt": prompt,
        "stream": True,
        "think": False,
        "options": {
            "temperature": 0.2,
            "num_ctx": ANSWER_NUM_CTX,
        },
    }

    chunks: list[str] = []
    translation_buffer = ""
    translation_started = False

    def emit_translation(segment: str) -> None:
        nonlocal translation_started
        if not segment.strip():
            return
        if not translation_started:
            print("[translated to Simplified Chinese]\n", flush=True)
            translation_started = True
        try:
            translated = translate_to_chinese(segment)
        except RuntimeError as exc:
            print(
                f"\nWarning: could not translate part of the answer: {exc}",
                file=sys.stderr,
            )
            translated = segment.strip()
        # Small models often drop list markers ("1.", "- ") while translating;
        # put them back so numbered and bulleted answers keep their structure.
        prefix_match = LIST_PREFIX.match(segment)
        if prefix_match and not LIST_PREFIX.match(translated):
            translated = f"{prefix_match.group(1)} {translated}"
        print(translated, end="", flush=True)
        trailing_newlines = re.search(r"(\n+)[ \t]*$", segment)
        if trailing_newlines:
            print("\n" * len(trailing_newlines.group(1)), end="", flush=True)

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
                    chunks.append(token)
                    if translate_answer:
                        translation_buffer += token
                        segment, translation_buffer = pop_translation_segment(
                            translation_buffer
                        )
                        if segment:
                            emit_translation(segment)
                    else:
                        print(token, end="", flush=True)
                if event.get("done"):
                    break
    except requests.RequestException as exc:
        raise RuntimeError(
            f"Could not reach Ollama at {OLLAMA_HOST}. Is it running? ({exc})"
        ) from exc

    if translate_answer:
        if translation_buffer.strip():
            emit_translation(translation_buffer)
        print(flush=True)
    else:
        print(flush=True)
    return "".join(chunks)


def lookup_wikipedia(word: str) -> None:
    """Print the Wikipedia summary for ``word``."""
    word = word.strip()
    if not word:
        print("Usage: /wiki <word>")
        return

    url = WIKI_SUMMARY_URL.format(word=quote(word, safe=""))
    try:
        response = requests.get(url, headers=HTTP_HEADERS, timeout=(10, 30))
    except requests.RequestException as exc:
        print(f"Wikipedia lookup failed: {exc}", file=sys.stderr)
        return

    if response.status_code == 404:
        print(f"Wikipedia: no page found for {word!r}.")
        return
    try:
        response.raise_for_status()
        data = response.json()
    except requests.RequestException as exc:
        print(f"Wikipedia lookup failed: {exc}", file=sys.stderr)
        return
    except ValueError:
        print("Wikipedia lookup failed: response was not valid JSON.", file=sys.stderr)
        return

    if not isinstance(data, dict):
        print("Wikipedia lookup failed: unexpected response.", file=sys.stderr)
        return

    title = data.get("title") or word
    extract = data.get("extract") or data.get("description")
    print(f"\n{title}")
    if extract:
        print(extract)
    else:
        print("No summary is available for this page.")

    page = ((data.get("content_urls") or {}).get("desktop") or {}).get("page")
    if page:
        print(f"Source: {page}")


def lookup_dictionary(word: str) -> None:
    """Print an English dictionary entry for ``word`` from a local model.

    Definitions are generated by Ollama using ``DICT_MODEL``; no external
    dictionary API is contacted.
    """
    word = word.strip()
    if not word:
        print("Usage: /dict <word>")
        return

    print(f"\nDictionary ({DICT_MODEL}):")
    prompt = DICT_PROMPT.format(word=word)
    try:
        stream_answer(prompt, model=DICT_MODEL)
    except RuntimeError as exc:
        print(f"Dictionary lookup failed: {exc}", file=sys.stderr)


def show_latest_answer_usage(word: str, latest_answer: str | None) -> None:
    """Explain how ``word`` is used in the most recent answer, if it appears."""
    word = word.strip()
    if not word or not latest_answer:
        return

    pattern = re.compile(rf"(?<![\w]){re.escape(word)}(?![\w])", re.IGNORECASE)
    if not pattern.search(latest_answer):
        return

    print(f"\nHow {word!r} is used in the latest answer:")
    prompt = DICT_USAGE_PROMPT.format(word=word, latest_answer=latest_answer)
    try:
        stream_answer(prompt)
    except RuntimeError as exc:
        print(f"Could not explain the latest usage: {exc}", file=sys.stderr)


def run_learn_step(script: str, argv: list[str]) -> bool:
    """Run one pipeline script, echoing its output into the chat."""
    process = None
    try:
        # -u keeps the child's output unbuffered so it appears as it happens.
        process = subprocess.Popen(
            [sys.executable, "-u", str(SCRIPT_DIR / script), *argv],
            cwd=SCRIPT_DIR,
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            text=True,
            bufsize=1,
        )
        assert process.stdout is not None
        for line in process.stdout:
            print(line, end="", flush=True)
        returncode = process.wait()
    except KeyboardInterrupt:
        if process is not None:
            process.terminate()
            try:
                process.wait(timeout=5)
            except subprocess.TimeoutExpired:
                process.kill()
                process.wait()
        print("\n/youtube learn interrupted.", file=sys.stderr)
        return False
    except OSError as exc:
        print(f"/youtube learn: could not run {script}: {exc}", file=sys.stderr)
        return False

    if returncode != 0:
        print(
            f"/youtube learn aborted: {script} failed with exit code "
            f"{returncode}",
            file=sys.stderr,
        )
        return False
    return True


def learn_video(value: str) -> str | None:
    """Run the youtube_video_learn.py pipeline for a video hash/URL.

    Returns the normalized video id on success, or None when the input is
    invalid or a pipeline step fails.
    """
    try:
        import youtube_video_learn as learn
    except ImportError as exc:  # pragma: no cover - ships alongside
        print(
            f"/youtube learn is unavailable: cannot import "
            f"youtube_video_learn.py ({exc})",
            file=sys.stderr,
        )
        return None

    try:
        video_id = learn.extract_video_hash(value)
    except ValueError as exc:
        print(f"/youtube learn: {exc}", file=sys.stderr)
        return None

    print(f"/youtube learn: {video_id}")
    for script, script_args in learn.STEPS:
        argv = [arg.replace("<hash>", video_id) for arg in script_args]
        print(f"\n=== {script} {' '.join(argv)} ===", flush=True)
        if not run_learn_step(script, argv):
            return None

    print(f"\n/youtube learn: {video_id} added to the knowledge base.")
    return video_id


def refresh_embeddings(current_chunks: list[Chunk]) -> list[Chunk] | None:
    """Handle /refresh: reload the global embedding corpus."""
    print("Refreshing the global embedding corpus...")
    try:
        new_chunks = load_embeddings()
    except SystemExit as exc:
        print(f"Cannot refresh embeddings: {exc}", file=sys.stderr)
        return None
    current_chunks.clear()
    gc.collect()
    print(embedding_memory_report(new_chunks))
    return new_chunks


def extract_transcript_video_id(value: str) -> str | None:
    """Accept a bare video id or YouTube URL and return the video id."""
    value = value.strip()
    try:
        import youtube_video_learn as learn

        return learn.extract_video_hash(value)
    except (ImportError, ValueError):
        # youtube_video_learn.py may be unavailable; still accept a bare id.
        return value if re.fullmatch(r"[A-Za-z0-9_-]{11}", value) else None


def activate_transcript(
    video_id: str, current_chunks: list[Chunk]
) -> tuple[str, list[Chunk]] | None:
    """Load only one transcript's embeddings and release the old ones."""
    print(f"Switching to transcript {video_id!r}, reloading embeddings...")
    try:
        new_chunks = load_embeddings(video_ids={video_id})
    except SystemExit as exc:
        print(
            f"Cannot switch to transcript {video_id!r}: {exc}",
            file=sys.stderr,
        )
        return None

    # Drop the old embeddings only after the new transcript loaded successfully.
    current_chunks.clear()
    gc.collect()
    print(embedding_memory_report(new_chunks))
    return video_id, new_chunks


def switch_transcript(
    argument: str, current_chunks: list[Chunk]
) -> tuple[str, list[Chunk]] | None:
    """Handle /transcript: pick one video's embeddings and release the rest.

    The argument may be a video id, a YouTube URL, or a keyword. Keywords
    are searched against ./.chat/youtube_transcripts/*.title with ag, and the
    matches are offered as a <hash> <title> hinting list to select from.

    Returns the new video id and chunks, or None when the switch was cancelled
    or failed (in which case the current embeddings stay loaded).
    """
    video_ids = available_video_ids()
    if not video_ids:
        print(f"No transcripts found in {OUTPUT_DIR}.", file=sys.stderr)
        return None

    value = argument.strip()
    try:
        if not value:
            video_id = prompt_for_video_id()
            return activate_transcript(video_id, current_chunks)

        # A bare video id or a YouTube URL still switches directly.
        video_id = extract_transcript_video_id(value)
        if video_id is not None:
            if video_id not in video_ids:
                print(
                    f"No embedding files for transcript {video_id!r}. "
                    "Use /youtube learn first."
                )
                return None
            return activate_transcript(video_id, current_chunks)

        # Otherwise treat the argument as an ag title keyword search.
        return search_and_switch_transcript(value, current_chunks)
    except SystemExit as exc:
        print(f"Transcript switch cancelled: {exc}", file=sys.stderr)
        return None


def search_transcript_titles(keyword: str) -> list[TranscriptMatch]:
    """Search ./.chat/youtube_transcripts/*.title files with ag."""
    keyword = keyword.strip()
    if not keyword:
        return []

    command = [
        "ag",
        "--nocolor",
        "--literal",
        "--vimgrep",
        "-G",
        r"\.title$",
        "--",
        keyword,
        str(OUTPUT_DIR),
    ]
    try:
        result = subprocess.run(command, capture_output=True, text=True)
    except FileNotFoundError:
        print(
            "Cannot search transcripts: the 'ag' (the_silver_searcher) "
            "command was not found.",
            file=sys.stderr,
        )
        return []
    except OSError as exc:
        print(f"Cannot search transcripts with ag: {exc}", file=sys.stderr)
        return []

    # ag exits 1 when there are no matches; anything else is an error.
    if result.returncode not in (0, 1):
        detail = result.stderr.strip() or f"ag exited with {result.returncode}"
        print(f"Transcript search failed: {detail}", file=sys.stderr)
        return []

    matches: dict[str, TranscriptMatch] = {}
    for line in result.stdout.splitlines():
        parsed = AG_MATCH_PATTERN.match(line)
        if not parsed:
            continue
        name = Path(parsed.group("path")).name
        if not name.endswith(".title"):
            continue
        video_id = name[: -len(".title")]
        title = parsed.group("title").strip()
        if video_id and video_id not in matches:
            matches[video_id] = TranscriptMatch(video_id, title)

    return sorted(matches.values(), key=lambda item: item.video_id.lower())


def select_transcript_match(
    matches: list[TranscriptMatch],
) -> TranscriptMatch | None:
    """Show the ag matches as a hinting list and let the user pick one."""
    print(f"\nFound {len(matches)} transcript(s):")
    for number, match in enumerate(matches, start=1):
        print(f"  {number:>4}. {match.video_id}  {match.title}")

    session = PromptSession(
        completer=TranscriptMatchCompleter(matches),
        complete_while_typing=False,
        multiline=False,
    )
    while True:
        try:
            choice = session.prompt(
                "Select transcript (number or id, Tab to complete, "
                "Enter to cancel): "
            ).strip()
        except (EOFError, KeyboardInterrupt):
            print()
            return None
        if not choice:
            print("Transcript selection cancelled.")
            return None

        if choice.isdigit():
            index = int(choice)
            if 1 <= index <= len(matches):
                return matches[index - 1]
            print(f"Choose a number between 1 and {len(matches)}.")
            continue

        exact = [match for match in matches if match.video_id == choice]
        if exact:
            return exact[0]

        by_prefix = [
            match for match in matches if match.video_id.startswith(choice)
        ]
        if len(by_prefix) == 1:
            return by_prefix[0]
        if len(by_prefix) > 1:
            print(
                f"{len(by_prefix)} video ids start with {choice!r}; "
                "type more characters."
            )
            continue

        by_title = [
            match for match in matches if choice.lower() in match.title.lower()
        ]
        if len(by_title) == 1:
            return by_title[0]
        if len(by_title) > 1:
            print(
                f"{len(by_title)} titles contain {choice!r}; "
                "type a number or a video id."
            )
            continue

        print(
            f"No match for {choice!r} in the list. "
            "Enter a number or video id."
        )


def search_and_switch_transcript(
    argument: str, current_chunks: list[Chunk]
) -> tuple[str, list[Chunk]] | None:
    """Handle /search_transcript and /transcript <keyword>."""
    keyword = argument.strip()
    if not keyword:
        print("Usage: /search_transcript <keyword>")
        return None

    matches = search_transcript_titles(keyword)
    if not matches:
        print(f"No transcript titles matched {keyword!r}.")
        return None

    available = set(available_video_ids())
    usable = [match for match in matches if match.video_id in available]
    skipped = len(matches) - len(usable)
    if not usable:
        print(
            f"Found {len(matches)} title match(es) for {keyword!r}, but none "
            "have embeddings. Use /youtube learn first."
        )
        return None
    if skipped:
        print(f"Skipping {skipped} title match(es) without embeddings.")

    match = select_transcript_match(usable)
    if match is None:
        return None
    return activate_transcript(match.video_id, current_chunks)


def retrieval_cap(depth: int | None) -> int:
    """Return the maximum number of trunks gradual retrieval may collect."""
    if depth is None or depth > GRADUAL_MAX_CONTEXT:
        return GRADUAL_MAX_CONTEXT
    return max(1, depth)


def minimum_context_size(question: str, cap: int) -> int:
    """Return how many trunks to gather before trusting a sufficiency verdict.

    Exhaustive-list questions need more material than a single batch for the
    checker to notice missing items, so gather a deterministic minimum first.
    The limit is bounded by the caller's retrieval cap.
    """
    if EXHAUSTIVE_PATTERN.search(question):
        floor = GRADUAL_MIN_EXHAUSTIVE
    elif LIST_PATTERN.search(question):
        floor = GRADUAL_MIN_LIST
    else:
        return 0
    return min(floor, cap)


def context_is_sufficient(
    question: str,
    selected: list[tuple[Chunk, float]],
) -> bool:
    """Ask the chat model whether the gathered context can answer the question."""
    context = build_context([chunk for chunk, _ in selected])
    prompt = SUFFICIENCY_PROMPT.format(context=context, question=question)
    try:
        reply = generate_text(prompt, model=CHAT_MODEL, num_ctx=32768)
    except RuntimeError as exc:
        print(
            f"Warning: sufficiency check failed ({exc}); adding more context.",
            file=sys.stderr,
        )
        return False

    verdict = re.findall(
        r"verdict\s*:\s*(yes|no)\b", clean_translation(reply), re.IGNORECASE
    )
    if verdict:
        return verdict[-1].lower() == "yes"
    # No explicit verdict: be conservative and fetch more context instead of
    # trusting an incidental yes/no elsewhere in the model's checklist.
    return False


def answer(
    question: str,
    chunks: list[Chunk],
    depth: int | None = None,
    translate_answer: bool = False,
) -> tuple[str | None, list[tuple[Chunk, float]]]:
    """Gradually retrieve context, then stream the model's answer.

    Trunks are added GRADUAL_BATCH_SIZE at a time in retrieval order. For
    list/exhaustive questions a minimum amount of context is gathered first;
    after that, each batch is followed by a sufficiency check. Retrieval stops
    as soon as the context is sufficient, when no trunks remain, or when the
    context reaches the retrieval cap.
    """
    cap = retrieval_cap(depth)
    normalized_question = normalize_question_text(question, chunks)
    if normalized_question != question:
        print(f"[normalized question: {normalized_question}]")
        question = normalized_question
    question_embedding = create_embedding(question)
    min_context = minimum_context_size(question, cap)
    ranked = top_chunks(
        question_embedding,
        chunks,
        limit=min(len(chunks), RERANK_CANDIDATES),
    )
    if not ranked:
        print("No trunks available to use as context.")
        return None, []

    print(
        f"[gradual context: up to {cap} trunk(s), "
        f"{GRADUAL_BATCH_SIZE} new trunk(s) per sufficiency check]"
    )
    if min_context:
        print(
            f"[list/exhaustive question: gathering at least {min_context} "
            "trunk(s) before the first sufficiency check]"
        )

    selected: list[tuple[Chunk, float]] = []
    position = 0
    sufficient = False
    while position < len(ranked) and len(selected) < cap:
        batch_size = min(GRADUAL_BATCH_SIZE, cap - len(selected))
        batch = ranked[position : position + batch_size]
        position += batch_size
        selected.extend(batch)
        if len(selected) >= cap or position >= len(ranked):
            break
        if len(selected) < min_context:
            continue
        print(
            f"[context: {len(selected)} trunk(s); "
            "checking whether it is sufficient...]"
        )
        if context_is_sufficient(question, selected):
            sufficient = True
            break
        print("[not sufficient yet; adding more context]")

    if sufficient:
        print(f"[context sufficient with {len(selected)} trunk(s)]")
    elif len(selected) >= cap:
        print(f"[context limited to {len(selected)} trunk(s)]")
    else:
        print(f"[no more trunks; using {len(selected)} trunk(s)]")

    context = build_context([chunk for chunk, _ in selected])
    if not context:
        print("I could not read any matching trunk contents.", file=sys.stderr)
        return None, []

    prompt = PROMPT.format(context=context, question=question)
    print(
        f"[context size: ~{estimate_tokens(context):,} token(s) estimated "
        f"across {len(selected)} trunk(s); "
        f"context window: {ANSWER_NUM_CTX:,} token(s)]"
    )
    response = stream_answer(
        prompt, translate_answer=translate_answer
    )
    return response, selected


def show_sources(sources: list[tuple[Chunk, float]]) -> None:
    """Print the source list for the latest answer."""
    if not sources:
        print("No sources available yet. Ask a question first.")
        return
    print("Sources for the latest answer:")
    for chunk, score in sources:
        print(f"  {chunk.label} ({score:.4f})")


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Interactive RAG chatbot over YouTube transcript embeddings."
    )
    return parser.parse_args(argv)


def main() -> None:
    parse_args()
    chunks = load_embeddings()
    print(embedding_memory_report(chunks))
    transcript: str | None = None
    latest_answer: str | None = None
    latest_sources: list[tuple[Chunk, float]] = []
    depth: int | None = DEFAULT_DEPTH

    history = PlainFileHistory(HISTORY_FILE)
    print(
        f"Chat history: {history_count(HISTORY_FILE)} saved entries "
        f"(questions + commands) in {HISTORY_FILE}"
    )
    session = make_prompt_session(history)
    print(
        f"RAG chatbot ready ({EMBED_MODEL} + {CHAT_MODEL}, "
        f"global corpus, gradual context up to {retrieval_cap(depth)} trunks). "
        f"Type /exit to quit."
    )
    print(
        "Simplified Chinese questions are translated to English "
        f"with {TRANSLATION_MODEL}; answers are translated back as they "
        "stream."
    )
    print(
        "Vim mode: Esc = normal, i = insert, v/V = visual, "
        "dd/dw = delete, u = undo. Up/Down = question + command history."
    )
    print(
        "Commands: /refresh, /transcript <hash|keyword>, "
        "/youtube learn <hash|url>, /wiki <word>, /dict <word>, "
        "/depth <n|auto>, "
        "/memory, /status, /sources, /exit. Tab completes them."
    )

    while True:
        try:
            question = session.prompt("You: ").strip()
        except (EOFError, KeyboardInterrupt):
            print()
            break

        if not question:
            continue
        command = question.lower()
        if command in {"/exit", "/quit", "/q", "exit", "quit"}:
            break
        if question.startswith("/"):
            name, _, argument = question.partition(" ")
            name = name.lower()
            if name == "/memory":
                print(embedding_memory_report(chunks))
            elif name == "/refresh":
                refreshed = refresh_embeddings(chunks)
                if refreshed is not None:
                    chunks = refreshed
                    transcript = None
                    print("Global embedding corpus refreshed.")
            elif name == "/transcript":
                switched = switch_transcript(argument, chunks)
                if switched is not None:
                    transcript, chunks = switched
                    print(
                        f"Now answering only from transcript {transcript!r}."
                    )
            elif name == "/search_transcript":
                switched = search_and_switch_transcript(argument, chunks)
                if switched is not None:
                    transcript, chunks = switched
                    print(
                        f"Now answering only from transcript {transcript!r}."
                    )
            elif name == "/youtube":
                subcommand, _, subargument = argument.partition(" ")
                subcommand = subcommand.strip().lower()
                value = subargument.strip()
                if subcommand != "learn":
                    print(
                        "Usage: /youtube learn <youtube_video_hash|url>"
                    )
                    continue
                if not value:
                    try:
                        value = session.prompt("Video hash or URL: ").strip()
                    except (EOFError, KeyboardInterrupt):
                        print()
                        continue
                    if not value:
                        print(
                            "Usage: /youtube learn "
                            "<youtube_video_hash|url>"
                        )
                        continue

                video_id = learn_video(value)
                if video_id is None:
                    continue

                if transcript is not None:
                    if video_id == transcript:
                        print(
                            f"Reloading embeddings for transcript "
                            f"{transcript!r}..."
                        )
                        try:
                            new_chunks = load_embeddings(
                                video_ids={transcript}
                            )
                        except SystemExit as exc:
                            print(
                                f"Could not reload embeddings: {exc}",
                                file=sys.stderr,
                            )
                        else:
                            chunks.clear()
                            chunks = new_chunks
                            gc.collect()
                            print(embedding_memory_report(chunks))
                    else:
                        print(
                            f"Still answering only from transcript "
                            f"{transcript!r}; the loaded embeddings are "
                            "unchanged."
                        )
                else:
                    print("Reloading the global embedding corpus...")
                    try:
                        new_chunks = load_embeddings()
                    except SystemExit as exc:
                        print(
                            f"Could not reload embeddings: {exc}",
                            file=sys.stderr,
                        )
                    else:
                        chunks.clear()
                        chunks = new_chunks
                        gc.collect()
                        print(embedding_memory_report(chunks))
            elif name == "/wiki":
                lookup_wikipedia(argument)
            elif name == "/dict":
                lookup_dictionary(argument)
                show_latest_answer_usage(argument, latest_answer)
            elif name == "/status":
                print(
                    chat_status_report(transcript, chunks, depth)
                )
            elif name == "/sources":
                show_sources(latest_sources)
            elif name == "/depth":
                value = argument.strip().lower()
                if not value:
                    print(
                        f"Retrieval cap: {retrieval_cap(depth)} trunk(s) "
                        f"({GRADUAL_BATCH_SIZE} new trunk(s) per check)"
                    )
                    print("Usage: /depth <maximum trunks> | /depth auto")
                    print(f"Default depth: {DEFAULT_DEPTH}")
                elif value in {"auto", "default", "0"}:
                    depth = None
                    print(
                        "Retrieval cap: default "
                        f"(up to {GRADUAL_MAX_CONTEXT} trunks, "
                        f"{GRADUAL_BATCH_SIZE} per sufficiency check)."
                    )
                else:
                    try:
                        new_depth = int(value)
                    except ValueError:
                        new_depth = 0
                    if new_depth < 1:
                        print(
                            f"Invalid depth: {argument.strip()!r}. "
                            "Use a positive integer, or 'auto'."
                        )
                    else:
                        depth = min(new_depth, GRADUAL_MAX_CONTEXT)
                        print(
                            f"Retrieval cap: up to {depth} trunk(s) "
                            f"({GRADUAL_BATCH_SIZE} per sufficiency check)."
                        )
            else:
                print(
                    f"Unknown command: {name}. "
                    "Try /refresh, /transcript <hash|keyword>, "
                    "/youtube learn <hash|url>, /wiki <word>, "
                    "/dict <word>, "
                    "/depth <n|auto>, /memory, /status, /sources, /exit."
                )
            continue

        translate_answer = False
        if contains_simplified_chinese(question):
            print("[translating the question to English...]", flush=True)
            try:
                english_question = translate_to_english(question)
            except RuntimeError as exc:
                print(
                    f"Error: could not translate the question: {exc}",
                    file=sys.stderr,
                )
                continue
            print(f"[translated question: {english_question}]")
            question = english_question
            translate_answer = True

        print("\nAssistant: ", end="", flush=True)
        try:
            response, sources = answer(
                question, chunks, depth=depth, translate_answer=translate_answer
            )
        except Exception as exc:
            print(f"\nError: {exc}", file=sys.stderr)
        else:
            if response is not None:
                latest_answer = response
                latest_sources = sources


if __name__ == "__main__":
    main()
