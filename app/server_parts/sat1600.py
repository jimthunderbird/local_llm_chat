"""1600.now SAT question-bank search for the /sat_1600 chat mode."""

from __future__ import annotations

from collections import defaultdict
from .core import *

SAT1600_BANK_URL = "https://1600.now/bank"
SAT1600_BANK_ORIGIN = "https://1600.now"
SAT1600_POOL_URLS = {
    "math": "https://1600.now/generated/bank-pools/math.json",
    "reading": "https://1600.now/generated/bank-pools/reading.json",
}
SAT1600_CACHE_DIR = SCRIPT_DIR / ".chat" / "sat_1600"
SAT1600_CACHE_TTL_SECONDS = 6 * 60 * 60
SAT1600_USER_AGENT = (
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
    "AppleWebKit/537.36 (KHTML, like Gecko) "
    "Chrome/124.0 Safari/537.36"
)
SAT1600_MODEL = chat.ANSWER_MODEL
SAT1600_NUM_CTX = chat.ANSWER_NUM_CTX
SAT1600_TOP_MATCHES = 5
SAT1600_CONTEXT_BUDGET_CHARS = 24_000
SAT1600_QUESTION_BUDGET_CHARS = 4_000

SAT1600_STOPWORDS = {
    "the", "a", "an", "is", "are", "was", "were", "be", "been", "being",
    "to", "of", "and", "or", "for", "in", "on", "with", "how", "what",
    "why", "when", "where", "which", "who", "can", "i", "you", "it",
    "this", "that", "these", "those", "does", "do", "did", "use", "using",
    "used", "example", "show", "me", "give", "please", "explain", "about",
    "from", "as", "at", "by", "into", "like", "via", "vs", "want", "need",
    "would", "should", "could", "will", "may", "might", "not", "no", "if",
    "then", "than", "so", "such", "also", "one", "two", "all", "any",
    "more", "most", "some", "other", "over", "after", "before", "between",
    "during", "under", "again", "further", "once", "here", "there", "both",
    "each", "few", "many", "own", "same", "too", "very", "just", "now",
    "question", "answer", "choice", "choices", "best", "following",
    "select", "student", "sat", "solve", "find", "correct", "incorrect",
}

SAT1600_ANSWER_PROMPT = """You are an SAT tutor using the official 1600.now question bank.

Use the question-bank excerpts below when they are relevant.
Requirements:
- If the student pasted or described an SAT question, solve it step by step and state the correct answer clearly.
- If a matching bank question has an official correct answer or rationale, use it and say why the answer is correct.
- For math, show the calculations. For reading and writing, explain why the correct choice fits the passage or the grammar/meaning rule.
- Do not invent an official bank answer. If the excerpts do not settle the question, give your best reasoning and say what is missing.
- If a question depends on a figure that is not available, say which figure or detail is needed.
- Mention the matching question ID(s) and that they come from 1600.now.
- Use Markdown and keep the explanation focused.

<question_bank>
{context}
</question_bank>

{conversation}Student question: {question}

Answer:"""

_SAT1600_LOCK = threading.Lock()
_SAT1600_QUESTIONS: list[dict] | None = None
_SAT1600_INDEX: dict[str, set[int]] | None = None
_SAT1600_IDF: dict[str, float] = {}
_SAT1600_BY_ID: dict[str, dict] = {}
_SAT1600_LOADED_AT = 0.0


def _sat1600_get_json(url: str):
    try:
        response = requests.get(
            url,
            headers={"User-Agent": SAT1600_USER_AGENT},
            timeout=90,
        )
        response.raise_for_status()
        return response.json()
    except requests.RequestException as exc:
        raise RuntimeError(f"Could not fetch {url}: {exc}") from exc
    except ValueError as exc:
        raise RuntimeError(f"{url} did not return valid JSON.") from exc


def _sat1600_cached_pool(subject: str) -> list[dict]:
    SAT1600_CACHE_DIR.mkdir(parents=True, exist_ok=True)
    path = SAT1600_CACHE_DIR / f"{subject}.json"
    try:
        fresh = (
            path.is_file()
            and time.time() - path.stat().st_mtime
            < SAT1600_CACHE_TTL_SECONDS
        )
    except OSError:
        fresh = False
    if fresh:
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
            if isinstance(data, list):
                return data
        except (OSError, ValueError, json.JSONDecodeError):
            pass

    url = SAT1600_POOL_URLS[subject]
    try:
        data = _sat1600_get_json(url)
        if not isinstance(data, list):
            raise RuntimeError(f"{url} returned an unexpected shape.")
        temporary = path.with_name(path.name + ".tmp")
        temporary.write_text(
            json.dumps(data, ensure_ascii=False, separators=(",", ":")) + "\n",
            encoding="utf-8",
        )
        temporary.replace(path)
        return data
    except Exception:
        # A stale cache is better than failing the whole mode.
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
            if isinstance(data, list):
                return data
        except (OSError, ValueError, json.JSONDecodeError):
            pass
        raise


def _sat1600_question_tokens(text: object) -> list[str]:
    return [
        token
        for token in re.findall(r"[a-z0-9]+", str(text or "").lower())
        if len(token) > 1 and token not in SAT1600_STOPWORDS
    ]


def _sat1600_search_text(question: dict) -> str:
    category = question.get("category") or {}
    parts = [
        question.get("prompt"),
        question.get("passage"),
        question.get("questionText"),
        question.get("rationale"),
        category.get("domain") if isinstance(category, dict) else "",
        category.get("skill") if isinstance(category, dict) else "",
    ]
    for choice in question.get("choices") or []:
        if isinstance(choice, dict):
            parts.append(choice.get("text"))
    return " ".join(str(part) for part in parts if part)


def _sat1600_build_index(questions: list[dict]) -> None:
    global _SAT1600_INDEX, _SAT1600_IDF, _SAT1600_BY_ID
    index: dict[str, set[int]] = defaultdict(set)
    by_id: dict[str, dict] = {}
    for doc_index, question in enumerate(questions):
        for token in set(_sat1600_question_tokens(_sat1600_search_text(question))):
            index[token].add(doc_index)
        for key in (
            question.get("stableId"),
            question.get("questionNumber"),
        ):
            key = str(key or "").strip().lower()
            if key:
                by_id[key] = question
    total = max(1, len(questions))
    _SAT1600_IDF = {
        token: math.log((total + 1) / (len(docs) + 1)) + 1.0
        for token, docs in index.items()
    }
    _SAT1600_INDEX = index
    _SAT1600_BY_ID = by_id


def load_sat1600_bank() -> list[dict]:
    """Load the 1600.now question pools and build the search index."""
    global _SAT1600_QUESTIONS, _SAT1600_LOADED_AT
    with _SAT1600_LOCK:
        if _SAT1600_QUESTIONS is not None:
            return _SAT1600_QUESTIONS
        pools: dict[str, list[dict]] = {}
        with ThreadPoolExecutor(max_workers=2) as pool:
            futures = {
                subject: pool.submit(_sat1600_cached_pool, subject)
                for subject in ("math", "reading")
            }
            for subject, future in futures.items():
                pools[subject] = future.result()
        questions: list[dict] = []
        for subject in ("math", "reading"):
            for question in pools[subject]:
                if not isinstance(question, dict):
                    continue
                if question.get("hiddenFromBank"):
                    continue
                question.setdefault("subject", subject)
                questions.append(question)
        if not questions:
            raise RuntimeError("The 1600.now question bank is empty.")
        _sat1600_build_index(questions)
        _SAT1600_QUESTIONS = questions
        _SAT1600_LOADED_AT = time.time()
        return questions


def sat1600_bank_ready() -> bool:
    return _SAT1600_QUESTIONS is not None


def _sat1600_direct_match(question: str) -> dict | None:
    match = re.search(
        r"\b(?:bank-(?:math|reading)-)?([0-9a-f]{8})\b",
        question,
        re.IGNORECASE,
    )
    if not match:
        return None
    return _SAT1600_BY_ID.get(match.group(1).lower())


def search_sat1600(question: str, limit: int = SAT1600_TOP_MATCHES) -> list[dict]:
    """Return bank questions most relevant to one student question."""
    questions = load_sat1600_bank()
    assert _SAT1600_INDEX is not None
    tokens = set(_sat1600_question_tokens(question))
    scores: dict[int, float] = defaultdict(float)
    for token in tokens:
        for doc_index in _SAT1600_INDEX.get(token, ()):
            scores[doc_index] += _SAT1600_IDF.get(token, 0.0)
    ranked = sorted(
        scores.items(),
        key=lambda item: (-item[1], questions[item[0]].get("id") or 0),
    )
    results: list[dict] = []
    direct = _sat1600_direct_match(question)
    if direct is not None:
        results.append(direct)
    for doc_index, _score in ranked:
        candidate = questions[doc_index]
        if candidate is direct:
            continue
        results.append(candidate)
        if len(results) >= limit:
            break
    return results


def _sat1600_format_question(question: dict) -> str:
    category = question.get("category") or {}
    subject = str(question.get("subject") or "").title() or "SAT"
    domain = str(category.get("domain") or "") if isinstance(category, dict) else ""
    skill = str(category.get("skill") or "") if isinstance(category, dict) else ""
    header = (
        f"### {subject} question {question.get('stableId') or question.get('questionNumber')}"
        f" · {question.get('difficulty') or 'unknown difficulty'}"
    )
    if domain or skill:
        header += f" · {domain}" + (f" / {skill}" if skill else "")
    parts = [header]
    prompt = (
        question.get("prompt")
        or question.get("questionText")
        or question.get("text")
        or ""
    )
    if prompt:
        parts.append(f"Prompt: {prompt}")
    choices = question.get("choices") or []
    if choices:
        choice_lines = []
        for choice in choices:
            if not isinstance(choice, dict):
                continue
            choice_lines.append(
                f"{choice.get('id') or '?'}) {choice.get('text') or ''}"
            )
        if choice_lines:
            parts.append("Choices:\n" + "\n".join(choice_lines))
    if question.get("correctAnswer") is not None:
        parts.append(f"Official correct answer: {question.get('correctAnswer')}")
    rationale = str(question.get("rationale") or "").strip()
    if rationale:
        parts.append(f"Official rationale: {rationale}")
    images = question.get("questionImages") or []
    if images:
        image_lines = []
        for image in images:
            if not isinstance(image, dict):
                continue
            alt = str(image.get("alt") or "SAT question figure").strip()
            src = str(image.get("optimizedSrc") or image.get("src") or "").strip()
            if src.startswith("/"):
                src = SAT1600_BANK_ORIGIN + src
            image_lines.append(f"{alt} ({src})" if src else alt)
        if image_lines:
            parts.append(
                "Figure(s), not visible to the text model: "
                + "; ".join(image_lines)
            )
    number = str(question.get("questionNumber") or "").strip()
    parts.append(
        "Source: 1600.now"
        + (f" — {SAT1600_BANK_URL}?q={quote(number)}" if number else "")
    )
    text = "\n\n".join(parts)
    if len(text) > SAT1600_QUESTION_BUDGET_CHARS:
        text = text[:SAT1600_QUESTION_BUDGET_CHARS].rstrip() + "\n…"
    return text


def build_sat1600_context(
    question: str,
    limit: int = SAT1600_TOP_MATCHES,
    budget: int = SAT1600_CONTEXT_BUDGET_CHARS,
) -> tuple[str, list[dict]]:
    matches = search_sat1600(question, limit=limit)
    context_parts: list[str] = []
    sources: list[dict] = []
    used = 0
    for match in matches:
        part = _sat1600_format_question(match)
        if context_parts and used + len(part) > budget:
            break
        context_parts.append(part)
        used += len(part) + 2
        number = str(match.get("questionNumber") or "").strip()
        sources.append(
            {
                "stable_id": str(match.get("stableId") or ""),
                "subject": str(match.get("subject") or ""),
                "question_number": number,
                "url": (
                    f"{SAT1600_BANK_URL}?q={quote(number)}" if number else SAT1600_BANK_URL
                ),
            }
        )
    return "\n\n".join(context_parts), sources


def sat1600_answer_prompt(
    question: str, context: str, conversation: str = ""
) -> str:
    conversation_block = (
        f"Recent conversation:\n{conversation}\n\n" if conversation else ""
    )
    return SAT1600_ANSWER_PROMPT.format(
        context=context or "(No matching 1600.now bank questions were found.)",
        conversation=conversation_block,
        question=question,
    )
