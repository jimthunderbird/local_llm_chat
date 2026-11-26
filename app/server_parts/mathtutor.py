"""Math tutor document uploads and generated practice problems."""

from __future__ import annotations

from .core import *
from .core import SCRIPT_DIR

# ---- /mathtutor ------------------------------------------------------
# A mathtutor session owns the Word test papers the student uploaded and the
# practice problems generated from them. Everything persists on disk so the
# session survives a server restart:
#   .chat/mathtutor/uploaded_word_docs/<session>_<filename>
#   .chat/mathtutor/sessions/<unixtimestamp>.context.json
MATHUTOR_DIR = SCRIPT_DIR / ".chat" / "mathtutor"

MATHUTOR_UPLOAD_DIR = MATHUTOR_DIR / "uploaded_word_docs"

MATHUTOR_SESSIONS_DIR = MATHUTOR_DIR / "sessions"

MATHUTOR_LOCK = threading.RLock()

MATHUTOR_SESSION_ID_RE = re.compile(r"\d{10,16}")

MATHUTOR_MAX_FILES = 10

MATHUTOR_MAX_FILE_BYTES = 8 * 1024 * 1024

MATHUTOR_MAX_TOTAL_BYTES = 12 * 1024 * 1024

# The tutor generates practice problems five at a time. The upload creates the
# first five; the browser asks for another five when the student reaches the
# end of the current set (problem offset divisible by five).
MATHUTOR_PROBLEMS_PER_BATCH = 5

MATHUTOR_GENERATION_ATTEMPTS = 5

MATHUTOR_GENERATION_TOKENS = 5_000

MATHUTOR_SOURCE_CHAR_LIMIT = 16_000

# The mathtutor uses the answer model (ornith-1.5:35b), as required by
# performance_improvement.prompt's back_to_ornith_35b task.
MATHUTOR_MODEL = chat.ANSWER_MODEL

def _docx_text_from_bytes(data: bytes) -> str:
    """Extract readable text from a .docx without requiring python-docx."""
    try:
        with zipfile.ZipFile(io.BytesIO(data)) as archive:
            try:
                document_xml = archive.read("word/document.xml")
            except KeyError as exc:
                raise ValueError(
                    "The Word document has no word/document.xml part"
                ) from exc
    except zipfile.BadZipFile as exc:
        raise ValueError("The .docx file is not a valid Word document") from exc

    try:
        root = ElementTree.fromstring(document_xml)
    except ElementTree.ParseError as exc:
        raise ValueError("The Word document XML could not be parsed") from exc

    parts: list[str] = []

    def walk(element: ElementTree.Element) -> None:
        tag = element.tag if isinstance(element.tag, str) else ""
        local = tag.rsplit("}", 1)[-1] if tag else ""
        if local == "p":
            if parts and not parts[-1].endswith("\n"):
                parts.append("\n")
            for child in element:
                walk(child)
            parts.append("\n")
            return
        if local == "t":
            parts.append(element.text or "")
            return
        if local == "tab":
            parts.append("\t")
            return
        if local in {"br", "cr"}:
            parts.append("\n")
            return
        for child in element:
            walk(child)

    walk(root)
    text = "".join(parts)
    text = re.sub(r"[ \t]+\n", "\n", text)
    text = re.sub(r"\n{3,}", "\n\n", text)
    return text.strip()

def _legacy_doc_text_from_bytes(data: bytes) -> str:
    """Use an installed converter for the old binary .doc format."""
    temp_path = ""
    try:
        with tempfile.NamedTemporaryFile(suffix=".doc", delete=False) as handle:
            handle.write(data)
            temp_path = handle.name
        commands = []
        textutil = shutil.which("textutil")
        if textutil:
            commands.append([textutil, "-convert", "txt", "-stdout", temp_path])
        antiword = shutil.which("antiword")
        if antiword:
            commands.append([antiword, temp_path])
        catdoc = shutil.which("catdoc")
        if catdoc:
            commands.append([catdoc, temp_path])
        errors: list[str] = []
        for command in commands:
            try:
                result = subprocess.run(
                    command,
                    check=True,
                    stdout=subprocess.PIPE,
                    stderr=subprocess.PIPE,
                    timeout=60,
                )
                text = result.stdout.decode("utf-8", errors="replace").strip()
                if text:
                    return text
            except (OSError, subprocess.SubprocessError) as exc:
                errors.append(f"{Path(command[0]).name}: {exc}")
        raise ValueError(
            "Could not read the .doc file. Save it as .docx and upload it again"
            + (f" ({'; '.join(errors)})" if errors else "")
        )
    finally:
        if temp_path:
            try:
                os.unlink(temp_path)
            except OSError:
                pass

def _mathtutor_extract_document_text(name: str, data: bytes) -> str:
    suffix = Path(name).suffix.lower()
    if suffix == ".docx" or data[:2] == b"PK":
        text = _docx_text_from_bytes(data)
    elif suffix == ".doc":
        text = _legacy_doc_text_from_bytes(data)
    else:
        raise ValueError(
            f"Unsupported file type {suffix or '(none)'}; upload .docx or .doc"
        )
    text = re.sub(r"\n{3,}", "\n\n", text).strip()
    if not text:
        raise ValueError(f"No readable text was found in {name}")
    return text

def _mathtutor_session_path(session_id: str) -> Path:
    if not MATHUTOR_SESSION_ID_RE.fullmatch(str(session_id or "")):
        raise ValueError("Invalid mathtutor session id")
    return MATHUTOR_SESSIONS_DIR / f"{session_id}.context.json"

def _mathtutor_public_session(session: dict) -> dict:
    """Return the browser-safe view (reference answers stay server-side)."""
    documents = []
    for document in session.get("documents", []):
        documents.append(
            {
                "name": str(document.get("name", "")),
                "bytes": int(document.get("bytes", 0) or 0),
                "characters": int(document.get("characters", 0) or 0),
            }
        )
    problems = []
    for problem in session.get("problems", []):
        problems.append(
            {
                "id": int(problem.get("id", 0) or 0),
                "topic": str(problem.get("topic", "")),
                "question": str(problem.get("question", "")),
            }
        )
    return {
        "id": str(session.get("id", "")),
        "created_at": float(session.get("created_at", 0) or 0),
        "updated_at": float(session.get("updated_at", 0) or 0),
        "documents": documents,
        "problems": problems,
    }

def _mathtutor_load_session(session_id: str) -> dict:
    path = _mathtutor_session_path(session_id)
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except FileNotFoundError as exc:
        raise ValueError("Mathtutor session not found") from exc
    except (OSError, json.JSONDecodeError) as exc:
        raise ValueError(f"Could not read mathtutor session: {exc}") from exc
    if not isinstance(data, dict):
        raise ValueError("Malformed mathtutor session")
    data.setdefault("documents", [])
    data.setdefault("problems", [])
    data.setdefault("history", [])
    return data

def _mathtutor_latest_session() -> dict | None:
    try:
        paths = list(MATHUTOR_SESSIONS_DIR.glob("*.context.json"))
    except OSError:
        return None

    def sort_key(path: Path) -> int:
        stem = path.name.split(".", 1)[0]
        return int(stem) if stem.isdigit() else 0

    for path in sorted(paths, key=sort_key, reverse=True):
        session_id = path.name.split(".", 1)[0]
        try:
            return _mathtutor_load_session(session_id)
        except ValueError:
            continue
    return None

def _mathtutor_save_session(session: dict) -> None:
    MATHUTOR_SESSIONS_DIR.mkdir(parents=True, exist_ok=True)
    session["updated_at"] = time.time()
    path = _mathtutor_session_path(str(session.get("id", "")))
    temp_path = path.with_name(f".{path.name}.{os.getpid()}.tmp")
    try:
        temp_path.write_text(
            json.dumps(session, ensure_ascii=False, indent=2),
            encoding="utf-8",
        )
        os.replace(temp_path, path)
    finally:
        try:
            temp_path.unlink()
        except OSError:
            pass

def _mathtutor_remove_document_files(session: dict) -> int:
    """Delete the Word files that belong to one mathtutor session."""
    session_id = str(session.get("id", ""))
    upload_root = MATHUTOR_UPLOAD_DIR.resolve()
    removed = 0
    for document in session.get("documents", []):
        stored_path = str(document.get("path") or "").strip()
        if not stored_path:
            continue
        path = Path(stored_path).resolve()
        try:
            path.relative_to(upload_root)
        except ValueError:
            continue
        if path.is_file():
            path.unlink()
            removed += 1
    # Also remove files whose session entry was lost, for example after a
    # partial upload. Every stored file starts with "<session id>_".
    if session_id:
        for path in MATHUTOR_UPLOAD_DIR.glob(f"{session_id}_*"):
            if path.is_file():
                path.unlink()
                removed += 1
    return removed

def _mathtutor_remove_all_documents(session_id: str | None = None) -> int:
    """Remove uploaded Word documents and the sessions derived from them.

    When ``session_id`` is given only that session is cleared. Otherwise the
    whole mathtutor store is emptied, which is what the browser's "Remove all"
    action uses when there is no active session.
    """
    with MATHUTOR_LOCK:
        sessions: list[dict] = []
        if session_id:
            sessions.append(_mathtutor_load_session(session_id))
        else:
            try:
                paths = list(MATHUTOR_SESSIONS_DIR.glob("*.context.json"))
            except OSError:
                paths = []
            for path in paths:
                candidate = path.name.split(".", 1)[0]
                if not MATHUTOR_SESSION_ID_RE.fullmatch(candidate):
                    continue
                try:
                    sessions.append(_mathtutor_load_session(candidate))
                except ValueError:
                    sessions.append({"id": candidate, "documents": []})

        removed = 0
        for session in sessions:
            removed += _mathtutor_remove_document_files(session)
            try:
                _mathtutor_session_path(str(session.get("id", ""))).unlink()
            except FileNotFoundError:
                pass

        if session_id is None:
            try:
                for path in MATHUTOR_UPLOAD_DIR.iterdir():
                    if path.is_file():
                        path.unlink()
                        removed += 1
            except FileNotFoundError:
                pass
        return removed

def _mathtutor_source_text(session: dict, limit: int) -> str:
    chunks: list[str] = []
    remaining = limit
    for document in session.get("documents", []):
        if remaining <= 0:
            break
        text = str(document.get("text", "")).strip()
        if not text:
            continue
        name = str(document.get("name", "test paper"))
        header = f"### {name}\n"
        take = max(0, remaining - len(header))
        if take <= 0:
            break
        chunks.append(header + text[:take])
        remaining -= len(header) + min(len(text), take)
    return "\n\n".join(chunks)

def _mathtutor_parse_problems(raw: str, limit: int | None = None) -> list[dict]:
    """Parse the model's JSON problem list, tolerating prose around it."""
    text = re.sub(r"^\s*```(?:json)?\s*", "", str(raw or "").strip(), flags=re.I)
    text = re.sub(r"\s*```\s*$", "", text)
    candidates = [text]
    start = text.find("[")
    end = text.rfind("]")
    if start >= 0 and end > start:
        candidates.append(text[start : end + 1])
    start = text.find("{")
    end = text.rfind("}")
    if start >= 0 and end > start:
        candidates.append(text[start : end + 1])

    payload = None
    for candidate in candidates:
        try:
            payload = json.loads(candidate)
            break
        except json.JSONDecodeError:
            continue

    items: list = []
    if isinstance(payload, dict):
        payload = payload.get("problems") or payload.get("questions") or []
    if isinstance(payload, list):
        items = payload
    if not items:
        # Recover complete objects from a truncated array (common when the
        # model is asked for many problems). raw_decode keeps nested LaTeX
        # braces inside strings intact.
        decoder = json.JSONDecoder()
        index = 0
        while True:
            start = text.find("{", index)
            if start < 0:
                break
            try:
                item, end = decoder.raw_decode(text, start)
            except json.JSONDecodeError:
                index = start + 1
                continue
            if isinstance(item, dict):
                items.append(item)
            index = end

    problems: list[dict] = []
    seen: set[str] = set()
    for item in items:
        if not isinstance(item, dict):
            continue
        question = str(item.get("question") or "").strip()
        answer = str(item.get("answer") or "").strip()
        topic = str(item.get("topic") or "Practice").strip()
        if not question or not answer or question in seen:
            continue
        seen.add(question)
        problems.append(
            {
                "id": len(problems) + 1,
                "topic": topic or "Practice",
                "question": question,
                "answer": answer,
            }
        )
        if limit is not None and len(problems) >= limit:
            break
    if not problems:
        raise ValueError("The model returned no usable practice problems")
    for index, problem in enumerate(problems, start=1):
        problem["id"] = index
    return problems

def _mathtutor_generate_problems(
    session: dict,
    count: int = MATHUTOR_PROBLEMS_PER_BATCH,
    start_id: int = 1,
) -> list[dict]:
    source = _mathtutor_source_text(session, MATHUTOR_SOURCE_CHAR_LIMIT)
    if not source:
        raise ValueError("The uploaded test papers contain no readable text")

    problems: list[dict] = []
    # Avoid questions already in the session as well as ones generated in this
    # batch, so repeated "Next question" requests never duplicate problems.
    seen: set[str] = {
        str(problem.get("question", ""))
        for problem in session.get("problems", [])
        if isinstance(problem, dict)
    }
    last_error = ""
    attempts = 0
    while len(problems) < count and attempts < MATHUTOR_GENERATION_ATTEMPTS:
        attempts += 1
        remaining = count - len(problems)
        batch_size = remaining
        avoid_block = ""
        existing = [
            problem
            for problem in session.get("problems", [])
            if isinstance(problem, dict)
        ] + problems
        if existing:
            avoid_list = "\n".join(
                f"- {problem.get('question', '')}" for problem in existing
            )
            avoid_block = (
                "\nDo NOT repeat or paraphrase any of these already generated problems:\n"
                f"{avoid_list}\n"
            )
        prompt = f"""You are an expert math teacher. The student uploaded test papers from their course.
Study the papers and create exactly {batch_size} NEW practice problems that test the same skills, methods, and difficulty level. Do not copy a problem verbatim.
{avoid_block}
<test_papers>
{source}
</test_papers>

Return ONLY a valid JSON array containing all {batch_size} problems (no prose, no markdown fences). Each element must be an object with exactly these string fields:
  "topic": a short topic label,
  "question": the complete practice problem, including all numbers and wording,
  "answer": a complete worked reference answer with the final result.
Use LaTeX inside $...$ for math when helpful.
If a problem gives data points or refers to a table, put that data in the "question" field as a GitHub-flavored Markdown pipe table (a header row, a | --- | separator row, then one row per data point). Do not describe the table only as inline prose.
"""
        for generation_prompt, temperature in (
            (prompt, 0.35),
            (prompt + "\nIMPORTANT: output JSON only.", 0.1),
        ):
            try:
                raw = chat.generate_text(
                    generation_prompt,
                    model=MATHUTOR_MODEL,
                    temperature=temperature,
                    num_predict=MATHUTOR_GENERATION_TOKENS,
                    num_ctx=chat.ANSWER_NUM_CTX,
                )
            except RuntimeError as exc:
                last_error = str(exc)
                break
            try:
                batch = _mathtutor_parse_problems(raw, limit=batch_size)
            except ValueError as exc:
                last_error = str(exc)
                continue
            for problem in batch:
                question = problem["question"]
                if question in seen:
                    continue
                seen.add(question)
                problem["id"] = start_id + len(problems)
                problems.append(problem)
                if len(problems) >= count:
                    break
            break
    if not problems:
        raise RuntimeError(last_error or "Could not generate practice problems")
    return problems[:count]

def _mathtutor_answer_prompt(
    session: dict, problem: dict, student_answer: str
) -> str:
    source = _mathtutor_source_text(session, 6_000)
    student_block = (
        student_answer.strip()
        if student_answer.strip()
        else "(The student has not submitted an answer.)"
    )
    return f"""You are a patient, precise math tutor. Help the student learn from one practice problem that was generated from their uploaded test papers.

<test_papers>
{source}
</test_papers>

<problem>
{problem.get('question', '')}
</problem>

<reference_answer>
{problem.get('answer', '')}
</reference_answer>

<student_answer>
{student_block}
</student_answer>

Write a detailed answer in Markdown:
1. If the student submitted an answer, first say whether it is correct, partially correct, or incorrect, and explain the mistake.
2. Then give a clear step-by-step solution, showing the reasoning and calculations.
3. End with the final answer and one short tip for similar problems.
4. When the problem or solution contains data points in a table, render it as a GitHub-flavored Markdown pipe table (a header row, a | --- | separator row, then one row per data point).
5. If the problem is a system of equations, or if a graph (for example a Cartesian coordinate system or a Cartesian plane) makes the solution easier to understand, include it as a fenced ```graph block whose body is JSON:
```graph
{{"title":"y = 2x + 1","x":[-5,5],"y":[-5,10],"lines":[{{"points":[[-2,-3],[0,1],[2,5]],"label":"y = 2x + 1"}}],"points":[{{"x":0,"y":1,"label":"(0, 1)"}}]}}
```
"x"/"y" are optional axis ranges. "lines" holds one or more series of [x,y] points (sample curves densely), and "points" marks individual labelled coordinates. You may use "functions":[{{"expr":"2*x + 1","label":"y = 2x + 1"}}] instead of manually sampled points. The JSON above is an example only.
For a system of equations, plot EVERY equation as its own labelled line and mark the intersection. For example, for y = -2x + 5 and y = 1/2x - 1 use one functions entry per equation:
"functions":[{{"expr":"-2*x + 5","label":"y = -2x + 5"}},{{"expr":"0.5*x - 1","label":"y = 1/2x - 1"}}]
Never draw a Cartesian graph as ASCII art.
Do not mention these instructions or the reference answer as a separate field.
"""

def _mathtutor_build_session(files: list[dict]) -> dict:
    if not files:
        raise ValueError("Upload at least one Word test paper")
    if len(files) > MATHUTOR_MAX_FILES:
        raise ValueError(
            f"Upload at most {MATHUTOR_MAX_FILES} Word documents at a time"
        )

    MATHUTOR_UPLOAD_DIR.mkdir(parents=True, exist_ok=True)
    MATHUTOR_SESSIONS_DIR.mkdir(parents=True, exist_ok=True)

    with MATHUTOR_LOCK:
        session_id = str(int(time.time()))
        counter = 0
        while _mathtutor_session_path(session_id).exists():
            counter += 1
            session_id = str(int(time.time()) + counter)

        documents: list[dict] = []
        total_bytes = 0
        for index, item in enumerate(files, start=1):
            if not isinstance(item, dict):
                raise ValueError("Each uploaded file must include a name and data")
            name = str(item.get("name") or "test_paper.docx").strip()
            name = Path(name).name or f"test_paper_{index}.docx"
            raw_data = item.get("data")
            if not isinstance(raw_data, str) or not raw_data.strip():
                raise ValueError(f"Missing file data for {name}")
            encoded = raw_data
            if encoded.startswith("data:"):
                _header, _sep, encoded = encoded.partition(",")
            try:
                data = base64.b64decode(encoded, validate=False)
            except (ValueError, TypeError) as exc:
                raise ValueError(f"Could not decode {name}") from exc
            if len(data) > MATHUTOR_MAX_FILE_BYTES:
                raise ValueError(
                    f"{name} is larger than "
                    f"{MATHUTOR_MAX_FILE_BYTES // (1024 * 1024)} MB"
                )
            total_bytes += len(data)
            if total_bytes > MATHUTOR_MAX_TOTAL_BYTES:
                raise ValueError(
                    "The selected Word documents are too large in total "
                    f"(limit {MATHUTOR_MAX_TOTAL_BYTES // (1024 * 1024)} MB)"
                )
            text = _mathtutor_extract_document_text(name, data)

            safe_name = re.sub(r"[^A-Za-z0-9._-]+", "_", name)
            stored_name = f"{session_id}_{index}_{safe_name}"
            stored_path = MATHUTOR_UPLOAD_DIR / stored_name
            stored_path.write_bytes(data)
            documents.append(
                {
                    "name": name,
                    "stored_name": stored_name,
                    "path": str(stored_path),
                    "bytes": len(data),
                    "characters": len(text),
                    "text": text,
                }
            )

        session = {
            "id": session_id,
            "created_at": time.time(),
            "updated_at": time.time(),
            "documents": documents,
            "problems": [],
            "history": [],
        }
        try:
            session["problems"] = _mathtutor_generate_problems(session)
        except Exception:
            # Do not leave uploaded files behind when problem generation fails.
            for document in documents:
                try:
                    Path(str(document.get("path", ""))).unlink()
                except OSError:
                    pass
            raise
        _mathtutor_save_session(session)
        return session

def _mathtutor_append_history(
    session: dict, problem: dict, student_answer: str, model_answer: str
) -> None:
    history = session.setdefault("history", [])
    history.append(
        {
            "problem_id": int(problem.get("id", 0) or 0),
            "question": str(problem.get("question", "")),
            "student_answer": student_answer,
            "model_answer": model_answer,
            "at": time.time(),
        }
    )
    del history[:-100]
    _mathtutor_save_session(session)
