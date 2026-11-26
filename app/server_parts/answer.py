"""Answer block splitting and source annotations."""

from __future__ import annotations

from .core import *

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


# Cross-component imports are placed after the definitions so a
# feature module can depend on another feature module safely.
from .retrieval import chunk_is_news, chunk_title, chunk_url
