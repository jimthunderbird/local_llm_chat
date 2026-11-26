"""News, documentation, travel, and map command handlers."""

from __future__ import annotations

from .core import *

class HandlerCommandsAMixin:
    def _command_news(self, argument: str) -> None:
        """Stream the latest news stories to the browser.

        Usage: /news [SOURCE ...] [number-of-stories]
               /news search <keywords> [website]
        Each SOURCE is one of wsj, cnn, cbslite, nprtext, yahoo_finance, medium,
        kqed, sfgate, sfbayview, mission_local, richmond_sunset, techcrunch,
        krasia, wccftech, or hackernews.
        Several sources may be selected.
        Without a source, every supported source is shown as its own section.
        "search <keywords> [website]" is the same as /googlenews <keywords>,
        except a trailing website restricts the search to that site, for
        example: /news search black holes howstuffworks.com. It renders the
        matches exactly like a /news story list.
        """
        stripped = argument.strip()
        search_match = re.match(r"(?i)^search(?:\s+(.*))?$", stripped)
        if search_match:
            keywords = (search_match.group(1) or "").strip()
            if not keywords:
                self._sse_text(
                    "output",
                    "\nUsage: /news search <keywords> [website]\n"
                    "Example: /news search jensen huang\n"
                    "Example: /news search black holes howstuffworks.com\n",
                )
                return
            keywords, website = split_news_search_website(keywords)
            query = f"site:{website} {keywords}" if website else keywords
            self._stream_news_events(
                "news_fetch.php",
                "/news",
                ["--source", "googlenews", "--search", query],
            )
            return

        argv: list[str] = []
        sources: list[str] = []
        for part in argument.replace(",", " ").split():
            lowered = part.lower()
            if part.isdigit():
                if int(part) < 1:
                    self._sse_text(
                        "output",
                        f"\nUsage: /news [{NEWS_SOURCE_USAGE}] ... "
                        "[number-of-stories]\n",
                    )
                    return
                argv.extend(["--limit", part])
            elif lowered in NEWS_SOURCES:
                if lowered not in sources:
                    sources.append(lowered)
            else:
                self._sse_text(
                    "output",
                    f"\nUsage: /news [{NEWS_SOURCE_USAGE}] ... "
                    "[number-of-stories]\n",
                )
                return
        for source in sources:
            argv.extend(["--source", source])
        self._stream_news_events("news_fetch.php", "/news", argv)

    def _command_mdn(self, argument: str) -> None:
        """Search MDN Web Docs and stream the matches as a /news-style list.

        Usage: /mdn <keywords>
        The public MDN search API supplies the titles, summaries, and matching
        snippets. The browser reuses the /news story-list and detail modal, so
        a result can be translated, read aloud, or opened on MDN.
        """
        keyword = " ".join(argument.split())
        if not keyword:
            self._sse_text(
                "output",
                "\nUsage: /mdn <keywords>\n"
                "Example: /mdn fetch api\n"
                "Example: /mdn javascript promises\n",
            )
            return

        self._sse_text(
            "log", f"[mdn: searching developer.mozilla.org for {keyword!r}]"
        )
        try:
            results = search_mdn_docs(keyword)
        except RuntimeError as exc:
            self._sse_text("error", f"\n/mdn: {exc}\n")
            return

        self._sse(
            "news",
            {
                "type": "start",
                "source": "MDN Web Docs",
                "feed": MDN_SEARCH_URL,
                "sources": ["MDN Web Docs"],
                "docs": True,
            },
        )
        for index, result in enumerate(results, 1):
            self._sse(
                "news",
                {
                    "type": "item",
                    "index": index,
                    "title": result["title"],
                    "link": result["link"],
                    "source": "MDN Web Docs",
                    "summary": result["summary"],
                    "docs": True,
                },
            )
            paragraphs = [
                text
                for text in [result["summary"], *result["paragraphs"]]
                if text
            ]
            seen: set[str] = set()
            for text in paragraphs:
                if text in seen:
                    continue
                seen.add(text)
                self._sse(
                    "news",
                    {"type": "paragraph", "index": index, "text": text},
                )
            self._sse("news", {"type": "item_end", "index": index})
        self._sse("news", {"type": "done"})
        if not results:
            self._sse_text(
                "output", f"\n/mdn: no documentation matched {keyword!r}.\n"
            )

    def _command_britanica(self, argument: str) -> None:
        """Search Encyclopædia Britannica and stream the top five matches.

        Usage: /britanica <keyword>
        The canonical Britannica search page supplies article titles, links,
        and snippets. Because that page is bot-challenged for non-browser
        clients, the same query transparently falls back to the Britannica
        school search portal. The browser reuses the /news story-list and
        detail modal, so a result can be translated, read aloud, or opened on
        Britannica.
        """
        keyword = " ".join(argument.split())
        if not keyword:
            self._sse_text(
                "output",
                "\nUsage: /britanica <keyword>\n"
                "Example: /britanica black holes\n"
                "Example: /britanica Carl Barks\n",
            )
            return

        self._sse_text(
            "log", f"[britanica: searching britannica.com for {keyword!r}]"
        )
        try:
            results = search_britannica_articles(keyword)
        except RuntimeError as exc:
            self._sse_text("error", f"\n/britanica: {exc}\n")
            return

        self._sse(
            "news",
            {
                "type": "start",
                "source": BRITANNICA_SOURCE_LABEL,
                "feed": BRITANNICA_SEARCH_URL,
                "sources": [BRITANNICA_SOURCE_LABEL],
                "docs": True,
                "docs_label": "Britannica",
            },
        )
        for index, result in enumerate(results, 1):
            self._sse(
                "news",
                {
                    "type": "item",
                    "index": index,
                    "title": result["title"],
                    "link": result["link"],
                    "source": BRITANNICA_SOURCE_LABEL,
                    "summary": result["summary"],
                    "docs": True,
                    "docs_label": "Britannica",
                },
            )
            if result["summary"]:
                self._sse(
                    "news",
                    {
                        "type": "paragraph",
                        "index": index,
                        "text": result["summary"],
                    },
                )
            self._sse("news", {"type": "item_end", "index": index})
        self._sse("news", {"type": "done"})
        if not results:
            self._sse_text(
                "output",
                f"\n/britanica: no results matched {keyword!r}.\n",
            )

    def _command_stackoverflow(self, argument: str) -> None:
        """Search Stack Overflow and stream the questions like /news search.

        Usage: /stackoverflow tag <tag>
               /stackoverflow search <keywords>
        The public Stack Exchange API supplies the newest questions for a tag
        or the newest keyword matches. The browser reuses the /news
        result list and detail modal, so each question opens with its body,
        tags, score, and answer count and links to stackoverflow.com.
        """
        stripped = argument.strip()
        match = re.match(r"(?i)^(tag|search)(?:\s+(.*))?$", stripped)
        mode = match.group(1).lower() if match else ""
        value = (match.group(2) or "").strip() if match else ""
        if not mode or not value:
            self._sse_text(
                "output",
                "\nUsage: /stackoverflow tag <tag>\n"
                "       /stackoverflow search <keywords>\n"
                "Example: /stackoverflow tag python\n"
                "Example: /stackoverflow search rust borrow checker\n",
            )
            return

        self._sse_text(
            "log",
            f"[stackoverflow: searching Stack Overflow for {mode} {value!r}]",
        )
        try:
            if mode == "tag":
                results = search_stackoverflow_questions(tagged=value)
            else:
                results = search_stackoverflow_questions(query=value)
        except RuntimeError as exc:
            self._sse_text("error", f"\n/stackoverflow: {exc}\n")
            return

        feed = (
            results[0]["feed"]
            if results
            else f"{STACKOVERFLOW_BASE_URL}/questions"
        )
        self._sse(
            "news",
            {
                "type": "start",
                "source": "Stack Overflow",
                "feed": feed,
                "sources": ["Stack Overflow"],
                "docs": True,
                "docs_label": "Stack Overflow",
            },
        )
        for index, result in enumerate(results, 1):
            self._sse(
                "news",
                {
                    "type": "item",
                    "index": index,
                    "title": result["title"],
                    "link": result["link"],
                    "source": "Stack Overflow",
                    "section": result["section"],
                    "published": result["published"],
                    "summary": result["summary"],
                    "question_id": result.get("question_id"),
                    "score": result.get("score", 0),
                    "answer_count": result.get("answer_count", 0),
                    "accepted_answer_id": result.get("accepted_answer_id"),
                    "blocks": result.get("blocks") or [],
                    "docs": True,
                    "docs_label": "Stack Overflow",
                },
            )
            seen: set[str] = set()
            for text in result["paragraphs"]:
                if text in seen:
                    continue
                seen.add(text)
                self._sse(
                    "news", {"type": "paragraph", "index": index, "text": text}
                )
            self._sse("news", {"type": "item_end", "index": index})
        self._sse("news", {"type": "done"})
        if not results:
            self._sse_text(
                "output",
                f"\n/stackoverflow: no questions matched {value!r}.\n",
            )

    def _command_wikivoyage(self, argument: str) -> None:
        """Search Wikivoyage destinations and stream a /news-style list.

        Usage: /wikivoyage <keywords>
        The Wikivoyage search API supplies the destination cards, and each
        destination summary adds its overview extract and coordinates. The
        browser reuses the /news result list and detail modal, so a card can
        be read, translated, learned, chatted with, and opened on Wikivoyage.
        """
        keyword = " ".join(argument.split())
        if not keyword:
            self._sse_text(
                "output",
                "\nUsage: /wikivoyage <keywords>\n"
                "Example: /wikivoyage Kyoto\n"
                "Example: /wikivoyage hiking in Japan\n",
            )
            return

        self._sse_text(
            "log", f"[wikivoyage: searching en.wikivoyage.org for {keyword!r}]"
        )
        try:
            results = search_wikivoyage_destinations(keyword)
        except RuntimeError as exc:
            self._sse_text("error", f"\n/wikivoyage: {exc}\n")
            return

        self._sse(
            "news",
            {
                "type": "start",
                "source": "Wikivoyage",
                "feed": WIKIVOYAGE_SEARCH_URL,
                "sources": ["Wikivoyage"],
                "docs": True,
                "docs_label": "Wikivoyage",
            },
        )
        for index, result in enumerate(results, 1):
            # A destination article can be learned exactly like a news story,
            # so attach the same stable id and learned status that /news items
            # carry. The browser then adds the Learn/Chat rail to the modal.
            news_id = news_id_for_item(result)
            self._sse(
                "news",
                {
                    "type": "item",
                    "index": index,
                    "title": result["title"],
                    "link": result["link"],
                    "source": "Wikivoyage",
                    "summary": result["summary"],
                    "images": result["images"],
                    "docs": True,
                    "docs_label": "Wikivoyage",
                    "learnable": True,
                    "news_id": news_id,
                    "learned": news_is_learned(news_id),
                    "article_api": "/api/wikivoyage_article",
                },
            )
            seen: set[str] = set()
            for text in result["paragraphs"]:
                if text in seen:
                    continue
                seen.add(text)
                self._sse(
                    "news", {"type": "paragraph", "index": index, "text": text}
                )
            self._sse("news", {"type": "item_end", "index": index})
        self._sse("news", {"type": "done"})
        if not results:
            self._sse_text(
                "output", f"\n/wikivoyage: no destinations matched {keyword!r}.\n"
            )

    def _command_game(self, argument: str) -> None:
        """List the available explorer games or start one.

        Usage: /game [<game_name>]
        With no argument the command lists every prompt in ./prompts/games.
        A known game emits one "game" SSE event; the browser opens a
        full-screen Leaflet explorer and loads nearby OSM places as the
        player moves from marker to marker.
        """
        games = list_game_prompts()
        requested = " ".join(argument.split()).strip()
        if not requested:
            if not games:
                self._sse_text(
                    "output",
                    "\nNo games are available in ./prompts/games yet.\n",
                )
                return
            lines = ["", "Games:"]
            for game in games:
                suffix = "" if game["playable"] else " (prompt only)"
                lines.append(
                    f"  /game {game['name']:<20} {game['title']}{suffix}"
                )
            lines.append("")
            self._sse_text("output", "\n".join(lines) + "\n")
            return

        normalized = re.sub(
            r"[^a-z0-9]+", "_", requested.lower()
        ).strip("_")
        compact = normalized.replace("_", "")
        match = next(
            (
                game
                for game in games
                if game["name"] == normalized
                or game["name"].replace("_", "") == compact
            ),
            None,
        )
        if match is None:
            available = ", ".join(
                f"/game {game['name']}" for game in games
            ) or "none"
            self._sse_text(
                "output",
                f"\nUnknown game: {requested}.\n"
                f"Available games: {available}\n",
            )
            return

        start = GAME_START_LOCATIONS.get(match["name"])
        if start is None:
            self._sse_text(
                "output",
                f"\nGame {match['title']} is not playable yet.\n",
            )
            return
        saved_start = saved_game_start(match["name"])
        game_bounds = match.get("bounds")
        if saved_start is not None and _game_bounds_contain(
            game_bounds,
            saved_start["latitude"],
            saved_start["longitude"],
        ):
            start = {**start, **saved_start}

        self._sse(
            "game",
            {
                "type": "start",
                "game": match["name"],
                "title": match["title"],
                "youtube_video_search_prefix": match.get(
                    "youtube_video_search_prefix", ""
                ),
                "start": {
                    "name": start["name"],
                    "latitude": start["latitude"],
                    "longitude": start["longitude"],
                    "link": start["link"],
                    "category": start.get("category", "Starting point"),
                },
                "radius_meters": GAME_NEARBY_RADIUS_METERS,
                "limit": GAME_NEARBY_LIMIT,
                "bounds": game_bounds,
            },
        )

    def _command_map(self, argument: str) -> None:
        """Geocode a location and list nearby Wikipedia articles.

        Usage: /map <location>
        OpenStreetMap Nominatim resolves the location, then the MediaWiki
        GeoSearch API lists English Wikipedia pages within 10 km. The browser
        renders them with the /news-style result list and detail modal.
        """
        location = " ".join(argument.split())
        if not location:
            self._sse_text(
                "output",
                "\nUsage: /map <location>\n"
                "Example: /map Ferry Building San Francisco\n"
                "Example: /map Nijo Market Sapporo\n",
            )
            return

        self._sse_text(
            "log",
            f"[map: geocoding {location!r} with OpenStreetMap Nominatim]",
        )
        try:
            geo, results = search_map_places(location)
        except RuntimeError as exc:
            self._sse_text("error", f"\n/map: {exc}\n")
            return

        description = (
            f"{geo['display_name']} · "
            f"{geo['latitude']:.5f}, {geo['longitude']:.5f} · "
            f"places within {MAP_SEARCH_RADIUS_METERS / 1000:g} km · "
            f"restaurants within {MAP_RESTAURANT_RADIUS_METERS / 1000:g} km"
        )
        self._sse(
            "news",
            {
                "type": "start",
                "source": "Nearby Places",
                "feed": WIKIPEDIA_API_URL,
                "sources": ["Wikipedia"],
                "docs": True,
                "docs_label": "Wikipedia",
                "description": description,
                "tabs": [
                    {"id": "places", "label": "Nearby Places"},
                    {"id": "restaurants", "label": "Nearby Restaurants"},
                    {"id": "to_location", "label": "To location"},
                ],
                "location": {
                    "display_name": geo["display_name"],
                    "latitude": geo["latitude"],
                    "longitude": geo["longitude"],
                    "radius_meters": MAP_RESTAURANT_RADIUS_METERS,
                },
            },
        )
        for index, result in enumerate(results, 1):
            self._sse(
                "news",
                {
                    "type": "item",
                    "index": index,
                    "title": result["title"],
                    "link": result["link"],
                    "source": "Wikipedia",
                    "section": result["section"],
                    "summary": result["summary"],
                    "images": result["images"],
                    "latitude": result["latitude"],
                    "longitude": result["longitude"],
                    "docs": True,
                    "docs_label": "Wikipedia",
                    "learnable": False,
                    "map": True,
                },
            )
            for text in result["paragraphs"]:
                self._sse(
                    "news", {"type": "paragraph", "index": index, "text": text}
                )
            self._sse("news", {"type": "item_end", "index": index})
        self._sse("news", {"type": "done"})
        if not results:
            self._sse_text(
                "output",
                f"\n/map: no Wikipedia articles were found within "
                f"{MAP_SEARCH_RADIUS_METERS / 1000:g} km of "
                f"{geo['display_name']}.\n",
            )

    def _command_changelog(self, argument: str) -> None:
        """List the completed-task summary pages, newest first.

        Usage: /changelog
        Every entry is one .logs/*.summary.html page. The browser renders the
        list and asks /api/changelog_file for the selected page when its row is
        clicked, so the modal receives the original HTML document.
        """
        entries = list_changelog_entries()
        self._sse(
            "changelog",
            {
                "type": "start",
                "directory": str(changelog_directory() or LOGS_DIR),
                "count": len(entries),
            },
        )
        for index, entry in enumerate(entries, 1):
            self._sse(
                "changelog",
                {"type": "item", "index": index, **entry},
            )
        self._sse("changelog", {"type": "done"})
        if not entries:
            self._sse_text(
                "output",
                "\nNo completed-task summaries were found in .logs.\n",
            )


# Cross-component imports are placed after the definitions so a
# feature module can depend on another feature module safely.
from .changelog import changelog_directory, list_changelog_entries
from .core import LOGS_DIR
from .docs import BRITANNICA_SEARCH_URL, BRITANNICA_SOURCE_LABEL, MDN_SEARCH_URL, search_britannica_articles, search_mdn_docs
from .game import _game_bounds_contain, list_game_prompts, saved_game_start
from .map import GAME_NEARBY_LIMIT, GAME_NEARBY_RADIUS_METERS, GAME_START_LOCATIONS, MAP_RESTAURANT_RADIUS_METERS, MAP_SEARCH_RADIUS_METERS, WIKIPEDIA_API_URL, search_map_places
from .news_research import NEWS_SOURCES, NEWS_SOURCE_USAGE, split_news_search_website
from .retrieval import news_id_for_item, news_is_learned
from .stackoverflow import STACKOVERFLOW_BASE_URL, search_stackoverflow_questions
from .wikivoyage import WIKIVOYAGE_SEARCH_URL, search_wikivoyage_destinations
