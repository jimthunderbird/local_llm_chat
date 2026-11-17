#!/usr/bin/env python3
"""Search the latest GDELT 2.0 Global Knowledge Graph (GKG) news records.

Usage:
    python3 gkg_fetch.py <keyword>[:<location>] [--limit 20]

The location part is optional.  ``keyword`` is matched against the article
metadata exactly as before, while ``location`` is geocoded with OpenStreetMap
Nominatim and restricts the result to GKG records whose extracted places fall
within a radius of those coordinates (15 km by default).  Either side may be
empty:

    python3 gkg_fetch.py restaurant:"san francisco, ca"
    python3 gkg_fetch.py :"san francisco bayview, ca"

When geocoding is unavailable, location filtering falls back to matching the
place name against GDELT's location metadata.  Locations can be a city/region
name, a comma-separated address-like string, or a US state abbreviation
(``ca`` is expanded to ``california``).

The GKG is published every 15 minutes as a zipped, tab-delimited file.  This
command downloads the newest GKG files (reusing a small on-disk cache), scans
their article metadata for the keyword, and writes NDJSON events that the
browser chat server already understands for /news-style story lists:

    {"type": "start", ...}
    {"type": "item", ...}
    {"type": "item_end", ...}
    {"type": "done"}

The GKG has no article body.  Each story is therefore listed with its title,
URL, publisher, publication time, and a compact metadata summary; opening the
story in the browser asks news_fetch.php for the full article on demand.
"""

from __future__ import annotations

import argparse
import html
import io
import json
import math
import re
import sys
import time
import urllib.error
import urllib.request
import zipfile
from datetime import datetime, timedelta, timezone
from pathlib import Path
from urllib.parse import unquote, urlencode, urlparse

SCRIPT_DIR = Path(__file__).resolve().parent
CACHE_DIR = SCRIPT_DIR / ".chat" / "gdelt_gkg_cache"

LASTUPDATE_URLS = (
    "https://data.gdeltproject.org/gdeltv2/lastupdate.txt",
    "http://data.gdeltproject.org/gdeltv2/lastupdate.txt",
)
GKG_URL = "https://data.gdeltproject.org/gdeltv2/{stamp}.gkg.csv.zip"

# GDELT's GEO 2.0 API (``/api/v2/geo/geo``) is no longer served, so the same
# intent is implemented directly against the GKG files: OpenStreetMap
# geocoding turns the place name into coordinates, and each record's parsed
# V1Locations/V2Locations points are filtered by distance from that point.
GEOCODE_URL = "https://nominatim.openstreetmap.org/search"
# Nominatim rejects browser-like agents with HTTP 429; it asks for a stable
# application identifier with a contact URL instead.
GEOCODE_USER_AGENT = "local-llm-chat/1.0 (+https://github.com/)"
GEOCODE_TIMEOUT = 20
GEO_RADIUS_KM = 15.0
GEO_BROADENED_RADIUS_KM = 50.0

RESULT_LIMIT = 20
MAX_FILES = 8          # scan at most two hours of 15-minute GKG batches
CACHE_FILE_LIMIT = 16  # keep roughly four hours of compressed GKG files
DOWNLOAD_TIMEOUT = 45
USER_AGENT = (
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
    "AppleWebKit/537.36 (KHTML, like Gecko) "
    "Chrome/131.0.0.0 Safari/537.36 local-chat-gkg/1.0"
)

PAGE_TITLE_RE = re.compile(r"<PAGE_TITLE>(.*?)</PAGE_TITLE>", re.I | re.S)
TAG_RE = re.compile(r"<[^>]+>")
SPACE_RE = re.compile(r"\s+")
NON_WORD_RE = re.compile(r"[^\w]+", re.UNICODE)
ZIP_SUFFIX = ".gkg.csv.zip"

# GDELT location names usually spell states out.  Expand common US/Canadian
# postal abbreviations so a query like ``restaurant:san francisco, ca`` still
# matches ``San Francisco, California, United States``.
REGION_ABBREVIATIONS = {
    "al": "alabama", "ak": "alaska", "az": "arizona", "ar": "arkansas",
    "ca": "california", "co": "colorado", "ct": "connecticut",
    "de": "delaware", "fl": "florida", "ga": "georgia", "hi": "hawaii",
    "id": "idaho", "il": "illinois", "in": "indiana", "ia": "iowa",
    "ks": "kansas", "ky": "kentucky", "la": "louisiana", "me": "maine",
    "md": "maryland", "ma": "massachusetts", "mi": "michigan",
    "mn": "minnesota", "ms": "mississippi", "mo": "missouri",
    "mt": "montana", "ne": "nebraska", "nv": "nevada",
    "nh": "new hampshire", "nj": "new jersey", "nm": "new mexico",
    "ny": "new york", "nc": "north carolina", "nd": "north dakota",
    "oh": "ohio", "ok": "oklahoma", "or": "oregon", "pa": "pennsylvania",
    "ri": "rhode island", "sc": "south carolina", "sd": "south dakota",
    "tn": "tennessee", "tx": "texas", "ut": "utah", "vt": "vermont",
    "va": "virginia", "wa": "washington", "wv": "west virginia",
    "wi": "wisconsin", "wy": "wyoming", "dc": "district of columbia",
    "ab": "alberta", "bc": "british columbia", "mb": "manitoba",
    "nb": "new brunswick", "nl": "newfoundland and labrador",
    "ns": "nova scotia", "nt": "northwest territories", "nu": "nunavut",
    "on": "ontario", "pe": "prince edward island", "qc": "quebec",
    "sk": "saskatchewan", "yt": "yukon",
}

# Tokens too generic to identify a place on their own.  They only count when
# part of a complete phrase (``new york``), never as the lone anchor for a
# relaxed neighbourhood-level match.
PLACE_STOPWORDS = {
    "city", "county", "district", "north", "south", "east", "west",
    "central", "greater", "metro", "metropolitan", "new", "old", "san",
    "santa", "saint", "st", "los", "las", "el", "la", "le", "the",
}


def emit(payload: dict) -> None:
    """Write one NDJSON event immediately so the browser can stream it."""
    sys.stdout.write(json.dumps(payload, ensure_ascii=False) + "\n")
    sys.stdout.flush()


def log(message: str) -> None:
    """Diagnostics go to stderr; stdout stays valid NDJSON."""
    sys.stderr.write(str(message).rstrip() + "\n")
    sys.stderr.flush()


def http_get(url: str, timeout: int = DOWNLOAD_TIMEOUT) -> bytes:
    request = urllib.request.Request(
        url, headers={"User-Agent": USER_AGENT, "Accept": "*/*"}
    )
    with urllib.request.urlopen(request, timeout=timeout) as response:
        return response.read()


def fetch_text(url: str, timeout: int = 30) -> str:
    return http_get(url, timeout=timeout).decode("utf-8", "replace")


def latest_gkg_stamp() -> str | None:
    """Return the newest GKG batch stamp named by GDELT's lastupdate file."""
    for url in LASTUPDATE_URLS:
        try:
            text = fetch_text(url)
        except (OSError, urllib.error.URLError) as exc:
            log(f"[gkg: could not read {url}: {exc}]")
            continue
        for line in text.splitlines():
            parts = line.split()
            if len(parts) < 3:
                continue
            candidate = parts[-1].strip()
            if not candidate.endswith(ZIP_SUFFIX):
                continue
            stamp = Path(urlparse(candidate).path).name[: -len(ZIP_SUFFIX)]
            if re.fullmatch(r"\d{14}", stamp):
                return stamp
    return None


def rounded_now_stamp() -> str:
    now = datetime.now(timezone.utc).replace(second=0, microsecond=0)
    now -= timedelta(minutes=now.minute % 15)
    return now.strftime("%Y%m%d%H%M%S")


def timestamp_candidates(base: str, count: int) -> list[str]:
    """Return ``base`` and the preceding 15-minute batch stamps, newest first."""
    try:
        current = datetime.strptime(base, "%Y%m%d%H%M%S").replace(
            tzinfo=timezone.utc
        )
    except ValueError:
        current = datetime.now(timezone.utc).replace(second=0, microsecond=0)
        current -= timedelta(minutes=current.minute % 15)
    stamps: list[str] = []
    for _ in range(max(1, count)):
        stamps.append(current.strftime("%Y%m%d%H%M%S"))
        current -= timedelta(minutes=15)
    return stamps


def cache_path(stamp: str) -> Path:
    return CACHE_DIR / f"{stamp}.gkg.csv.zip"


def valid_zip(path: Path) -> bool:
    if not path.is_file() or path.stat().st_size == 0:
        return False
    try:
        with zipfile.ZipFile(path) as archive:
            return bool(archive.namelist())
    except (OSError, zipfile.BadZipFile):
        return False


def prune_cache() -> None:
    try:
        files = sorted(
            CACHE_DIR.glob("*.gkg.csv.zip"),
            key=lambda path: path.stat().st_mtime,
            reverse=True,
        )
    except OSError:
        return
    for stale in files[CACHE_FILE_LIMIT:]:
        try:
            stale.unlink()
        except OSError:
            pass


def download_gkg(stamp: str) -> Path | None:
    """Return a cached/downloaded GKG zip, or None when the batch is absent."""
    target = cache_path(stamp)
    if valid_zip(target):
        return target

    # Remove a previous zero-byte/partial download before retrying.
    if target.exists() and not valid_zip(target):
        try:
            target.unlink()
        except OSError:
            pass

    CACHE_DIR.mkdir(parents=True, exist_ok=True)
    url = GKG_URL.format(stamp=stamp)
    temporary = target.with_suffix(target.suffix + ".part")
    try:
        data = http_get(url)
        if not data:
            return None
        temporary.write_bytes(data)
        if not valid_zip(temporary):
            temporary.unlink(missing_ok=True)
            return None
        temporary.replace(target)
        return target
    except urllib.error.HTTPError as exc:
        if exc.code in {403, 404}:
            return None
        log(f"[gkg: {url} returned HTTP {exc.code}]")
        return None
    except (OSError, urllib.error.URLError) as exc:
        log(f"[gkg: could not download {url}: {exc}]")
        try:
            temporary.unlink(missing_ok=True)
        except OSError:
            pass
        return None


def clean_text(value: str) -> str:
    value = html.unescape(value or "")
    value = TAG_RE.sub(" ", value)
    return SPACE_RE.sub(" ", value).strip()


def normalise(value: str) -> str:
    """Normalise metadata for case/delimiter-insensitive keyword matching."""
    value = html.unescape(value or "")
    value = unquote(value)
    value = value.replace("_", " ").replace("-", " ")
    value = NON_WORD_RE.sub(" ", value)
    return SPACE_RE.sub(" ", value).casefold().strip()


def page_title(extra_xml: str) -> str:
    match = PAGE_TITLE_RE.search(extra_xml or "")
    if match is None:
        return ""
    return clean_text(match.group(1))


def title_from_url(url: str) -> str:
    parsed = urlparse(url)
    slug = unquote(parsed.path.rstrip("/").rsplit("/", 1)[-1])
    slug = re.sub(r"\.(?:html?|php|aspx?|jsp)$", "", slug, flags=re.I)
    slug = re.sub(r"[-_+]+", " ", slug).strip()
    if not slug:
        return parsed.netloc or "Untitled story"
    return slug[:1].upper() + slug[1:]


def published_iso(stamp: str) -> str:
    try:
        moment = datetime.strptime(stamp, "%Y%m%d%H%M%S").replace(
            tzinfo=timezone.utc
        )
        return moment.isoformat().replace("+00:00", "Z")
    except ValueError:
        return ""


def first_values(block: str, limit: int, *, field_index: int = 0) -> list[str]:
    values: list[str] = []
    for part in (block or "").split(";"):
        part = part.strip()
        if not part:
            continue
        pieces = part.split("#")
        if field_index >= len(pieces):
            continue
        value = clean_text(pieces[field_index])
        if value and value not in values:
            values.append(value)
        if len(values) >= limit:
            break
    return values


def metadata_summary(fields: list[str]) -> str:
    """Build a short human-readable digest from GKG's themes and entities."""
    bits: list[str] = []

    themes = [
        clean_text(theme).replace("_", " ").lower()
        for theme in (fields[7] if len(fields) > 7 else "").split(";")
        if theme.strip()
    ][:6]
    if themes:
        bits.append("Themes: " + ", ".join(themes))

    people = first_values(fields[11] if len(fields) > 11 else "", 4)
    if people:
        bits.append("People: " + ", ".join(people))

    organizations = first_values(fields[13] if len(fields) > 13 else "", 4)
    if organizations:
        bits.append("Organizations: " + ", ".join(organizations))

    places = first_values(
        fields[9] if len(fields) > 9 else "", 4, field_index=1
    )
    if places:
        bits.append("Places: " + ", ".join(places))

    if len(fields) > 15:
        tone_text = fields[15].split(",", 1)[0].strip()
        try:
            bits.append(f"Tone: {float(tone_text):+.1f}")
        except ValueError:
            pass

    summary = " · ".join(bits)
    if len(summary) > 700:
        summary = summary[:697].rstrip() + "..."
    return summary


def _token_matches(text: str, tokens: set[str], token: str) -> bool:
    """Return True when ``token`` (or its postal expansion) is present."""
    expansion = REGION_ABBREVIATIONS.get(token)
    choices = [token]
    if expansion:
        choices.append(expansion)
    return any(
        (choice in text if " " in choice else choice in tokens)
        for choice in choices
    )


def _component_quality(
    component: str, text: str, text_tokens: set[str], *, relaxed: bool
) -> int | None:
    """Score how well one comma-separated location component matches.

    Quality 3 is an exact phrase (``san francisco``), 2 means every token
    matched (possibly via a postal abbreviation), and -- only in relaxed mode
    -- 1 means a distinctive part of a finer-grained name matched.  The last
    tier is what lets ``san francisco bayview, ca`` still return San Francisco
    stories when GDELT has no record carrying the neighbourhood name itself.
    """
    normalised = normalise(component)
    if not normalised:
        return None
    tokens = normalised.split()
    if normalised in text:
        return 3
    if all(_token_matches(text, text_tokens, token) for token in tokens):
        return 2
    if not relaxed:
        return None

    # A distinctive token can anchor a multi-word place name even when the
    # finer-grained qualifier (a neighbourhood or suburb) is absent.
    if any(
        len(token) >= 5
        and token not in PLACE_STOPWORDS
        and _token_matches(text, text_tokens, token)
        for token in tokens
    ):
        return 1
    if len(tokens) >= 2 and sum(
        _token_matches(text, text_tokens, token) for token in tokens
    ) >= 2:
        return 1
    return None


def location_score(
    location_metadata: str, location: str, *, relaxed: bool = False
) -> int | None:
    """Return a score when a GKG record mentions ``location``, else None.

    Comma-separated components are all required.  Each component may match as
    a complete phrase, or all of its tokens may occur across the combined
    location names (GDELT stores multiple places in one field).  Postal
    abbreviations are expanded, so ``ca`` matches ``California``.

    With ``relaxed=True`` a component may match only its distinctive part, so
    ``san francisco bayview, ca`` accepts a record tagged
    ``San Francisco, California, United States`` when no record mentions
    Bayview during the scanned window.  Relaxed matches always score below
    exact ones, and at least one non-region component must still match.
    """
    phrase = normalise(location)
    if not phrase:
        return 0
    text = normalise(location_metadata)
    if not text:
        return None
    text_tokens = set(text.split())

    components = [part.strip() for part in location.split(",") if part.strip()]
    if not components:
        components = [location]

    qualities: list[int] = []
    for component in components:
        quality = _component_quality(
            component, text, text_tokens, relaxed=relaxed
        )
        if quality is None:
            return None
        qualities.append(quality)

    if relaxed:
        # Do not turn a relaxed query into a statewide match: at least one
        # component must be a real place name rather than only a region code.
        place_components = [
            component
            for component in components
            if not (
                len(normalise(component).split()) == 1
                and normalise(component) in REGION_ABBREVIATIONS
            )
        ]
        if not place_components:
            return None
        if not any(
            quality == 1
            for component, quality in zip(components, qualities)
            if component in place_components
        ) and not all(quality >= 2 for quality in qualities):
            return None

    # Prefer an exact phrase match over a scattered token match.
    if phrase in text:
        return 60
    if all(quality >= 2 for quality in qualities):
        return 35
    # Relaxed, partial-name match: still useful, noticeably below exact hits.
    return 18


def _haversine_km(
    latitude: float, longitude: float, other_latitude: float, other_longitude: float
) -> float:
    """Return the great-circle distance between two points in kilometres."""
    radius = 6371.0088
    lat1 = math.radians(latitude)
    lat2 = math.radians(other_latitude)
    delta_lat = math.radians(other_latitude - latitude)
    delta_lon = math.radians(other_longitude - longitude)
    haversine = (
        math.sin(delta_lat / 2) ** 2
        + math.cos(lat1) * math.cos(lat2) * math.sin(delta_lon / 2) ** 2
    )
    return radius * 2 * math.atan2(math.sqrt(haversine), math.sqrt(1 - haversine))


def geocode_location(location: str) -> dict | None:
    """Resolve ``location`` to coordinates with OpenStreetMap Nominatim.

    Nominatim requires an identifying User-Agent and asks clients to stay
    below roughly one request per second.  ``gkg_fetch.py`` makes at most one
    lookup per command, so no persistent cache is needed.  A failed lookup is
    reported to stderr and returns ``None`` so the caller can fall back to the
    GKG's own place-name metadata.
    """
    location = " ".join(str(location or "").split())
    if not location:
        return None
    params = urlencode(
        {
            "q": location,
            "format": "jsonv2",
            "limit": 5,
            "addressdetails": 1,
        }
    )
    request = urllib.request.Request(
        f"{GEOCODE_URL}?{params}",
        headers={
            "User-Agent": GEOCODE_USER_AGENT,
            "Accept": "application/json",
            "Accept-Language": "en",
        },
    )
    try:
        with urllib.request.urlopen(request, timeout=GEOCODE_TIMEOUT) as response:
            data = json.load(response)
    except (OSError, ValueError, urllib.error.URLError) as exc:
        log(f"[gkg: geocoding {location!r} failed: {exc}]")
        return None

    if not isinstance(data, list) or not data:
        return None

    # Prefer a settlement/administrative boundary over an arbitrary road or
    # building when Nominatim returns several equally valid matches.
    preferred_types = {
        "city", "town", "village", "municipality", "borough", "suburb",
        "neighbourhood", "quarter", "county", "state", "country",
        "administrative",
    }
    best: tuple[float, dict] | None = None
    for match in data:
        if not isinstance(match, dict):
            continue
        try:
            latitude = float(match.get("lat"))
            longitude = float(match.get("lon"))
        except (TypeError, ValueError):
            continue
        if not (-90 <= latitude <= 90 and -180 <= longitude <= 180):
            continue
        try:
            rank = float(match.get("place_rank") or 0)
        except (TypeError, ValueError):
            rank = 0.0
        address_type = str(match.get("addresstype") or match.get("type") or "")
        score = rank + (20.0 if address_type in preferred_types else 0.0)
        if best is None or score > best[0]:
            best = (score, match)
    if best is None:
        return None

    match = best[1]
    return {
        "query": location,
        "latitude": float(match["lat"]),
        "longitude": float(match["lon"]),
        "display_name": clean_text(match.get("display_name")) or location,
    }


def parse_location_points(location_blocks: list[str]) -> list[tuple[float, float, str]]:
    """Extract ``(latitude, longitude, name)`` triples from GKG location fields.

    V2Locations stores nine ``#``-delimited values per place (latitude at 5,
    longitude at 6); the legacy V1Locations field stores seven (latitude at 4,
    longitude at 5).  V2 is parsed first and V1 fills in any gaps for older or
    partially populated records.
    """
    points: list[tuple[float, float, str]] = []
    seen: set[tuple[float, float, str]] = set()
    layouts = ((1, 5, 6), (0, 4, 5))
    for block_index, latitude_index, longitude_index in layouts:
        if block_index >= len(location_blocks):
            continue
        block = location_blocks[block_index]
        for part in (block or "").split(";"):
            pieces = part.split("#")
            if len(pieces) <= max(latitude_index, longitude_index):
                continue
            try:
                latitude = float(pieces[latitude_index])
                longitude = float(pieces[longitude_index])
            except (TypeError, ValueError):
                continue
            if not (-90 <= latitude <= 90 and -180 <= longitude <= 180):
                continue
            if latitude == 0 and longitude == 0:
                continue
            name = clean_text(pieces[1]) if len(pieces) > 1 else ""
            key = (round(latitude, 4), round(longitude, 4), name.casefold())
            if key in seen:
                continue
            seen.add(key)
            points.append((latitude, longitude, name))
    return points


def geo_location_score(
    location_blocks: list[str],
    latitude: float,
    longitude: float,
    radius_km: float,
) -> int | None:
    """Score a record by the closest GKG place inside ``radius_km``.

    Records without any parsed place inside the radius are rejected.  Closer
    places score higher, while still leaving enough range for keyword
    prominence to matter when several records are within the area.
    """
    closest: float | None = None
    for point_latitude, point_longitude, _ in parse_location_points(location_blocks):
        distance = _haversine_km(
            latitude, longitude, point_latitude, point_longitude
        )
        if distance <= radius_km and (closest is None or distance < closest):
            closest = distance
    if closest is None:
        return None
    proximity = max(0.0, 1.0 - closest / radius_km)
    return 40 + int(round(35 * proximity))


def record_score(
    title: str, url: str, metadata: str, phrase: str, tokens: list[str]
) -> int | None:
    """Score one GKG record against the keyword, or None when it is a miss.

    Single-word keywords must match a whole normalised token, so ``ai`` does
    not match ``said``.  Multi-word keywords match their complete phrase in
    the title, URL, or metadata; as a fallback all of their words may appear
    in the title or URL, but not scattered across long metadata lists.
    """
    title_text = normalise(title)
    url_text = normalise(unquote(url))
    metadata_text = normalise(metadata)
    title_tokens = set(title_text.split())
    url_tokens = set(url_text.split())
    metadata_tokens = set(metadata_text.split())

    def field_match(
        text: str, token_set: set[str], *, allow_token_fallback: bool
    ) -> bool:
        if len(tokens) == 1:
            return tokens[0] in token_set
        if phrase and phrase in text:
            return True
        return bool(
            allow_token_fallback
            and tokens
            and all(token in token_set for token in tokens)
        )

    matched = False
    score = 0

    if field_match(title_text, title_tokens, allow_token_fallback=True):
        score += 140 if phrase and phrase in title_text else 60
        matched = True

    if field_match(url_text, url_tokens, allow_token_fallback=True):
        score += 40 if phrase and phrase in url_text else 18
        matched = True

    # Metadata is broad: require the complete phrase there, or an exact
    # single-word token.  This avoids the "all words somewhere in a long
    # list of themes/entities" false positives.
    if field_match(
        metadata_text, metadata_tokens, allow_token_fallback=False
    ):
        score += 30 if phrase and phrase in metadata_text else 12
        matched = True

    if not matched:
        return None

    # Prefer records where the keyword is prominent in the title/URL over a
    # match buried in a long list of themes or extracted entity names.
    if phrase and phrase in title_text:
        score += 20
    if phrase and phrase in metadata_text:
        score += 5
    return score


def iter_gkg_rows(path: Path):
    """Yield the tab-delimited GKG rows from one cached zip file."""
    with zipfile.ZipFile(path) as archive:
        for name in archive.namelist():
            if not name.endswith(".gkg.csv"):
                continue
            with archive.open(name) as raw:
                for encoded in io.TextIOWrapper(
                    raw, encoding="utf-8", errors="replace", newline=""
                ):
                    line = encoded.rstrip("\r\n")
                    if line:
                        yield line


def parse_record(line: str) -> dict | None:
    fields = line.split("\t")
    if len(fields) < 27:
        return None
    if len(fields) > 27:
        # PAGE_TITLE/PAGE_LINKS XML can contain literal tabs.
        fields[26] = "\t".join(fields[26:])
        del fields[27:]

    record_id = fields[0].strip()
    published = fields[1].strip()
    collection = fields[2].strip()
    source = clean_text(fields[3])
    url = fields[4].strip()
    if collection != "1" or not re.match(r"^https?://", url, re.I):
        return None

    title = page_title(fields[26]) or title_from_url(url)
    summary = metadata_summary(fields)

    images: list[str] = []
    sharing = fields[18].strip() if len(fields) > 18 else ""
    if re.match(r"^https?://", sharing, re.I):
        images.append(sharing)
    related = fields[19].strip() if len(fields) > 19 else ""
    for image in related.split(";"):
        image = image.strip()
        if re.match(r"^https?://", image, re.I) and image not in images:
            images.append(image)
        if len(images) >= 3:
            break

    return {
        "record_id": record_id,
        "published_stamp": published,
        "published": published_iso(published),
        "source": source or urlparse(url).netloc,
        "url": url.split("#", 1)[0].strip(),
        "title": clean_text(title) or title_from_url(url),
        "summary": summary,
        "images": images,
        # Everything in these GKG fields is searchable metadata. The browser
        # receives the compact summary, not the full raw metadata.
        "metadata": " ".join(
            [
                fields[3],
                fields[4],
                fields[7],
                fields[8],
                fields[9],
                fields[10],
                fields[11],
                fields[12],
                fields[13],
                fields[14],
                fields[22],
                fields[23],
                fields[24],
                fields[25],
                fields[26],
            ]
        ),
        # Location fields are kept separately so a location-only search works
        # even when no keyword is supplied.  The raw blocks retain the
        # coordinates used by the geocoded distance filter.
        "locations": " ".join(
            [fields[9], fields[10] if len(fields) > 10 else ""]
        ),
        "location_blocks": [
            fields[9],
            fields[10] if len(fields) > 10 else "",
        ],
    }


def search_gkg(
    keyword: str,
    location: str,
    limit: int,
    *,
    relaxed: bool = False,
    geo: dict | None = None,
    radius_km: float = GEO_RADIUS_KM,
) -> list[dict]:
    phrase = normalise(keyword)
    tokens = [token for token in phrase.split() if token]
    if not phrase and not normalise(location):
        return []

    geo_latitude = None
    geo_longitude = None
    if geo is not None and location:
        geo_latitude = float(geo["latitude"])
        geo_longitude = float(geo["longitude"])

    base = latest_gkg_stamp() or rounded_now_stamp()
    stamps = timestamp_candidates(base, MAX_FILES + 4)
    seen_urls: set[str] = set()
    found: list[dict] = []
    scanned = 0

    for stamp in stamps:
        if len(found) >= limit or scanned >= MAX_FILES:
            break
        path = download_gkg(stamp)
        if path is None:
            continue
        scanned += 1

        batch: list[tuple[int, dict]] = []
        try:
            for line in iter_gkg_rows(path):
                record = parse_record(line)
                if record is None:
                    continue
                url = record["url"]
                if not url or url in seen_urls:
                    continue
                score = 0
                if phrase:
                    keyword_score = record_score(
                        record["title"],
                        url,
                        record["metadata"],
                        phrase,
                        tokens,
                    )
                    if keyword_score is None:
                        continue
                    score += keyword_score
                if location:
                    if geo_latitude is not None and geo_longitude is not None:
                        place_score = geo_location_score(
                            record.get("location_blocks") or [],
                            geo_latitude,
                            geo_longitude,
                            radius_km,
                        )
                    else:
                        place_score = location_score(
                            record.get("locations", ""),
                            location,
                            relaxed=relaxed,
                        )
                    if place_score is None:
                        continue
                    score += place_score
                seen_urls.add(url)
                batch.append((score, record))
        except (OSError, zipfile.BadZipFile) as exc:
            log(f"[gkg: could not read {path.name}: {exc}]")
            continue

        batch.sort(
            key=lambda pair: (
                -pair[0],
                pair[1].get("published_stamp", ""),
            ),
            reverse=False,
        )
        for score, record in batch:
            if len(found) >= limit:
                break
            record["score"] = score
            found.append(record)

    prune_cache()
    return found


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="Search the latest GDELT 2.0 GKG news records"
    )
    parser.add_argument(
        "keyword",
        nargs="*",
        help="keywords to search for; use <keyword>:<location> to filter by place",
    )
    parser.add_argument(
        "--location",
        default="",
        help="restrict results to this location (alternative to keyword:location)",
    )
    parser.add_argument("--limit", type=int, default=RESULT_LIMIT)
    parser.add_argument(
        "--radius",
        type=float,
        default=GEO_RADIUS_KM,
        help=(
            "search radius in kilometres around the geocoded location "
            f"(default {GEO_RADIUS_KM:g})"
        ),
    )
    # The chat server always passes these for the shared /news pipeline.
    parser.add_argument("--json", action="store_true", help=argparse.SUPPRESS)
    parser.add_argument("--lazy", action="store_true", help=argparse.SUPPRESS)
    args = parser.parse_args(argv)

    raw_keyword = " ".join(part for part in args.keyword if part).strip()
    location = " ".join((args.location or "").split()).strip()
    keyword = raw_keyword
    # A single colon separates the optional location, e.g.
    # ``restaurant:san francisco, ca``.  Ignore URL-like ``://`` occurrences.
    if ":" in raw_keyword and "://" not in raw_keyword:
        left, right = raw_keyword.split(":", 1)
        keyword = left.strip()
        if not location:
            location = right.strip()
    if not keyword and not location:
        emit(
            {
                "type": "error",
                "message": (
                    "Usage: /gkg <keyword>[:<location>] "
                    "(for example /gkg restaurant:san francisco, ca)"
                ),
            }
        )
        return 2

    limit = max(1, min(250, int(args.limit or RESULT_LIMIT)))
    radius_km = max(0.5, min(500.0, float(args.radius or GEO_RADIUS_KM)))
    geo = geocode_location(location) if location else None
    if geo is not None:
        log(
            f"[gkg: geocoded {location!r} to "
            f"{geo['latitude']:.5f}, {geo['longitude']:.5f} "
            f"({geo['display_name']})]"
        )
    elif location:
        log(
            f"[gkg: could not geocode {location!r}; "
            "falling back to GKG place-name matching]"
        )

    subject = keyword or "news"
    if location:
        subject = f"{subject} in {location}"
    description = (
        f"Newest {limit} GDELT 2.0 GKG news records matching "
        f"{subject!r}."
    )
    if geo is not None:
        description = (
            f"Newest {limit} GDELT 2.0 GKG news records matching "
            f"{subject!r} within {radius_km:g} km of "
            f"{geo['display_name']}."
        )
    emit(
        {
            "type": "start",
            "source": "GDELT Global Knowledge Graph",
            "description": description,
            "feed": "https://www.gdeltproject.org/",
        }
    )

    started = time.monotonic()
    try:
        records = search_gkg(
            keyword,
            location,
            limit,
            geo=geo,
            radius_km=radius_km,
        )
        broadened_message = ""
        if not records and location and geo is not None:
            # The exact place turned up no GKG rows in the scanned batches.
            # Retry a wider radius around the same geocoded point before
            # falling back to place-name text matching.
            wider_radius = max(GEO_BROADENED_RADIUS_KM, radius_km * 3.0)
            records = search_gkg(
                keyword,
                location,
                limit,
                geo=geo,
                radius_km=wider_radius,
            )
            if records:
                broadened_message = (
                    f"No GDELT GKG records had a place within "
                    f"{radius_km:g} km of {geo['display_name']!r} in the "
                    f"scanned batches; showing matches within "
                    f"{wider_radius:g} km instead."
                )
        if not records and location:
            # No story carries the exact place hierarchy in the current
            # batches.  Retry with a relaxed neighbourhood-level match (for
            # example, San Francisco stories for "san francisco bayview, ca").
            records = search_gkg(keyword, location, limit, relaxed=True)
            if records and not broadened_message:
                broadened_message = (
                    f"No GDELT GKG records mentioned {location!r} exactly in "
                    "the scanned batches; showing matches for the broader "
                    "place name instead."
                )
    except Exception as exc:  # Keep the browser informed instead of hanging.
        log(f"[gkg: search failed: {exc}]")
        emit({"type": "error", "message": f"GDELT GKG search failed: {exc}"})
        return 1

    if broadened_message:
        emit({"type": "warning", "message": broadened_message})

    for index, record in enumerate(records, start=1):
        item = {
            "type": "item",
            "index": index,
            "title": record["title"],
            "link": record["url"],
            "published": record["published"],
            "source": record["source"],
            "summary": record["summary"],
            "images": record.get("images") or [],
            "complete": True,
            "gkg_record_id": record["record_id"],
        }
        emit(item)
        emit({"type": "item_end", "index": index})

    if not records:
        emit(
            {
                "type": "warning",
                "message": (
                    f"No GDELT GKG records matched {subject!r} in the most "
                    "recent news batches."
                ),
            }
        )

    elapsed = time.monotonic() - started
    emit({"type": "done", "count": len(records), "elapsed": round(elapsed, 3)})
    return 0 if records else 0


if __name__ == "__main__":
    raise SystemExit(main())
