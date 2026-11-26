"""MoonBit documentation search for the /moonbit chat mode."""

from __future__ import annotations

from .core import *

MOONBIT_DOC_BASE = "https://docs.moonbitlang.com/en/latest/"
MOONBIT_SEARCH_INDEX_URL = MOONBIT_DOC_BASE + "searchindex.js"
MOONBIT_USER_AGENT = (
    "Mozilla/5.0 (compatible; local-llm-chat-moonbit/1.0)"
)
MOONBIT_MODEL = chat.ANSWER_MODEL
MOONBIT_NUM_CTX = chat.ANSWER_NUM_CTX
MOONBIT_TOP_DOCS = 3
MOONBIT_PAGE_BUDGET_CHARS = 8_000
MOONBIT_CONTEXT_BUDGET_CHARS = 26_000
MOONBIT_INDEX_TTL_SECONDS = 6 * 60 * 60
MOONBIT_PAGE_TTL_SECONDS = 60 * 60

MOONBIT_STOPWORDS = {
    "a", "an", "the", "is", "are", "was", "were", "be", "been", "being",
    "to", "of", "and", "or", "for", "in", "on", "with", "how", "what",
    "why", "when", "where", "which", "who", "can", "i", "you", "it",
    "this", "that", "does", "do", "use", "using", "example", "code",
    "sample", "samples", "show", "me", "give", "please", "explain",
    "difference", "between", "about", "from", "as", "at", "by", "into",
    "like", "via", "vs", "want", "need", "would", "should", "could",
    "work", "works", "working",
}

MOONBIT_ANSWER_PROMPT = """You are a MoonBit programming language expert. Answer the question using the official MoonBit documentation excerpts below.

Requirements:
- Explain the concept clearly, in a way a programmer new to MoonBit can follow.
- Include complete MoonBit code examples when they help answer the question.
- Use MoonBit code examples only; do not include code from other languages.
- Do not invent APIs, syntax, or behavior that the excerpts do not support.
- If the excerpts do not contain the answer, say so clearly and name what is missing.
- Use Markdown. End with a short "Sources" list containing the documentation URL(s) you used.

<documentation>
{context}
</documentation>

{conversation}Question: {question}

Answer:"""

_MOONBIT_INDEX_LOCK = threading.Lock()
_MOONBIT_INDEX_CACHE: dict | None = None
_MOONBIT_INDEX_LOADED_AT = 0.0
_MOONBIT_PAGE_LOCK = threading.Lock()
_MOONBIT_PAGE_CACHE: dict[str, tuple[float, list]] = {}


def _moonbit_get(url: str) -> str:
    try:
        response = requests.get(
            url,
            headers={"User-Agent": MOONBIT_USER_AGENT},
            timeout=30,
        )
        response.raise_for_status()
    except requests.RequestException as exc:
        raise RuntimeError(
            f"Could not fetch {url}: {exc}"
        ) from exc
    return response.text


def _as_doc_list(value: object) -> list[int]:
    if isinstance(value, list):
        return [int(item) for item in value if isinstance(item, int)]
    if isinstance(value, int):
        return [value]
    return []


def _clean_moonbit_title(title: object) -> str:
    text = html.unescape(re.sub(r"<[^>]+>", " ", str(title or "")))
    return " ".join(text.split())


def _moonbit_index() -> dict:
    global _MOONBIT_INDEX_CACHE, _MOONBIT_INDEX_LOADED_AT
    now = time.monotonic()
    with _MOONBIT_INDEX_LOCK:
        if (
            _MOONBIT_INDEX_CACHE is not None
            and now - _MOONBIT_INDEX_LOADED_AT < MOONBIT_INDEX_TTL_SECONDS
        ):
            return _MOONBIT_INDEX_CACHE
        raw = _moonbit_get(MOONBIT_SEARCH_INDEX_URL)
        match = re.match(r"Search\.setIndex\((.*)\)\s*;?\s*$", raw, re.S)
        if not match:
            raise RuntimeError("The MoonBit search index has an unknown format.")
        try:
            data = json.loads(match.group(1))
        except json.JSONDecodeError as exc:
            raise RuntimeError(
                "Could not parse the MoonBit search index."
            ) from exc
        docnames = [str(item) for item in data.get("docnames", [])]
        titles = [
            _clean_moonbit_title(item) for item in data.get("titles", [])
        ]
        urls = [
            MOONBIT_DOC_BASE + quote(docname, safe="/") + ".html"
            for docname in docnames
        ]
        _MOONBIT_INDEX_CACHE = {
            "docnames": docnames,
            "titles": titles,
            "urls": urls,
            "terms": data.get("terms") or {},
            "titleterms": data.get("titleterms") or {},
            "alltitles": data.get("alltitles") or {},
        }
        _MOONBIT_INDEX_LOADED_AT = now
        return _MOONBIT_INDEX_CACHE


def _moonbit_token_variants(token: str) -> set[str]:
    token = token.lower()
    variants = {token}
    if len(token) > 3 and token.endswith("s"):
        variants.add(token[:-1])
    if len(token) > 4 and token.endswith("es"):
        variants.add(token[:-2])
    if len(token) > 5 and token.endswith("ed"):
        variants.add(token[:-2])
    if len(token) > 5 and token.endswith("ing"):
        variants.add(token[:-3])
    if len(token) > 5 and token.endswith("ly"):
        variants.add(token[:-2])
    return variants


def moonbit_question_terms(question: str) -> list[str]:
    terms: list[str] = []
    seen: set[str] = set()
    for token in re.findall(r"[A-Za-z_][A-Za-z0-9_']*", question.lower()):
        if len(token) < 2 or token in MOONBIT_STOPWORDS:
            continue
        if token in seen:
            continue
        seen.add(token)
        terms.append(token)
    return terms


def _moonbit_rank_docs(
    index: dict, terms: list[str], limit: int = MOONBIT_TOP_DOCS
) -> list[dict]:
    scores: dict[int, float] = {}
    titleterms = index["titleterms"]
    bodyterms = index["terms"]
    alltitles = index["alltitles"]
    titles = index["titles"]
    docnames = index["docnames"]

    def add(doc_index: int, score: float) -> None:
        scores[doc_index] = scores.get(doc_index, 0.0) + score

    for term in terms:
        for variant in _moonbit_token_variants(term):
            for doc_index in _as_doc_list(bodyterms.get(variant, [])):
                add(doc_index, 1.0)
            for doc_index in _as_doc_list(titleterms.get(variant, [])):
                add(doc_index, 8.0)
            for title, entries in alltitles.items():
                if variant not in title.lower():
                    continue
                for entry in entries:
                    if isinstance(entry, list) and entry:
                        add(int(entry[0]), 12.0)
        for doc_index, title in enumerate(titles):
            if term in title.lower():
                add(doc_index, 4.0)
        for doc_index, docname in enumerate(docnames):
            if term in docname.lower():
                add(doc_index, 2.0)

    for doc_index, docname in enumerate(docnames):
        if docname.startswith("language/"):
            add(doc_index, 15.0)
        elif docname.startswith("tutorial/"):
            add(doc_index, 2.0)
        elif docname.startswith("example/"):
            add(doc_index, -5.0)

    overview_query = set(terms) <= {
        "moonbit", "language", "programming", "introduction", "overview",
    }
    if overview_query:
        for doc_index, docname in enumerate(docnames):
            if docname == "index":
                add(doc_index, 25.0)
            elif docname == "language/index":
                add(doc_index, 20.0)
            elif docname == "tutorial/tour":
                add(doc_index, 10.0)

    if not scores:
        return []
    ranked = sorted(
        scores.items(), key=lambda item: (-item[1], titles[item[0]])
    )
    return [
        {
            "title": titles[doc_index],
            "docname": docnames[doc_index],
            "url": index["urls"][doc_index],
            "score": score,
        }
        for doc_index, score in ranked[:limit]
    ]


class _MoonbitArticleParser(HTMLParser):
    """Collect headings, paragraphs, list items, and code from a docs page."""

    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.stack: list[tuple[str, dict]] = []
        self.in_article = False
        self.article_level = 0
        self.blocks: list[tuple] = []
        self.kind: str | None = None
        self.block_tag: str | None = None
        self.heading_level = 0
        self.buffer: list[str] = []
        self.in_pre = False

    def _skipping(self) -> bool:
        for tag, attrs in self.stack:
            classes = attrs.get("class") or ""
            if tag in {"script", "style", "button"} or "headerlink" in classes:
                return True
        return False

    def _flush(self) -> None:
        if not self.kind:
            return
        text = "".join(self.buffer)
        if self.kind == "code":
            text = text.strip("\n")
            if text.strip():
                self.blocks.append(("code", text))
        else:
            text = " ".join(text.split())
            if text:
                if self.kind == "heading":
                    self.blocks.append(
                        ("heading", text, self.heading_level)
                    )
                else:
                    self.blocks.append(("text", text))
        self.kind = None
        self.block_tag = None
        self.buffer = []

    def handle_starttag(self, tag: str, attrs) -> None:
        attrs_dict = dict(attrs)
        classes = attrs_dict.get("class") or ""
        self.stack.append((tag, attrs_dict))
        if not self.in_article:
            if tag == "article" and "bd-article" in classes:
                self.in_article = True
                self.article_level = len(self.stack)
            return
        if self.in_pre:
            return
        if tag == "pre":
            self._flush()
            self.kind = "code"
            self.block_tag = "pre"
            self.buffer = []
            self.in_pre = True
            return
        if tag in {"h1", "h2", "h3", "h4", "h5", "h6"}:
            self._flush()
            self.kind = "heading"
            self.block_tag = tag
            self.heading_level = int(tag[1])
            self.buffer = []
            return
        if tag in {"p", "li", "td", "th", "dt", "dd"} and self.kind is None:
            self.kind = "text"
            self.block_tag = tag
            self.buffer = []

    def handle_endtag(self, tag: str) -> None:
        if not self.in_article:
            if self.stack:
                self.stack.pop()
            return
        if self.in_pre:
            if tag == "pre":
                self._flush()
                self.in_pre = False
        elif self.kind and tag == self.block_tag:
            self._flush()
        if tag == "article" and len(self.stack) == self.article_level:
            self._flush()
            self.in_article = False
        if self.stack:
            self.stack.pop()

    def handle_data(self, data: str) -> None:
        if not self.in_article:
            return
        if self._skipping() and not self.in_pre:
            return
        if self.kind:
            self.buffer.append(data)


def _moonbit_page_blocks(url: str) -> list[tuple]:
    now = time.monotonic()
    with _MOONBIT_PAGE_LOCK:
        cached = _MOONBIT_PAGE_CACHE.get(url)
        if cached and now - cached[0] < MOONBIT_PAGE_TTL_SECONDS:
            return cached[1]
    raw = _moonbit_get(url)
    parser = _MoonbitArticleParser()
    parser.feed(raw)
    parser.close()
    blocks = parser.blocks
    with _MOONBIT_PAGE_LOCK:
        _MOONBIT_PAGE_CACHE[url] = (now, blocks)
        if len(_MOONBIT_PAGE_CACHE) > 120:
            oldest = sorted(
                _MOONBIT_PAGE_CACHE.items(), key=lambda item: item[1][0]
            )[:20]
            for cached_url, _ in oldest:
                _MOONBIT_PAGE_CACHE.pop(cached_url, None)
    return blocks


def _moonbit_sections(blocks: list[tuple]) -> list[dict]:
    sections: list[dict] = []
    current: dict | None = None
    for block in blocks:
        if block[0] == "heading":
            if current is not None:
                sections.append(current)
            current = {"heading": block, "blocks": []}
        else:
            if current is None:
                current = {"heading": ("heading", "", 0), "blocks": []}
            current["blocks"].append(block)
    if current is not None:
        sections.append(current)
    return sections


def _moonbit_select_sections(
    blocks: list[tuple], terms: list[str], budget: int
) -> str:
    variants: list[str] = []
    seen: set[str] = set()
    for term in terms:
        for variant in _moonbit_token_variants(term):
            if variant not in seen:
                seen.add(variant)
                variants.append(variant)
    sections = _moonbit_sections(blocks)
    scored: list[tuple[int, float]] = []
    for index, section in enumerate(sections):
        heading = str(section["heading"][1]).lower()
        body = " ".join(str(block[1]) for block in section["blocks"]).lower()
        score = 0.0
        for variant in variants:
            if variant in heading:
                score += 20.0
            score += body.count(variant)
        present = sum(
            1 for variant in variants if variant in heading or variant in body
        )
        score += present * 10.0
        if score:
            scored.append((index, score))
    if scored:
        chosen = sorted(
            index
            for index, _ in sorted(
                scored, key=lambda item: (-item[1], item[0])
            )[:3]
        )
    else:
        chosen = list(range(min(3, len(sections))))

    parts: list[str] = []
    used = 0
    for index in chosen:
        section = sections[index]
        heading = str(section["heading"][1]).strip()
        if heading:
            parts.append(f"## {heading}")
            used += len(heading) + 4
        for block in section["blocks"]:
            if block[0] == "code":
                text = "```moonbit\n" + str(block[1])[:2500] + "\n```"
            else:
                text = str(block[1])
            if parts and used + len(text) > budget:
                break
            parts.append(text)
            used += len(text) + 2
        if used >= budget:
            break
    return "\n\n".join(parts)


def moonbit_docs_context(
    question: str,
    max_pages: int = MOONBIT_TOP_DOCS,
    per_page_budget: int = MOONBIT_PAGE_BUDGET_CHARS,
    total_budget: int = MOONBIT_CONTEXT_BUDGET_CHARS,
) -> tuple[str, list[dict]]:
    terms = moonbit_question_terms(question)
    index = _moonbit_index()
    results = _moonbit_rank_docs(index, terms, limit=max_pages)
    if not results:
        return "", []

    page_blocks: dict[str, list[tuple]] = {}
    with ThreadPoolExecutor(max_workers=min(4, len(results))) as pool:
        futures = {
            pool.submit(_moonbit_page_blocks, result["url"]): result
            for result in results
        }
        for future in as_completed(futures):
            result = futures[future]
            try:
                page_blocks[result["url"]] = future.result()
            except Exception:
                continue

    context_parts: list[str] = []
    sources: list[dict] = []
    used = 0
    for result in results:
        blocks = page_blocks.get(result["url"])
        if not blocks:
            continue
        excerpt = _moonbit_select_sections(blocks, terms, per_page_budget)
        if not excerpt:
            continue
        part = (
            f"### {result['title']}\n"
            f"URL: {result['url']}\n\n{excerpt}"
        )
        if context_parts and used + len(part) > total_budget:
            break
        context_parts.append(part)
        used += len(part) + 2
        sources.append(
            {"title": result["title"], "url": result["url"]}
        )
    return "\n\n".join(context_parts), sources


def moonbit_answer_prompt(
    question: str, context: str, conversation: str = ""
) -> str:
    conversation_block = (
        f"Recent conversation:\n{conversation}\n\n" if conversation else ""
    )
    return MOONBIT_ANSWER_PROMPT.format(
        context=context,
        conversation=conversation_block,
        question=question,
    )
