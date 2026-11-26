"""Question and slash-command request handling."""

from __future__ import annotations

from .core import *

class HandlerAskMixin:
    # ---- SSE: ask / commands -----------------------------------------
    def _handle_ask(
        self, question: str, image: str | None = None
    ) -> None:
        state = self.state
        with state.lock:
            state.append_history(question)
            self._begin_sse()
            try:
                self._sse("meta", state.snapshot())
                if image is not None:
                    self._run_image_question(question, image)
                elif question.startswith("/"):
                    self._run_command(question)
                else:
                    self._run_question(question)
                self._sse(
                    "done",
                    {
                        "state": state.snapshot(),
                        "benchmark": (
                            chat.format_benchmark_line(state.latest_benchmark)
                            if state.latest_benchmark is not None
                            else ""
                        ),
                    },
                )
            except ClientDisconnected:
                pass
            except Exception as exc:  # Keep the browser informed on failure.
                try:
                    self._sse("error", {"message": str(exc)})
                    self._sse("done", {"state": state.snapshot()})
                except ClientDisconnected:
                    pass

    def _handle_video_chat(
        self, video_id: str, question: str, reset: bool = False
    ) -> None:
        """Stream an answer from one video's transcript inside the video modal.

        This is the in-modal equivalent of ``/youtube transcript <url>``
        followed by a question, but it runs against a dedicated session so the
        main chat's conversation and scope are left untouched.
        """
        try:
            session = get_video_chat_state(video_id)
        except RuntimeError as exc:
            self._begin_sse()
            try:
                self._sse("error", {"message": str(exc)})
                self._sse("done", {})
            except ClientDisconnected:
                pass
            return

        previous_state = self.state
        self.state = session
        try:
            with session.lock:
                if reset:
                    session.reset_conversation()
                    session.cache.clear()
                    session.latest_answer = None
                    session.latest_sources = []
                    session.latest_benchmark = None
                self._begin_sse()
                try:
                    self._sse("meta", session.snapshot())
                    self._run_question(question)
                    self._sse(
                        "done",
                        {
                            "state": session.snapshot(),
                            "benchmark": (
                                chat.format_benchmark_line(
                                    session.latest_benchmark
                                )
                                if session.latest_benchmark is not None
                                else ""
                            ),
                        },
                    )
                except ClientDisconnected:
                    pass
                except Exception as exc:  # Keep the modal informed.
                    try:
                        self._sse("error", {"message": str(exc)})
                        self._sse("done", {"state": session.snapshot()})
                    except ClientDisconnected:
                        pass
        finally:
            self.state = previous_state

    def _handle_news_chat(
        self, news_id: str, question: str, reset: bool = False
    ) -> None:
        """Stream an answer from one learned news story in its modal."""
        try:
            session = get_news_chat_state(news_id)
        except RuntimeError as exc:
            self._begin_sse()
            try:
                self._sse("error", {"message": str(exc)})
                self._sse("done", {})
            except ClientDisconnected:
                pass
            return

        previous_state = self.state
        self.state = session
        try:
            with session.lock:
                if reset:
                    session.reset_conversation()
                    session.cache.clear()
                    session.latest_answer = None
                    session.latest_sources = []
                    session.latest_benchmark = None
                self._begin_sse()
                try:
                    self._sse("meta", session.snapshot())
                    self._run_question(question)
                    self._sse(
                        "done",
                        {
                            "state": session.snapshot(),
                            "benchmark": (
                                chat.format_benchmark_line(
                                    session.latest_benchmark
                                )
                                if session.latest_benchmark is not None
                                else ""
                            ),
                        },
                    )
                except ClientDisconnected:
                    pass
                except Exception as exc:
                    try:
                        self._sse("error", {"message": str(exc)})
                        self._sse("done", {"state": session.snapshot()})
                    except ClientDisconnected:
                        pass
        finally:
            self.state = previous_state

    def _handle_gutenberg_chat(
        self,
        book_id: str,
        question: str,
        *,
        title: str = "",
        text_url: str = "",
        reset: bool = False,
    ) -> None:
        """Stream an answer from one Project Gutenberg book.

        The book is split into article-style paragraph trunks on first use and
        searched with the same iterative, section-aware RAG loop as /article.
        A dedicated session keeps the main chat's scope and conversation
        untouched while the modal is open.
        """
        try:
            session = get_gutenberg_chat_state(
                book_id, text_url=text_url, title=title
            )
        except (RuntimeError, ValueError) as exc:
            self._begin_sse()
            try:
                self._sse("error", {"message": str(exc)})
                self._sse("done", {})
            except ClientDisconnected:
                pass
            return

        previous_state = self.state
        self.state = session
        try:
            with session.lock:
                if reset:
                    session.reset_conversation()
                    session.cache.clear()
                    session.latest_answer = None
                    session.latest_sources = []
                    session.latest_benchmark = None
                self._begin_sse()
                try:
                    self._sse("meta", session.snapshot())
                    self._run_question(question)
                    self._sse(
                        "done",
                        {
                            "state": session.snapshot(),
                            "benchmark": (
                                chat.format_benchmark_line(
                                    session.latest_benchmark
                                )
                                if session.latest_benchmark is not None
                                else ""
                            ),
                        },
                    )
                except ClientDisconnected:
                    pass
                except Exception as exc:  # Keep the book modal informed.
                    try:
                        self._sse("error", {"message": str(exc)})
                        self._sse("done", {"state": session.snapshot()})
                    except ClientDisconnected:
                        pass
        finally:
            self.state = previous_state

    def _handle_mathtutor_answer(
        self, session_id: str, index: int, student_answer: str = ""
    ) -> None:
        """Stream a worked solution for one uploaded-paper practice problem."""
        self._begin_sse()
        session = None
        problem = None
        try:
            session = _mathtutor_load_session(session_id)
            problems = session.get("problems", [])
            if index < 0 or index >= len(problems):
                raise ValueError("Invalid practice problem index")
            problem = problems[index]
            if not isinstance(problem, dict):
                raise ValueError("Malformed practice problem")

            self._sse(
                "mathtutor_answer_start",
                {
                    "id": str(session.get("id", "")),
                    "index": index,
                    "problem_id": int(problem.get("id", 0) or 0),
                },
            )
            prompt = _mathtutor_answer_prompt(session, problem, student_answer)
            response = chat.stream_answer(
                prompt,
                model=MATHUTOR_MODEL,
                emit=lambda text: self._sse_text("delta", text),
            )
            if response:
                with MATHUTOR_LOCK:
                    # Reload before recording: another answer request may
                    # have updated the same session in the meantime.
                    try:
                        latest = _mathtutor_load_session(session_id)
                    except ValueError:
                        latest = session
                    _mathtutor_append_history(
                        latest, problem, student_answer, response
                    )
            self._sse(
                "mathtutor_answer_done",
                {
                    "id": str(session.get("id", "")),
                    "index": index,
                    "text": response,
                },
            )
            self._sse("done", {})
        except ClientDisconnected:
            pass
        except (ValueError, RuntimeError, OSError) as exc:
            try:
                self._sse("error", {"message": str(exc)})
                self._sse("done", {})
            except ClientDisconnected:
                pass

    def _run_image_question(self, question: str, image: str) -> None:
        """Answer a question about one attached image with the vision model.

        Image questions deliberately bypass transcript retrieval: the answer
        comes from the picture and, for pronoun-heavy follow-ups, the recent
        conversation. The usual ``delta``/``done`` SSE events keep the browser
        rendering it like any other answer.
        """
        state = self.state
        original_question = (question or "Describe this image in detail.").strip()
        if len(original_question) > MAX_IMAGE_QUESTION_CHARS:
            raise ValueError(
                "The question is too long "
                f"(limit {MAX_IMAGE_QUESTION_CHARS:,} characters)."
            )
        resolved_question = original_question
        if state.conversation:
            resolution = state.resolve_question(original_question)
            if resolution.is_unrelated:
                self._sse_text(
                    "log",
                    "[conversation: the question is unrelated to the previous "
                    "turns; starting a fresh context]",
                )
            elif resolution.question != original_question:
                self._sse_text(
                    "log",
                    "[conversation: interpreting the follow-up as: "
                    f"{resolution.question}]",
                )
            resolved_question = resolution.question

        self._sse(
            "mode",
            {"model": IMAGE_MODEL, "label": "image question"},
        )
        self._sse_text("log", "[image: examining the attached image]")
        prompt = IMAGE_QUESTION_PROMPT.format(
            question=resolved_question,
            conversation=image_conversation_context(
                state.conversation, state.conversation_summary
            ),
        )

        models = image_model_candidates()
        answer = ""
        for index, model in enumerate(models):
            if index:
                self._sse(
                    "mode",
                    {"model": model, "label": "image question (vision)"},
                )
            try:
                answer = self._stream_image_answer(model, prompt, image)
                break
            except (ImageModelUnsupported, ImageModelUnavailable) as exc:
                if index == len(models) - 1:
                    if isinstance(exc, ImageModelUnsupported):
                        raise RuntimeError(
                            "No installed Ollama model can read images. Pull a "
                            "vision model or set OLLAMA_IMAGE_MODEL to one that is "
                            "installed."
                        ) from exc
                    raise RuntimeError(
                        "No local image model is available. Install a vision "
                        "model or set OLLAMA_IMAGE_MODEL."
                    ) from exc
                reason = (
                    "cannot read images"
                    if isinstance(exc, ImageModelUnsupported)
                    else "is not installed"
                )
                self._sse_text(
                    "log",
                    f"[image: the selected model {reason}; retrying with the "
                    "next local vision model]",
                )

        state.latest_answer = answer or None
        state.latest_sources = []
        state.record_turn(original_question, resolved_question, answer)

    def _stream_image_answer(
        self, model: str, prompt: str, image: str
    ) -> str:
        """Stream one vision model's answer to an image question."""
        payload = {
            "model": model,
            "prompt": prompt,
            "images": [image],
            "stream": True,
            "think": False,
            "keep_alive": chat.OLLAMA_KEEP_ALIVE,
            "options": {
                "temperature": 0.2,
                "num_ctx": IMAGE_NUM_CTX,
            },
        }
        answer_parts: list[str] = []
        try:
            with requests.post(
                f"{chat.OLLAMA_HOST}/api/generate",
                json=payload,
                stream=True,
                timeout=(10, 900),
            ) as response:
                if response.status_code >= 400:
                    detail = (response.text or "")[:800]
                    if is_multimodal_unsupported(detail):
                        raise ImageModelUnsupported(detail)
                    if response.status_code == 404 or is_model_unavailable(detail):
                        raise ImageModelUnavailable(detail)
                    response.raise_for_status()
                for line in response.iter_lines(decode_unicode=True):
                    if not line:
                        continue
                    try:
                        event = json.loads(line)
                    except json.JSONDecodeError:
                        continue
                    error = event.get("error")
                    if error:
                        if not answer_parts and is_multimodal_unsupported(error):
                            raise ImageModelUnsupported(str(error))
                        if not answer_parts and is_model_unavailable(error):
                            raise ImageModelUnavailable(str(error))
                        raise RuntimeError(f"Ollama error: {error}")
                    token = event.get("response", "")
                    if token:
                        answer_parts.append(token)
                        self._sse_text("delta", token)
                    if event.get("done"):
                        break
        except requests.HTTPError as exc:
            detail = ""
            if exc.response is not None:
                detail = exc.response.text or ""
            if is_multimodal_unsupported(detail):
                raise ImageModelUnsupported(detail) from exc
            if exc.response is not None and exc.response.status_code == 404:
                raise ImageModelUnavailable(detail) from exc
            raise RuntimeError(
                f"Image model request failed: {detail or exc}"
            ) from exc
        except requests.RequestException as exc:
            raise RuntimeError(
                f"Could not reach Ollama at {chat.OLLAMA_HOST}. "
                f"Is it running? ({exc})"
            ) from exc
        return "".join(answer_parts).strip()

    def _run_question(self, question: str) -> None:
        state = self.state
        if state.moonbit_mode:
            # /moonbit keeps its own docs-only thread. It takes precedence
            # over the other modes until /moonbit off is entered.
            self._run_moonbit_question(question)
            return
        if state.united_healthcare_mode:
            self._run_united_healthcare_question(question)
            return
        if state.anthem_mode:
            # /anthem keeps its own provider-directory thread. Every ordinary
            # question stays in that thread, with the recent provider results
            # available for follow-up references, until /anthem off.
            self._run_anthem_question(question)
            return
        if state.research_with_news_mode:
            if _anthem_online_tool_matches(question):
                self._sse_text(
                    "log",
                    "[online: routing the provider question to the Anthem "
                    "directory tool]",
                )
                self._run_anthem_question(question)
                return
            # In this mode every question continues the current research
            # thread. The first researched question uses the direct initial
            # search (no related-question planning); later questions keep the
            # earlier briefings so their search queries can resolve follow-up
            # references.
            initial = not state.conversation and not state.conversation_summary
            self._run_research_with_the_news(question, initial=initial)
            return
        article_mode = state.article_path is not None
        # Iterative evidence is reused within this question.
        iterative_cache: dict = {}
        answer_language: str | None = None
        retrieval_question = question
        if chat.contains_simplified_chinese(question):
            answer_language = "Simplified Chinese"
            self._sse_text(
                "log",
                "[Simplified Chinese detected; translating the question "
                "into English for BM25 retrieval and answering directly "
                "in Simplified Chinese]",
            )
            try:
                retrieval_question = chat.translate_to_english(question)
            except RuntimeError:
                # chat.answer() retries the translation and reports its own
                # warning if this pre-translation failed.
                retrieval_question = question

        context_sensitive = False
        if state.conversation:
            resolution = state.resolve_question(retrieval_question)
            if resolution.is_unrelated:
                self._sse_text(
                    "log",
                    "[conversation: the question is unrelated to the "
                    "previous turns; starting a fresh context]",
                )
            if resolution.question != retrieval_question:
                self._sse_text(
                    "log",
                    "[conversation: interpreting the follow-up as: "
                    f"{resolution.question}]",
                )
                retrieval_question = resolution.question
            context_sensitive = resolution.is_follow_up

        article_route: ArticleRoute | None = None
        article_prepare_selected: Callable | None = None
        article_prepare_stats: dict = {}
        article_attempt_depth: int | None = state.depth
        article_attempt_budget = state.max_context_tokens
        article_use_all_chunks = False
        article_prompt_prefix: str | None = None
        article_cache_variant = ""
        article_overview_mode = False
        if article_mode:
            article_route = article_question_route(
                retrieval_question,
                state.article_structure,
                iterative_default=state.article_iterative,
            )
            route_label = article_route.label
            if article_route.section is not None:
                route_label += f" {article_route.section}"
            if article_route.iterative:
                route_label += " + iterative"
            self._sse_text(
                "log",
                f"[article router: {route_label} ({article_route.source})]",
            )

        paragraph_chunks = (
            _iterative_paragraph_chunks(state.chunks)
            if article_mode
            else state.chunks
        )
        answer_chunks = (
            paragraph_chunks if article_mode else state.chunks
        )
        if article_route is not None:
            if article_route.label == "section" and article_route.section is not None:
                section = (state.article_structure or {}).get(
                    "_section_lookup", {}
                ).get(article_route.section)
                if section is None:
                    article_route = ArticleRoute(
                        "narrow", None, False, "section-missing"
                    )
                else:
                    answer_chunks = [
                        chunk
                        for chunk in paragraph_chunks
                        if int(section["start"]) <= chunk.index <= int(section["end"])
                    ]
                    if not answer_chunks:
                        answer_chunks = paragraph_chunks
                    article_attempt_depth = ARTICLE_SECTION_DEPTH
                    article_attempt_budget = ARTICLE_SECTION_CONTEXT_TOKENS
                    article_cache_variant = (
                        f"article-section:{article_route.section}"
                    )
                    article_prompt_prefix = (
                        "### ARTICLE SECTION\n"
                        f"The context is focused on the article section "
                        f"'{section.get('heading', '')}'. It includes the "
                        "section summary and neighboring paragraphs. Prefer "
                        "the specific paragraphs for facts and use the "
                        "section summary only for orientation. Ground every "
                        "claim in this context; if a detail is absent, omit "
                        "it rather than refusing.\n\n"
                    )
                    article_prepare_selected, article_prepare_stats = (
                        _make_article_expansion(
                            state.article_structure,
                            state.chunks,
                            ARTICLE_SECTION_CONTEXT_TOKENS,
                            lambda text: self._sse_text("log", text),
                        )
                    )
            elif article_route.iterative:
                evidence = _article_iterative_evidence(
                    retrieval_question,
                    state.chunks,
                    state.article_structure,
                    ARTICLE_ITERATIVE_EVIDENCE_TOKENS,
                    lambda text: self._sse_text("log", text),
                )
                if evidence:
                    answer_chunks = [chunk for chunk, _score in evidence]
                    article_use_all_chunks = True
                    article_attempt_depth = state.depth
                    article_attempt_budget = ARTICLE_ITERATIVE_EVIDENCE_TOKENS
                    article_cache_variant = "article-iterative"
                    article_prompt_prefix = (
                        "### ARTICLE EVIDENCE\n"
                        "The context below was gathered in several retrieval "
                        "rounds across the whole article, in document order; "
                        "it includes section summaries. Answer the complete "
                        "question, including enumerations and comparisons, "
                        "using every relevant part of this evidence. Do not "
                        "say that the context is insufficient; if one detail "
                        "is genuinely absent, answer the supported parts and "
                        "omit the unsupported detail.\n\n"
                    )
                else:
                    article_route = ArticleRoute(
                        "narrow", None, False, "iterative-empty"
                    )
                    self._sse_text(
                        "log",
                        "[context gathering: no new evidence found; using the "
                        "normal narrow retrieval path]",
                    )
            if article_route.label == "narrow" and not article_route.iterative:
                article_attempt_depth = state.depth
                article_attempt_budget = ARTICLE_NARROW_CONTEXT_TOKENS
                article_cache_variant = "article-narrow"
                article_prompt_prefix = (
                    "### ARTICLE CONTEXT\n"
                    "The context was retrieved from one article and includes "
                    "parent-section summaries and neighboring paragraphs. "
                    "Use the specific paragraphs for facts and the section "
                    "summary only for orientation. Ground every claim in the "
                    "context; if a specific detail is absent, omit it rather "
                    "than refusing.\n\n"
                )
                article_prepare_selected, article_prepare_stats = (
                    _make_article_expansion(
                        state.article_structure,
                        state.chunks,
                        ARTICLE_NARROW_CONTEXT_TOKENS,
                        lambda text: self._sse_text("log", text),
                    )
                )

        article_overview_mode = bool(
            article_route is not None and article_route.label == "overview"
        )
        article_overview_prefix: str | None = None
        article_overview_context_ready = False
        if article_overview_mode:
            digest_chunks = [
                chunk
                for chunk in state.chunks
                if chunk.index == ARTICLE_DIGEST_INDEX
            ]
            overview_chunks = [
                chunk
                for chunk in state.chunks
                if chunk.index == ARTICLE_OVERVIEW_INDEX
            ]
            requested_words = (
                article_requested_word_count(question)
                or article_requested_word_count(retrieval_question)
            )
            detailed = bool(
                ARTICLE_DETAILED_OVERVIEW_RE.search(question)
                or ARTICLE_DETAILED_OVERVIEW_RE.search(retrieval_question)
            )
            if digest_chunks and (requested_words or detailed):
                # The digest contains the title, outline, TL;DR, and
                # representative content from each major section. Use it as
                # the sole candidate so BM25 cannot replace it with unrelated
                # paragraphs that merely share generic words such as
                # "what"/"this"/"about".
                answer_chunks = digest_chunks[:1]
                context_kind = "article digest"
                context_detail = (
                    "its title, outline, TL;DR/summary, and representative "
                    "content from each major section"
                )
            elif overview_chunks:
                # Concise overview questions only need the compact overview;
                # the digest is reserved for long-form/detailed requests.
                answer_chunks = overview_chunks[:1]
                context_kind = "document overview"
                context_detail = "its title, outline, and TL;DR/summary"
            else:
                context_kind = ""
                context_detail = ""

            if context_kind:
                article_overview_context_ready = True
                if requested_words:
                    length_instruction = (
                        "Target length: approximately "
                        f"{requested_words:,} words. Write a detailed, "
                        "well-structured answer with headings where they "
                        "help. Develop the points supported by the context "
                        "instead of padding with unsupported facts. "
                    )
                elif detailed:
                    length_instruction = (
                        "Write a detailed, well-structured answer with "
                        "headings where they help. "
                    )
                else:
                    length_instruction = (
                        "Answer the user's question with a concise, "
                        "well-organized summary. "
                    )
                article_overview_prefix = (
                    "### ARTICLE OVERVIEW\n"
                    f"The context below is the {context_kind} of the entire "
                    f"article: {context_detail}. "
                    + length_instruction
                    + "Ground every claim in this context. If a specific "
                    "detail is absent, omit it rather than refusing; never "
                    "say that the context is insufficient.\n\n"
                )
                self._sse_text(
                    "log",
                    f"[article: answering from the {context_kind} "
                    "(title, outline, summary, and representative "
                    "sections)]",
                )

        detected_language = detect_code_language(retrieval_question)
        if (
            not detected_language
            and programming_feature_question(retrieval_question)
        ):
            # A follow-up such as "now add error handling" or "how does
            # ownership work?" may not repeat the language. Keep the previous
            # one only when the new question is about a language or feature.
            detected_language = state.code_language
        if detected_language:
            state.code_language = detected_language
            self._sse("meta", state.snapshot())
        code_language = detected_language

        golang_package_docs = (
            not article_mode
            and code_language == GO_LANGUAGE
            and golang_package_docs_request(retrieval_question)
        )
        rust_crate_docs = (
            not article_mode
            and code_language == RUST_LANGUAGE
            and rust_crate_docs_request(retrieval_question)
        )
        rust_code_docs = (
            not article_mode
            and code_language == RUST_LANGUAGE
            and rust_code_docs_request(retrieval_question)
        )
        php_symbol_docs = (
            not article_mode
            and code_language == PHP_LANGUAGE
            and php_symbol_docs_request(retrieval_question)
        )
        documentation_code = (
            not article_mode
            and documentation_code_request(code_language, retrieval_question)
        )
        coding_mode = (
            question_requests_coding(retrieval_question)
            or golang_package_docs
            or rust_crate_docs
            or rust_code_docs
            or php_symbol_docs
            or documentation_code
        )
        if (
            coding_mode
            and programming_explanation_question(retrieval_question)
            and not explicit_code_request(retrieval_question)
        ):
            # "What is ownership in Rust?" is an explanation, not a request
            # for a program. Keep the answer in the normal prompt path; no
            # official documentation is loaded unless code is requested.
            coding_mode = False
            documentation_code = False
            golang_package_docs = False
            rust_crate_docs = False
            rust_code_docs = False
            php_symbol_docs = False
            self._sse_text(
                "log",
                "[programming question: explanation detected; no language "
                "documentation is loaded because no code was requested]",
            )
        # Go, Rust, and PHP answer with the dedicated programming model for
        # both chat and code generation.
        coding_model = language_coding_model(code_language)
        chat_model = language_chat_model(code_language)
        if golang_package_docs:
            self._sse_text(
                "log",
                "[go: code-sample question references or implies a Go "
                "package; pkg.go.dev package documentation will be searched]",
            )
        elif documentation_code and code_language == GO_LANGUAGE:
            self._sse_text(
                "log",
                "[go: code-sample question detected; searching the "
                "official Go documentation and answering in Go]",
            )
        if rust_crate_docs:
            self._sse_text(
                "log",
                "[rust: code-sample question references a Rust crate; "
                "docs.rs crate documentation will be searched]",
            )
        elif rust_code_docs:
            self._sse_text(
                "log",
                "[rust: code-sample question detected; searching the official "
                "Rust documentation instead of GitHub]",
            )
        if php_symbol_docs:
            self._sse_text(
                "log",
                "[php: code-sample question references a PHP function, "
                "class, or extension; the dedicated manual page will be "
                "searched]",
            )
        elif documentation_code and code_language == PHP_LANGUAGE:
            self._sse_text(
                "log",
                "[php: code-sample question detected; searching the "
                "official PHP manual and answering in PHP]",
            )
        if coding_mode:
            mode_label = (
                f"{code_language} coding mode"
                if code_language
                else "coding mode"
            )
            self._sse(
                "mode",
                {
                    "mode": "coding",
                    "model": coding_model,
                    "think": False,
                    "language": code_language or "",
                    "label": mode_label,
                },
            )
            language_note = (
                f"; all generated code will be {code_language}"
                if code_language
                else ""
            )
            self._sse_text(
                "log",
                "[coding mode: code/architecture question detected; using "
                f"{coding_model} without thinking{language_note}]",
            )

        def attempt(
            chunks: list[chat.Chunk],
            scope_label: str,
            report_errors: bool = True,
            prompt_prefix_override: str | None = None,
            cache_variant: str = "",
            use_all_chunks: bool = False,
            depth_override: int | None = None,
            max_context_tokens_override: int | None = None,
            prepare_selected: Callable | None = None,
            prepare_stats: dict | None = None,
        ):
            """Answer once from the supplied corpus scope."""
            attempt_stats: dict = {}
            # A documentation-backed language must use that language for any
            # code in the answer, not only for answers classified as coding
            # questions. A normal explanation may still include a snippet.
            prompt_prefix = (
                prompt_prefix_override
                if prompt_prefix_override is not None
                else (
                    coding_prompt_prefix(code_language)
                    if coding_mode
                    else language_coding_directive(code_language)
                )
            )
            memory_prefix = conversation_answer_prefix(
                state.conversation_summary
            )
            if memory_prefix:
                prompt_prefix = memory_prefix + prompt_prefix
            if (
                chat.question_is_time_sensitive(retrieval_question)
                and not article_mode
                and not coding_mode
            ):
                prompt_prefix = (
                    TEMPORAL_QUESTION_PROMPT_PREFIX.format(
                        today=time.strftime("%Y-%m-%d")
                    )
                    + prompt_prefix
                )
                self._sse_text(
                    "log",
                    "[time-sensitive question: dated evidence is ordered "
                    "newest first, and newer reports override older ones]",
                )
            # /article gathers evidence in small rounds and stops as soon as
            # the small model says the gathered paragraphs are sufficient.
            # Apply the same loop to normal transcript questions. Article mode
            # already did this above; coding questions keep their
            # documentation-first path. A cached iterative answer is reused
            # without repeating the retrieval rounds.
            iterative_eligible = (
                not article_mode and not use_all_chunks and not coding_mode
            )
            if iterative_eligible:
                iterative_variant = (
                    f"{cache_variant}+youtube-iterative"
                    if cache_variant
                    else "youtube-iterative"
                )
                iterative_cached = state.cache.get(
                    state.cache_key(
                        retrieval_question,
                        answer_language,
                        conversation_revision=(
                            state.conversation_revision
                            if context_sensitive
                            else None
                        ),
                        language=code_language,
                        coding_mode=coding_mode,
                        prompt_variant=iterative_variant,
                        memory_revision=(
                            state.conversation_summary_turns
                            if state.conversation_summary
                            else 0
                        ),
                    )
                ) is not None
                if iterative_cached:
                    cache_variant = iterative_variant
                else:
                    evidence_budget = (
                        max_context_tokens_override
                        if max_context_tokens_override is not None
                        else state.max_context_tokens
                    )
                    evidence = _youtube_iterative_evidence(
                        retrieval_question,
                        chunks,
                        evidence_budget,
                        lambda text: self._sse_text("log", text),
                        depth=(
                            depth_override
                            if depth_override is not None
                            else state.depth
                        ),
                        cache=iterative_cache,
                    )
                    if evidence:
                        chunks = [chunk for chunk, _score in evidence]
                        use_all_chunks = True
                        cache_variant = iterative_variant
                        prompt_prefix = (
                            YOUTUBE_ITERATIVE_PROMPT_PREFIX + prompt_prefix
                        )
                    elif state.news_id is not None or state.transcript is not None:
                        # The scoped corpus is small and there is no BM25
                        # match. Answer from its reading-order text rather
                        # than ending the stream with no message at all.
                        scoped_chunks = _scoped_context_fallback(
                            chunks, evidence_budget
                        )
                        if scoped_chunks:
                            chunks = scoped_chunks
                            use_all_chunks = True
                            cache_variant = (
                                f"{cache_variant}+scoped-context-fallback"
                                if cache_variant
                                else "scoped-context-fallback"
                            )
                            prompt_prefix = (
                                SCOPED_CONTEXT_FALLBACK_PROMPT_PREFIX
                                + prompt_prefix
                            )
                            self._sse_text(
                                "log",
                                "[no matching paragraph in this scoped "
                                "source; using its reading-order text as "
                                "context]",
                            )
            emit_delta = lambda text: self._sse("delta", {"text": text})
            docs_backed = language_uses_official_docs(code_language)
            answer_filter = InsufficientContextFilter(enabled=docs_backed)
            fence_filter = CodeFenceRepairFilter(enabled=docs_backed)

            def emit_answer_delta(text: str) -> None:
                cleaned = answer_filter.feed(text)
                if not cleaned:
                    return
                cleaned = fence_filter.feed(cleaned)
                if cleaned:
                    emit_delta(cleaned)

            try:
                response, sources = chat.answer(
                    retrieval_question,
                    chunks,
                    depth=(
                        depth_override
                        if depth_override is not None
                        else state.depth
                    ),
                    max_context_tokens=(
                        max_context_tokens_override
                        if max_context_tokens_override is not None
                        else state.max_context_tokens
                    ),
                    answer_language=answer_language,
                    cache=state.cache,
                    cache_key=state.cache_key(
                        retrieval_question,
                        answer_language,
                        conversation_revision=(
                            state.conversation_revision
                            if context_sensitive
                            else None
                        ),
                        language=code_language,
                        coding_mode=coding_mode,
                        prompt_variant=cache_variant,
                        memory_revision=(
                            state.conversation_summary_turns
                            if state.conversation_summary
                            else 0
                        ),
                    ),
                    stats=attempt_stats,
                    progressive=state.progressive,
                    prompt_prefix=prompt_prefix,
                    model=(
                        language_coding_model(code_language)
                        if coding_mode
                        else language_chat_model(code_language)
                    ),
                    think=False,
                    use_all_chunks=use_all_chunks,
                    prepare_selected=prepare_selected,
                    emit=emit_answer_delta,
                    log=lambda text: self._sse_text("log", text),
                )
            except RuntimeError as exc:
                if report_errors:
                    self._sse("error", {"message": str(exc)})
                return None, [], attempt_stats, str(exc)
            except Exception as exc:  # Keep unexpected failures visible.
                message = f"{type(exc).__name__}: {exc}"
                if report_errors:
                    self._sse("error", {"message": message})
                return None, [], attempt_stats, message
            tail = answer_filter.flush()
            if tail:
                tail = fence_filter.feed(tail)
                if tail:
                    emit_delta(tail)
            fence_tail = fence_filter.flush()
            if fence_tail:
                emit_delta(fence_tail)
            if docs_backed and response is not None:
                # Keep the returned text in sync with what was streamed: the
                # refusal is already suppressed and malformed continuation
                # fences are already repaired, so neither can reappear in the
                # saved answer, annotations, or a documentation merge.
                response = repair_code_fences(
                    strip_leading_insufficient_context(response)
                )
            if prepare_stats is not None and prepare_stats.get("added"):
                attempt_stats["article_expansion"] = dict(prepare_stats)
            return response, sources or [], attempt_stats, None

        scope_label = (
            f"article {state.article_path!r}"
            if article_mode
            else (
                f"transcript {state.transcript!r}"
                if state.transcript is not None
                else "global corpus"
            )
        )
        documentation_language = (
            None
            if article_mode or not coding_mode
            else (
                code_language
                if language_uses_official_docs(code_language)
                else None
            )
        )
        local_model = coding_model if coding_mode else chat_model
        documentation_first_done = False
        first_response: str | None = None
        first_sources: list[tuple[chat.Chunk, float]] = []
        first_stats: dict = {}
        response: str | None = None
        sources: list[tuple[chat.Chunk, float]] = []
        stats: dict = {}

        # A Go, Rust, or PHP code question must consult the official
        # documentation before the general local model generates any code.
        # The docs answer is streamed first; only then is the local coding
        # model called, with that answer inserted in its prompt as the
        # authoritative reference. This prevents a code sample being invented
        # before the corresponding documentation has been retrieved.
        if documentation_language and coding_mode:
            documentation_first_done = True
            descriptor = documentation_descriptor(documentation_language)
            assert descriptor is not None
            self._sse_answer_source(
                language=documentation_language,
                source=SOURCE_DOCUMENTATION,
                label=(
                    f"official docs {descriptor['source_label']} via "
                    f"{coding_model}"
                ),
                model=coding_model,
                url=descriptor["source_url"],
                detail=(
                    "the official documentation is retrieved before any "
                    "local code generation"
                ),
            )
            self._sse_text(
                "log",
                f"[docs-first: consulting {descriptor['source_label']} before "
                f"{coding_model} generates code]",
            )
            doc_answer, doc_error = descriptor["runner"](
                retrieval_question,
                log=lambda text: self._sse_text("log", text),
                emit=lambda text: self._sse_text("delta", text),
            )

            doc_incomplete = bool(doc_answer) and (
                coding_mode
                and documentation_answer_is_insufficient(doc_answer)
            )
            if doc_answer and doc_incomplete:
                # The excerpts were too thin for the documentation assistant,
                # which streamed a refusal. A code request must not end there:
                # clear the refusal and let the local coding model write the
                # program from the limited excerpts plus its own knowledge.
                self._sse_text(
                    "log",
                    f"[docs-first: the {descriptor['source_label']} excerpts "
                    "were incomplete; asking the local coding model to "
                    "generate the code from the limited excerpts instead of "
                    "refusing]",
                )
                self._sse_answer_source(
                    language=documentation_language,
                    source=SOURCE_OLLAMA,
                    label=f"local LLM {coding_model} (code rescue)",
                    model=coding_model,
                    detail=(
                        "the official documentation excerpts were incomplete; "
                        "the local model is generating the requested code "
                        "from those excerpts and its own programming knowledge"
                    ),
                )
                self._sse(
                    "reset",
                    {"reason": "documentation answer was incomplete"},
                )
                rescue_response, rescue_metrics = stream_code_rescue(
                    retrieval_question,
                    documentation_language,
                    doc_answer,
                    descriptor,
                    emit=lambda text: self._sse("delta", {"text": text}),
                )
                if rescue_response and rescue_response.strip():
                    response = rescue_response + "\n"
                    sources = []
                    stats = dict(rescue_metrics)
                    stats["documentation_first"] = {
                        "used": True,
                        "source": descriptor["source_url"],
                        "doc_chars": len(doc_answer),
                        "doc_insufficient": True,
                        "local_code_rescue": True,
                        "error": doc_error,
                    }
                    first_response = response
                    first_sources = []
                    first_stats = stats
                else:
                    # Extremely unlikely: the rescue model returned nothing.
                    # Restore the documentation answer rather than leaving the
                    # screen empty.
                    self._sse_text(
                        "log",
                        "[docs-first: the local code rescue returned no text; "
                        "keeping the documentation answer]",
                    )
                    response = doc_answer + "\n"
                    self._sse_text("delta", response)
                    sources = []
                    stats = {
                        "documentation_first": {
                            "used": True,
                            "source": descriptor["source_url"],
                            "doc_chars": len(doc_answer),
                            "doc_insufficient": True,
                            "local_code_rescue": False,
                            "error": doc_error,
                        }
                    }
                    first_response = response
                    first_sources = []
                    first_stats = stats
            elif doc_answer:
                # The official documentation answer is streamed as the final
                # answer. The local model is not asked for a second
                # "Complementary notes from the local transcript context"
                # section: the documentation already contains the code and
                # explanation, and the extra section only duplicates it.
                stats = {
                    "documentation_first": {
                        "used": True,
                        "source": descriptor["source_url"],
                        "doc_chars": len(doc_answer),
                        "streamed_before_local": True,
                        "complementary_notes_removed": True,
                        "error": doc_error,
                    }
                }
                response = doc_answer + "\n"
                sources = []
                self._sse_answer_source(
                    language=documentation_language,
                    source=SOURCE_DOCUMENTATION,
                    label=(
                        f"official docs {descriptor['source_label']} via "
                        f"{coding_model}"
                    ),
                    model=coding_model,
                    url=descriptor["source_url"],
                    detail=(
                        "the answer comes from the official documentation; "
                        "no additional local complement is appended"
                    ),
                )
                first_response = response
                first_sources = []
                first_stats = stats
            else:
                self._sse_text(
                    "log",
                    f"[docs-first: documentation unavailable: {doc_error}; "
                    "falling back to the local coding model]",
                )
                self._sse_answer_source(
                    language=documentation_language,
                    source=SOURCE_OLLAMA,
                    label=f"local LLM {local_model}",
                    model=local_model,
                    detail=(
                        "the official documentation was unavailable; the "
                        "local model is answering as a fallback"
                    ),
                )
                (
                    first_response,
                    first_sources,
                    first_stats,
                    error,
                ) = attempt(
                    answer_chunks,
                    scope_label,
                    prompt_prefix_override=(
                        article_overview_prefix
                        if article_overview_prefix is not None
                        else article_prompt_prefix
                    ),
                    cache_variant=article_cache_variant,
                    use_all_chunks=(
                        article_overview_context_ready
                        or article_use_all_chunks
                    ),
                    depth_override=article_attempt_depth,
                    max_context_tokens_override=article_attempt_budget,
                    prepare_selected=article_prepare_selected,
                    prepare_stats=article_prepare_stats,
                )
                if error:
                    return
                response, sources, stats = (
                    first_response,
                    first_sources,
                    first_stats,
                )

        if not documentation_first_done:
            if documentation_language:
                self._sse_answer_source(
                    language=documentation_language,
                    source=SOURCE_OLLAMA,
                    label=f"local LLM {local_model}",
                    model=local_model,
                    detail=(
                        "the answer is being generated by the local Ollama "
                        "model from the transcript knowledge base"
                    ),
                )
            first_response, first_sources, first_stats, error = attempt(
                answer_chunks,
                scope_label,
                prompt_prefix_override=(
                    article_overview_prefix
                    if article_overview_prefix is not None
                    else article_prompt_prefix
                ),
                cache_variant=article_cache_variant,
                use_all_chunks=(
                    article_overview_context_ready or article_use_all_chunks
                ),
                depth_override=article_attempt_depth,
                max_context_tokens_override=article_attempt_budget,
                prepare_selected=article_prepare_selected,
                prepare_stats=article_prepare_stats,
            )
            if error:
                return
            response, sources, stats = (
                first_response,
                first_sources,
                first_stats,
            )

        # For non-coding questions in a documentation-backed language, the
        # local knowledge base still answers first and the official docs are
        # consulted when it cannot. Coding questions already took the
        # documentation-first path above, so they are never retried here.
        local_answer_insufficient = answer_is_insufficient(first_response)
        documentation = None
        if (
            not documentation_first_done
            and documentation_language
            and (coding_mode or local_answer_insufficient)
        ):
            documentation = documentation_descriptor(documentation_language)

        if documentation is not None:
            if not coding_mode:
                self._sse_text(
                    "log",
                    "the local knowledge base could not answer; consulting "
                    f"{documentation['source_label']} with {coding_model}",
                )
            response, sources, stats = attach_documentation_answer(
                first_response,
                first_sources,
                first_stats,
                question=retrieval_question,
                emit_delta=lambda text: self._sse_text("delta", text),
                emit_reset=lambda reason: self._sse(
                    "reset", {"reason": reason}
                ),
                emit_log=lambda text: self._sse_text("log", text),
                emit_source=self._sse_doc_source,
                **documentation,
            )
        # For every other language, an unusable coding answer falls back to
        # GitHub's public API and an answer built from code snippets. Go,
        # Rust, and PHP never take this path: their code samples come from
        # the official documentation above.
        elif (
            coding_mode
            and not article_mode
            and github_code_fallback_allowed(code_language)
            and answer_is_insufficient(first_response)
        ):
            self._sse_text(
                "log",
                "[coding mode: no usable answer; searching GitHub for "
                "related code snippets]",
            )
            snippets = github_code_snippets(
                retrieval_question,
                limit=GITHUB_SNIPPET_LIMIT,
                log=lambda text: self._sse_text("log", text),
                language_guard=code_language,
            )
            if snippets:
                if first_response and first_response.strip():
                    self._sse("reset", {"reason": "github code fallback"})
                response = format_github_code_answer(snippets)
                self._sse_text("delta", response)
                sources = []
                stats = dict(first_stats)
                stats["github_fallback"] = {
                    "snippets": len(snippets),
                    "sources": [
                        {
                            "repo": snippet.get("repo"),
                            "path": snippet.get("path"),
                            "url": snippet.get("url"),
                        }
                        for snippet in snippets
                    ],
                }
                self._sse_text(
                    "log",
                    f"[github fallback: using {len(snippets)} code "
                    "snippet(s)]",
                )
            else:
                self._sse_text(
                    "log",
                    "[github fallback: no related code snippets found]",
                )

        if response is None:
            # An isolated modal must never finish with an empty answer. The
            # scoped fallback above normally prevents this, but a model or
            # upstream failure can still produce no text; make that visible.
            if state.news_id is not None:
                message = (
                    "I could not produce an answer from this learned story. "
                    "Try rephrasing the question or learning the article again."
                )
                self._sse_text("delta", message)
                response = message
                sources = []
                stats = dict(stats or {})
            elif state.transcript is not None:
                message = (
                    "I could not produce an answer from this transcript. "
                    "Try rephrasing the question or learning the video again."
                )
                self._sse_text("delta", message)
                response = message
                sources = []
                stats = dict(stats or {})
            else:
                return
        if documentation_language and response is not None:
            # Final safety net: whatever path produced the answer (local,
            # documentation merge, or code rescue), never persist or annotate
            # a leading "provided context is insufficient" refusal.
            response = strip_leading_insufficient_context(response)
        state.latest_answer = response
        state.latest_sources = sources
        stats["coding_mode"] = coding_mode
        stats["model"] = coding_model if coding_mode else chat_model
        state.latest_benchmark = stats
        annotations = build_answer_annotations(response, sources)
        if article_mode:
            # Article paragraphs have no YouTube source. Keep the paragraph
            # mapping for the UI but suppress the video icon/link and add the
            # nearest cached section heading as a precise citation.
            for item in annotations:
                item["video_id"] = ""
                item["url"] = ""
                title = state.article_title or "article"
                section = _article_source_section(
                    state.article_structure, int(item.get("chunk_index", -1))
                )
                heading = str(section.get("heading", "")) if section else ""
                item["title"] = (
                    f"{title} - {heading}" if heading else title
                )
        self._sse("annotations", {"items": annotations})
        state.record_turn(question, retrieval_question, response)
        if state.benchmark_mode:
            self._sse_text("log", chat.format_benchmark_line(stats))

    def _run_command(self, text: str) -> None:
        name, _, argument = text.partition(" ")
        name = name.lower()
        argument = argument.strip()

        if name in {"/exit", "/quit", "/q"}:
            self._sse_text("output", "\nBye. Close the browser tab to exit.\n")
        elif name == "/help":
            self._command_help()
        elif name == "/memory":
            report = chat.index_memory_report(
                self.state.chunks, article=self.state.article_path
            )
            self._sse_text("output", "\n" + report + "\n")
        elif name == "/refresh":
            self._command_refresh()
        elif name == "/youtube":
            self._command_youtube(argument)
        elif name == "/ask":
            self._command_ask(argument)
        elif name == "/news":
            self._command_news(argument)
        elif name == "/reddit":
            self._command_reddit(argument)
        elif name == "/mdn":
            self._command_mdn(argument)
        elif name in {"/britanica", "/britannica"}:
            self._command_britanica(argument)
        elif name == "/stackoverflow":
            self._command_stackoverflow(argument)
        elif name == "/googlenews":
            self._command_googlenews(argument)
        elif name == "/gkg":
            self._command_gkg(argument)
        elif name == "/howstuffworks":
            self._command_howstuffworks(argument)
        elif name == "/tablelog":
            self._command_tablelog(argument)
        elif name == "/stock":
            self._command_stock(argument)
        elif name == "/robinhood":
            self._command_robinhood(argument)
        elif name == "/zillow":
            self._command_zillow(argument)
        elif name == "/moonbit":
            self._command_moonbit(argument)
        elif name == "/wikivoyage":
            self._command_wikivoyage(argument)
        elif name == "/map":
            self._command_map(argument)
        elif name == "/game":
            self._command_game(argument)
        elif name == "/medium":
            self._command_medium(argument)
        elif name == "/ai_news_podcast":
            self._command_ai_news_podcast(argument)
        elif name == "/gutenberg":
            self._command_gutenberg(argument)
        elif name == "/openlibrary":
            self._command_openlibrary(argument)
        elif name in {"/internet_archive", "/internet_artchive"}:
            self._command_internet_archive(argument)
        elif name == "/changelog":
            self._command_changelog(argument)
        elif name == "/wiki":
            self._command_wiki(argument)
        elif name == "/wiki_article":
            self._command_wiki_article(argument)
        elif name == "/article":
            self._command_article(argument)
        elif name == "/mathtutor":
            self._command_mathtutor(argument)
        elif name == "/dict":
            self._command_dict(argument)
        elif name == "/dictionary_history":
            self._command_dictionary_history()
        elif name == "/settings":
            self._command_settings(argument)
        elif name in {"/settins", "/setting"}:
            # Tolerate the original misspelling used when this command was
            # requested; /settings is the documented spelling.
            self._command_settings(argument)
        elif name == "/depth":
            self._command_depth(argument)
        elif name == "/compact":
            state = self.state
            if state.compact_conversation(force=True):
                summary_tokens = (
                    chat.estimate_tokens(state.conversation_summary)
                    if state.conversation_summary
                    else 0
                )
                self._sse_text(
                    "output",
                    "\nCompacted conversation: "
                    f"summary ~{summary_tokens:,} token(s), "
                    f"{len(state.conversation)} recent turn(s) kept.\n",
                )
            else:
                self._sse_text(
                    "output",
                    "\nNothing to compact yet; the conversation has only "
                    "one turn or is already compact.\n",
                )
        elif name == "/status":
            state = self.state
            report = chat.chat_status_report(
                state.transcript,
                state.chunks,
                state.depth,
                state.max_context_tokens,
                len(state.cache),
                article=state.article_path,
            )
            report += (
                f"\n  conversation: {len(state.conversation)} recent turn(s) "
                f"(verbatim window: {CONVERSATION_MAX_TURNS})"
            )
            summary_tokens = (
                chat.estimate_tokens(state.conversation_summary)
                if state.conversation_summary
                else 0
            )
            if state.conversation_summary:
                report += (
                    f"\n  conversation summary: ~{summary_tokens:,} token(s) "
                    f"across {state.conversation_summary_turns} compacted "
                    "turn(s)"
                )
            else:
                report += "\n  conversation summary: none"
            recent_tokens = chat.estimate_tokens(
                format_conversation(state.conversation)
            )
            report += (
                f"\n  accumulated context: ~{summary_tokens + recent_tokens:,} "
                f"token(s) (summary ~{summary_tokens:,} + recent "
                f"~{recent_tokens:,})"
            )
            if state.anthem_mode:
                report += (
                    "\n  provider search: Anthem mode on "
                    f"({state.anthem_records} record(s) in context)"
                )
            if state.united_healthcare_mode:
                report += (
                    "\n  provider search: UnitedHealthcare mode on "
                    f"({state.united_healthcare_records} record(s) in "
                    "context)"
                )
            self._sse_text("output", "\n" + report + "\n")
        elif name == "/sources":
            self._command_sources()
        elif name == "/benchmark":
            self._command_benchmark(argument)
        elif name == "/deepresearch":
            self._command_deepresearch(argument)
        elif name == "/research_with_the_news":
            self._command_research_with_the_news(argument)
        elif name == "/anthem":
            self._command_anthem(argument)
        elif name == "/united_healthcare":
            self._command_united_healthcare(argument)
        elif name == "/write_a_book":
            self._command_write_a_book(argument)
        elif name == "/books":
            self._command_books(argument)
        elif name == "/pdf_books":
            self._command_pdf_books(argument)
        else:
            self._sse_text(
                "output",
                f"\nUnknown command: {name}. Use /help to see the available "
                "commands.\n",
            )

    def _command_help(self) -> None:
        lines = ["", "Commands:"]
        for name, description in sorted(WEB_COMMANDS.items()):
            lines.append(f"  {name:<20} {description}")
        lines.extend(
            [
                "",
                "Keyboard:",
                "  Esc          normal mode        i / a / A     insert",
                "  h / l / 0 / $                 move left / right / start / end",
                "  w / b                        move by word",
                "  x / dw / dd / D              delete char / word / line / to end",
                "  v / V                        visual / visual-line mode",
                "  u                            undo",
                "  Tab                          complete command or transcript",
                "  Up / Down, or k / j          previous / next history entry",
                "  Click any word in an answer  open a streaming /dict popup",
                "",
            ]
        )
        self._sse_text("output", "\n".join(lines))


# Cross-component imports are placed after the definitions so a
# feature module can depend on another feature module safely.
from .answer import build_answer_annotations
from .article import ARTICLE_DETAILED_OVERVIEW_RE, ARTICLE_DIGEST_INDEX, ARTICLE_ITERATIVE_EVIDENCE_TOKENS, ARTICLE_NARROW_CONTEXT_TOKENS, ARTICLE_OVERVIEW_INDEX, ARTICLE_SECTION_CONTEXT_TOKENS, ARTICLE_SECTION_DEPTH, ArticleRoute, SCOPED_CONTEXT_FALLBACK_PROMPT_PREFIX, TEMPORAL_QUESTION_PROMPT_PREFIX, YOUTUBE_ITERATIVE_PROMPT_PREFIX, _article_iterative_evidence, _article_source_section, _iterative_paragraph_chunks, _make_article_expansion, _scoped_context_fallback, _youtube_iterative_evidence, article_question_route, article_requested_word_count
from .config import CONVERSATION_MAX_TURNS, GITHUB_SNIPPET_LIMIT, GO_LANGUAGE, PHP_LANGUAGE, RUST_LANGUAGE, SOURCE_DOCUMENTATION, SOURCE_OLLAMA
from .conversation import conversation_answer_prefix, format_conversation
from .core import WEB_COMMANDS
from .directory_anthem import _anthem_online_tool_matches
from .github import format_github_code_answer, github_code_snippets
from .image import IMAGE_MODEL, IMAGE_NUM_CTX, IMAGE_QUESTION_PROMPT, ImageModelUnavailable, ImageModelUnsupported, MAX_IMAGE_QUESTION_CHARS, image_conversation_context, image_model_candidates, is_model_unavailable, is_multimodal_unsupported
from .mathtutor import MATHUTOR_LOCK, MATHUTOR_MODEL, _mathtutor_answer_prompt, _mathtutor_append_history, _mathtutor_load_session
from .programming import coding_prompt_prefix, detect_code_language, explicit_code_request, github_code_fallback_allowed, language_chat_model, language_coding_directive, language_coding_model, language_uses_official_docs, programming_explanation_question, programming_feature_question, question_requests_coding
from .programming_docs import answer_is_insufficient, attach_documentation_answer, documentation_answer_is_insufficient, documentation_code_request, documentation_descriptor, golang_package_docs_request, php_symbol_docs_request, rust_code_docs_request, rust_crate_docs_request, stream_code_rescue
from .retrieval import ClientDisconnected
from .video import get_gutenberg_chat_state, get_news_chat_state, get_video_chat_state
