"""Server-side chat state, retrieval, and answer streaming."""

from __future__ import annotations

from .core import *

class ChatState:
    """The server-side equivalent of chat.py's interactive session state."""

    def __init__(
        self,
        max_context_tokens: int = chat.MAX_CONTEXT_TOKENS,
        progressive: bool = False,
        chunks: list[chat.Chunk] | None = None,
    ) -> None:
        self.lock = threading.RLock()
        self.max_context_tokens = max_context_tokens
        self.progressive = progressive
        self.depth: int | None = chat.DEFAULT_DEPTH
        # The language of the most recent code question, when one was named.
        # It lets a follow-up such as "now add error handling" keep using the
        # same official documentation.
        self.code_language: str = ""
        self.transcript: str | None = None
        # Set on an isolated in-modal session for one learned news story.
        self.news_id: str | None = None
        self.article_path: str | None = None
        self.article_title: str = ""
        self.article_structure: dict | None = None
        self.article_iterative = False
        # The /mathtutor session the browser is currently using. Sessions are
        # also discoverable on disk, so a server restart resumes the latest
        # uploaded test papers.
        self.mathtutor_session_id: str | None = None
        self.cache = chat.AnswerCache()
        # /youtube search results are reused by the "More" button instead of
        # rerunning the (network-bound) YouTube search from scratch.
        self.youtube_search_cache: dict[str, list[dict]] = {}
        # In-progress background cache growth, keyed by search keyword.
        # The More handler waits on the event instead of starting a duplicate
        # search for the same keyword.
        self.youtube_prefetch: dict[str, threading.Event] = {}
        # Background /youtube search card "Learn" jobs, keyed by video id.
        self.video_learn_jobs: dict[str, dict] = {}
        self.video_learn_lock = threading.Lock()
        self.latest_answer: str | None = None
        self.latest_sources: list[tuple[chat.Chunk, float]] = []
        self.latest_benchmark: dict | None = None
        self.benchmark_mode = False
        # /anthem keeps a provider-directory thread. While the mode is on,
        # ordinary questions are answered by the Anthem search path until
        # the user enters /anthem off. The last directory context is retained
        # so a follow-up such as "what is his phone number?" can be answered.
        self.anthem_mode = False
        self.anthem_context = ""
        self.anthem_plan: dict[str, str] = {}
        self.anthem_providers: list[dict] = []
        self.anthem_source = ""
        self.anthem_records = 0
        self.anthem_items_sent = False
        # /united_healthcare uses the public UnitedHealthcare Plan-Net FHIR
        # directory. It has its own mode and conversation thread.
        self.united_healthcare_mode = False
        self.united_healthcare_context = ""
        self.united_healthcare_plan: dict[str, str] = {}
        self.united_healthcare_providers: list[dict] = []
        self.united_healthcare_source = ""
        self.united_healthcare_records = 0
        # /research_with_the_news keeps answering every following question in
        # the accumulated research context until the user turns it off. The
        # Mode selector defaults to Online, so the browser starts in this mode.
        self.research_with_news_mode = True
        # /moonbit answers questions from the official MoonBit documentation
        # in its own conversation thread until /moonbit off.
        self.moonbit_mode = False
        self.conversation: list[dict] = []
        self.conversation_summary: str = ""
        self.conversation_summary_turns: int = 0
        self.conversation_revision = 0
        # English Corner: corpus band highlighted in article readers. Shared
        # through .chat/english_corner_settings.json so a server restart keeps
        # the learner's chosen difficulty band. When the setting has never
        # been written, highlighting starts at the documented default band;
        # an explicitly saved empty value/"off" keeps it disabled.
        saved_english_range = _load_english_corner_settings().get(
            "english_words_to_learn"
        )
        if saved_english_range is None:
            self.english_words_to_learn = ENGLISH_CORNER_DEFAULT_RANGE
        else:
            self.english_words_to_learn = str(saved_english_range).strip()
        self.english_words_label = ""
        chat.HISTORY_FILE.parent.mkdir(parents=True, exist_ok=True)
        chat.HISTORY_FILE.touch(exist_ok=True)
        if chunks is None:
            chunks, _printed, error = load_all_chunks_captured()
            if error or chunks is None:
                raise SystemExit(error or "No trunks found in the global corpus")
        self.chunks = chunks
        # The global corpus stays loaded even while /transcript or /article uses
        # a narrower in-memory scope.
        # An explicitly supplied corpus (the in-video chat) is also used as the
        # fallback corpus.
        self.global_chunks = chunks
        self.history: list[str] = self.read_history()
        self.started_at = __import__("time").time()

    # ---- history -----------------------------------------------------
    def read_history(self) -> list[str]:
        try:
            lines = chat.HISTORY_FILE.read_text(
                encoding="utf-8", errors="replace"
            ).splitlines()
        except OSError:
            return []
        return [line for line in lines if line.strip()]

    def append_history(self, entry: str) -> None:
        entry = entry.replace("\n", " ").strip()
        if not entry:
            return
        try:
            with chat.HISTORY_FILE.open("a", encoding="utf-8") as handle:
                handle.write(entry + "\n")
        except OSError:
            return
        if not self.history or self.history[-1] != entry:
            self.history.append(entry)
            if len(self.history) > 2000:
                self.history = self.history[-2000:]

    # ---- conversation ------------------------------------------------
    def reset_conversation(self) -> None:
        self.conversation.clear()
        self.conversation_summary = ""
        self.conversation_summary_turns = 0
        self.conversation_revision += 1

    def default_to_online_mode(self) -> None:
        """Reset the knowledge mode to Online for a fresh chat load.

        The chat state is shared by the browser and the server, so a provider
        mode left active in an earlier session must not become the default
        mode a newly loaded page sees.
        """
        provider_mode_was_on = (
            self.anthem_mode
            or self.united_healthcare_mode
            or self.moonbit_mode
        )
        self.moonbit_mode = False
        self.research_with_news_mode = True
        self.anthem_mode = False
        self.anthem_context = ""
        self.anthem_plan = {}
        self.anthem_providers = []
        self.anthem_source = ""
        self.anthem_records = 0
        self.anthem_items_sent = False
        self.united_healthcare_mode = False
        self.united_healthcare_context = ""
        self.united_healthcare_plan = {}
        self.united_healthcare_providers = []
        self.united_healthcare_source = ""
        self.united_healthcare_records = 0
        if provider_mode_was_on:
            self.reset_conversation()
        self.cache.clear()

    def resolve_question(self, question: str) -> ConversationResolution:
        """Interpret a question against the recent conversation.

        A related follow-up is rewritten into a standalone retrieval question.
        A question that is completely unrelated to the previous turns starts a
        fresh context, so stale names and pronouns cannot leak into later
        retrieval.
        """
        if not self.conversation:
            return ConversationResolution(question)
        if not question_needs_context(question, self.conversation):
            overlap = conversation_overlap_ratio(question, self.conversation)
            if overlap > 0.5:
                return ConversationResolution(question)
            if overlap == 0.0 and not self.conversation_summary:
                # No shared topic at all: confidently a fresh question.
                self.reset_conversation()
                return ConversationResolution(
                    question, is_follow_up=False, is_unrelated=True
                )
            # Partial overlap is ambiguous: ask the conversation model instead
            # of guessing, so a longer follow-up is not dropped and a longer
            # unrelated question does not inherit stale context.
        resolution = rewrite_follow_up(
            question, self.conversation, self.conversation_summary
        )
        if resolution is None:
            return ConversationResolution(question)
        if resolution.is_unrelated:
            self.reset_conversation()
        return resolution

    def compact_conversation(self, force: bool = False) -> bool:
        """Fold older turns into a compact running summary.

        The newest turns stay verbatim. Compaction triggers only after the
        conversation has grown past ``CONVERSATION_COMPACT_AFTER_TURNS``; a
        manual ``/compact`` can force it earlier. The summary model is the small
        helper that is already resident, so this does not add an answer-model
        call. If summarization fails, a deterministic question list is kept so
        no history is silently lost.
        """
        if len(self.conversation) <= 1:
            return False
        if (
            not force
            and len(self.conversation) <= CONVERSATION_COMPACT_AFTER_TURNS
        ):
            return False
        keep_count = min(
            CONVERSATION_RECENT_TURNS_AFTER_COMPACT,
            len(self.conversation) - 1,
        )
        if keep_count < 1:
            return False
        evicted = self.conversation[:-keep_count]
        recent = self.conversation[-keep_count:]
        if not evicted:
            return False

        summary = ""
        try:
            prompt = CONVERSATION_SUMMARY_PROMPT.format(
                summary=self.conversation_summary or "(none)",
                turns=format_conversation(evicted, limit=None),
            )
            summary = chat.generate_text(
                prompt,
                model=CONVERSATION_MODEL,
                temperature=0.0,
                num_predict=CONVERSATION_SUMMARY_NUM_PREDICT,
                num_ctx=chat.ANSWER_NUM_CTX,
                stop=["</summary>"],
            )
        except Exception:
            # A summarization failure must never block the chat. The fallback
            # below keeps the questions and any existing summary.
            summary = ""
        summary = clean_conversation_summary(summary)
        if not summary:
            summary = fallback_conversation_summary(
                evicted, self.conversation_summary
            )
        self.conversation_summary = clamp_conversation_summary(summary)
        self.conversation = recent
        self.conversation_summary_turns += len(evicted)
        self.conversation_revision += 1
        return True

    def record_turn(
        self, question: str, resolved_question: str, answer: str
    ) -> None:
        self.conversation.append(
            {
                "question": question,
                "resolved_question": resolved_question,
                "answer": answer,
            }
        )
        if len(self.conversation) > CONVERSATION_COMPACT_AFTER_TURNS:
            self.compact_conversation()
        self.conversation_revision += 1

    # ---- state -------------------------------------------------------
    def replace_chunks(
        self,
        chunks: list[chat.Chunk],
        article_path: str | None = None,
        article_title: str = "",
        article_structure: dict | None = None,
        article_iterative: bool = False,
    ) -> None:
        """Install a new knowledge scope and drop the current conversation.

        Switching transcripts or articles starts a fresh chain of questions.
        Passing an ``article_path`` enters article mode; every other caller
        leaves article mode.
        """
        self.chunks = chunks
        self.article_path = article_path
        self.article_title = article_title
        self.article_structure = article_structure
        self.article_iterative = bool(article_iterative and article_path)
        chat.clear_bm25_cache()
        self.reset_conversation()
        gc.collect()

    @property
    def depth_label(self) -> str:
        if self.depth is None:
            return "auto"
        return str(self.depth)

    def snapshot(self, include_history: bool = False) -> dict:
        all_ids = {chunk.video_id for chunk in self.chunks}
        news_ids = {video_id for video_id in all_ids if NEWS_ID_PATTERN.fullmatch(video_id)}
        video_ids = all_ids - news_ids
        summary_tokens = (
            chat.estimate_tokens(self.conversation_summary)
            if self.conversation_summary
            else 0
        )
        recent_tokens = chat.estimate_tokens(
            format_conversation(self.conversation)
        )
        data = {
            "transcript": self.transcript,
            "depth": self.depth,
            "depth_label": self.depth_label,
            "default_depth": chat.DEFAULT_DEPTH,
            "max_context_tokens": self.max_context_tokens,
            "context_tokens": int(
                (self.latest_benchmark or {}).get("context_tokens") or 0
            ),
            "progressive": self.progressive,
            "chunks": len(self.chunks),
            "videos": len(video_ids),
            "news": len(news_ids),
            "code_language": self.code_language or "",
            "scope": (
                f"news {self.news_id!r}"
                if self.news_id is not None
                else (
                    f"article {self.article_path!r}"
                    if self.article_path is not None
                    else (
                        f"transcript {self.transcript!r}"
                        if self.transcript is not None
                        else "global corpus (all learned trunks)"
                    )
                )
            ),
            "article": self.article_path,
            "article_title": self.article_title,
            "article_iterative": self.article_iterative,
            "article_sections": len(
                (self.article_structure or {}).get("sections", [])
            ),
            "models": {
                "chat": language_chat_model(self.code_language),
                "coding": language_coding_model(self.code_language),
                "line_explain": language_coding_model(self.code_language),
                "expansion": chat.EXPANSION_MODEL,
                "filter": chat.FILTER_MODEL,
                "translation": chat.TRANSLATION_MODEL,
                "translation_to_chinese": ANSWER_TRANSLATION_MODEL,
                "conversation": CONVERSATION_MODEL,
                "dict": chat.DICT_MODEL,
            },
            "benchmark_mode": self.benchmark_mode,
            "anthem_mode": self.anthem_mode,
            "anthem_has_context": bool(self.anthem_context),
            "united_healthcare_mode": self.united_healthcare_mode,
            "united_healthcare_has_context": bool(
                self.united_healthcare_context
            ),
            "research_with_news_mode": self.research_with_news_mode,
            "moonbit_mode": self.moonbit_mode,
            "english_words_to_learn": self.english_words_to_learn,
            "english_words_label": self.english_words_label,
            "has_answer": self.latest_answer is not None,
            "source_count": len(self.latest_sources),
            "history_count": len(self.history),
            "conversation_turns": len(self.conversation),
            "conversation_max_turns": CONVERSATION_MAX_TURNS,
            "conversation_summary_tokens": summary_tokens,
            "conversation_recent_tokens": recent_tokens,
            "conversation_context_tokens": summary_tokens + recent_tokens,
            "conversation_summary_turns": self.conversation_summary_turns,
            "conversation_compact_after_turns": CONVERSATION_COMPACT_AFTER_TURNS,
            "commands": [
                {"name": name, "description": description}
                for name, description in sorted(WEB_COMMANDS.items())
            ],
            "argument_commands": sorted(COMMAND_ARGUMENTS),
            "news_sources": [
                {"name": name, "label": label}
                for name, label in sorted(NEWS_SOURCES.items())
            ],
        }
        if include_history:
            data["history"] = self.history[-1000:]
        return data

    def cache_key(
        self,
        question: str,
        answer_language: str | None,
        conversation_revision: int | None = None,
        language: str | None = None,
        coding_mode: bool = False,
        prompt_variant: str = "",
        memory_revision: int = 0,
    ) -> tuple:
        key = chat.make_cache_key(
            self.transcript,
            self.depth,
            self.max_context_tokens,
            answer_language,
            question,
            self.progressive,
        )
        if conversation_revision is not None:
            # Context-dependent questions must not hit an answer cached for a
            # different "he", "that book", etc.
            key += (conversation_revision,)
        if memory_revision:
            # The running summary is part of the answer prompt. Invalidate a
            # cached answer when compaction changes that memory block.
            key += ("conversation-memory", memory_revision)
        if self.article_path is not None:
            # Article answers depend on the section map and route, not only on
            # the scope that happened to be active when /article was entered.
            key += (
                "article",
                self.article_path,
                (self.article_structure or {}).get("video_id", ""),
                self.article_iterative,
            )
        if self.news_id is not None:
            key += ("news", self.news_id)
        if coding_mode:
            # The coding model and prompt differ from the regular RAG answer.
            key += ("coding", language_coding_model(language))
        else:
            # The model differs when a documentation-backed language is
            # active.
            key += ("model", language_chat_model(language))
        if prompt_variant:
            # Documentation-first answers use the official docs as prompt
            # context, so they must not reuse a plain coding-mode answer.
            key += (prompt_variant,)
        return key


# Cross-component imports are placed after the definitions so a
# feature module can depend on another feature module safely.
from .command_config import COMMAND_ARGUMENTS
from .config import CONVERSATION_COMPACT_AFTER_TURNS, CONVERSATION_MAX_TURNS, CONVERSATION_MODEL, CONVERSATION_RECENT_TURNS_AFTER_COMPACT, CONVERSATION_SUMMARY_NUM_PREDICT
from .conversation import CONVERSATION_SUMMARY_PROMPT, ConversationResolution, clamp_conversation_summary, clean_conversation_summary, conversation_overlap_ratio, fallback_conversation_summary, format_conversation, question_needs_context, rewrite_follow_up
from .core import WEB_COMMANDS
from .english_corner import ENGLISH_CORNER_DEFAULT_RANGE, _load_english_corner_settings
from .news_research import NEWS_SOURCES
from .programming import language_chat_model, language_coding_model
from .retrieval import NEWS_ID_PATTERN, load_all_chunks_captured
from .translation import ANSWER_TRANSLATION_MODEL
