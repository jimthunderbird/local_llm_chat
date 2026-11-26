"""Project Gutenberg, Open Library, and Internet Archive search."""

from __future__ import annotations

from .core import *
from .command_config import GUTENBERG_BOOK_RESULT_LIMIT, INTERNET_ARCHIVE_RESULT_LIMIT, OPENLIBRARY_RESULT_LIMIT

GUTENBERG_DOWNLOAD_LOCK = threading.Lock()

def _first_gutenberg_id(values) -> str:
    """Return the canonical numeric id from an Open Library identifier list."""
    if not isinstance(values, list):
        values = [values]
    for value in values:
        text = str(value or "").strip()
        if GUTENBERG_BOOK_ID_RE.fullmatch(text):
            return str(int(text))
    return ""

def _gutenberg_openlibrary_card(doc: dict) -> dict | None:
    """Normalize one Open Library work into a Gutenberg book card."""
    if not isinstance(doc, dict):
        return None
    book_id = _first_gutenberg_id(doc.get("id_project_gutenberg"))
    if not book_id:
        return None
    title = str(doc.get("title") or f"Book {book_id}").strip()
    authors = ", ".join(
        str(name).strip()
        for name in (doc.get("author_name") or [])
        if str(name).strip()
    )
    subjects = [
        str(value).strip()
        for value in (doc.get("subject") or [])
        if str(value).strip()
    ][:4]
    languages = [
        str(value).strip()
        for value in (doc.get("language") or [])
        if str(value).strip()
    ]
    try:
        first_publish_year = int(doc.get("first_publish_year") or 0)
    except (TypeError, ValueError):
        first_publish_year = 0
    return {
        "id": book_id,
        "title": title,
        "authors": authors,
        "languages": languages,
        "subjects": subjects,
        "first_publish_year": first_publish_year,
        "download_count": 0,
        "url": GUTENBERG_BOOK_URL.format(book_id=book_id),
        "text_url": f"{GUTENBERG_BOOK_URL.format(book_id=book_id)}.txt.utf-8",
        "cover_url": GUTENBERG_COVER_URL.format(book_id=book_id),
        "source": "openlibrary",
    }

def _gutenberg_search_card(block: str) -> dict | None:
    """Normalize one result block from the Project Gutenberg search page."""
    link = GUTENBERG_SEARCH_LINK_RE.search(block)
    if link is None:
        return None
    book_id = str(int(link.group(1)))
    title_match = GUTENBERG_SEARCH_TITLE_RE.search(block)
    author_match = GUTENBERG_SEARCH_AUTHOR_RE.search(block)
    extra_match = GUTENBERG_SEARCH_EXTRA_RE.search(block)
    title = html.unescape(re.sub(r"<[^>]+>", "", title_match.group(1))).strip() if title_match else ""
    authors = html.unescape(re.sub(r"<[^>]+>", "", author_match.group(1))).strip() if author_match else ""
    downloads = 0
    if extra_match:
        extra_text = html.unescape(re.sub(r"<[^>]+>", "", extra_match.group(1)))
        downloads_match = GUTENBERG_SEARCH_DOWNLOADS_RE.search(extra_text)
        if downloads_match:
            downloads = int(downloads_match.group(1).replace(",", ""))
    if not title:
        title = f"Book {book_id}"
    return {
        "id": book_id,
        "title": title,
        "authors": authors,
        "languages": [],
        "subjects": [],
        "first_publish_year": 0,
        "download_count": downloads,
        "url": GUTENBERG_BOOK_URL.format(book_id=book_id),
        "text_url": f"{GUTENBERG_BOOK_URL.format(book_id=book_id)}.txt.utf-8",
        "cover_url": GUTENBERG_COVER_URL.format(book_id=book_id),
        "source": "gutenberg.org",
    }

_NUMBER_ONES = (
    "zero",
    "one",
    "two",
    "three",
    "four",
    "five",
    "six",
    "seven",
    "eight",
    "nine",
    "ten",
    "eleven",
    "twelve",
    "thirteen",
    "fourteen",
    "fifteen",
    "sixteen",
    "seventeen",
    "eighteen",
    "nineteen",
)

_NUMBER_TENS = (
    "",
    "",
    "twenty",
    "thirty",
    "forty",
    "fifty",
    "sixty",
    "seventy",
    "eighty",
    "ninety",
)

_GUTENBERG_STOPWORDS = {
    "a",
    "an",
    "and",
    "as",
    "at",
    "by",
    "for",
    "from",
    "in",
    "of",
    "on",
    "or",
    "the",
    "to",
    "with",
}

def _number_to_words(value: int) -> str:
    """Render a non-negative integer as lowercase English words."""
    if value < 0 or value > 999999999:
        return str(value)

    def under_thousand(number: int) -> list[str]:
        words: list[str] = []
        if number >= 100:
            words.extend((_NUMBER_ONES[number // 100], "hundred"))
            number %= 100
        if number >= 20:
            words.append(_NUMBER_TENS[number // 10])
            number %= 10
        if number or not words:
            words.append(_NUMBER_ONES[number])
        return words

    if value == 0:
        return "zero"
    parts: list[str] = []
    for divisor, label in ((1000000, "million"), (1000, "thousand")):
        if value >= divisor:
            parts.extend(under_thousand(value // divisor))
            parts.append(label)
            value %= divisor
    if value:
        parts.extend(under_thousand(value))
    return " ".join(parts)

def _year_to_words(value: int) -> str:
    """Render a year-like number the way book titles usually spell it."""
    if not 1100 <= value <= 2099:
        return ""
    high, low = divmod(value, 100)
    if low == 0:
        return f"{_number_to_words(high)} hundred"
    return f"{_number_to_words(high)} {_number_to_words(low)}"

def _gutenberg_query_variants(keyword: str) -> list[str]:
    """Return catalogue queries for a free-form book search.

    Project Gutenberg's catalogue search is literal: ``the 39 steps`` does not
    match ``The Thirty-Nine Steps`` even though a reader expects it to. Build a
    few spelling variants (digits as words, year-style numbers, and the query
    without a leading article) so titles written either way remain findable.
    """
    base = " ".join(str(keyword or "").split())
    if not base:
        return []
    variants: list[str] = []
    seen: set[str] = set()

    def add(value: str) -> None:
        value = " ".join(value.split())
        key = value.casefold()
        if value and key not in seen:
            seen.add(key)
            variants.append(value)

    add(base)
    add(
        re.sub(
            r"\b\d{1,6}\b",
            lambda match: _number_to_words(int(match.group(0))),
            base,
        )
    )
    add(
        re.sub(
            r"\b\d{4}\b",
            lambda match: _year_to_words(int(match.group(0)))
            or match.group(0),
            base,
        )
    )
    for value in list(variants):
        add(re.sub(r"^(?:the|a|an)\s+", "", value, flags=re.IGNORECASE))
    return variants[:6]

def _gutenberg_tokens(text: str) -> list[str]:
    text = html.unescape(str(text or "")).casefold()
    return [token for token in re.split(r"[^a-z0-9]+", text) if token]

def _gutenberg_query_term_sets(keyword: str) -> list[set[str]]:
    term_sets: list[set[str]] = []
    seen: set[frozenset[str]] = set()
    for variant in _gutenberg_query_variants(keyword):
        terms = {
            token
            for token in _gutenberg_tokens(variant)
            if token not in _GUTENBERG_STOPWORDS
        }
        if not terms:
            continue
        frozen = frozenset(terms)
        if frozen not in seen:
            seen.add(frozen)
            term_sets.append(terms)
    return term_sets

def _gutenberg_card_relevance(card: dict, keyword: str) -> float:
    """Score a book card against the query and its spelling variants.

    The catalogue can return loosely related books for a literal query (for
    example other John Buchan novels for ``the 39 steps``). Ranking by title
    and author coverage keeps the exact title ahead of those results.
    """
    title_tokens = _gutenberg_tokens(str(card.get("title") or ""))
    author_tokens = _gutenberg_tokens(str(card.get("authors") or ""))
    title_set = set(title_tokens)
    author_set = set(author_tokens)
    title_text = " ".join(title_tokens)
    variants = _gutenberg_query_variants(keyword)
    variant_texts = {" ".join(_gutenberg_tokens(value)) for value in variants}
    best = 0.0
    for terms in _gutenberg_query_term_sets(keyword):
        title_coverage = len(terms & title_set) / len(terms)
        author_coverage = len(terms & author_set) / len(terms)
        score = title_coverage * 6.0 + author_coverage * 1.5
        if terms <= title_set:
            score += 3.0
        if title_text in variant_texts:
            score += 4.0
        best = max(best, score)
    return best

def _combine_gutenberg_cards(
    keyword: str,
    *groups: list[dict],
    limit: int = GUTENBERG_BOOK_RESULT_LIMIT,
) -> list[dict]:
    """Merge book cards from all sources, deduplicate, and rank them."""
    by_id: dict[str, dict] = {}
    order: list[str] = []
    for group in groups:
        for card in group or []:
            if not isinstance(card, dict):
                continue
            book_id = str(card.get("id") or "")
            if not book_id:
                continue
            existing = by_id.get(book_id)
            if existing is None:
                by_id[book_id] = dict(card)
                order.append(book_id)
            else:
                _merge_gutenberg_news_card(existing, card)
    cards = [by_id[book_id] for book_id in order]
    ranked = sorted(
        enumerate(cards),
        key=lambda pair: (-_gutenberg_card_relevance(pair[1], keyword), pair[0]),
    )
    return [card for _index, card in ranked[: max(1, limit)]]

def _openlibrary_search_params(keyword: str) -> dict:
    """Build Open Library search parameters from a free-form query.

    A query such as ``the time machine by h g wells`` maps to the documented
    ``?title=...&author=...`` form. Everything else uses the general ``q``
    search so keywords and subjects still work.
    """
    match = re.match(
        r"^(?P<title>.+?)\s+by\s+(?P<author>.+)$", keyword.strip(), re.IGNORECASE
    )
    if match:
        title = match.group("title").strip()
        author = match.group("author").strip()
        if title and author and len(title) <= 240 and len(author) <= 240:
            return {"title": title, "author": author}
    return {"q": keyword}

def _openlibrary_plain_text(value: object, limit: int = 0) -> str:
    """Flatten an Open Library text field into readable plain text."""
    if isinstance(value, dict):
        value = value.get("value") or value.get("description") or ""
    if isinstance(value, (list, tuple)):
        parts = [_openlibrary_plain_text(item) for item in value]
        value = " ".join(part for part in parts if part)
    text = str(value or "")
    text = text.replace("{{{", "").replace("}}}", "")
    text = re.sub(r"<[^>]+>", " ", text)
    text = html.unescape(text)
    text = " ".join(text.split())
    if limit > 0 and len(text) > limit:
        shortened = text[:limit].rsplit(" ", 1)[0].rstrip(" ,;:-")
        text = (shortened or text[:limit].rstrip()) + "..."
    return text

def _openlibrary_key(value: object) -> str:
    """Return a normalized Open Library key such as /works/OL123W."""
    key = str(value or "").strip()
    if not key:
        return ""
    if key.startswith(("http://", "https://")):
        path = urlparse(key).path
        key = path or ""
    if key and not key.startswith("/"):
        key = "/" + key
    return key

def _openlibrary_cover_url(doc: dict) -> str:
    """Return a small Open Library cover image URL when the doc has one."""
    if not isinstance(doc, dict):
        return ""
    cover_id = str(doc.get("cover_i") or "").strip()
    if cover_id.isdigit():
        return OPENLIBRARY_COVER_ID_URL.format(cover_id=cover_id)
    olid = _openlibrary_key(doc.get("cover_edition_key")).lstrip("/")
    if olid:
        return OPENLIBRARY_COVER_OLID_URL.format(olid=quote(olid))
    return ""

def _openlibrary_first_text(value: object) -> str:
    if isinstance(value, (list, tuple)):
        for item in value:
            text = _openlibrary_plain_text(item)
            if text:
                return text
        return ""
    return _openlibrary_plain_text(value)

def _openlibrary_related_text(hit: dict, keyword: str) -> list[str]:
    """Return the strongest full-text snippets for one search hit."""
    highlight = hit.get("highlight") if isinstance(hit, dict) else None
    raw_snippets = highlight.get("text") if isinstance(highlight, dict) else None
    if not isinstance(raw_snippets, list):
        return []
    terms = {
        token
        for token in re.split(r"[^a-z0-9]+", keyword.casefold())
        if len(token) > 1
    }
    snippets: list[tuple[float, int, str]] = []
    seen: set[str] = set()
    for index, raw in enumerate(raw_snippets):
        text = _openlibrary_plain_text(raw, limit=520)
        if not text or text in seen:
            continue
        seen.add(text)
        tokens = {
            token
            for token in re.split(r"[^a-z0-9]+", text.casefold())
            if len(token) > 1
        }
        coverage = len(terms & tokens) / len(terms) if terms else 0.0
        snippets.append((-coverage, index, text))
    snippets.sort()
    return [text for _coverage, _index, text in snippets[:OPENLIBRARY_HIGHLIGHT_LIMIT]]

def _openlibrary_metadata_description(
    authors: str,
    first_publish_year: int,
    publisher: str,
    subjects: list[str],
) -> str:
    parts: list[str] = []
    if authors:
        parts.append(f"By {authors}")
    if first_publish_year > 0:
        parts.append(f"first published {first_publish_year}")
    if publisher:
        parts.append(f"published by {publisher}")
    if subjects:
        parts.append(f"subjects: {', '.join(subjects[:3])}")
    return " · ".join(parts)

def _openlibrary_doc_card(doc: dict, keyword: str) -> dict | None:
    """Normalize one Open Library catalogue work into a book card.

    Catalogue results carry no full-text excerpts, but they are the only place
    where a title/series phrase such as "the rust programming language" is
    matched as a whole. They are merged with the inside-search hits so books
    about the complete phrase outrank books that merely mention its words.
    """
    if not isinstance(doc, dict):
        return None
    title = str(doc.get("title") or "").strip()
    if not title:
        return None
    work_key = _openlibrary_key(doc.get("key"))
    if not work_key:
        return None
    authors = ", ".join(
        str(name).strip()
        for name in (doc.get("author_name") or [])
        if str(name).strip()
    )
    subjects = [
        str(value).strip()
        for value in (doc.get("subject") or [])
        if str(value).strip()
    ][:5]
    languages = [
        str(value).strip()
        for value in (doc.get("language") or [])
        if str(value).strip()
    ]
    try:
        first_publish_year = int(doc.get("first_publish_year") or 0)
    except (TypeError, ValueError):
        first_publish_year = 0
    try:
        download_count = int(doc.get("download_count") or 0)
    except (TypeError, ValueError):
        download_count = 0
    return {
        "id": work_key.lstrip("/"),
        "title": title,
        "authors": authors,
        "description": _openlibrary_metadata_description(
            authors, first_publish_year, "", subjects
        ),
        "related_text": [],
        "languages": languages,
        "subjects": subjects,
        "first_publish_year": first_publish_year,
        "download_count": download_count,
        "url": OPENLIBRARY_BOOK_PAGE_URL.format(key=work_key),
        "text_url": "",
        "cover_url": _openlibrary_cover_url(doc),
        "source": "openlibrary",
        "work_key": work_key,
    }

def _openlibrary_hit_card(hit: dict, keyword: str) -> dict | None:
    """Normalize one Open Library full-text search hit into a book card."""
    if not isinstance(hit, dict):
        return None
    edition = hit.get("edition") if isinstance(hit.get("edition"), dict) else {}
    fields = hit.get("fields") if isinstance(hit.get("fields"), dict) else {}

    title = _openlibrary_first_text(
        edition.get("title") or fields.get("meta_title") or ""
    )
    if not title:
        return None

    authors = ", ".join(
        str(author.get("name") or "").strip()
        for author in (edition.get("authors") or [])
        if isinstance(author, dict) and str(author.get("name") or "").strip()
    )
    if not authors:
        authors = _openlibrary_first_text(fields.get("meta_creator"))

    publisher = _openlibrary_first_text(fields.get("meta_publisher"))
    subjects = [
        _openlibrary_plain_text(value)
        for value in (fields.get("meta_subject") or [])
        if _openlibrary_plain_text(value)
    ][:5]

    first_publish_year = 0
    for value in (
        _openlibrary_first_text(fields.get("meta_year")),
        str(edition.get("publish_date") or ""),
        _openlibrary_first_text(fields.get("meta_date")),
    ):
        match = re.search(r"\b(\d{4})\b", value)
        if match:
            first_publish_year = int(match.group(1))
            break

    work_key = _openlibrary_key(edition.get("work_key"))
    edition_key = _openlibrary_key(edition.get("key"))
    page_key = work_key or edition_key
    card_id = (page_key or str(hit.get("_id") or "")).lstrip("/")
    if not card_id:
        return None
    url = (
        OPENLIBRARY_BOOK_PAGE_URL.format(key=page_key)
        if page_key
        else f"https://openlibrary.org/search/inside?q={quote(keyword)}"
    )
    description = _openlibrary_metadata_description(
        authors, first_publish_year, publisher, subjects
    )
    return {
        "id": card_id,
        "title": title,
        "authors": authors,
        "description": description,
        "related_text": _openlibrary_related_text(hit, keyword),
        "languages": [],
        "subjects": subjects,
        "first_publish_year": first_publish_year,
        "download_count": 0,
        "url": url,
        "text_url": "",
        "cover_url": _openlibrary_cover_url(edition),
        "source": "openlibrary",
        "work_key": work_key,
    }

def _openlibrary_work_description(work_key: str) -> str:
    """Fetch a work description, caching both hits and misses in memory."""
    work_key = _openlibrary_key(work_key)
    if not work_key.startswith("/works/"):
        return ""
    with OPENLIBRARY_DESCRIPTION_CACHE_LOCK:
        if work_key in OPENLIBRARY_DESCRIPTION_CACHE:
            return OPENLIBRARY_DESCRIPTION_CACHE[work_key]
    description = ""
    try:
        response = requests.get(
            OPENLIBRARY_WORK_JSON_URL.format(key=work_key),
            headers={**chat.HTTP_HEADERS, "Accept": "application/json"},
            timeout=(4, 10),
        )
        response.raise_for_status()
        data = response.json()
        if isinstance(data, dict):
            description = _openlibrary_plain_text(
                data.get("description") or data.get("first_sentence") or "",
                limit=OPENLIBRARY_DESCRIPTION_LIMIT,
            )
    except (requests.RequestException, ValueError):
        description = ""
    with OPENLIBRARY_DESCRIPTION_CACHE_LOCK:
        OPENLIBRARY_DESCRIPTION_CACHE[work_key] = description
    return description

def _openlibrary_attach_descriptions(cards: list[dict]) -> None:
    """Add real work descriptions in parallel, keeping metadata fallbacks."""
    pending = [
        (card, str(card.get("work_key") or ""))
        for card in cards
        if str(card.get("work_key") or "").startswith("/works/")
    ]
    if not pending:
        return
    with ThreadPoolExecutor(max_workers=min(8, len(pending))) as executor:
        futures = {
            executor.submit(_openlibrary_work_description, work_key): card
            for card, work_key in pending
        }
        for future in as_completed(futures):
            card = futures[future]
            try:
                description = future.result()
            except Exception:
                description = ""
            if description:
                card["description"] = description
    for card in cards:
        card.pop("work_key", None)

def _openlibrary_card_relevance(card: dict, keyword: str) -> float:
    """Score a book card against the whole query phrase and its variants.

    A full-text hit can match every word separately (for "the rust programming
    language" that includes books about rust the metal or the fungal disease),
    while the catalogue result matches the phrase as a title. Title/author
    coverage and an exact phrase match therefore dominate, and excerpt coverage
    only breaks ties between otherwise similar cards.
    """
    title_tokens = set(_gutenberg_tokens(str(card.get("title") or "")))
    author_tokens = set(_gutenberg_tokens(str(card.get("authors") or "")))
    subject_tokens = set(_gutenberg_tokens(" ".join(card.get("subjects") or [])))
    excerpt_token_sets = [
        set(_gutenberg_tokens(text))
        for text in (card.get("related_text") or [])
    ]
    description_tokens = set(_gutenberg_tokens(str(card.get("description") or "")))
    title_text = " ".join(sorted(title_tokens))
    variant_texts = {
        " ".join(sorted(_gutenberg_tokens(value)))
        for value in _gutenberg_query_variants(keyword)
    }
    best = 0.0
    for terms in _gutenberg_query_term_sets(keyword):
        if not terms:
            continue
        total = len(terms)
        title_coverage = len(terms & title_tokens) / total
        author_coverage = len(terms & author_tokens) / total
        score = title_coverage * 8.0 + author_coverage * 2.0
        if terms <= title_tokens:
            score += 4.0
        if title_text in variant_texts:
            score += 6.0
        if excerpt_token_sets:
            excerpt_coverage = max(
                len(terms & tokens) / total for tokens in excerpt_token_sets
            )
            score += excerpt_coverage * 2.0
        score += len(terms & description_tokens) / total * 1.0
        score += len(terms & subject_tokens) / total * 1.0
        best = max(best, score)
    return best

def _combine_openlibrary_cards(
    keyword: str,
    *groups: list[dict],
    limit: int = OPENLIBRARY_RESULT_LIMIT,
) -> list[dict]:
    """Merge inside-search and catalogue cards, then rank by phrase match."""
    by_id: dict[str, dict] = {}
    order: list[str] = []
    for group in groups:
        for card in group or []:
            if not isinstance(card, dict):
                continue
            card_id = str(card.get("id") or "")
            if not card_id:
                continue
            existing = by_id.get(card_id)
            if existing is None:
                by_id[card_id] = dict(card)
                order.append(card_id)
                continue
            if len(str(card.get("description") or "")) > len(
                str(existing.get("description") or "")
            ):
                existing["description"] = card["description"]
            for field in ("authors", "title"):
                if not existing.get(field) and card.get(field):
                    existing[field] = card[field]
            if not existing.get("first_publish_year") and card.get("first_publish_year"):
                existing["first_publish_year"] = card["first_publish_year"]
            if not existing.get("work_key") and card.get("work_key"):
                existing["work_key"] = card["work_key"]
            for field in ("languages", "subjects"):
                merged = list(existing.get(field) or [])
                for value in card.get(field) or []:
                    if value not in merged:
                        merged.append(value)
                existing[field] = merged
            merged_text = list(existing.get("related_text") or [])
            for snippet in card.get("related_text") or []:
                if snippet not in merged_text:
                    merged_text.append(snippet)
            existing["related_text"] = merged_text[:OPENLIBRARY_HIGHLIGHT_LIMIT]
    cards = [by_id[card_id] for card_id in order]
    ranked = sorted(
        enumerate(cards),
        key=lambda pair: (-_openlibrary_card_relevance(pair[1], keyword), pair[0]),
    )
    return [card for _index, card in ranked[: max(1, limit)]]

def _fetch_openlibrary_inside(keyword: str, limit: int) -> list[dict]:
    """Fetch and normalize the full-text (inside) search hits."""
    try:
        response = requests.get(
            OPENLIBRARY_INSIDE_SEARCH_URL,
            params={"q": keyword, "limit": max(20, limit * 3)},
            headers={**chat.HTTP_HEADERS, "Accept": "application/json"},
            timeout=(10, 40),
        )
        response.raise_for_status()
        data = response.json()
    except (requests.RequestException, ValueError) as exc:
        raise RuntimeError(f"Open Library search failed: {exc}") from exc

    hits = data.get("hits") if isinstance(data, dict) else None
    raw_hits = hits.get("hits") if isinstance(hits, dict) else None
    if not isinstance(raw_hits, list):
        raise RuntimeError("Open Library returned an unexpected response.")

    cards: list[dict] = []
    for hit in raw_hits:
        card = _openlibrary_hit_card(hit, keyword)
        if card is not None:
            cards.append(card)
    return cards

def _fetch_openlibrary_metadata(keyword: str, limit: int) -> list[dict]:
    """Fetch catalogue works whose title/author matches the whole query."""
    params = _openlibrary_search_params(keyword)
    params["fields"] = OPENLIBRARY_SEARCH_FIELDS
    params["limit"] = max(20, limit * 3)
    try:
        response = requests.get(
            OPENLIBRARY_SEARCH_URL,
            params=params,
            headers={**chat.HTTP_HEADERS, "Accept": "application/json"},
            timeout=(10, 30),
        )
        response.raise_for_status()
        data = response.json()
    except (requests.RequestException, ValueError) as exc:
        raise RuntimeError(f"Open Library catalogue search failed: {exc}") from exc
    docs = data.get("docs") if isinstance(data, dict) else None
    if not isinstance(docs, list):
        return []
    cards: list[dict] = []
    for doc in docs:
        card = _openlibrary_doc_card(doc, keyword)
        if card is not None:
            cards.append(card)
    return cards

def search_openlibrary_inside(
    keyword: str, limit: int = OPENLIBRARY_RESULT_LIMIT
) -> list[dict]:
    """Search Open Library books and return cards for the whole query.

    Full-text (inside) hits supply matching passages, while the catalogue
    search supplies works whose title/author matches the complete phrase (for
    example "the rust programming language"). Both sources are merged, ranked
    by phrase/title relevance, and the remaining work descriptions are fetched
    in parallel.
    """
    keyword = " ".join(str(keyword or "").split())
    if not keyword:
        return []
    limit = max(1, min(int(limit), 50))

    inside_cards: list[dict] = []
    metadata_cards: list[dict] = []
    errors: list[str] = []
    with ThreadPoolExecutor(max_workers=2) as executor:
        futures = {
            executor.submit(_fetch_openlibrary_inside, keyword, limit): "inside",
            executor.submit(_fetch_openlibrary_metadata, keyword, limit): "metadata",
        }
        for future in as_completed(futures):
            try:
                cards = future.result()
            except RuntimeError as exc:
                errors.append(str(exc))
                continue
            if futures[future] == "inside":
                inside_cards = cards
            else:
                metadata_cards = cards
    if not inside_cards and not metadata_cards and errors:
        raise RuntimeError("; ".join(errors))

    cards = _combine_openlibrary_cards(
        keyword, metadata_cards, inside_cards, limit=limit
    )
    _openlibrary_attach_descriptions(cards)
    for card in cards:
        if not card.get("description"):
            card["description"] = f"Open Library match for {keyword!r}."
    return cards

def _internet_archive_first_text(value: object) -> str:
    """Return the first meaningful text from an Internet Archive field."""
    if isinstance(value, (list, tuple)):
        for item in value:
            text = _openlibrary_plain_text(item)
            if text:
                return text
        return ""
    return _openlibrary_plain_text(value)

def _internet_archive_values(value: object, limit: int = 0) -> list[str]:
    """Normalize a possibly multi-valued Internet Archive metadata field."""
    if isinstance(value, (list, tuple)):
        values = [_openlibrary_plain_text(item) for item in value]
    else:
        values = [_openlibrary_plain_text(value)]
    unique: list[str] = []
    for text in values:
        if text and text not in unique:
            unique.append(text)
        if limit > 0 and len(unique) >= limit:
            break
    return unique

def _internet_archive_publish_date(fields: dict) -> str:
    """Return a human-readable publication date for an Internet Archive item."""
    for name in ("date", "publicdate", "year", "addeddate"):
        text = _internet_archive_first_text(fields.get(name)).strip()
        if not text:
            continue
        match = re.match(r"^(\d{4})(?:-(\d{2})(?:-(\d{2}))?)?", text)
        if match:
            return "-".join(part for part in match.groups() if part)
        return text[:40]
    return ""

def _internet_archive_year(fields: dict) -> int:
    """Extract a four-digit publication year from the available fields."""
    for name in ("year", "date", "publicdate", "addeddate"):
        match = re.search(r"\b(\d{4})\b", _internet_archive_first_text(fields.get(name)))
        if match:
            return int(match.group(1))
    return 0

def _internet_archive_restricted_access(fields: dict) -> bool:
    """Return True when an item is lending/borrow-only or otherwise restricted.

    Internet Archive sets ``access-restricted-item`` on most lending items.
    That field is not exposed by the full-text index, so the item's collections
    and ``lending___status`` values are also inspected. Membership in the
    generic ``internetarchivebooks`` collection is not enough: that collection
    also holds public-domain items. Items with no lending/restriction marker
    are publicly downloadable.
    """
    flag = _internet_archive_first_text(
        fields.get("access-restricted-item")
    ).strip().lower()
    if flag in {"true", "yes", "1"}:
        return True
    if flag in {"false", "no", "0"}:
        return False
    collections = {
        value.strip().lower()
        for value in _internet_archive_values(fields.get("collection"))
    }
    if collections & INTERNET_ARCHIVE_RESTRICTED_COLLECTIONS:
        return True
    statuses = {
        value.strip().lower()
        for value in _internet_archive_values(fields.get("lending___status"))
    }
    return bool(statuses & INTERNET_ARCHIVE_RESTRICTED_LENDING_STATUSES)

def _internet_archive_related_text(hit: dict) -> list[str]:
    """Collect and clean the highlighted OCR passages from one FTS hit."""
    highlight = hit.get("highlight") if isinstance(hit.get("highlight"), dict) else {}
    raw_texts = highlight.get("text")
    if not isinstance(raw_texts, (list, tuple)):
        raw_texts = [raw_texts] if raw_texts else []
    snippets: list[str] = []
    for raw in raw_texts:
        text = _openlibrary_plain_text(raw, limit=480)
        if text and text not in snippets:
            snippets.append(text)
        if len(snippets) >= INTERNET_ARCHIVE_HIGHLIGHT_LIMIT:
            break
    return snippets

def _internet_archive_card(
    fields: dict,
    keyword: str,
    snippets: list[str] | None = None,
    source: str = "fts",
) -> dict | None:
    """Normalize one Internet Archive hit or catalogue document into a card."""
    if not isinstance(fields, dict):
        return None
    identifier = _internet_archive_first_text(fields.get("identifier"))
    if not identifier:
        return None
    title = _internet_archive_first_text(fields.get("title")) or identifier
    authors = ", ".join(_internet_archive_values(fields.get("creator"), limit=5))
    subjects = _internet_archive_values(fields.get("subject"), limit=5)
    languages = _internet_archive_values(fields.get("language"), limit=5)
    first_publish_year = _internet_archive_year(fields)
    try:
        download_count = int(fields.get("downloads") or 0)
    except (TypeError, ValueError):
        download_count = 0
    description = _openlibrary_plain_text(
        fields.get("description"), limit=INTERNET_ARCHIVE_DESCRIPTION_LIMIT
    )
    if not description:
        description = _openlibrary_metadata_description(
            authors, first_publish_year, "", subjects
        )
    href = str(fields.get("__href__") or "").strip()
    if href.startswith("/"):
        url = "https://archive.org" + href
    else:
        url = INTERNET_ARCHIVE_DETAILS_URL.format(identifier=quote(identifier))
    availability = (
        "restricted" if _internet_archive_restricted_access(fields) else "public"
    )
    return {
        "id": identifier,
        "title": title,
        "authors": authors,
        "description": description,
        "related_text": list(snippets or []),
        "languages": languages,
        "subjects": subjects,
        "first_publish_year": first_publish_year,
        "publish_date": _internet_archive_publish_date(fields),
        "download_count": download_count,
        "url": url,
        "text_url": "",
        "cover_url": INTERNET_ARCHIVE_COVER_URL.format(
            identifier=quote(identifier, safe="")
        ),
        "__text_hint__": _internet_archive_text_hint(href),
        "source": "internet_archive",
        "search_source": source,
        "availability": availability,
    }

def _internet_archive_text_hint(href: str) -> str:
    """Return the file stem from an FTS ``/details/<id>/<file>`` link."""
    path = urlparse(str(href or "")).path
    parts = [unquote(part) for part in path.split("/") if part]
    if len(parts) >= 3 and parts[0] == "details":
        return parts[2]
    return ""

def _internet_archive_plaintext_filename(files: object, hint: str = "") -> str:
    """Choose the best browser-readable OCR plaintext file for an item."""
    if not isinstance(files, list):
        return ""
    hint_lower = str(hint or "").strip().lower()
    candidates: list[tuple[int, int, int, str]] = []
    for item in files:
        if not isinstance(item, dict):
            continue
        name = str(item.get("name") or "").strip()
        lower = name.lower()
        if not lower.endswith(".txt") or lower.endswith("_meta.txt"):
            continue
        base = lower.rsplit("/", 1)[-1]
        hint_match = 0 if hint_lower and base in {
            f"{hint_lower}.txt",
            f"{hint_lower}_djvu.txt",
        } else 1
        djvu = 0 if lower.endswith("_djvu.txt") else 1
        candidates.append((hint_match, djvu, len(base), name))
    if not candidates:
        return ""
    return min(candidates)[3]

def _internet_archive_item_details(identifier: str) -> dict:
    """Fetch and cache an item's metadata and file list from archive.org."""
    identifier = str(identifier or "").strip()
    if not identifier:
        return {}
    with INTERNET_ARCHIVE_METADATA_CACHE_LOCK:
        cached = INTERNET_ARCHIVE_METADATA_CACHE.get(identifier)
    if cached is not None:
        return cached

    details: dict = {}
    try:
        response = requests.get(
            INTERNET_ARCHIVE_METADATA_URL.format(identifier=quote(identifier)),
            headers=INTERNET_ARCHIVE_HEADERS,
            timeout=(4, 15),
        )
        response.raise_for_status()
        data = response.json()
        if isinstance(data, dict):
            metadata = data.get("metadata")
            files = data.get("files")
            details = {
                "metadata": metadata if isinstance(metadata, dict) else {},
                "files": files if isinstance(files, list) else [],
            }
    except (requests.RequestException, ValueError):
        details = {}

    with INTERNET_ARCHIVE_METADATA_CACHE_LOCK:
        INTERNET_ARCHIVE_METADATA_CACHE[identifier] = details
    return details

def _internet_archive_apply_item_details(
    card: dict, details: dict, *, public: bool
) -> None:
    """Fill a card's missing date; point public cards at their plaintext file."""
    metadata = details.get("metadata")
    if not isinstance(metadata, dict):
        metadata = {}
    # The full-text index omits ``publicdate``/``addeddate`` for many items,
    # so books often arrive without a date. The per-item metadata endpoint
    # still has it; copy it onto the card when the hit lacked it.
    if not card.get("publish_date"):
        card["publish_date"] = _internet_archive_publish_date(metadata)
    if not card.get("first_publish_year"):
        card["first_publish_year"] = _internet_archive_year(metadata)

    if not public:
        return
    identifier = str(card.get("id") or "").strip()
    if not identifier:
        return
    hint = str(card.get("__text_hint__") or "")
    filename = _internet_archive_plaintext_filename(details.get("files"), hint)
    if not filename:
        # The conventional OCR plaintext name is the fallback when the
        # metadata endpoint cannot answer, so every public result still gets
        # a direct plaintext URL instead of a catalogue page.
        filename = f"{identifier}_djvu.txt"
    card["url"] = INTERNET_ARCHIVE_DOWNLOAD_URL.format(
        identifier=quote(identifier), filename=quote(filename, safe="/")
    )

def _internet_archive_enrich_cards(cards: list[dict]) -> None:
    """Add dates to cards missing one and plaintext URLs to public cards."""
    pending = [
        card
        for card in cards
        if card.get("id")
        and (card.get("availability") == "public" or not card.get("publish_date"))
    ]
    if pending:
        with ThreadPoolExecutor(max_workers=min(8, len(pending))) as executor:
            futures = {
                executor.submit(_internet_archive_item_details, str(card["id"])): card
                for card in pending
            }
            for future in as_completed(futures):
                card = futures[future]
                try:
                    details = future.result()
                except Exception:
                    details = {}
                _internet_archive_apply_item_details(
                    card,
                    details,
                    public=card.get("availability") == "public",
                )
    for card in cards:
        card.pop("__text_hint__", None)

def _fetch_internet_archive_fulltext(keyword: str, limit: int) -> list[dict]:
    """Fetch full-text search hits from the Internet Archive beta API."""
    params = {
        "service_backend": "fts",
        "user_query": keyword,
        "hits_per_page": max(20, limit * 3),
        "page": 1,
        "aggregations": "false",
        "fields": INTERNET_ARCHIVE_FTS_FIELDS,
    }
    try:
        response = requests.get(
            INTERNET_ARCHIVE_FTS_URL,
            params=params,
            headers=INTERNET_ARCHIVE_HEADERS,
            timeout=(10, 60),
        )
        response.raise_for_status()
        data = response.json()
    except (requests.RequestException, ValueError) as exc:
        raise RuntimeError(f"Internet Archive full-text search failed: {exc}") from exc
    result = data.get("response") if isinstance(data, dict) else None
    body = result.get("body") if isinstance(result, dict) else None
    hits = body.get("hits") if isinstance(body, dict) else None
    raw_hits = hits.get("hits") if isinstance(hits, dict) else None
    if not isinstance(raw_hits, list):
        raise RuntimeError("Internet Archive returned an unexpected full-text response.")

    cards: list[dict] = []
    by_id: dict[str, dict] = {}
    for hit in raw_hits:
        if not isinstance(hit, dict):
            continue
        fields = hit.get("fields") if isinstance(hit.get("fields"), dict) else {}
        # The beta index also matches audio/video/data items whose nested
        # files contain text. This command is for digitized books, and such
        # non-text items usually have no publication date, so skip them.
        mediatype = _internet_archive_first_text(fields.get("mediatype")).strip().lower()
        if mediatype and mediatype != "texts":
            continue
        card = _internet_archive_card(
            fields, keyword, _internet_archive_related_text(hit), source="fts"
        )
        if card is None:
            continue
        existing = by_id.get(card["id"])
        if existing is None:
            by_id[card["id"]] = card
            cards.append(card)
        else:
            # FTS returns one hit per matching page; merge their passages so
            # each book appears once with up to three excerpts.
            seen = set(existing.get("related_text") or [])
            for snippet in card.get("related_text") or []:
                if snippet not in seen and len(existing["related_text"]) < INTERNET_ARCHIVE_HIGHLIGHT_LIMIT:
                    existing["related_text"].append(snippet)
                    seen.add(snippet)
    return cards

def _fetch_internet_archive_metadata(keyword: str, limit: int) -> list[dict]:
    """Fetch catalogue metadata from the Internet Archive Advanced Search API."""
    escaped = keyword.replace("\\", "\\\\").replace('"', '\\"')
    params = {
        "q": f'"{escaped}" AND mediatype:(texts)',
        "fl[]": INTERNET_ARCHIVE_SEARCH_FIELDS.split(","),
        "rows": max(20, limit * 2),
        "page": 1,
        "output": "json",
        "sort[]": "downloads desc",
    }
    try:
        response = requests.get(
            INTERNET_ARCHIVE_SEARCH_URL,
            params=params,
            headers=INTERNET_ARCHIVE_HEADERS,
            timeout=(10, 40),
        )
        response.raise_for_status()
        data = response.json()
    except (requests.RequestException, ValueError) as exc:
        raise RuntimeError(f"Internet Archive catalogue search failed: {exc}") from exc
    response_data = data.get("response") if isinstance(data, dict) else None
    docs = response_data.get("docs") if isinstance(response_data, dict) else None
    if not isinstance(docs, list):
        raise RuntimeError("Internet Archive returned an unexpected catalogue response.")
    cards: list[dict] = []
    seen: set[str] = set()
    for doc in docs:
        if not isinstance(doc, dict):
            continue
        card = _internet_archive_card(doc, keyword, source="metadata")
        if card is None or card["id"] in seen:
            continue
        seen.add(card["id"])
        cards.append(card)
    return cards

def search_internet_archive(
    keyword: str, limit: int = INTERNET_ARCHIVE_RESULT_LIMIT
) -> list[dict]:
    """Search Internet Archive texts and return clickable result cards.

    The beta full-text API matches phrases inside OCR text, which is the main
    use case. Only publicly downloadable items are returned. When it fails or
    finds no public item, fall back to catalogue metadata (title, creator,
    description) so a known public book can still be located.
    """
    keyword = " ".join(str(keyword or "").split())
    if not keyword:
        return []
    limit = max(1, min(int(limit), 50))

    def public_only(cards: list[dict]) -> list[dict]:
        return [
            card
            for card in cards
            if card.get("availability") == "public"
        ]

    errors: list[str] = []
    try:
        cards = public_only(_fetch_internet_archive_fulltext(keyword, limit))
    except RuntimeError as exc:
        errors.append(str(exc))
        cards = []
    if not cards:
        try:
            cards = public_only(
                _fetch_internet_archive_metadata(keyword, limit)
            )
        except RuntimeError as exc:
            errors.append(str(exc))
    if not cards and errors and len(errors) > 1:
        raise RuntimeError("; ".join(errors))
    cards = cards[:limit]
    _internet_archive_enrich_cards(cards)
    return cards

def _search_gutenberg_openlibrary(
    keyword: str, limit: int = GUTENBERG_BOOK_RESULT_LIMIT
) -> list[dict]:
    """Resolve Gutenberg text identifiers through Open Library's search API."""
    params = _openlibrary_search_params(keyword)
    params["fields"] = OPENLIBRARY_SEARCH_FIELDS
    # Open Library returns works, and not every work has a linked Gutenberg
    # identifier. Ask for a wider page so there are enough usable cards left
    # after filtering.
    params["limit"] = max(40, max(1, limit) * 5)
    try:
        response = requests.get(
            OPENLIBRARY_SEARCH_URL,
            params=params,
            headers=chat.HTTP_HEADERS,
            timeout=(10, 30),
        )
        response.raise_for_status()
        data = response.json()
    except (requests.RequestException, ValueError) as exc:
        raise RuntimeError(f"Open Library search failed: {exc}") from exc
    docs = data.get("docs") if isinstance(data, dict) else None
    if not isinstance(docs, list):
        raise RuntimeError("Open Library returned an unexpected response.")
    items: list[dict] = []
    seen: set[str] = set()
    for doc in docs:
        card = _gutenberg_openlibrary_card(doc)
        if card is None or card["id"] in seen:
            continue
        seen.add(card["id"])
        GUTENBERG_CATALOG_CACHE[card["id"]] = card
        items.append(card)
        if len(items) >= max(1, limit):
            break
    return items

def _search_gutenberg_gutenberg_org(
    keyword: str, limit: int = GUTENBERG_BOOK_RESULT_LIMIT
) -> list[dict]:
    """Search the Project Gutenberg catalogue page for one query.

    Open Library is authoritative for identifiers it has linked, but some
    public-domain works (for example early H. G. Wells editions) have no
    `id_project_gutenberg` value yet. The catalogue search page still returns
    their ebook numbers, so parse those cards without another third-party API.
    """
    try:
        response = requests.get(
            GUTENBERG_SEARCH_URL,
            params={"query": keyword},
            headers=chat.HTTP_HEADERS,
            timeout=(5, 20),
        )
        response.raise_for_status()
        page = response.text
    except requests.RequestException as exc:
        raise RuntimeError(f"Project Gutenberg search failed: {exc}") from exc
    items: list[dict] = []
    seen: set[str] = set()
    for block in GUTENBERG_SEARCH_BLOCK_RE.findall(page):
        card = _gutenberg_search_card(block)
        if card is None or card["id"] in seen:
            continue
        seen.add(card["id"])
        GUTENBERG_CATALOG_CACHE[card["id"]] = card
        items.append(card)
        if len(items) >= max(1, limit):
            break
    return items

def search_gutenberg_books(keyword: str, limit: int = GUTENBERG_BOOK_RESULT_LIMIT) -> list[dict]:
    """Search for Project Gutenberg books and return normalized book cards.

    Open Library is the primary catalog: each matching work contributes its
    ``id_project_gutenberg`` identifier. Because not every work is linked to a
    Gutenberg text there yet, the Project Gutenberg catalogue search fills in
    the missing cards. Every catalogue spelling variant is consulted, and the
    merged results are ranked against the original query so an exact title is
    not pushed out by loosely related matches.
    """
    keyword = keyword.strip()
    if not keyword:
        return []
    limit = max(1, limit)
    items: list[dict] = []
    errors: list[str] = []

    try:
        items.extend(_search_gutenberg_openlibrary(keyword, limit))
    except RuntimeError as exc:
        errors.append(str(exc))

    # Search the catalogue even when Open Library returned a full page: its
    # literal search misses digit/word title spellings, so a variant may hold
    # the exact book while the other sources only returned related works.
    for variant in _gutenberg_query_variants(keyword):
        try:
            items.extend(
                _search_gutenberg_gutenberg_org(
                    variant, max(limit, GUTENBERG_BOOK_RESULT_LIMIT)
                )
            )
        except RuntimeError as exc:
            errors.append(str(exc))

    merged = _combine_gutenberg_cards(keyword, items, limit=limit)
    if not merged and errors:
        raise RuntimeError("; ".join(errors))
    return merged

def _gutenberg_news_book_id(link: str) -> str:
    """Extract a Gutenberg ebook id from a resolved Google News link."""
    match = GUTENBERG_NEWS_BOOK_LINK_RE.search(link or "")
    if match is None:
        return ""
    value = next((group for group in match.groups() if group), "")
    return str(int(value)) if value else ""

def _gutenberg_card_from_news_item(item: dict) -> dict | None:
    """Turn one resolved Google News story into a normalized book card.

    Google News indexes the public Gutenberg ebook pages, including their
    ``/ebooks/<id>``, ``/cache/epub/<id>/`` and ``/files/<id>/`` URLs. Only
    those links are books; category, author-search and site-homepage hits are
    ignored. Titles commonly look like "Persuasion by Jane Austen - Project
    Gutenberg", so the site suffix and author are split out when present.
    """
    if not isinstance(item, dict):
        return None
    book_id = _gutenberg_news_book_id(str(item.get("link") or ""))
    if not book_id:
        return None

    title = html.unescape(str(item.get("title") or "")).strip()
    title = GUTENBERG_NEWS_TITLE_SUFFIX_RE.sub("", title).strip()
    authors = ""
    by_match = GUTENBERG_NEWS_BY_RE.match(title)
    if by_match is not None:
        title = by_match.group("title").strip()
        authors = by_match.group("authors").strip()
    if not title:
        title = f"Book {book_id}"

    images = item.get("images")
    cover_url = ""
    if isinstance(images, list):
        cover_url = next(
            (str(value).strip() for value in images if str(value).strip()),
            "",
        )
    if not cover_url:
        cover_url = GUTENBERG_COVER_URL.format(book_id=book_id)

    return {
        "id": book_id,
        "title": title,
        "authors": authors,
        "languages": [],
        "subjects": [],
        "first_publish_year": 0,
        "download_count": 0,
        "url": GUTENBERG_BOOK_URL.format(book_id=book_id),
        "text_url": f"{GUTENBERG_BOOK_URL.format(book_id=book_id)}.txt.utf-8",
        "cover_url": cover_url,
        "source": "google_news",
    }

def _merge_gutenberg_news_card(existing: dict, incoming: dict) -> bool:
    """Fill gaps on a duplicate card (for example its missing author).

    The same ebook can appear through both its reading page and its canonical
    ``/ebooks/<id>`` page, and only one of those titles may include the author.
    Returns True when the existing card was changed.
    """
    changed = False
    for key in (
        "authors",
        "languages",
        "subjects",
        "first_publish_year",
        "download_count",
        "cover_url",
    ):
        if not existing.get(key) and incoming.get(key):
            existing[key] = incoming[key]
            changed = True
    return changed

def search_gutenberg_books_google_news(
    keyword: str,
    limit: int = GUTENBERG_BOOK_RESULT_LIMIT,
    on_item: Callable[[list[dict]], None] | None = None,
) -> list[dict]:
    """Search Gutenberg books through the Google News RSS mechanism.

    This deliberately reuses ``news_fetch.php``'s dynamic googlenews source so
    the search behaves exactly like ``/news search``: the query is restricted
    with ``site:gutenberg.org``, Google News redirect links are resolved by the
    existing reader, and each NDJSON ``item`` event becomes a book card. The
    callback receives the accumulated cards as they arrive so the browser can
    render results progressively.
    """
    keyword = keyword.strip()
    if not keyword:
        return []
    limit = max(1, limit)
    script = SCRIPT_DIR / "news_fetch.php"
    if not script.is_file():
        raise RuntimeError(f"missing {script.name}")

    query = f"{GUTENBERG_NEWS_SITE_QUERY} {keyword}"
    # Some hits are category or search pages rather than books, so request
    # more feed items than the number of cards we ultimately want.
    argv = [
        "php",
        str(script),
        "--json",
        "--source",
        "googlenews",
        "--search",
        query,
        "--limit",
        str(max(limit * 2, 20)),
    ]
    try:
        process = subprocess.Popen(
            argv,
            cwd=SCRIPT_DIR,
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            text=True,
            bufsize=1,
        )
    except OSError as exc:
        raise RuntimeError(f"could not run {script.name}: {exc}") from exc

    assert process.stdout is not None
    cards: dict[str, dict] = {}
    stopped = False
    try:
        for raw_line in process.stdout:
            line = raw_line.strip()
            if not line:
                continue
            try:
                payload = json.loads(line)
            except json.JSONDecodeError:
                continue
            if not isinstance(payload, dict) or payload.get("type") != "item":
                continue
            card = _gutenberg_card_from_news_item(payload)
            if card is None:
                continue
            existing = cards.get(card["id"])
            if existing is not None:
                if on_item is not None and _merge_gutenberg_news_card(
                    existing, card
                ):
                    on_item(list(cards.values()))
                continue
            if len(cards) >= limit:
                stopped = True
                break
            cards[card["id"]] = card
            GUTENBERG_CATALOG_CACHE[card["id"]] = card
            if on_item is not None:
                on_item(list(cards.values()))
        if stopped:
            process.terminate()
            try:
                process.wait(timeout=5)
            except subprocess.TimeoutExpired:
                process.kill()
                process.wait()
        else:
            process.wait()
    except BaseException:
        process.terminate()
        try:
            process.wait(timeout=5)
        except subprocess.TimeoutExpired:
            process.kill()
            process.wait()
        raise
    return list(cards.values())[:limit]

def _clean_gutenberg_plaintext(raw_text: str) -> str:
    """Remove the Project Gutenberg header/footer from one downloaded book."""
    text = raw_text.replace("\r\n", "\n").replace("\r", "\n").lstrip("\ufeff")
    start = GUTENBERG_START_MARKER_RE.search(text)
    if start is not None:
        text = text[start.end():]
    end = GUTENBERG_END_MARKER_RE.search(text)
    if end is not None:
        text = text[:end.start()]
    text = GUTENBERG_OLD_END_RE.sub("", text)
    return text.strip() + "\n"

def _gutenberg_rag_text(text: str, title: str) -> str:
    """Make a book-friendly source for the existing article/RAG splitter.

    Project Gutenberg files use hard-wrapped prose and plain ``CHAPTER I``
    lines. Promote those lines to Markdown headings so the article structure
    builder can produce section summaries, and put the title first so overview
    questions retrieve a useful document heading.
    """
    lines: list[str] = []
    for line in text.splitlines():
        stripped = line.strip()
        if stripped and GUTENBERG_CHAPTER_RE.match(stripped):
            lines.append(f"\n## {stripped}\n")
        else:
            lines.append(line)
    body = "\n".join(lines).strip()
    if title and title.casefold() not in body[:500].casefold():
        body = f"# {title}\n\n{body}"
    return body + "\n"

def _gutenberg_book_paths(book_id: str) -> tuple[Path, Path]:
    GUTENBERG_BOOK_CACHE_DIR.mkdir(parents=True, exist_ok=True)
    return (
        GUTENBERG_BOOK_CACHE_DIR / f"{book_id}.txt",
        GUTENBERG_BOOK_CACHE_DIR / f"{book_id}.rag.txt",
    )

def _gutenberg_download_lock(book_id: str) -> threading.Lock:
    with GUTENBERG_DOWNLOAD_LOCK:
        lock = GUTENBERG_DOWNLOAD_LOCKS.get(book_id)
        if lock is None:
            lock = threading.Lock()
            GUTENBERG_DOWNLOAD_LOCKS[book_id] = lock
        return lock

def ensure_gutenberg_book(
    book_id: str,
    text_url: str = "",
    title: str = "",
) -> tuple[str, Path]:
    """Download (once) and return a book's cleaned plaintext and text path."""
    if not GUTENBERG_BOOK_ID_RE.fullmatch(book_id):
        raise ValueError("Missing or invalid Project Gutenberg book id")
    text_path, rag_path = _gutenberg_book_paths(book_id)
    if text_path.is_file() and rag_path.is_file():
        try:
            return text_path.read_text(encoding="utf-8"), rag_path
        except OSError:
            pass

    catalog = GUTENBERG_CATALOG_CACHE.get(book_id) or {}
    text_url = (text_url or catalog.get("text_url") or "").strip()
    title = (title or catalog.get("title") or "").strip()
    if not text_url:
        # This URL pattern redirects to the available plaintext edition for
        # nearly every English-language Gutenberg book.
        text_url = f"https://www.gutenberg.org/ebooks/{book_id}.txt.utf-8"

    with _gutenberg_download_lock(book_id):
        if text_path.is_file() and rag_path.is_file():
            return text_path.read_text(encoding="utf-8"), rag_path
        try:
            response = requests.get(
                text_url,
                headers={**chat.HTTP_HEADERS, "Accept": "text/plain"},
                stream=True,
                timeout=(10, 90),
                allow_redirects=True,
            )
            response.raise_for_status()
            chunks = bytearray()
            for block in response.iter_content(chunk_size=256 * 1024):
                if not block:
                    continue
                chunks.extend(block)
                if len(chunks) > GUTENBERG_TEXT_MAX_BYTES:
                    raise RuntimeError(
                        "Book plaintext is too large to cache "
                        f"(limit {GUTENBERG_TEXT_MAX_BYTES:,} bytes)."
                    )
        except requests.RequestException as exc:
            raise RuntimeError(f"Could not download the book: {exc}") from exc

        raw_text = _decode_article_bytes(bytes(chunks))
        text = _clean_gutenberg_plaintext(raw_text)
        if not text.strip():
            raise RuntimeError("The downloaded book has no readable text.")
        if not title:
            title = next(
                (line.strip() for line in text.splitlines() if line.strip()),
                f"Book {book_id}",
            )[:200]

        text_path.parent.mkdir(parents=True, exist_ok=True)
        temp_text = text_path.with_name(text_path.name + ".tmp")
        temp_rag = rag_path.with_name(rag_path.name + ".tmp")
        temp_text.write_text(text, encoding="utf-8")
        temp_rag.write_text(_gutenberg_rag_text(text, title), encoding="utf-8")
        temp_text.replace(text_path)
        temp_rag.replace(rag_path)
        return text, rag_path


# Cross-component imports are placed after the definitions so a
# feature module can depend on another feature module safely.
from .article import GUTENBERG_CATALOG_CACHE, GUTENBERG_DOWNLOAD_LOCKS, _decode_article_bytes
from .command_config import GUTENBERG_BOOK_CACHE_DIR, GUTENBERG_BOOK_ID_RE, GUTENBERG_BOOK_URL, GUTENBERG_CHAPTER_RE, GUTENBERG_COVER_URL, GUTENBERG_END_MARKER_RE, GUTENBERG_NEWS_BOOK_LINK_RE, GUTENBERG_NEWS_BY_RE, GUTENBERG_NEWS_SITE_QUERY, GUTENBERG_NEWS_TITLE_SUFFIX_RE, GUTENBERG_OLD_END_RE, GUTENBERG_SEARCH_AUTHOR_RE, GUTENBERG_SEARCH_BLOCK_RE, GUTENBERG_SEARCH_DOWNLOADS_RE, GUTENBERG_SEARCH_EXTRA_RE, GUTENBERG_SEARCH_LINK_RE, GUTENBERG_SEARCH_TITLE_RE, GUTENBERG_SEARCH_URL, GUTENBERG_START_MARKER_RE, GUTENBERG_TEXT_MAX_BYTES, INTERNET_ARCHIVE_COVER_URL, INTERNET_ARCHIVE_DESCRIPTION_LIMIT, INTERNET_ARCHIVE_DETAILS_URL, INTERNET_ARCHIVE_DOWNLOAD_URL, INTERNET_ARCHIVE_FTS_FIELDS, INTERNET_ARCHIVE_FTS_URL, INTERNET_ARCHIVE_HEADERS, INTERNET_ARCHIVE_HIGHLIGHT_LIMIT, INTERNET_ARCHIVE_METADATA_CACHE, INTERNET_ARCHIVE_METADATA_CACHE_LOCK, INTERNET_ARCHIVE_METADATA_URL, INTERNET_ARCHIVE_RESTRICTED_COLLECTIONS, INTERNET_ARCHIVE_RESTRICTED_LENDING_STATUSES, INTERNET_ARCHIVE_SEARCH_FIELDS, INTERNET_ARCHIVE_SEARCH_URL, OPENLIBRARY_BOOK_PAGE_URL, OPENLIBRARY_COVER_ID_URL, OPENLIBRARY_COVER_OLID_URL, OPENLIBRARY_DESCRIPTION_CACHE, OPENLIBRARY_DESCRIPTION_CACHE_LOCK, OPENLIBRARY_DESCRIPTION_LIMIT, OPENLIBRARY_HIGHLIGHT_LIMIT, OPENLIBRARY_INSIDE_SEARCH_URL, OPENLIBRARY_SEARCH_FIELDS, OPENLIBRARY_SEARCH_URL, OPENLIBRARY_WORK_JSON_URL
from .core import SCRIPT_DIR
