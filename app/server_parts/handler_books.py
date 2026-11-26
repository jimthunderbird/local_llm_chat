"""Book writing, book listing, and PDF request handling."""

from __future__ import annotations

from .core import *

class HandlerBooksMixin:
    def _command_write_a_book(self, subject: str) -> None:
        """Research a subject and write a chapter-by-chapter HTML book.

        The keyword, outline, and chapter prompts all run on the local
        book-writing model. Google News supplies only articles published in
        the last two years; their full bodies become the evidence for the
        chapters. The finished, self-contained HTML book is saved under
        ./.chat/books and emitted as a `written_book` event with a browser URL.
        """
        options = _parse_write_a_book_options(subject)
        subject = options.subject
        words_per_chapter = options.words_per_chapter
        chapter_count = options.chapter_count
        # Research breadth now comes solely from the planning settings: every
        # sub-question contributes up to its own article limit. There is no
        # separate articles-to-learn budget any more.
        research_budget = (
            options.sub_question_count
            * options.sub_question_article_limit
        )
        book_model = options.model
        book_directory = options.directory
        if not subject:
            self._sse_text(
                "output",
                "\nUsage: /write_a_book <subject> [--words N] [--chapters N] "
                "[--sub-questions N] "
                "[--sub-question-articles N] [--model MODEL] "
                "[--directory DIR]\n"
                "Defaults: 800 words per chapter, 60 chapters, 10 planning "
                "sub-questions, 10 articles per sub-question, the configured "
                "book model, and .chat/books.\n"
                "Every setting has no maximum.\n"
                "Example: /write_a_book the future of open source AI\n",
            )
            return

        started = time.monotonic()
        self._sse_text(
            "log",
            f"[book: planning {subject!r} with {book_model} "
            f"({chapter_count} chapter(s), about {words_per_chapter} words "
            f"each, learn up to {research_budget} article(s) across "
            f"up to {options.sub_question_count} sub-question(s), "
            f"up to {options.sub_question_article_limit} article(s) per "
            f"sub-question; saving to {book_directory})]",
        )
        try:
            keyword_reply = chat.generate_text(
                WRITE_A_BOOK_KEYWORD_PROMPT.format(
                    subject=subject,
                    limit=options.sub_question_count,
                ),
                model=book_model,
                temperature=0.4,
                num_predict=260,
            )
        except RuntimeError as exc:
            self._sse_text(
                "log",
                "[book: keyword planning failed (" + str(exc)
                + "); using the subject as the only search keyword]",
            )
            keyword_reply = ""
        keywords = _book_keyword_list(
            keyword_reply, subject, options.sub_question_count
        )
        self._sse_text(
            "log", "[book: search keywords: " + " | ".join(keywords) + "]"
        )
        research_deadline = (
            time.monotonic() + WRITE_A_BOOK_RESEARCH_BUDGET_SECONDS
        )
        self._sse_text(
            "log",
            "[book: research time budget: "
            f"{WRITE_A_BOOK_RESEARCH_BUDGET_SECONDS}s; "
            f"{WRITE_A_BOOK_RESEARCH_WORKERS} article fetch(es) at a time]",
        )

        candidates: list[dict] = []
        try:
            candidates = _book_search_candidates(
                keywords,
                research_budget,
                lambda text: self._sse_text("log", text),
                keyword_limit=options.sub_question_count,
                per_question_limit=(
                    options.sub_question_article_limit
                ),
                timeout=WRITE_A_BOOK_SEARCH_TIMEOUT_SECONDS,
                deadline=research_deadline,
            )
        except RuntimeError as exc:
            self._sse_text(
                "log",
                f"[book: recent-news search failed ({exc}); writing from "
                "the model's general knowledge]",
            )
            candidates = []
        if not candidates and subject.casefold() not in {
            keyword.casefold() for keyword in keywords
        }:
            self._sse_text(
                "log",
                "[book: no usable keyword results; retrying with the "
                "subject]",
            )
            try:
                candidates = _book_search_candidates(
                    [subject],
                    research_budget,
                    lambda text: self._sse_text("log", text),
                    keyword_limit=1,
                    per_question_limit=(
                        options.sub_question_article_limit
                    ),
                    timeout=WRITE_A_BOOK_SEARCH_TIMEOUT_SECONDS,
                    deadline=research_deadline,
                )
            except RuntimeError as exc:
                self._sse_text(
                    "log", f"[book: subject search failed: {exc}]"
                )

        if candidates:
            self._sse_text(
                "log",
                f"[book: found {len(candidates)} recent article(s); reading "
                "their full text]",
            )
        else:
            self._sse_text(
                "log",
                "[book: no articles from the last 2 years were found; writing "
                "from the model's general knowledge]",
            )

        sources: list[dict] = []
        if candidates:
            def load_source(index: int, item: dict) -> dict | None:
                title = str(item.get("title") or "Untitled")
                link = str(item.get("link") or "").strip()
                self._sse_text(
                    "log",
                    f"[book: reading source {index}/{len(candidates)}: "
                    f"{title} ({link or 'no URL'})]",
                )
                try:
                    return _book_load_source(
                        item,
                        lambda text: self._sse_text("log", text),
                        fetch_timeout=WRITE_A_BOOK_ARTICLE_TIMEOUT_SECONDS,
                        deadline=research_deadline,
                    )
                except ClientDisconnected:
                    raise
                except (OSError, RuntimeError, ValueError) as exc:
                    self._sse_text(
                        "log",
                        f"[book: could not read source {index} "
                        f"({title}): {exc}]",
                    )
                    return None

            workers = min(
                WRITE_A_BOOK_RESEARCH_WORKERS, len(candidates)
            )
            with ThreadPoolExecutor(max_workers=workers) as executor:
                for source in executor.map(
                    load_source,
                    range(1, len(candidates) + 1),
                    candidates,
                ):
                    if source is not None:
                        sources.append(source)
        if sources:
            self._sse_text(
                "log",
                f"[book: learned {len(sources)} article(s) and "
                f"{sum(len(source['paragraphs']) for source in sources)} "
                "paragraph(s)]",
            )
        elif candidates:
            self._sse_text(
                "log",
                "[book: no article body could be read; writing from the "
                "model's general knowledge]",
            )

        digest = _book_source_digest(sources, WRITE_A_BOOK_DIGEST_CHARS)
        if not digest:
            digest = (
                "No recent source text was available. Plan a useful general "
                f"book about {subject} without inventing specific facts."
            )
        self._sse_text("log", "[book: planning the chapter outline]")
        try:
            outline_reply = chat.generate_text(
                WRITE_A_BOOK_OUTLINE_PROMPT.format(
                    subject=subject,
                    digest=digest,
                    chapters=chapter_count,
                ),
                model=book_model,
                temperature=0.45,
                num_predict=360,
            )
        except RuntimeError as exc:
            self._sse_text(
                "log", f"[book: outline planning failed ({exc}); using a "
                "default outline]"
            )
            outline_reply = ""
        chapter_titles = _book_chapter_titles(
            outline_reply, subject, chapter_count
        )
        self._sse_text(
            "log",
            f"[book: outline has {len(chapter_titles)} chapter(s)]",
        )

        chapters: list[tuple[str, str]] = []
        reference_map = _book_reference_map(
            sources, WRITE_A_BOOK_REFERENCE_MAP_CHARS
        )
        for index, chapter_title in enumerate(chapter_titles, 1):
            self._sse_text(
                "delta", f"\n\n## Chapter {index}: {chapter_title}\n\n"
            )
            if sources:
                evidence = _book_chapter_evidence(
                    sources,
                    subject,
                    chapter_title,
                    WRITE_A_BOOK_EVIDENCE_CHARS,
                )
            else:
                evidence = (
                    "No recent source text was available. Use general "
                    "knowledge, and avoid specific claims that cannot be "
                    "verified."
                )
                reference_map = (
                    "No numbered research references were available."
                )
            prompt = WRITE_A_BOOK_CHAPTER_PROMPT.format(
                subject=subject,
                number=index,
                title=chapter_title,
                target_words=max(80, int(words_per_chapter * 0.95)),
                max_words=words_per_chapter,
                references=reference_map,
                evidence=evidence,
            )
            try:
                chapter_text = chat.stream_answer(
                    prompt,
                    model=book_model,
                    emit=lambda text: self._sse_text("delta", text),
                )
            except RuntimeError as exc:
                self._sse_text(
                    "log",
                    f"[book: chapter {index} failed with research "
                    f"excerpts: {exc}]",
                )
                if not sources:
                    if not chapters:
                        self._sse_text(
                            "output",
                            "\n/write_a_book: could not write the book: "
                            f"{exc}\n",
                        )
                    break
                self._sse_text(
                    "log",
                    f"[book: retrying chapter {index} from general "
                    "knowledge]",
                )
                fallback_prompt = WRITE_A_BOOK_CHAPTER_PROMPT.format(
                    subject=subject,
                    number=index,
                    title=chapter_title,
                    target_words=max(80, int(words_per_chapter * 0.95)),
                    max_words=words_per_chapter,
                    references=(
                        "No numbered research references were available."
                    ),
                    evidence=(
                        "No recent source text was available. Use general "
                        "knowledge, and avoid specific claims that cannot "
                        "be verified."
                    ),
                )
                try:
                    chapter_text = chat.stream_answer(
                        fallback_prompt,
                        model=book_model,
                        emit=lambda text: self._sse_text("delta", text),
                    )
                except RuntimeError as retry_exc:
                    self._sse_text(
                        "log",
                        f"[book: chapter {index} retry failed: "
                        f"{retry_exc}]",
                    )
                    if not chapters:
                        self._sse_text(
                            "output",
                            "\n/write_a_book: could not write the book: "
                            f"{retry_exc}\n",
                        )
                    break
            chapter_text = _book_strip_chapter_heading(
                chapter_text, chapter_title
            )
            chapter_text = _book_limit_words(
                chapter_text, words_per_chapter
            )
            if not chapter_text.strip():
                self._sse_text(
                    "log",
                    f"[book: chapter {index} was empty; skipping it]",
                )
                continue
            chapters.append((chapter_title, chapter_text))
            self._sse_text(
                "log",
                f"[book: chapter {index}/{len(chapter_titles)} written "
                f"({len(chapter_text.split()):,} words)]",
            )

        if not chapters:
            self._sse_text(
                "output", "\n/write_a_book: no chapter was produced.\n"
            )
            return

        created = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
        book_html = _book_html_document(
            subject, chapters, sources, created
        )
        filename = _book_filename(subject)
        path = book_directory / filename
        try:
            book_directory.mkdir(parents=True, exist_ok=True)
            path.write_text(book_html, encoding="utf-8")
        except OSError as exc:
            self._sse_text(
                "output",
                f"\n/write_a_book: could not save the book: {exc}\n",
            )
            return

        total_words = sum(len(text.split()) for _title, text in chapters)
        url = f"/api/book_file?name={quote(filename)}"
        if book_directory != _resolve_book_directory():
            url += f"&dir={quote(str(book_directory))}"
        self._sse(
            "written_book",
            {
                "title": subject,
                "filename": filename,
                "url": url,
                "directory": str(book_directory),
                "model": book_model,
                "chapters": [
                    {
                        "title": title,
                        "words": len(text.split()),
                    }
                    for title, text in chapters
                ],
                "words": total_words,
                "sources": [
                    {
                        "title": source.get("title", ""),
                        "link": source.get("link", ""),
                    }
                    for source in sources
                ],
                "created": created,
            },
        )
        self._sse_text(
            "output",
            f"\nBook saved to {path}\n"
            f"{len(chapters)} chapter(s), {total_words:,} words.\n",
        )
        state = self.state
        state.latest_answer = (
            f"Wrote the book {subject!r}: {len(chapters)} chapters, "
            f"{total_words:,} words, saved to {path}."
        )
        state.latest_sources = []
        state.latest_benchmark = {
            "model": book_model,
            "trunks": len(sources),
            "context_tokens": 0,
            "total": time.monotonic() - started,
            "book_words": total_words,
        }
        if state.benchmark_mode:
            self._sse_text(
                "log", chat.format_benchmark_line(state.latest_benchmark)
            )

    def _command_books(self, argument: str) -> None:
        """List every book written by /write_a_book in ./.chat/books.

        An optional keyword filters the list by title or filename. Each item
        is linked to /api/book_file so the browser can open and read the book
        in a new tab.
        """
        keyword = " ".join(argument.split()).casefold()
        entries = list_written_books()
        if keyword:
            entries = [
                entry
                for entry in entries
                if keyword in entry["title"].casefold()
                or keyword in entry["name"].casefold()
            ]
        self._sse(
            "book_library",
            {
                "type": "start",
                "source": "Books",
                "directory": str(WRITE_A_BOOK_DIR),
            },
        )
        for index, entry in enumerate(entries, 1):
            self._sse(
                "book_library",
                {
                    "type": "item",
                    "index": index,
                    "name": entry["name"],
                    "title": entry["title"],
                    "url": entry["url"],
                    "size": entry["size"],
                    "modified": entry["modified"],
                },
            )
        self._sse(
            "book_library",
            {
                "type": "done",
                "count": len(entries),
                "keyword": keyword,
                "directory": str(WRITE_A_BOOK_DIR),
            },
        )
        if not entries:
            hint = (
                f"No books matched {keyword!r}.\n"
                if keyword
                else "No books have been written yet.\n"
            )
            self._sse_text(
                "output",
                "\n"
                + hint
                + "Use /write_a_book <subject> to write one.\n",
            )

    def _command_pdf_books(self, argument: str) -> None:
        """Show downloaded PDFs and Manning's free ebook catalogue.

        The first section lists every PDF in ./.chat/pdf_books. The second
        section lists Manning's free ebooks newest-first; each card downloads
        the publisher's direct PDF into ./.chat/pdf_books when clicked. The
        optional argument filters both sections by title or filename/slug.
        """
        keyword = " ".join(argument.split()).casefold()
        downloaded = list_pdf_books()
        if keyword:
            downloaded = [
                entry
                for entry in downloaded
                if keyword in entry["title"].casefold()
                or keyword in entry["name"].casefold()
            ]

        manning_books: list[dict] = []
        manning_error = ""
        try:
            manning_books = list_manning_free_ebooks()
        except (requests.RequestException, RuntimeError, OSError) as exc:
            manning_error = str(exc)
        if keyword and manning_books:
            manning_books = [
                entry
                for entry in manning_books
                if keyword in str(entry.get("title") or "").casefold()
                or keyword in str(entry.get("slug") or "").casefold()
            ]

        self._sse(
            "book_library",
            {
                "type": "start",
                "source": "PDF books",
                "directory": str(PDF_BOOKS_DIR),
                "pdf": True,
                "groups": [
                    {
                        "id": "downloaded",
                        "title": "Downloaded PDF Books",
                        "empty": (
                            f"No downloaded PDF books matched {keyword!r}."
                            if keyword
                            else "No PDF books downloaded yet."
                        ),
                    },
                    {
                        "id": "manning",
                        "title": "Manning Free PDF Books",
                        "empty": (
                            manning_error
                            or (
                                f"No Manning free ebooks matched {keyword!r}."
                                if keyword
                                else "Manning's free ebook list is empty."
                            )
                        ),
                    },
                ],
            },
        )

        for index, entry in enumerate(downloaded, 1):
            self._sse(
                "book_library",
                {
                    "type": "item",
                    "section": "downloaded",
                    "index": index,
                    "name": entry["name"],
                    "title": entry["title"],
                    "url": entry["url"],
                    "size": entry["size"],
                    "modified": entry["modified"],
                    "pdf": True,
                },
            )
        for index, entry in enumerate(manning_books, 1):
            self._sse(
                "book_library",
                {
                    "type": "item",
                    "section": "manning",
                    "index": index,
                    "name": entry.get("slug", ""),
                    "slug": entry.get("slug", ""),
                    "title": entry.get("title", ""),
                    "subtitle": entry.get("subtitle", ""),
                    "description": entry.get("description", ""),
                    "pages": entry.get("pages", ""),
                    "published": entry.get("published", ""),
                    "cover": entry.get("cover", ""),
                    "book_url": entry.get("book_url", ""),
                    "pdf_ready": bool(entry.get("pdf_url")),
                    "pdf": False,
                },
            )
        self._sse(
            "book_library",
            {
                "type": "done",
                "count": len(downloaded) + len(manning_books),
                "downloaded_count": len(downloaded),
                "manning_count": len(manning_books),
                "manning_error": manning_error,
                "keyword": keyword,
                "directory": str(PDF_BOOKS_DIR),
                "pdf": True,
            },
        )
        if not downloaded and not manning_books and not manning_error:
            hint = (
                f"No PDF books matched {keyword!r}.\n"
                if keyword
                else "No PDF books found.\n"
            )
            self._sse_text(
                "output",
                "\n"
                + hint
                + f"Add PDFs to {PDF_BOOKS_DIR}.\n",
            )


# Cross-component imports are placed after the definitions so a
# feature module can depend on another feature module safely.
from .books import PDF_BOOKS_DIR, WRITE_A_BOOK_ARTICLE_TIMEOUT_SECONDS, WRITE_A_BOOK_CHAPTER_PROMPT, WRITE_A_BOOK_DIGEST_CHARS, WRITE_A_BOOK_DIR, WRITE_A_BOOK_EVIDENCE_CHARS, WRITE_A_BOOK_KEYWORD_PROMPT, WRITE_A_BOOK_OUTLINE_PROMPT, WRITE_A_BOOK_REFERENCE_MAP_CHARS, WRITE_A_BOOK_RESEARCH_BUDGET_SECONDS, WRITE_A_BOOK_RESEARCH_WORKERS, WRITE_A_BOOK_SEARCH_TIMEOUT_SECONDS, _book_chapter_evidence, _book_chapter_titles, _book_filename, _book_html_document, _book_keyword_list, _book_limit_words, _book_load_source, _book_reference_map, _book_search_candidates, _book_source_digest, _book_strip_chapter_heading, _parse_write_a_book_options, _resolve_book_directory, list_manning_free_ebooks, list_pdf_books, list_written_books
from .retrieval import ClientDisconnected
