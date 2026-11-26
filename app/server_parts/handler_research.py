"""Deep research and news research request handling."""

from __future__ import annotations

from .core import *

class HandlerResearchMixin:
    def _command_deepresearch(self, topic: str) -> None:
        """Run the bounded research loop and stream its process and report."""
        topic = " ".join(topic.split())
        if not topic:
            self._sse_text(
                "output",
                "\nUsage: /deepresearch <topic>\n"
                "Plans sub-questions, retrieves a diverse evidence ledger over "
                "several rounds, then writes a cited report.\n",
            )
            return
        state = self.state
        scope = (
            f"article {state.article_path!r}"
            if state.article_path is not None
            else (
                f"transcript {state.transcript!r}"
                if state.transcript is not None
                else "global corpus"
            )
        )
        self._sse_text(
            "log",
            f"[research] starting /deepresearch on {topic!r} over the {scope}",
        )
        if state.article_path is not None:
            chunks = _iterative_paragraph_chunks(state.chunks)
        else:
            chunks = state.chunks
        started = time.monotonic()
        try:
            report, sources, trace = deep_research.run_deep_research(
                topic,
                chunks,
                log=lambda text: self._sse_text("log", text),
                emit=lambda text: self._sse("delta", {"text": text}),
                source_info=lambda chunk: (chunk_title(chunk), chunk_url(chunk)),
            )
        except (RuntimeError, ValueError) as exc:
            self._sse("error", {"message": str(exc)})
            return
        state.latest_answer = report
        state.latest_sources = sources
        state.latest_benchmark = {
            "model": chat.ANSWER_MODEL,
            "deep_research": trace,
            "trunks": len(sources),
            "candidates": int(trace.get("evidence_count", 0)),
            "context_tokens": 0,
            "total": time.monotonic() - started,
        }
        annotations = build_answer_annotations(report, sources)
        self._sse("annotations", {"items": annotations})
        state.record_turn(topic, topic, report)
        if state.benchmark_mode:
            self._sse_text("log", chat.format_benchmark_line(state.latest_benchmark))

    def _command_research_with_the_news(self, argument: str) -> None:
        """Turn the news-research mode on or off.

        Usage: /research_with_the_news <topic>
               /research_with_the_news on
               /research_with_the_news off
        The explicit /research_with_the_news <topic> command and the first
        ordinary question after the mode is enabled search the topic directly,
        use the top four news stories, and write a 100-150 word briefing.
        Later ordinary questions are added to the current research context;
        for those, the server asks the research model for one time-neutral
        search question, pulls the top four full articles for it from Google
        News, and writes a 100-150 word briefing.
        Turning the mode on starts a clean context; turning it off returns to
        normal chat.
        """
        topic = " ".join(argument.split())
        state = self.state
        if state.anthem_mode:
            # Switching the knowledge mode must not leave the Anthem provider
            # thread in front of every subsequent question.
            state.anthem_mode = False
            self._reset_anthem_context()
            self._sse("meta", state.snapshot())
            self._sse_text(
                "log",
                "[anthem: mode off because the knowledge mode changed]",
            )
        if state.united_healthcare_mode and topic.casefold() not in {
            "off",
            "stop",
            "exit",
            "disable",
            "disabled",
        }:
            state.united_healthcare_mode = False
            self._reset_united_healthcare_context()
            self._sse("meta", state.snapshot())
            self._sse_text(
                "log",
                "[uhc: mode off because the knowledge mode changed]",
            )
        if not topic:
            self._sse_text(
                "output",
                "\nUsage: /research_with_the_news <topic>\n"
                "       /research_with_the_news on\n"
                "       /research_with_the_news off\n"
                "Researches a topic from recent news and keeps doing so for "
                "each follow-up question.\n",
            )
            return
        normalized = topic.casefold()
        if normalized in {"on", "start", "enable", "enabled"}:
            if state.research_with_news_mode:
                self._sse_text(
                    "output",
                    "\nRESEARCH_WITH_NEWS_MODE is already on; questions are "
                    "answered from online research.\n",
                )
                return
            state.research_with_news_mode = True
            # Starting the mode must not carry ordinary chat context into the
            # research thread.
            self._reset_research_with_the_news_context()
            self._sse("meta", state.snapshot())
            self._sse_text(
                "log",
                "[research_with_the_news: mode on; cleared the previous "
                "conversation context]",
            )
            self._sse_text(
                "output",
                "\nRESEARCH_WITH_NEWS_MODE is on; questions are answered from "
                "online research.\n",
            )
            return
        if normalized in {"off", "stop", "disable", "disabled"}:
            if not state.research_with_news_mode:
                self._sse_text(
                    "output",
                    "\nRESEARCH_WITH_NEWS_MODE is already off; normal chat "
                    "is active.\n",
                )
                return
            state.research_with_news_mode = False
            # Leaving the mode must not leave the research conversation behind
            # as context for ordinary chat. Clear the accumulated turns (and
            # the per-turn caches) so the next normal question starts clean.
            self._reset_research_with_the_news_context()
            self._sse("meta", state.snapshot())
            self._sse_text(
                "log",
                "[research_with_the_news: mode off; cleared the accumulated "
                "research context]",
            )
            self._sse_text(
                "output",
                "\nRESEARCH_WITH_NEWS_MODE is off; normal chat is back.\n",
            )
            return

        was_on = state.research_with_news_mode
        state.research_with_news_mode = True
        self._sse("meta", state.snapshot())
        if not was_on:
            self._sse_text(
                "log",
                "[research_with_the_news: mode on; clearing the previous "
                "conversation context]",
            )
            self._reset_research_with_the_news_context()
        else:
            self._sse_text(
                "log",
                "[research_with_the_news: continuing the accumulated "
                "research context]",
            )
        self._run_research_with_the_news(topic, initial=True)

    def _reset_research_with_the_news_context(self) -> None:
        """Start the news-research mode from a clean conversational context."""
        state = self.state
        state.reset_conversation()
        state.cache.clear()
        state.latest_answer = None
        state.latest_sources = []
        state.latest_benchmark = None
        state.code_language = ""

    def _run_research_with_the_news(
        self, topic: str, initial: bool = False
    ) -> None:
        """Research one question in the current accumulated news context.

        ``initial`` is used for the explicit /research_with_the_news <topic>
        command and for the first ordinary question after the mode is enabled
        (when there is no accumulated conversation yet). That path searches the
        topic directly instead of asking the planner for related questions,
        uses the top four news stories, and writes a 100-150 word briefing.
        Later follow-up questions while the mode is on keep the related-question
        planner, but that planner now returns one question and also uses the
        top four news stories and the 100-150 word briefing.
        """
        topic = " ".join(topic.split())
        if not topic:
            return
        state = self.state
        # Earlier research turns stay in state.conversation. They are used to
        # resolve follow-up questions, but they are never mixed into the
        # factual context: only the freshly fetched sources supply facts.
        accumulated_context = format_conversation(
            state.conversation, state.conversation_summary
        )
        started = time.monotonic()
        log = lambda text: self._sse_text("log", text)
        source = classify_research_with_news_source(
            topic, log, accumulated_context
        )
        general_fact = source == "wikipedia"
        doctor_research = source == "doctor"
        doctor_profile: dict[str, str] = {}
        if general_fact:
            log(
                "[research_with_the_news: general-fact question; restricting "
                "research to en.wikipedia.org]"
            )
        elif doctor_research:
            log(
                "[research_with_the_news: doctor question; identifying the "
                "doctor's hospital and its website]"
            )
            doctor_profile = plan_research_with_doctor_profile(
                topic, log, accumulated_context
            )
            profile_websites = [
                str(value)
                for value in (doctor_profile.get("websites") or [])
                if str(value).strip()
            ]
            if profile_websites:
                log(
                    "[research_with_the_news: restricting searches to the "
                    "hospital website(s) "
                    + ", ".join(repr(value) for value in profile_websites)
                    + "]"
                )
            elif doctor_profile.get("hospital"):
                log(
                    "[research_with_the_news: no hospital website was found "
                    f"for {doctor_profile['hospital']!r}; using the news "
                    "index for the doctor and hospital]"
                )
            else:
                log(
                    "[research_with_the_news: could not identify the "
                    "doctor's hospital; using the news index]"
                )
        if initial:
            log(
                "[research_with_the_news: using /news search with the topic "
                f"{topic!r} directly; no related-question planning]"
            )
            questions = [topic]
        else:
            if accumulated_context:
                log(
                    "[research_with_the_news: planning one time-neutral "
                    f"search question about {topic!r} in the accumulated "
                    "research context]"
                )
            else:
                log(
                    "[research_with_the_news: planning one time-neutral "
                    f"search question about {topic!r}]"
                )
            questions = plan_research_with_news_questions(
                topic, log, accumulated_context=accumulated_context
            )
        if not questions:
            message = (
                f"Could not plan a search for {topic!r}. "
                "Try rephrasing the topic."
            )
            self._sse_text("delta", message)
            state.latest_answer = message
            state.latest_sources = []
            state.latest_benchmark = {
                "model": RESEARCH_WITH_NEWS_MODEL,
                "research_with_the_news": {"questions": [], "articles": 0},
                "total": time.monotonic() - started,
            }
            self._sse("annotations", {"items": []})
            state.record_turn(topic, topic, message)
            return
        for index, question in enumerate(questions, start=1):
            log(
                f"[research_with_the_news: question {index}/"
                f"{len(questions)}: {question!r}]"
            )

        articles: list[dict] = []
        seen_articles: set[str] = set()
        source_noun = {
            "wikipedia": "Wikipedia article",
            "doctor": "doctor article",
        }.get(source, "news article")
        wikipedia_fallback = ""
        per_question_limit = RESEARCH_WITH_NEWS_ARTICLES_PER_QUESTION

        def collect_articles(
            results: list[dict], question: str, noun: str
        ) -> None:
            added = 0
            for article in results:
                key = (
                    str(article.get("link") or "").strip()
                    or str(article.get("title") or "").strip()
                ).casefold()
                if not key or key in seen_articles:
                    continue
                seen_articles.add(key)
                article["question"] = question
                articles.append(article)
                added += 1
                if added >= per_question_limit:
                    break
            log(
                "[research_with_the_news: using "
                f"{added} {noun}(s) for {question!r}]"
            )

        def collect_news_fallback() -> None:
            for question in questions:
                try:
                    results = search_research_with_news(
                        question, limit=per_question_limit
                    )
                except RuntimeError as exc:
                    log(
                        "[research_with_the_news: fallback news search failed "
                        f"for {question!r}: {exc}]"
                    )
                    continue
                collect_articles(results, question, "news article")

        for question in questions:
            try:
                if general_fact:
                    results = search_research_with_wikipedia(
                        question, log=log
                    )
                elif doctor_research:
                    results = search_research_with_doctor(
                        question, doctor_profile, log=log
                    )
                else:
                    results = search_research_with_news(
                        question, limit=per_question_limit
                    )
            except RuntimeError as exc:
                log(
                    "[research_with_the_news: search failed for "
                    f"{question!r}: {exc}]"
                )
                continue
            collect_articles(results, question, source_noun)

        if general_fact and not articles:
            # A general-fact question is routed to Wikipedia first, but
            # Wikipedia does not cover every topic. When no readable article
            # is found, fall back to the same news search path instead of
            # ending the request with "no Wikipedia article".
            log(
                "[research_with_the_news: no readable Wikipedia article was "
                "found; falling back to news search]"
            )
            wikipedia_fallback = "missing"
            general_fact = False
            source = "news"
            source_noun = "news article"
            collect_news_fallback()

        if not articles:
            if wikipedia_fallback == "missing":
                message = (
                    f"No readable Wikipedia article or recent news stories "
                    f"were found for {topic!r}. Try another topic."
                )
            elif general_fact:
                message = (
                    f"No readable Wikipedia article was found for "
                    f"{topic!r}. Try a more specific name or topic."
                )
            elif doctor_research:
                doctor = doctor_profile.get("doctor") or "the doctor"
                hospital = doctor_profile.get("hospital") or "their hospital"
                message = (
                    f"No readable articles about {doctor} were found from "
                    f"{hospital}. Try the doctor's full name or another "
                    "topic."
                )
            else:
                message = (
                    f"No readable recent news stories were found for "
                    f"{topic!r}. Try a broader topic."
                )
            self._sse_text("delta", message)
            state.latest_answer = message
            state.latest_sources = []
            state.latest_benchmark = {
                "model": RESEARCH_WITH_NEWS_MODEL,
                "research_with_the_news": {
                    "questions": questions,
                    "articles": 0,
                },
                "total": time.monotonic() - started,
            }
            self._sse("annotations", {"items": []})
            state.record_turn(topic, topic, message)
            return

        context_started = time.monotonic()
        context = build_research_with_news_context(articles)
        context_seconds = time.monotonic() - context_started
        if (
            general_fact
            and context
            and not research_with_news_context_is_sufficient(
                topic, context, log
            )
        ):
            # A matching Wikipedia article can still be too general or about
            # a different aspect of the subject. Replace the Wikipedia
            # evidence with a news search instead of answering from an
            # article that does not contain the needed facts.
            log(
                "[research_with_the_news: the Wikipedia article does not "
                "contain enough information; falling back to news search]"
            )
            wikipedia_fallback = "insufficient"
            general_fact = False
            source = "news"
            source_noun = "news article"
            articles.clear()
            seen_articles.clear()
            collect_news_fallback()
            context_started = time.monotonic()
            context = build_research_with_news_context(articles)
            context_seconds = time.monotonic() - context_started
        if not context:
            if wikipedia_fallback == "insufficient":
                message = (
                    f"The Wikipedia article for {topic!r} did not contain "
                    "enough information to answer the question, and no "
                    "readable recent news stories were found. Try another "
                    "topic."
                )
            elif general_fact:
                message = (
                    f"The Wikipedia article found for {topic!r} had no "
                    "readable text. Try another topic."
                )
            elif doctor_research:
                message = (
                    f"The articles found for {topic!r} had no readable "
                    "text. Try another doctor or topic."
                )
            else:
                message = (
                    f"The news stories found for {topic!r} had no readable "
                    "text. Try another topic."
                )
            self._sse_text("delta", message)
            state.latest_answer = message
            state.latest_sources = []
            self._sse("annotations", {"items": []})
            state.record_turn(topic, topic, message)
            return

        log(
            "[research_with_the_news: answering from "
            f"{len(articles)} article(s) with {RESEARCH_WITH_NEWS_MODEL}]"
        )
        self._sse(
            "mode",
            {
                "mode": "research_with_the_news",
                "model": RESEARCH_WITH_NEWS_MODEL,
                "think": False,
                "label": {
                    "wikipedia": "wikipedia research",
                    "doctor": "doctor research",
                }.get(source, "news research"),
            },
        )
        if doctor_research:
            doctor = doctor_profile.get("doctor") or "the doctor"
            hospital = doctor_profile.get("hospital") or "their hospital"
            website_articles = sum(
                1
                for article in articles
                if article.get("research_source") == "hospital_website"
            )
            news_articles = len(articles) - website_articles
            profile_websites = [
                str(value)
                for value in (doctor_profile.get("websites") or [])
                if str(value).strip()
            ]
            website_label = ", ".join(profile_websites) or str(
                doctor_profile.get("website") or "no mapped website"
            )
            if website_articles == len(articles):
                detail = (
                    f"answer synthesized from {len(articles)} article(s) "
                    f"gathered from {hospital}'s website "
                    f"({website_label}) for {doctor}"
                )
            elif website_articles:
                detail = (
                    f"answer synthesized from {len(articles)} article(s): "
                    f"{website_articles} from {hospital}'s website "
                    f"({website_label}) and {news_articles} "
                    f"from the news index for {doctor}"
                )
            else:
                detail = (
                    f"answer synthesized from {len(articles)} article(s) "
                    f"about {doctor} and {hospital}; no hospital-website "
                    "article was readable"
                )
        elif general_fact:
            detail = (
                f"answer synthesized from {len(articles)} Wikipedia "
                "article(s) gathered from en.wikipedia.org"
            )
        elif wikipedia_fallback == "insufficient":
            detail = (
                f"answer synthesized from {len(articles)} recent news "
                "article(s) because the Wikipedia article did not contain "
                "enough information"
            )
        elif wikipedia_fallback:
            detail = (
                f"answer synthesized from {len(articles)} recent news "
                "article(s) after no readable Wikipedia article was found"
            )
        elif initial:
            detail = (
                f"answer synthesized from {len(articles)} recent news "
                "article(s) gathered by the direct /news search for the topic"
            )
        else:
            detail = (
                f"answer synthesized from {len(articles)} recent news "
                "article(s) gathered by the generated search question"
            )
        self._sse_answer_source(
            source=SOURCE_OLLAMA,
            label={
                "wikipedia": (
                    f"Wikipedia research with {RESEARCH_WITH_NEWS_MODEL}"
                ),
                "doctor": (
                    f"doctor research with {RESEARCH_WITH_NEWS_MODEL}"
                ),
            }.get(source, f"news research with {RESEARCH_WITH_NEWS_MODEL}"),
            model=RESEARCH_WITH_NEWS_MODEL,
            detail=detail,
        )
        prompt = RESEARCH_WITH_NEWS_ANSWER_PROMPT.format(
            topic=topic,
            context=context,
            accumulated_context=(
                accumulated_context.strip()
                or "(no previous research context)"
            ),
        )
        stats: dict = {}
        try:
            response = chat.stream_answer(
                prompt,
                model=RESEARCH_WITH_NEWS_MODEL,
                metrics=stats,
                emit=lambda text: self._sse_text("delta", text),
            )
        except RuntimeError as exc:
            self._sse("error", {"message": f"News research failed: {exc}"})
            return
        if not response.strip():
            message = "The news research model returned no answer. Please try again."
            self._sse_text("delta", message)
            response = message

        research_sources = []
        for article in articles:
            link = str(article.get("link") or "").strip()
            title = " ".join(str(article.get("title") or "").split())
            if not link and not title:
                continue
            research_sources.append(
                {
                    "title": title or link,
                    "url": link,
                    "source": str(article.get("source") or "").strip(),
                    "published": str(
                        article.get("published") or ""
                    ).strip(),
                    "question": " ".join(
                        str(article.get("question") or "").split()
                    ),
                }
            )
        self._sse("research_sources", {"items": research_sources})

        state.latest_answer = response
        state.latest_sources = []
        state.latest_benchmark = {
            "model": RESEARCH_WITH_NEWS_MODEL,
            "cached": False,
            "retrieval": 0.0,
            "expansion": 0.0,
            "bm25": 0.0,
            "filter": 0.0,
            "candidates": len(articles),
            "trunks": len(articles),
            "context_build": context_seconds,
            "prompt_build": 0.0,
            "context_tokens": len(context) // 4,
            "prefill": stats.get("prompt_eval_duration", 0.0),
            "load": stats.get("load_duration", 0.0),
            "end_to_end_ttft": stats.get("request_ttft", 0.0),
            "prompt_tokens": stats.get("prompt_eval_count", 0),
            "prompt_tokens_cached": stats.get(
                "prompt_eval_cached_count", 0
            ),
            "response_tokens": stats.get("eval_count", 0),
            "total": time.monotonic() - started,
            "research_with_the_news": {
                "questions": questions,
                "articles": len(articles),
                "source": source,
                "doctor": doctor_profile.get("doctor", ""),
                "hospital": doctor_profile.get("hospital", ""),
            },
        }
        self._sse("annotations", {"items": []})
        state.record_turn(topic, topic, response)
        if state.benchmark_mode:
            self._sse_text("log", chat.format_benchmark_line(state.latest_benchmark))


# Cross-component imports are placed after the definitions so a
# feature module can depend on another feature module safely.
from .answer import build_answer_annotations
from .article import _iterative_paragraph_chunks
from .config import SOURCE_OLLAMA
from .conversation import format_conversation
from .news_research import RESEARCH_WITH_NEWS_ANSWER_PROMPT, RESEARCH_WITH_NEWS_ARTICLES_PER_QUESTION, RESEARCH_WITH_NEWS_MODEL, build_research_with_news_context, classify_research_with_news_source, plan_research_with_doctor_profile, plan_research_with_news_questions, research_with_news_context_is_sufficient, search_research_with_doctor, search_research_with_news, search_research_with_wikipedia
from .retrieval import chunk_title, chunk_url
