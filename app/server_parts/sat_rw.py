"""SAT Reading & Writing question bank built from the sample-test PDFs.

Source (prompts/sat.prompt ``sat_test_samples``): the PDFs dropped into
``./.chat/sat/sample-2026`` - currently the Test Ninjas SAT full practice
exam, whose Reading & Writing modules carry numbered multiple-choice
questions and a printed answer key.

The bank is extracted once with pypdf and cached as
``reading_writing.<pdf stem>.json`` beside the PDF; the cache is rebuilt
when the PDF changes.  The PDF's text layer drops the italic
fill-in-the-blank words of some "Words in Context" questions (those glyphs
use an encoding pypdf cannot decode), so questions whose choices did not
survive extraction are skipped rather than served broken.

The two clickable sections of task.reading_and_writing_section - Reading
and Writing - come from the exam's own skill headers:

* Writing: Boundaries, Transitions, Rhetorical Synthesis, Form/Structure/
  Sense (the Standard English Conventions and Expression of Ideas skills).
* Reading: everything else (Command of Evidence, Cross-Text Connections,
  Central Ideas and Details, Text Structure and Purpose, Inferences, Words
  in Context, ...).

``sat_rw_question`` returns the same payload shape as the math path
(``sat_domain_question``), so the existing question card, Answer/analysis
stream, and Previous/Next navigation keep working unchanged.
"""

from __future__ import annotations

import hashlib
import json
import random
import re
import threading
import time
from pathlib import Path

from .core import SCRIPT_DIR

SAMPLES_DIR = SCRIPT_DIR / ".chat" / "sat" / "sample-2026"

# prompts/sat.prompt: sections are unlimited — past the sample-test pool,
# {question_llm_reading_and_writing} (ornith-1.5:35b, resolved lazily from
# sat.py to avoid a circular import) generates the next question on demand
# and it persists here in the final RW-bank record shape.
_RW_GENERATED_DIR = SAMPLES_DIR.parent / "rw_generated"
_RW_GENERATED_META_FILE = "_meta.json"

# prompts/sat.prompt: Reading/Writing answers and generated questions use
# {question_llm_reading_and_writing}; resolved lazily to avoid a circular
# import (sat.py imports this module at startup).
def _analysis_model() -> str:
    from .sat import SAT_RW_MODEL

    return SAT_RW_MODEL

# The two subsections of the "SAT Reading And Writing" block.
RW_READING = "Reading"
RW_WRITING = "Writing"
RW_DOMAIN_NAMES = (RW_READING.casefold(), RW_WRITING.casefold())

WRITING_SKILL_MARKERS = (
    "boundaries",
    "transitions",
    "rhetorical synthesis",
    "form, structure, and sense",
    "form structure and sense",
    "expression of ideas",
)

READING_SKILL_MARKERS = (
    "command of evidence",
    "cross-text connections",
    "central ideas and details",
    "text structure and purpose",
    "inferences",
    "words in context",
    "information and ideas",
    "craft and structure",
)

# One line per question header, e.g. "5 COMMAND OF EVIDENCE MULTIPLE CHOICE".
# The final "E" is allowed to be clipped when a header sits on the last line
# of a page.
_QUESTION_HEADER_RE = re.compile(
    r"^(?P<num>\d{1,2})\s+(?P<skill>[A-Z(),'’() \-.]*)\s*MULTIPLE CHOIC"
)
_CHOICE_RE = re.compile(r"^(?P<id>[A-D])\)\s*(?P<text>.*)$")
_PAGE_NUMBER_RE = re.compile(r"^\d{1,3}$")
# Text-completion stems (Words in Context, Boundaries, Transitions, Form/
# Structure/Sense) lose their italic blank word to the PDF's text layer,
# which leaves a doubled-space hole in the prompt.  The hole is rendered as
# a solid underline (prompts/sat.prompt).  Fullwidth low lines (U+FF3F) are
# used because ASCII underscores would collide with the chat renderer's
# __emphasis__ parsing when a stem happens to carry two holes.
_RW_BLANK_RUN_RE = re.compile(r"\s{2,}")
_RW_BLANK_MARKER = "\uff3f\uff3f\uff3f\uff3f"
_SECTION_MARKER_RE = re.compile(r"^S\s+E\s+C\s+T\s+I\s+O\s+N", re.IGNORECASE)
_FOOTER_RE = re.compile(
    r"(TEST NINJAS.*(CONTINUE|PRACTICE MATERIALS|INSTRUCTOR COPY)"
    r"|PRACTICE MATERIALS.*CONTINUE|NO TEST MATERIAL ON THIS PAGE"
    r"|DO NOT (?:READ|TURN)|MARKS? YOUR ANSWERS?)",
    re.IGNORECASE,
)
_KEY_BLOCK_RE = re.compile(
    r"READING\s*&\s*WRITING\s*·\s*MODULE\s*(?P<module>[12])(?P<rows>.*?)"
    r"(?=READING|WRITING|MATH|SCORING|$)",
    re.DOTALL | re.IGNORECASE,
)
_KEY_PAIR_RE = re.compile(r"(?P<num>\d{1,2})\s+(?P<answer>[A-D])(?!\w)")

_BANK_LOCK = threading.Lock()
_BANK: dict | None = None


def _clean_lines(lines: list[str]) -> list[str]:
    """Drop page furniture that pypdf interleaves with question text."""
    cleaned: list[str] = []
    for raw in lines:
        line = raw.replace("\u00a0", " ").rstrip()
        if not line.strip():
            continue
        if _PAGE_NUMBER_RE.fullmatch(line.strip()):
            continue
        if _SECTION_MARKER_RE.match(line):
            line = _SECTION_MARKER_RE.sub("", line).strip()
            if not line:
                continue
        if line.strip().casefold().startswith(
            ("test ninjas", "module ", "directions")
        ):
            continue
        if _FOOTER_RE.search(line) and len(line) < 120:
            continue
        cleaned.append(line)
    return cleaned


def _dehyphenate_join(lines: list[str]) -> str:
    """Join wrapped PDF lines, healing words broken across lines.

    A line that begins with whitespace marks a dropped-italic hole at the
    line boundary (the text-completion blank word lived at the start of the
    wrapped line), so a blank marker is inserted there as well.
    """
    parts: list[str] = []
    for line in lines:
        stripped = str(line).strip()
        if not stripped:
            continue
        had_leading_hole = str(line) != stripped
        if parts and parts[-1].endswith("-"):
            head = parts[-1]
            if head[-2].isalpha() and stripped[:1].islower():
                parts[-1] = head[:-1] + stripped
                continue
        if had_leading_hole and parts:
            stripped = _RW_BLANK_MARKER + stripped
        parts.append(stripped)
    return " ".join(parts)


def _classify_skill(skill: str) -> str:
    """Map one exam skill header onto the Reading / Writing section."""
    normalized = " ".join(str(skill or "").split()).casefold()
    for marker in WRITING_SKILL_MARKERS:
        if marker in normalized:
            return RW_WRITING
    return RW_READING


def _parse_answer_key(text: str) -> dict[str, dict[int, str]]:
    """Read the RW answer blocks ("1 A 2 D 3 C ...") from the key page."""
    keys: dict[str, dict[int, str]] = {}
    for block in _KEY_BLOCK_RE.finditer(text):
        module = f"m{block.group('module')}"
        answers: dict[int, str] = {}
        for pair in _KEY_PAIR_RE.finditer(block.group("rows")):
            number = int(pair.group("num"))
            if 1 <= number <= 50:
                answers[number] = pair.group("answer").upper()
        if answers:
            keys[module] = answers
    return keys


def _page_module(text: str) -> str | None:
    """Return "m1"/"m2" when the page names its module (question pages do)."""
    tokens = re.findall(r"MODULE\s+([12])", text, re.IGNORECASE)
    return f"m{tokens[0]}" if tokens else None


def _page_is_rw_question_page(text: str) -> tuple[bool, str | None]:
    """Classify one PDF page for the extraction state machine.

    Returns ``(is_rw_question_page, module)``.  Module transition pages,
    break pages, and the instructor answer key are excluded even though
    they still mention Reading & Writing; question pages carry both the
    section name and their own ``MODULE n`` token.  The math test looks at
    the page header only (the part before "TEST NINJAS"), because a
    Reading & Writing passage may legitimately contain the word
    "mathematics" in its body text.
    """
    normalized = " ".join(text.split()).casefold()
    if "answer key" in normalized:
        return False, None
    if "module transition" in normalized or "no test material" in normalized:
        return False, None
    header_region = normalized.split("test ninjas", 1)[0]
    if "math" in header_region:
        return False, None
    if "reading & writing" not in normalized:
        return False, None
    return True, _page_module(normalized)


def _extract_pdf(pdf_path: Path) -> dict:
    """Parse one practice-exam PDF into an RW question bank."""
    from pypdf import PdfReader

    reader = PdfReader(pdf_path)
    key_page_text = ""
    chunks: list[dict] = []
    current: dict | None = None

    for page in reader.pages:
        try:
            text = page.extract_text() or ""
        except Exception:
            text = ""
        if "answer key" in " ".join(text.split()).casefold():
            key_page_text += text
        is_question_page, page_module = _page_is_rw_question_page(text)
        if not is_question_page:
            # Transition / break / key pages must not leak prose into the
            # preceding module's last question.
            current = None
            continue

        for raw_line in text.splitlines():
            header = _QUESTION_HEADER_RE.match(raw_line.strip())
            if header:
                current = {
                    "module": page_module or "m1",
                    "number": int(header.group("num")),
                    "skill": " ".join(header.group("skill").split()),
                    "raw_lines": [],
                }
                chunks.append(current)
                continue
            if current is not None:
                current["raw_lines"].append(raw_line)

    keys = _parse_answer_key(key_page_text)
    questions: list[dict] = []
    skipped: list[dict] = []
    seen_ids: set[str] = set()
    for chunk in chunks:
        module = chunk["module"]
        number = int(chunk["number"])
        if not 1 <= number <= 50:
            continue
        stable_id = f"ninjas-sample-2026-{module}-q{number}"

        lines = _clean_lines(chunk["raw_lines"])
        # Choices split the body: everything before the first "A)" line is
        # the passage plus the question prompt, kept together on purpose -
        # splitting them reliably is not worth the fragility.
        body_before: list[str] = []
        choices: dict[str, str] = {}
        current_choice: str | None = None
        for line in lines:
            match = _CHOICE_RE.match(line.strip())
            if match:
                current_choice = match.group("id")
                choices[current_choice] = (
                    choices.get(current_choice, "")
                    + " "
                    + match.group("text").strip()
                ).strip()
                continue
            if current_choice:
                choices[current_choice] = (
                    choices[current_choice] + " " + line.strip()
                ).strip()
            else:
                body_before.append(line)

        prompt = _dehyphenate_join(body_before)
        # The doubled-space hole marks the text-completion blank; replace it
        # with a solid underline so the question reads as the intended stem.
        prompt = _RW_BLANK_RUN_RE.sub(_RW_BLANK_MARKER, prompt)
        ordered = [choices.get(letter, "") for letter in "ABCD"]
        answer = keys.get(module, {}).get(number, "")

        # Questions whose italic fill-in words never made it into the PDF's
        # text layer lose their choices or a trailing choice variant; skip
        # any question that does not carry four complete answer choices
        # rather than serving a broken card.
        if len([part for part in ordered if part]) < 4:
            skipped.append(
                {
                    "stable_id": stable_id,
                    "skill": chunk["skill"],
                    "reason": "choices were not extractable from the PDF",
                }
            )
            continue
        if not prompt or len(prompt) < 40:
            skipped.append(
                {
                    "stable_id": stable_id,
                    "skill": chunk["skill"],
                    "reason": "question text was too short to be usable",
                }
            )
            continue
        if stable_id in seen_ids:
            continue
        seen_ids.add(stable_id)
        section = _classify_skill(chunk["skill"])
        questions.append(
            {
                "stable_id": stable_id,
                "module": module,
                "number": number,
                "skill": chunk["skill"].title(),
                "section": section,
                "prompt": prompt,
                "choices": [
                    {"id": letter, "text": text}
                    for letter, text in zip("ABCD", ordered)
                ],
                "answer": answer,
                "source": "Test Ninjas SAT full practice exam (sample-2026)",
                "source_pdf": pdf_path.name,
            }
        )

    order = {RW_READING: 0, RW_WRITING: 1}
    questions.sort(key=lambda q: (order.get(q["section"], 9), q["stable_id"]))
    return {"questions": questions, "skipped": skipped}


def _cache_path(pdf_path: Path) -> Path:
    return pdf_path.with_name(f"reading_writing.{pdf_path.stem}.json")


def _pdf_fingerprint(pdf_path: Path) -> dict:
    stat = pdf_path.stat()
    return {
        "name": pdf_path.name,
        "size": stat.st_size,
        "mtime": int(stat.st_mtime),
    }


def _build_bank() -> dict:
    """Extract every sample PDF once and cache the result on disk."""
    questions: list[dict] = []
    skipped: list[dict] = []
    sources: list[str] = []
    for pdf_path in sorted(SAMPLES_DIR.glob("*.pdf")):
        try:
            fingerprint = _pdf_fingerprint(pdf_path)
            cache_path = _cache_path(pdf_path)
            payload = None
            if cache_path.is_file():
                try:
                    cached = json.loads(
                        cache_path.read_text(encoding="utf-8")
                    )
                except (OSError, ValueError):
                    cached = None
                if cached and cached.get("source") == fingerprint:
                    payload = cached
            if payload is None:
                payload = _extract_pdf(pdf_path)
                payload["source"] = fingerprint
                payload["built_at"] = int(time.time())
                cache_path.write_text(
                    json.dumps(payload, ensure_ascii=False, indent=1),
                    encoding="utf-8",
                )
            questions.extend(payload.get("questions") or [])
            skipped.extend(payload.get("skipped") or [])
            sources.append(pdf_path.name)
        except Exception as exc:
            skipped.append(
                {
                    "stable_id": f"pdf:{pdf_path.name}",
                    "skill": "",
                    "reason": f"could not parse the PDF: {exc}",
                }
            )
    generated = _load_rw_generated()
    questions.extend(generated)
    if generated:
        sources.append(
            f"{len(generated)} question(s) generated with the RW model"
        )
    return {"questions": questions, "skipped": skipped, "sources": sources}


def _load_bank() -> dict:
    global _BANK
    if _BANK is not None:
        return _BANK
    with _BANK_LOCK:
        if _BANK is None:
            try:
                _BANK = _build_bank()
            except Exception as exc:
                _BANK = {
                    "questions": [],
                    "skipped": [
                        {
                            "stable_id": "bank",
                            "skill": "",
                            "reason": f"bank build failed: {exc}",
                        }
                    ],
                    "sources": [],
                }
    return _BANK


def sat_rw_bank_ready() -> bool:
    """True once at least one sample PDF was parsed into questions."""
    return bool(_load_bank().get("questions"))


def _load_rw_generated() -> list[dict]:
    """Load every previously generated Reading/Writing question on disk."""
    if not _RW_GENERATED_DIR.is_dir():
        return []
    questions: list[dict] = []
    for path in sorted(_RW_GENERATED_DIR.glob("*.json")):
        if path.name.startswith("_"):
            continue
        try:
            question = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, ValueError, json.JSONDecodeError):
            continue
        if not isinstance(question, dict):
            continue
        if question.get("section") not in (RW_READING, RW_WRITING):
            continue
        if not str(question.get("prompt") or "").strip():
            continue
        choices = question.get("choices")
        if not isinstance(choices, list) or len(choices) < 2:
            continue
        questions.append(question)
    return questions


def _rw_next_number() -> int:
    try:
        meta = json.loads(
            (_RW_GENERATED_DIR / _RW_GENERATED_META_FILE).read_text(
                encoding="utf-8"
            )
        )
        return int(meta.get("count") or 0) + 1
    except (OSError, ValueError, TypeError, json.JSONDecodeError):
        return 1


_RW_GENERATION_PROMPT = """You are writing NEW original SAT questions for an endless practice bank.

Write ONE new original SAT {section} question in the style of the digital SAT
practice tests. Return ONLY a JSON object (no markdown fences, no commentary)
with exactly this shape:
{{
  "skill": "<short skill label, e.g. {skill_hint}>",
  "passage": "<the passage the question is based on>",
  "question": "<the question prompt about the passage>",
  "choices": {{"A": "<text>", "B": "<text>", "C": "<text>", "D": "<text>"}},
  "paragraph": ""
}}

For "Reading": the passage runs 60-110 words of narrative or argument; the
question tests central ideas, inference, command of evidence, cross-text
connections, text structure, purpose, or words in context.
For "Writing": the passage runs 1-4 sentences and contains an editing issue;
the question asks which choice best meets Standard English conventions
(boundaries, agreement, punctuation) or expression of ideas (transitions,
rhetorical synthesis, logic).

Rules:
- Exactly four choices A-D; exactly one is best, but do NOT say which one.
- The question must be fully answerable from the passage and prompt alone.
- Do not include an answer key, rationale, or explanation.
"""


def _rw_generate_question(section: str) -> dict | None:
    """Generate one new Reading/Writing question with the RW {question_llm}.

    prompts/sat.prompt: reading and writing related answers use
    {question_llm_reading_and_writing} (ornith-1.5:35b), unlike the math
    generation path.  The record is persisted into
    {questions_cache_dir}/rw_generated in the final RW-bank shape, so it
    survives restarts and joins the section pool exactly like a
    sample-test question (without an answer key — the analysis pass stays
    the single source of truth).
    """
    from .sat import (
        SAT_RW_MODEL,
        _sat_call_local_model,
        _sat_parse_generated_question,
    )

    skill_hint = (
        "Central Ideas and Details" if section == RW_READING else "Boundaries"
    )
    prompt = _RW_GENERATION_PROMPT.format(
        section=section, skill_hint=skill_hint
    )
    payload = _sat_parse_generated_question(
        _sat_call_local_model(prompt, model=SAT_RW_MODEL)
    )
    if not isinstance(payload, dict):
        return None
    number = _rw_next_number()
    passage = str(payload.get("passage") or "").strip()
    question_text = str(payload.get("question") or "").strip()
    if passage and question_text:
        prompt_text = f"{passage}\n\n{question_text}"
    else:
        prompt_text = question_text or passage
    if len(prompt_text) < 40:
        return None
    raw_choices = payload.get("choices")
    if isinstance(raw_choices, dict):
        items = list(raw_choices.items())
    elif isinstance(raw_choices, list):
        items = list(enumerate(raw_choices))
    else:
        items = []
    choices: list[dict[str, str]] = []
    for item_index, item in enumerate(items):
        key, value = item
        text = "" if value is None else str(value).strip()
        if not text:
            continue
        letter = (
            str(key)
            if isinstance(raw_choices, dict)
            else chr(ord("A") + item_index)
        )
        choices.append({"id": letter, "text": text})
    texts = {choice["text"].casefold() for choice in choices}
    if len(choices) < 2 or len(texts) < 2:
        return None
    digest = hashlib.sha1(
        json.dumps(
            {
                "p": prompt_text,
                "c": [choice["text"] for choice in choices],
            },
            ensure_ascii=False,
        ).encode("utf-8")
    ).hexdigest()[:12]
    skill = " ".join(str(payload.get("skill") or "").split()).title()
    question = {
        "stable_id": (
            f"flint-rw-{section.casefold()}-{number:06d}-{digest}"
        ),
        "module": f"gen-{section.casefold()}",
        "number": number,
        "skill": skill or skill_hint,
        "section": section,
        "prompt": prompt_text,
        "choices": choices,
        "answer": "",
        "source": f"{SAT_RW_MODEL} (generated)",
        "source_pdf": "",
    }
    try:
        _RW_GENERATED_DIR.mkdir(parents=True, exist_ok=True)
        path = _RW_GENERATED_DIR / (
            f"rw-{section.casefold()}-{number:06d}-{digest}.json"
        )
        temporary = path.with_name(path.name + ".tmp")
        temporary.write_text(
            json.dumps(question, ensure_ascii=False, indent=2) + "\n",
            encoding="utf-8",
        )
        temporary.replace(path)
        meta_path = _RW_GENERATED_DIR / _RW_GENERATED_META_FILE
        meta_path.write_text(
            json.dumps(
                {
                    "count": number,
                    "written_at": time.time(),
                    "model": SAT_RW_MODEL,
                    "source": "generated",
                },
                ensure_ascii=False,
            )
            + "\n",
            encoding="utf-8",
        )
    except OSError:
        pass
    with _BANK_LOCK:
        if _BANK is not None:
            _BANK.setdefault("questions", []).append(question)
    return question


def sat_rw_domains() -> list[dict]:
    """Return the Reading / Writing subsections with counts."""
    questions = _load_bank().get("questions") or []
    counts = {RW_READING: 0, RW_WRITING: 0}
    for question in questions:
        section = question.get("section") in counts
        if section:
            counts[question["section"]] += 1
    return [
        {
            "domain": section,
            "count": counts[section],
            "url": (
                f"/api/sat_question?domain={section}&index=0"
            ),
        }
        for section in (RW_READING, RW_WRITING)
        if counts[section] > 0
    ]


def sat_rw_question(
    domain: str, index: int = 0, random_pick: bool = False
) -> dict | None:
    """Return one Reading/Writing question in the math card's payload shape.

    Keeping the exact same ``view``/``question`` contract lets the existing
    front-end card, the streamed Answer/analysis, and the Previous/Next
    buttons work on the RW bank without any special-casing.  With
    ``random_pick`` a random question of the section is served instead of
    the index-th one (prompts/sat.prompt: section clicks present a fresh
    question, the bank carries no preset numbered order).
    """
    normalized = str(domain or "").strip().casefold()
    if normalized not in RW_DOMAIN_NAMES:
        return None
    section_name = RW_READING if normalized == RW_READING.casefold() else RW_WRITING

    def section_pool() -> list[dict]:
        return [
            question
            for question in (_load_bank().get("questions") or [])
            if question.get("section") == section_name
        ]

    questions = section_pool()
    if not questions:
        return None
    total = len(questions)
    position = int(index) if isinstance(index, int) else int(index or 0)
    if random_pick:
        position = random.randrange(total)
        index = position
    # prompts/sat.prompt: a section must never carry a maximum question
    # count — once navigation moves past the sample-test pool, {question_llm}
    # generates the next question on demand and appends it to
    # {questions_cache_dir}/rw_generated.  Failures fall back to the previous
    # wrap-around behaviour.
    guard = 0
    while position >= total and guard < 5:
        generated = _rw_generate_question(section_name)
        questions = section_pool()
        total = len(questions)
        if generated is None or not total:
            break
        guard += 1
    if not total:
        return None
    # Same wrap semantics as the math path (index % total); after successful
    # on-demand generation position points exactly at the new question.
    position = position % total
    selected = questions[position]
    return {
        "view": "question",
        "domain": section_name,
        "index": position,
        "total": total,
        "analysis_model": _analysis_model(),
        "question": {
            "stable_id": selected["stable_id"],
            "subject": "Reading And Writing",
            "domain": section_name,
            "skill": selected["skill"],
            "difficulty": "",
            "prompt": selected["prompt"],
            "choices": selected["choices"],
            "source": selected["source"],
            "source_url": "",
        },
    }
