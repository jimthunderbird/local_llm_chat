"""OpenStreetMap geocoding, nearby places, restaurants, and driving routes."""

from __future__ import annotations

from .core import *
from .core import SCRIPT_DIR

# /map geocodes a location with OpenStreetMap's Nominatim service and lists
# nearby English Wikipedia articles from the MediaWiki GeoSearch API. The
# browser renders the results with the same /news-style list and modal used by
# /wikivoyage, so each place can be read or translated in place.
#
# Nominatim requires a User-Agent that identifies the application. A real
# contact URL is used because example.com addresses are rejected with HTTP 403.
MAP_USER_AGENT = "local-llm-chat/1.0 (+https://github.com/)"

NOMINATIM_SEARCH_URL = "https://nominatim.openstreetmap.org/search"

# Photon (Komoot) is a Nominatim-backed geocoder with fuzzy prefix matching.
# It recognizes place names such as "Land End Trail" even when Nominatim's
# exact address search finds nothing, so it is used for the /game place
# suggestions layered above the existing address suggestions.
PHOTON_SEARCH_URL = "https://photon.komoot.io/api/"

# Address autocomplete for /map's "To location" tab. Nominatim asks clients to
# cache results and to stay below roughly one request per second, so recent
# prefixes are served from this small in-process cache.
MAP_GEOCODE_CACHE_TTL_SECONDS = 600

MAP_GEOCODE_CACHE_MAX_ITEMS = 256

MAP_GEOCODE_LIMIT = 6

MAP_GEOCODE_CACHE: dict[tuple[str, int], tuple[float, list[dict]]] = {}

MAP_GEOCODE_CACHE_LOCK = threading.Lock()

WIKIPEDIA_API_URL = "https://en.wikipedia.org/w/api.php"

WIKIPEDIA_BASE_URL = "https://en.wikipedia.org"

MAP_SEARCH_RADIUS_METERS = 10_000

MAP_RESULT_LIMIT = 15

MAP_RETRY_MAX_SECONDS = 20.0

# /map's second tab lists restaurants from the Overpass API. A 1.5 km radius
# keeps the list walkable, and the fallback endpoint keeps the tab usable when
# the main Overpass instance is rate-limited or down.
OVERPASS_API_URLS = (
    "https://overpass.openstreetmap.fr/api/interpreter",
    "https://overpass-api.de/api/interpreter",
    "https://overpass.kumi.systems/api/interpreter",
)

MAP_RESTAURANT_RADIUS_METERS = 1_500

MAP_RESTAURANT_LIMIT = 30

# Restaurant modals fetch their primary details from the exact OSM object via
# Overpass, so cuisine, address, and opening hours work without any API key.
# Yelp Fusion v3 is only an optional enrichment when a key is configured.
OSM_RESTAURANT_CACHE_TTL_SECONDS = 3_600

OSM_RESTAURANT_CACHE_MAX_ITEMS = 512

OSM_RESTAURANT_TYPES = {"node", "way", "relation"}

OSM_RESTAURANT_CACHE: dict[tuple[str, str], tuple[float, dict]] = {}

OSM_RESTAURANT_CACHE_LOCK = threading.Lock()

# Driving instructions use the public OSRM demo server that the map examples
# reference. Routes are cached briefly so reopening the same destination does
# not hit the shared demo server again.
OSRM_ROUTE_URL = "https://router.project-osrm.org/route/v1/driving/{coordinates}"

DRIVING_CACHE_TTL_SECONDS = 3_600

DRIVING_CACHE_MAX_ITEMS = 256

DRIVING_CACHE: dict[tuple[float, float, float, float], tuple[float, dict]] = {}

DRIVING_CACHE_LOCK = threading.Lock()

YELP_SEARCH_URL = "https://api.yelp.com/v3/businesses/search"

YELP_BUSINESS_URL = "https://api.yelp.com/v3/businesses/{business_id}"

YELP_DAY_NAMES = ("Mon", "Tue", "Wed", "Thu", "Fri", "Sat", "Sun")

# /game is a small walking-explorer game built on the same OpenStreetMap data
# as /map. Each prompt under ./prompts/games supplies one game; the catalogue
# below holds the fixed starting point and the browser asks /api/game_nearby
# for the places around whichever marker the player clicks.
GAMES_DIR = SCRIPT_DIR / "prompts" / "games"

GAME_DATA_DIR = SCRIPT_DIR / ".chat" / "games"

GAME_DATA_LOCK = threading.Lock()

GAME_START_LOCATIONS = {
    "sapporo_explorer": {
        "title": "Sapporo Explorer",
        "name": "Sapporo JR Tower",
        "latitude": 43.0681986,
        "longitude": 141.3525873,
        "link": (
            "https://www.openstreetmap.org/?mlat=43.0681986"
            "&mlon=141.3525873#map=18/43.0681986/141.3525873"
        ),
    },
    "san_francisco_explorer": {
        "title": "San Francisco Explorer",
        "name": "San Francisco Ferry Building",
        "latitude": 37.7955487,
        "longitude": -122.3934746,
        "link": "https://www.openstreetmap.org/way/558731934",
    },
}

GAME_NEARBY_RADIUS_METERS = 1_200

GAME_NEARBY_LIMIT = 20

# Each explorer location also marks its nearby restaurants. They use the same
# walking radius as the other places but a separate 20-item cap so the two
# marker groups stay balanced on the map.
GAME_NEARBY_RESTAURANT_LIMIT = 20

# Overpass rate-limits concurrent queries, so the same coordinate can briefly
# return no places, no restaurants, or both. Keep the last successful payload
# for a few minutes; reopening a marker then paints immediately and a transient
# outage cannot make both marker groups disappear from the explorer.
GAME_NEARBY_CACHE_TTL_SECONDS = 300

GAME_NEARBY_CACHE_MAX_ITEMS = 256

GAME_NEARBY_CACHE: dict[
    tuple[float, float, int, int, int], tuple[float, dict]
] = {}

GAME_NEARBY_CACHE_LOCK = threading.Lock()

# Right-clicking a game marker opens a /map-style place modal. OSM tags and
# the linked Wikipedia extract rarely change, so the detail payload is cached
# per OSM id for an hour.
GAME_PLACE_CACHE_TTL_SECONDS = 3_600

GAME_PLACE_CACHE_MAX_ITEMS = 512

GAME_PLACE_CACHE: dict[tuple[str, str], tuple[float, dict]] = {}

GAME_PLACE_CACHE_LOCK = threading.Lock()

# A place's linked website is extracted with trafilatura and cached separately
# from the OSM/Wikipedia payload. The modal asks for both pieces together, but
# keeping this cache independent lets a transient website failure be retried
# without discarding the Wikipedia details.
GAME_WEBSITE_CACHE_TTL_SECONDS = 3_600

GAME_WEBSITE_CACHE_MAX_ITEMS = 256

GAME_WEBSITE_MAX_BYTES = 5 * 1024 * 1024

GAME_WEBSITE_MAX_PARAGRAPHS = 12

GAME_WEBSITE_MAX_CHARS = 6_000

GAME_WEBSITE_CACHE: dict[str, tuple[float, dict]] = {}

GAME_WEBSITE_CACHE_LOCK = threading.Lock()

# The explorer place modal also lists recent local news. The place name comes
# from Nominatim reverse geocoding, and the stories come from Google News
# through news_fetch.php, exactly like /news search. Both lookups are cached per
# coordinate so reopening a marker does not repeat them.
GAME_NEWS_LIMIT = 5

GAME_NEWS_CACHE_TTL_SECONDS = 900

GAME_NEWS_CACHE_MAX_ITEMS = 256

GAME_NEWS_CACHE: dict[tuple[float, float, int], tuple[float, dict]] = {}

GAME_NEWS_CACHE_LOCK = threading.Lock()

NOMINATIM_REVERSE_URL = "https://nominatim.openstreetmap.org/reverse"

def _map_plain_text(value: object) -> str:
    """Turn a Wikipedia excerpt, extract, or description into plain text."""
    text = html.unescape(re.sub(r"<[^>]*>", "", str(value or "")))
    text = re.sub(r"[ \t]+", " ", text)
    text = re.sub(r"\n{3,}", "\n\n", text)
    return text.strip()

def _map_distance(meters: object) -> str:
    """Format a GeoSearch distance for the result card."""
    if not isinstance(meters, (int, float)):
        return ""
    if meters < 1000:
        return f"{meters:.0f} m"
    return f"{meters / 1000:.1f} km"

def _geocode_viewbox(value: object) -> tuple[float, float, float, float] | None:
    """Normalize a Nominatim viewbox into (west, south, east, north).

    Callers may pass either a string such as ``-124.4,32.5,-114.1,42.0`` or a
    sequence of four numbers. Nominatim accepts any two opposite corners, so
    the coordinates are reordered here rather than requiring one exact order.
    """
    if isinstance(value, str):
        parts = [part for part in re.split(r"[,\s]+", value.strip()) if part]
    elif isinstance(value, (list, tuple)):
        parts = list(value)
    else:
        return None
    if len(parts) != 4:
        return None
    try:
        first_lon, first_lat, second_lon, second_lat = (
            float(part) for part in parts
        )
    except (TypeError, ValueError):
        return None
    west = min(first_lon, second_lon)
    east = max(first_lon, second_lon)
    south = min(first_lat, second_lat)
    north = max(first_lat, second_lat)
    if not (
        -180 <= west < east <= 180
        and -90 <= south < north <= 90
    ):
        return None
    return (west, south, east, north)

def _geocode_countrycodes(value: object) -> str:
    """Normalize a comma-separated list of ISO country codes for Nominatim."""
    if not value:
        return ""
    if isinstance(value, str):
        parts = value.split(",")
    elif isinstance(value, (list, tuple)):
        parts = [str(part) for part in value]
    else:
        return ""
    codes = [
        part.strip().lower()
        for part in parts
        if re.fullmatch(r"[A-Za-z]{2}", part.strip())
    ]
    return ",".join(codes)

def geocode_locations(
    location: str,
    limit: int = MAP_GEOCODE_LIMIT,
    *,
    viewbox: object = None,
    bounded: bool = False,
    countrycodes: object = None,
) -> list[dict]:
    """Resolve a place name to a list of OpenStreetMap Nominatim matches.

    Nominatim is limited by its usage policy, so each normalized query/limit
    pair is cached for a short time. A query with no matches returns an empty
    list rather than an error so the autocomplete UI can simply show nothing.

    ``viewbox`` (west, south, east, north) and ``countrycodes`` narrow address
    suggestions. When ``bounded`` is true Nominatim only returns results from
    inside the viewbox, which lets the /game explorer restrict its address
    dropdown to the game's exploration area.
    """
    location = " ".join(str(location or "").split())
    if not location:
        raise RuntimeError("Missing location")
    limit = max(1, min(int(limit), MAP_GEOCODE_LIMIT))
    normalized_viewbox = _geocode_viewbox(viewbox)
    normalized_countries = _geocode_countrycodes(countrycodes)
    normalized_bounded = bool(bounded and normalized_viewbox)
    cache_key = (
        location.lower(),
        limit,
        normalized_viewbox,
        normalized_bounded,
        normalized_countries,
    )
    now = time.time()
    with MAP_GEOCODE_CACHE_LOCK:
        cached = MAP_GEOCODE_CACHE.get(cache_key)
        if cached and now - cached[0] <= MAP_GEOCODE_CACHE_TTL_SECONDS:
            return [dict(item) for item in cached[1]]

    params: dict[str, object] = {
        "q": location,
        "format": "jsonv2",
        "limit": limit,
        "addressdetails": 1,
    }
    if normalized_viewbox:
        west, south, east, north = normalized_viewbox
        params["viewbox"] = f"{west},{south},{east},{north}"
    if normalized_bounded:
        params["bounded"] = 1
    if normalized_countries:
        params["countrycodes"] = normalized_countries

    try:
        response = requests.get(
            NOMINATIM_SEARCH_URL,
            params=params,
            headers={
                **chat.HTTP_HEADERS,
                "User-Agent": MAP_USER_AGENT,
                "Accept-Language": "en",
            },
            timeout=(10, 30),
        )
        response.raise_for_status()
        data = response.json()
    except (requests.RequestException, ValueError) as exc:
        raise RuntimeError(f"OpenStreetMap geocoding failed: {exc}") from exc

    matches = data if isinstance(data, list) else []
    results: list[dict] = []
    for match in matches:
        if not isinstance(match, dict):
            continue
        try:
            latitude = float(match.get("lat"))
            longitude = float(match.get("lon"))
        except (TypeError, ValueError):
            continue
        display_name = _map_plain_text(match.get("display_name")) or location
        address = match.get("address")
        results.append(
            {
                "query": location,
                "display_name": display_name,
                "latitude": latitude,
                "longitude": longitude,
                "category": str(
                    match.get("category") or match.get("class") or ""
                ),
                "type": str(match.get("type") or ""),
                "osm_type": str(match.get("osm_type") or ""),
                "osm_id": match.get("osm_id"),
                "address": address if isinstance(address, dict) else {},
            }
        )

    with MAP_GEOCODE_CACHE_LOCK:
        if len(MAP_GEOCODE_CACHE) >= MAP_GEOCODE_CACHE_MAX_ITEMS:
            oldest = min(
                MAP_GEOCODE_CACHE,
                key=lambda key: MAP_GEOCODE_CACHE[key][0],
            )
            MAP_GEOCODE_CACHE.pop(oldest, None)
        MAP_GEOCODE_CACHE[cache_key] = (now, [dict(item) for item in results])
    return results

def geocode_location(location: str) -> dict:
    """Resolve a place name to coordinates with OpenStreetMap Nominatim."""
    location = " ".join(str(location or "").split())
    if not location:
        raise RuntimeError("Missing location")
    results = geocode_locations(location, 1)
    if not results:
        raise RuntimeError(f"No location matched {location!r}.")
    result = results[0]
    result["query"] = location
    return result

def _map_retry_delay(response: requests.Response, attempt: int) -> float:
    """Return a bounded delay for a MediaWiki rate-limit response."""
    value = response.headers.get("Retry-After")
    try:
        delay = float(value)
    except (TypeError, ValueError):
        delay = 1.5 * (attempt + 1)
    return min(MAP_RETRY_MAX_SECONDS, max(0.5, delay))

def _haversine_meters(
    latitude: float, longitude: float, other_latitude: float, other_longitude: float
) -> float:
    """Return the great-circle distance between two coordinates in meters."""
    earth_radius = 6_371_000.0
    lat1 = math.radians(latitude)
    lat2 = math.radians(other_latitude)
    delta_lat = math.radians(other_latitude - latitude)
    delta_lon = math.radians(other_longitude - longitude)
    value = (
        math.sin(delta_lat / 2) ** 2
        + math.cos(lat1) * math.cos(lat2) * math.sin(delta_lon / 2) ** 2
    )
    return earth_radius * 2 * math.asin(min(1.0, math.sqrt(value)))

def search_wikipedia_nearby(
    latitude: float,
    longitude: float,
    radius_meters: int = MAP_SEARCH_RADIUS_METERS,
    limit: int = MAP_RESULT_LIMIT,
) -> list[dict]:
    """List Wikipedia pages near one coordinate with the GeoSearch API.

    A single ``generator=geosearch`` query also returns each page's extract,
    thumbnail, short description, coordinates, and canonical URL. Keeping this
    to one MediaWiki request avoids the API's per-IP rate limit that one
    summary request per result would quickly hit.
    """
    params = {
        "action": "query",
        "generator": "geosearch",
        "ggscoord": f"{latitude}|{longitude}",
        "ggsradius": max(10, min(int(radius_meters), 10_000)),
        "ggslimit": max(1, min(int(limit), 50)),
        "prop": "coordinates|pageimages|extracts|description|info",
        "inprop": "url",
        "exintro": 1,
        "explaintext": 1,
        "exchars": 1500,
        "piprop": "thumbnail",
        "pithumbsize": 320,
        "format": "json",
        "formatversion": 2,
    }
    data = None
    last_error: Exception | None = None
    for attempt in range(3):
        try:
            response = requests.get(
                WIKIPEDIA_API_URL,
                params=params,
                headers=chat.HTTP_HEADERS,
                timeout=(10, 30),
            )
            if response.status_code in {429, 503} and attempt < 2:
                time.sleep(_map_retry_delay(response, attempt))
                continue
            response.raise_for_status()
            data = response.json()
            break
        except (requests.RequestException, ValueError) as exc:
            last_error = exc
            if attempt < 2:
                time.sleep(1.0 + attempt)
    if not isinstance(data, dict):
        raise RuntimeError(f"Wikipedia GeoSearch failed: {last_error}")

    query = data.get("query")
    pages = query.get("pages") if isinstance(query, dict) else None
    if isinstance(pages, dict):
        pages = list(pages.values())
    if not isinstance(pages, list):
        raise RuntimeError("Wikipedia GeoSearch returned an unexpected response.")

    results: list[dict] = []
    for page in pages:
        if not isinstance(page, dict):
            continue
        page_latitude = None
        page_longitude = None
        coordinates = page.get("coordinates")
        if isinstance(coordinates, list) and coordinates:
            first = coordinates[0]
            if isinstance(first, dict):
                try:
                    page_latitude = float(first.get("lat"))
                    page_longitude = float(first.get("lon"))
                except (TypeError, ValueError):
                    page_latitude = None
                    page_longitude = None
        distance_meters = None
        if page_latitude is not None and page_longitude is not None:
            distance_meters = _haversine_meters(
                latitude, longitude, page_latitude, page_longitude
            )

        thumbnail = page.get("thumbnail") or {}
        image_url = ""
        if isinstance(thumbnail, dict):
            image_url = str(thumbnail.get("source") or "").strip()
            if image_url.startswith("//"):
                image_url = "https:" + image_url

        link = str(page.get("fullurl") or "").strip()
        if not link:
            title = str(page.get("title") or "").strip()
            if title:
                link = WIKIPEDIA_BASE_URL + "/wiki/" + quote(
                    title.replace(" ", "_"), safe="/()_',.:!-~"
                )

        results.append(
            {
                "title": str(page.get("title") or ""),
                "description": str(page.get("description") or ""),
                "extract": str(page.get("extract") or ""),
                "link": link,
                "latitude": page_latitude,
                "longitude": page_longitude,
                "distance_m": distance_meters,
                "image": image_url,
            }
        )
    results.sort(
        key=lambda item: (
            item["distance_m"] is None,
            item["distance_m"] if item["distance_m"] is not None else 0.0,
        )
    )
    return results

def search_nearby_restaurants(
    latitude: float,
    longitude: float,
    radius_meters: int = MAP_RESTAURANT_RADIUS_METERS,
    limit: int = MAP_RESTAURANT_LIMIT,
) -> list[dict]:
    """List named restaurants near a coordinate with the Overpass API."""
    radius = max(100, min(int(radius_meters), 5_000))
    query = (
        "[out:json][timeout:25];\n"
        "(\n"
        "  nwr[\"amenity\"~\"^(restaurant|fast_food|cafe|food_court)$\"]"
        f"(around:{radius},{latitude},{longitude});\n"
        ");\n"
        "out center 80;"
    )
    data = None
    last_error: Exception | None = None
    for endpoint in OVERPASS_API_URLS:
        try:
            response = requests.get(
                endpoint,
                params={"data": query},
                headers={**chat.HTTP_HEADERS, "User-Agent": MAP_USER_AGENT},
                timeout=(8, 35),
            )
            response.raise_for_status()
            data = response.json()
            break
        except (requests.RequestException, ValueError) as exc:
            last_error = exc
    if not isinstance(data, dict):
        raise RuntimeError(
            f"OpenStreetMap Overpass search failed: {last_error}"
        )

    elements = data.get("elements")
    if not isinstance(elements, list):
        raise RuntimeError(
            "OpenStreetMap Overpass returned an unexpected response."
        )

    results: list[dict] = []
    seen: set[str] = set()
    yelp_available = bool(yelp_api_key())
    for element in elements:
        if not isinstance(element, dict):
            continue
        tags = element.get("tags")
        if not isinstance(tags, dict):
            tags = {}
        name = _map_plain_text(tags.get("name"))
        if not name:
            continue

        element_latitude = element.get("lat")
        element_longitude = element.get("lon")
        if not isinstance(element_latitude, (int, float)) or not isinstance(
            element_longitude, (int, float)
        ):
            center = element.get("center")
            if isinstance(center, dict):
                element_latitude = center.get("lat")
                element_longitude = center.get("lon")
        if not isinstance(element_latitude, (int, float)) or not isinstance(
            element_longitude, (int, float)
        ):
            continue

        osm_type = str(element.get("type") or "node")
        osm_id = element.get("id")
        if osm_id is None:
            continue
        link = f"https://www.openstreetmap.org/{osm_type}/{osm_id}"
        if link in seen:
            continue
        seen.add(link)

        distance = _haversine_meters(
            latitude, longitude, float(element_latitude), float(element_longitude)
        )
        amenity = str(tags.get("amenity") or "restaurant")
        kind = amenity.replace("_", " ").title()
        cuisine = _map_plain_text(tags.get("cuisine"))
        address = " ".join(
            value
            for value in (
                _map_plain_text(tags.get("addr:housenumber")),
                _map_plain_text(tags.get("addr:street")),
                _map_plain_text(tags.get("addr:city")),
                _map_plain_text(tags.get("addr:postcode")),
            )
            if value
        ).strip()
        website = str(
            tags.get("website") or tags.get("contact:website") or ""
        ).strip()
        phone = str(tags.get("phone") or tags.get("contact:phone") or "").strip()
        hours = _map_plain_text(tags.get("opening_hours"))
        distance_text = _map_distance(distance)

        section = " · ".join(
            value for value in (distance_text, kind, cuisine) if value
        )
        summary = " · ".join(
            value for value in (kind, cuisine, address) if value
        )
        paragraphs: list[str] = []
        if summary:
            paragraphs.append(summary)
        details: list[str] = []
        if distance_text:
            details.append(f"Distance: {distance_text}")
        if address:
            details.append(f"Address: {address}")
        if hours:
            details.append(f"Opening hours: {hours}")
        if phone:
            details.append(f"Phone: {phone}")
        if website:
            details.append(f"Website: {website}")
        if details:
            paragraphs.append(" · ".join(details))

        results.append(
            {
                "title": name,
                "link": link,
                "category": kind,
                "section": section,
                "summary": summary,
                "paragraphs": paragraphs,
                "coordinates": (
                    f"{float(element_latitude):.6g}, "
                    f"{float(element_longitude):.6g}"
                ),
                "distance_m": distance,
                "distance_text": distance_text,
                "source": "OpenStreetMap",
                "docs": True,
                "docs_label": "OpenStreetMap",
                "learnable": False,
                "osm": True,
                "osm_type": osm_type,
                "osm_id": osm_id,
                "cuisine": cuisine,
                "address": address,
                "hours": hours,
                "phone": phone,
                "website": website,
                "yelp": yelp_available,
                "latitude": float(element_latitude),
                "longitude": float(element_longitude),
            }
        )
    results.sort(key=lambda item: item["distance_m"])
    return results[: max(1, min(int(limit), 100))]

def _format_route_distance(meters: object) -> str:
    """Format an OSRM route distance for the driving-instructions modal."""
    try:
        value = float(meters)
    except (TypeError, ValueError):
        return ""
    if value < 1000:
        return f"{value:.0f} m"
    return f"{value / 1000:.1f} km"

def _format_route_duration(seconds: object) -> str:
    """Format an OSRM duration as minutes, or hours and minutes."""
    try:
        value = max(0.0, float(seconds))
    except (TypeError, ValueError):
        return ""
    minutes = max(1, int(round(value / 60.0)))
    if minutes < 60:
        return f"{minutes} min"
    hours, remainder = divmod(minutes, 60)
    if not remainder:
        return f"{hours} h"
    return f"{hours} h {remainder} min"

def _osrm_lonlat_pair(value: object) -> list[float] | None:
    """Return a validated [longitude, latitude] pair from an OSRM geometry."""
    if not isinstance(value, (list, tuple)) or len(value) < 2:
        return None
    try:
        longitude = float(value[0])
        latitude = float(value[1])
    except (TypeError, ValueError):
        return None
    if not (math.isfinite(longitude) and math.isfinite(latitude)):
        return None
    if not (-180 <= longitude <= 180 and -90 <= latitude <= 90):
        return None
    return [round(longitude, 6), round(latitude, 6)]

def _osrm_modifier_phrase(modifier: str) -> str:
    """Make OSRM's turn modifiers read naturally."""
    return {
        "slight left": "slightly left",
        "slight right": "slightly right",
        "sharp left": "sharply left",
        "sharp right": "sharply right",
        "uturn": "U-turn",
    }.get(modifier, modifier)

def _osrm_step_instruction(step: dict) -> str:
    """Turn one OSRM step into a readable driving instruction."""
    maneuver = step.get("maneuver")
    if not isinstance(maneuver, dict):
        maneuver = {}
    maneuver_type = str(maneuver.get("type") or "").strip().lower()
    raw_modifier = str(maneuver.get("modifier") or "").strip().lower()
    raw_modifier = raw_modifier.replace("_", " ")
    modifier = _osrm_modifier_phrase(raw_modifier)
    road = _map_plain_text(step.get("name")) or _map_plain_text(
        step.get("ref")
    )
    road_suffix = f" onto {road}" if road else ""

    if maneuver_type == "depart":
        return f"Depart on {road}" if road else "Depart"
    if maneuver_type == "arrive":
        return "Arrive at your destination"
    if raw_modifier == "uturn" and maneuver_type in {
        "turn",
        "continue",
        "new name",
        "end of road",
    }:
        return "Make a U-turn" + road_suffix
    if maneuver_type in {"roundabout", "rotary", "roundabout turn"}:
        exit_number = maneuver.get("exit")
        text = "Enter the roundabout"
        if exit_number not in (None, ""):
            text += f" and take exit {exit_number}"
        return text + road_suffix
    if maneuver_type in {"exit roundabout", "exit rotary"}:
        return "Exit the roundabout" + road_suffix
    if maneuver_type == "end of road":
        text = "At the end of the road"
        if modifier:
            text += f", turn {modifier}"
        return text + road_suffix
    if maneuver_type == "fork":
        fork_modifier = {
            "slightly left": "left",
            "slightly right": "right",
        }.get(modifier, modifier)
        text = f"Keep {fork_modifier}" if fork_modifier else "Keep straight"
        return text + road_suffix
    if maneuver_type == "merge":
        text = f"Merge {modifier}" if modifier else "Merge"
        return text + road_suffix
    if maneuver_type == "on ramp":
        text = "Take the ramp"
        if modifier:
            text += f" {modifier}"
        return text + road_suffix
    if maneuver_type == "off ramp":
        return "Take the exit" + road_suffix
    if maneuver_type == "continue":
        text = f"Continue {modifier}" if modifier else "Continue"
        return text + road_suffix
    if maneuver_type == "new name":
        return ("Continue" + road_suffix) if road else "Continue"
    if maneuver_type == "notification":
        message = _map_plain_text(step.get("name"))
        return message or "Continue"

    text = maneuver_type.replace("_", " ").strip() or "Continue"
    text = text[:1].upper() + text[1:]
    if modifier:
        text += f" {modifier}"
    return text + road_suffix

def fetch_driving_instructions(
    from_latitude: float,
    from_longitude: float,
    to_latitude: float,
    to_longitude: float,
) -> dict:
    """Return an OSRM driving route with turn-by-turn instructions.

    The OSRM demo server is shared and rate-limited, so recent routes are
    cached and callers make one request per destination click rather than
    polling it in the UI.
    """
    coordinates = (from_latitude, from_longitude, to_latitude, to_longitude)
    for value in coordinates:
        if not isinstance(value, (int, float)) or not math.isfinite(float(value)):
            raise ValueError("Invalid driving-route coordinates")
    if not (-90 <= from_latitude <= 90 and -180 <= from_longitude <= 180):
        raise ValueError("Invalid driving-route origin")
    if not (-90 <= to_latitude <= 90 and -180 <= to_longitude <= 180):
        raise ValueError("Invalid driving-route destination")

    cache_key = tuple(round(float(value), 5) for value in coordinates)
    now = time.time()
    with DRIVING_CACHE_LOCK:
        cached = DRIVING_CACHE.get(cache_key)
        if cached and now - cached[0] <= DRIVING_CACHE_TTL_SECONDS:
            return cached[1]

    route_coordinates = (
        f"{float(from_longitude):.6f},{float(from_latitude):.6f};"
        f"{float(to_longitude):.6f},{float(to_latitude):.6f}"
    )
    try:
        response = requests.get(
            OSRM_ROUTE_URL.format(coordinates=route_coordinates),
            params={
                "overview": "full",
                "geometries": "geojson",
                "steps": "true",
                "alternatives": "false",
            },
            headers={**chat.HTTP_HEADERS, "User-Agent": MAP_USER_AGENT},
            timeout=(8, 30),
        )
        response.raise_for_status()
        data = response.json()
    except (requests.RequestException, ValueError) as exc:
        raise RuntimeError(f"OSRM driving directions failed: {exc}") from exc

    if not isinstance(data, dict):
        raise RuntimeError("OSRM returned an unexpected response.")
    if data.get("code") != "Ok":
        message = _map_plain_text(data.get("message")) or data.get("code")
        raise RuntimeError(f"OSRM could not find a driving route: {message}")
    routes = data.get("routes")
    if not isinstance(routes, list) or not routes:
        raise RuntimeError("OSRM could not find a driving route.")

    route = routes[0]
    if not isinstance(route, dict):
        raise RuntimeError("OSRM returned an unexpected route.")

    steps: list[dict] = []
    legs = route.get("legs")
    if isinstance(legs, list):
        for leg in legs:
            if not isinstance(leg, dict):
                continue
            leg_steps = leg.get("steps")
            if not isinstance(leg_steps, list):
                continue
            for step in leg_steps:
                if not isinstance(step, dict):
                    continue
                distance = step.get("distance")
                duration = step.get("duration")
                maneuver = step.get("maneuver")
                if not isinstance(maneuver, dict):
                    maneuver = {}
                step_data = {
                    "instruction": _osrm_step_instruction(step),
                    "distance_m": distance,
                    "duration_s": duration,
                    "distance_text": _format_route_distance(distance),
                    "duration_text": _format_route_duration(duration),
                    "maneuver": {
                        "type": str(maneuver.get("type") or ""),
                        "modifier": str(maneuver.get("modifier") or ""),
                    },
                }
                location = _osrm_lonlat_pair(maneuver.get("location"))
                if location is not None:
                    step_data["location"] = {
                        "longitude": location[0],
                        "latitude": location[1],
                    }
                steps.append(step_data)

    geometry: list[list[float]] = []
    raw_geometry = route.get("geometry")
    if isinstance(raw_geometry, dict):
        raw_coordinates = raw_geometry.get("coordinates")
        if isinstance(raw_coordinates, list):
            for coordinate in raw_coordinates:
                pair = _osrm_lonlat_pair(coordinate)
                if pair is not None:
                    geometry.append(pair)

    result = {
        "from": {
            "latitude": float(from_latitude),
            "longitude": float(from_longitude),
        },
        "to": {
            "latitude": float(to_latitude),
            "longitude": float(to_longitude),
        },
        "distance_m": route.get("distance"),
        "duration_s": route.get("duration"),
        "distance_text": _format_route_distance(route.get("distance")),
        "duration_text": _format_route_duration(route.get("duration")),
        "geometry": geometry,
        "steps": steps,
    }

    with DRIVING_CACHE_LOCK:
        if len(DRIVING_CACHE) >= DRIVING_CACHE_MAX_ITEMS:
            oldest = min(DRIVING_CACHE, key=lambda key: DRIVING_CACHE[key][0])
            DRIVING_CACHE.pop(oldest, None)
        DRIVING_CACHE[cache_key] = (now, result)
    return result

def _osm_tag_text(tags: dict, *keys: str) -> str:
    """Return the first non-empty plain-text value for the given OSM keys."""
    for key in keys:
        value = _map_plain_text(tags.get(key))
        if value:
            return value
    return ""

def _osm_coordinates(element: dict) -> tuple[float | None, float | None]:
    """Read a node's coordinates, or a way/relation's Overpass center."""
    latitude = element.get("lat")
    longitude = element.get("lon")
    if not isinstance(latitude, (int, float)) or not isinstance(
        longitude, (int, float)
    ):
        center = element.get("center")
        if isinstance(center, dict):
            latitude = center.get("lat")
            longitude = center.get("lon")
    if isinstance(latitude, (int, float)) and isinstance(
        longitude, (int, float)
    ):
        return float(latitude), float(longitude)
    return None, None

def _osm_hours_summary(
    value: object, latitude: float | None, longitude: float | None
) -> dict:
    """Summarize an OSM opening_hours value, falling back to the raw text.

    Parsing uses opening_hours_py when it is installed. The public Overpass
    workflow must remain keyless and dependency-free, so a missing parser or
    an unusual expression is never fatal: the raw OSM expression is returned.
    """
    raw = _map_plain_text(value)
    result = {
        "raw": raw,
        "open_now": None,
        "next_change": None,
        "parsed": False,
    }
    if not raw:
        return result
    try:
        from opening_hours import OpeningHours  # type: ignore import-not-found

        coordinates = (
            (latitude, longitude)
            if latitude is not None and longitude is not None
            else None
        )
        hours = OpeningHours(raw, coords=coordinates)
        state = hours.state()
        if state in {"open", "closed", "unknown"}:
            result["open_now"] = state == "open" if state != "unknown" else None
        if result["open_now"] is not None:
            try:
                result["next_change"] = hours.next_change().isoformat()
            except Exception:
                result["next_change"] = None
            result["parsed"] = True
    except Exception:
        # No parser, unsupported syntax, or missing coordinates. Show raw.
        pass
    return result

def _normalize_osm_restaurant(
    element: dict, fallback_type: str = "", fallback_id: int | None = None
) -> dict:
    """Normalize one Overpass element into the restaurant-modal model."""
    tags = element.get("tags")
    if not isinstance(tags, dict):
        tags = {}
    osm_type = str(element.get("type") or fallback_type or "node")
    osm_id = element.get("id", fallback_id)
    latitude, longitude = _osm_coordinates(element)

    raw_cuisine = _osm_tag_text(tags, "cuisine")
    cuisine = [
        part.strip()
        for part in raw_cuisine.split(";")
        if part.strip()
    ]
    address = " ".join(
        value
        for value in (
            _osm_tag_text(tags, "addr:housenumber"),
            _osm_tag_text(tags, "addr:street", "addr:place"),
            _osm_tag_text(tags, "addr:city", "addr:town", "addr:village"),
            _osm_tag_text(tags, "addr:postcode"),
        )
        if value
    ).strip()
    amenity = _osm_tag_text(tags, "amenity") or "restaurant"
    opening_hours = _osm_hours_summary(
        tags.get("opening_hours"), latitude, longitude
    )
    kitchen_hours = _map_plain_text(tags.get("opening_hours:kitchen"))
    website = _osm_tag_text(tags, "website", "contact:website", "url")
    phone = _osm_tag_text(tags, "phone", "contact:phone")

    dietary = {
        key: _osm_tag_text(tags, f"diet:{key}")
        for key in ("vegetarian", "vegan", "halal", "kosher", "gluten_free")
    }
    dietary = {key: value for key, value in dietary.items() if value}
    service = {
        key: _osm_tag_text(tags, key)
        for key in (
            "takeaway",
            "delivery",
            "outdoor_seating",
            "wheelchair",
            "internet_access",
        )
    }
    service = {key: value for key, value in service.items() if value}

    coordinates = ""
    if latitude is not None and longitude is not None:
        coordinates = f"{latitude:.6g}, {longitude:.6g}"

    return {
        "found": True,
        "osm_type": osm_type,
        "osm_id": osm_id,
        "name": _osm_tag_text(tags, "name"),
        "type": amenity.replace("_", " ").title(),
        "amenity": amenity,
        "cuisine": cuisine,
        "address": address,
        "opening_hours": opening_hours,
        "kitchen_hours": kitchen_hours,
        "phone": phone,
        "website": website,
        "service": service,
        "dietary": dietary,
        "stars": _osm_tag_text(tags, "stars"),
        "brand": _osm_tag_text(tags, "brand"),
        "coordinates": coordinates,
        "latitude": latitude,
        "longitude": longitude,
        "url": f"https://www.openstreetmap.org/{osm_type}/{osm_id}",
        "attribution": "© OpenStreetMap contributors",
    }

def fetch_osm_restaurant(osm_type: str, osm_id: str | int) -> dict:
    """Fetch one restaurant's details from Overpass by its OSM identity.

    The /map list already knows the exact OSM object, so querying that object
    is cheaper and more accurate than a second name search. Results are cached
    per OSM id because restaurant names, addresses, and hours rarely change.
    """
    identifier = str(osm_id).strip()
    if not identifier.isdigit() or int(identifier) <= 0:
        raise ValueError("Invalid OSM id")
    osm_type = str(osm_type or "").strip().lower()
    if osm_type not in OSM_RESTAURANT_TYPES:
        raise ValueError("Invalid OSM type")

    cache_key = (osm_type, identifier)
    now = time.monotonic()
    with OSM_RESTAURANT_CACHE_LOCK:
        cached = OSM_RESTAURANT_CACHE.get(cache_key)
        if cached and now - cached[0] <= OSM_RESTAURANT_CACHE_TTL_SECONDS:
            return cached[1]

    query = (
        "[out:json][timeout:15];\n"
        f"{osm_type}({identifier});\n"
        "out center;"
    )
    data = None
    last_error: Exception | None = None
    for endpoint in OVERPASS_API_URLS:
        try:
            response = requests.get(
                endpoint,
                params={"data": query},
                headers={**chat.HTTP_HEADERS, "User-Agent": MAP_USER_AGENT},
                timeout=(8, 30),
            )
            response.raise_for_status()
            data = response.json()
            break
        except (requests.RequestException, ValueError) as exc:
            last_error = exc
    if not isinstance(data, dict):
        raise RuntimeError(
            f"OpenStreetMap Overpass detail lookup failed: {last_error}"
        )

    elements = data.get("elements")
    if not isinstance(elements, list):
        raise RuntimeError(
            "OpenStreetMap Overpass returned an unexpected response."
        )
    element = next(
        (
            item
            for item in elements
            if isinstance(item, dict) and isinstance(item.get("tags"), dict)
        ),
        None,
    )
    if element is None:
        result = {
            "found": False,
            "osm_type": osm_type,
            "osm_id": int(identifier),
        }
    else:
        result = _normalize_osm_restaurant(element, osm_type, int(identifier))

    with OSM_RESTAURANT_CACHE_LOCK:
        if len(OSM_RESTAURANT_CACHE) >= OSM_RESTAURANT_CACHE_MAX_ITEMS:
            OSM_RESTAURANT_CACHE.pop(next(iter(OSM_RESTAURANT_CACHE)), None)
        OSM_RESTAURANT_CACHE[cache_key] = (now, result)
    return result

def yelp_api_key() -> str:
    """Return the configured Yelp Fusion API key, or an empty string."""
    key = os.environ.get("YELP_API_KEY", "").strip()
    if key:
        return key
    try:
        return (Path.home() / ".yelp_api_key").read_text(
            encoding="utf-8"
        ).strip()
    except OSError:
        return ""

def _yelp_name_key(value: object) -> str:
    """Normalize a business name for exact-match selection."""
    return " ".join(re.findall(r"[a-z0-9]+", str(value or "").casefold()))

def _yelp_clock(value: object) -> str:
    """Turn Yelp's HHMM clock value into HH:MM."""
    text = str(value or "").strip()
    if len(text) == 4 and text.isdigit():
        return f"{text[:2]}:{text[2:]}"
    if len(text) == 3 and text.isdigit():
        return f"0{text[0]}:{text[1:]}"
    return text

def _yelp_hours(hours: object) -> tuple[list[str], bool | None]:
    """Flatten Yelp's regular opening hours into display lines."""
    lines: list[str] = []
    is_open_now = None
    if not isinstance(hours, list):
        return lines, is_open_now
    for block in hours:
        if not isinstance(block, dict):
            continue
        if block.get("is_open_now") is not None:
            is_open_now = bool(block.get("is_open_now"))
        hours_type = str(block.get("hours_type") or "").upper()
        if hours_type and hours_type != "REGULAR":
            continue
        entries = block.get("open")
        if not isinstance(entries, list):
            continue
        for entry in entries:
            if not isinstance(entry, dict):
                continue
            try:
                day = int(entry.get("day"))
            except (TypeError, ValueError):
                continue
            if not 0 <= day < 7:
                continue
            start = _yelp_clock(entry.get("start"))
            end = _yelp_clock(entry.get("end"))
            if start and end:
                lines.append(f"{YELP_DAY_NAMES[day]} {start}-{end}")
    return lines, is_open_now

def fetch_yelp_restaurant(
    latitude: float, longitude: float, name: str
) -> dict:
    """Look up a restaurant by coordinates and name with Yelp Fusion v3."""
    key = yelp_api_key()
    if not key:
        raise RuntimeError(
            "Yelp API key is not configured. Set YELP_API_KEY or add "
            "~/.yelp_api_key, then restart the server."
        )
    query = " ".join(str(name or "").split())
    if not query:
        raise ValueError("Missing restaurant name")

    headers = {
        "Authorization": f"Bearer {key}",
        "Accept": "application/json",
    }
    try:
        response = requests.get(
            YELP_SEARCH_URL,
            params={
                "latitude": latitude,
                "longitude": longitude,
                "term": query,
                "limit": 10,
                "sort_by": "distance",
            },
            headers=headers,
            timeout=(10, 30),
        )
    except requests.RequestException as exc:
        raise RuntimeError(f"Yelp search failed: {exc}") from exc
    if response.status_code in {401, 403}:
        raise RuntimeError(
            f"Yelp rejected the API key (HTTP {response.status_code}). "
            "Check YELP_API_KEY."
        )
    if response.status_code == 429:
        raise RuntimeError("Yelp rate limit reached. Try again later.")
    try:
        response.raise_for_status()
        data = response.json()
    except (requests.RequestException, ValueError) as exc:
        raise RuntimeError(f"Yelp search failed: {exc}") from exc

    businesses = data.get("businesses") if isinstance(data, dict) else None
    if not isinstance(businesses, list) or not businesses:
        return {"found": False, "query": query}
    businesses = [item for item in businesses if isinstance(item, dict)]
    if not businesses:
        return {"found": False, "query": query}

    target = _yelp_name_key(query)
    best = next(
        (
            item
            for item in businesses
            if _yelp_name_key(item.get("name")) == target
        ),
        businesses[0],
    )
    business_id = str(best.get("id") or "")
    details: dict = {}
    if business_id:
        try:
            detail_response = requests.get(
                YELP_BUSINESS_URL.format(
                    business_id=quote(business_id, safe="")
                ),
                headers=headers,
                timeout=(10, 30),
            )
            if detail_response.status_code == 200:
                payload = detail_response.json()
                if isinstance(payload, dict):
                    details = payload
        except (requests.RequestException, ValueError):
            details = {}

    merged = {**best, **details}
    hours, is_open_now = _yelp_hours(merged.get("hours"))
    raw_categories = merged.get("categories")
    categories = [
        str(item.get("title") or "").strip()
        for item in raw_categories
        if isinstance(item, dict)
    ] if isinstance(raw_categories, list) else []
    location = merged.get("location")
    location = location if isinstance(location, dict) else {}
    raw_address = location.get("display_address")
    address = ", ".join(
        str(part).strip()
        for part in raw_address
        if str(part).strip()
    ) if isinstance(raw_address, list) else ""
    raw_photos = merged.get("photos")
    photos = [
        str(url).strip()
        for url in raw_photos
        if str(url).strip()
    ] if isinstance(raw_photos, list) else []
    raw_transactions = merged.get("transactions")
    transactions = [
        str(value).strip()
        for value in raw_transactions
        if str(value).strip()
    ] if isinstance(raw_transactions, list) else []
    try:
        rating = float(merged.get("rating"))
    except (TypeError, ValueError):
        rating = None
    try:
        review_count = int(merged.get("review_count"))
    except (TypeError, ValueError):
        review_count = 0
    try:
        distance = float(merged.get("distance"))
    except (TypeError, ValueError):
        distance = None

    return {
        "found": True,
        "id": business_id,
        "name": str(merged.get("name") or query),
        "rating": rating,
        "review_count": review_count,
        "price": str(merged.get("price") or ""),
        "categories": categories,
        "address": address,
        "phone": str(
            merged.get("display_phone") or merged.get("phone") or ""
        ),
        "url": str(merged.get("url") or ""),
        "image_url": str(merged.get("image_url") or ""),
        "photos": photos[:6],
        "hours": hours,
        "is_open_now": is_open_now,
        "transactions": transactions,
        "distance_m": distance,
        "is_closed": bool(merged.get("is_closed")),
    }

def search_map_places(location: str) -> tuple[dict, list[dict]]:
    """Geocode a location and return nearby Wikipedia article cards."""
    geo = geocode_location(location)
    pages = search_wikipedia_nearby(geo["latitude"], geo["longitude"])

    results: list[dict] = []
    for page in pages:
        title = _map_plain_text(page.get("title")) or "(untitled)"
        description = _map_plain_text(page.get("description"))
        extract = _map_plain_text(page.get("extract"))
        link = str(page.get("link") or "").strip()
        if not link and not extract:
            continue

        distance = _map_distance(page.get("distance_m"))
        coordinates = ""
        latitude = page.get("latitude")
        longitude = page.get("longitude")
        if isinstance(latitude, (int, float)) and isinstance(
            longitude, (int, float)
        ):
            coordinates = f"{latitude:.6g}, {longitude:.6g}"

        paragraphs: list[str] = []
        if extract:
            paragraphs.append(extract)
        detail_parts: list[str] = []
        if distance:
            detail_parts.append(
                f"Distance: {distance} from {geo['display_name']}"
            )
        if coordinates:
            detail_parts.append(f"Coordinates: {coordinates}")
        if detail_parts:
            paragraphs.append(" · ".join(detail_parts))

        # The list shows the distance and description in its meta line; the
        # modal already renders the extract paragraph, so keep the summary to
        # the one-line description to avoid showing the extract twice.
        card_summary = description
        section = " · ".join(value for value in (distance, description) if value)

        image_url = str(page.get("image") or "").strip()

        results.append(
            {
                "title": title,
                "link": link,
                "section": section,
                "summary": card_summary,
                "paragraphs": paragraphs,
                "images": [image_url] if image_url else [],
                "distance": distance,
                "coordinates": coordinates,
                "latitude": (
                    float(latitude)
                    if isinstance(latitude, (int, float))
                    else None
                ),
                "longitude": (
                    float(longitude)
                    if isinstance(longitude, (int, float))
                    else None
                ),
            }
        )
    return geo, results
