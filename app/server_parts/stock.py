"""Stock quotes, ETF holdings, company profiles, and related stories."""

from __future__ import annotations

from .core import *

# /stock resolves a ticker through CNBC's public quote service and enriches it
# with Nasdaq's company profile/financial statements and a Wikipedia summary.
# Related stories come from Google News through news_fetch.php, so /stock uses
# the same story-list and detail modal as /news without touching Yahoo Finance.
STOCK_CNBC_QUOTE_URL = (
    "https://quote.cnbc.com/quote-html-webservice/restQuote/symbolType/symbol"
)

STOCK_NASDAQ_QUOTE_URL = "https://api.nasdaq.com/api/quote"

STOCK_NASDAQ_COMPANY_URL = "https://api.nasdaq.com/api/company"

STOCK_NASDAQ_AUTOCOMPLETE_URL = (
    "https://api.nasdaq.com/api/autocomplete/slookup/10"
)

STOCK_WIKIPEDIA_SEARCH_URL = "https://en.wikipedia.org/w/api.php"

STOCK_WIKIPEDIA_SUMMARY_URL = (
    "https://en.wikipedia.org/api/rest_v1/page/summary"
)

STOCK_USER_AGENT = (
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
    "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/131.0.0.0 Safari/537.36"
)

STOCK_CACHE_TTL = 600.0

STOCK_STALE_CACHE_TTL = 86_400.0

STOCK_CACHE_MAX_ENTRIES = 32

_STOCK_CACHE: dict[str, tuple[float, dict]] = {}

_STOCK_CACHE_LOCK = threading.Lock()

# ETF holdings show each constituent's next earnings date. CNBC's quote feed
# already carries EventData.next_earnings_date, so no extra provider is needed.
# Dates change rarely, so cache them longer than quotes; a miss is retried
# sooner in case the upstream response was temporarily incomplete.
STOCK_EARNINGS_CACHE_TTL = 3_600.0

STOCK_EARNINGS_MISS_TTL = 300.0

_STOCK_EARNINGS_CACHE: dict[str, tuple[float, str]] = {}

_STOCK_EARNINGS_CACHE_LOCK = threading.Lock()

STOCK_INCOME_ROWS = (
    "Total Revenue",
    "Gross Profit",
    "Operating Income",
    "Net Income",
    "Net Income Applicable to Common Shareholders",
)

# ETFs publish their portfolio holdings every business day. Direxion exposes a
# per-fund CSV; StockAnalysis.com renders the same top-holdings table for most
# other issuers. Both sources are independent of Yahoo Finance.
STOCK_DIREXION_HOLDINGS_URL = "https://www.direxion.com/holdings/{symbol}.csv"

STOCK_ANALYSIS_HOLDINGS_URL = "https://stockanalysis.com/etf/{symbol}/holdings/"

STOCK_HOLDINGS_LIMIT = 10

_STOCK_HOLDINGS_NON_EQUITY = re.compile(
    r"\b(SWAP|CASH|TREASUR|TRSRY|TREASURY|GOVERNMENT|GOVT|MONEY MARKET|"
    r"DREYFUS|GOLDMAN|J\.?P\.? MORGAN|MUNICIPAL)\b",
    re.IGNORECASE,
)

_STOCK_HOLDINGS_TICKER = re.compile(r"[A-Z][A-Z0-9.\-]{0,11}")

def _stock_value(value) -> str:
    """Render one Nasdaq/CNBC field, unwrapping their label/value objects."""
    if value is None:
        return ""
    if isinstance(value, dict):
        for key in ("value", "fmt", "longFmt"):
            formatted = value.get(key)
            if formatted not in (None, ""):
                text = str(formatted).strip()
                if text and text not in {"--", "-", "N/A", "n/a", "NA"}:
                    return text
        return ""
    if isinstance(value, bool):
        return "Yes" if value else "No"
    if isinstance(value, int):
        return f"{value:,}"
    if isinstance(value, float):
        if not math.isfinite(value):
            return ""
        if abs(value) >= 1000:
            return f"{value:,.2f}".rstrip("0").rstrip(".")
        return f"{value:,.6f}".rstrip("0").rstrip(".")
    text = str(value).strip()
    return "" if text in {"--", "-", "N/A", "n/a", "NA"} else text

def _stock_summary_text(value: str, limit: int = 900) -> str:
    """Collapse a long company description and trim it at a sentence boundary."""
    text = re.sub(r"\s+", " ", str(value or "")).strip()
    if len(text) <= limit:
        return text
    cut = text[:limit]
    boundary = max(cut.rfind(". "), cut.rfind("; "))
    if boundary >= limit // 2:
        cut = cut[: boundary + 1]
    return cut.rstrip() + " ..."

def _stock_cache_key(symbol: str) -> str:
    """Normalize a symbol or query for the in-memory stock cache."""
    return " ".join(str(symbol or "").split()).casefold()

def _stock_cache_read(key: str, ttl: float, *, allow_stale: bool = False):
    """Return (value, is_stale) for a cache entry, or None on a miss."""
    now = time.time()
    with _STOCK_CACHE_LOCK:
        entry = _STOCK_CACHE.get(key)
        if entry is None:
            return None
        stored, value = entry
        age = now - stored
        if age <= ttl:
            return value, False
        if allow_stale and age <= STOCK_STALE_CACHE_TTL:
            return value, True
        if age > STOCK_STALE_CACHE_TTL:
            _STOCK_CACHE.pop(key, None)
        return None

def _stock_cache_store(key: str, value: dict) -> None:
    with _STOCK_CACHE_LOCK:
        if len(_STOCK_CACHE) >= STOCK_CACHE_MAX_ENTRIES and key not in _STOCK_CACHE:
            oldest = min(_STOCK_CACHE, key=lambda item: _STOCK_CACHE[item][0])
            _STOCK_CACHE.pop(oldest, None)
        _STOCK_CACHE[key] = (time.time(), value)

def _stock_get_json(url: str, params: dict, *, referer: str = ""):
    """Fetch one public JSON endpoint with browser-like request headers."""
    headers = {
        "User-Agent": STOCK_USER_AGENT,
        "Accept": "application/json, text/plain, */*",
        "Accept-Language": "en-US,en;q=0.9",
    }
    if referer:
        headers["Origin"] = "https://www.nasdaq.com"
        headers["Referer"] = referer
    try:
        response = requests.get(
            url, params=params, headers=headers, timeout=(8, 25)
        )
    except requests.RequestException as exc:
        raise RuntimeError(f"stock data request failed: {exc}") from exc
    if response.status_code != 200:
        raise RuntimeError(
            f"stock data request returned HTTP {response.status_code}."
        )
    try:
        payload = response.json()
    except ValueError as exc:
        raise RuntimeError(f"stock data request returned invalid JSON: {exc}") from exc
    return payload if isinstance(payload, (dict, list)) else {}

def fetch_cnbc_quote(symbol: str) -> dict:
    """Resolve one symbol through CNBC's public quote endpoint.

    CNBC covers US and many international exchanges without an API key. The
    response carries the quote, valuation ratios, margins, and dividend data
    that used to come from quoteSummary. An unknown symbol yields ``code`` 1
    and is returned as an empty dict.
    """
    query = " ".join(str(symbol or "").split())
    if not query:
        return {}
    payload = _stock_get_json(
        STOCK_CNBC_QUOTE_URL,
        {
            "symbols": query,
            "requestMethod": "itv",
            "noform": "1",
            "partnerId": "2",
            "fund": "1",
            "exthrs": "1",
            "output": "json",
            "events": "1",
        },
    )
    result = payload.get("FormattedQuoteResult")
    quotes = result.get("FormattedQuote") if isinstance(result, dict) else None
    if not isinstance(quotes, list) or not quotes:
        return {}
    quote = quotes[0] if isinstance(quotes[0], dict) else {}
    try:
        code = int(quote.get("code") or 0)
    except (TypeError, ValueError):
        code = 0
    if code != 0 or not str(quote.get("symbol") or "").strip():
        return {}
    return quote

def fetch_nasdaq_autocomplete(query: str) -> list[dict]:
    """Return Nasdaq's symbol suggestions, used when a CNBC name lookup fails."""
    text = " ".join(str(query or "").split())
    if not text:
        return []
    try:
        payload = _stock_get_json(
            STOCK_NASDAQ_AUTOCOMPLETE_URL,
            {"search": text},
            referer="https://www.nasdaq.com/",
        )
    except RuntimeError:
        return []
    data = payload.get("data")
    if not isinstance(data, list):
        return []
    return [row for row in data if isinstance(row, dict)]

def resolve_stock_symbol(query: str) -> tuple[dict, str]:
    """Resolve a ticker or company name to (CNBC quote, resolved symbol)."""
    text = " ".join(str(query or "").split())
    if not text:
        return {}, ""
    quote = fetch_cnbc_quote(text)
    if quote:
        return quote, str(quote.get("symbol") or text).strip().upper()

    # A company name such as "apple" is not a CNBC symbol. Nasdaq's
    # autocomplete maps it to a US ticker; verify each suggestion through CNBC
    # before committing to it.
    candidates = fetch_nasdaq_autocomplete(text)
    tried: set[str] = set()
    for candidate in candidates[:5]:
        candidate_symbol = str(candidate.get("symbol") or "").strip()
        if not candidate_symbol or candidate_symbol.casefold() in tried:
            continue
        tried.add(candidate_symbol.casefold())
        quote = fetch_cnbc_quote(candidate_symbol)
        if quote:
            return quote, str(quote.get("symbol") or candidate_symbol).upper()
    return {}, text.upper()

def fetch_nasdaq_summary(symbol: str, asset_class: str) -> dict:
    """Return Nasdaq's summary table (target, yield, AUM, expense ratio, ...)."""
    try:
        payload = _stock_get_json(
            f"{STOCK_NASDAQ_QUOTE_URL}/{quote(symbol, safe='')}/summary",
            {"assetclass": asset_class},
            referer="https://www.nasdaq.com/",
        )
    except RuntimeError:
        return {}
    data = payload.get("data")
    if not isinstance(data, dict):
        return {}
    summary = data.get("summaryData")
    return summary if isinstance(summary, dict) else {}

def fetch_nasdaq_company_profile(symbol: str) -> dict:
    """Return Nasdaq's company description, sector, industry, and website."""
    try:
        payload = _stock_get_json(
            f"{STOCK_NASDAQ_COMPANY_URL}/{quote(symbol, safe='')}/company-profile",
            {"assetclass": "stocks"},
            referer="https://www.nasdaq.com/",
        )
    except RuntimeError:
        return {}
    data = payload.get("data")
    return data if isinstance(data, dict) else {}

def fetch_nasdaq_financials(symbol: str) -> dict:
    """Return Nasdaq's annual income statement (latest columns first)."""
    try:
        payload = _stock_get_json(
            f"{STOCK_NASDAQ_COMPANY_URL}/{quote(symbol, safe='')}/financials",
            {"frequency": "1", "assetclass": "stocks"},
            referer="https://www.nasdaq.com/",
        )
    except RuntimeError:
        return {}
    data = payload.get("data")
    return data if isinstance(data, dict) else {}

def _stock_holding_weight(value) -> float:
    """Parse a holdings percentage such as '5.85%' or '5.8465700134'."""
    match = re.search(r"-?\d+(?:\.\d+)?", str(value or "").replace(",", ""))
    if not match:
        return 0.0
    try:
        return float(match.group())
    except ValueError:
        return 0.0

def _stock_holding_symbol(value) -> str:
    symbol = str(value or "").strip().upper().lstrip("$")
    if symbol in {"", "N/A", "NA", "NONE", "-", "--"}:
        return ""
    return symbol if _STOCK_HOLDINGS_TICKER.fullmatch(symbol) else ""

def _stock_is_equity_holding(symbol: str, name: str) -> bool:
    """Keep operating-company positions, not swaps, cash, or treasuries."""
    if not symbol:
        return False
    return not _STOCK_HOLDINGS_NON_EQUITY.search(str(name or ""))

def _stock_holding_row(
    symbol: str,
    name: str,
    weight: float,
    shares: str = "",
    value: str = "",
) -> dict:
    return {
        "symbol": symbol,
        "name": " ".join(str(name or "").split()),
        "weight": weight,
        "shares": " ".join(str(shares or "").split()),
        "value": " ".join(str(value or "").split()),
    }

def _stock_trade_date(value: str) -> str:
    """Return the date portion of a Direxion TradeDate field."""
    text = " ".join(str(value or "").split())
    match = re.match(r"(\d{1,2}/\d{1,2}/\d{4})", text)
    return match.group(1) if match else text

def fetch_direxion_etf_holdings(symbol: str) -> dict:
    """Fetch one Direxion fund's daily holdings CSV."""
    ticker = str(symbol or "").strip().upper()
    if not ticker:
        return {}
    url = STOCK_DIREXION_HOLDINGS_URL.format(symbol=quote(ticker, safe=""))
    try:
        response = requests.get(
            url,
            headers={
                "User-Agent": STOCK_USER_AGENT,
                "Accept": "text/csv,text/plain,*/*",
            },
            timeout=(8, 25),
        )
    except requests.RequestException:
        return {}
    if response.status_code != 200:
        return {}
    lines = response.text.splitlines()
    header_index = next(
        (
            index
            for index, line in enumerate(lines)
            if "AccountTicker" in line and "HoldingsPercent" in line
        ),
        None,
    )
    if header_index is None:
        return {}
    try:
        rows = list(csv.DictReader(lines[header_index:]))
    except csv.Error:
        return {}
    holdings: list[dict] = []
    as_of = ""
    for row in rows:
        holding_symbol = _stock_holding_symbol(row.get("StockTicker"))
        name = row.get("SecurityDescription") or ""
        weight = _stock_holding_weight(row.get("HoldingsPercent"))
        if not _stock_is_equity_holding(holding_symbol, name) or weight <= 0:
            continue
        as_of = as_of or _stock_trade_date(row.get("TradeDate") or "")
        holdings.append(
            _stock_holding_row(
                holding_symbol,
                name,
                weight,
                row.get("Shares") or "",
                row.get("MarketValue") or "",
            )
        )
    if not holdings:
        return {}
    holdings.sort(key=lambda item: item["weight"], reverse=True)
    return {
        "source": "Direxion daily fund holdings",
        "source_url": url,
        "as_of": as_of,
        "total": len(rows),
        "holdings": holdings,
    }

class _StockHoldingsTableParser(HTMLParser):
    """Collect the rows of StockAnalysis.com's holdings table."""

    def __init__(self) -> None:
        super().__init__()
        self._in_table = False
        self._in_row = False
        self._in_cell = False
        self._cell: list[str] = []
        self._row: list[str] = []
        self.rows: list[list[str]] = []

    def handle_starttag(self, tag: str, attrs) -> None:
        attributes = dict(attrs)
        if tag == "table" and attributes.get("id") == "main-table":
            self._in_table = True
        elif self._in_table and tag == "tr":
            self._in_row = True
            self._row = []
        elif self._in_row and tag in {"td", "th"}:
            self._in_cell = True
            self._cell = []

    def handle_endtag(self, tag: str) -> None:
        if tag == "table" and self._in_table:
            self._in_table = False
        elif self._in_row and tag == "tr":
            if self._row:
                self.rows.append(self._row)
            self._in_row = False
            self._row = []
        elif self._in_cell and tag in {"td", "th"}:
            self._row.append(" ".join("".join(self._cell).split()))
            self._in_cell = False
            self._cell = []

    def handle_data(self, data: str) -> None:
        if self._in_cell:
            self._cell.append(data)

def fetch_stockanalysis_etf_holdings(symbol: str) -> dict:
    """Fetch an ETF's top holdings from StockAnalysis.com's public table."""
    ticker = str(symbol or "").strip().upper()
    if not ticker:
        return {}
    url = STOCK_ANALYSIS_HOLDINGS_URL.format(symbol=quote(ticker.lower(), safe=""))
    try:
        response = requests.get(
            url,
            headers={
                "User-Agent": STOCK_USER_AGENT,
                "Accept": "text/html,application/xhtml+xml",
                "Accept-Language": "en-US,en;q=0.9",
            },
            timeout=(8, 25),
        )
    except requests.RequestException:
        return {}
    if response.status_code != 200 or "main-table" not in response.text:
        return {}
    parser = _StockHoldingsTableParser()
    try:
        parser.feed(response.text)
    except Exception:
        return {}
    holdings: list[dict] = []
    for row in parser.rows:
        if len(row) < 4 or row[0].strip().casefold().rstrip(".") == "no":
            continue
        holding_symbol = _stock_holding_symbol(row[1])
        name = row[2]
        weight = _stock_holding_weight(row[3])
        if not _stock_is_equity_holding(holding_symbol, name) or weight <= 0:
            continue
        holdings.append(
            _stock_holding_row(
                holding_symbol,
                name,
                weight,
                row[4] if len(row) > 4 else "",
            )
        )
    if not holdings:
        return {}
    holdings.sort(key=lambda item: item["weight"], reverse=True)
    as_of_match = re.search(r'Updated:"?([A-Z][a-z]{2} \d{1,2}, \d{4})"?', response.text)
    total_match = re.search(
        r"total of\s+([\d,]+)\s+individual holdings", response.text, re.I
    )
    total = 0
    if total_match:
        try:
            total = int(total_match.group(1).replace(",", ""))
        except ValueError:
            total = 0
    return {
        "source": "StockAnalysis.com ETF holdings",
        "source_url": url,
        "as_of": as_of_match.group(1) if as_of_match else "",
        "total": total or len(holdings),
        "holdings": holdings,
    }

def fetch_etf_holdings(symbol: str) -> dict:
    """Resolve ETF holdings without using Yahoo Finance."""
    for fetcher in (
        fetch_direxion_etf_holdings,
        fetch_stockanalysis_etf_holdings,
    ):
        try:
            data = fetcher(symbol)
        except Exception:
            data = {}
        if isinstance(data, dict) and data.get("holdings"):
            return data
    return {}

def fetch_stock_next_earnings_date(symbol: str) -> str:
    """Return one stock's next earnings date from CNBC's quote events.

    CNBC's public quote response includes ``EventData.next_earnings_date`` for
    most US and international listings (for example ``11/17/2026``). The value
    is cached per symbol because ETF constituents change slowly and one /stock
    request asks about every major holding.
    """
    ticker = str(symbol or "").strip().upper()
    if not ticker:
        return ""
    now = time.time()
    with _STOCK_EARNINGS_CACHE_LOCK:
        cached = _STOCK_EARNINGS_CACHE.get(ticker)
        if cached is not None:
            stored, value = cached
            ttl = STOCK_EARNINGS_CACHE_TTL if value else STOCK_EARNINGS_MISS_TTL
            if now - stored <= ttl:
                return value

    try:
        quote = fetch_cnbc_quote(ticker)
    except RuntimeError:
        quote = {}
    events = quote.get("EventData")
    value = ""
    if isinstance(events, dict):
        value = " ".join(
            str(events.get("next_earnings_date") or "").split()
        )
    with _STOCK_EARNINGS_CACHE_LOCK:
        _STOCK_EARNINGS_CACHE[ticker] = (time.time(), value)
    return value

def fetch_holding_earnings_dates(holdings: dict) -> dict[str, str]:
    """Fetch next earnings dates for an ETF's displayed stock holdings."""
    rows = holdings.get("holdings") if isinstance(holdings, dict) else None
    if not isinstance(rows, list) or not rows:
        return {}
    symbols: list[str] = []
    seen: set[str] = set()
    for row in rows[:STOCK_HOLDINGS_LIMIT]:
        if not isinstance(row, dict):
            continue
        symbol = str(row.get("symbol") or "").strip().upper()
        if not symbol or symbol in seen:
            continue
        seen.add(symbol)
        symbols.append(symbol)
    if not symbols:
        return {}
    with ThreadPoolExecutor(max_workers=min(8, len(symbols))) as executor:
        dates = list(executor.map(fetch_stock_next_earnings_date, symbols))
    return dict(zip(symbols, dates))

def stock_holdings_payload(
    holdings: dict, earnings_dates: dict[str, str] | None = None
) -> dict:
    """Return structured ETF holdings for the browser's clickable list."""
    rows = holdings.get("holdings") if isinstance(holdings, dict) else None
    if not isinstance(rows, list) or not rows:
        return {}
    if earnings_dates is None:
        earnings_dates = fetch_holding_earnings_dates(holdings)
    items: list[dict] = []
    for row in rows[:STOCK_HOLDINGS_LIMIT]:
        if not isinstance(row, dict):
            continue
        symbol = str(row.get("symbol") or "").strip().upper()
        if not symbol:
            continue
        try:
            weight = float(row.get("weight") or 0)
        except (TypeError, ValueError):
            weight = 0.0
        items.append(
            {
                "symbol": symbol,
                "name": " ".join(str(row.get("name") or "").split()),
                "weight": round(weight, 2) if weight > 0 else 0,
                "earnings_date": str(earnings_dates.get(symbol) or "").strip(),
            }
        )
    if not items:
        return {}
    return {
        "source": str(holdings.get("source") or "ETF holdings"),
        "as_of": str(holdings.get("as_of") or "").strip(),
        "total": holdings.get("total"),
        "holdings": items,
    }

def fetch_wikipedia_company_summary(name: str) -> dict:
    """Return the English Wikipedia summary for a company name, if any."""
    text = " ".join(str(name or "").split())
    if not text:
        return {}
    try:
        payload = _stock_get_json(
            STOCK_WIKIPEDIA_SEARCH_URL,
            {
                "action": "opensearch",
                "search": text,
                "limit": "8",
                "namespace": "0",
                "format": "json",
            },
        )
    except RuntimeError:
        return {}
    titles = payload[1] if isinstance(payload, list) and len(payload) > 1 else []
    if not isinstance(titles, list) or not titles:
        return {}

    # Opensearch can rank a plant, subsidiary, or list page above the company
    # itself. Prefer an exact/near-exact title and penalise those narrow pages.
    query_tokens = set(re.findall(r"[a-z0-9]+", text.casefold()))
    best_title = ""
    best_score = float("-inf")
    for candidate in titles[:8]:
        title_text = str(candidate or "").strip()
        if not title_text:
            continue
        candidate_lower = title_text.casefold()
        candidate_tokens = set(re.findall(r"[a-z0-9]+", candidate_lower))
        overlap = len(query_tokens & candidate_tokens)
        extra = len(candidate_tokens - query_tokens)
        score = overlap * 10 - extra * 3 - len(candidate_tokens) * 0.1
        if candidate_lower == text.casefold():
            score += 100
        if any(
            word in candidate_lower
            for word in (
                "plant",
                "factory",
                "headquarters",
                "building",
                "subsidiary",
                "list of",
                "disambiguation",
            )
        ):
            score -= 50
        if score > best_score:
            best_score = score
            best_title = title_text
    title = best_title
    if not title:
        return {}
    try:
        summary = _stock_get_json(
            f"{STOCK_WIKIPEDIA_SUMMARY_URL}/{quote(title, safe='')}", {}
        )
    except RuntimeError:
        return {}
    if str(summary.get("type") or "") == "disambiguation":
        return {}
    extract = _stock_summary_text(summary.get("extract"), 900)
    if not extract:
        return {}
    content_urls = summary.get("content_urls")
    desktop = content_urls.get("desktop") if isinstance(content_urls, dict) else None
    url = desktop.get("page") if isinstance(desktop, dict) else ""
    return {
        "title": str(summary.get("title") or title),
        "extract": extract,
        "description": str(summary.get("description") or ""),
        "url": str(url or ""),
    }

def stock_quote_description(
    quote: dict,
    profile: dict,
    summary: dict,
    financials: dict,
    wiki: dict,
    symbol: str,
    holdings: dict | None = None,
) -> str:
    """Compose the /stock header from CNBC, Nasdaq, and Wikipedia data."""
    name = str(quote.get("name") or quote.get("altName") or symbol).strip()
    resolved = str(quote.get("symbol") or symbol).strip().upper()
    subtype = str(quote.get("subType") or quote.get("type") or "").strip()
    exchange = str(quote.get("exchange") or "").strip()
    country = str(quote.get("countryCode") or "").strip()
    sector = _stock_value(profile.get("Sector")) or _stock_value(
        summary.get("Sector")
    )
    industry = _stock_value(profile.get("Industry")) or _stock_value(
        summary.get("Industry")
    )
    website = _stock_value(profile.get("CompanyUrl"))

    intro = f"{name} ({resolved})"
    if subtype:
        intro += f" is listed as {subtype}"
    if exchange:
        intro += f" on {exchange}"
    if country:
        intro += f" ({country})"
    intro += "."
    if sector:
        intro += f" Sector: {sector}."
    if industry:
        intro += f" Industry: {industry}."

    sections: list[str] = [intro]

    company: list[str] = []
    if sector:
        company.append(f"Sector: {sector}")
    if industry:
        company.append(f"Industry: {industry}")
    region = _stock_value(profile.get("Region")) or country
    if region:
        company.append(f"Region: {region}")
    if website:
        company.append(f"Website: {website}")
    if company:
        sections.append("Company\n" + "\n".join(company))

    business = _stock_summary_text(
        _stock_value(profile.get("CompanyDescription"))
    )
    if business:
        sections.append("Business summary\n" + business)
    elif wiki.get("extract"):
        label = "About (Wikipedia)"
        if wiki.get("url"):
            label += f"\n{wiki['url']}"
        sections.append(label + "\n" + str(wiki["extract"]))

    financial_lines: list[str] = []

    def add_financial(label: str, value, *, suffix: str = "") -> None:
        text = _stock_value(value)
        if text:
            financial_lines.append(f"{label}: {text}{suffix}")

    currency = str(quote.get("currencyCode") or "").strip()
    last = _stock_value(quote.get("last"))
    change = _stock_value(quote.get("change"))
    percent = _stock_value(quote.get("change_pct"))
    if last:
        line = last + (f" {currency}" if currency else "")
        if change or percent:
            line += f" ({change}{', ' if change and percent else ''}{percent})"
        add_financial("Last price", line)
    add_financial("Previous close", quote.get("previous_day_closing"))
    low = _stock_value(quote.get("low"))
    high = _stock_value(quote.get("high"))
    if low or high:
        add_financial("Day range", f"{low or '?'} - {high or '?'}")
    year_low = _stock_value(quote.get("yrloprice"))
    year_high = _stock_value(quote.get("yrhiprice"))
    if year_low or year_high:
        add_financial("52-week range", f"{year_low or '?'} - {year_high or '?'}")
    add_financial("Volume", quote.get("volume"))
    add_financial("Market cap", quote.get("mktcapView"))
    add_financial("Enterprise value (millions)", quote.get("enterpriseValue"))
    add_financial("P/E (TTM)", quote.get("pe"))
    add_financial("Forward P/E", quote.get("fpe"))
    add_financial("EPS (TTM)", quote.get("eps"))
    add_financial("Forward EPS", quote.get("feps"))
    add_financial("Revenue (TTM)", quote.get("revenuettm"))
    add_financial("Gross margin", quote.get("GROSMGNTTM"))
    add_financial("Net profit margin", quote.get("NETPROFTTM"))
    add_financial("Return on equity", quote.get("ROETTM"))
    add_financial("Debt/equity", quote.get("DEBTEQTYQ"))
    add_financial("Beta", quote.get("beta"))
    dividend = _stock_value(quote.get("dividend"))
    dividend_yield = _stock_value(quote.get("dividendyield"))
    if dividend:
        add_financial(
            "Dividend",
            f"{dividend} ({dividend_yield})" if dividend_yield else dividend,
        )
    add_financial("Shares outstanding", quote.get("sharesout"))
    add_financial("Cash (MRQ, millions)", quote.get("cashmrq"))
    target = _stock_value(summary.get("OneYrTarget"))
    if target:
        add_financial("1-year target", target)
    expense_ratio = _stock_value(summary.get("ExpenseRatio"))
    if expense_ratio:
        add_financial("Expense ratio", expense_ratio)
    aum = _stock_value(summary.get("AUM"))
    if aum:
        add_financial("Assets under management ($000)", aum)
    market_status = str(quote.get("curmktstatus") or "").strip()
    if market_status:
        add_financial("Market status", market_status)
    events = quote.get("EventData")
    if isinstance(events, dict):
        add_financial("Next earnings", events.get("next_earnings_date"))
        add_financial("Ex-dividend date", events.get("div_ex_date"))
    if financial_lines:
        sections.append("Financials\n" + "\n".join(financial_lines))

    income = fetch_income_statement_lines(financials)
    if income:
        sections.append(income)

    # ETF holdings are rendered separately in the browser so every symbol can
    # link to its own company-summary modal. The description keeps the source
    # attribution below, and the command sends the structured holdings table
    # alongside this text.
    has_holdings = bool(
        isinstance(holdings, dict) and holdings.get("holdings")
    )
    wiki_line = ""
    if wiki.get("url") and business:
        wiki_line = f"\nWikipedia: {wiki['url']}"
    sources_used = ["CNBC quote"]
    if profile or financials or summary:
        sources_used.append("Nasdaq company profile and financials")
    if wiki:
        sources_used.append("Wikipedia")
    if has_holdings:
        sources_used.append(
            str((holdings or {}).get("source") or "ETF holdings")
        )
    sources_used.append("Google News related stories")
    sections.append("Data source: " + "; ".join(sources_used) + "." + wiki_line)
    return "\n\n".join(section for section in sections if section.strip())

def fetch_stock_company_summary(symbol: str) -> dict:
    """Return the company summary shown when an ETF holding is clicked.

    Holdings are ordinary tickers, so CNBC supplies the quote and next
    earnings date. US listings also get Nasdaq's business description and
    profile; other exchanges fall back to the English Wikipedia summary. The
    result is cached with the other /stock data so clicking several holdings
    does not repeatedly hit the upstream services.
    """
    ticker = " ".join(str(symbol or "").split()).strip()
    if not ticker:
        raise RuntimeError("Missing stock symbol.")
    cache_key = _stock_cache_key(f"company:{ticker}")
    cached = _stock_cache_read(cache_key, STOCK_CACHE_TTL)
    if cached is not None:
        return cached[0]

    quote = fetch_cnbc_quote(ticker)
    if not quote:
        quote, _ = resolve_stock_symbol(ticker)
    if not quote:
        raise RuntimeError(f"no company summary found for {ticker!r}")

    resolved = str(quote.get("symbol") or ticker).strip().upper()
    name = str(quote.get("name") or quote.get("altName") or resolved).strip()
    is_us_listing = str(quote.get("countryCode") or "").upper() == "US"
    profile = fetch_nasdaq_company_profile(resolved) if is_us_listing else {}
    wiki = fetch_wikipedia_company_summary(name)
    business = _stock_summary_text(
        _stock_value(profile.get("CompanyDescription")), 2400
    )
    summary_text = business or str(wiki.get("extract") or "").strip()
    events = quote.get("EventData")
    next_earnings = ""
    if isinstance(events, dict):
        next_earnings = " ".join(
            str(events.get("next_earnings_date") or "").split()
        )

    source_url = str(wiki.get("url") or "").strip()
    if not source_url:
        source_url = str(_stock_value(profile.get("CompanyUrl")) or "").strip()
    result = {
        "symbol": resolved,
        "name": name,
        "exchange": str(quote.get("exchange") or "").strip(),
        "asset_type": str(
            quote.get("subType") or quote.get("type") or ""
        ).strip(),
        "sector": str(_stock_value(profile.get("Sector")) or "").strip(),
        "industry": str(_stock_value(profile.get("Industry")) or "").strip(),
        "website": str(_stock_value(profile.get("CompanyUrl")) or "").strip(),
        "summary": summary_text,
        "wiki_url": str(wiki.get("url") or "").strip(),
        "last_price": _stock_value(quote.get("last")),
        "currency": str(quote.get("currencyCode") or "").strip(),
        "change": _stock_value(quote.get("change")),
        "change_pct": _stock_value(quote.get("change_pct")),
        "market_cap": _stock_value(quote.get("mktcapView")),
        "next_earnings_date": next_earnings,
        "source_url": source_url,
        "cnbc_url": "https://www.cnbc.com/quotes/" + quote_plus(
            resolved, safe=""
        ),
        "data_source": [
            source
            for source in (
                "CNBC quote",
                "Nasdaq company profile" if profile else "",
                "Wikipedia" if wiki else "",
            )
            if source
        ],
    }
    _stock_cache_store(cache_key, result)
    return result

def fetch_income_statement_lines(financials: dict) -> str:
    """Render the latest fiscal-year income statement from Nasdaq's table."""
    table = financials.get("incomeStatementTable")
    if not isinstance(table, dict):
        return ""
    headers = table.get("headers")
    headers = headers if isinstance(headers, dict) else {}
    rows = table.get("rows")
    rows = rows if isinstance(rows, list) else []
    period = _stock_value(headers.get("value2"))
    lines: list[str] = []
    wanted = {label.casefold() for label in STOCK_INCOME_ROWS}
    seen: set[str] = set()
    for row in rows:
        if not isinstance(row, dict):
            continue
        label = _stock_value(row.get("value1"))
        if not label or label.casefold() not in wanted:
            continue
        if label.casefold() in seen:
            continue
        value = _stock_value(row.get("value2"))
        if not value:
            continue
        seen.add(label.casefold())
        lines.append(f"{label}: {value}")
    if not lines:
        return ""
    heading = "Income statement"
    if period:
        heading += f" ({period})"
    return heading + "\n" + "\n".join(lines)
