"""Map explorer games, place modals, and local news."""

from __future__ import annotations

from .core import *
from .map import GAME_NEARBY_LIMIT, GAME_NEARBY_RADIUS_METERS, GAME_NEWS_LIMIT, MAP_GEOCODE_LIMIT

def _game_place_display_name(properties: dict) -> str:
    """Build a readable suggestion label from Photon feature properties."""
    name = _map_plain_text(properties.get("name"))
    if not name:
        name = _map_plain_text(properties.get("street"))
    if not name:
        return ""
    parts = [name]
    seen = {name.casefold()}
    for key in ("street", "city", "state"):
        value = _map_plain_text(properties.get(key))
        if value and value.casefold() not in seen:
            parts.append(value)
            seen.add(value.casefold())
    return ", ".join(parts)

def search_game_place_suggestions(
    query: str,
    limit: int = MAP_GEOCODE_LIMIT,
    *,
    bbox: object = None,
) -> list[dict]:
    """Find named places for the /game current-location autocomplete.

    Photon resolves fuzzy place names (for example a misspelled trail name)
    and returns OSM nodes, ways, and relations with coordinates. The result
    shape matches ``geocode_locations`` so the browser can use one suggestion
    list and one selection handler for both places and addresses.
    """
    query = " ".join(str(query or "").split())
    if not query:
        return []
    limit = max(1, min(int(limit), 10))
    normalized_bbox = _geocode_viewbox(bbox)

    params: dict[str, object] = {
        "q": query,
        "limit": limit,
        "lang": "en",
    }
    if normalized_bbox:
        west, south, east, north = normalized_bbox
        params["bbox"] = f"{west},{south},{east},{north}"

    try:
        response = requests.get(
            PHOTON_SEARCH_URL,
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
        raise RuntimeError(f"Place search failed: {exc}") from exc

    features = data.get("features") if isinstance(data, dict) else None
    results: list[dict] = []
    seen: set[tuple[str, int, int]] = set()
    for feature in features if isinstance(features, list) else []:
        if not isinstance(feature, dict):
            continue
        properties = feature.get("properties")
        geometry = feature.get("geometry")
        if not isinstance(properties, dict) or not isinstance(geometry, dict):
            continue
        display_name = _game_place_display_name(properties)
        coordinates = geometry.get("coordinates")
        if (
            not display_name
            or not isinstance(coordinates, list)
            or len(coordinates) < 2
        ):
            continue
        try:
            longitude = float(coordinates[0])
            latitude = float(coordinates[1])
        except (TypeError, ValueError):
            continue
        key = (display_name.casefold(), round(latitude, 6), round(longitude, 6))
        if key in seen:
            continue
        seen.add(key)

        raw_osm_type = str(properties.get("osm_type") or "").strip().upper()
        osm_type = {
            "N": "node",
            "W": "way",
            "R": "relation",
        }.get(raw_osm_type, raw_osm_type.lower())
        osm_id = properties.get("osm_id")
        if osm_type and osm_id is not None:
            link = f"https://www.openstreetmap.org/{osm_type}/{osm_id}"
        else:
            link = (
                f"https://www.openstreetmap.org/?mlat={latitude:.6f}"
                f"&mlon={longitude:.6f}#map=18/{latitude:.6f}/{longitude:.6f}"
            )
        osm_value = _map_plain_text(
            properties.get("osm_value")
            or properties.get("type")
            or properties.get("osm_key")
        )
        category = osm_value.replace("_", " ").strip().title() or "Place"
        results.append(
            {
                "query": query,
                "display_name": display_name,
                "latitude": latitude,
                "longitude": longitude,
                "category": category,
                "type": osm_value,
                "osm_type": osm_type,
                "osm_id": osm_id,
                "address": {
                    key: _map_plain_text(properties.get(key))
                    for key in (
                        "street", "district", "city", "state",
                        "postcode", "country",
                    )
                    if properties.get(key)
                },
                "suggestion_kind": "place",
            }
        )
    return results

def search_game_geocode_suggestions(
    game: dict,
    query: str,
    limit: int = MAP_GEOCODE_LIMIT,
) -> list[dict]:
    """Return place suggestions followed by address suggestions for /game."""
    bounds = game.get("bounds")
    viewbox = None
    if isinstance(bounds, dict):
        viewbox = (
            bounds.get("west"),
            bounds.get("south"),
            bounds.get("east"),
            bounds.get("north"),
        )

    places: list[dict] = []
    addresses: list[dict] = []
    place_error: Exception | None = None
    address_error: Exception | None = None
    with ThreadPoolExecutor(max_workers=2) as executor:
        places_future = executor.submit(
            search_game_place_suggestions,
            query,
            limit,
            bbox=viewbox,
        )
        addresses_future = executor.submit(
            geocode_locations,
            query,
            limit,
            viewbox=viewbox,
            bounded=viewbox is not None,
        )
        try:
            places = places_future.result()
        except Exception as exc:  # noqa: BLE001 - report one side's failure
            place_error = exc
        try:
            addresses = addresses_future.result()
        except Exception as exc:  # noqa: BLE001 - report one side's failure
            address_error = exc

    if place_error is not None and address_error is not None:
        raise RuntimeError(
            f"Place and address search failed: {place_error}; {address_error}"
        )
    for item in addresses:
        item["suggestion_kind"] = "address"

    # Places are the new layer and appear above the existing addresses. Drop
    # an address that is the same named feature at the same coordinate.
    seen: set[tuple[str, int, int]] = set()
    combined: list[dict] = []
    for item in [*places, *addresses]:
        try:
            key = (
                str(item.get("display_name") or "").casefold(),
                round(float(item.get("latitude")), 6),
                round(float(item.get("longitude")), 6),
            )
        except (TypeError, ValueError):
            continue
        if key in seen:
            continue
        seen.add(key)
        combined.append(item)
    return combined

# ---- /game -----------------------------------------------------------
def list_game_prompts() -> list[dict]:
    """List the playable prompts in ./prompts/games.

    A prompt file gives the game its name, while GAME_START_LOCATIONS holds
    the map data the browser needs. Entries in the catalogue are still shown
    if the prompt folder is unavailable, so /game remains discoverable.
    """
    games: dict[str, dict] = {}
    try:
        prompt_paths = sorted(GAMES_DIR.glob("*.prompt"))
    except OSError:
        prompt_paths = []
    for path in prompt_paths:
        name = path.stem
        meta = GAME_START_LOCATIONS.get(name, {})
        games[name] = {
            "name": name,
            "title": str(meta.get("title") or name.replace("_", " ").title()),
            "playable": name in GAME_START_LOCATIONS,
            "youtube_video_search_prefix": _game_prompt_video_prefix(path),
            "bounds": _game_prompt_exploration_bounds(path),
        }
    for name, meta in GAME_START_LOCATIONS.items():
        games.setdefault(
            name,
            {
                "name": name,
                "title": str(
                    meta.get("title") or name.replace("_", " ").title()
                ),
                "playable": True,
                "youtube_video_search_prefix": "",
                "bounds": meta.get("bounds"),
            },
        )
    return [games[name] for name in sorted(games)]

def _game_prompt_video_prefix(path: Path) -> str:
    """Read the optional youtube_video_search_prefix from a game prompt."""
    try:
        text = path.read_text(encoding="utf-8", errors="replace")
    except OSError:
        return ""
    match = re.search(
        r"^\s*youtube_video_search_prefix\s*:\s*(.+?)\s*$",
        text,
        re.MULTILINE,
    )
    return match.group(1).strip() if match else ""

def _game_prompt_exploration_bounds(path: Path) -> dict | None:
    """Read an optional geographic boundary from a game prompt.

    A line such as ``exploration_bounds: south=32.5, west=-124.4,
    north=42.0, east=-114.1`` lets one game cover a whole state while the
    explorer still refuses to save a current location outside that area. The
    parser is deliberately small so prompts stay readable and no map service
    is required just to start a game.
    """
    try:
        text = path.read_text(encoding="utf-8", errors="replace")
    except OSError:
        return None
    match = re.search(
        r"^\s*exploration_bounds\s*:\s*(.+?)\s*$", text, re.MULTILINE
    )
    if not match:
        return None
    values: dict[str, float] = {}
    for part in match.group(1).split(","):
        key, separator, raw = part.partition("=")
        if not separator:
            return None
        key = key.strip().lower()
        if key not in {"south", "west", "north", "east"}:
            return None
        try:
            values[key] = float(raw.strip())
        except ValueError:
            return None
    if set(values) != {"south", "west", "north", "east"}:
        return None
    if not (
        -90 <= values["south"] < values["north"] <= 90
        and -180 <= values["west"] < values["east"] <= 180
    ):
        return None
    return values

def _game_prompt_data_file(path: Path) -> Path:
    """Resolve the optional game_data_file declared by one game prompt.

    The path may contain the same ``{diretory_of_this_prompt}`` placeholder
    (including its historical spelling) used by the prompt tasks. Declared
    paths are allowed only inside the repository so a prompt can never make
    the server write outside its own state directory.
    """
    fallback = GAME_DATA_DIR / f"{path.stem}.json"
    try:
        text = path.read_text(encoding="utf-8", errors="replace")
    except OSError:
        return fallback
    match = re.search(
        r"^\s*game_data_file\s*:\s*(.+?)\s*$", text, re.MULTILINE
    )
    if not match:
        return fallback
    raw = match.group(1).strip().strip('"').strip("'")
    for placeholder in (
        "{diretory_of_this_prompt}",
        "{directory_of_this_prompt}",
    ):
        raw = raw.replace(placeholder, str(path.parent))
    candidate = Path(raw).expanduser()
    if not candidate.is_absolute():
        candidate = SCRIPT_DIR / candidate
    try:
        candidate = candidate.resolve()
        candidate.relative_to(SCRIPT_DIR.resolve())
    except (OSError, ValueError):
        return fallback
    if candidate.suffix.lower() != ".json":
        return fallback
    return candidate

def game_data_file(game_name: str) -> Path:
    """Return the JSON state file used by one explorer game."""
    name = str(game_name or "")
    prompt_path = GAMES_DIR / f"{name}.prompt"
    try:
        prompt_path.resolve().relative_to(GAMES_DIR.resolve())
    except (OSError, ValueError):
        prompt_path = GAMES_DIR / "__invalid__.prompt"
    if prompt_path.is_file():
        return _game_prompt_data_file(prompt_path)
    safe = re.sub(r"[^A-Za-z0-9_.-]+", "_", name).strip("._") or "game"
    return GAME_DATA_DIR / f"{safe}.json"

def load_game_data(game_name: str) -> dict:
    """Read one game's persisted state, tolerating a missing/corrupt file."""
    path = game_data_file(game_name)
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}
    return data if isinstance(data, dict) else {}

def save_game_data(game_name: str, data: dict) -> None:
    """Atomically write one game's state file."""
    path = game_data_file(game_name)
    path.parent.mkdir(parents=True, exist_ok=True)
    with GAME_DATA_LOCK:
        descriptor, temporary = tempfile.mkstemp(
            prefix=f".{path.name}.", suffix=".tmp", dir=str(path.parent)
        )
        try:
            with os.fdopen(descriptor, "w", encoding="utf-8") as handle:
                json.dump(data, handle, ensure_ascii=False, indent=2)
                handle.write("\n")
            os.replace(temporary, path)
        except Exception:
            try:
                os.unlink(temporary)
            except OSError:
                pass
            raise

def _game_bounds_contain(bounds: object, latitude: float, longitude: float) -> bool:
    """Return whether a coordinate is inside a prompt's exploration bounds."""
    if not isinstance(bounds, dict):
        return True
    try:
        south = float(bounds["south"])
        west = float(bounds["west"])
        north = float(bounds["north"])
        east = float(bounds["east"])
    except (KeyError, TypeError, ValueError):
        return True
    return south <= latitude <= north and west <= longitude <= east

def _valid_game_coordinates(latitude: object, longitude: object) -> bool:
    """Return whether two values are an in-range latitude/longitude pair."""
    return (
        isinstance(latitude, (int, float))
        and isinstance(longitude, (int, float))
        and math.isfinite(latitude)
        and math.isfinite(longitude)
        and -90 <= latitude <= 90
        and -180 <= longitude <= 180
    )

def _osm_point_link(latitude: float, longitude: float) -> str:
    """Build an OpenStreetMap link for a right-clicked map coordinate."""
    return (
        f"https://www.openstreetmap.org/?mlat={latitude:.6f}"
        f"&mlon={longitude:.6f}#map=18/{latitude:.6f}/{longitude:.6f}"
    )

def _game_nearby_cache_key(
    latitude: float,
    longitude: float,
    radius_meters: int,
    place_limit: int,
    restaurant_limit: int,
) -> tuple[float, float, int, int, int]:
    """Return a stable cache key for one explorer nearby lookup."""
    return (
        round(float(latitude), 5),
        round(float(longitude), 5),
        int(radius_meters),
        int(place_limit),
        int(restaurant_limit),
    )

def cached_game_nearby(
    key: tuple[float, float, int, int, int],
) -> dict | None:
    """Return the unexpired cached nearby payload for key, if any."""
    now = time.monotonic()
    with GAME_NEARBY_CACHE_LOCK:
        entry = GAME_NEARBY_CACHE.get(key)
        if entry is None:
            return None
        stored_at, payload = entry
        if now - stored_at > GAME_NEARBY_CACHE_TTL_SECONDS:
            GAME_NEARBY_CACHE.pop(key, None)
            return None
        return dict(payload)

def store_game_nearby(
    key: tuple[float, float, int, int, int], payload: dict
) -> None:
    """Remember a successful nearby payload until its TTL expires."""
    with GAME_NEARBY_CACHE_LOCK:
        if len(GAME_NEARBY_CACHE) >= GAME_NEARBY_CACHE_MAX_ITEMS:
            oldest = min(
                GAME_NEARBY_CACHE,
                key=lambda item: GAME_NEARBY_CACHE[item][0],
            )
            GAME_NEARBY_CACHE.pop(oldest, None)
        GAME_NEARBY_CACHE[key] = (time.monotonic(), dict(payload))

def saved_game_start(game_name: str) -> dict | None:
    """Return the saved current_location as a /game start payload."""
    saved = load_game_data(game_name).get("current_location")
    if not isinstance(saved, dict):
        return None
    try:
        latitude = float(saved.get("latitude"))
        longitude = float(saved.get("longitude"))
    except (TypeError, ValueError):
        return None
    if not _valid_game_coordinates(latitude, longitude):
        return None
    name = " ".join(
        str(saved.get("name") or saved.get("title") or "Saved location").split()
    )
    link = str(saved.get("link") or "").strip()
    category = " ".join(str(saved.get("category") or "").split())[:80]
    return {
        "name": name or "Saved location",
        "latitude": latitude,
        "longitude": longitude,
        "link": link or _osm_point_link(latitude, longitude),
        "category": category or "Custom location",
    }

def _game_place_category(tags: dict) -> str:
    """Return a short readable category for one OSM element."""
    for key in ("tourism", "historic", "leisure", "amenity"):
        value = _map_plain_text(tags.get(key)).replace("_", " ").strip()
        if value:
            return value.title()
    return "Place"

def search_game_places(
    latitude: float,
    longitude: float,
    radius_meters: int = GAME_NEARBY_RADIUS_METERS,
    limit: int = GAME_NEARBY_LIMIT,
) -> list[dict]:
    """List named places near one coordinate for the /game explorer.

    Overpass supplies the landmarks, parks, museums, historic sites, markets,
    hospitals, libraries, schools, churches, and other visitor-friendly places
    around the current location. Named elements only are kept because every
    result becomes a clickable marker in the explorer modal. If Overpass is
    unavailable, the Wikipedia GeoSearch fallback still gives the game useful
    places to walk between.
    """
    radius = max(100, min(int(radius_meters), 15_000))
    limit = max(1, min(int(limit), 30))
    query = (
        "[out:json][timeout:25];\n"
        "(\n"
        '  nwr["name"]["tourism"~"^(attraction|museum|viewpoint|'
        'gallery|zoo|theme_park|aquarium)$"]'
        f"(around:{radius},{latitude:.6f},{longitude:.6f});\n"
        '  nwr["name"]["historic"~"^(castle|fort|monument|archaeological_site|'
        'ruins|city_gate|battlefield|wayside_shrine|tower)$"]'
        f"(around:{radius},{latitude:.6f},{longitude:.6f});\n"
        '  nwr["name"]["leisure"~"^(park|garden|nature_reserve)$"]'
        f"(around:{radius},{latitude:.6f},{longitude:.6f});\n"
        '  nwr["name"]["amenity"~"^(theatre|arts_centre|marketplace|hospital|'
        'library|school|place_of_worship)$"]'
        f"(around:{radius},{latitude:.6f},{longitude:.6f});\n"
        '  nwr["name"]["building"="church"]'
        f"(around:{radius},{latitude:.6f},{longitude:.6f});\n"
        ");\n"
        "out center 100;"
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

    elements = data.get("elements") if isinstance(data, dict) else None
    results: list[dict] = []
    seen_links: set[str] = set()
    seen_names: set[str] = set()
    if isinstance(elements, list):
        for element in elements:
            if not isinstance(element, dict):
                continue
            tags = element.get("tags")
            if not isinstance(tags, dict):
                continue
            name = _osm_tag_text(tags, "name:en", "name")
            if not name or name.lower() in seen_names:
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
            if link in seen_links:
                continue
            seen_links.add(link)
            seen_names.add(name.lower())

            place_latitude = float(element_latitude)
            place_longitude = float(element_longitude)
            distance = _haversine_meters(
                latitude, longitude, place_latitude, place_longitude
            )
            # The current location may also be tagged as an attraction; do not
            # offer it as a place to click while standing on it.
            if distance is not None and distance < 15:
                continue

            category = _game_place_category(tags)
            description = _map_plain_text(
                tags.get("description:en") or tags.get("description")
            )
            distance_text = _map_distance(distance)
            section = " · ".join(
                value for value in (distance_text, category) if value
            )
            results.append(
                {
                    "title": name,
                    "link": link,
                    "category": category,
                    "section": section,
                    "summary": description or category,
                    "latitude": place_latitude,
                    "longitude": place_longitude,
                    "distance_m": distance,
                    "distance_text": distance_text,
                    "source": "OpenStreetMap",
                    "osm_type": osm_type,
                    "osm_id": osm_id,
                }
            )

    results.sort(
        key=lambda item: (
            item["distance_m"] is None,
            item["distance_m"] if item["distance_m"] is not None else 0.0,
            item["title"].lower(),
        )
    )
    if results:
        return results[:limit]

    # Overpass returned nothing (or failed). Wikipedia's GeoSearch API uses a
    # different service, so it can keep the explorer playable during an
    # Overpass outage.
    try:
        pages = search_wikipedia_nearby(
            latitude, longitude, radius_meters=radius, limit=limit
        )
    except RuntimeError:
        if isinstance(data, dict):
            return []
        raise RuntimeError(f"OpenStreetMap place search failed: {last_error}")

    fallback: list[dict] = []
    for page in pages:
        title = _map_plain_text(page.get("title"))
        page_latitude = page.get("latitude")
        page_longitude = page.get("longitude")
        if (
            not title
            or not isinstance(page_latitude, (int, float))
            or not isinstance(page_longitude, (int, float))
        ):
            continue
        distance = page.get("distance_m")
        distance_text = _map_distance(distance)
        description = _map_plain_text(page.get("description"))
        fallback.append(
            {
                "title": title,
                "link": str(page.get("link") or ""),
                "category": "Wikipedia",
                "section": " · ".join(
                    value for value in (distance_text, "Wikipedia") if value
                ),
                "summary": description or "Wikipedia article",
                "latitude": float(page_latitude),
                "longitude": float(page_longitude),
                "distance_m": distance,
                "distance_text": distance_text,
                "source": "Wikipedia",
            }
        )
    return fallback[:limit]

@lru_cache(maxsize=512)
def _game_wikipedia_page(language: str, page_title: str) -> dict:
    """Fetch a Wikipedia page's lead extract, image, and URL."""
    language = re.sub(r"[^a-z-]", "", str(language or "").lower()) or "en"
    page_title = str(page_title or "").strip()
    if not page_title:
        return {}
    params = {
        "action": "query",
        "titles": page_title,
        "prop": "extracts|pageimages|info|coordinates",
        "inprop": "url",
        "exintro": 1,
        "explaintext": 1,
        "exchars": 2_500,
        "piprop": "thumbnail",
        "pithumbsize": 640,
        "redirects": 1,
        "format": "json",
        "formatversion": 2,
    }
    try:
        response = requests.get(
            f"https://{language}.wikipedia.org/w/api.php",
            params=params,
            headers=chat.HTTP_HEADERS,
            timeout=(10, 30),
        )
        response.raise_for_status()
        data = response.json()
    except (requests.RequestException, ValueError):
        return {}
    query = data.get("query") if isinstance(data, dict) else None
    pages = query.get("pages") if isinstance(query, dict) else None
    if isinstance(pages, dict):
        pages = list(pages.values())
    if not isinstance(pages, list) or not pages:
        return {}
    page = pages[0]
    if not isinstance(page, dict) or page.get("missing"):
        return {}
    thumbnail = page.get("thumbnail")
    image = ""
    if isinstance(thumbnail, dict):
        image = str(thumbnail.get("source") or "").strip()
        if image.startswith("//"):
            image = "https:" + image
    latitude = None
    longitude = None
    coordinates = page.get("coordinates")
    if isinstance(coordinates, list) and coordinates:
        first = coordinates[0]
        if isinstance(first, dict):
            try:
                latitude = float(first.get("lat"))
                longitude = float(first.get("lon"))
            except (TypeError, ValueError):
                latitude = None
                longitude = None
    return {
        "title": str(page.get("title") or ""),
        "extract": _map_plain_text(page.get("extract")),
        "description": _map_plain_text(page.get("description")),
        "image": image,
        "link": str(page.get("fullurl") or "").strip(),
        "latitude": latitude,
        "longitude": longitude,
    }

def _game_wikipedia_details(tags: dict) -> dict:
    """Return Wikipedia details from an OSM ``wikipedia`` tag, if present."""
    raw = _map_plain_text(tags.get("wikipedia:en") or tags.get("wikipedia"))
    if not raw:
        return {}
    if ":" in raw:
        language, page_title = raw.split(":", 1)
    else:
        language, page_title = "en", raw
    return _game_wikipedia_page(language, page_title)

GAME_WIKI_NAME_STOP_WORDS = {
    "the",
    "a",
    "an",
    "of",
    "and",
    "at",
    "in",
    "on",
    "for",
    "to",
    "gallery",
    "museum",
    "park",
    "building",
    "center",
    "centre",
    "theater",
    "theatre",
    "plaza",
    "garden",
    "gardens",
}

def _game_wiki_normalize(value: object) -> str:
    """Normalize a place or article name for matching."""
    text = html.unescape(str(value or "")).lower().replace("&", " and ")
    text = unicodedata.normalize("NFKD", text)
    text = "".join(
        char for char in text if not unicodedata.combining(char)
    )
    text = re.sub(r"[^a-z0-9\u3040-\u30ff\u4e00-\u9fff]+", " ", text)
    return " ".join(text.split())

def _game_wiki_tokens(value: object) -> set[str]:
    """Return distinctive tokens from a place or article name."""
    return {
        token
        for token in _game_wiki_normalize(value).split()
        if len(token) > 2 and token not in GAME_WIKI_NAME_STOP_WORDS
    }

def _game_wiki_matches_place(page_title: str, names: list[str]) -> bool:
    """Check whether a Wikipedia page title really describes the place.

    The GeoSearch fallback used by /map returns the closest articles around a
    coordinate, not necessarily the same landmark. For example, searching
    around Modern Eden Gallery can return the Hotel Carlton article. A nearby
    article is only used as the place introduction when its title matches the
    OSM name, the English name, or shares enough distinctive words.
    """
    title_normalized = _game_wiki_normalize(page_title)
    if not title_normalized:
        return False
    title_tokens = _game_wiki_tokens(page_title)
    for candidate in names:
        name_normalized = _game_wiki_normalize(candidate)
        if not name_normalized:
            continue
        if title_normalized == name_normalized:
            return True
        # A multi-word name contained in the title (or the reverse) is a
        # strong match. A single-word page such as "Sapporo" must not match
        # every place whose name starts with that city name.
        if (
            len(name_normalized) >= 8
            and len(name_normalized.split()) >= 2
            and name_normalized in title_normalized
        ):
            return True
        if (
            len(title_normalized) >= 8
            and len(title_normalized.split()) >= 2
            and title_normalized in name_normalized
        ):
            return True
        name_tokens = _game_wiki_tokens(candidate)
        if not name_tokens or not title_tokens:
            continue
        overlap = len(name_tokens & title_tokens)
        if (
            overlap >= 2
            and overlap / max(len(name_tokens), len(title_tokens)) >= 0.75
        ):
            return True
    return False

@lru_cache(maxsize=512)
def _game_wikidata_sitelink(wikidata_id: str, language: str = "en") -> str:
    """Return the Wikipedia title linked from an OSM ``wikidata`` tag."""
    identifier = str(wikidata_id or "").strip().upper()
    if not re.fullmatch(r"Q\d+", identifier):
        return ""
    site = f"{language}wiki"
    try:
        response = requests.get(
            "https://www.wikidata.org/w/api.php",
            params={
                "action": "wbgetentities",
                "ids": identifier,
                "props": "sitelinks",
                "sitefilter": site,
                "format": "json",
            },
            headers=chat.HTTP_HEADERS,
            timeout=(10, 30),
        )
        response.raise_for_status()
        data = response.json()
    except (requests.RequestException, ValueError):
        return ""
    entities = data.get("entities") if isinstance(data, dict) else None
    entity = entities.get(identifier) if isinstance(entities, dict) else None
    sitelinks = entity.get("sitelinks") if isinstance(entity, dict) else None
    sitelink = sitelinks.get(site) if isinstance(sitelinks, dict) else None
    if not isinstance(sitelink, dict):
        return ""
    return str(sitelink.get("title") or "").strip()

def _game_wikipedia_from_url(url: str) -> dict:
    """Return Wikipedia details when the place link is already a wiki URL."""
    match = re.match(
        r"https?://([a-z-]+)\.wikipedia\.org/wiki/([^?#]+)",
        str(url or ""),
        re.IGNORECASE,
    )
    if not match:
        return {}
    return _game_wikipedia_page(
        match.group(1), unquote(match.group(2).replace("_", " "))
    )

def _game_image_url(tags: dict) -> str:
    """Turn an OSM image or Wikimedia Commons tag into an image URL."""
    value = _osm_tag_text(tags, "image", "wikimedia_commons")
    if not value:
        return ""
    if value.startswith("http://") or value.startswith("https://"):
        return value
    if value.lower().startswith("file:"):
        return (
            "https://commons.wikimedia.org/wiki/Special:FilePath/"
            + quote(value[5:], safe="()_,.- ")
        )
    return ""

GAME_WEBSITE_BOILERPLATE_RE = re.compile(
    r"^(?:your cart|sign up|subscribe|newsletter|loading\b|cookies?\b|"
    r"accept (?:all|cookies)|all rights reserved|taxes and shipping|"
    r"browse our (?:entire|full) inventory|all available works|"
    r"available works|privacy policy|terms of service)\b",
    re.IGNORECASE,
)

def _game_text_paragraphs(text: str) -> list[str]:
    """Split Wikipedia/trafilatura plain text into clean paragraphs."""
    paragraphs: list[str] = []
    seen: set[str] = set()
    for chunk in re.split(r"\n\s*\n|\n", str(text or "")):
        paragraph = " ".join(html.unescape(chunk).split())
        if len(paragraph) < 12:
            continue
        if GAME_WEBSITE_BOILERPLATE_RE.match(paragraph):
            continue
        folded = paragraph.casefold()
        if folded in seen:
            continue
        seen.add(folded)
        paragraphs.append(paragraph)
    return paragraphs

def fetch_game_place_website(url: str) -> dict:
    """Extract readable information from a place's linked website.

    The OSM ``website``/``contact:website`` tag is often the most useful
    description of a small gallery, shop, or venue that has no Wikipedia
    article. trafilatura removes navigation and boilerplate and returns the
    main article text plus its metadata. The result is cached by URL for an
    hour so reopening the same place modal does not re-download the site.
    """
    url = str(url or "").strip()
    result = {
        "url": url,
        "link": url,
        "title": "",
        "site": "",
        "paragraphs": [],
        "image": "",
        "error": "",
    }
    parsed = urlparse(url)
    if parsed.scheme not in {"http", "https"} or not parsed.netloc:
        result["error"] = "The place's website address is not a valid HTTP URL."
        return result

    now = time.monotonic()
    with GAME_WEBSITE_CACHE_LOCK:
        cached = GAME_WEBSITE_CACHE.get(url)
        if cached and now - cached[0] <= GAME_WEBSITE_CACHE_TTL_SECONDS:
            return cached[1]

    if trafilatura is None:
        result["error"] = (
            "The Python trafilatura package is not installed, so the "
            "place's website could not be read."
        )
        return result

    try:
        with requests.get(
            url,
            headers={
                **chat.HTTP_HEADERS,
                "User-Agent": MAP_USER_AGENT,
                "Accept": "text/html,application/xhtml+xml",
            },
            timeout=(8, 20),
            stream=True,
            allow_redirects=True,
        ) as response:
            response.raise_for_status()
            content_type = str(
                response.headers.get("Content-Type") or ""
            ).split(";", 1)[0].strip().lower()
            if content_type and not (
                content_type.startswith("text/")
                or content_type in {"application/xhtml+xml", "application/xml"}
            ):
                result["error"] = (
                    "The place's website did not return an HTML page."
                )
                return result
            final_url = str(response.url or url).strip()
            chunks: list[bytes] = []
            remaining = GAME_WEBSITE_MAX_BYTES
            for chunk in response.iter_content(chunk_size=65_536):
                if not chunk or remaining <= 0:
                    continue
                piece = chunk[:remaining]
                chunks.append(piece)
                remaining -= len(piece)
            raw = b"".join(chunks)
            encoding = response.encoding or "utf-8"
    except requests.RequestException as exc:
        result["error"] = f"Could not load the place's website: {exc}"
        return result

    try:
        html_text = raw.decode(encoding, errors="replace")
    except LookupError:
        html_text = raw.decode("utf-8", errors="replace")

    metadata: dict = {}
    last_extraction_error: Exception | None = None
    for extraction_options in (
        {"favor_precision": True},
        {},
        {"favor_recall": True},
    ):
        try:
            extracted = trafilatura.extract(
                html_text,
                url=final_url,
                output_format="json",
                with_metadata=True,
                include_comments=False,
                include_tables=False,
                **extraction_options,
            )
        except Exception as exc:  # trafilatura can raise parser-specific errors
            last_extraction_error = exc
            continue
        if not extracted:
            continue
        try:
            decoded = json.loads(extracted)
        except (TypeError, ValueError):
            continue
        if not isinstance(decoded, dict):
            continue
        if not metadata:
            metadata = decoded
        if str(decoded.get("text") or "").strip():
            metadata = decoded
            break
    if not metadata and last_extraction_error is not None:
        result["error"] = (
            "Could not extract readable text from the place's website: "
            f"{last_extraction_error}"
        )
        return result

    text = str(metadata.get("text") or "")
    excerpt = _map_plain_text(
        metadata.get("excerpt") or metadata.get("description") or ""
    )
    paragraphs = _game_text_paragraphs(text)
    if excerpt and not any(
        excerpt.casefold() in paragraph.casefold()
        or paragraph.casefold() in excerpt.casefold()
        for paragraph in paragraphs
    ):
        paragraphs.insert(0, excerpt)

    limited: list[str] = []
    budget = GAME_WEBSITE_MAX_CHARS
    for paragraph in paragraphs:
        if budget <= 0 or len(limited) >= GAME_WEBSITE_MAX_PARAGRAPHS:
            break
        if len(paragraph) > budget:
            paragraph = paragraph[:budget].rsplit(" ", 1)[0].rstrip() + "…"
        limited.append(paragraph)
        budget -= len(paragraph)

    link = str(metadata.get("source") or final_url).strip()
    if not re.match(r"^https?://", link, re.IGNORECASE):
        link = final_url
    result.update(
        {
            "link": link,
            "title": _map_plain_text(metadata.get("title") or ""),
            "site": _map_plain_text(
                metadata.get("sitename")
                or metadata.get("source-hostname")
                or ""
            ),
            "paragraphs": limited,
            "image": str(metadata.get("image") or "").strip(),
            "error": "" if limited else (
                "No readable text could be extracted from the place's website."
            ),
        }
    )
    if limited:
        with GAME_WEBSITE_CACHE_LOCK:
            if len(GAME_WEBSITE_CACHE) >= GAME_WEBSITE_CACHE_MAX_ITEMS:
                GAME_WEBSITE_CACHE.pop(next(iter(GAME_WEBSITE_CACHE)), None)
            GAME_WEBSITE_CACHE[url] = (now, result)
    return result

def fetch_game_place_details(
    osm_type: str = "",
    osm_id: str | int = "",
    name: str = "",
    latitude: float | None = None,
    longitude: float | None = None,
    link: str = "",
) -> dict:
    """Fetch one explorer marker's details for the /map-style place modal.

    The right-click handler already knows the marker's OSM identity and link,
    so the exact OSM object is queried for its tags. When the object carries a
    ``wikipedia`` tag (or its link is a Wikipedia URL), that page's lead
    extract and image are added too. The same tags supply a ``website`` URL,
    whose readable text is extracted with trafilatura for the modal's "From
    Website" section. The result deliberately reuses the /map card shape: a
    summary, paragraphs, images, coordinates, and ``map: true`` so the shared
    modal draws the OpenStreetMap embed and related content.
    """
    osm_type = str(osm_type or "").strip().lower()
    identifier = str(osm_id or "").strip()
    if link:
        match = re.search(
            r"openstreetmap\.org/(node|way|relation)/(\d+)",
            str(link),
            re.IGNORECASE,
        )
        if match:
            osm_type = match.group(1).lower()
            identifier = match.group(2)

    valid_identity = (
        osm_type in OSM_RESTAURANT_TYPES
        and identifier.isdigit()
        and int(identifier) > 0
    )
    cache_key = (osm_type, identifier) if valid_identity else None
    now = time.monotonic()
    if cache_key is not None:
        with GAME_PLACE_CACHE_LOCK:
            cached = GAME_PLACE_CACHE.get(cache_key)
            if cached and now - cached[0] <= GAME_PLACE_CACHE_TTL_SECONDS:
                return cached[1]

    tags: dict = {}
    found = False
    if valid_identity:
        query = (
            "[out:json][timeout:20];\n"
            f"{osm_type}({identifier});\n"
            "out center;"
        )
        data = None
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
            except (requests.RequestException, ValueError):
                continue
        elements = data.get("elements") if isinstance(data, dict) else None
        element = next(
            (
                item
                for item in elements or []
                if isinstance(item, dict) and isinstance(item.get("tags"), dict)
            ),
            None,
        )
        if element is not None:
            tags = element["tags"]
            found = True
            if latitude is None or longitude is None:
                latitude, longitude = _osm_coordinates(element)

    osm_name = _osm_tag_text(tags, "name")
    title = _osm_tag_text(tags, "name:en", "name") or _map_plain_text(name) or "Place"
    category = _game_place_category(tags) if tags else "Place"
    description = _osm_tag_text(tags, "description", "note")
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
    opening_hours = _osm_hours_summary(
        tags.get("opening_hours"), latitude, longitude
    ).get("raw", "")
    website = _osm_tag_text(tags, "website", "contact:website", "url")
    phone = _osm_tag_text(tags, "phone", "contact:phone")

    wiki = _game_wikipedia_details(tags)
    # /map cards use English Wikipedia, so prefer the English page from the
    # Wikidata sitelink when the OSM object has one.
    wikidata_title = _game_wikidata_sitelink(tags.get("wikidata", ""))
    if wikidata_title:
        english_wiki = _game_wikipedia_page("en", wikidata_title)
        if english_wiki.get("extract"):
            wiki = english_wiki
    if not wiki and link:
        wiki = _game_wikipedia_from_url(link)
    if not wiki and title and re.search(r"[A-Za-z]", title):
        # Exact-title lookup only; a missing page simply returns no extract.
        wiki = _game_wikipedia_page("en", title)
    if not wiki and osm_name and re.search(r"[\u3040-\u30ff\u4e00-\u9fff]", osm_name):
        # Many Japanese landmarks have a Japanese Wikipedia page even when
        # their OSM element has no wikipedia tag.
        wiki = _game_wikipedia_page("ja", osm_name)
    if not wiki and isinstance(latitude, (int, float)) and isinstance(
        longitude, (int, float)
    ):
        # Last resort mirrors /map, but only a title-matching page is used.
        # GeoSearch returns the nearest articles, not necessarily this place;
        # accepting the first result blindly produced unrelated introductions
        # such as Hotel Carlton for Modern Eden Gallery.
        try:
            wiki_pages = search_wikipedia_nearby(
                float(latitude), float(longitude), radius_meters=500, limit=5
            )
        except RuntimeError:
            wiki_pages = []
        place_names = list(
            dict.fromkeys(
                value
                for value in (title, osm_name, _map_plain_text(name))
                if value
            )
        )
        for page in wiki_pages:
            page_title = str(page.get("title") or "")
            if not _game_wiki_matches_place(page_title, place_names):
                continue
            wiki = {
                "title": page_title,
                "extract": _map_plain_text(page.get("extract")),
                "description": _map_plain_text(page.get("description")),
                "image": str(page.get("image") or "").strip(),
                "link": str(page.get("link") or "").strip(),
            }
            break
    extract = str(wiki.get("extract") or "").strip()
    wiki_paragraphs = _game_text_paragraphs(extract)
    wiki_description = str(wiki.get("description") or "").strip()
    wiki_title = str(wiki.get("title") or "").strip()
    wiki_link = str(wiki.get("link") or "").strip()
    summary_description = wiki_description or description or category
    wikipedia = {}
    if extract or wiki_description or wiki_link:
        wikipedia = {
            "title": wiki_title or title,
            "description": wiki_description,
            "link": wiki_link,
            "paragraphs": wiki_paragraphs,
            "image": str(wiki.get("image") or "").strip(),
        }
    website_info = fetch_game_place_website(website) if website else {}

    # Wikipedia and website text are rendered as their own labelled sections,
    # so the generic paragraph list only carries the OSM facts. This avoids
    # showing the same extract twice in the place modal.
    paragraphs: list[str] = []
    detail_parts: list[str] = []
    if category and category != "Place":
        detail_parts.append(f"Type: {category}")
    if description and description not in (extract, wiki_description):
        detail_parts.append(f"About: {description}")
    if address:
        detail_parts.append(f"Address: {address}")
    if opening_hours:
        detail_parts.append(f"Opening hours: {opening_hours}")
    if phone:
        detail_parts.append(f"Phone: {phone}")
    if isinstance(latitude, (int, float)) and isinstance(longitude, (int, float)):
        detail_parts.append(
            f"Coordinates: {float(latitude):.6g}, {float(longitude):.6g}"
        )
    if detail_parts:
        paragraphs.append(" · ".join(detail_parts))
    if not paragraphs:
        paragraphs.append(
            "No detailed description is available for this place yet."
        )

    images: list[str] = []
    for image in (_game_image_url(tags), str(wiki.get("image") or "")):
        image = str(image or "").strip()
        if image and image not in images:
            images.append(image)

    if valid_identity:
        detail_link = f"https://www.openstreetmap.org/{osm_type}/{identifier}"
    else:
        detail_link = str(wiki.get("link") or link or "").strip()
    result = {
        "title": title,
        "link": detail_link,
        "source": "OpenStreetMap" if found else "Wikipedia",
        "section": category if category != "Place" else "Place",
        "summary": summary_description,
        "paragraphs": paragraphs,
        "wikipedia": wikipedia,
        "website": website_info,
        "images": images,
        "latitude": (
            float(latitude) if isinstance(latitude, (int, float)) else None
        ),
        "longitude": (
            float(longitude) if isinstance(longitude, (int, float)) else None
        ),
        "coordinates": (
            f"{float(latitude):.6g}, {float(longitude):.6g}"
            if isinstance(latitude, (int, float))
            and isinstance(longitude, (int, float))
            else ""
        ),
        "docs": True,
        "docs_label": "OpenStreetMap" if found else "Wikipedia",
        "learnable": False,
        "map": True,
        "complete": True,
        "osm_type": osm_type if found else "",
        "osm_id": int(identifier) if found else None,
        "found": found or bool(extract),
    }
    # A website failure is not cached with the place, so reopening the modal
    # can retry the site even while the Overpass/Wikipedia details stay valid.
    if cache_key is not None and not website_info.get("error"):
        with GAME_PLACE_CACHE_LOCK:
            if len(GAME_PLACE_CACHE) >= GAME_PLACE_CACHE_MAX_ITEMS:
                GAME_PLACE_CACHE.pop(next(iter(GAME_PLACE_CACHE)), None)
            GAME_PLACE_CACHE[cache_key] = (now, result)
    return result

def _game_reverse_place_name(data: dict) -> str:
    """Build a concise search phrase from a Nominatim reverse response."""
    address = data.get("address")
    address = address if isinstance(address, dict) else {}

    primary = str(data.get("name") or "").strip()
    if not primary:
        for key in (
            "amenity",
            "tourism",
            "shop",
            "leisure",
            "building",
            "road",
            "neighbourhood",
            "suburb",
            "city",
            "town",
            "village",
        ):
            primary = str(address.get(key) or "").strip()
            if primary:
                break
    if not primary:
        display_name = str(data.get("display_name") or "").strip()
        primary = display_name.split(",", 1)[0].strip()
    if not primary:
        return ""

    locality = ""
    for key in (
        "city",
        "town",
        "village",
        "municipality",
        "suburb",
        "neighbourhood",
        "county",
    ):
        locality = str(address.get(key) or "").strip()
        if locality:
            break

    if locality:
        primary_key = primary.casefold()
        locality_key = locality.casefold()
        if locality_key not in primary_key and primary_key not in locality_key:
            return f"{primary}, {locality}"
    return primary

def reverse_game_place_name(latitude: float, longitude: float) -> str:
    """Return the place name Nominatim assigns to a coordinate."""
    try:
        response = requests.get(
            NOMINATIM_REVERSE_URL,
            params={
                "lat": f"{float(latitude):.6f}",
                "lon": f"{float(longitude):.6f}",
                "format": "jsonv2",
                "zoom": 18,
                "addressdetails": 1,
            },
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
        raise RuntimeError(
            f"OpenStreetMap reverse geocoding failed: {exc}"
        ) from exc
    if not isinstance(data, dict) or data.get("error"):
        raise RuntimeError(
            "OpenStreetMap could not name the place at these coordinates."
        )
    place_name = _game_reverse_place_name(data)
    if not place_name:
        raise RuntimeError(
            "OpenStreetMap could not name the place at these coordinates."
        )
    return place_name

def search_game_place_news(
    latitude: float, longitude: float, limit: int = GAME_NEWS_LIMIT
) -> dict:
    """List recent Google News stories for the place at one coordinate.

    The explorer prompt asks for a Local News section: the place name is
    resolved from OpenStreetMap's Nominatim reverse endpoint, then Google News
    is searched through news_fetch.php's dynamic googlenews source. The list is
    lazy, so each story's full body still loads only when its card is opened.
    """
    latitude = float(latitude)
    longitude = float(longitude)
    if not (-90 <= latitude <= 90 and -180 <= longitude <= 180):
        raise ValueError("Missing or invalid place coordinates")
    limit = max(1, min(int(limit), 10))

    cache_key = (round(latitude, 5), round(longitude, 5), limit)
    now = time.monotonic()
    with GAME_NEWS_CACHE_LOCK:
        cached = GAME_NEWS_CACHE.get(cache_key)
        if cached and now - cached[0] <= GAME_NEWS_CACHE_TTL_SECONDS:
            return {
                **cached[1],
                "items": [dict(item) for item in cached[1].get("items", [])],
            }

    script = SCRIPT_DIR / "news_fetch.php"
    if not script.is_file():
        raise RuntimeError(f"missing {script.name}")

    place_name = reverse_game_place_name(latitude, longitude)
    argv = [
        "php",
        str(script),
        "--json",
        "--lazy",
        "--source",
        "googlenews",
        "--search",
        place_name,
        "--limit",
        str(max(limit * 2, 10)),
    ]
    try:
        completed = subprocess.run(
            argv,
            cwd=SCRIPT_DIR,
            capture_output=True,
            text=True,
            timeout=120,
        )
    except (OSError, subprocess.SubprocessError) as exc:
        raise RuntimeError(f"could not run {script.name}: {exc}") from exc

    items: list[dict] = []
    seen_links: set[str] = set()
    for payload in _read_news_ndjson(completed.stdout):
        title = str(payload.get("title") or "").strip()
        link = str(payload.get("link") or "").strip()
        if not title or not link or link in seen_links:
            continue
        seen_links.add(link)
        items.append(
            {
                "title": title,
                "link": link,
                "published": str(payload.get("published") or "").strip(),
                "source": str(payload.get("source") or "Google News").strip(),
                "section": str(payload.get("section") or "").strip(),
                "summary": str(payload.get("summary") or "").strip()[:400],
            }
        )
        if len(items) >= limit:
            break

    if completed.returncode != 0 and not items:
        detail = (completed.stderr or completed.stdout or "").strip()
        detail = detail.splitlines()[-1] if detail else "no news data"
        raise RuntimeError(f"Google News search failed: {detail}")

    result = {
        "place_name": place_name,
        "query": place_name,
        "items": items,
    }
    with GAME_NEWS_CACHE_LOCK:
        if len(GAME_NEWS_CACHE) >= GAME_NEWS_CACHE_MAX_ITEMS:
            GAME_NEWS_CACHE.pop(next(iter(GAME_NEWS_CACHE)), None)
        GAME_NEWS_CACHE[cache_key] = (
            now,
            {**result, "items": [dict(item) for item in items]},
        )
    return result


# Cross-component imports are placed after the definitions so a
# feature module can depend on another feature module safely.
from .books import _read_news_ndjson
from .core import SCRIPT_DIR
from .map import GAMES_DIR, GAME_DATA_DIR, GAME_DATA_LOCK, GAME_NEARBY_CACHE, GAME_NEARBY_CACHE_LOCK, GAME_NEARBY_CACHE_MAX_ITEMS, GAME_NEARBY_CACHE_TTL_SECONDS, GAME_NEWS_CACHE, GAME_NEWS_CACHE_LOCK, GAME_NEWS_CACHE_MAX_ITEMS, GAME_NEWS_CACHE_TTL_SECONDS, GAME_PLACE_CACHE, GAME_PLACE_CACHE_LOCK, GAME_PLACE_CACHE_MAX_ITEMS, GAME_PLACE_CACHE_TTL_SECONDS, GAME_START_LOCATIONS, GAME_WEBSITE_CACHE, GAME_WEBSITE_CACHE_LOCK, GAME_WEBSITE_CACHE_MAX_ITEMS, GAME_WEBSITE_CACHE_TTL_SECONDS, GAME_WEBSITE_MAX_BYTES, GAME_WEBSITE_MAX_CHARS, GAME_WEBSITE_MAX_PARAGRAPHS, MAP_USER_AGENT, NOMINATIM_REVERSE_URL, OSM_RESTAURANT_TYPES, OVERPASS_API_URLS, PHOTON_SEARCH_URL, _geocode_viewbox, _haversine_meters, _map_distance, _map_plain_text, _osm_coordinates, _osm_hours_summary, _osm_tag_text, geocode_locations, search_wikipedia_nearby
