"""Article, dictionary, code, video, and settings handlers."""

from __future__ import annotations

from .core import *

class HandlerCommandsCMixin:
    def _command_ask(self, argument: str) -> None:
        """Answer a quick, self-contained question without retrieval."""
        question = argument.strip()
        if not question:
            self._sse_text("output", "\nUsage: /ask <question>\n")
            return
        stats: dict = {}
        started = time.monotonic()
        try:
            response = chat.answer_simple_question(
                question,
                metrics=stats,
                emit=lambda text: self._sse_text("delta", text),
            )
        except RuntimeError as exc:
            self._sse_text("output", f"\nQuick answer failed: {exc}\n")
            return
        if response:
            self.state.latest_answer = response
            self.state.latest_sources = []
            stats.update(
                {
                    "cached": False,
                    "model": chat.SIMPLE_ANSWER_MODEL,
                    "retrieval": 0.0,
                    "expansion": 0.0,
                    "bm25": 0.0,
                    "filter": 0.0,
                    "candidates": 0,
                    "trunks": 0,
                    "context_build": 0.0,
                    "prompt_build": 0.0,
                    "context_tokens": 0,
                    "prefill": stats.get("prompt_eval_duration", 0.0),
                    "load": stats.get("load_duration", 0.0),
                    "end_to_end_ttft": stats.get("request_ttft", 0.0),
                    "prompt_tokens": stats.get("prompt_eval_count", 0),
                    "prompt_tokens_cached": stats.get(
                        "prompt_eval_cached_count", 0
                    ),
                    "response_tokens": stats.get("eval_count", 0),
                    "total": time.monotonic() - started,
                }
            )
            self.state.latest_benchmark = stats

    def _command_wiki(self, argument: str) -> None:
        question = argument.strip()
        if not question:
            self._sse_text("output", "\nUsage: /wiki <question>\n")
            return
        response = chat.answer_wiki_question(
            question,
            emit=lambda text: self._sse("delta", {"text": text}),
            log=lambda text: self._sse_text("log", text),
        )
        if response:
            self.state.latest_answer = response
            self.state.latest_sources = []

    def _command_wiki_article(self, argument: str) -> None:
        question = argument.strip()
        if not question:
            self._sse_text("output", "\nUsage: /wiki_article <question>\n")
            return
        response = chat.print_wiki_full_articles(
            question,
            emit=lambda text: self._sse("delta", {"text": text}),
            log=lambda text: self._sse_text("log", text),
        )
        if response:
            self.state.latest_answer = response
            self.state.latest_sources = []

    def _command_article(self, argument: str) -> None:
        """Open an article URL in the /news reader or scope the chat to a file.

        A URL uses the same reader popup as clicking a /news result. A local
        path keeps the original section-aware /article retrieval scope.
        """
        state = self.state
        raw_path = argument.strip()
        simple = bool(
            re.search(r"(?:^|\s)--(?:simple|fast)\b", raw_path)
        )
        iterative = not simple or bool(
            re.search(r"(?:^|\s)--(?:iterative|deep)\b", raw_path)
        )
        raw_path = re.sub(
            r"(?:^|\s)--(?:iterative|deep|simple|fast)\b", " ", raw_path
        ).strip()
        if not raw_path:
            self._sse_text(
                "output",
                "\nUsage: /article <article_url_or_local_path> [--simple]\n"
                "An http(s) URL opens the same article popup as a /news "
                "result, with Learn and Chat available.\n"
                "A local file becomes the only context for follow-up "
                "questions until /youtube transcript is used; the capped "
                "multi-round evidence loop is enabled by default "
                "(--simple or --fast disables it).\n",
            )
            return
        if (
            len(raw_path) >= 2
            and raw_path[0] == raw_path[-1]
            and raw_path[0] in {"'", '"'}
        ):
            raw_path = raw_path[1:-1]
        if re.match(r"^https?://", raw_path, re.IGNORECASE):
            self._open_article_url(raw_path)
            return
        path = Path(raw_path).expanduser()
        if not path.is_absolute():
            path = SCRIPT_DIR / path
        path = path.resolve()
        if not path.is_file():
            self._sse_text(
                "output", f"\nArticle not found: {path}\n"
            )
            return
        try:
            new_chunks, title, structure = _load_article_chunks(path)
        except (OSError, ValueError) as exc:
            self._sse_text(
                "output",
                f"\nCannot use article {path}: {exc}\n",
            )
            return
        # /article replaces the transcript scope for subsequent questions.
        state.transcript = None
        state.replace_chunks(
            new_chunks,
            article_path=str(path),
            article_title=title,
            article_structure=structure,
            article_iterative=iterative,
        )
        state.cache.clear()
        self._sse_text(
            "output",
            "\n"
            + chat.index_memory_report(
                new_chunks, build_index=False, article=str(path)
            )
            + f"\nNow answering from article {str(path)!r} "
            f"({sum(1 for chunk in new_chunks if chunk.index > 0)} "
            "paragraph(s), "
            f"{len(structure.get('sections', []))} section(s)). Existing "
            "conversation context was cleared; follow-up questions stay on "
            "this article."
            + (
                " Iterative evidence mode is enabled.\n"
                if state.article_iterative
                else " Fast single-pass retrieval mode is enabled.\n"
            ),
        )
        self._sse("meta", state.snapshot())

    def _open_article_url(self, url: str) -> None:
        """Populate the /news reader modal for an arbitrary article URL."""
        self._sse_text("log", f"[article: loading {url}]")
        try:
            article = fetch_news_article(url)
        except (
            ValueError,
            OSError,
            subprocess.SubprocessError,
            RuntimeError,
        ) as exc:
            self._sse_text("error", f"\n/article: {exc}\n")
            return
        paragraphs = [
            str(text).strip()
            for text in (article.get("paragraphs") or [])
            if str(text).strip()
        ]
        if not paragraphs:
            self._sse_text(
                "output",
                "\n/article: the page did not contain readable article text.\n",
            )
            return
        images = [
            str(image).strip()
            for image in (article.get("images") or [])
            if str(image).strip()
        ]
        link = str(article.get("link") or url).strip()
        parsed = urlparse(link)
        host = parsed.netloc.lower()
        if host.startswith("www."):
            host = host[4:]
        title = str(article.get("title") or "").strip()
        if not title:
            title = _article_title_from_url(link)
        item = {
            "title": title,
            "link": link,
            "source": host or "Article",
            "section": "",
            "published": "",
            "summary": "",
            "paragraphs": paragraphs,
            "images": images,
            "complete": True,
            "learnable": True,
            "findable": True,
            "learn_noun": "article",
        }
        try:
            news_id = news_id_for_item(item)
        except ValueError:
            news_id = ""
        item["news_id"] = news_id
        item["learned"] = bool(news_id and news_is_learned(news_id))
        self._sse("article", {"item": item})

    def _command_mathtutor(self, argument: str) -> None:
        """Open the Word-test-paper tutor, resuming the latest session."""
        state = self.state
        session = None
        if state.mathtutor_session_id:
            try:
                session = _mathtutor_load_session(state.mathtutor_session_id)
            except ValueError:
                session = None
        if session is None:
            session = _mathtutor_latest_session()
        if session is not None:
            state.mathtutor_session_id = str(session.get("id", ""))
        self._sse(
            "mathtutor",
            {
                "session": (
                    _mathtutor_public_session(session) if session else None
                )
            },
        )
        if session is None:
            self._sse_text(
                "output",
                "\nMath Tutor: upload one or more Microsoft Word test papers "
                "(.docx or .doc) below to generate similar practice problems.\n",
            )
        else:
            self._sse_text(
                "output",
                f"\nMath Tutor: resumed session {session.get('id')} with "
                f"{len(session.get('problems', []))} practice problem(s).\n",
            )

    def _command_dict(self, argument: str) -> None:
        word = argument.strip()
        if not word:
            self._sse_text("output", "\nUsage: /dict <word>\n")
            return
        self._stream_dictionary(word, self.state.latest_answer)

    def _command_dictionary_history(self) -> None:
        entries = chat.dictionary_history_entries()
        if not entries:
            self._sse_text(
                "output",
                "\nNo dictionary history yet. Look up a word with /dict first.\n",
            )
            return
        self._sse(
            "dictionary_history",
            {
                "items": [
                    {
                        "word": entry["word"],
                        "meaning": " ".join(entry["meaning"].split()),
                        "context": " ".join(entry["context"].split()),
                        "timestamp": datetime.fromtimestamp(
                            entry["mtime"]
                        ).strftime("%Y-%m-%d %H:%M"),
                    }
                    for entry in entries
                ]
            },
        )

    def _command_sources(self) -> None:
        sources = self.state.latest_sources
        if not sources:
            self._sse_text(
                "output", "\nNo sources available yet. Ask a question first.\n"
            )
            return
        lines = ["", "Sources for the latest answer:"]
        for index, (chunk, score) in enumerate(sources, start=1):
            label = chunk.label
            if self.state.article_path is not None:
                section = _article_source_section(
                    self.state.article_structure, chunk.index
                )
                if section is not None:
                    heading = str(section.get("heading", ""))
                    if heading:
                        label += f" ({heading})"
            lines.append(f"  [Chunk {index}] {label} (bm25 {score:.4f})")
        self._sse_text("output", "\n".join(lines) + "\n")

    def _command_benchmark(self, argument: str) -> None:
        state = self.state
        value = argument.strip().lower()
        if value in {"", "last", "show"}:
            if state.latest_benchmark is None:
                self._sse_text(
                    "output",
                    "\nNo benchmark data yet. Ask a question first, or use "
                    "/benchmark on to show metrics after each answer.\n"
                    + chat.memory_usage_line()
                    + "\n",
                )
            else:
                self._sse_text(
                    "output",
                    "\n" + chat.format_benchmark_line(state.latest_benchmark) + "\n",
                )
            return
        if value in {"on", "enable", "enabled"}:
            state.benchmark_mode = True
            message = (
                "\nBenchmark display: on (metrics shown after each answer).\n"
            )
            if state.latest_benchmark is not None:
                message += chat.format_benchmark_line(state.latest_benchmark) + "\n"
            else:
                message += chat.memory_usage_line() + "\n"
            self._sse_text("output", message)
            self._sse("meta", state.snapshot())
            return
        if value in {"off", "disable", "disabled"}:
            state.benchmark_mode = False
            self._sse_text("output", "\nBenchmark display: off.\n")
            self._sse("meta", state.snapshot())
            return
        self._sse_text(
            "output",
            "\nUsage: /benchmark [on|off] (no argument shows the latest "
            "answer's metrics)\n",
        )

    def _command_depth(self, argument: str) -> None:
        state = self.state
        value = argument.strip().lower()
        if not value:
            current = "auto" if state.depth is None else str(state.depth)
            self._sse_text(
                "output",
                f"\nRetrieval depth: {current}\n"
                "Usage: /depth <number of trunks> | /depth auto\n"
                f"Default: {chat.DEFAULT_DEPTH} top BM25 matching trunks; "
                "/depth auto enables adaptive retrieval.\n",
            )
            return
        if value in {"auto", "adaptive", "0"}:
            state.depth = None
            state.cache.clear()
            self._sse_text(
                "output",
                "\nRetrieval depth: auto (BM25 score elbow, token budget).\n",
            )
            self._sse("meta", state.snapshot())
            return
        if value in {"default", "reset"}:
            state.depth = chat.DEFAULT_DEPTH
            state.cache.clear()
            self._sse_text(
                "output",
                f"\nRetrieval depth: top {state.depth} BM25 matching trunk(s) "
                "(default).\n",
            )
            self._sse("meta", state.snapshot())
            return
        try:
            new_depth = int(value)
        except ValueError:
            new_depth = 0
        if new_depth < 1:
            self._sse_text(
                "output",
                f"\nInvalid depth: {argument!r}. Use a positive integer, "
                "'auto', or 'default'.\n",
            )
            return
        state.depth = new_depth
        state.cache.clear()
        self._sse_text(
            "output",
            f"\nRetrieval depth: top {new_depth} BM25 matching trunk(s).\n",
        )
        self._sse("meta", state.snapshot())

    def _command_settings(self, argument: str) -> None:
        """Get or set English Corner article highlighting options."""
        state = self.state
        parts = argument.split(None, 1)
        if not parts:
            current = state.english_words_to_learn or "off"
            label = state.english_words_label
            if (
                current
                and current.lower() not in ENGLISH_CORNER_DISABLED_VALUES
                and not label
            ):
                try:
                    _vocabulary, label = english_corner_vocabulary(current)
                    state.english_words_label = label
                except (ValueError, RuntimeError, OSError):
                    label = ""
            self._sse_text(
                "output",
                "\nEnglish Corner settings:\n"
                f"  english_words_to_learn = {current}"
                + (f" ({label})" if label else "")
                + "\n\nUsage: /settings english_words_to_learn <range>\n"
                "  <range> is 3k, 5k, 10k, 20k, 30k, 50k, 100k, or a range\n"
                "  such as 3k..50k / 20k..30k / 30k..50k. The default is "
                f"{ENGLISH_CORNER_DEFAULT_RANGE}.\n"
                "  Use 'off' to stop highlighting.\n",
            )
            return

        key = parts[0].strip().lower().replace("-", "_")
        if key != "english_words_to_learn":
            self._sse_text(
                "output",
                f"\nUnknown setting: {parts[0]!r}. "
                "The only setting is english_words_to_learn.\n",
            )
            return

        spec = " ".join(parts[1].split()) if len(parts) > 1 else ""
        if not spec:
            spec = ENGLISH_CORNER_DEFAULT_RANGE
        normalized = spec.strip().lower()

        if normalized in ENGLISH_CORNER_DISABLED_VALUES:
            state.english_words_to_learn = ""
            state.english_words_label = ""
            settings = _load_english_corner_settings()
            settings["english_words_to_learn"] = ""
            _save_english_corner_settings(settings)
            self._sse_text(
                "output",
                "\nEnglish Corner highlighting: off.\n",
            )
            self._sse("meta", state.snapshot())
            return

        try:
            vocabulary, label = english_corner_vocabulary(normalized)
        except (ValueError, RuntimeError, OSError) as exc:
            self._sse_text(
                "output",
                f"\nCould not set english_words_to_learn: {exc}\n"
                "Use a corpus name (3k, 5k, 10k, 20k, 30k, 50k, 100k) "
                "or a range such as 3k..50k.\n",
            )
            return
        if not vocabulary:
            self._sse_text(
                "output",
                "\nThat range contains no words.\n",
            )
            return

        state.english_words_to_learn = normalized
        state.english_words_label = label
        settings = _load_english_corner_settings()
        settings["english_words_to_learn"] = normalized
        _save_english_corner_settings(settings)
        self._sse_text(
            "output",
            "\nEnglish Corner highlighting: on.\n"
            f"  {label}\n"
            f"  {len(vocabulary):,} words will be highlighted in articles.\n",
        )
        self._sse("meta", state.snapshot())

    # ---- SSE: answer translation -------------------------------------
    def _handle_translate(self, text: str) -> None:
        """Translate a finished answer into Simplified Chinese paragraph by paragraph."""
        self._begin_sse()
        try:
            if len(text) > MAX_TRANSLATION_CHARS:
                raise RuntimeError(
                    f"Answer is too long to translate (limit "
                    f"{MAX_TRANSLATION_CHARS:,} characters)."
                )
            paragraphs = split_translation_paragraphs(text)
            if not paragraphs:
                raise RuntimeError("There is no answer text to translate.")

            self._sse(
                "start",
                {
                    "model": ANSWER_TRANSLATION_MODEL,
                    "paragraphs": len(paragraphs),
                },
            )
            for index, paragraph in enumerate(paragraphs):
                if index:
                    self._sse_text("delta", "\n\n")
                self._stream_translation_paragraph(paragraph)
                self._sse(
                    "paragraph",
                    {"index": index + 1, "total": len(paragraphs)},
                )
            self._sse("done", {"paragraphs": len(paragraphs)})
        except ClientDisconnected:
            pass
        except Exception as exc:
            try:
                self._sse("error", {"message": f"Translation failed: {exc}"})
                self._sse("done", {})
            except ClientDisconnected:
                pass

    # ---- SSE: AI news podcast ---------------------------------------
    def _handle_ai_news_podcast(self, payload: dict) -> None:
        """Draft a 2000-word host/guest podcast from current news stories.

        The browser sends the five stories from /ai_news_podcast. Missing
        article bodies are fetched on demand here so the script is grounded in
        the full news content, not just the feed headlines. The script streams
        back as ``delta`` events; ``start`` carries the two male voices the
        browser should use with /api/tts.
        """
        self._begin_sse()
        try:
            stories = self._ai_news_podcast_stories(payload)
            if not stories:
                raise RuntimeError(
                    "No article content was available for the podcast."
                )
            source = self._ai_news_podcast_source(stories)
            voices = _podcast_voice_defaults()
            self._sse(
                "start",
                {
                    "model": AI_NEWS_PODCAST_MODEL,
                    "voices": voices,
                    "stories": len(stories),
                    "target_words": 2000,
                },
            )
            prompt = AI_NEWS_PODCAST_PROMPT.format(source=source)
            request = {
                "model": AI_NEWS_PODCAST_MODEL,
                "prompt": prompt,
                "stream": True,
                "think": False,
                "keep_alive": chat.OLLAMA_KEEP_ALIVE,
                "options": {
                    "temperature": 0.55,
                    "num_ctx": AI_NEWS_PODCAST_NUM_CTX,
                    "num_predict": AI_NEWS_PODCAST_NUM_PREDICT,
                },
            }
            text_parts: list[str] = []
            try:
                with requests.post(
                    f"{chat.OLLAMA_HOST}/api/generate",
                    json=request,
                    stream=True,
                    timeout=(10, 1800),
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
                            raise RuntimeError(
                                f"Ollama error: {event['error']}"
                            )
                        token = event.get("response", "")
                        if token:
                            text_parts.append(token)
                            self._sse_text("delta", token)
                        if event.get("done"):
                            break
            except requests.RequestException as exc:
                raise RuntimeError(
                    f"Could not reach Ollama at {chat.OLLAMA_HOST}. "
                    f"Is it running? ({exc})"
                ) from exc
            script = "".join(text_parts).strip()
            if not script:
                raise RuntimeError(
                    "The podcast model returned an empty script."
                )
            self._sse(
                "done",
                {
                    "stories": len(stories),
                    "words": len(script.split()),
                },
            )
        except ClientDisconnected:
            pass
        except Exception as exc:
            try:
                self._sse("error", {"message": f"Podcast failed: {exc}"})
                self._sse("done", {})
            except ClientDisconnected:
                pass

    def _ai_news_podcast_stories(self, payload: dict) -> list[dict]:
        """Normalize the browser's story list and fill in missing bodies."""
        raw_stories = payload.get("stories")
        if not isinstance(raw_stories, list) or not raw_stories:
            raw_stories = [payload]
        stories: list[dict] = []
        for raw in raw_stories[:AI_NEWS_PODCAST_LIMIT]:
            if not isinstance(raw, dict):
                continue
            title = str(raw.get("title") or "").strip()
            link = str(raw.get("link") or "").strip()
            summary = str(raw.get("summary") or "").strip()
            paragraphs = [
                str(value).strip()
                for value in (raw.get("paragraphs") or [])
                if str(value).strip()
            ]
            if not paragraphs and re.match(
                r"^https?://", link, re.IGNORECASE
            ):
                try:
                    article = fetch_news_article(link, title)
                except (RuntimeError, ValueError, OSError):
                    article = {}
                paragraphs = list(article.get("paragraphs") or [])
                link = str(article.get("link") or link).strip()
            if not paragraphs and summary:
                paragraphs = [summary]
            if not title and not paragraphs:
                continue
            stories.append(
                {
                    "title": title or "Untitled story",
                    "source": str(raw.get("source") or "").strip(),
                    "link": link,
                    "summary": summary,
                    "paragraphs": paragraphs,
                }
            )
        return stories

    def _ai_news_podcast_source(self, stories: list[dict]) -> str:
        """Build the bounded source block that grounds the podcast script."""
        blocks: list[str] = []
        for index, story in enumerate(stories, start=1):
            lines = [
                f"STORY {index}",
                f"Title: {story['title']}",
            ]
            if story.get("source"):
                lines.append(f"Source: {story['source']}")
            if story.get("summary"):
                lines.append(f"Summary: {story['summary']}")
            if story.get("link"):
                lines.append(f"Link: {story['link']}")
            lines.append("Article:")
            lines.extend(story.get("paragraphs") or [])
            blocks.append("\n".join(lines))
        return "\n\n".join(blocks)[:AI_NEWS_PODCAST_SOURCE_CHARS]

    def _stream_translation_paragraph(self, paragraph: str) -> None:
        """Stream one paragraph's Simplified Chinese translation."""
        payload = {
            "model": ANSWER_TRANSLATION_MODEL,
            "prompt": chat.ANSWER_TRANSLATION_PROMPT.format(text=paragraph),
            "stream": True,
            "think": False,
            "keep_alive": chat.OLLAMA_KEEP_ALIVE,
            "options": {
                "temperature": 0.1,
                "num_ctx": ANSWER_TRANSLATION_NUM_CTX,
            },
        }
        prefix_filter = TranslationPrefixFilter()
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
                        raise RuntimeError(
                            f"Ollama error: {event['error']}"
                        )
                    token = event.get("response", "")
                    if token:
                        cleaned = prefix_filter.feed(token)
                        if cleaned:
                            self._sse_text("delta", cleaned)
                    if event.get("done"):
                        break
        except requests.RequestException as exc:
            raise RuntimeError(
                f"Could not reach Ollama at {chat.OLLAMA_HOST}. "
                f"Is it running? ({exc})"
            ) from exc

        trailing = prefix_filter.flush()
        if trailing:
            self._sse_text("delta", trailing)

    # ---- SSE: simple-English article rewrite --------------------------
    def _handle_simplify(self, text: str) -> None:
        """Rewrite an article in simple English paragraph by paragraph."""
        self._begin_sse()
        try:
            if len(text) > MAX_SIMPLIFY_CHARS:
                raise RuntimeError(
                    f"Article is too long to simplify (limit "
                    f"{MAX_SIMPLIFY_CHARS:,} characters)."
                )
            paragraphs = split_simplify_paragraphs(text)
            if not paragraphs:
                raise RuntimeError("There is no article text to simplify.")
            self._sse(
                "start",
                {"model": SIMPLIFY_MODEL, "paragraphs": len(paragraphs)},
            )
            for index, paragraph in enumerate(paragraphs):
                if index:
                    self._sse_text("delta", "\n\n")
                self._stream_simplify_paragraph(paragraph)
                self._sse(
                    "paragraph",
                    {"index": index + 1, "total": len(paragraphs)},
                )
            self._sse("done", {"paragraphs": len(paragraphs)})
        except ClientDisconnected:
            pass
        except Exception as exc:
            try:
                self._sse("error", {"message": f"Simplification failed: {exc}"})
                self._sse("done", {})
            except ClientDisconnected:
                pass

    def _stream_simplify_paragraph(self, paragraph: str) -> None:
        """Stream one paragraph's simple-English rewrite."""
        payload = {
            "model": SIMPLIFY_MODEL,
            "prompt": SIMPLIFY_PROMPT.format(text=paragraph),
            "stream": True,
            "think": False,
            "keep_alive": chat.OLLAMA_KEEP_ALIVE,
            "options": {
                "temperature": 0.2,
                "num_ctx": SIMPLIFY_NUM_CTX,
            },
        }
        prefix_filter = SimplificationPrefixFilter()
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
                        cleaned = prefix_filter.feed(token)
                        if cleaned:
                            self._sse_text("delta", cleaned)
                    if event.get("done"):
                        break
        except requests.RequestException as exc:
            raise RuntimeError(
                f"Could not reach Ollama at {chat.OLLAMA_HOST}. "
                f"Is it running? ({exc})"
            ) from exc

        trailing = prefix_filter.flush()
        if trailing:
            self._sse_text("delta", trailing)

    # ---- SSE: single code-line explanation ---------------------------
    def _handle_explain_line(self, payload: dict) -> None:
        """Explain one line of a Go, Rust, or PHP code snippet."""
        language = normalize_language(
            str(payload.get("language") or self.state.code_language or "")
        )
        code = payload.get("code")
        try:
            line_number = int(payload.get("line") or 0)
        except (TypeError, ValueError):
            line_number = 0

        if not language_uses_official_docs(language):
            self._send_error_json(
                "Line explanations are available for Go, Rust, and PHP.",
                400,
            )
            return
        if not isinstance(code, str) or not code.strip():
            self._send_error_json("Missing code snippet", 400)
            return
        if len(code) > MAX_LINE_EXPLAIN_CODE_CHARS:
            self._send_error_json(
                f"Code snippet is too long to explain "
                f"(limit {MAX_LINE_EXPLAIN_CODE_CHARS:,} characters).",
                413,
            )
            return
        code_lines = code.splitlines()
        if line_number < 1 or line_number > len(code_lines):
            self._send_error_json(
                f"Line {line_number} is outside the code snippet "
                f"(1-{len(code_lines)}).",
                400,
            )
            return

        fence = language_code_fence(language) or "text"
        model = language_coding_model(language)
        self._begin_sse()
        try:
            self._sse(
                "start",
                {
                    "model": model,
                    "language": language,
                    "line": line_number,
                },
            )
            self._stream_line_explanation(
                code_lines, line_number, language, fence, model
            )
            self._sse("done", {"line": line_number})
        except ClientDisconnected:
            pass
        except Exception as exc:
            try:
                self._sse("error", {"message": f"Line explanation failed: {exc}"})
                self._sse("done", {})
            except ClientDisconnected:
                pass

    def _stream_line_explanation(
        self,
        code_lines: list[str],
        line_number: int,
        language: str,
        fence: str,
        model: str,
    ) -> None:
        numbered_code = "\n".join(
            f"{index:>4} | {line}"
            for index, line in enumerate(code_lines, start=1)
        )
        prompt = LINE_EXPLAIN_PROMPT.format(
            language=language,
            fence=fence,
            numbered_code=numbered_code,
            line_number=line_number,
            line=code_lines[line_number - 1],
        )
        payload = {
            "model": model,
            "prompt": prompt,
            "stream": True,
            "think": False,
            "keep_alive": chat.OLLAMA_KEEP_ALIVE,
            "options": {
                "temperature": 0.2,
                "num_ctx": LINE_EXPLAIN_NUM_CTX,
            },
        }
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
                        self._sse_text("delta", token)
                    if event.get("done"):
                        break
        except requests.RequestException as exc:
            raise RuntimeError(
                f"Could not reach Ollama at {chat.OLLAMA_HOST}. "
                f"Is it running? ({exc})"
            ) from exc

    # ---- SSE: ask a question about a whole code snippet -----------------
    def _handle_snippet_question(self, payload: dict) -> None:
        """Answer a free-form question about one code snippet."""
        code = payload.get("code")
        question = str(payload.get("question") or "").strip()
        raw_language = str(
            payload.get("language") or self.state.code_language or ""
        ).strip()

        if not question:
            self._send_error_json("Missing question", 400)
            return
        if not isinstance(code, str) or not code.strip():
            self._send_error_json("Missing code snippet", 400)
            return
        if len(code) > MAX_SNIPPET_QUESTION_CODE_CHARS:
            self._send_error_json(
                f"Code snippet is too long to ask about "
                f"(limit {MAX_SNIPPET_QUESTION_CODE_CHARS:,} characters).",
                413,
            )
            return

        unsupported = ("", "default", "text", "plaintext")
        language = (
            "programming"
            if raw_language.lower() in unsupported
            else raw_language
        )
        fence = re.sub(r"[^A-Za-z0-9_+.#-]", "", raw_language) or "text"
        model = CODE_GENERATION_MODEL

        self._begin_sse()
        try:
            self._sse(
                "start",
                {"model": model, "language": language},
            )
            self._stream_snippet_question(
                code, question, language, fence, model
            )
            self._sse("done", {})
        except ClientDisconnected:
            pass
        except Exception as exc:
            try:
                self._sse(
                    "error",
                    {"message": f"Snippet answer failed: {exc}"},
                )
                self._sse("done", {})
            except ClientDisconnected:
                pass

    def _stream_snippet_question(
        self,
        code: str,
        question: str,
        language: str,
        fence: str,
        model: str,
    ) -> None:
        prompt = SNIPPET_QUESTION_PROMPT.format(
            language=language,
            fence=fence,
            code=code,
            question=question,
        )
        payload = {
            "model": model,
            "prompt": prompt,
            "stream": True,
            "think": False,
            "keep_alive": chat.OLLAMA_KEEP_ALIVE,
            "options": {
                "temperature": 0.2,
                "num_ctx": SNIPPET_QUESTION_NUM_CTX,
            },
        }
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
                        self._sse_text("delta", token)
                    if event.get("done"):
                        break
        except requests.RequestException as exc:
            raise RuntimeError(
                f"Could not reach Ollama at {chat.OLLAMA_HOST}. "
                f"Is it running? ({exc})"
            ) from exc

    # ---- SSE: YouTube video summary ----------------------------------
    def _handle_video_summary(self, payload: dict) -> None:
        """Fetch one video's transcript and stream an AI summary."""
        video_id = str(payload.get("id") or "").strip()
        if not re.fullmatch(r"[A-Za-z0-9_-]{11}", video_id):
            self._send_error_json("Missing or invalid YouTube video id", 400)
            return

        title = str(payload.get("title") or "").strip() or video_id
        channel = str(payload.get("channel") or "").strip()
        published = str(payload.get("published") or "").strip()
        url = str(payload.get("url") or "").strip() or (
            f"https://www.youtube.com/watch?v={video_id}"
        )
        try:
            duration = int(payload.get("duration") or 0)
        except (TypeError, ValueError):
            duration = 0

        self._begin_sse()
        try:
            self._sse(
                "start",
                {
                    "id": video_id,
                    "title": title,
                    "channel": channel,
                    "published": published,
                    "duration": duration,
                    "url": url,
                },
            )
            # Import lazily so a missing requests dependency only breaks
            # this endpoint, not the whole chat server.
            try:
                import fetch_youtube_transcript as transcript_module
            except ImportError as exc:
                raise RuntimeError(
                    "the third-party transcript module is required for video "
                    f"summaries ({exc})"
                ) from exc

            transcript = transcript_module.fetch_transcript(video_id)
            if not transcript.strip():
                raise RuntimeError("the video has an empty transcript")
            self._stream_video_summary(
                title=title,
                channel=channel,
                published=published,
                duration=duration,
                url=url,
                transcript=transcript,
            )
            self._sse("done", {"id": video_id})
        except ClientDisconnected:
            pass
        except Exception as exc:
            try:
                self._sse("error", {"message": f"Video summary failed: {exc}"})
                self._sse("done", {"id": video_id})
            except ClientDisconnected:
                pass

    def _stream_video_summary(
        self,
        *,
        title: str,
        channel: str,
        published: str,
        duration: int,
        url: str,
        transcript: str,
    ) -> None:
        """Stream the model's summary of one YouTube transcript."""
        transcript = transcript.strip()
        if len(transcript) > MAX_VIDEO_SUMMARY_TRANSCRIPT_CHARS:
            transcript = transcript[:MAX_VIDEO_SUMMARY_TRANSCRIPT_CHARS].rsplit(
                " ", 1
            )[0]
            transcript += "\n\n[Transcript truncated because of its length.]"

        prompt = VIDEO_SUMMARY_PROMPT.format(
            title=title,
            channel=channel or "unknown",
            published=published or "unknown",
            duration=(
                f"{duration // 60}:{duration % 60:02d}" if duration > 0 else "unknown"
            ),
            url=url,
            transcript=transcript,
        )
        payload = {
            "model": VIDEO_SUMMARY_MODEL,
            "prompt": prompt,
            "stream": True,
            "think": False,
            "keep_alive": chat.OLLAMA_KEEP_ALIVE,
            "options": {
                "temperature": 0.2,
                "num_ctx": VIDEO_SUMMARY_NUM_CTX,
            },
        }
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
                        self._sse_text("delta", token)
                    if event.get("done"):
                        break
        except requests.RequestException as exc:
            raise RuntimeError(
                f"Could not reach Ollama at {chat.OLLAMA_HOST}. "
                f"Is it running? ({exc})"
            ) from exc

    # ---- SSE: dictionary popup ---------------------------------------
    def _handle_dict(self, word: str, answer: str | None) -> None:
        self._begin_sse()
        try:
            if not word:
                self._sse_text("delta", "Usage: /dict <word>\n")
            else:
                context = answer if answer is not None else self.state.latest_answer
                self._stream_dictionary(word, context)
            self._sse("done", {"word": word})
        except ClientDisconnected:
            pass
        except Exception as exc:
            try:
                self._sse("error", {"message": str(exc)})
                self._sse("done", {"word": word})
            except ClientDisconnected:
                pass

    def _stream_dictionary(self, word: str, latest_answer: str | None) -> None:
        # The popup shows exactly: word, IPA, part of speech, synonym, a
        # COBUILD-style meaning, the surrounding context, and two sample
        # sentences. Keep the prompt and the streamed output limited to those
        # fields (no quote, model banner, or follow-up usage section).
        context = chat.dictionary_surrounding_context(word, latest_answer)
        prompt = chat.DICT_POPUP_PROMPT.format(
            word=word,
            context=(context or "(no previous answer available)"),
        )
        try:
            chunks: list[str] = []

            def emit(text: str) -> None:
                chunks.append(text)
                self._sse("delta", {"text": text})

            answer = chat.stream_answer(
                prompt,
                model=chat.DICT_MODEL,
                emit=emit,
                think=False,
            )
            try:
                chat.record_dictionary_history(
                    word, context, answer or "".join(chunks)
                )
            except OSError as exc:
                self._sse(
                    "log",
                    f"[dictionary] could not save history: {exc}",
                )
        except RuntimeError as exc:
            self._sse("error", {"message": f"Dictionary lookup failed: {exc}"})


# Cross-component imports are placed after the definitions so a
# feature module can depend on another feature module safely.
from .article import _article_source_section, _load_article_chunks
from .code import LINE_EXPLAIN_NUM_CTX, LINE_EXPLAIN_PROMPT, MAX_LINE_EXPLAIN_CODE_CHARS, MAX_SNIPPET_QUESTION_CODE_CHARS, SNIPPET_QUESTION_NUM_CTX, SNIPPET_QUESTION_PROMPT
from .config import CODE_GENERATION_MODEL
from .core import SCRIPT_DIR, _podcast_voice_defaults
from .english_corner import ENGLISH_CORNER_DEFAULT_RANGE, ENGLISH_CORNER_DISABLED_VALUES, _load_english_corner_settings, _save_english_corner_settings, english_corner_vocabulary
from .mathtutor import _mathtutor_latest_session, _mathtutor_load_session, _mathtutor_public_session
from .news import _article_title_from_url, fetch_news_article
from .podcast import AI_NEWS_PODCAST_LIMIT, AI_NEWS_PODCAST_MODEL, AI_NEWS_PODCAST_NUM_CTX, AI_NEWS_PODCAST_NUM_PREDICT, AI_NEWS_PODCAST_PROMPT, AI_NEWS_PODCAST_SOURCE_CHARS
from .programming import language_code_fence, language_coding_model, language_uses_official_docs, normalize_language
from .programming_docs import split_translation_paragraphs
from .retrieval import ClientDisconnected, news_id_for_item, news_is_learned
from .simplify import MAX_SIMPLIFY_CHARS, SIMPLIFY_MODEL, SIMPLIFY_NUM_CTX, SIMPLIFY_PROMPT, SimplificationPrefixFilter, split_simplify_paragraphs
from .translation import ANSWER_TRANSLATION_MODEL, ANSWER_TRANSLATION_NUM_CTX, MAX_TRANSLATION_CHARS, TranslationPrefixFilter
from .video import MAX_VIDEO_SUMMARY_TRANSCRIPT_CHARS, VIDEO_SUMMARY_MODEL, VIDEO_SUMMARY_NUM_CTX, VIDEO_SUMMARY_PROMPT
