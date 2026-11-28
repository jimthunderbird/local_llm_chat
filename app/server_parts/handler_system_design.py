"""Slash-command requests for the System Design and Architecture reader."""

from __future__ import annotations

from .core import *


class HandlerSystemDesignMixin:
    def _command_system_design_and_architecture(self, argument: str) -> None:
        """Open or close the System Design and Architecture reading mode.

        Usage: /system_design_and_architecture [off|refresh]
        The command clears the current conversation context, then streams the
        repository's topic list. The browser renders the list as a reading
        panel and fetches each English Markdown chapter on demand through
        /api/system_design_topic, so the modal can show text and images.
        """
        state = self.state
        value = " ".join(str(argument or "").split())
        normalized = value.casefold()
        if normalized in {"off", "stop", "exit", "quit", "disable", "disabled"}:
            self._sse("system_design", {"type": "off"})
            self._sse_text(
                "output",
                "\nSystem Design and Architecture mode is off; "
                "back to the main chat.\n",
            )
            return

        # "clear current chat context": a reader opened from this command is
        # a fresh session, so an earlier conversation cannot leak into later
        # questions asked after returning to the main chat.
        state.reset_conversation()
        state.cache.clear()
        state.latest_answer = None
        state.latest_sources = []
        state.latest_benchmark = None
        state.code_language = ""
        self._sse("meta", state.snapshot())

        try:
            topics = system_design_topics(force=normalized == "refresh")
        except (RuntimeError, OSError) as exc:
            self._sse(
                "system_design",
                {"type": "error", "message": str(exc)},
            )
            self._sse_text(
                "output",
                f"\nCould not load the System Design and Architecture "
                f"topics: {exc}\n",
            )
            return

        groups: list[str] = []
        for topic in topics:
            group = str(topic.get("group") or "Topics")
            if not groups or groups[-1] != group:
                groups.append(group)
        self._sse(
            "system_design",
            {
                "type": "start",
                "repo": SYSTEM_DESIGN_REPO_URL,
                "count": len(topics),
                "groups": groups,
            },
        )
        for index, topic in enumerate(topics, 1):
            self._sse(
                "system_design",
                {"type": "item", "index": index, **topic},
            )
        self._sse("system_design", {"type": "done"})
        self._sse_text(
            "output",
            (
                f"\nOpened System Design and Architecture: "
                f"{len(topics)} topic(s). Click a topic to read it, or use "
                "/system_design_and_architecture off to return to the main "
                "chat.\n"
            ),
        )


# Cross-component imports are placed after the definitions so a
# feature module can depend on another feature module safely.
from .system_design import SYSTEM_DESIGN_REPO_URL, system_design_topic, system_design_topics
