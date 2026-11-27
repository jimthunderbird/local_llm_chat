"""SAT question-bank chat mode."""

from __future__ import annotations

from .core import *
from .conversation import format_conversation
from .sat import (
    SAT_MODEL,
    SAT_NUM_CTX,
    build_sat_context,
    sat_answer_model_for,
    sat_answer_prompt,
    sat_analysis_prompt,
    sat_bank_ready,
    sat_domain_question,
    sat_domains,
    sat_generated_question_answer,
    sat_is_next_question_request,
    sat_model_for_subject,
    sat_question_request_answer,
    sat_random_question_answer,
    sat_reveal_prompt,
    sat_rw_domains,
    sat_topic_list_answer,
    sat_unseen_question_left,
)


class HandlerSatMixin:
    def _command_sat(self, argument: str) -> None:
        """Start, stop, or query the SAT question-bank mode."""
        state = self.state
        value = " ".join(argument.split())
        normalized = value.casefold()
        if normalized in {"off", "stop", "exit", "disable", "disabled"}:
            if not state.sat_mode:
                self._sse_text(
                    "output",
                    "\nSAT mode is already off; normal chat is active.\n",
                )
                return
            state.sat_mode = False
            self._reset_sat_context()
            self._sse("meta", state.snapshot())
            self._sse_text(
                "output",
                "\nSAT mode is off; normal chat is back.\n",
            )
            return
        if normalized in {"", "on", "start", "enable", "enabled"}:
            was_on = state.sat_mode
            if not was_on:
                self._enter_sat_mode()
                self._sse("meta", state.snapshot())
            try:
                domains = sat_domains()
            except Exception as exc:
                self._sse_text(
                    "output", f"\nCould not load SAT subjects: {exc}\n"
                )
                return
            self._sse(
                "sat",
                {
                    "view": "domains",
                    "domains": domains,
                    "rw_domains": sat_rw_domains(),
                },
            )
            self._sse_text(
                "output",
                (
                    "\nSAT subjects are listed above.\n"
                    if was_on
                    else "\nSAT mode is on. Choose a subject above to "
                    "practice, or ask an SAT math question. The Reading and "
                    "Writing sections below practice the sample-test bank. "
                    "Use /sat off to return to normal chat.\n"
                ),
            )
            return

        was_on = state.sat_mode
        if not was_on:
            self._enter_sat_mode()
        self._sse("meta", state.snapshot())
        self._run_sat_question(value)

    def _enter_sat_mode(self) -> None:
        state = self.state
        if state.moonbit_mode:
            state.moonbit_mode = False
            self._reset_moonbit_context()
        if state.anthem_mode:
            state.anthem_mode = False
            self._reset_anthem_context()
        if state.united_healthcare_mode:
            state.united_healthcare_mode = False
            self._reset_united_healthcare_context()
        state.sat_mode = True
        self._reset_sat_context()

    def _reset_sat_context(self) -> None:
        state = self.state
        state.reset_conversation()
        state.cache.clear()
        state.latest_answer = None
        state.latest_sources = []
        state.latest_benchmark = None
        state.code_language = ""
        state.sat_pending_question = None
        state.sat_recent_question_ids = []

    def _run_sat_question(self, question: str) -> None:
        """Answer one SAT math question using the PineSAT bank and local model."""
        state = self.state
        question = " ".join(str(question or "").split())
        if not question:
            self._sse_text(
                "output",
                "\nAsk an SAT question, for example a practice question or "
                "a topic you want explained.\n",
            )
            return

        resolved = question
        if state.conversation:
            resolution = state.resolve_question(question)
            if resolution.question != question:
                self._sse_text(
                    "log",
                    "[sat: interpreting the follow-up as: "
                    f"{resolution.question}]",
                )
                resolved = resolution.question

        if not sat_bank_ready():
            self._sse_text(
                "output",
                "\nLoading the SAT question bank; this first lookup "
                "may take a few seconds…\n",
            )
        try:
            reveal_prompt = sat_reveal_prompt(
                resolved, state.sat_pending_question
            )
            if reveal_prompt is not None:
                self._sse_text(
                    "log",
                    "[sat: solving the pending question with the local model]",
                )
                answer = self._stream_sat_answer(
                    reveal_prompt,
                    model=sat_model_for_subject(
                        str((state.sat_pending_question or {}).get(
                            "subject"
                        ) or "")
                    ),
                )
                state.latest_answer = answer
                state.latest_sources = []
                state.latest_benchmark = None
                state.record_turn(question, resolved, answer)
                return
            next_question = sat_is_next_question_request(resolved)
            recent_ids = state.sat_recent_question_ids
            if next_question:
                if sat_unseen_question_left(exclude_ids=recent_ids):
                    question_request = sat_random_question_answer(
                        exclude_ids=recent_ids
                    )
                else:
                    # Every question in {questions_cache_dir} was recently
                    # shown; {question_llm} generates a similar new one so
                    # the question supply stays unlimited.
                    question_request = sat_generated_question_answer(
                        "", exclude_ids=recent_ids
                    ) or sat_random_question_answer()
            else:
                question_request = sat_question_request_answer(
                    resolved,
                    exclude_ids=recent_ids,
                )
                if question_request is None:
                    # No cached question matches the request; generate a
                    # similar one with {question_llm} instead of falling
                    # back to an ungrounded answer.
                    question_request = sat_generated_question_answer(
                        resolved, exclude_ids=recent_ids
                    )
            if question_request is not None:
                answer, sources, selected = question_request
                state.sat_pending_question = selected
                selected_id = str(
                    selected.get("stableId")
                    or selected.get("questionNumber")
                    or ""
                ).strip()
                if selected_id:
                    state.sat_recent_question_ids.append(selected_id)
                    state.sat_recent_question_ids = (
                        state.sat_recent_question_ids[-20:]
                    )
                generated = str(selected.get("source") or "") == "generated"
                if generated and next_question:
                    log_line = (
                        "[sat: cache exhausted — "
                        f"{SAT_MODEL} generated a new question]"
                    )
                elif generated:
                    log_line = (
                        "[sat: no cache match — "
                        f"{SAT_MODEL} generated a similar question]"
                    )
                elif next_question:
                    log_line = (
                        "[sat: presenting a random question from the cache]"
                    )
                else:
                    log_line = (
                        "[sat: showing a cache question without its answer]"
                    )
                self._sse_text("log", log_line)
                self._sse_text("delta", answer)
                state.latest_answer = answer
                state.latest_sources = []
                state.latest_benchmark = None
                state.record_turn(question, resolved, answer)
                return
            topic_answer = sat_topic_list_answer(resolved)
        except Exception as exc:
            message = f"SAT question-bank search failed: {exc}"
            self._sse_text("output", f"\n{message}\n")
            state.latest_answer = message
            state.latest_sources = []
            state.latest_benchmark = None
            state.record_turn(question, resolved, message)
            return
        if topic_answer is not None:
            answer, topic_sources = topic_answer
            self._sse_text(
                "log",
                "[sat: answering from the bank's domain/skill taxonomy]",
            )
            for source in topic_sources:
                self._sse_text(
                    "log",
                    "[sat: bank topics — "
                    f"{source.get('subject') or 'SAT'}]",
                )
            self._sse_text("delta", answer)
            state.latest_answer = answer
            state.latest_sources = []
            state.latest_benchmark = None
            state.record_turn(question, resolved, answer)
            return

        self._sse_text(
            "log",
            "[sat: searching the PineSAT math question bank]",
        )
        try:
            context, sources = build_sat_context(resolved)
        except Exception as exc:
            message = f"SAT question-bank search failed: {exc}"
            self._sse_text("output", f"\n{message}\n")
            state.latest_answer = message
            state.latest_sources = []
            state.latest_benchmark = None
            state.record_turn(question, resolved, message)
            return

        if sources:
            for source in sources:
                self._sse_text(
                    "log",
                    "[sat: matched "
                    f"{source.get('subject') or 'SAT'} question "
                    f"{source.get('stable_id') or source.get('question_number')}]",
                )
        else:
            self._sse_text(
                "log",
                "[sat: no matching PineSAT question; answering from "
                "general SAT math knowledge]",
            )

        conversation = ""
        if state.conversation:
            conversation = format_conversation(
                state.conversation,
                state.conversation_summary,
                limit=4,
            )
        prompt = sat_answer_prompt(resolved, context, conversation)
        answer_model = sat_answer_model_for(resolved)
        if answer_model != SAT_MODEL:
            self._sse_text(
                "log",
                "[sat: answering with the reading and writing model]",
            )
        answer = self._stream_sat_answer(prompt, model=answer_model)
        state.latest_answer = answer
        state.latest_sources = []
        state.latest_benchmark = None
        state.record_turn(question, resolved, answer)

    def _handle_sat_analysis(self, payload: dict) -> None:
        """Stream the local model's answer and teacher analysis for a question."""
        domain = str(payload.get("domain") or "").strip()
        try:
            index = int(payload.get("index") or 0)
        except (TypeError, ValueError):
            index = 0
        try:
            data = sat_domain_question(domain, index)
        except Exception as exc:
            self._send_error_json(f"SAT analysis lookup failed: {exc}", 502)
            return
        if data is None:
            self._send_error_json("Unknown SAT subject or question.", 404)
            return

        prompt = sat_analysis_prompt(data.get("question") or {})
        # prompts/sat.prompt: math related answers use {question_llm_math},
        # reading and writing related answers use
        # {question_llm_reading_and_writing}.
        analysis_model = sat_model_for_subject(
            str((data.get("question") or {}).get("subject") or "")
        )
        self._begin_sse()
        request_payload = {
            "model": analysis_model,
            "prompt": prompt,
            "stream": True,
            "think": False,
            "keep_alive": chat.OLLAMA_KEEP_ALIVE,
            "options": {
                "temperature": 0.3,
                "num_ctx": SAT_NUM_CTX,
            },
        }
        try:
            with requests.post(
                f"{chat.OLLAMA_HOST}/api/generate",
                json=request_payload,
                stream=True,
                timeout=(10, 900),
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
                        self._sse_text("delta", token)
                    if event.get("done"):
                        break
        except requests.RequestException as exc:
            self._sse(
                "error",
                {
                    "message": (
                        f"Could not reach Ollama at {chat.OLLAMA_HOST}. "
                        f"Is it running? ({exc})"
                    )
                },
            )
        except Exception as exc:
            self._sse("error", {"message": f"SAT analysis failed: {exc}"})
        finally:
            self._sse("done", {})

    def _stream_sat_answer(self, prompt: str, model: str = SAT_MODEL) -> str:
        payload = {
            "model": model,
            "prompt": prompt,
            "stream": True,
            "think": False,
            "keep_alive": chat.OLLAMA_KEEP_ALIVE,
            "options": {
                "temperature": 0.2,
                "num_ctx": SAT_NUM_CTX,
            },
        }
        parts: list[str] = []
        try:
            with requests.post(
                f"{chat.OLLAMA_HOST}/api/generate",
                json=payload,
                stream=True,
                timeout=(10, 900),
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
                        parts.append(token)
                        self._sse_text("delta", token)
                    if event.get("done"):
                        break
        except requests.RequestException as exc:
            raise RuntimeError(
                f"Could not reach Ollama at {chat.OLLAMA_HOST}. "
                f"Is it running? ({exc})"
            ) from exc
        return "".join(parts)
