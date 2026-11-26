"""Stack Overflow search and answer parsing."""

from __future__ import annotations

from .core import *

# /stackoverflow tag <tag> and /stackoverflow search <keywords> query the
# public Stack Exchange API and render the questions as a /news-style result
# list. Each card carries the question body, score, answer count, tags, and a
# link to the question on stackoverflow.com.
STACKOVERFLOW_API_URL = "https://api.stackexchange.com/2.3"

STACKOVERFLOW_SITE = "stackoverflow"

STACKOVERFLOW_BASE_URL = "https://stackoverflow.com"

STACKOVERFLOW_RESULT_LIMIT = 10

STACKOVERFLOW_BODY_PARAGRAPH_LIMIT = 24

STACKOVERFLOW_TAG_LIMIT = 5

STACKOVERFLOW_ANSWER_LIMIT = 8

class _StackOverflowBodyParser(HTMLParser):
    """Turn one Stack Overflow HTML body into ordered reader blocks.

    Unlike a plain-text conversion, this keeps ``<pre><code>`` snippets as
    separate blocks with their language hint. The browser renders those blocks
    as fenced code, so answers keep their indentation and can be copied.
    """

    _HEADINGS = {f"h{level}": level for level in range(1, 7)}
    _BLOCK_TAGS = {
        "address",
        "article",
        "aside",
        "blockquote",
        "div",
        "dl",
        "dt",
        "dd",
        "figcaption",
        "figure",
        "hr",
        "p",
        "section",
        "table",
        "tr",
    }

    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.blocks: list[dict] = []
        self._text: list[str] = []
        self._heading_level = 0
        self._heading_text: list[str] = []
        self._in_pre = False
        self._pre_text: list[str] = []
        self._pre_language = ""
        self._list_stack: list[dict] = []
        self._li_text: list[str] | None = None

    @staticmethod
    def _normalize(value: str) -> str:
        text = (
            str(value or "")
            .replace("\r\n", "\n")
            .replace("\r", "\n")
            .replace("\xa0", " ")
        )
        text = re.sub(r"[ \t\f\v]+", " ", text)
        text = re.sub(r" *\n *", "\n", text)
        text = re.sub(r"\n{3,}", "\n\n", text)
        return text.strip()

    @staticmethod
    def _code_language(attrs: list[tuple[str, str | None]]) -> str:
        merged = " ".join(
            str(value or "")
            for name, value in attrs
            if name in {"class", "data-lang", "lang"}
        )
        match = re.search(
            r"(?:^|\s)(?:lang(?:uage)?-)([A-Za-z0-9_+#.-]+)", merged
        )
        return match.group(1).lower() if match else ""

    def _target(self) -> list[str]:
        if self._heading_level:
            return self._heading_text
        if self._li_text is not None:
            return self._li_text
        return self._text

    def _append(self, text: str) -> None:
        if text:
            self._target().append(text)

    def _flush_text(self) -> None:
        text = self._normalize("".join(self._text))
        self._text.clear()
        if text:
            self.blocks.append({"type": "paragraph", "text": text})

    def _flush_heading(self) -> None:
        text = self._normalize("".join(self._heading_text))
        level = self._heading_level
        self._heading_text.clear()
        self._heading_level = 0
        if text and level:
            self.blocks.append(
                {"type": "heading", "level": level, "text": text}
            )

    def _flush_li(self) -> None:
        if self._li_text is None:
            return
        text = self._normalize("".join(self._li_text))
        self._li_text = None
        if text and self._list_stack:
            self._list_stack[-1]["items"].append(text)

    def _flush_pre(self) -> None:
        text = (
            "".join(self._pre_text)
            .replace("\r\n", "\n")
            .replace("\r", "\n")
            .strip("\n")
        )
        language = self._pre_language
        self._pre_text.clear()
        self._pre_language = ""
        self._in_pre = False
        if text:
            self.blocks.append(
                {"type": "code", "text": text, "language": language}
            )

    def handle_starttag(
        self, tag: str, attrs: list[tuple[str, str | None]]
    ) -> None:
        tag = tag.lower()
        if tag == "pre":
            self._flush_text()
            self._in_pre = True
            self._pre_text.clear()
            self._pre_language = self._code_language(attrs)
            return
        if self._in_pre:
            if tag == "code":
                language = self._code_language(attrs)
                if language:
                    self._pre_language = language
            return
        if tag == "br":
            self._append("\n")
            return
        if tag in self._HEADINGS:
            self._flush_text()
            self._heading_level = self._HEADINGS[tag]
            self._heading_text.clear()
            return
        if tag in {"ul", "ol"}:
            self._flush_li()
            self._flush_text()
            self._list_stack.append(
                {"ordered": tag == "ol", "items": []}
            )
            return
        if tag == "li":
            self._flush_li()
            self._flush_text()
            self._li_text = []
            return
        if tag in self._BLOCK_TAGS:
            self._flush_text()
            return
        if tag in {"td", "th"}:
            self._append(" ")

    def handle_endtag(self, tag: str) -> None:
        tag = tag.lower()
        if tag == "pre":
            if self._in_pre:
                self._flush_pre()
            return
        if self._in_pre:
            return
        if tag in self._HEADINGS:
            self._flush_heading()
            return
        if tag == "li":
            self._flush_li()
            return
        if tag in {"ul", "ol"}:
            self._flush_li()
            if not self._list_stack:
                return
            context = self._list_stack.pop()
            items = [item for item in context["items"] if item]
            if not items:
                return
            if self._list_stack:
                # Keep nested lists inside the parent item so the reader
                # still sees them in source order without a broken list.
                marker = "1. " if context["ordered"] else "- "
                parent_items = self._list_stack[-1]["items"]
                nested = "\n".join(marker + item for item in items)
                if parent_items:
                    parent_items[-1] += "\n" + nested
                else:
                    parent_items.append(nested)
            else:
                self.blocks.append(
                    {
                        "type": "list",
                        "ordered": bool(context["ordered"]),
                        "items": items,
                    }
                )
            return
        if tag in self._BLOCK_TAGS:
            self._flush_text()

    def handle_data(self, data: str) -> None:
        if not data:
            return
        if self._in_pre:
            self._pre_text.append(data)
        else:
            self._append(data)

    def close(self) -> None:
        super().close()
        if self._in_pre:
            self._flush_pre()
        self._flush_li()
        while self._list_stack:
            context = self._list_stack.pop()
            items = [item for item in context["items"] if item]
            if items:
                self.blocks.append(
                    {
                        "type": "list",
                        "ordered": bool(context["ordered"]),
                        "items": items,
                    }
                )
        self._flush_heading()
        self._flush_text()

def _stackoverflow_body_blocks(value: object) -> list[dict]:
    """Parse one Stack Overflow HTML body into headings, text, lists, code."""
    parser = _StackOverflowBodyParser()
    try:
        parser.feed(str(value or ""))
        parser.close()
    except Exception:  # pragma: no cover - defensive HTML fallback
        fallback = _stackoverflow_plain_text(value)
        return [{"type": "paragraph", "text": fallback}] if fallback else []
    return parser.blocks

def _stackoverflow_plain_text(value: object) -> str:
    """Turn one Stack Overflow HTML title or question body into plain text."""
    text = str(value or "")
    text = re.sub(r"(?i)<br\s*/?>", "\n", text)
    text = re.sub(r"(?i)<li[^>]*>", "• ", text)
    text = re.sub(
        r"(?i)</(?:p|div|li|tr|h[1-6]|blockquote|pre|table)\s*>",
        "\n\n",
        text,
    )
    text = re.sub(r"(?i)<[^>]+>", "", text)
    text = html.unescape(text).replace("\xa0", " ")
    text = re.sub(r"[ \t]+", " ", text)
    text = re.sub(r"\n[ \t]+", "\n", text)
    text = re.sub(r"\n{3,}", "\n\n", text)
    return text.strip()

def _stackoverflow_stats(item: dict) -> str:
    """Summarize one question's score, answers, and tags for the modal."""
    parts: list[str] = []
    if item.get("score") is not None:
        parts.append(f"Score {int(item.get('score') or 0)}")
    answers = int(item.get("answer_count") or 0)
    if answers:
        accepted = " (accepted)" if item.get("accepted_answer_id") else ""
        parts.append(f"{answers} answer{'' if answers == 1 else 's'}{accepted}")
    else:
        parts.append("No answers yet")
    owner = item.get("owner")
    if isinstance(owner, dict) and owner.get("display_name"):
        parts.append(f"asked by {owner['display_name']}")
    tags = [str(tag) for tag in item.get("tags") or [] if str(tag).strip()]
    if tags:
        parts.append("Tags: " + ", ".join(tags[:STACKOVERFLOW_TAG_LIMIT * 2]))
    return " · ".join(parts)

def _stackoverflow_iso_time(timestamp: object) -> str:
    """Convert a Stack Exchange epoch-seconds value to an ISO UTC string."""
    try:
        seconds = int(timestamp)  # type: ignore[arg-type]
    except (TypeError, ValueError):
        return ""
    if seconds <= 0:
        return ""
    return time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(seconds))

def search_stackoverflow_questions(
    query: str = "",
    tagged: str = "",
    limit: int = STACKOVERFLOW_RESULT_LIMIT,
) -> list[dict]:
    """Search Stack Overflow by tag or keywords and return result cards.

    ``tagged`` selects the API's /questions endpoint sorted by creation time,
    matching the prompt's tag_search example. ``query`` selects the advanced
    search endpoint, also sorted by newest first, matching the prompt's
    keyword_search example.
    """
    query = " ".join(str(query or "").split())
    tag_value = ";".join(
        tag for tag in re.split(r"[\s,;]+", str(tagged or "").strip()) if tag
    )
    if not query and not tag_value:
        return []
    try:
        pagesize = max(1, min(int(limit), 100))
    except (TypeError, ValueError):
        pagesize = STACKOVERFLOW_RESULT_LIMIT

    if tag_value:
        endpoint = "questions"
        params = {
            "tagged": tag_value,
            "order": "desc",
            "sort": "creation",
        }
        feed = f"{STACKOVERFLOW_BASE_URL}/questions/tagged/{quote(tag_value, safe=';')}"
    else:
        endpoint = "search/advanced"
        params = {
            "q": query,
            "order": "desc",
            "sort": "creation",
        }
        feed = (
            f"{STACKOVERFLOW_BASE_URL}/search?q={quote_plus(query)}&tab=Newest"
        )
    params.update(
        {
            "site": STACKOVERFLOW_SITE,
            "pagesize": str(pagesize),
            "filter": "withbody",
        }
    )

    try:
        response = requests.get(
            f"{STACKOVERFLOW_API_URL}/{endpoint}",
            params=params,
            headers={
                **chat.HTTP_HEADERS,
                "Accept": "application/json",
                "Accept-Encoding": "gzip, deflate",
            },
            timeout=(10, 30),
        )
        response.raise_for_status()
        data = response.json()
    except (requests.RequestException, ValueError) as exc:
        raise RuntimeError(f"Stack Overflow search failed: {exc}") from exc

    if not isinstance(data, dict):
        raise RuntimeError("Stack Overflow returned an unexpected response.")
    if data.get("error_message"):
        raise RuntimeError(
            f"Stack Overflow API error: {data['error_message']}"
        )
    items = data.get("items")
    if not isinstance(items, list):
        raise RuntimeError("Stack Overflow returned an unexpected response.")

    results: list[dict] = []
    for item in items[:pagesize]:
        if not isinstance(item, dict):
            continue
        link = str(item.get("link") or "").strip()
        title = _stackoverflow_plain_text(item.get("title")) or "(untitled)"
        if not link:
            question_id = item.get("question_id")
            if question_id is None:
                continue
            link = f"{STACKOVERFLOW_BASE_URL}/questions/{question_id}"

        body = _stackoverflow_plain_text(item.get("body"))
        body_paragraphs = [
            paragraph.strip()
            for paragraph in re.split(r"\n{2,}", body)
            if paragraph.strip()
        ]
        paragraphs = [
            text
            for text in [_stackoverflow_stats(item), *body_paragraphs]
            if text
        ][:STACKOVERFLOW_BODY_PARAGRAPH_LIMIT]
        if len(paragraphs) < 2:
            paragraphs.append(
                "Open this question on Stack Overflow to read it in full."
            )

        question_blocks = _stackoverflow_body_blocks(item.get("body"))
        if question_blocks:
            question_blocks.insert(
                0,
                {"type": "paragraph", "text": _stackoverflow_stats(item)},
            )

        tags = [str(tag) for tag in item.get("tags") or [] if str(tag).strip()]
        question_id = item.get("question_id")
        results.append(
            {
                "title": title,
                "link": link,
                "question_id": question_id,
                "score": int(item.get("score") or 0),
                "answer_count": int(item.get("answer_count") or 0),
                "accepted_answer_id": item.get("accepted_answer_id"),
                "section": ", ".join(tags[:3]),
                "published": _stackoverflow_iso_time(
                    item.get("creation_date")
                ),
                "summary": paragraphs[0],
                "paragraphs": paragraphs,
                "blocks": question_blocks,
                "feed": feed,
            }
        )
    return results

def fetch_stackoverflow_answers(
    question_id: object, limit: int = STACKOVERFLOW_ANSWER_LIMIT
) -> dict:
    """Fetch one question's answers and parse their bodies into blocks.

    This follows the prompt's load_answer_to_a_question endpoint. Accepted
    answers are listed first so the modal shows the likely solution before
    lower-voted alternatives.
    """
    try:
        numeric_id = int(str(question_id).strip())
    except (TypeError, ValueError):
        raise ValueError("Missing or invalid Stack Overflow question id") from None
    if numeric_id <= 0:
        raise ValueError("Missing or invalid Stack Overflow question id")
    try:
        pagesize = max(1, min(int(limit), 30))
    except (TypeError, ValueError):
        pagesize = STACKOVERFLOW_ANSWER_LIMIT

    try:
        response = requests.get(
            f"{STACKOVERFLOW_API_URL}/questions/{numeric_id}/answers",
            params={
                "site": STACKOVERFLOW_SITE,
                "filter": "withbody",
                "order": "desc",
                "sort": "votes",
                "pagesize": str(pagesize),
            },
            headers={
                **chat.HTTP_HEADERS,
                "Accept": "application/json",
                "Accept-Encoding": "gzip, deflate",
            },
            timeout=(10, 30),
        )
        response.raise_for_status()
        data = response.json()
    except (requests.RequestException, ValueError) as exc:
        raise RuntimeError(f"Stack Overflow answers failed: {exc}") from exc

    if not isinstance(data, dict):
        raise RuntimeError("Stack Overflow returned an unexpected response.")
    if data.get("error_message"):
        raise RuntimeError(
            f"Stack Overflow API error: {data['error_message']}"
        )
    items = data.get("items")
    if not isinstance(items, list):
        raise RuntimeError("Stack Overflow returned an unexpected response.")

    answers: list[dict] = []
    for item in items[:pagesize]:
        if not isinstance(item, dict):
            continue
        body_blocks = _stackoverflow_body_blocks(item.get("body"))
        body_text = _stackoverflow_plain_text(item.get("body"))
        if not body_blocks and not body_text:
            continue
        answer_id = item.get("answer_id")
        owner = item.get("owner")
        owner_name = ""
        if isinstance(owner, dict):
            owner_name = str(owner.get("display_name") or "").strip()
        answers.append(
            {
                "answer_id": answer_id,
                "score": int(item.get("score") or 0),
                "accepted": bool(item.get("is_accepted")),
                "owner": owner_name,
                "created": _stackoverflow_iso_time(
                    item.get("creation_date")
                ),
                "link": (
                    f"{STACKOVERFLOW_BASE_URL}/a/{answer_id}"
                    if answer_id is not None
                    else f"{STACKOVERFLOW_BASE_URL}/questions/{numeric_id}"
                ),
                "blocks": body_blocks,
                "paragraphs": [
                    block.get("text", "")
                    for block in body_blocks
                    if block.get("type") in {"paragraph", "heading", "code"}
                    and block.get("text")
                ] or [body_text],
            }
        )
    answers.sort(key=lambda answer: (not answer["accepted"], -answer["score"]))
    return {
        "question_id": numeric_id,
        "link": f"{STACKOVERFLOW_BASE_URL}/questions/{numeric_id}",
        "answers": answers,
    }
