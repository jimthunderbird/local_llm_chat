"""1600.now SAT question-bank search for the /sat_1600 chat mode."""

from __future__ import annotations

import difflib
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
SAT1600_IMAGE_PROXY_PATH = "/api/sat1600_image?url="
SAT1600_IMAGE_CACHE_TTL_SECONDS = 24 * 60 * 60
SAT1600_IMAGE_CACHE_MAX_ITEMS = 100

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
    "problem", "problems", "exercise", "exercises", "yet", "please", "show",
    "give", "provide", "display", "send", "pick", "pull", "suggest", "want",
    "new", "another", "different", "next", "again",
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
_SAT1600_VOCAB: list[str] = []
_SAT1600_FUZZY_CACHE: dict[str, tuple[str, ...]] = {}
_SAT1600_FUZZY_LOCK = threading.Lock()
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
    global _SAT1600_INDEX, _SAT1600_IDF, _SAT1600_BY_ID, _SAT1600_VOCAB
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
    _SAT1600_VOCAB = sorted(index.keys())
    with _SAT1600_FUZZY_LOCK:
        _SAT1600_FUZZY_CACHE.clear()


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


def _sat1600_expand_query_tokens(tokens: set[str]) -> set[str]:
    """Add close bank-vocabulary spellings for unknown topic tokens."""
    expanded = set(tokens)
    if _SAT1600_INDEX is None:
        return expanded
    for token in tokens:
        if (
            token in _SAT1600_INDEX
            or len(token) < 5
            or not token.isalpha()
        ):
            continue
        with _SAT1600_FUZZY_LOCK:
            cached = _SAT1600_FUZZY_CACHE.get(token)
        if cached is None:
            cached = tuple(
                difflib.get_close_matches(
                    token, _SAT1600_VOCAB, n=3, cutoff=0.8
                )
            )
            with _SAT1600_FUZZY_LOCK:
                _SAT1600_FUZZY_CACHE[token] = cached
        expanded.update(cached)
    return expanded


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
    tokens = _sat1600_expand_query_tokens(
        set(_sat1600_question_tokens(question))
    )
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


_SAT1600_TOPIC_WORD_RE = re.compile(
    r"\b(?:topics?|subjects?|areas?|categories?|skills?|domains)\b",
    re.IGNORECASE,
)
_SAT1600_TOPIC_TRIGGER_RE = re.compile(
    r"\b(?:list|show|give|tell|what|which|name|enumerate|cover|covered|"
    r"covers|include|includes|included|all|tested|test|appear|appears)\b",
    re.IGNORECASE,
)


def _sat1600_topic_subjects(question: str) -> list[str]:
    text = " ".join(str(question or "").lower().split())
    if "topic sentence" in text:
        return []
    if not _SAT1600_TOPIC_WORD_RE.search(text):
        return []
    if not _SAT1600_TOPIC_TRIGGER_RE.search(text):
        return []
    wants_math = bool(re.search(
        r"\b(?:math|mathematics|maths|algebra|geometry|advanced math|"
        r"problem[- ]solving(?: and data analysis)?)\b",
        text,
    ))
    wants_reading = bool(re.search(
        r"\b(?:reading|writing|english|verbal|grammar|rhetoric|"
        r"information and ideas|craft and structure|expression of ideas|"
        r"standard english conventions)\b",
        text,
    ))
    if "trigonometry" in _sat1600_expand_query_tokens(
        set(_sat1600_question_tokens(text))
    ):
        wants_math = True
    if wants_math and not wants_reading:
        return ["math"]
    if wants_reading and not wants_math:
        return ["reading"]
    return ["math", "reading"]


_SAT1600_QUESTION_WORD_RE = re.compile(
    r"\b(?:questions?|problems?|exercises?)\b", re.IGNORECASE
)
_SAT1600_QUESTION_VERB_RE = re.compile(
    r"\b(?:show|give|find|get|pick|pull|suggest|provide|display|send|"
    r"want|need|have|what|which)\b",
    re.IGNORECASE,
)
_SAT1600_REVEAL_RE = re.compile(
    r"\b(?:show|give|reveal|tell|check|what(?:'s| is))\b[\s\S]{0,60}"
    r"\b(?:answer|solution|explanation)\b",
    re.IGNORECASE,
)
_SAT1600_NEGATED_ANSWER_RE = re.compile(
    r"\b(?:do not|don't|dont|without|not)\b[\s\S]{0,40}"
    r"\b(?:answer|solution|explanation)\b",
    re.IGNORECASE,
)


def _sat1600_is_question_request(question: str) -> bool:
    text = " ".join(str(question or "").lower().split())
    return bool(
        _SAT1600_QUESTION_WORD_RE.search(text)
        and _SAT1600_QUESTION_VERB_RE.search(text)
    )


def _sat1600_is_reveal_request(question: str) -> bool:
    text = str(question or "")
    if _SAT1600_NEGATED_ANSWER_RE.search(text):
        return False
    return bool(_SAT1600_REVEAL_RE.search(text))


def _sat1600_question_figure_markdown(question: dict) -> list[str]:
    lines: list[str] = []
    for figure in _sat1600_question_figures(question)[:4]:
        url = str(figure.get("url") or "").strip()
        if not url:
            continue
        alt = (
            str(figure.get("alt") or "SAT question figure")
            .replace("[", "")
            .replace("]", "")
        )
        lines.append(f"![{alt}]({sat1600_image_url(url)})")
    if lines:
        lines.append("*Figure from the 1600.now question bank.*")
    return lines


def _sat1600_question_meta(question: dict) -> tuple[str, str, str, str]:
    category = question.get("category") or {}
    subject = str(question.get("subject") or "").title() or "SAT"
    domain = str(category.get("domain") or "") if isinstance(category, dict) else ""
    skill = str(category.get("skill") or "") if isinstance(category, dict) else ""
    stable_id = str(question.get("stableId") or question.get("questionNumber") or "")
    return subject, domain, skill, stable_id


def _sat1600_select_question_for_request(
    question: str, exclude_ids: list[str] | None = None
) -> dict | None:
    exclude = {
        str(value or "").strip().lower()
        for value in (exclude_ids or [])
        if str(value or "").strip()
    }

    def is_excluded(candidate: dict) -> bool:
        for key in ("stableId", "questionNumber"):
            value = str(candidate.get(key) or "").strip().lower()
            if value and value in exclude:
                return True
        return False

    candidates: list[dict] = []
    for limit in (80, 200):
        candidates = [
            candidate
            for candidate in search_sat1600(question, limit=limit)
            if not is_excluded(candidate)
        ]
        if candidates:
            break
    if not candidates:
        return None
    topic_tokens = _sat1600_expand_query_tokens(
        set(_sat1600_question_tokens(question))
    )
    if not topic_tokens:
        return candidates[0]

    scored: list[tuple[float, int, int, dict]] = []
    for index, candidate in enumerate(candidates):
        category = candidate.get("category") or {}
        skill = str(category.get("skill") or "").lower()
        domain = str(category.get("domain") or "").lower()
        prompt = str(candidate.get("prompt") or "").lower()
        passage = str(candidate.get("passage") or "").lower()
        value = 0.0
        for token in topic_tokens:
            if token in skill:
                value += 8
            if token in prompt:
                value += 4
            if token in domain:
                value += 3
            if token in passage:
                value += 1
        scored.append(
            (value, len(candidate.get("questionImages") or []), index, candidate)
        )
    scored.sort(key=lambda item: (-item[0], -item[1], item[2]))
    return scored[0][3] if scored else None


def sat1600_question_request_answer(
    question: str, exclude_ids: list[str] | None = None
) -> tuple[str, list[dict], dict] | None:
    """Return one bank question without its answer for a question request."""
    if not _sat1600_is_question_request(question):
        return None
    selected = _sat1600_select_question_for_request(
        question, exclude_ids=exclude_ids
    )
    if selected is None:
        return None
    subject, domain, skill, stable_id = _sat1600_question_meta(selected)
    lines = [
        f"## {subject} question from the 1600.now bank",
        "",
    ]
    lines.extend(_sat1600_question_figure_markdown(selected))
    if len(lines) > 2:
        lines.append("")
    details = [stable_id]
    if selected.get("difficulty"):
        details.append(str(selected["difficulty"]))
    if domain:
        details.append(domain + (f" / {skill}" if skill else ""))
    lines.append("**" + " · ".join(details) + "**")
    lines.append("")
    lines.append(
        str(
            selected.get("prompt")
            or selected.get("questionText")
            or selected.get("text")
            or ""
        )
    )
    choices = selected.get("choices") or []
    if choices:
        lines.append("")
        for choice in choices:
            if not isinstance(choice, dict):
                continue
            lines.append(
                f"{choice.get('id') or '?'}) {choice.get('text') or ''}"
            )
    number = str(selected.get("questionNumber") or "").strip()
    lines.append("")
    lines.append(
        "Source: "
        + (
            f"{SAT1600_BANK_URL}?q={quote(number)}"
            if number
            else SAT1600_BANK_URL
        )
    )
    lines.append("")
    lines.append(
        "Solve it when you are ready, then ask **show the answer** to reveal "
        "the official answer and explanation."
    )
    sources = [
        {
            "stable_id": stable_id,
            "subject": str(selected.get("subject") or ""),
            "question_number": number,
            "url": (
                f"{SAT1600_BANK_URL}?q={quote(number)}"
                if number
                else SAT1600_BANK_URL
            ),
            "figures": _sat1600_question_figures(selected),
        }
    ]
    return "\n".join(lines).strip() + "\n", sources, selected


def sat1600_reveal_answer(
    question: str, pending: dict | None
) -> tuple[str, list[dict]] | None:
    """Reveal the official answer for the pending bank question."""
    if not pending or not _sat1600_is_reveal_request(question):
        return None
    subject, domain, skill, stable_id = _sat1600_question_meta(pending)
    lines = [f"## Official answer — {stable_id}", ""]
    lines.extend(_sat1600_question_figure_markdown(pending))
    if len(lines) > 2:
        lines.append("")
    lines.append(
        f"**Correct answer:** {pending.get('correctAnswer') or '(not provided)'}"
    )
    rationale = str(pending.get("rationale") or "").strip()
    if rationale:
        lines.append("")
        lines.append(f"**Official rationale:** {rationale}")
    else:
        lines.append("")
        lines.append(
            "The 1600.now bank does not include a written rationale for this "
            "question; the official answer above is the bank answer."
        )
    number = str(pending.get("questionNumber") or "").strip()
    lines.append("")
    lines.append(
        "Source: "
        + (
            f"{SAT1600_BANK_URL}?q={quote(number)}"
            if number
            else SAT1600_BANK_URL
        )
    )
    sources = [
        {
            "stable_id": stable_id,
            "subject": str(pending.get("subject") or ""),
            "question_number": number,
            "url": (
                f"{SAT1600_BANK_URL}?q={quote(number)}"
                if number
                else SAT1600_BANK_URL
            ),
            "figures": _sat1600_question_figures(pending),
        }
    ]
    return "\n".join(lines).strip() + "\n", sources


def sat1600_topic_list_answer(question: str) -> tuple[str, list[dict]] | None:
    """Return a deterministic topic list from the bank's domain/skill taxonomy."""
    questions = load_sat1600_bank()
    subjects = _sat1600_topic_subjects(question)
    if not subjects:
        return None
    groups: dict[str, dict[str, dict[str, int]]] = {
        subject: defaultdict(lambda: defaultdict(int)) for subject in subjects
    }
    for question_row in questions:
        subject = str(question_row.get("subject") or "")
        if subject not in groups:
            continue
        category = question_row.get("category") or {}
        domain = (
            str(category.get("domain") or "Other")
            if isinstance(category, dict)
            else "Other"
        )
        skill = (
            str(category.get("skill") or "Other")
            if isinstance(category, dict)
            else "Other"
        )
        groups[subject][domain][skill] += 1

    lines: list[str] = []
    if subjects == ["math"]:
        title = "Math"
    elif subjects == ["reading"]:
        title = "Reading and Writing"
    else:
        title = "Math and Reading"
    lines.append(f"## {title} topics in the 1600.now question bank")
    lines.append("")
    for subject in subjects:
        subject_groups = groups.get(subject) or {}
        if not subject_groups:
            continue
        total = sum(
            sum(skill_counts.values())
            for skill_counts in subject_groups.values()
        )
        lines.append(f"### {subject.title()} — {total:,} questions")
        for domain, skill_counts in sorted(
            subject_groups.items(),
            key=lambda item: (-sum(item[1].values()), item[0]),
        ):
            domain_count = sum(skill_counts.values())
            lines.append(
                f"- **{domain}** — {domain_count:,} questions"
            )
            for skill, count in sorted(
                skill_counts.items(), key=lambda item: (-item[1], item[0])
            ):
                lines.append(f"  - {skill} — {count:,}")
        lines.append("")
    lines.append(f"Source: {SAT1600_BANK_URL}")
    answer = "\n".join(lines).strip() + "\n"
    sources = [
        {
            "stable_id": "",
            "subject": subject,
            "question_number": "",
            "url": SAT1600_BANK_URL,
        }
        for subject in subjects
    ]
    return answer, sources


_SAT1600_IMAGE_LOCK = threading.Lock()
_SAT1600_IMAGE_CACHE: dict[str, tuple[float, bytes, str]] = {}


def sat1600_image_url(url: str) -> str:
    """Return a same-origin proxy URL for one 1600.now figure."""
    return SAT1600_IMAGE_PROXY_PATH + quote(str(url), safe="")


def sat1600_allowed_image_url(url: str) -> bool:
    parsed = urlparse(str(url or ""))
    if parsed.scheme != "https" or parsed.netloc != "1600.now":
        return False
    path = unquote(parsed.path or "")
    return (
        path.startswith("/generated/sat-images/")
        or path.startswith("/images/SAT-Style Questions/")
        or path.startswith("/images/SAT-Style%20Questions/")
    )


def fetch_sat1600_image(url: str) -> tuple[bytes, str]:
    """Fetch and cache one allowed 1600.now figure for the browser."""
    if not sat1600_allowed_image_url(url):
        raise ValueError("Only 1600.now SAT question figures are allowed.")
    now = time.time()
    with _SAT1600_IMAGE_LOCK:
        cached = _SAT1600_IMAGE_CACHE.get(url)
        if cached and now - cached[0] < SAT1600_IMAGE_CACHE_TTL_SECONDS:
            return cached[1], cached[2]
    try:
        response = requests.get(
            url,
            headers={"User-Agent": SAT1600_USER_AGENT},
            timeout=60,
        )
        response.raise_for_status()
    except requests.RequestException as exc:
        raise RuntimeError(f"Could not fetch the SAT figure: {exc}") from exc
    data = response.content
    if not data:
        raise RuntimeError("The SAT figure was empty.")
    if len(data) > 12 * 1024 * 1024:
        raise RuntimeError("The SAT figure is too large.")
    content_type = (
        response.headers.get("content-type", "").split(";")[0].strip()
        or "image/webp"
    )
    if not content_type.startswith("image/"):
        raise RuntimeError("The SAT figure URL did not return an image.")
    with _SAT1600_IMAGE_LOCK:
        _SAT1600_IMAGE_CACHE[url] = (now, data, content_type)
        if len(_SAT1600_IMAGE_CACHE) > SAT1600_IMAGE_CACHE_MAX_ITEMS:
            oldest = sorted(
                _SAT1600_IMAGE_CACHE.items(), key=lambda item: item[1][0]
            )[:20]
            for cached_url, _value in oldest:
                _SAT1600_IMAGE_CACHE.pop(cached_url, None)
    return data, content_type


def _sat1600_question_figures(question: dict) -> list[dict]:
    figures: list[dict] = []
    for image in question.get("questionImages") or []:
        if not isinstance(image, dict):
            continue
        src = str(
            image.get("optimizedSrc") or image.get("src") or ""
        ).strip()
        if not src:
            continue
        if src.startswith("/"):
            src = SAT1600_BANK_ORIGIN + src
        if not re.match(r"^https?://", src, re.IGNORECASE):
            continue
        alt = str(image.get("alt") or "SAT question figure").strip()
        figures.append({"url": src, "alt": alt})
    return figures


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
                "figures": _sat1600_question_figures(match),
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
