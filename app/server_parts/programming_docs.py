"""Official Go, Rust, and PHP documentation lookups."""

from __future__ import annotations

from .core import *
from .config import PROGRAMMING_MODEL

def golang_package_docs_request(question: str) -> bool:
    """Return True for a Go code-sample question that names or implies a package.

    ``golang_doc_search.py`` already performs this check with the keyword
    model, but the server needs a cheap, deterministic way to route questions
    such as "how do I use sync.WaitGroup" into coding mode and the Go
    documentation path. Package detection and the code-sample regex are
    imported from the script so both layers agree on what counts as a Go
    package mention; ``candidate_packages`` also maps concept words such as
    "iterator" to the related ``iter`` package.
    """
    try:
        import golang_doc_search as go_docs
    except ImportError:
        return False
    text = " ".join(str(question or "").split())
    if not text:
        return False
    wants_code = bool(
        go_docs.CODE_SAMPLE_RE.search(text)
        or CODE_REQUEST_PATTERN.search(text)
    )
    if not wants_code:
        return False
    return bool(go_docs.candidate_packages(text))

def rust_crate_docs_request(question: str) -> bool:
    """Return True for a Rust code-sample question that names a crate.

    ``rust_doc_search.py`` performs the same check with the keyword model,
    but the server needs a cheap, deterministic way to route questions such
    as "how do I use serde to deserialize JSON" into coding mode and the Rust
    documentation path. The script's code-sample regex and crate detector are
    imported so both layers agree on what counts as a crate mention.
    """
    try:
        import rust_doc_search as rust_docs
    except ImportError:
        return False
    text = " ".join(str(question or "").split())
    if not text:
        return False
    wants_code = bool(
        rust_docs.CODE_SAMPLE_RE.search(text)
        or CODE_REQUEST_PATTERN.search(text)
    )
    if not wants_code:
        return False
    return bool(rust_docs.candidate_crates(text))

def rust_code_docs_request(question: str) -> bool:
    """Return True for any Rust code-sample question.

    Rust code samples must come from the official Rust documentation
    (doc.rust-lang.org and docs.rs), never from the GitHub
    code-search fallback. This check does not require a crate name, so a
    question such as "show me how to iterate over a HashMap" is routed to
    ``rust_doc_search.py`` as well.
    """
    try:
        import rust_doc_search as rust_docs
    except ImportError:
        return False
    text = " ".join(str(question or "").split())
    if not text:
        return False
    return bool(
        rust_docs.CODE_SAMPLE_RE.search(text)
        or CODE_REQUEST_PATTERN.search(text)
        or CHINESE_CODING_PATTERN.search(question)
    )

def php_symbol_docs_request(question: str) -> bool:
    """Return True for a PHP code-sample question that names a symbol.

    ``php_doc_search.py`` performs the same check with the keyword model,
    but the server needs a cheap, deterministic way to route questions such
    as "show me code sample using array_map" into coding mode and the PHP
    manual path. The script's code-sample regex and symbol detector are
    imported so both layers agree on what counts as a PHP symbol mention.
    """
    try:
        import php_doc_search as php_docs
    except ImportError:
        return False
    text = " ".join(str(question or "").split())
    if not text:
        return False
    wants_code = bool(
        php_docs.CODE_SAMPLE_RE.search(text)
        or CODE_REQUEST_PATTERN.search(text)
    )
    if not wants_code:
        return False
    return bool(php_docs.candidate_symbols(text))

def documentation_code_request(language: str | None, question: str) -> bool:
    """Return True when a documentation-backed language asks for code.

    The language documentation scripts use their own ``CODE_SAMPLE_RE``,
    which recognises requests such as "write a CLI" that the generic server
    detector may miss. Mirroring that check here guarantees every code
    request in Go, Rust, or PHP enters coding mode.
    """
    normalized = normalize_language(language)
    if not language_uses_official_docs(normalized):
        return False
    text = " ".join(str(question or "").split())
    if not text:
        return False
    if CODE_REQUEST_PATTERN.search(text) or CHINESE_CODING_PATTERN.search(
        question
    ):
        return True
    try:
        if normalized == GO_LANGUAGE:
            import golang_doc_search as docs
        elif normalized == RUST_LANGUAGE:
            import rust_doc_search as docs
        else:
            import php_doc_search as docs
    except ImportError:
        return False
    return bool(docs.CODE_SAMPLE_RE.search(text))

def _run_doc_search(
    script_name: str,
    question: str,
    *,
    timeout: float,
    log_prefix: str,
    start_message: str,
    success_message: str,
    model: str = PROGRAMMING_MODEL,
    log: Callable[[str], None] | None = None,
    emit: Callable[[str], None] | None = None,
) -> tuple[str | None, str]:
    """Run an official documentation search script and stream its stdout.

    The script prints the documentation answer incrementally. ``emit`` is
    called with each decoded stdout chunk as soon as it arrives, so the HTTP
    handler can forward it to the browser without waiting for the whole
    subprocess to finish. Progress lines from stderr are forwarded through
    ``log`` in the same way.

    Returns ``(answer, "")`` on success and ``(error_answer, error)`` on
    failure. The returned text includes the Sources section printed by the
    script; when a run fails after producing partial output, that partial
    answer is returned together with the error.
    """
    script = SCRIPT_DIR / script_name
    if not script.is_file():
        return None, f"missing script: {script}"

    if log is not None:
        log(f"[{log_prefix}: {start_message}]")
    try:
        process = subprocess.Popen(
            [sys.executable, "-u", str(script), question, "--model", model],
            cwd=SCRIPT_DIR,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            bufsize=0,
        )
    except OSError as exc:
        return None, f"could not run {script_name}: {exc}"

    assert process.stdout is not None and process.stderr is not None
    selector = selectors.DefaultSelector()
    selector.register(process.stdout, selectors.EVENT_READ, "stdout")
    selector.register(process.stderr, selectors.EVENT_READ, "stderr")
    decoder = codecs.getincrementaldecoder("utf-8")("replace")
    fence_filter = CodeFenceRepairFilter()
    answer_parts: list[str] = []
    stderr_text = ""
    stderr_line_buffer = ""
    deadline = time.monotonic() + timeout
    timed_out = False
    returncode: int | None = None

    def deliver_stdout(text: str) -> None:
        """Repair a stdout chunk before it is stored or streamed."""
        repaired = fence_filter.feed(text)
        if not repaired:
            return
        answer_parts.append(repaired)
        if emit is not None:
            emit(repaired)

    try:
        while selector.get_map():
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                timed_out = True
                try:
                    process.kill()
                except OSError:
                    pass
                break
            events = selector.select(timeout=min(remaining, 1.0))
            for key, _mask in events:
                chunk = os.read(key.fileobj.fileno(), 4096)
                if not chunk:
                    selector.unregister(key.fileobj)
                    continue
                if key.data == "stdout":
                    text = decoder.decode(chunk)
                    if not text:
                        continue
                    deliver_stdout(text)
                else:
                    decoded = chunk.decode("utf-8", "replace")
                    stderr_text += decoded
                    stderr_line_buffer += decoded
                    while "\n" in stderr_line_buffer:
                        line, stderr_line_buffer = stderr_line_buffer.split(
                            "\n", 1
                        )
                        line = line.strip()
                        if (
                            line
                            and not line.startswith("Warning:")
                            and log is not None
                        ):
                            log(f"[{log_prefix}: {line}]")

        tail = decoder.decode(b"", final=True)
        if tail:
            deliver_stdout(tail)
        fence_tail = fence_filter.flush()
        if fence_tail:
            answer_parts.append(fence_tail)
            if emit is not None:
                emit(fence_tail)
        trailing_line = stderr_line_buffer.strip()
        if (
            trailing_line
            and not trailing_line.startswith("Warning:")
            and log is not None
        ):
            log(f"[{log_prefix}: {trailing_line}]")

        if not timed_out:
            try:
                returncode = process.wait(timeout=5)
            except subprocess.TimeoutExpired:
                timed_out = True
                try:
                    process.kill()
                except OSError:
                    pass
                returncode = process.wait()
        else:
            returncode = process.wait()
    except BaseException:
        # Includes ClientDisconnected raised by the SSE emitter. Always reap
        # the child so a closed browser tab does not leave the model running.
        try:
            process.kill()
        except OSError:
            pass
        try:
            process.wait(timeout=5)
        except subprocess.TimeoutExpired:
            pass
        raise
    finally:
        selector.close()
        for stream in (process.stdout, process.stderr):
            try:
                stream.close()
            except OSError:
                pass

    answer = "".join(answer_parts).strip()
    if timed_out:
        return (answer or None), f"{script_name} timed out"
    if returncode != 0:
        lines = stderr_text.strip().splitlines()
        reason = lines[-1].strip() if lines else ""
        return (answer or None), reason or f"exit status {returncode}"
    if not answer:
        return None, f"{script_name} returned an empty answer"

    if log is not None:
        log(f"[{log_prefix}: {success_message}]")
    return answer, ""

def _load_programming_docs_index(
    language: str,
) -> tuple[object, list[dict], object | None] | None:
    """Load the cached official-docs index for ``language``.

    The documentation scripts already crawl and cache their language docs.
    Reusing that cache avoids a second crawler and guarantees the excerpts
    shown in a chat answer come from the same reference source described by
    ``PROGRAMMING_DOCS_MAP``. Returns ``None`` when the language is not in the
    map or the cache has not been built yet; the caller then keeps the normal
    transcript-only answer path.
    """
    normalized = normalize_language(language)
    descriptor = PROGRAMMING_DOCS_MAP.get(normalized)
    if descriptor is None:
        return None
    try:
        module = importlib.import_module(descriptor["module"])
    except ImportError:
        return None

    cache_dir = Path(module.DEFAULT_CACHE_DIR).expanduser()
    try:
        cache_file = module.cache_path(cache_dir)
        cache_mtime = cache_file.stat().st_mtime
    except (AttributeError, OSError):
        return None

    with _PROGRAMMING_DOCS_INDEX_LOCK:
        cached = _PROGRAMMING_DOCS_INDEX_CACHE.get(normalized)
        if (
            cached is not None
            and cached[0] == cache_mtime
            and cached[1] is module
        ):
            return module, cached[2], cached[3]

    args = argparse.Namespace(
        cache_dir=str(cache_dir),
        offline=True,
        refresh=False,
        max_pages=getattr(module, "DEFAULT_MAX_PAGES", 250),
    )
    try:
        with redirect_stdout(io.StringIO()), redirect_stderr(io.StringIO()):
            pages = module.get_pages(args)
    except SystemExit:
        return None
    except Exception:
        return None
    if not pages:
        return None

    try:
        chunks = module.chunk_pages(pages)
    except Exception:
        return None
    if not chunks:
        return None

    # Curated notes in ./knowledge/<tag> supplement the official manual, just
    # as they do when the documentation script answers by itself.
    try:
        from knowledge_docs import knowledge_chunks

        chunks.extend(
            knowledge_chunks(
                descriptor["knowledge_tag"], module.chunk_pages
            )
        )
    except Exception:
        pass

    bm25 = None
    bm25_class = getattr(module, "BM25Okapi", None)
    if bm25_class is not None:
        try:
            bm25 = bm25_class(
                [
                    module.tokenize(
                        f"{chunk.get('text', '')} {chunk.get('title', '')}"
                    )
                    for chunk in chunks
                ]
            )
        except Exception:
            bm25 = None

    with _PROGRAMMING_DOCS_INDEX_LOCK:
        _PROGRAMMING_DOCS_INDEX_CACHE[normalized] = (
            cache_mtime,
            module,
            chunks,
            bm25,
        )
    return module, chunks, bm25

def _rank_programming_docs(
    module,
    chunks: list[dict],
    bm25,
    question: str,
    limit: int,
) -> list[tuple[float, dict]]:
    """Rank documentation excerpts for ``question`` with the docs tokenizer."""
    keywords = chat.tokenize(question) or [question]
    if bm25 is None:
        try:
            return module.rank_chunks(question, keywords, chunks, limit)
        except Exception:
            return []

    query_text = " ".join(keywords * 2) + " " + question
    query_tokens = module.tokenize(query_text) or [question.lower()]
    try:
        scores = [float(score) for score in bm25.get_scores(query_tokens)]
    except Exception:
        return []
    try:
        from knowledge_docs import boost_knowledge_scores

        boost_knowledge_scores(chunks, scores)
    except Exception:
        pass

    order = sorted(
        range(len(chunks)), key=lambda index: scores[index], reverse=True
    )
    selected: list[tuple[float, dict]] = []
    per_page: dict[str, int] = {}
    seen_text: set[str] = set()
    for index in order:
        if len(selected) >= limit:
            break
        score = scores[index]
        if score <= 0:
            continue
        chunk = chunks[index]
        url = str(chunk.get("url") or "")
        if (
            not chunk.get("knowledge")
            and per_page.get(url, 0) >= PROGRAMMING_DOCS_CHUNKS_PER_PAGE
        ):
            continue
        fingerprint = str(chunk.get("text") or "")[:160]
        if fingerprint in seen_text:
            continue
        seen_text.add(fingerprint)
        per_page[url] = per_page.get(url, 0) + 1
        selected.append((score, chunk))
    return selected

def programming_docs_context(
    language: str, question: str
) -> tuple[str, dict]:
    """Build an official-documentation reference block for a chat prompt.

    The block is added next to the normal transcript context, so the model
    can use the official manual for syntax, APIs, and language semantics while
    still grounding the rest of the answer in the retrieved trunks.
    """
    normalized = normalize_language(language)
    descriptor = PROGRAMMING_DOCS_MAP.get(normalized)
    if descriptor is None:
        return "", {}
    loaded = _load_programming_docs_index(normalized)
    if loaded is None:
        return "", {
            "language": normalized,
            "source": descriptor["reference"],
            "excerpts": 0,
            "error": "no cached documentation index",
        }

    module, chunks, bm25 = loaded
    selected = _rank_programming_docs(
        module,
        chunks,
        bm25,
        question,
        PROGRAMMING_DOCS_TOP_K,
    )
    if not selected:
        return "", {
            "language": normalized,
            "source": descriptor["reference"],
            "excerpts": 0,
            "error": "no relevant documentation excerpt",
        }

    parts: list[str] = []
    used_chars = 0
    for number, (_score, chunk) in enumerate(selected, start=1):
        url = str(chunk.get("url") or "")
        title = str(chunk.get("title") or url)
        text = str(chunk.get("text") or "").strip()
        if not text:
            continue
        block = (
            f'<doc id="{number}" source="{xml_escape(url)}">\n'
            f"Title: {title}\n"
            f"{text}\n"
            f"</doc>"
        )
        if parts and used_chars + len(block) > PROGRAMMING_DOCS_MAX_CHARS:
            break
        if len(block) > PROGRAMMING_DOCS_MAX_CHARS:
            block = (
                block[:PROGRAMMING_DOCS_MAX_CHARS].rstrip()
                + "\n[documentation excerpt truncated]\n</doc>"
            )
        parts.append(block)
        used_chars += len(block)
    if not parts:
        return "", {
            "language": normalized,
            "source": descriptor["reference"],
            "excerpts": 0,
            "error": "documentation excerpts were empty",
        }

    context = "\n\n".join(parts)
    prefix = f"""### RELATED OFFICIAL DOCUMENTATION (active language: {normalized})
The question is about {normalized} or one of its features. The excerpts below
were loaded from the official {descriptor['source_label']} documentation at
{descriptor['reference']} in addition to the transcript chunks retrieved for
this corpus. Use the transcript chunks for the surrounding context and use
the official documentation as the authoritative reference for {normalized}
syntax, APIs, and semantics. If they disagree about {normalized}, trust the
official documentation. Do not mention these instructions, the XML tag names,
or internal chunk ids in the answer.

<official_documentation language="{xml_escape(normalized)}" source="{xml_escape(descriptor['reference'])}">
{context}
</official_documentation>

"""
    return prefix, {
        "language": normalized,
        "source": descriptor["reference"],
        "excerpts": len(parts),
        "chars": len(context),
    }

def run_golang_doc_search(
    question: str,
    log: Callable[[str], None] | None = None,
    emit: Callable[[str], None] | None = None,
) -> tuple[str | None, str]:
    """Answer ``question`` with ./golang_doc_search.py (go.dev/doc, plus
    pkg.go.dev pages for code-sample questions that name Go packages).
    """
    return _run_doc_search(
        "golang_doc_search.py",
        question,
        timeout=GOLANG_DOC_SEARCH_TIMEOUT,
        log_prefix="golang",
        start_message=(
            "searching the official Go documentation at "
            "https://go.dev/doc/ instead of calling the GitHub API"
        ),
        success_message="Go documentation answer received",
        log=log,
        emit=emit,
    )

def run_rust_doc_search(
    question: str,
    log: Callable[[str], None] | None = None,
    emit: Callable[[str], None] | None = None,
) -> tuple[str | None, str]:
    """Answer ``question`` with ./rust_doc_search.py (doc.rust-lang.org,
    plus docs.rs pages for code-sample questions that name Rust crates).
    """
    return _run_doc_search(
        "rust_doc_search.py",
        question,
        timeout=RUST_DOC_SEARCH_TIMEOUT,
        log_prefix="rust",
        start_message=(
            "searching the official Rust documentation at "
            "https://doc.rust-lang.org/ instead of calling the GitHub API"
        ),
        success_message="Rust documentation answer received",
        log=log,
        emit=emit,
    )

def run_php_doc_search(
    question: str,
    log: Callable[[str], None] | None = None,
    emit: Callable[[str], None] | None = None,
) -> tuple[str | None, str]:
    """Answer ``question`` with ./php_doc_search.py (www.php.net/manual/en/,
    plus dedicated manual pages for named functions, classes, and extensions).
    """
    return _run_doc_search(
        "php_doc_search.py",
        question,
        timeout=PHP_DOC_SEARCH_TIMEOUT,
        log_prefix="php",
        start_message=(
            "searching the official PHP documentation at "
            "https://www.php.net/manual/en/ instead of calling the GitHub API"
        ),
        success_message="PHP documentation answer received",
        log=log,
        emit=emit,
    )

def merge_doc_answer(
    chat_answer: str | None, doc_answer: str, heading: str
) -> str:
    """Merge the local knowledge answer with an official docs answer."""
    doc_answer = doc_answer.strip()
    local_answer = (chat_answer or "").strip()
    if not local_answer or answer_is_insufficient(local_answer):
        return doc_answer + "\n"
    # The local answer already contains the documentation text; do not repeat
    # the whole documentation response.
    if doc_answer and doc_answer.lower() in local_answer.lower():
        return local_answer + "\n"
    return (
        f"{local_answer}\n\n"
        "---\n\n"
        f"### {heading}\n\n"
        f"{doc_answer}\n"
    )

def merge_golang_doc_answer(
    chat_answer: str | None, doc_answer: str
) -> str:
    """Merge the local knowledge answer with the go.dev/doc answer."""
    return merge_doc_answer(
        chat_answer,
        doc_answer,
        "Go documentation reference (go.dev/doc)",
    )

def merge_rust_doc_answer(
    chat_answer: str | None, doc_answer: str
) -> str:
    """Merge the local knowledge answer with the doc.rust-lang.org answer."""
    return merge_doc_answer(
        chat_answer,
        doc_answer,
        "Rust documentation reference (doc.rust-lang.org)",
    )

def merge_php_doc_answer(
    chat_answer: str | None, doc_answer: str
) -> str:
    """Merge the local knowledge answer with the www.php.net answer."""
    return merge_doc_answer(
        chat_answer,
        doc_answer,
        "PHP documentation reference (www.php.net/manual/en)",
    )

def documentation_descriptor(language: str | None) -> dict | None:
    """Return the official-documentation configuration for a language.

    Go, Rust, and PHP code answers must be grounded in go.dev/doc,
    doc.rust-lang.org, or www.php.net/manual/en/ before the general local
    model is allowed to generate code.
    """
    normalized = normalize_language(language)
    if normalized == GO_LANGUAGE:
        return {
            "name": "go",
            "source_label": "go.dev/doc",
            "source_name": "Go documentation (go.dev/doc)",
            "source_url": "https://go.dev/doc/",
            "heading": "Go documentation reference (go.dev/doc)",
            "runner": run_golang_doc_search,
            "merge": merge_golang_doc_answer,
        }
    if normalized == RUST_LANGUAGE:
        return {
            "name": "rust",
            "source_label": "doc.rust-lang.org",
            "source_name": "Rust documentation (doc.rust-lang.org)",
            "source_url": "https://doc.rust-lang.org/",
            "heading": "Rust documentation reference (doc.rust-lang.org)",
            "runner": run_rust_doc_search,
            "merge": merge_rust_doc_answer,
        }
    if normalized == PHP_LANGUAGE:
        return {
            "name": "php",
            "source_label": "www.php.net/manual/en",
            "source_name": "PHP documentation (www.php.net/manual/en)",
            "source_url": "https://www.php.net/manual/en/",
            "heading": (
                "PHP documentation reference (www.php.net/manual/en)"
            ),
            "runner": run_php_doc_search,
            "merge": merge_php_doc_answer,
        }
    return None

# Official documentation answers are also used as a reference for local code
# generation when the excerpts are incomplete. Keep that reference bounded so
# it cannot crowd out the model's answer window.
DOCUMENTATION_REFERENCE_MAX_CHARS = 8000

DOCUMENTATION_INSUFFICIENT_PATTERN = re.compile(
    r"(?:excerpts?\s+(?:do|does|did)\s+not\s+contain"
    r"|(?:indexed\s+)?[\w.]+\s+documentation\s+(?:excerpts?\s+)?"
    r"(?:does|do|did)\s+not\s+contain"
    r"|(?:cannot|can't|unable\s+to)\s+(?:provide|give|write)\s+"
    r"(?:a\s+)?(?:complete|full)\s+answer"
    r"|not\s+contain\s+the\s+information\s+needed"
    r"|(?:based\s+solely|only)\s+on\s+the\s+provided\s+excerpts)",
    re.IGNORECASE,
)

# A fenced code sample is a strong signal that a documentation answer is
# complete even when it opens with a caveat about the indexed excerpts. The
# docs assistants often write "the excerpts do not contain X" and then still
# provide a full working sample, so the caveat alone must not classify the
# answer as a refusal and trigger a second, duplicate generation.
ANSWER_CODE_FENCE_PATTERN = re.compile(
    r"```[^\n]*\n(.*?)```", re.DOTALL
)

def answer_has_code_sample(answer: str | None) -> bool:
    """Return True when an answer contains a substantive fenced code sample."""
    text = str(answer or "")
    for match in ANSWER_CODE_FENCE_PATTERN.finditer(text):
        body = match.group(1)
        lines = [line for line in body.splitlines() if line.strip()]
        if len(lines) >= 3 or len(body.strip()) >= 120:
            return True
    return False

def documentation_answer_is_insufficient(answer: str | None) -> bool:
    """Return True when an official-docs answer is a refusal.

    The documentation scripts use wording such as "the indexed Go
    documentation excerpts do not contain ...", which differs from the
    transcript prompt's refusal phrase. Coding-mode answers are rescued by
    the local model when either form appears.
    """
    if not answer or not answer.strip():
        return True
    # A code sample means the answer is usable even if a caveat sentence
    # mentions that the indexed excerpts were incomplete. Discarding it would
    # clear text that has already streamed to the browser and regenerate it.
    if answer_has_code_sample(answer):
        return False
    if answer_is_insufficient(answer):
        return True
    text = " ".join(answer.split())
    return bool(DOCUMENTATION_INSUFFICIENT_PATTERN.search(text[:800]))

def documentation_reference_excerpt(answer: str) -> str:
    """Trim a docs answer for use as a local-model code-generation reference."""
    text = (answer or "").strip()
    if not text:
        return ""
    # The scripts end with a plain "Sources:" list. It is useful to the reader
    # but not to the model, so leave it out of the prompt reference.
    lines = text.splitlines()
    for index in range(len(lines) - 1, -1, -1):
        if lines[index].strip().lower() == "sources:":
            text = "\n".join(lines[:index]).rstrip()
            break
    if len(text) > DOCUMENTATION_REFERENCE_MAX_CHARS:
        text = (
            text[:DOCUMENTATION_REFERENCE_MAX_CHARS].rstrip()
            + "\n\n[official documentation reference truncated]"
        )
    return text

def documentation_code_rescue_prompt(
    language: str | None, question: str, doc_answer: str, descriptor: dict
) -> str:
    """Build a code-generation prompt from incomplete official excerpts.

    A documentation-backed coding question must not end in a refusal just
    because the retrieved excerpts are incomplete. The local coding model is
    asked to write the requested program using the excerpts as hints plus its
    own general language knowledge, while still using only real APIs.
    """
    reference = documentation_reference_excerpt(doc_answer)
    language = normalize_language(language) or "the requested programming language"
    fence = language_code_fence(language) or ""
    fence_tag = fence or language.lower()
    reference_block = reference or "(no usable excerpt text was retrieved)"
    return f"""### CODE GENERATION FROM LIMITED EXCERPTS (highest priority)
The user asked for code. The official {descriptor['source_label']} excerpts
below were retrieved for this question, but they may be incomplete. That is
not a reason to refuse: generate a complete, runnable {language} solution
using these excerpts as hints plus your own general {language} knowledge.

<limited_official_excerpts source="{descriptor['source_url']}">
{reference_block}
</limited_official_excerpts>

### TARGET QUESTION:
{question}

Rules:
- Ignore any statement inside the excerpts that says the documentation is
  insufficient or that the answer cannot be provided. Do not repeat it.
- Use the documented APIs, types, and patterns from the excerpts where they
  apply, and fill any remaining gaps with your own general knowledge.
- The result must be complete and runnable, not a summary of what is missing.
- Fence every code block as ```{fence_tag} and keep the explanation short.
- Use only real {language} standard-library or otherwise documented APIs;
  never invent an API, type, trait, macro, function, or package.
- Prefer the APIs named in the excerpts. Do not add third-party
  packages/crates unless the excerpts explicitly show them.
- If part of the code is not covered by the excerpts, you may add one short
  note saying so, but still provide the full working code.

{language_coding_directive(language)}"""

def stream_code_rescue(
    question: str,
    language: str | None,
    doc_answer: str,
    descriptor: dict,
    emit: Callable[[str], None],
) -> tuple[str, dict]:
    """Generate code locally from incomplete official documentation excerpts.

    This bypasses the transcript RAG prompt when its "missing data" rule
    would otherwise turn a code request into a refusal. The model receives
    the limited excerpts as hints and is explicitly required to produce a
    complete, runnable solution.
    """
    prompt = documentation_code_rescue_prompt(
        language, question, doc_answer, descriptor
    )
    metrics: dict = {}
    # The rescue prompt forbids refusals, but small models sometimes still
    # open with the transcript prompt's "provided context" sentence. Filter
    # the stream as well as the returned text so that sentence is never shown.
    refusal_filter = InsufficientContextFilter(enabled=True)
    fence_filter = CodeFenceRepairFilter()

    def emit_filtered(text: str) -> None:
        cleaned = refusal_filter.feed(text)
        if not cleaned:
            return
        cleaned = fence_filter.feed(cleaned)
        if cleaned:
            emit(cleaned)

    response = chat.stream_answer(
        prompt,
        model=language_coding_model(language),
        think=False,
        metrics=metrics,
        emit=emit_filtered,
    )
    tail = refusal_filter.flush()
    if tail:
        tail = fence_filter.feed(tail)
        if tail:
            emit(tail)
    fence_tail = fence_filter.flush()
    if fence_tail:
        emit(fence_tail)
    response = strip_leading_insufficient_context(response)
    return repair_code_fences(response), metrics

FOLLOW_UP_PRONOUNS = re.compile(
    r"\b(?:he|she|him|her|his|hers|it|its|they|them|their|theirs|"
    r"this|that|these|those|former|latter|same)\b",
    re.IGNORECASE,
)

FOLLOW_UP_OPENERS = re.compile(
    r"^\s*(?:and|also|but|so|then|what about|how about|what else|"
    r"anything else|why|how so|really|tell me more|more|continue|go on|"
    r"elaborate|expand|which one)\b",
    re.IGNORECASE,
)

INSUFFICIENT_ANSWER_PATTERN = re.compile(
    r"(?:"
    r"(?:the\s+)?(?:provided|available|given\s+)?context\s+"
    r"(?:does\s+not|doesn't|did\s+not|didn't)\s+"
    r"(?:contain|include|provide)\s+"
    r"(?:(?:sufficient|enough|any)\s+(?:information|details?|data|evidence)"
    r"|(?:the\s+)?(?:information|details?|data|evidence)\s+"
    r"(?:needed|required)\s+to\s+answer"
    r"|(?:information|details?|data|evidence)\s+(?:about|regarding|on)\s+"
    r"(?:this|the|that)\s+(?:question|topic|request))"
    r"|insufficient\s+(?:information|context|data|evidence)"
    r"|not\s+(?:enough|sufficient)\s+(?:information|context|data|evidence)"
    r"|(?:there\s+is|there's)\s+no\s+(?:relevant\s+)?"
    r"(?:information|context|data|details?)"
    r"|no\s+(?:relevant\s+)?(?:information|details?|data)\s+"
    r"(?:is\s+)?(?:available|provided|found)\s+in\s+the\s+"
    r"(?:provided\s+)?context"
    r"|(?:i\s+)?(?:do\s+not|don't)\s+have\s+(?:enough|sufficient)\s+"
    r"(?:information|context|data)"
    r"|(?:based\s+on\s+)?(?:the\s+)?(?:provided|available|given\s+)?"
    r"context\s*,?\s*(?:i\s+)?(?:cannot|can't|am\s+unable\s+to)\s+"
    r"(?:answer|determine)"
    r"|(?:i\s+)?(?:cannot|can't|am\s+unable\s+to)\s+(?:answer|determine)\s+"
    r"(?:this|the|that)\s+question\s*(?:[,;:]?\s*)?"
    r"(?:from|in|using|based\s+on)?\s*(?:the\s+)?"
    r"(?:provided|available|given\s+)?context"
    r"|(?:the\s+)?(?:provided|available|given\s+)?context\s+"
    r"(?:is|seems)\s+(?:insufficient|not\s+enough|too\s+limited)"
    r"|(?:i\s+)?(?:cannot|can't|am\s+unable\s+to)\s+(?:answer|determine)\s+"
    r"(?:this|the|that)\s+question\s*[.!]?\s*$"
    r")",
    re.IGNORECASE,
)

def answer_is_insufficient(answer: str | None) -> bool:
    """Return True when an answer is empty or reports missing context.

    Refusals are normally the first sentence of the answer. For long answers
    only the opening is inspected, so a valid answer that mentions a missing
    detail later on is not mistaken for a refusal.
    """
    if not answer or not answer.strip():
        return True
    # Answers that contain a real code sample are not refusals, even when
    # their opening sentence notes that the retrieved context was partial.
    if answer_has_code_sample(answer):
        return False
    # Collapse whitespace so line-wrapped refusals still match the patterns.
    text = " ".join(answer.split())
    if len(text) > 1200:
        text = text[:600]
    return bool(INSUFFICIENT_ANSWER_PATTERN.search(text))

def attach_documentation_answer(
    first_response: str | None,
    first_sources,
    first_stats: dict,
    *,
    name: str,
    source_label: str,
    source_name: str,
    source_url: str,
    heading: str,
    runner: Callable[..., tuple[str | None, str]],
    merge: Callable[[str | None, str], str],
    question: str,
    emit_delta: Callable[[str], None],
    emit_reset: Callable[[str], None],
    emit_log: Callable[[str], None],
    emit_source: Callable[[dict], None],
):
    """Stream an official documentation answer and merge it with the local one.

    Shared by the Go, Rust, and PHP documentation paths. ``runner`` streams
    the documentation answer through the supplied callbacks; ``merge``
    combines it with the answer already produced from the local knowledge
    base.
    """
    local_answer_usable = bool(
        first_response and first_response.strip()
    ) and not answer_is_insufficient(first_response)
    doc_stream_started = False

    def announce(source: str, label: str, detail: str) -> None:
        emit_source(
            {
                "source": source,
                "label": label,
                "url": source_url,
                "detail": detail,
            }
        )

    def emit_doc_delta(text: str) -> None:
        nonlocal doc_stream_started
        if not doc_stream_started:
            doc_stream_started = True
            announce(
                SOURCE_DOCUMENTATION,
                source_name,
                "streaming the authoritative answer from the official "
                f"documentation at {source_url}",
            )
            if local_answer_usable:
                # The local answer is already on screen and streams above
                # this point; only add the reference divider.
                emit_delta(f"\n\n---\n\n### {heading}\n\n")
            elif first_response and first_response.strip():
                # Drop an unusable/refusal local answer as soon as the
                # documentation answer starts arriving.
                emit_reset(f"{name} documentation merge")
        emit_delta(text)

    doc_answer, doc_error = runner(
        question,
        log=emit_log,
        emit=emit_doc_delta,
    )
    if not doc_answer:
        emit_log(
            f"[{name}: documentation search unavailable: {doc_error}; "
            "keeping the local answer]"
        )
        return first_response, first_sources, first_stats

    response = merge(first_response, doc_answer)
    # Keep video annotations only for the part that came from the local
    # transcript context.
    sources = first_sources if local_answer_usable else []
    stats = dict(first_stats)
    stats[f"{name}_doc_fallback"] = {
        "used": True,
        "doc_chars": len(doc_answer),
        "local_answer_used": local_answer_usable,
        "streamed": doc_stream_started,
    }
    if doc_error:
        emit_log(
            f"[{name}: the documentation stream ended early: {doc_error}]"
        )
    if local_answer_usable:
        announce(
            SOURCE_MERGED,
            f"local LLM + {source_name}",
            "the final answer combines the local Ollama answer with the "
            f"official documentation at {source_url}",
        )
    emit_log(
        f"[{name}: merged the {source_label} answer with the local "
        "knowledge answer]"
    )
    return response, sources, stats

def split_translation_paragraphs(text: str) -> list[str]:
    """Split an answer into paragraph-sized blocks for streaming translation."""
    normalized = text.replace("\r\n", "\n").replace("\r", "\n").strip()
    if not normalized:
        return []
    return [
        block.strip("\n")
        for block in TRANSLATION_PARAGRAPH_PATTERN.split(normalized)
        if block.strip()
    ]


# Cross-component imports are placed after the definitions so a
# feature module can depend on another feature module safely.
from .config import GOLANG_DOC_SEARCH_TIMEOUT, GO_LANGUAGE, PHP_DOC_SEARCH_TIMEOUT, PHP_LANGUAGE, PROGRAMMING_DOCS_CHUNKS_PER_PAGE, PROGRAMMING_DOCS_MAP, PROGRAMMING_DOCS_MAX_CHARS, PROGRAMMING_DOCS_TOP_K, RUST_DOC_SEARCH_TIMEOUT, RUST_LANGUAGE, SOURCE_DOCUMENTATION, SOURCE_MERGED, _PROGRAMMING_DOCS_INDEX_CACHE, _PROGRAMMING_DOCS_INDEX_LOCK
from .core import SCRIPT_DIR
from .programming import CHINESE_CODING_PATTERN, CODE_REQUEST_PATTERN, language_code_fence, language_coding_directive, language_coding_model, language_uses_official_docs, normalize_language
from .translation import TRANSLATION_PARAGRAPH_PATTERN
