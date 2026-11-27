"""MoonBit documentation chat mode."""

from __future__ import annotations

from .core import *
from .conversation import format_conversation
from .moonbit import (
    MOONBIT_MODEL,
    MOONBIT_NUM_CTX,
    moonbit_answer_prompt,
    moonbit_docs_context,
)


class HandlerMoonbitMixin:
    def _command_moonbit(self, argument: str) -> None:
        """Start, stop, or query the MoonBit documentation mode."""
        state = self.state
        value = " ".join(argument.split())
        normalized = value.casefold()
        if normalized in {"off", "stop", "exit", "disable", "disabled"}:
            if not state.moonbit_mode:
                self._sse_text(
                    "output",
                    "\nMoonBit docs mode is already off; normal chat "
                    "is active.\n",
                )
                return
            state.moonbit_mode = False
            self._reset_moonbit_context()
            self._sse("meta", state.snapshot())
            self._sse_text(
                "output",
                "\nMoonBit docs mode is off; normal chat is back.\n",
            )
            return
        if normalized in {"", "on", "start", "enable", "enabled"}:
            if state.moonbit_mode:
                self._sse_text(
                    "output",
                    "\nMoonBit docs mode is already on. Ask a question "
                    "about MoonBit.\n",
                )
                return
            self._enter_moonbit_mode()
            self._sse("meta", state.snapshot())
            self._sse_text(
                "output",
                "\nMoonBit docs mode is on. Ask about the MoonBit "
                "programming language; answers use the official docs at "
                "https://docs.moonbitlang.com/. Use /moonbit off to return "
                "to normal chat.\n",
            )
            return

        was_on = state.moonbit_mode
        if not was_on:
            self._enter_moonbit_mode()
        self._sse("meta", state.snapshot())
        self._run_moonbit_question(value)

    def _enter_moonbit_mode(self) -> None:
        state = self.state
        if state.sat_mode:
            state.sat_mode = False
            self._reset_sat_context()
        if state.anthem_mode:
            state.anthem_mode = False
            self._reset_anthem_context()
        if state.united_healthcare_mode:
            state.united_healthcare_mode = False
            self._reset_united_healthcare_context()
        state.moonbit_mode = True
        self._reset_moonbit_context()

    def _reset_moonbit_context(self) -> None:
        state = self.state
        state.reset_conversation()
        state.cache.clear()
        state.latest_answer = None
        state.latest_sources = []
        state.latest_benchmark = None
        state.code_language = ""

    def _run_moonbit_question(self, question: str) -> None:
        """Answer one question from the official MoonBit documentation."""
        state = self.state
        question = " ".join(str(question or "").split())
        if not question:
            self._sse_text(
                "output",
                "\nAsk a question about the MoonBit programming language.\n",
            )
            return

        resolved = question
        if state.conversation:
            resolution = state.resolve_question(question)
            if resolution.question != question:
                self._sse_text(
                    "log",
                    "[moonbit: interpreting the follow-up as: "
                    f"{resolution.question}]",
                )
                resolved = resolution.question

        self._sse_text(
            "log",
            "[moonbit: searching the official MoonBit documentation]",
        )
        try:
            context, sources = moonbit_docs_context(resolved)
        except Exception as exc:
            message = f"MoonBit documentation search failed: {exc}"
            self._sse_text("output", f"\n{message}\n")
            state.latest_answer = message
            state.latest_sources = []
            state.latest_benchmark = None
            state.record_turn(question, resolved, message)
            return

        if not context:
            message = (
                "I could not find a matching page in the official MoonBit "
                "documentation. Try rephrasing the question with a MoonBit "
                "keyword such as struct, enum, trait, async, or package."
            )
            self._sse_text("output", f"\n{message}\n")
            state.latest_answer = message
            state.latest_sources = []
            state.latest_benchmark = None
            state.record_turn(question, resolved, message)
            return

        for source in sources:
            self._sse_text(
                "log",
                f"[moonbit: {source['title']} — {source['url']}]",
            )

        conversation = ""
        if state.conversation:
            conversation = format_conversation(
                state.conversation,
                state.conversation_summary,
                limit=4,
            )
        prompt = moonbit_answer_prompt(resolved, context, conversation)
        answer = self._stream_moonbit_answer(prompt)
        state.latest_answer = answer
        state.latest_sources = []
        state.latest_benchmark = None
        state.record_turn(question, resolved, answer)

    def _stream_moonbit_answer(self, prompt: str) -> str:
        payload = {
            "model": MOONBIT_MODEL,
            "prompt": prompt,
            "stream": True,
            "think": False,
            "keep_alive": chat.OLLAMA_KEEP_ALIVE,
            "options": {
                "temperature": 0.2,
                "num_ctx": MOONBIT_NUM_CTX,
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
