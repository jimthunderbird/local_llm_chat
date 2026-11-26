"""Reddit thread search and detail extraction."""

from __future__ import annotations

from .core import *

# /reddit <keywords> searches Google News RSS restricted to reddit.com. The
# result cards reuse the /news list, and each post body is fetched on demand
# from Reddit's public JSON (or Atom RSS) endpoint.
REDDIT_USER_AGENT = (
    "python:personal-chatbot:v1.0 (by /u/personal_chatbot)"
)

REDDIT_REQUEST_TIMEOUT = 30

REDDIT_MAX_COMMENTS = 40

REDDIT_MAX_BLOCKS = 500

# Reddit's legacy `.embed` endpoint is the iframe friendly rendering of a
# thread. It is intentionally used as the last server-side fallback: the
# browser cannot read a cross-origin iframe's DOM, so the server fetches the
# same embed document, extracts the comments, and the UI also offers the live
# embed in an iframe when the JSON/Atom endpoints are unavailable.
REDDIT_EMBED_QUERY = "?limit=100&depth=2&sort=top"

# /reddit searches site:reddit.com first, then repeats the search inside each
# Reddit community suggested for the query's main entities. Results from all
# searches are merged, de-duplicated, and renumbered into one list.
REDDIT_MAIN_LIMIT = 20

REDDIT_ENTITY_LIMIT = 10

REDDIT_MAX_RESULTS = 60

REDDIT_MAX_ENTITY_QUERIES = 3

REDDIT_ENTITY_MODEL = chat.ANSWER_MODEL

REDDIT_ENTITY_PROMPT = """Extract the main named entities and topics from this Reddit search query.
Return a JSON array of up to 3 short strings. Each string must be a plausible
subreddit name: one word, letters/digits/underscores only, no r/ prefix.
Return only the JSON array.
Query: {query}"""

REDDIT_ENTITY_TOKEN_RE = re.compile(r"[A-Za-z0-9_]+")

REDDIT_ENTITY_STOPWORDS = frozenset(
    "a an the and or but if then than that this these those there here "
    "what which who whom whose when where why how is are was were be been "
    "being do does did done have has had can could should would will may "
    "might must shall to of in on at for from with about into over after "
    "before under between during any some all more most other another "
    "such only just also very much many few own same so not no nor too "
    "best top good great new latest current review reviews guide help "
    "please tell show give me my i you your we our us them they he she it "
    "reddit subreddit post posts thread threads comment comments".split()
)

GOOGLE_NEWS_ARTICLE_RE = re.compile(
    r"^https?://news\.google\.com/(?:rss/)?articles/", re.IGNORECASE
)

GOOGLE_NEWS_LOCALE_QUERY = "hl=en-US&gl=US&ceid=US:en"

# The main news.google.com wrapper endpoint can answer HTTP 429 with Google's
# "automated queries" page while the article id remains resolvable through a
# regional Google News edge. Try the English regional endpoints first, then
# the US endpoint as a final check.
GOOGLE_NEWS_WRAPPER_ENDPOINTS = (
    ("news.google.co.uk", "hl=en-GB&gl=GB&ceid=GB:en"),
    ("news.google.co.jp", "hl=ja&gl=JP&ceid=JP:ja"),
    ("news.google.com.au", "hl=en-AU&gl=AU&ceid=AU:en"),
    ("news.google.de", "hl=de&gl=DE&ceid=DE:de"),
    ("news.google.com", GOOGLE_NEWS_LOCALE_QUERY),
)

GOOGLE_NEWS_USER_AGENT = (
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
    "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/131.0.0.0 Safari/537.36"
)

# Resolving a wrapper takes two Google requests. Cache successful publisher
# URLs by article id so a repeated /news or /reddit result does not make the
# same handshake again (and does not contribute to Google's rate limiting).
_GOOGLE_NEWS_RESOLUTION_CACHE: dict[str, tuple[float, str]] = {}

_GOOGLE_NEWS_RESOLUTION_TTL = 86400

def _is_reddit_url(url: str) -> bool:
    host = (urlparse(url).hostname or "").lower()
    return host == "redd.it" or host == "reddit.com" or host.endswith(
        ".reddit.com"
    )

def _reddit_canonical_url(url: str) -> str:
    """Return the public www.reddit.com thread URL without JSON/RSS suffix."""
    parts = urlparse(url)
    host = (parts.hostname or "").lower()
    path = re.sub(r"\.(?:json|rss)$", "", parts.path or "/", flags=re.I)
    if host == "redd.it":
        post_id = (parts.path or "").strip("/").split("/")[0]
        if post_id:
            return f"https://www.reddit.com/comments/{post_id}/"
    path = re.sub(r"/+", "/", path)
    if not path.startswith("/"):
        path = "/" + path
    if not path.endswith("/"):
        path += "/"
    return "https://www.reddit.com" + path

def _reddit_section(url: str) -> str:
    match = re.search(
        r"/r/([^/]+)/comments/", urlparse(url).path, re.IGNORECASE
    )
    return f"r/{match.group(1)}" if match else ""

def _reddit_published_timestamp(value: object) -> float | None:
    """Parse a feed publish date into an epoch timestamp for sorting."""
    text = str(value or "").strip()
    if not text:
        return None
    try:
        return datetime.fromisoformat(text.replace("Z", "+00:00")).timestamp()
    except ValueError:
        pass
    try:
        return parsedate_to_datetime(text).timestamp()
    except (TypeError, ValueError, OverflowError):
        return None

def _reddit_result_sort_key(entry: dict) -> tuple[bool, float]:
    """Sort newest published posts first, with undated posts last."""
    timestamp = _reddit_published_timestamp(
        entry.get("payload", {}).get("published")
    )
    return (timestamp is not None, timestamp or 0.0)

def collect_reddit_entries(
    queries: list[tuple[str, str]],
    *,
    log: Callable[[str], None] | None = None,
    error: Callable[[str], None] | None = None,
) -> list[dict]:
    """Run Reddit searches and return one merged, unique, newest-first list.

    ``queries`` holds ``(label, google_news_query)`` pairs. Each search is an
    independent ``news_fetch.php`` process. Duplicate threads are dropped by
    canonical Reddit URL (or by normalized title for Google News wrapper
    links), then the merged list is sorted newest-first by publish date. Every
    post is labelled with the subreddit it belongs to; Google News wrapper
    links are resolved in parallel so the label is the real community rather
    than the search query.

    ``log`` and ``error`` receive progress/error text. The /reddit command
    forwards both to the SSE stream, while the /stock Reddit tab just wants the
    finished payloads.
    """
    def emit_log(text: str) -> None:
        if log is not None:
            log(text)

    def emit_error(text: str) -> None:
        if error is not None:
            error(text)

    script = SCRIPT_DIR / "news_fetch.php"
    if not script.exists():
        raise RuntimeError("missing news_fetch.php")

    collected: list[dict] = []
    key_to_entry: dict[str, dict] = {}
    for position, (label, query) in enumerate(queries):
        if position:
            emit_log(f"[reddit: searching {query!r}]")
        limit = REDDIT_MAIN_LIMIT if position == 0 else REDDIT_ENTITY_LIMIT
        argv = [
            "php",
            str(script),
            "--json",
            "--lazy",
            "--source",
            "googlenews",
            "--search",
            query,
            "--limit",
            str(limit),
        ]
        process = None
        returncode = 0
        active: dict[int, dict] = {}
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
            for raw_line in process.stdout:
                line = raw_line.strip()
                if not line:
                    continue
                try:
                    payload = json.loads(line)
                except json.JSONDecodeError:
                    emit_log(f"[reddit: {line}]")
                    continue
                if not isinstance(payload, dict):
                    continue
                kind = payload.get("type")
                if kind in {"start", "done"}:
                    # One start/done pair is emitted for the whole merge.
                    continue
                if kind == "warning":
                    message = str(payload.get("message") or "").strip()
                    if message:
                        emit_log(f"[reddit: {message}]")
                    continue
                if kind == "error":
                    message = str(payload.get("message") or "").strip()
                    if message:
                        emit_error(f"\n/reddit: {message}\n")
                    continue

                try:
                    source_index = int(payload.get("index"))
                except (TypeError, ValueError):
                    continue

                if kind == "item":
                    keys = _reddit_result_keys(payload)
                    if not keys:
                        continue
                    existing = next(
                        (
                            key_to_entry[key]
                            for key in keys
                            if key in key_to_entry
                        ),
                        None,
                    )
                    if existing is not None:
                        if label not in existing["labels"]:
                            existing["labels"].append(label)
                        active[source_index] = existing
                        continue
                    entry = {
                        "payload": payload,
                        "labels": [label],
                        "paragraphs": [],
                        "ended": False,
                    }
                    collected.append(entry)
                    for key in keys:
                        key_to_entry[key] = entry
                    active[source_index] = entry
                    continue

                if kind in {"paragraph", "item_end"}:
                    entry = active.get(source_index)
                    if entry is None:
                        continue
                    if kind == "paragraph":
                        text = str(payload.get("text") or "").strip()
                        if text and text not in entry["paragraphs"]:
                            entry["paragraphs"].append(text)
                    else:
                        entry["ended"] = True
                    continue

            returncode = process.wait()
        except ClientDisconnected:
            if process is not None:
                process.terminate()
                try:
                    process.wait(timeout=5)
                except subprocess.TimeoutExpired:
                    process.kill()
                    process.wait()
            raise
        except OSError as exc:
            emit_error(f"\n/reddit: could not run {script.name}: {exc}\n")
            continue
        if returncode != 0:
            emit_error(
                f"\n/reddit: {script.name} exited with code "
                f"{returncode} while searching {query!r}.\n"
            )

    # Newest first; undated results (if any) stay at the end.
    collected.sort(key=_reddit_result_sort_key, reverse=True)
    selected = collected[:REDDIT_MAX_RESULTS]

    # Google News hides the Reddit thread behind a wrapper URL, so fetch
    # those wrappers concurrently to label each post with its real
    # subreddit. Direct reddit.com links need no network call.
    unresolved = [
        entry
        for entry in selected
        if not _reddit_section(str(entry["payload"].get("link") or ""))
        and GOOGLE_NEWS_ARTICLE_RE.match(
            str(entry["payload"].get("link") or "")
        )
    ]
    if unresolved:
        with ThreadPoolExecutor(
            max_workers=min(8, len(unresolved))
        ) as executor:
            futures = {
                executor.submit(
                    resolve_google_news_url,
                    str(entry["payload"].get("link") or ""),
                ): entry
                for entry in unresolved
            }
            for future in as_completed(futures):
                entry = futures[future]
                try:
                    resolved = future.result()
                except Exception:
                    resolved = ""
                section = _reddit_section(resolved) if resolved else ""
                if section:
                    entry["resolved_section"] = section

    for emitted_index, entry in enumerate(selected, 1):
        payload = entry["payload"]
        link = str(payload.get("link") or "").strip()
        section = _reddit_section(link)
        if not section:
            section = str(entry.get("resolved_section") or "")
        if not section:
            # Fall back to the subreddit-specific query that found it.
            for label in entry["labels"]:
                if label.startswith("r/"):
                    section = label
                    break
        payload["index"] = emitted_index
        payload["source"] = "Reddit"
        payload["section"] = section
        payload["article_api"] = "/api/reddit_article"
        payload["docs"] = True
        payload["docs_label"] = "Reddit"
        payload["learnable"] = True
        payload["learn_noun"] = "post"
        payload["images"] = []
        try:
            news_id = news_id_for_item(payload)
        except ValueError:
            news_id = ""
        payload["news_id"] = news_id
        payload["learned"] = bool(news_id and news_is_learned(news_id))

    return selected

def stock_reddit_queries(symbol: str) -> list[tuple[str, str]]:
    """Return the two subreddit-restricted searches used by /stock.

    The first search covers r/stock, the second covers the ticker's own
    subreddit (for example r/AAPL). The symbol is the search topic in both
    communities so the Reddit tab stays focused on this exact stock.
    """
    cleaned = str(symbol or "").strip().upper()
    if not cleaned:
        return []
    subreddit = re.sub(r"[^A-Za-z0-9_]", "", cleaned)
    topic = _reddit_quote_phrase(cleaned)
    queries: list[tuple[str, str]] = [
        ("r/stock", f"site:reddit.com/r/stock {topic}")
    ]
    if subreddit and subreddit.casefold() != "stock":
        queries.append(
            (
                f"r/{subreddit}",
                f"site:reddit.com/r/{subreddit} {topic}",
            )
        )
    return queries

def fetch_stock_reddit_items(symbol: str) -> list[dict]:
    """Collect unique Reddit posts for one stock's Reddit tab."""
    queries = stock_reddit_queries(symbol)
    if not queries:
        return []
    entries = collect_reddit_entries(queries)
    items: list[dict] = []
    for entry in entries:
        payload = dict(entry["payload"])
        payload["complete"] = True
        payload["paragraphs"] = list(entry.get("paragraphs") or [])
        items.append(payload)
    return items

def _reddit_clean_entity(value: object) -> str:
    """Normalize one model/heuristic suggestion into a subreddit name."""
    text = str(value or "").strip().strip("'\"`[](),.")
    text = re.sub(r"^/?r/", "", text, flags=re.IGNORECASE)
    text = re.sub(r"[^A-Za-z0-9_]", "", text)
    if not (2 <= len(text) <= 30):
        return ""
    if text.lower() in REDDIT_ENTITY_STOPWORDS:
        return ""
    return text

def _reddit_fallback_entities(keyword: str) -> list[str]:
    """Derive plausible subreddit names without calling the model."""
    words = [
        word.lower()
        for word in REDDIT_ENTITY_TOKEN_RE.findall(keyword.lower())
        if len(word) > 1 and word.lower() not in REDDIT_ENTITY_STOPWORDS
    ]
    candidates: list[str] = []
    # Two-word names such as "python asyncio" or "jensen huang" are common
    # subreddit spellings, so try the joined form before the single words.
    candidates.extend(first + second for first, second in zip(words, words[1:]))
    candidates.extend(words)
    result: list[str] = []
    seen: set[str] = set()
    for candidate in candidates:
        cleaned = _reddit_clean_entity(candidate)
        if not cleaned or cleaned.lower() in seen:
            continue
        seen.add(cleaned.lower())
        result.append(cleaned)
        if len(result) >= REDDIT_MAX_ENTITY_QUERIES:
            break
    return result

def _reddit_entities_from_response(response: str, keyword: str) -> list[str]:
    """Parse an entity-model response and normalize it into subreddit names.

    The model is asked for a JSON array, but small/local models occasionally
    wrap it in prose or return a comma-separated line. Both forms are accepted,
    and an empty result falls back to deterministic keyword candidates.
    """
    candidates: list[object] = []
    match = re.search(r"\[.*?\]", response, re.DOTALL)
    if match:
        try:
            parsed = json.loads(match.group(0))
        except json.JSONDecodeError:
            parsed = None
        if isinstance(parsed, list):
            candidates.extend(parsed)
    if not candidates:
        candidates.extend(re.split(r"[,\n]", response))

    result: list[str] = []
    seen: set[str] = set()
    for candidate in candidates:
        cleaned = _reddit_clean_entity(candidate)
        if not cleaned or cleaned.lower() in seen:
            continue
        seen.add(cleaned.lower())
        result.append(cleaned)
        if len(result) >= REDDIT_MAX_ENTITY_QUERIES:
            break
    if not result:
        result = _reddit_fallback_entities(keyword)
    return result

@lru_cache(maxsize=256)
def _reddit_entities(keyword: str) -> tuple[str, ...]:
    """Return up to three subreddit names related to a /reddit query.

    The 35B answer model is good at turning "best ramen in tokyo" into
    ``tokyo, ramen, JapanFood`` and "jensen huang" into ``nvidia, ...``. A
    deterministic keyword fallback keeps the command useful when Ollama is
    unavailable or returns prose instead of the requested JSON array.
    """
    keyword = " ".join(keyword.split())
    if not keyword:
        return ()
    try:
        response = chat.generate_text(
            REDDIT_ENTITY_PROMPT.format(query=keyword),
            model=REDDIT_ENTITY_MODEL,
            temperature=0.0,
            num_predict=80,
        )
    except (RuntimeError, ValueError):
        response = ""
    return tuple(_reddit_entities_from_response(response, keyword))

def _reddit_entities_streaming(
    keyword: str, emit: Callable[[str], None] | None = None
) -> tuple[str, ...]:
    """Extract related subreddits while streaming the model's raw output.

    ``/reddit`` calls this variant so the browser can show the entity list
    being produced instead of waiting for an unlabelled pause. ``emit`` receives
    each token as Ollama returns it; the accumulated text is then parsed with
    the same rules as :func:`_reddit_entities`.
    """
    keyword = " ".join(keyword.split())
    if not keyword:
        return ()
    payload = {
        "model": REDDIT_ENTITY_MODEL,
        "prompt": REDDIT_ENTITY_PROMPT.format(query=keyword),
        "stream": True,
        "think": False,
        "keep_alive": chat.OLLAMA_KEEP_ALIVE,
        "options": {
            "temperature": 0.0,
            "num_ctx": chat.ANSWER_NUM_CTX,
            "num_predict": 80,
        },
    }
    parts: list[str] = []
    try:
        with requests.post(
            f"{chat.OLLAMA_HOST}/api/generate",
            json=payload,
            stream=True,
            timeout=(10, 120),
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
                    break
                token = str(event.get("response") or "")
                if token:
                    parts.append(token)
                    if emit is not None:
                        emit(token)
                if event.get("done"):
                    break
    except requests.RequestException:
        pass
    return tuple(
        _reddit_entities_from_response("".join(parts), keyword)
    )

def _reddit_quote_phrase(keyword: str) -> str:
    """Return a query's keywords as one exact phrase.

    The related-subreddit searches repeat the same keywords that the main
    ``site:reddit.com`` search uses. Without quotes a multi-word query such as
    ``jensen huang`` is ranked as two independent terms, so an entity search
    can return posts that mention only one of them. Wrapping the whole query
    in double quotes keeps the words adjacent; an explicitly quoted query is
    left untouched so the user's own operators are preserved.
    """
    phrase = " ".join(str(keyword or "").split())
    if not phrase or '"' in phrase or "\u201c" in phrase or "\u201d" in phrase:
        return phrase
    return f'"{phrase}"'

def _reddit_result_keys(payload: dict) -> tuple[str, ...]:
    """Return stable keys for de-duplicating results across searches.

    Google News wrapper links do not expose the final Reddit URL, so a
    normalized title is used as a second identity. Tracking both keys also
    catches the same thread when one search returns a direct reddit.com link
    and another returns a news.google.com wrapper.
    """
    keys: list[str] = []
    link = str(payload.get("link") or "").strip()
    if link:
        if _is_reddit_url(link):
            keys.append(_reddit_canonical_url(link).lower())
        else:
            keys.append("link:" + link.lower())
    title = " ".join(
        REDDIT_ENTITY_TOKEN_RE.findall(str(payload.get("title") or "").lower())
    )
    if title:
        keys.append("title:" + title)
    return tuple(keys)

def resolve_google_news_url(url: str) -> str:
    """Resolve a news.google.com wrapper to its real publisher URL.

    Google exposes a timestamp and signature in the wrapper page that must be
    exchanged through its batchexecute endpoint. This mirrors the resolution
    news_fetch.php uses for Google News feeds, so a Reddit result or a stored
    story link can be opened without sending the browser to Google.
    """
    url = str(url or "").strip()
    match = re.match(
        r"^https?://news\.google\.com/(?:rss/)?articles/([^/?#]+)",
        url,
        re.IGNORECASE,
    )
    if not match:
        return ""
    article_id = match.group(1)
    cached = _GOOGLE_NEWS_RESOLUTION_CACHE.get(article_id)
    if cached and time.time() - cached[0] < _GOOGLE_NEWS_RESOLUTION_TTL:
        return cached[1]

    headers = {
        "User-Agent": GOOGLE_NEWS_USER_AGENT,
        "Accept": "text/html,application/xhtml+xml;q=0.9,*/*;q=0.8",
        "Accept-Language": "en-US,en;q=0.9",
    }
    timestamp = None
    signature = None
    for host, locale in GOOGLE_NEWS_WRAPPER_ENDPOINTS:
        page_url = (
            f"https://{host}/rss/articles/"
            f"{quote(article_id, safe='')}?{locale}"
        )
        try:
            page = requests.get(
                page_url, headers=headers, timeout=REDDIT_REQUEST_TIMEOUT
            )
        except requests.RequestException:
            continue
        if page.status_code >= 400:
            continue
        timestamp = re.search(r'data-n-a-ts="(\d+)"', page.text)
        signature = re.search(r'data-n-a-sg="([^"]+)"', page.text)
        if timestamp and signature:
            break
    if not timestamp or not signature:
        return ""

    garturlreq = [
        "garturlreq",
        [
            ["X", "X", ["X", "X"], None, None, 1, 1, "US:en", None, 1,
             None, None, None, None, None, 0, 1],
            "X",
            "X",
            1,
            [1, 1, 1],
            1,
            1,
            None,
            0,
            0,
            None,
            0,
        ],
        article_id,
        int(timestamp.group(1)),
        signature.group(1),
    ]
    inner = json.dumps(garturlreq, separators=(",", ":"))
    outer = json.dumps([[['Fbv4je', inner]]], separators=(",", ":"))
    try:
        response = requests.post(
            "https://news.google.com/_/DotsSplashUi/data/batchexecute?"
            + GOOGLE_NEWS_LOCALE_QUERY,
            data={"f.req": outer},
            headers={
                "User-Agent": GOOGLE_NEWS_USER_AGENT,
                "Content-Type": (
                    "application/x-www-form-urlencoded;charset=UTF-8"
                ),
                "Accept": "*/*",
            },
            timeout=REDDIT_REQUEST_TIMEOUT,
        )
        response.raise_for_status()
    except requests.RequestException:
        return ""
    body = response.text
    if body.startswith(")]}'"):
        body = body[4:].lstrip()
    try:
        decoded = json.loads(body)
    except json.JSONDecodeError:
        return ""
    if not isinstance(decoded, list):
        return ""
    for entry in decoded:
        if not isinstance(entry, list) or len(entry) < 3:
            continue
        if entry[0] != "wrb.fr" or entry[1] != "Fbv4je":
            continue
        try:
            inner_data = json.loads(str(entry[2]))
        except (TypeError, json.JSONDecodeError):
            continue
        if (
            isinstance(inner_data, list)
            and inner_data
            and inner_data[0] == "garturlres"
            and isinstance(inner_data[1], str)
            and re.match(r"^https?://", inner_data[1], re.IGNORECASE)
        ):
            _GOOGLE_NEWS_RESOLUTION_CACHE[article_id] = (
                time.time(),
                inner_data[1],
            )
            return inner_data[1]
    return ""

def _reddit_plain_blocks(text: str) -> list[dict]:
    """Turn Reddit self-text markdown outside code fences into reader blocks."""
    blocks: list[dict] = []
    for raw in re.split(r"\n\s*\n+", str(text or "").strip()):
        chunk = raw.strip()
        if not chunk:
            continue
        lines = [line.rstrip() for line in chunk.splitlines() if line.strip()]
        if not lines:
            continue
        heading = re.match(r"^(#{1,6})\s+(.*)$", lines[0])
        if heading:
            blocks.append(
                {
                    "type": "heading",
                    "level": len(heading.group(1)),
                    "text": heading.group(2).strip(),
                }
            )
            lines = lines[1:]
            if not lines:
                continue
        matches = [
            re.match(r"^\s*(?:([-*+])|(\d+)[.)])\s+(.*)$", line)
            for line in lines
        ]
        if matches and all(matches):
            items = [
                match.group(3).strip()
                for match in matches
                if match and match.group(3).strip()
            ]
            if items:
                blocks.append(
                    {
                        "type": "list",
                        "ordered": bool(matches[0].group(2)),
                        "items": items,
                    }
                )
            continue
        blocks.append(
            {"type": "paragraph", "text": " ".join(line.strip() for line in lines)}
        )
    return blocks

def _reddit_markdown_blocks(text: str) -> list[dict]:
    """Split Reddit self-text markdown into paragraphs, lists, and code blocks."""
    text = html.unescape(
        str(text or "").replace("\r\n", "\n").replace("\r", "\n")
    )
    blocks: list[dict] = []
    position = 0
    fence = re.compile(r"(?ms)^```([^\n`]*)\n(.*?)^```\s*$")
    for match in fence.finditer(text):
        blocks.extend(_reddit_plain_blocks(text[position : match.start()]))
        code = match.group(2).rstrip("\n")
        if code.strip():
            blocks.append(
                {
                    "type": "code",
                    "text": code,
                    "language": match.group(1).strip(),
                }
            )
        position = match.end()
    blocks.extend(_reddit_plain_blocks(text[position:]))
    return blocks

def _reddit_blocks_from_html(document: str) -> list[dict]:
    parser = _StackOverflowBodyParser()
    parser.feed(str(document or ""))
    parser.close()
    return parser.blocks

def _reddit_clean_blocks(blocks: list[dict]) -> list[dict]:
    cleaned: list[dict] = []
    for block in blocks:
        if not isinstance(block, dict):
            continue
        kind = block.get("type")
        if kind in {"paragraph", "heading"}:
            text = str(block.get("text") or "").strip()
            if not text:
                continue
            lowered = text.lower()
            if lowered in {"[link]", "[comments]", "[removed]", "[deleted]"}:
                continue
            if "submitted by" in lowered and (
                "[link]" in lowered or "[comments]" in lowered
            ):
                continue
            block = {**block, "text": text}
        elif kind == "list":
            items = [
                str(item).strip()
                for item in (block.get("items") or [])
                if str(item).strip()
            ]
            if not items:
                continue
            block = {**block, "items": items}
        elif kind == "code":
            if not str(block.get("text") or "").strip():
                continue
        else:
            continue
        cleaned.append(block)
        if len(cleaned) >= REDDIT_MAX_BLOCKS:
            break
    return cleaned

def _reddit_paragraphs_from_blocks(blocks: list[dict]) -> list[str]:
    paragraphs: list[str] = []
    for block in blocks:
        kind = block.get("type")
        if kind in {"paragraph", "heading", "code"}:
            text = str(block.get("text") or "").strip()
        elif kind == "list":
            text = "\n".join(
                str(item).strip() for item in block.get("items") or []
            ).strip()
        else:
            text = ""
        if text:
            paragraphs.append(text)
    return paragraphs

def _reddit_embed_url(canonical: str) -> str:
    """Return the iframe-friendly URL for a canonical Reddit thread."""
    return canonical.rstrip("/") + "/.embed" + REDDIT_EMBED_QUERY

def _reddit_iframe_url(url: str) -> str:
    """Resolve a feed link to a Reddit embed URL suitable for an iframe."""
    value = str(url or "").strip()
    if GOOGLE_NEWS_ARTICLE_RE.match(value):
        value = resolve_google_news_url(value)
    if not value or not _is_reddit_url(value):
        return ""
    return _reddit_embed_url(_reddit_canonical_url(value))

def _reddit_embed_document(document: str) -> str:
    """Unwrap Reddit's ``document.write("...")`` embed response into HTML."""
    text = str(document or "").strip()
    if not text:
        return ""
    literal = re.search(
        r'document\.write\(\s*("(?:[^"\\]|\\.)*")\s*\)', text, re.S
    )
    if literal:
        raw = literal.group(1)
        try:
            text = json.loads(raw)
        except json.JSONDecodeError:
            try:
                text = bytes(raw[1:-1], "utf-8").decode("unicode_escape")
            except UnicodeDecodeError:
                return ""
    return text if "<" in text else ""

class _RedditEmbedParser(HTMLParser):
    """Extract author, score, and body blocks from Reddit's iframe embed.

    The legacy embed is an HTML fragment wrapped in ``document.write``. It is
    the only Reddit endpoint that is consistently embeddable and not blocked
    on this network. Its comments are nested ``div.reddit-link`` elements with
    a ``div.md`` body; capturing the inner HTML of each body and reusing the
    Stack Overflow block parser keeps code and lists instead of flattening
    everything to one paragraph.
    """

    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.records: list[dict] = []
        self._link_stack: list[dict] = []
        self._div_stack: list[bool] = []
        self._capture: dict | None = None
        self._author_parts: list[str] | None = None
        self._score_parts: list[str] | None = None
        self._small_depth = 0

    def _finish_capture(self) -> None:
        capture = self._capture
        if not capture:
            return
        self._capture = None
        fragment = "".join(capture["parts"])
        owner = capture["owner"]
        if not isinstance(owner, dict):
            return
        blocks = _reddit_clean_blocks(_reddit_blocks_from_html(fragment))
        if blocks:
            owner["blocks"].extend(blocks)

    def handle_starttag(
        self, tag: str, attrs: list[tuple[str, str | None]]
    ) -> None:
        tag = tag.lower()
        attributes = {name: value for name, value in attrs}
        classes = str(attributes.get("class") or "").split()
        if self._capture is not None:
            self._capture["parts"].append(self.get_starttag_text() or "")
            if tag == "div":
                self._capture["div_depth"] += 1
            return
        if tag == "div" and "md" in classes:
            owner = self._link_stack[-1] if self._link_stack else None
            if owner is None:
                owner = {
                    "author": "",
                    "score": "",
                    "blocks": [],
                    "depth": 0,
                }
                self.records.append(owner)
            self._capture = {
                "parts": [],
                "div_depth": 1,
                "owner": owner,
            }
            return
        if tag == "div":
            is_link = "reddit-link" in classes
            self._div_stack.append(is_link)
            if is_link:
                record = {
                    "author": "",
                    "score": "",
                    "blocks": [],
                    "depth": len(self._link_stack),
                }
                self.records.append(record)
                self._link_stack.append(record)
            return
        if tag == "small" and self._link_stack:
            self._small_depth += 1
            return
        if self._small_depth and self._link_stack:
            if tag == "a":
                href = str(attributes.get("href") or "")
                if "/user/" in href:
                    self._author_parts = []
            elif tag == "span":
                span_id = str(attributes.get("id") or "")
                if span_id.startswith("score_"):
                    self._score_parts = []

    def handle_endtag(self, tag: str) -> None:
        tag = tag.lower()
        if self._capture is not None:
            self._capture["parts"].append(f"</{tag}>")
            if tag == "div":
                self._capture["div_depth"] -= 1
                if self._capture["div_depth"] <= 0:
                    self._finish_capture()
            return
        if tag == "a" and self._author_parts is not None:
            author = " ".join("".join(self._author_parts).split())
            if author and self._link_stack:
                self._link_stack[-1]["author"] = author
            self._author_parts = None
            return
        if tag == "span" and self._score_parts is not None:
            score = " ".join("".join(self._score_parts).split())
            if score and self._link_stack:
                self._link_stack[-1]["score"] = score
            self._score_parts = None
            return
        if tag == "small" and self._small_depth:
            self._small_depth -= 1
            return
        if tag == "div" and self._div_stack:
            if self._div_stack.pop() and self._link_stack:
                self._link_stack.pop()

    def handle_data(self, data: str) -> None:
        if self._capture is not None:
            self._capture["parts"].append(html.escape(data))
            return
        if self._author_parts is not None:
            self._author_parts.append(data)
        elif self._score_parts is not None:
            self._score_parts.append(data)

def _reddit_post_from_embed(
    document: str, canonical: str, fallback_title: str
) -> dict:
    """Extract the post and comments from Reddit's iframe embed endpoint."""
    embed_html = _reddit_embed_document(document)
    if not embed_html:
        raise RuntimeError("Reddit returned an empty embed document")
    parser = _RedditEmbedParser()
    parser.feed(embed_html)
    parser.close()

    post_blocks: list[dict] = []
    comment_blocks: list[dict] = []
    comments = 0
    for record in parser.records:
        record_blocks = _reddit_clean_blocks(record.get("blocks") or [])
        if not record_blocks:
            continue
        author = str(record.get("author") or "").strip()
        score = str(record.get("score") or "").strip()
        if not author:
            # The embed can include a body outside a comment link. Use the
            # first such body as the post instead of mislabelling it as a
            # comment.
            if not post_blocks:
                post_blocks.extend(record_blocks)
                continue
            label = "Comment"
        else:
            label = _reddit_author_label(author, "Comment")
        if score:
            label += f" · {score}"
        if comments >= REDDIT_MAX_COMMENTS:
            break
        comments += 1
        comment_blocks.append({"type": "heading", "level": 3, "text": label})
        comment_blocks.extend(record_blocks)

    if not post_blocks and not comment_blocks:
        raise RuntimeError("Reddit returned no readable content in the embed")
    blocks = list(post_blocks)
    if comments:
        blocks.append(
            {"type": "heading", "level": 2, "text": f"Comments ({comments})"}
        )
        blocks.extend(comment_blocks)
    blocks = _reddit_clean_blocks(blocks)
    return {
        "link": canonical,
        "title": str(fallback_title or "").strip(),
        "blocks": blocks,
        "paragraphs": _reddit_paragraphs_from_blocks(blocks),
        "images": [],
        "partial": True,
    }

def _reddit_response(
    result: dict, canonical: str, source: str, partial: bool = False
) -> dict:
    """Attach the live iframe URL and the source used to extract a thread."""
    response = dict(result)
    response["iframe_url"] = _reddit_embed_url(canonical)
    response["source"] = source
    response["partial"] = bool(partial or result.get("partial"))
    return response

def _reddit_images(post: dict) -> list[str]:
    images: list[str] = []

    def add(value: object) -> None:
        if not isinstance(value, str):
            return
        value = html.unescape(value).strip()
        if re.search(r"\.(?:jpe?g|png|gif|webp)(?:\?|$)", value, re.I):
            images.append(value)

    add(post.get("url_overridden_by_dest"))
    add(post.get("url"))
    preview = post.get("preview")
    if isinstance(preview, dict):
        for image in preview.get("images") or []:
            if isinstance(image, dict) and isinstance(image.get("source"), dict):
                add(image["source"].get("url"))
    return list(dict.fromkeys(images))[:6]

def _reddit_author_label(author: object, fallback: str) -> str:
    value = str(author or "").strip().lstrip("/")
    if not value:
        return fallback
    if not value.startswith("u/"):
        value = "u/" + value
    return value

def _reddit_comment_blocks(
    children: object, depth: int, state: dict
) -> list[dict]:
    blocks: list[dict] = []
    if not isinstance(children, list):
        return blocks
    for child in children:
        if state["count"] >= REDDIT_MAX_COMMENTS:
            break
        if not isinstance(child, dict):
            continue
        if child.get("kind") != "t1":
            continue
        data = child.get("data") or {}
        body = str(data.get("body") or "").strip()
        if body and body not in {"[removed]", "[deleted]"}:
            state["count"] += 1
            author = str(data.get("author") or "").strip()
            label = _reddit_author_label(author, "Comment")
            score = data.get("score")
            if isinstance(score, int):
                label += f" · {score} points"
            blocks.append({"type": "heading", "level": 3, "text": label})
            blocks.extend(_reddit_clean_blocks(_reddit_markdown_blocks(body)))
        replies = data.get("replies")
        if depth < 2 and isinstance(replies, dict):
            reply_children = ((replies.get("data") or {}).get("children"))
            blocks.extend(
                _reddit_comment_blocks(reply_children, depth + 1, state)
            )
    return blocks

def _reddit_post_from_json(
    data: object, canonical: str, fallback_title: str
) -> dict:
    try:
        post = data[0]["data"]["children"][0]["data"]
    except (IndexError, KeyError, TypeError) as exc:
        raise RuntimeError("Reddit did not return a post") from exc
    if not isinstance(post, dict):
        raise RuntimeError("Reddit did not return a post")

    title = str(post.get("title") or fallback_title or "").strip()
    author = str(post.get("author") or "").strip()
    blocks: list[dict] = [
        {
            "type": "heading",
            "level": 3,
            "text": _reddit_author_label(author, "Post"),
        }
    ]
    selftext = str(post.get("selftext") or "").strip()
    if selftext and selftext not in {"[removed]", "[deleted]"}:
        blocks.extend(_reddit_markdown_blocks(selftext))
    elif post.get("url") and not post.get("is_self"):
        blocks.append(
            {"type": "paragraph", "text": f"Link: {post.get('url')}"}
        )

    state = {"count": 0}
    comments: list[dict] = []
    try:
        children = data[1]["data"]["children"]
    except (IndexError, KeyError, TypeError):
        children = []
    comments.extend(_reddit_comment_blocks(children, 0, state))
    if state["count"]:
        blocks.append(
            {
                "type": "heading",
                "level": 2,
                "text": f"Comments ({state['count']})",
            }
        )
        blocks.extend(comments)

    blocks = _reddit_clean_blocks(blocks)
    return {
        "link": canonical,
        "title": title,
        "blocks": blocks,
        "paragraphs": _reddit_paragraphs_from_blocks(blocks),
        "images": _reddit_images(post),
    }

def _reddit_post_from_rss(
    document: str, canonical: str, fallback_title: str
) -> dict:
    """Parse Reddit's Atom thread feed when the .json endpoint is blocked."""
    try:
        root = ElementTree.fromstring(document)
    except ElementTree.ParseError as exc:
        raise RuntimeError("Reddit returned invalid RSS") from exc
    namespace = {"a": "http://www.w3.org/2005/Atom"}
    entries = root.findall("a:entry", namespace)
    if not entries:
        raise RuntimeError("Reddit returned no post content")

    title = fallback_title
    post_blocks: list[dict] = []
    comment_blocks: list[dict] = []
    comments = 0
    for index, entry in enumerate(entries):
        entry_id = entry.findtext("a:id", default="", namespaces=namespace)
        entry_title = entry.findtext("a:title", default="", namespaces=namespace)
        author = entry.findtext(
            "a:author/a:name", default="", namespaces=namespace
        ).strip()
        content_element = entry.find("a:content", namespace)
        content = content_element.text if content_element is not None else ""
        is_post = entry_id.startswith("t3_") or (
            index == 0 and not entry_id.startswith("t1_")
        )
        parsed = _reddit_clean_blocks(_reddit_blocks_from_html(content or ""))
        label = _reddit_author_label(author, "Post" if is_post else "Comment")
        if is_post:
            if entry_title.strip():
                title = entry_title.strip()
            post_blocks.append({"type": "heading", "level": 3, "text": label})
            post_blocks.extend(parsed)
            continue
        if not parsed or comments >= REDDIT_MAX_COMMENTS:
            continue
        comments += 1
        comment_blocks.append({"type": "heading", "level": 3, "text": label})
        comment_blocks.extend(parsed)

    blocks = list(post_blocks)
    if comments:
        blocks.append(
            {"type": "heading", "level": 2, "text": f"Comments ({comments})"}
        )
        blocks.extend(comment_blocks)
    blocks = _reddit_clean_blocks(blocks)
    return {
        "link": canonical,
        "title": str(title or "").strip(),
        "blocks": blocks,
        "paragraphs": _reddit_paragraphs_from_blocks(blocks),
        "images": [],
    }

def fetch_reddit_post(url: str, title: str = "") -> dict:
    """Fetch one public Reddit thread for the /reddit detail modal.

    Reddit's public .json endpoint is tried first, with a descriptive
    User-Agent as required by Reddit. Some networks are challenged with HTTP
    403/429 on .json, so the thread's Atom feed is used as a fallback; it
    carries the post and its comments in the same order. When both are
    blocked, Reddit's legacy ``/.embed`` document (the same document an iframe
    would load) is parsed for comments. Every successful response also carries
    an ``iframe_url`` so the browser can show the live thread if extraction is
    incomplete.
    """
    url = str(url or "").strip()
    if not re.match(r"^https?://", url, re.IGNORECASE):
        raise ValueError("Missing or invalid Reddit post URL")
    if GOOGLE_NEWS_ARTICLE_RE.match(url):
        resolved = resolve_google_news_url(url)
        if not resolved:
            raise RuntimeError(
                "Could not resolve the Google News link to a Reddit post"
            )
        url = resolved
    if not _is_reddit_url(url):
        raise ValueError("The URL is not a Reddit post")

    canonical = _reddit_canonical_url(url)
    base = canonical.rstrip("/")
    headers = {
        "User-Agent": REDDIT_USER_AGENT,
        "Accept": "application/json, text/plain;q=0.9, */*;q=0.8",
        "Accept-Language": "en-US,en;q=0.9",
    }
    errors: list[str] = []
    try:
        response = requests.get(
            base + "/.json",
            headers=headers,
            timeout=REDDIT_REQUEST_TIMEOUT,
        )
        response.raise_for_status()
        result = _reddit_post_from_json(response.json(), canonical, title)
        return _reddit_response(result, canonical, "json")
    except (requests.RequestException, ValueError, RuntimeError) as exc:
        errors.append(f"JSON: {exc}")

    rss_headers = {
        **headers,
        "Accept": "application/atom+xml, application/xml;q=0.9, */*;q=0.8",
    }
    try:
        for attempt in range(2):
            response = requests.get(
                base + "/.rss",
                headers=rss_headers,
                timeout=REDDIT_REQUEST_TIMEOUT,
            )
            if response.status_code == 429 and attempt == 0:
                # Reddit rate-limits the Atom endpoint aggressively after a
                # challenged .json request. One short pause usually clears it.
                time.sleep(2.0)
                continue
            response.raise_for_status()
            result = _reddit_post_from_rss(response.text, canonical, title)
            return _reddit_response(result, canonical, "rss")
    except (requests.RequestException, RuntimeError) as exc:
        errors.append(f"RSS: {exc}")

    embed_headers = {
        **headers,
        "Accept": "text/javascript, text/html;q=0.9, */*;q=0.8",
    }
    try:
        response = requests.get(
            _reddit_embed_url(canonical),
            headers=embed_headers,
            timeout=REDDIT_REQUEST_TIMEOUT,
        )
        response.raise_for_status()
        result = _reddit_post_from_embed(response.text, canonical, title)
        return _reddit_response(result, canonical, "embed", partial=True)
    except (requests.RequestException, RuntimeError) as exc:
        errors.append(f"EMBED: {exc}")

    raise RuntimeError(
        "Could not load the Reddit post (" + "; ".join(errors) + ")"
    )


# Cross-component imports are placed after the definitions so a
# feature module can depend on another feature module safely.
from .core import SCRIPT_DIR
from .retrieval import ClientDisconnected, news_id_for_item, news_is_learned
from .stackoverflow import _StackOverflowBodyParser
