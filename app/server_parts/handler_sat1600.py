"""1600.now SAT question-bank chat mode."""

from __future__ import annotations

from .core import *
from .conversation import format_conversation
from .sat1600 import (
    SAT1600_MODEL,
    SAT1600_NUM_CTX,
    build_sat1600_context,
    fetch_sat1600_image,
    sat1600_answer_prompt,
    sat1600_bank_ready,
    sat1600_image_url,
    sat1600_question_request_answer,
    sat1600_reveal_answer,
    sat1600_topic_list_answer,
)


class HandlerSat1600Mixin:
    def _command_sat_1600(self, argument: str) -> None:
        """Start, stop, or query the 1600.now SAT question-bank mode."""
        state = self.state
        value = " ".join(argument.split())
        normalized = value.casefold()
        if normalized in {"off", "stop", "exit", "disable", "disabled"}:
            if not state.sat1600_mode:
                self._sse_text(
                    "output",
                    "\nSAT 1600 mode is already off; normal chat is active.\n",
                )
                return
            state.sat1600_mode = False
            self._reset_sat1600_context()
            self._sse("meta", state.snapshot())
            self._sse_text(
                "output",
                "\nSAT 1600 mode is off; normal chat is back.\n",
            )
            return
        if normalized in {"", "on", "start", "enable", "enabled"}:
            if state.sat1600_mode:
                self._sse_text(
                    "output",
                    "\nSAT 1600 mode is already on. Ask an SAT question.\n",
                )
                return
            self._enter_sat1600_mode()
            self._sse("meta", state.snapshot())
            self._sse_text(
                "output",
                "\nSAT 1600 mode is on. Ask an SAT question; I will use the "
                "1600.now question bank and the local model to explain and "
                "solve it. Use /sat_1600 off to return to normal chat.\n",
            )
            return

        was_on = state.sat1600_mode
        if not was_on:
            self._enter_sat1600_mode()
        self._sse("meta", state.snapshot())
        self._run_sat1600_question(value)

    def _enter_sat1600_mode(self) -> None:
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
        state.sat1600_mode = True
        self._reset_sat1600_context()

    def _reset_sat1600_context(self) -> None:
        state = self.state
        state.reset_conversation()
        state.cache.clear()
        state.latest_answer = None
        state.latest_sources = []
        state.latest_benchmark = None
        state.code_language = ""
        state.sat1600_pending_question = None
        state.sat1600_recent_question_ids = []

    def _run_sat1600_question(self, question: str) -> None:
        """Answer one SAT question using the 1600.now bank and local model."""
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
                    "[sat_1600: interpreting the follow-up as: "
                    f"{resolution.question}]",
                )
                resolved = resolution.question

        if not sat1600_bank_ready():
            self._sse_text(
                "output",
                "\nLoading the 1600.now SAT question bank; this first lookup "
                "may take a few seconds…\n",
            )
        try:
            reveal = sat1600_reveal_answer(
                resolved, state.sat1600_pending_question
            )
            if reveal is not None:
                answer, sources = reveal
                self._sse_text(
                    "log",
                    "[sat_1600: revealing the official answer for the "
                    "pending question]",
                )
                self._sse_text("delta", answer)
                state.latest_answer = answer
                state.latest_sources = []
                state.latest_benchmark = None
                state.record_turn(question, resolved, answer)
                return
            question_request = sat1600_question_request_answer(
                resolved,
                exclude_ids=state.sat1600_recent_question_ids,
            )
            if question_request is not None:
                answer, sources, selected = question_request
                state.sat1600_pending_question = selected
                selected_id = str(
                    selected.get("stableId")
                    or selected.get("questionNumber")
                    or ""
                ).strip()
                if selected_id:
                    state.sat1600_recent_question_ids.append(selected_id)
                    state.sat1600_recent_question_ids = (
                        state.sat1600_recent_question_ids[-20:]
                    )
                self._sse_text(
                    "log",
                    "[sat_1600: showing a bank question without its answer]",
                )
                self._sse_text("delta", answer)
                state.latest_answer = answer
                state.latest_sources = []
                state.latest_benchmark = None
                state.record_turn(question, resolved, answer)
                return
            topic_answer = sat1600_topic_list_answer(resolved)
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
                "[sat_1600: answering from the bank's domain/skill taxonomy]",
            )
            for source in topic_sources:
                self._sse_text(
                    "log",
                    "[sat_1600: bank topics — "
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
            "[sat_1600: searching the 1600.now question bank]",
        )
        try:
            context, sources = build_sat1600_context(resolved)
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
                    "[sat_1600: matched "
                    f"{source.get('subject') or 'SAT'} question "
                    f"{source.get('stable_id') or source.get('question_number')}]",
                )
        else:
            self._sse_text(
                "log",
                "[sat_1600: no matching bank question; answering from "
                "general SAT knowledge]",
            )

        figure_prefix = ""
        if sources:
            figures = sources[0].get("figures") or []
            image_lines = []
            for figure in figures[:4]:
                url = str(figure.get("url") or "").strip()
                if not url:
                    continue
                alt = (
                    str(figure.get("alt") or "SAT question figure")
                    .replace("[", "")
                    .replace("]", "")
                )
                image_lines.append(
                    f"![{alt}]({sat1600_image_url(url)})"
                )
            if image_lines:
                image_lines.append(
                    "*Figure from the 1600.now question bank.*"
                )
                figure_prefix = "\n".join(image_lines) + "\n\n"
                self._sse_text("delta", figure_prefix)

        conversation = ""
        if state.conversation:
            conversation = format_conversation(
                state.conversation,
                state.conversation_summary,
                limit=4,
            )
        prompt = sat1600_answer_prompt(resolved, context, conversation)
        answer = figure_prefix + self._stream_sat1600_answer(prompt)
        state.latest_answer = answer
        state.latest_sources = []
        state.latest_benchmark = None
        state.record_turn(question, resolved, answer)

    def _send_sat1600_image(self, url: str) -> None:
        """Proxy one 1600.now figure so the chat can display it reliably."""
        try:
            data, content_type = fetch_sat1600_image(url)
        except ValueError as exc:
            self._send_error_json(str(exc), 400)
            return
        except (OSError, RuntimeError) as exc:
            self._send_error_json(str(exc), 502)
            return
        try:
            self.send_response(200)
            self.send_header("Content-Type", content_type)
            self.send_header("Content-Length", str(len(data)))
            self.send_header("Cache-Control", "public, max-age=86400")
            self.end_headers()
            self.wfile.write(data)
        except (BrokenPipeError, ConnectionResetError):
            pass

    def _stream_sat1600_answer(self, prompt: str) -> str:
        payload = {
            "model": SAT1600_MODEL,
            "prompt": prompt,
            "stream": True,
            "think": False,
            "keep_alive": chat.OLLAMA_KEEP_ALIVE,
            "options": {
                "temperature": 0.2,
                "num_ctx": SAT1600_NUM_CTX,
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
