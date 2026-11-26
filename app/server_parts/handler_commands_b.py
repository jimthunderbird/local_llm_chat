"""Reddit, finance, library, and podcast command handlers."""

from __future__ import annotations

from .core import *

class HandlerCommandsBMixin:
    def _stream_reddit_queries(
        self, queries: list[tuple[str, str]]
    ) -> int:
        """Run several Reddit searches and stream one merged, unique list.

        ``queries`` holds ``(label, google_news_query)`` pairs. Collection is
        delegated to :func:`collect_reddit_entries`; this method only emits the
        single ``start`` event, the renumbered items (with their paragraphs),
        and the final ``done`` event.
        """
        script = SCRIPT_DIR / "news_fetch.php"
        if not script.exists():
            self._sse_text("output", "\n/reddit: missing news_fetch.php.\n")
            return 0

        labels = ["Reddit"] + [label for label, _query in queries[1:]]
        self._sse(
            "news",
            {
                "type": "start",
                "source": "Reddit",
                "sources": labels,
                "docs": True,
                "docs_label": "Reddit",
                "limit": REDDIT_MAX_RESULTS,
            },
        )

        selected = collect_reddit_entries(
            queries,
            log=lambda text: self._sse_text("log", text),
            error=lambda text: self._sse_text("output", text),
        )

        item_count = 0
        for emitted_index, entry in enumerate(selected, 1):
            payload = dict(entry["payload"])
            payload["index"] = emitted_index
            self._sse("news", payload)
            for text in entry["paragraphs"]:
                self._sse(
                    "news",
                    {
                        "type": "paragraph",
                        "index": emitted_index,
                        "text": text,
                    },
                )
            if entry["ended"]:
                self._sse(
                    "news", {"type": "item_end", "index": emitted_index}
                )
            item_count += 1

        self._sse("news", {"type": "done", "count": item_count})
        if item_count == 0:
            self._sse_text(
                "output", "\n/reddit: no posts were found in the feed.\n"
            )
        return item_count

    def _command_reddit(self, argument: str) -> None:
        """Search Reddit posts through Google News RSS and stream them as cards.

        Usage: /reddit <keywords>
        The search first targets site:reddit.com, then repeats the same
        keywords inside the subreddits the answer model associates with the
        query's main entities (for example /reddit jensen huang also searches
        r/nvidia). All result lists are merged, de-duplicated, and streamed as
        one set of /news cards. Opening a post fetches its body and comments
        through /api/reddit_article, which uses Reddit's public .json endpoint
        with an Atom RSS fallback. Once the full thread has loaded, the modal
        offers Learn and then an isolated Chat, exactly like a learned /news
        story.
        """
        keyword = " ".join(argument.split())
        if keyword.lower().startswith("search "):
            keyword = keyword[7:].strip()
        # Accept a manually typed site restriction without doubling it.
        keyword = re.sub(
            r"^\s*site:reddit\.com(?:/r/[A-Za-z0-9_]+)?\s+",
            "",
            keyword,
            flags=re.IGNORECASE,
        )
        if not keyword:
            self._sse_text(
                "output",
                "\nUsage: /reddit <keywords>\n"
                "Example: /reddit jensen huang\n"
                "Example: /reddit best ramen in tokyo\n",
            )
            return

        self._sse_text(
            "log_update",
            f"[reddit: extracting entities from {keyword!r}...]",
        )
        entity_text = ""

        def emit_entity_token(token: str) -> None:
            nonlocal entity_text
            entity_text += token
            preview = " ".join(entity_text.split())
            if len(preview) > 240:
                preview = "..." + preview[-240:]
            self._sse_text(
                "log_update",
                f"[reddit: extracted entities: {preview}]",
            )

        entities = _reddit_entities_streaming(
            keyword, emit=emit_entity_token
        )
        if entities:
            self._sse_text(
                "log_update",
                "[reddit: related subreddits: "
                + ", ".join(f"r/{entity}" for entity in entities)
                + "]",
            )
        queries: list[tuple[str, str]] = [
            ("Reddit", f"site:reddit.com {keyword}")
        ]
        # Inside each related subreddit, search the complete keyword phrase so
        # "jensen huang" is not reduced to separate "jensen" and "huang"
        # matches. The main site-wide search keeps its original query.
        subreddit_keyword = _reddit_quote_phrase(keyword)
        for entity in entities:
            queries.append(
                (
                    f"r/{entity}",
                    f"site:reddit.com/r/{entity} {subreddit_keyword}",
                )
            )
        self._sse_text(
            "log",
            f"[reddit: searching site:reddit.com {keyword!r}]",
        )
        self._stream_reddit_queries(queries)

    def _command_googlenews(self, argument: str) -> None:
        """Search Google News RSS for the latest stories matching keywords.

        Usage: /googlenews <keywords>
        news_fetch.php creates a dynamic googlenews source for the query, so
        the browser reuses the /news story-list and detail rendering.
        """
        keywords = argument.strip()
        if not keywords:
            self._sse_text(
                "output",
                "\nUsage: /googlenews <keywords>\n"
                "Example: /googlenews open source llm\n",
            )
            return
        self._stream_news_events(
            "news_fetch.php",
            "/googlenews",
            ["--source", "googlenews", "--search", keywords],
        )

    def _command_gkg(self, argument: str) -> None:
        """Search the newest GDELT 2.0 Global Knowledge Graph batches.

        Usage: /gkg <keyword>[:<location>]

        gkg_fetch.py downloads the latest 15-minute GKG files, matches the
        keyword against each article's title, themes, people, organizations,
        locations, and URL, and streams at most 20 records as the same
        news-style list used by /news. A ``:location`` suffix is geocoded with
        OpenStreetMap Nominatim and restricts the results to stories whose
        GDELT places fall within 15 km of those coordinates; if geocoding is
        unavailable it falls back to matching the place name in GDELT's
        location metadata. Either side may be empty (for example
        ``/gkg :san francisco bayview, ca``). Opening a story fetches the full
        article body through the normal /api/news_article path.
        """
        keywords = " ".join(argument.split())
        if not keywords:
            self._sse_text(
                "output",
                "\nUsage: /gkg <keyword>[:<location>]\n"
                "Example: /gkg open source llm\n"
                "Example: /gkg restaurant:san francisco, ca\n"
                "Example: /gkg :san francisco bayview, ca\n",
            )
            return
        self._stream_news_events(
            "gkg_fetch.py",
            "/gkg",
            ["--limit", "20", keywords],
        )

    def _command_howstuffworks(self, argument: str) -> None:
        """Search HowStuffWorks articles by keywords.

        Usage: /howstuffworks <keywords>
        The command reuses the /news search pipeline: news_fetch.php searches
        Google News RSS, with the query restricted to
        site:howstuffworks.com. Its result list and detail modal therefore
        support Learn, Chat, and read-aloud exactly like a /news story. Full
        article bodies are fetched on demand when a story is opened.
        """
        keywords = argument.strip()
        if not keywords:
            self._sse_text(
                "output",
                "\nUsage: /howstuffworks <keywords>\n"
                "Example: /howstuffworks how do black holes work\n",
            )
            return
        self._stream_news_events(
            "news_fetch.php",
            "/howstuffworks",
            [
                "--source",
                "googlenews",
                "--search",
                f"site:howstuffworks.com {keywords}",
            ],
        )

    def _command_tablelog(self, argument: str) -> None:
        """Search Tabelog restaurant pages by keywords.

        Usage: /tablelog <keywords>
        This is the /news search pipeline restricted to tabelog.com/en: the
        query becomes ``site:tabelog.com/en <keywords>`` and news_fetch.php
        streams Google News RSS matches. The browser renders the same story
        list as /news, and each result supports the article modal, Learn, and
        Chat like any other news story.
        """
        keywords = argument.strip()
        if not keywords:
            self._sse_text(
                "output",
                "\nJapan food and restaurant guide for travelers\n"
                "Usage: /tablelog <keywords>\n"
                "Example: /tablelog sushi in ginza\n",
            )
            return
        self._stream_news_events(
            "news_fetch.php",
            "/tablelog",
            [
                "--source",
                "googlenews",
                "--search",
                f"site:tabelog.com/en {keywords}",
            ],
        )

    def _command_robinhood(self, argument: str) -> None:
        """Show one symbol's holdings, pending orders, and transactions.

        Usage: /robinhood <stock_symbol>
        The data comes from a Robinhood MCP server, either the official
        streamable-HTTP endpoint or a local stdio server such as
        ``uvx robinhood-mcp``. See _robinhood_mcp_config for the supported
        configuration keys and the ``.chat/robinhood_mcp.json`` template.
        """
        symbol = argument.strip().strip('"').strip("'").lstrip("$").upper()
        if not symbol or " " in symbol:
            self._sse_text(
                "output",
                "\nUsage: /robinhood <stock_symbol>\n"
                "Example: /robinhood AAPL\n"
                "Requires a Robinhood MCP server: fill in "
                ".chat/robinhood_mcp.json or set ROBINHOOD_MCP_COMMAND / "
                "ROBINHOOD_MCP_TOKEN.\n",
            )
            return
        self._sse_text(
            "log", f"[robinhood: reading {symbol} through the Robinhood MCP server]"
        )
        try:
            report = robinhood_holdings_report(symbol)
        except RobinhoodMCPError as exc:
            self._sse("error", {"message": f"\n/robinhood: {exc}\n"})
            return
        self._sse_text("output", "\n" + report + "\n")

    def _command_zillow(self, argument: str) -> None:
        """Show nearby for-sale and rental listings from Zillow's MCP.

        Usage: /zillow <latitude,longitude> [radius_miles]
        The official Zillow MCP server accepts named regions. The command
        reverse-geocodes the coordinate to a city/neighborhood, requests
        nearby-sorted for-sale and rental searches, then filters the returned
        listings by exact Haversine distance. No account or API key is needed.
        """
        parts = [part for part in re.split(r"[\s,]+", argument.strip()) if part]
        if len(parts) < 2:
            self._sse_text(
                "output",
                "\nUsage: /zillow <latitude,longitude> [radius_miles]\n"
                "Example: /zillow 37.7749,-122.4194 1\n"
                "Uses the official Zillow MCP server; no account or API key "
                "is needed.\n",
            )
            return
        try:
            latitude = float(parts[0])
            longitude = float(parts[1])
            radius = (
                float(parts[2])
                if len(parts) >= 3
                else ZILLOW_MCP_RADIUS_MILES
            )
        except ValueError:
            self._sse_text(
                "output",
                "\n/zillow: latitude, longitude, and radius must be numbers.\n",
            )
            return
        if not (-90 <= latitude <= 90 and -180 <= longitude <= 180):
            self._sse_text(
                "output", "\n/zillow: invalid latitude or longitude.\n"
            )
            return
        self._sse_text(
            "log",
            f"[zillow: searching Zillow around {latitude:.5f}, "
            f"{longitude:.5f} within {radius:g} mile(s)]",
        )
        try:
            report = zillow_listings_report(latitude, longitude, radius)
        except ValueError as exc:
            self._sse_text("output", f"\n/zillow: {exc}\n")
            return
        except ZillowMCPError as exc:
            self._sse_text("output", f"\n/zillow: {exc}\n")
            return
        self._sse_text("output", "\n" + report + "\n")

    def _command_stock(self, argument: str) -> None:
        """Show one stock symbol, its profile/financials, and related articles.

        Usage: /stock <stock_symbol>
        CNBC's public quote service supplies the quote, valuation ratios, and
        margins for US and international tickers. Nasdaq's public company
        endpoints add the business description, profile, and annual income
        statement for US listings; a Wikipedia summary fills in the company
        background for other exchanges. Related stories come from Google News
        through news_fetch.php, so the article list reuses the /news story
        modal and its Learn, Chat, translate, and read-aloud actions. No Yahoo
        Finance API is used.
        """
        symbol = argument.strip().strip('"').strip("'")
        if not symbol:
            self._sse_text(
                "output",
                "\nUsage: /stock <stock_symbol>\n"
                "Example: /stock AAPL\n"
                "Example: /stock 7203.T\n",
            )
            return

        cache_key = _stock_cache_key(symbol)
        cached = _stock_cache_read(cache_key, STOCK_CACHE_TTL)
        if cached is not None:
            data, _ = cached
            self._sse_text(
                "log", f"[stock: using cached data for {symbol!r}]"
            )
        else:
            self._sse_text(
                "log",
                f"[stock: looking up {symbol!r} via CNBC and Nasdaq]",
            )
            try:
                quote, resolved = resolve_stock_symbol(symbol)
                if not quote:
                    raise RuntimeError(f"no stock quote match for {symbol!r}")

                asset_text = " ".join(
                    str(quote.get(key) or "")
                    for key in ("subType", "type")
                )
                asset_class = (
                    "etf"
                    if re.search(r"\b(etf|fund|trust)\b", asset_text, re.I)
                    else "stocks"
                )
                is_us_listing = (
                    str(quote.get("countryCode") or "").upper() == "US"
                )
                summary = (
                    fetch_nasdaq_summary(resolved, asset_class)
                    if is_us_listing
                    else {}
                )
                if asset_class == "etf" or not is_us_listing:
                    profile: dict = {}
                    financials: dict = {}
                else:
                    profile = fetch_nasdaq_company_profile(resolved)
                    financials = fetch_nasdaq_financials(resolved)
                wiki = fetch_wikipedia_company_summary(
                    str(quote.get("name") or resolved)
                )
                holdings = (
                    fetch_etf_holdings(resolved)
                    if asset_class == "etf"
                    else {}
                )
                data = {
                    "quote": quote,
                    "summary": summary,
                    "profile": profile,
                    "financials": financials,
                    "wiki": wiki,
                    "holdings": holdings,
                    "asset_class": asset_class,
                    "resolved": resolved,
                }
            except RuntimeError as exc:
                stale = _stock_cache_read(
                    cache_key, STOCK_CACHE_TTL, allow_stale=True
                )
                if stale is not None:
                    data, _ = stale
                    self._sse_text(
                        "log",
                        "[stock: live data unavailable "
                        f"({exc}); using cached data]",
                    )
                else:
                    self._sse_text("output", f"\n/stock: {exc}\n")
                    return
            else:
                _stock_cache_store(cache_key, data)
                _stock_cache_store(
                    _stock_cache_key(data["resolved"]), data
                )

        quote = data.get("quote") if isinstance(data.get("quote"), dict) else {}
        resolved = str(
            data.get("resolved") or quote.get("symbol") or symbol
        ).strip().upper()
        quote_asset_text = " ".join(
            str(quote.get(key) or "") for key in ("subType", "type")
        )
        is_etf = bool(
            re.search(r"\b(etf|fund|trust)\b", quote_asset_text, re.I)
        )
        holdings = data.get("holdings")
        if not isinstance(holdings, dict):
            holdings = {}
        if is_etf and not holdings:
            holdings = fetch_etf_holdings(resolved)
        description = stock_quote_description(
            quote,
            data.get("profile") if isinstance(data.get("profile"), dict) else {},
            data.get("summary") if isinstance(data.get("summary"), dict) else {},
            data.get("financials") if isinstance(data.get("financials"), dict) else {},
            data.get("wiki") if isinstance(data.get("wiki"), dict) else {},
            resolved,
            holdings,
        )
        earnings_dates = (
            fetch_holding_earnings_dates(holdings)
            if isinstance(holdings.get("holdings"), list)
            and holdings.get("holdings")
            else {}
        )
        holdings_payload = stock_holdings_payload(holdings, earnings_dates)
        company_name = str(quote.get("name") or resolved).strip()
        search_query = f"{company_name} stock"
        sources = ["CNBC"]
        if data.get("profile") or data.get("financials") or data.get("summary"):
            sources.append("Nasdaq")
        if data.get("wiki"):
            sources.append("Wikipedia")
        if holdings.get("holdings"):
            sources.append(str(holdings.get("source") or "ETF holdings"))
        sources.append("Google News")
        self._sse(
            "news",
            {
                "type": "start",
                "source": "Stock",
                "feed": (
                    "https://www.cnbc.com/quotes/"
                    f"{quote_plus(resolved, safe='')}"
                ),
                "sources": sources,
                "description": description,
                "holdings": holdings_payload,
                "symbol": resolved,
                "company_name": company_name,
                "tabs": ["News", "Reddit"],
            },
        )
        self._stream_news_events(
            "news_fetch.php",
            "/stock",
            ["--source", "googlenews", "--search", search_query],
            skip_start=True,
        )

    def _command_medium(self, argument: str) -> None:
        """Stream the latest stories from the configured Medium author.

        Usage: /medium [number-of-stories]
        The author handle is read from ~/.medium/username; news_fetch.php
        fetches https://medium.com/feed/@<handle> through its medium_author
        source and the browser reuses the /news story-list and detail rendering.
        """
        username = medium_username()
        if not username:
            self._sse_text(
                "output",
                "\n/medium: write your Medium handle (for example "
                "@yourname) to ~/.medium/username first.\n",
            )
            return

        argv: list[str] = ["--source", "medium_author"]
        for part in argument.replace(",", " ").split():
            if part.isdigit():
                if int(part) < 1:
                    self._sse_text(
                        "output", "\nUsage: /medium [number-of-stories]\n"
                    )
                    return
                argv.extend(["--limit", part])
            else:
                self._sse_text(
                    "output", "\nUsage: /medium [number-of-stories]\n"
                )
                return
        self._stream_news_events("news_fetch.php", "/medium", argv)

    def _command_ai_news_podcast(self, argument: str) -> None:
        """List the five newest stories and offer a two-voice podcast.

        Usage: /ai_news_podcast
        The list is the same Google News RSS query as the prompt's
        latest_news_url (the last twelve hours). The browser adds a Podcast
        button under the five story links; using it drafts a host/guest script
        from the full article bodies and reads it with two male voices.
        """
        if argument.strip():
            self._sse_text(
                "output",
                "\nUsage: /ai_news_podcast\n"
                "Shows the top five stories from the last 12 hours and adds a "
                "podcast button underneath them.\n",
            )
            return
        self._stream_news_events(
            "news_fetch.php",
            "/ai_news_podcast",
            [
                "--source",
                "googlenews",
                "--search",
                AI_NEWS_PODCAST_QUERY,
                "--limit",
                str(AI_NEWS_PODCAST_LIMIT),
            ],
            podcast=True,
        )

    def _command_gutenberg(self, argument: str) -> None:
        """Search Project Gutenberg; the browser renders clickable book cards.

        Usage: /gutenberg search <keywords>
        Clicking a card opens a two-column modal: the book's plaintext on the
        left and an isolated RAG chat about that book on the right.
        """
        subcommand, _, rest = argument.strip().partition(" ")
        keyword = rest.strip()
        if subcommand.lower() != "search" or not keyword:
            self._sse_text(
                "output",
                "\nUsage: /gutenberg search <keywords>\n"
                "Search Project Gutenberg by title, author, or keyword, then "
                "click a book to read it and ask questions.\n",
            )
            return
        heading = f"Project Gutenberg · {keyword}"
        self._sse_text(
            "log",
            f"[gutenberg: searching Google News for "
            f"{GUTENBERG_NEWS_SITE_QUERY} {keyword!r}]",
        )

        def publish(cards: list[dict]) -> None:
            self._sse(
                "books",
                {
                    "keyword": keyword,
                    "heading": heading,
                    "items": cards,
                },
            )

        items: list[dict] = []
        try:
            items = search_gutenberg_books_google_news(keyword, on_item=publish)
        except RuntimeError as exc:
            self._sse_text(
                "log", f"[gutenberg: Google News search failed: {exc}]"
            )

        # Google News searches full text and can fill a page with loosely
        # related books; the catalogue search is literal, so augment every
        # search instead of using it only when the news feed is empty. Query
        # variants (for example ``39`` -> ``thirty nine``) let the catalogue
        # find an exact title such as The Thirty-Nine Steps.
        try:
            items = _combine_gutenberg_cards(
                keyword,
                items,
                search_gutenberg_books(keyword),
                limit=GUTENBERG_BOOK_RESULT_LIMIT,
            )
        except RuntimeError as exc:
            self._sse_text(
                "log", f"[gutenberg: catalogue search failed: {exc}]"
            )

        # Google News may have published partial results while it streamed;
        # this final event replaces them with the merged, relevance-ranked
        # list so an exact catalogue hit is not hidden behind loose news hits.
        publish(items)

    def _command_openlibrary(self, argument: str) -> None:
        """Search inside Open Library books and stream clickable book cards.

        Usage: /openlibrary search <keywords>
        Open Library's inside-search endpoint matches the query against the
        full text of digitized books. Each card shows the title, authors,
        catalogue description, and the passages where the query matched. The
        card links to the Open Library record; unlike /gutenberg these books
        are not downloaded into the local reader.
        """
        subcommand, _, rest = argument.strip().partition(" ")
        keyword = rest.strip()
        if subcommand.lower() != "search" or not keyword:
            self._sse_text(
                "output",
                "\nUsage: /openlibrary search <keywords>\n"
                "Example: /openlibrary search rust borrow checker\n",
            )
            return

        heading = f"Open Library · {keyword}"
        self._sse_text(
            "log", f"[openlibrary: searching inside books for {keyword!r}]"
        )
        try:
            items = search_openlibrary_inside(keyword)
        except RuntimeError as exc:
            self._sse_text("error", f"\n{exc}\n")
            return

        self._sse(
            "books",
            {
                "keyword": keyword,
                "heading": heading,
                "items": items,
            },
        )
        if not items:
            self._sse_text(
                "output",
                f"\n/openlibrary: no books matched {keyword!r}.\n",
            )

    def _command_internet_archive(self, argument: str) -> None:
        """Search Internet Archive texts and stream clickable result cards.

        Usage: /internet_archive <keywords>
        The beta full-text search matches inside the OCR text of digitized
        books and returns the passages where the query appeared. When the
        full-text index has no match, the command falls back to the Internet
        Archive catalogue so known titles can still be found.
        """
        keyword = argument.strip()
        if keyword.lower().startswith("search "):
            keyword = keyword[7:].strip()
        if not keyword:
            self._sse_text(
                "output",
                "\nUsage: /internet_archive <keywords>\n"
                "Example: /internet_archive the spice must flow\n",
            )
            return

        heading = f"Internet Archive · {keyword}"
        self._sse_text(
            "log",
            f"[internet_archive: searching digitized texts for {keyword!r}]",
        )
        try:
            items = search_internet_archive(keyword)
        except RuntimeError as exc:
            self._sse_text("error", f"\n{exc}\n")
            return

        has_excerpts = any(item.get("related_text") for item in items)
        note = (
            f"{len(items)} publicly downloadable match"
            f"{'' if len(items) == 1 else 'es'} · "
            + (
                "full-text matches · results link to plaintext"
                if has_excerpts
                else "catalogue matches · results link to plaintext"
            )
        )
        self._sse(
            "books",
            {
                "keyword": keyword,
                "heading": heading,
                "note": note,
                "source": "internet_archive",
                "items": items,
            },
        )
        if not items:
            self._sse_text(
                "output",
                f"\n/internet_archive: no texts matched {keyword!r}.\n",
            )

    def _stream_news_events(
        self,
        script_name: str,
        command: str,
        extra_argv: list[str],
        podcast: bool = False,
        event_transform: Callable[[dict], dict] | None = None,
        skip_start: bool = False,
    ) -> int:
        """Stream a news fetcher's NDJSON events as `news` SSE events.

        news_fetch.php emits one JSON event per line; relaying each one lets
        the title list appear story by story while the feeds are still being
        downloaded. List requests run with --lazy so publisher article bodies
        are not downloaded up front; the detail modal fetches the selected
        story on demand through /api/news_article.
        """
        script = SCRIPT_DIR / script_name
        if not script.exists():
            self._sse_text(
                "output", f"\n{command}: missing {script.name}.\n"
            )
            return 0

        argv = (
            [sys.executable, str(script), "--json", "--lazy", *extra_argv]
            if script.suffix == ".py"
            else ["php", str(script), "--json", "--lazy", *extra_argv]
        )
        log_prefix = command.lstrip("/")
        process = None
        item_count = 0
        try:
            process = subprocess.Popen(
                argv,
                cwd=SCRIPT_DIR,
                stdout=subprocess.PIPE,
                stderr=subprocess.STDOUT,
                text=True,
                bufsize=1,
            )
            assert process.stdout is not None
            try:
                for raw_line in process.stdout:
                    line = raw_line.strip()
                    if not line:
                        continue
                    try:
                        payload = json.loads(line)
                    except json.JSONDecodeError:
                        self._sse_text("log", f"[{log_prefix}: {line}]")
                        continue
                    if not isinstance(payload, dict):
                        continue
                    if skip_start and payload.get("type") == "start":
                        continue
                    if event_transform is not None:
                        payload = event_transform(payload)
                    if payload.get("type") == "item":
                        item_count += 1
                        # Attach the stable learned-story id and status so the
                        # browser can offer Learn or Chat without a second
                        # request for every story in the feed.
                        try:
                            news_id = news_id_for_item(payload)
                        except ValueError:
                            news_id = ""
                        payload["news_id"] = news_id
                        payload["learned"] = bool(
                            news_id and news_is_learned(news_id)
                        )
                    if podcast:
                        # Mark every event so the browser can add the podcast
                        # action underneath this particular story list.
                        payload["ai_news_podcast"] = True
                    self._sse("news", payload)
                returncode = process.wait()
            except ClientDisconnected:
                process.terminate()
                try:
                    process.wait(timeout=5)
                except subprocess.TimeoutExpired:
                    process.kill()
                    process.wait()
                raise
        except OSError as exc:
            self._sse_text(
                "output", f"\n{command}: could not run {script.name}: {exc}\n"
            )
            return 0

        if returncode != 0:
            self._sse_text(
                "output",
                f"\n{command}: {script.name} exited with code {returncode}.\n",
            )
        elif item_count == 0:
            self._sse_text(
                "output", f"\n{command}: no stories were found in the feed.\n"
            )
        return item_count


# Cross-component imports are placed after the definitions so a
# feature module can depend on another feature module safely.
from .command_config import GUTENBERG_BOOK_RESULT_LIMIT, GUTENBERG_NEWS_SITE_QUERY
from .core import SCRIPT_DIR
from .library import _combine_gutenberg_cards, search_gutenberg_books, search_gutenberg_books_google_news, search_internet_archive, search_openlibrary_inside
from .news import medium_username
from .podcast import AI_NEWS_PODCAST_LIMIT, AI_NEWS_PODCAST_QUERY
from .reddit import REDDIT_MAX_RESULTS, _reddit_entities_streaming, _reddit_quote_phrase, collect_reddit_entries
from .retrieval import ClientDisconnected, news_id_for_item, news_is_learned
from .robinhood import RobinhoodMCPError, ZILLOW_MCP_RADIUS_MILES, ZillowMCPError, robinhood_holdings_report
from .stock import STOCK_CACHE_TTL, _stock_cache_key, _stock_cache_read, _stock_cache_store, fetch_etf_holdings, fetch_holding_earnings_dates, fetch_nasdaq_company_profile, fetch_nasdaq_financials, fetch_nasdaq_summary, fetch_wikipedia_company_summary, resolve_stock_symbol, stock_holdings_payload, stock_quote_description
from .zillow import zillow_listings_report
