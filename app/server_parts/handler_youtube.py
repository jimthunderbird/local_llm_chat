"""Transcript, YouTube, and learning request handling."""

from __future__ import annotations

from .core import *

class HandlerYoutubeMixin:
    def _command_refresh(self) -> None:
        state = self.state
        if state.article_path is not None:
            self._refresh_article(state.article_path)
            return
        new_chunks, printed, error = load_all_chunks_captured()
        if error or new_chunks is None:
            self._sse_text(
                "output",
                f"\nCannot refresh the knowledge corpus: {error}\n",
            )
            return
        state.global_chunks = new_chunks
        state.replace_chunks(new_chunks)
        state.transcript = None
        state.cache.clear()
        self._sse_text(
            "output",
            "\n" + (printed + "\n" if printed else "")
            + chat.index_memory_report(new_chunks, build_index=False)
            + "\nGlobal corpus refreshed.\n",
        )
        self._sse("meta", state.snapshot())

    def _command_transcript(
        self, argument: str, direct: bool = True, title_only: bool = False
    ) -> None:
        if not argument:
            self._sse_text(
                "output",
                "\nUsage: /youtube transcript <hash|URL|keyword> "
                "(Tab completes ids and titles)\n",
            )
            return

        video_id = None if title_only else chat.extract_transcript_video_id(argument)
        if video_id is not None:
            # A direct hash/URL may refer to a video that has a transcript but
            # no trunks yet. _activate_transcript runs the learn pipeline on
            # demand before switching the scope.
            self._activate_transcript(video_id)
            return

        matches = chat.search_transcript_titles(argument)
        if not matches:
            available = set(chat.available_video_ids())
            if not available:
                self._sse_text(
                    "output",
                    f"\nNo transcripts found in {chat.OUTPUT_DIR}. "
                    "Use /youtube learn first.\n",
                )
            else:
                self._sse_text(
                    "output", f"\nNo transcript titles matched {argument!r}.\n"
                )
            return
        if len(matches) == 1:
            # A title match without trunks is learned on demand by
            # _activate_transcript.
            self._activate_transcript(matches[0].video_id)
            return

        lines = [f"\nFound {len(matches)} transcript(s) for {argument!r}:"]
        for index, match in enumerate(matches, start=1):
            lines.append(f"  {index:>4}. {match.video_id}  {match.title}")
        lines.append(
            "Select one with /youtube transcript <video_id> or click a result below.\n"
        )
        self._sse_text("output", "\n".join(lines))
        self._sse(
            "choices",
            {
                "kind": "transcript",
                "items": [
                    {
                        "value": match.video_id,
                        "description": match.title,
                    }
                    for match in matches
                ],
            },
        )

    def _refresh_article(self, article_path: str) -> None:
        """Re-read the active article and rebuild its paragraph trunks."""
        state = self.state
        path = Path(article_path)
        try:
            new_chunks, title, structure = _load_article_chunks(path)
        except (OSError, ValueError) as exc:
            self._sse_text(
                "output",
                f"\nCannot refresh article {article_path!r}: {exc}\n",
            )
            return
        state.replace_chunks(
            new_chunks,
            article_path=article_path,
            article_title=title,
            article_structure=structure,
            article_iterative=state.article_iterative,
        )
        state.cache.clear()
        self._sse_text(
            "output",
            "\n"
            + chat.index_memory_report(
                new_chunks, build_index=False, article=article_path
            )
            + f"\nNow answering from article {article_path!r} "
            f"({sum(1 for chunk in new_chunks if chunk.index > 0)} "
            "paragraph(s)).\n",
        )
        self._sse("meta", state.snapshot())

    def _activate_transcript(self, video_id: str) -> None:
        state = self.state
        if not video_is_learned(video_id):
            self._sse_text(
                "output",
                f"\nTranscript {video_id!r} is not learned yet; running "
                f"/youtube learn {video_id}.\n",
            )
            if not self._learn_video(video_id, label="/youtube learn"):
                return
            self._finish_learn([video_id], label="/youtube learn")
        new_chunks, printed, error = load_chunks_captured(video_ids={video_id})
        if error or new_chunks is None:
            self._sse_text(
                "output",
                f"\nCannot switch to transcript {video_id!r}: {error}\n",
            )
            return
        state.replace_chunks(new_chunks)
        state.transcript = video_id
        state.cache.clear()
        # Switching to a single transcript clears the accumulated research
        # conversation and returns the chat to Local mode. The evidence must
        # come from this transcript, not from the online research path.
        state.research_with_news_mode = False
        state.latest_answer = None
        state.latest_sources = []
        state.latest_benchmark = None
        self._sse_text(
            "output",
            "\n" + (printed + "\n" if printed else "")
            + chat.index_memory_report(new_chunks, build_index=False)
            + f"\nNow answering only from transcript {video_id!r}. "
            "Chat mode: local.\n",
        )
        self._sse("meta", state.snapshot())

    def _command_youtube(self, argument: str) -> None:
        """Dispatch /youtube <subcommand> [argument]."""
        subcommand, _, rest = argument.partition(" ")
        subcommand = subcommand.strip().lower()
        rest = rest.strip()
        if not subcommand:
            lines = [
                "\nUsage: /youtube <subcommand> ...",
                "",
            ]
            for name, description in YOUTUBE_SUBCOMMANDS.items():
                lines.append(f"  /youtube {name:<14} {description}")
            self._sse_text("output", "\n".join(lines) + "\n")
            return
        if subcommand == "learn":
            self._command_learn(rest)
        elif subcommand == "relearn":
            self._command_learn(rest, relearn=True)
        elif subcommand == "learn_topic":
            self._command_learn_topic(rest)
        elif subcommand == "learn_playlist":
            self._command_learn_playlist(rest)
        elif subcommand == "search":
            self._command_youtube_search(rest)
        elif subcommand == "transcript":
            self._command_transcript(rest, direct=False)
        elif subcommand == "search_transcript":
            self._command_transcript(rest, direct=False, title_only=True)
        elif subcommand == "bookmark":
            self._command_youtube_bookmark(rest)
        else:
            self._sse_text(
                "output",
                f"\nUnknown /youtube subcommand: {subcommand!r}. "
                "Use /youtube to see the available subcommands.\n",
            )

    def _command_youtube_bookmark(self, argument: str) -> None:
        """List the videos saved with the player modal's Bookmark button."""
        action = argument.strip().lower()
        if action != "list":
            self._sse_text(
                "output",
                "\nUsage: /youtube bookmark list\n"
                "Use the Bookmark button in a video player to save a video.\n",
            )
            return
        entries = read_youtube_bookmarks()
        if not entries:
            self._sse_text(
                "output",
                "\nNo bookmarked videos yet. Open a /youtube search video and "
                "click Bookmark.\n",
            )
            return
        # Render the bookmarks with the same cards as /youtube search so a
        # saved video can be played, summarized, or learned in place.  The
        # entries are stored oldest-first; show the newest first.
        videos: list[dict] = []
        for entry in reversed(entries):
            video_id = str(entry.get("id") or "")
            if not re.fullmatch(r"[A-Za-z0-9_-]{11}", video_id):
                continue
            try:
                duration = max(0, int(entry.get("duration") or 0))
            except (TypeError, ValueError):
                duration = 0
            date = str(entry.get("date") or "").strip()
            videos.append(
                {
                    "id": video_id,
                    "title": str(entry.get("title") or video_id),
                    "thumbnail": str(entry.get("thumbnail") or ""),
                    "duration": duration,
                    "upload_date": date,
                    "published": date,
                    "published_text": date,
                    "url": f"https://www.youtube.com/watch?v={video_id}",
                }
            )
        if not videos:
            self._sse_text(
                "output", "\nNo valid bookmarked videos found.\n"
            )
            return
        self._sse_text(
            "output",
            f"\nBookmarked videos ({len(videos)}, latest first):\n",
        )
        self._sse(
            "videos",
            {
                "keyword": "",
                "items": videos,
                "has_more": False,
                "pending": False,
                "heading": "Bookmarked videos",
                "note": (
                    f"{len(videos)} saved video"
                    f"{'' if len(videos) == 1 else 's'} · latest first"
                ),
            },
        )

    def _command_youtube_search(self, argument: str) -> None:
        """Search YouTube's own results page and show video cards.

        Usage: /youtube search "<keyword>"
        The command loads ``https://www.youtube.com/results?search_query=
        <keyword>`` and extracts the video metadata embedded in the page.
        Each card carries the watch url, title, channel, duration, thumbnail,
        and relative publish date.  Each card opens a centered player, and its
        Summary/Learn buttons keep working from the video id.  The fetched
        result page is cached so the "More" button can reveal the remaining
        videos without another search.
        """
        keyword = strip_surrounding_search_quotes(argument)
        if not keyword:
            self._sse_text(
                "output", '\nUsage: /youtube search "<keyword>"\n'
            )
            return

        page_url = (
            "https://www.youtube.com/results?search_query="
            + quote_plus(keyword)
        )
        self._sse_text(
            "output",
            f"\n/youtube search: loading {page_url}\n",
        )
        # Fetch one extra result so the UI knows whether a "More" button is
        # useful without making a second search just to find out.
        candidate_state: dict[str, object] = {"items": [], "has_more": False}

        def on_candidates(items: list[dict], has_more: bool) -> None:
            if not items:
                return
            candidate_state["items"] = items
            candidate_state["has_more"] = has_more
            # Paint the cards immediately if a future backend streams
            # candidates before the final result set.
            self._sse(
                "videos",
                {
                    "keyword": keyword,
                    "items": items[:YOUTUBE_SEARCH_PAGE_SIZE],
                    "has_more": has_more,
                    "pending": True,
                },
            )

        try:
            results = self._cached_youtube_search(
                keyword,
                YOUTUBE_SEARCH_PAGE_SIZE + 1,
                on_candidates=on_candidates,
            )
        except RuntimeError as exc:
            self._sse_text("output", f"\n/youtube search: {exc}\n")
            return

        videos = results[:YOUTUBE_SEARCH_PAGE_SIZE]
        if not videos:
            pending_items = list(candidate_state["items"])[
                :YOUTUBE_SEARCH_PAGE_SIZE
            ]
            if pending_items:
                self._sse_text(
                    "output",
                    "\n/youtube search: showing the matching videos found so "
                    "far.\n",
                )
                self._sse(
                    "videos",
                    {
                        "keyword": keyword,
                        "items": pending_items,
                        "has_more": bool(candidate_state["has_more"]),
                        "pending": False,
                        "captions_unverified": True,
                    },
                )
                self._merge_youtube_cache(
                    keyword, list(candidate_state["items"])
                )
                self._start_youtube_prefetch(keyword)
                return
            self._sse_text(
                "output",
                "\n/youtube search: no YouTube videos were found for "
                f"{keyword!r}.\n",
            )
            return

        self._sse_text(
            "output",
            f"\n/youtube search: {len(videos)} video(s) for {keyword!r}. "
            "Click a video to play it here, or use Summary for an AI overview.\n",
        )
        self._sse(
            "videos",
            {
                "keyword": keyword,
                "items": videos,
                "has_more": len(results) > YOUTUBE_SEARCH_PAGE_SIZE,
                "pending": False,
                "heading": "YouTube videos · search page",
                "note": (
                    f"{len(videos)} result"
                    f"{'' if len(videos) == 1 else 's'} · "
                    "youtube.com/results"
                ),
            },
        )
        if len(results) > YOUTUBE_SEARCH_PAGE_SIZE:
            # Warm the remaining cards from the same results page while the
            # user is still looking at the first one.
            self._start_youtube_prefetch(keyword)

    def _youtube_search_command(
        self,
        keyword: str,
        limit: int,
        progressive: bool,
        exclude: set[str] | None = None,
    ) -> list[str]:
        command = [
            sys.executable,
            "-u",
            str(SCRIPT_DIR / "youtube_news_search.py"),
            "--search-page",
            "--progressive-json" if progressive else "--json",
            "--limit",
            str(limit),
        ]
        if exclude:
            # Attach the value with ``=`` instead of passing it as a separate
            # argument.  YouTube ids may start with ``-`` (for example
            # ``-PcBFgkrR2o``), and argparse would otherwise mistake the
            # comma-separated exclusion list for an option and fail with
            # "argument --exclude: expected one argument".
            command.append("--exclude=" + ",".join(sorted(exclude)))
        command.extend(["--", keyword])
        return command

    def _normalize_youtube_items(
        self, raw_items: list[dict], limit: int
    ) -> list[dict]:
        videos = []
        seen = set()
        for video in raw_items or []:
            if not isinstance(video, dict):
                continue
            video_id = str(video.get("id") or "")
            if (
                not re.fullmatch(r"[A-Za-z0-9_-]{11}", video_id)
                or video_id in seen
            ):
                continue
            seen.add(video_id)
            videos.append(
                {
                    "id": video_id,
                    "title": str(video.get("title") or "").strip(),
                    "duration": int(video.get("duration") or 0),
                    "channel": str(video.get("channel") or "").strip(),
                    "thumbnail": str(video.get("thumbnail") or "").strip(),
                    "no_thumbnail": bool(video.get("no_thumbnail")),
                    "upload_date": str(video.get("upload_date") or "").strip(),
                    "published": str(video.get("published") or "").strip(),
                    "published_text": str(
                        video.get("published_text") or ""
                    ).strip(),
                    "timestamp": int(video.get("timestamp") or 0),
                    "url": str(video.get("url") or "").strip()
                    or f"https://www.youtube.com/watch?v={video_id}",
                }
            )
            if len(videos) >= limit:
                break
        return videos

    def _parse_youtube_json_lines(
        self, output: str, limit: int
    ) -> list[dict]:
        raw_items = []
        for line in output.splitlines():
            line = line.strip()
            if not line:
                continue
            try:
                video = json.loads(line)
            except json.JSONDecodeError:
                continue
            if not isinstance(video, dict):
                continue
            if video.get("type") == "videos":
                raw_items = video.get("items") or []
            elif video.get("id"):
                # Legacy one-object-per-line output.
                raw_items.append(video)
        return self._normalize_youtube_items(raw_items, limit)

    def _run_youtube_search(
        self,
        keyword: str,
        limit: int,
        on_candidates: Callable[[list[dict], bool], None] | None = None,
        exclude: set[str] | None = None,
        fallback_to_candidates: bool = False,
        status: dict[str, bool] | None = None,
    ) -> list[dict]:
        """Run youtube_news_search.py and return normalized video metadata.

        The search script reads YouTube's own search-results page and extracts
        each video's id, title, watch URL, thumbnail, channel, duration, and
        relative publish date.  ``exclude`` omits already-known video ids so a
        "More" request only returns genuinely new results.

        ``on_candidates`` is retained for the progressive caller interface,
        although the results-page search emits one final result set.  The
        ``fallback_to_candidates`` and ``status`` arguments are kept for cache
        prefetch callers.
        """
        progressive = on_candidates is not None or fallback_to_candidates
        command = self._youtube_search_command(
            keyword, limit, progressive, exclude=exclude
        )

        if not progressive:
            try:
                result = subprocess.run(
                    command,
                    cwd=SCRIPT_DIR,
                    stdout=subprocess.PIPE,
                    stderr=subprocess.PIPE,
                    text=True,
                    timeout=180,
                )
            except subprocess.TimeoutExpired as exc:
                raise RuntimeError("YouTube search timed out.") from exc
            except OSError as exc:
                raise RuntimeError(
                    f"could not run youtube_news_search.py: {exc}"
                ) from exc

            if result.returncode != 0:
                detail = (result.stderr or result.stdout or "").strip()
                raise RuntimeError(
                    "YouTube search failed" + (f": {detail}" if detail else ".")
                )
            return self._parse_youtube_json_lines(result.stdout, limit)

        try:
            process = subprocess.Popen(
                command,
                cwd=SCRIPT_DIR,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                text=True,
            )
        except OSError as exc:
            raise RuntimeError(
                f"could not run youtube_news_search.py: {exc}"
            ) from exc

        stderr_lines: list[str] = []

        def drain_stderr() -> None:
            if process.stderr is None:
                return
            try:
                for line in process.stderr:
                    stderr_lines.append(line)
            except Exception:
                pass

        stderr_thread = threading.Thread(target=drain_stderr, daemon=True)
        stderr_thread.start()
        timed_out = threading.Event()

        def kill_process() -> None:
            timed_out.set()
            try:
                process.kill()
            except OSError:
                pass

        timer = threading.Timer(180, kill_process)
        timer.start()
        final_videos: list[dict] = []
        candidate_videos: list[dict] = []
        try:
            if process.stdout is not None:
                for line in process.stdout:
                    line = line.strip()
                    if not line:
                        continue
                    try:
                        payload = json.loads(line)
                    except json.JSONDecodeError:
                        continue
                    if not isinstance(payload, dict):
                        continue
                    event_type = payload.get("type")
                    if event_type == "candidates":
                        items = self._normalize_youtube_items(
                            payload.get("items") or [], limit
                        )
                        if items:
                            candidate_videos = items
                            if on_candidates is not None:
                                on_candidates(
                                    items,
                                    len(items) > YOUTUBE_SEARCH_PAGE_SIZE,
                                )
                    elif event_type == "videos":
                        final_videos = self._normalize_youtube_items(
                            payload.get("items") or [], limit
                        )
                    elif payload.get("id"):
                        candidate_videos.append(payload)
            returncode = process.wait()
        finally:
            timer.cancel()
            if process.poll() is None:
                try:
                    process.kill()
                except OSError:
                    pass
                process.wait()
            stderr_thread.join(timeout=1)

        if timed_out.is_set():
            raise RuntimeError("YouTube search timed out.")
        if returncode != 0:
            detail = "".join(stderr_lines).strip()
            raise RuntimeError(
                "YouTube search failed" + (f": {detail}" if detail else ".")
            )
        if final_videos:
            if status is not None:
                status["captions_unverified"] = False
            return final_videos
        # The final event was empty: usually every caption lookup timed out or
        # was blocked.  A normal /youtube search keeps the candidate cards it
        # already emitted and labels them as unverified.  "More" and prefetch
        # have no such callback, so they explicitly ask for the candidates.
        if fallback_to_candidates and candidate_videos:
            if status is not None:
                status["captions_unverified"] = True
            return candidate_videos[:limit]
        if status is not None:
            status["captions_unverified"] = False
        return []

    def _cached_youtube_search(
        self,
        keyword: str,
        limit: int,
        on_candidates: Callable[[list[dict], bool], None] | None = None,
    ) -> list[dict]:
        """Return up to `limit` results, growing a per-keyword cache on demand.

        YouTube's recent-first ordering is stable, so asking for a larger
        limit returns the earlier page unchanged followed by new videos. The
        cache therefore lets the "More" button fetch just one larger result
        set and append the unseen tail.
        """
        limit = max(1, min(limit, YOUTUBE_SEARCH_MAX_RESULTS))
        with self.state.lock:
            cached = self.state.youtube_search_cache.get(keyword)
            if cached is not None and len(cached) >= limit:
                return list(cached[:limit])

        videos = self._run_youtube_search(
            keyword, limit, on_candidates=on_candidates
        )
        with self.state.lock:
            existing = self.state.youtube_search_cache.get(keyword)
            if existing is not None and len(existing) >= len(videos):
                videos = existing
            elif videos:
                self.state.youtube_search_cache[keyword] = videos
        return list(videos[:limit])

    def _youtube_cache_items(self, keyword: str) -> list[dict]:
        with self.state.lock:
            return list(self.state.youtube_search_cache.get(keyword) or [])

    def _merge_youtube_cache(
        self, keyword: str, videos: list[dict]
    ) -> list[dict]:
        """Append unseen videos to the keyword cache and return the cache."""
        with self.state.lock:
            existing = list(
                self.state.youtube_search_cache.get(keyword) or []
            )
            seen = {video["id"] for video in existing}
            for video in videos:
                if video["id"] in seen:
                    continue
                seen.add(video["id"])
                existing.append(video)
            if existing:
                self.state.youtube_search_cache[keyword] = existing
            return list(existing)

    def _grow_youtube_cache(self, keyword: str, target: int) -> list[dict]:
        """Fetch only the videos needed to reach ``target`` cached results.

        Known ids are passed as ``--exclude`` so the results-page search only
        returns videos that are not already cached.
        """
        target = max(1, min(target, YOUTUBE_SEARCH_MAX_RESULTS))
        cached = self._youtube_cache_items(keyword)
        if len(cached) >= target:
            return cached
        exclude = {video["id"] for video in cached}
        videos = self._run_youtube_search(
            keyword,
            target - len(cached),
            exclude=exclude,
            fallback_to_candidates=True,
        )
        return self._merge_youtube_cache(keyword, videos)

    def _start_youtube_prefetch(self, keyword: str) -> None:
        """Warm the cache for the next "More" clicks in the background."""
        if not keyword:
            return
        with self.state.lock:
            cached = self.state.youtube_search_cache.get(keyword) or []
            target = min(
                max(
                    YOUTUBE_SEARCH_PREFETCH_RESULTS,
                    len(cached) + YOUTUBE_SEARCH_PAGE_SIZE * 3,
                ),
                YOUTUBE_SEARCH_MAX_RESULTS,
            )
            if len(cached) >= target or keyword in self.state.youtube_prefetch:
                return
            # Grow in a few small steps.  The first step covers the next
            # "More" page quickly; later steps keep filling the cache while
            # the user reads the cards.
            step = YOUTUBE_SEARCH_PAGE_SIZE * 2
            targets = []
            next_target = min(len(cached) + step + 1, target)
            while next_target < target:
                targets.append(next_target)
                next_target = min(next_target + step, target)
            targets.append(target)
            event = threading.Event()
            self.state.youtube_prefetch[keyword] = event

        def prefetch() -> None:
            try:
                for step_target in targets:
                    self._grow_youtube_cache(keyword, step_target)
            except Exception:
                # Prefetch is opportunistic; a failure just means "More" will
                # perform a normal search when the user asks for it.
                pass
            finally:
                with self.state.lock:
                    if self.state.youtube_prefetch.get(keyword) is event:
                        self.state.youtube_prefetch.pop(keyword, None)
                event.set()

        threading.Thread(target=prefetch, daemon=True).start()

    def _handle_youtube_more(self, keyword: str, exclude: set[str]) -> None:
        """Return the next page of a previous /youtube search as JSON."""
        if not keyword:
            self._send_error_json('Usage: /youtube search "<keyword>"', 400)
            return

        page_size = YOUTUBE_SEARCH_PAGE_SIZE
        cached = self._youtube_cache_items(keyword)
        unseen = [video for video in cached if video["id"] not in exclude]

        # A background prefetch may already be filling the cache.  Poll it for
        # a short while so we can return as soon as this page is ready, rather
        # than launching a duplicate YouTube search.
        if len(unseen) < page_size:
            with self.state.lock:
                event = self.state.youtube_prefetch.get(keyword)
            deadline = time.monotonic() + YOUTUBE_PREFETCH_WAIT_SECONDS
            while event is not None and time.monotonic() < deadline:
                event.wait(timeout=0.15)
                cached = self._youtube_cache_items(keyword)
                unseen = [
                    video for video in cached if video["id"] not in exclude
                ]
                if len(unseen) >= page_size or event.is_set():
                    break
            if len(unseen) < page_size:
                with self.state.lock:
                    event = self.state.youtube_prefetch.get(keyword)
                if event is not None:
                    event.wait(timeout=0.5)
                    cached = self._youtube_cache_items(keyword)
                    unseen = [
                        video for video in cached
                        if video["id"] not in exclude
                    ]

        if len(unseen) >= page_size:
            items = unseen[:page_size]
            self._send_json(
                {
                    "keyword": keyword,
                    "items": items,
                    "has_more": len(unseen) > page_size,
                }
            )
            self._start_youtube_prefetch(keyword)
            return

        # The cache is short: fetch only the missing, unseen videos.  Asking
        # for one extra result keeps `has_more` accurate.  The results-page
        # search skips known ids and returns only unseen videos.
        missing = page_size - len(unseen) + 1
        known = set(exclude) | {video["id"] for video in cached}
        status: dict[str, bool] = {}
        try:
            results = self._run_youtube_search(
                keyword,
                missing,
                exclude=known,
                fallback_to_candidates=True,
                status=status,
            )
        except RuntimeError as exc:
            self._send_error_json(f"/youtube search: {exc}", 502)
            return

        cached = self._merge_youtube_cache(keyword, results)
        unseen = [video for video in cached if video["id"] not in exclude]
        items = unseen[:page_size]
        self._send_json(
            {
                "keyword": keyword,
                "items": items,
                "has_more": len(unseen) > page_size,
                "captions_unverified": bool(
                    status.get("captions_unverified")
                ),
            }
        )
        self._start_youtube_prefetch(keyword)

    def _command_learn(self, argument: str, relearn: bool = False) -> None:
        label = "/youtube relearn" if relearn else "/youtube learn"
        if not argument:
            self._sse_text(
                "output",
                f"\nUsage: {label} <youtube_video_hash|url>\n",
            )
            return
        try:
            import youtube_video_learn as learn
        except ImportError as exc:
            self._sse_text(
                "output", f"\n{label} is unavailable: cannot import "
                f"youtube_video_learn.py ({exc})\n"
            )
            return
        try:
            video_id = learn.extract_video_hash(argument)
        except ValueError as exc:
            self._sse_text("output", f"\n{label}: {exc}\n")
            return

        self._sse_text("output", f"\n{label}: {video_id}\n")
        if not self._learn_video(video_id, label=label, relearn=relearn):
            return
        self._finish_learn([video_id], label=label)

    def _command_learn_topic(self, argument: str) -> None:
        topic = argument.strip()
        if not topic:
            self._sse_text(
                "output", "\nUsage: /youtube learn_topic <topic>\n"
            )
            return

        self._sse_text(
            "output", f"\n/youtube learn_topic: searching YouTube for {topic!r}...\n"
        )
        try:
            result = subprocess.run(
                [
                    sys.executable,
                    "-u",
                    str(SCRIPT_DIR / "search_youtube.py"),
                    "--limit",
                    "3",
                    "--",
                    topic,
                ],
                cwd=SCRIPT_DIR,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                text=True,
                timeout=600,
            )
        except subprocess.TimeoutExpired:
            self._sse_text(
                "output", "\n/youtube learn_topic: YouTube search timed out.\n"
            )
            return
        except OSError as exc:
            self._sse_text(
                "output", f"\n/youtube learn_topic: could not run search_youtube.py: {exc}\n"
            )
            return

        if result.stderr.strip():
            self._sse_text(
                "output",
                result.stderr if result.stderr.endswith("\n") else result.stderr + "\n",
            )
        if result.returncode != 0:
            self._sse_text(
                "output",
                f"\n/youtube learn_topic: search_youtube.py failed with exit code "
                f"{result.returncode}.\n",
            )
            return

        video_ids = []
        for line in result.stdout.splitlines():
            video_id = line.strip()
            if (
                re.fullmatch(r"[A-Za-z0-9_-]{11}", video_id)
                and video_id not in video_ids
            ):
                video_ids.append(video_id)
        video_ids = video_ids[:3]

        if not video_ids:
            self._sse_text(
                "output",
                "\n/youtube learn_topic: no videos with an English transcript were "
                "found for this topic.\n",
            )
            return

        self._sse_text(
            "output",
            f"\n/youtube learn_topic: learning {len(video_ids)} video(s): "
            f"{', '.join(video_ids)}\n",
        )
        learned = []
        for video_id in video_ids:
            self._sse_text("output", f"\n/youtube learn_topic: {video_id}\n")
            if self._learn_video(video_id, label="/youtube learn_topic"):
                learned.append(video_id)
            else:
                self._sse_text(
                    "output",
                    f"\n/youtube learn_topic: could not learn {video_id}; continuing "
                    "with any remaining videos.\n",
                )

        if not learned:
            self._sse_text(
                "output", "\n/youtube learn_topic: no videos could be learned.\n"
            )
            return
        self._finish_learn(learned, label="/youtube learn_topic")

    def _command_learn_playlist(self, argument: str) -> None:
        """Learn every video in a YouTube playlist."""
        playlist = argument.strip()
        if not playlist:
            self._sse_text(
                "output",
                "\nUsage: /youtube learn_playlist <youtube_playlist_id|url>\n",
            )
            return

        self._sse_text(
            "output",
            f"\n/youtube learn_playlist: fetching videos from {playlist!r}...\n",
        )
        try:
            result = subprocess.run(
                [
                    sys.executable,
                    "-u",
                    str(SCRIPT_DIR / "get_videos_in_playlist.py"),
                    playlist,
                ],
                cwd=SCRIPT_DIR,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                text=True,
                timeout=600,
            )
        except subprocess.TimeoutExpired:
            self._sse_text(
                "output",
                "\n/youtube learn_playlist: playlist fetch timed out.\n",
            )
            return
        except OSError as exc:
            self._sse_text(
                "output",
                "\n/youtube learn_playlist: could not run "
                f"get_videos_in_playlist.py: {exc}\n",
            )
            return

        if result.stderr.strip():
            self._sse_text(
                "output",
                result.stderr
                if result.stderr.endswith("\n")
                else result.stderr + "\n",
            )
        if result.returncode != 0:
            self._sse_text(
                "output",
                "\n/youtube learn_playlist: get_videos_in_playlist.py failed with "
                f"exit code {result.returncode}.\n",
            )
            return

        video_ids: list[str] = []
        for line in result.stdout.splitlines():
            video_id = line.strip()
            if (
                re.fullmatch(r"[A-Za-z0-9_-]{11}", video_id)
                and video_id not in video_ids
            ):
                video_ids.append(video_id)

        if not video_ids:
            self._sse_text(
                "output",
                "\n/youtube learn_playlist: the playlist contains no videos.\n",
            )
            return

        total = len(video_ids)
        self._sse_text(
            "output",
            f"\n/youtube learn_playlist: learning {total} video(s) from the "
            "playlist.\n",
        )
        learned = []
        for index, video_id in enumerate(video_ids, start=1):
            self._sse_text(
                "output",
                f"\n/youtube learn_playlist: [{index}/{total}] {video_id}\n",
            )
            if self._learn_video(video_id, label="/youtube learn_playlist"):
                learned.append(video_id)
            else:
                self._sse_text(
                    "output",
                    f"\n/youtube learn_playlist: could not learn {video_id}; "
                    "continuing with any remaining videos.\n",
                )

        if not learned:
            self._sse_text(
                "output",
                "\n/youtube learn_playlist: no videos could be learned.\n",
            )
            return
        self._finish_learn(learned, label="/youtube learn_playlist")

    def _learn_video(
        self,
        video_id: str,
        label: str = "/youtube learn",
        relearn: bool = False,
    ) -> bool:
        """Run the youtube_video_learn.py pipeline for one video."""
        try:
            import youtube_video_learn as learn
        except ImportError as exc:
            self._sse_text(
                "output",
                f"\n{label} is unavailable: cannot import "
                f"youtube_video_learn.py ({exc})\n",
            )
            return False

        if relearn:
            self._sse_text(
                "output",
                f"\n{label}: removing previous title and trunks; "
                "the transcript is unchanged.\n",
            )
            try:
                removed = learn.remove_generated_artifacts(video_id)
            except OSError as exc:
                self._sse_text(
                    "output",
                    f"\n{label}: could not remove old artifacts: {exc}\n",
                )
                return False
            self._sse_text(
                "output",
                f"\n{label}: removed {len(removed)} generated file(s).\n",
            )

        for script, script_args in learn.steps_for(relearn):
            argv = [arg.replace("<hash>", video_id) for arg in script_args]
            if not self._run_learn_step(script, argv, label=label):
                return False
        return True

    def _run_learn_step(
        self, script: str, argv: list[str], label: str = "/youtube learn"
    ) -> bool:
        """Run one learn pipeline step, streaming its output."""
        self._sse_text(
            "output", f"\n=== {script} {' '.join(argv)} ===\n"
        )
        process = None
        try:
            process = subprocess.Popen(
                [
                    sys.executable,
                    "-u",
                    str(SCRIPT_DIR / script),
                    *argv,
                ],
                cwd=SCRIPT_DIR,
                stdout=subprocess.PIPE,
                stderr=subprocess.STDOUT,
                text=True,
                bufsize=1,
            )
            assert process.stdout is not None
            try:
                for line in process.stdout:
                    self._sse_text("output", line)
                returncode = process.wait()
            except ClientDisconnected:
                # The browser cancelled the stream; do not leave the
                # learn pipeline running in the background.
                process.terminate()
                try:
                    process.wait(timeout=5)
                except subprocess.TimeoutExpired:
                    process.kill()
                    process.wait()
                raise
        except OSError as exc:
            self._sse_text(
                "output", f"\n{label}: could not run {script}: {exc}\n"
            )
            return False
        if returncode != 0:
            self._sse_text(
                "output",
                f"\n{label} aborted: {script} failed with exit code "
                f"{returncode}\n",
            )
            return False
        return True

    def _finish_learn(self, video_ids: list[str], label: str = "/youtube learn") -> None:
        """Invalidate caches and refresh the global corpus."""
        state = self.state
        chat.clear_bm25_cache()
        state.cache.clear()
        for video_id in video_ids:
            self._sse_text(
                "output",
                f"\n{label}: {video_id} added to the knowledge base.\n",
            )

        new_chunks = printed = error = None
        reloaded = False

        # Keep the global corpus current even when an article or transcript
        # scope is active, so a later /refresh sees the new video.
        global_chunks, _global_printed, global_error = load_all_chunks_captured()
        if not global_error and global_chunks is not None:
            state.global_chunks = global_chunks
            # Index the newly learned trunks and compact the FTS5 segments
            # here, during the learn command rather than on the next question.
            try:
                chat.index_chunks_in_sqlite(
                    global_chunks, corpus_key="global", prune=True
                )
            except Exception:
                pass

        if state.article_path is not None:
            self._sse_text(
                "output",
                f"\nStill answering from article {state.article_path!r}; "
                "the loaded article context is unchanged.\n",
            )
        elif state.transcript is not None:
            if state.transcript in video_ids:
                new_chunks, printed, error = load_chunks_captured(
                    video_ids={state.transcript}
                )
                reloaded = True
            else:
                self._sse_text(
                    "output",
                    f"\nStill answering only from transcript "
                    f"{state.transcript!r}; the loaded trunks are unchanged.\n",
                )
        else:
            # A newly learned video belongs to the global corpus immediately,
            # without a membership check or classifier pass.
            new_chunks, printed, error = global_chunks, _global_printed, global_error
            reloaded = True

        if reloaded:
            if error or new_chunks is None:
                self._sse_text(
                    "output",
                    f"\n{label}: could not reload the active scope: {error}\n",
                )
            else:
                state.global_chunks = new_chunks
                state.replace_chunks(new_chunks)
                self._sse_text(
                    "output",
                    "\n" + (printed + "\n" if printed else "")
                    + chat.index_memory_report(
                        new_chunks, build_index=False
                    )
                    + "\n",
                )
        self._sse("meta", state.snapshot())


# Cross-component imports are placed after the definitions so a
# feature module can depend on another feature module safely.
from .article import _load_article_chunks
from .command_config import YOUTUBE_PREFETCH_WAIT_SECONDS, YOUTUBE_SEARCH_MAX_RESULTS, YOUTUBE_SEARCH_PAGE_SIZE, YOUTUBE_SEARCH_PREFETCH_RESULTS, YOUTUBE_SUBCOMMANDS, strip_surrounding_search_quotes
from .core import SCRIPT_DIR
from .retrieval import ClientDisconnected, load_all_chunks_captured, load_chunks_captured
from .video import video_is_learned
from .youtube import read_youtube_bookmarks
