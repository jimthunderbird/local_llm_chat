"""Zillow MCP nearby-listing searches."""

from __future__ import annotations

from .core import *
from .robinhood import ZILLOW_MCP_LIMIT, ZILLOW_MCP_RADIUS_MILES

_ZILLOW_CACHE_LOCK = threading.Lock()

_ZILLOW_REGION_CACHE: dict[tuple, tuple[float, dict]] = {}

_ZILLOW_REGION_CACHE_LOCK = threading.Lock()

def _haversine_miles(
    latitude1: float, longitude1: float, latitude2: float, longitude2: float
) -> float:
    """Return the great-circle distance between two coordinates in miles."""
    radius = 3958.7613
    lat1 = math.radians(latitude1)
    lat2 = math.radians(latitude2)
    delta_lat = lat2 - lat1
    delta_lon = math.radians(longitude2 - longitude1)
    a = (
        math.sin(delta_lat / 2) ** 2
        + math.cos(lat1) * math.cos(lat2) * math.sin(delta_lon / 2) ** 2
    )
    return radius * 2 * math.atan2(math.sqrt(a), math.sqrt(1 - a))

def _zillow_region_for_coordinate(latitude: float, longitude: float) -> dict:
    """Resolve a coordinate to a Zillow search region with Nominatim."""
    cache_key = (round(latitude, 5), round(longitude, 5))
    now = time.monotonic()
    with _ZILLOW_REGION_CACHE_LOCK:
        cached = _ZILLOW_REGION_CACHE.get(cache_key)
        if (
            cached
            and now - cached[0] <= ZILLOW_MCP_REGION_CACHE_TTL_SECONDS
        ):
            return dict(cached[1])

    try:
        response = requests.get(
            NOMINATIM_REVERSE_URL,
            params={
                "lat": f"{latitude:.6f}",
                "lon": f"{longitude:.6f}",
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
        raise ZillowMCPError(
            f"Could not resolve the place at these coordinates: {exc}"
        ) from exc
    if not isinstance(data, dict) or data.get("error"):
        raise ZillowMCPError(
            "OpenStreetMap could not name the place at these coordinates."
        )
    address = data.get("address")
    address = address if isinstance(address, dict) else {}

    def first(*keys: str) -> str:
        for key in keys:
            value = str(address.get(key) or "").strip()
            if value:
                return value
        return ""

    city = first("city", "town", "village", "municipality", "county")
    state = first("state")
    state_code = first("ISO3166-2-lvl4")
    if state_code.upper().startswith("US-"):
        state_code = state_code[3:]
    state_label = state_code or state
    neighborhood = first("neighbourhood", "suburb", "quarter")
    if not city or not state_label:
        raise ZillowMCPError(
            "OpenStreetMap could not resolve these coordinates to a "
            "Zillow city and state."
        )
    pivot = f"{city}, {state_label}"
    if neighborhood and neighborhood.casefold() != city.casefold():
        region_name = f"{neighborhood}, {city}, {state_label}"
        display_name = neighborhood
    else:
        region_name = pivot
        display_name = city
    result = {
        "name": region_name,
        "regionDisplayName": display_name,
        "description": f"Nearby listings around {latitude:.5f}, {longitude:.5f}",
        "pivot": pivot,
        "city": city,
        "state": state_label,
    }
    with _ZILLOW_REGION_CACHE_LOCK:
        if len(_ZILLOW_REGION_CACHE) >= 200:
            _ZILLOW_REGION_CACHE.pop(next(iter(_ZILLOW_REGION_CACHE)), None)
        _ZILLOW_REGION_CACHE[cache_key] = (time.monotonic(), dict(result))
    return result

def _zillow_listing_records(payload: object) -> list[dict]:
    """Flatten Zillow search results, including apartment groups."""
    results = payload.get("results") if isinstance(payload, dict) else None
    if not isinstance(results, list):
        results = _mcp_collect_dicts(payload)
    records: list[dict] = []
    for item in results:
        if not isinstance(item, dict):
            continue
        result = item.get("result")
        result = result if isinstance(result, dict) else item
        is_group = str(item.get("type") or "").lower() == "group"
        if is_group and isinstance(result.get("summary"), dict):
            record = dict(result["summary"])
        else:
            record = dict(result)
        record["_zillow_group"] = is_group
        record["_zillow_home_url"] = str(
            result.get("homeDetailsPageUrl") or record.get("homeDetailsPageUrl") or ""
        ).strip()
        records.append(record)
    return records

def _zillow_range_text(
    value: object,
    formatter: Callable[[float], str],
    collapse_wide: bool = False,
) -> str:
    """Format a number or min/max range for display."""
    if isinstance(value, dict):
        minimum = _mcp_number(value.get("min"))
        maximum = _mcp_number(value.get("max"))
        if minimum is not None and maximum is not None:
            if collapse_wide and maximum > minimum * 5:
                return f"From {formatter(minimum)}"
            if maximum == minimum:
                return formatter(minimum)
            return f"{formatter(minimum)}–{formatter(maximum)}"
        if minimum is not None:
            return formatter(minimum)
        return ""
    number = _mcp_number(value)
    return formatter(number) if number is not None else ""

def _zillow_format_address(record: dict) -> str:
    address = record.get("formattedAddress")
    if isinstance(address, dict):
        line1 = " ".join(str(address.get("line1") or "").split())
        line2 = " ".join(str(address.get("line2") or "").split())
        if line1 and line2:
            return f"{line1}, {line2}"
        if line1 or line2:
            return line1 or line2
        city = " ".join(str(address.get("city") or "").split())
        state = " ".join(str(address.get("stateOrProvince") or "").split())
        postal = " ".join(str(address.get("postalCode") or "").split())
        tail = " ".join(part for part in (state, postal) if part)
        return ", ".join(part for part in (city, tail) if part)
    direct = _mcp_field(
        record, ("address", "fullAddress", "formattedAddressString")
    )
    return " ".join(str(direct or "").split())

def _zillow_listing_item(record: dict, rental: bool) -> dict:
    def range_min(value: object) -> float | None:
        if isinstance(value, dict):
            return _mcp_number(value.get("min"))
        return _mcp_number(value)

    price_value = record.get("price")
    if isinstance(price_value, dict):
        price_value = price_value.get("filteredPrice")
    price_number = range_min(price_value)

    def money(value: float) -> str:
        return f"${value:,.0f}" + ("/mo" if rental else "")

    price_text = _zillow_range_text(price_value, money, collapse_wide=True)
    beds_text = _zillow_range_text(
        record.get("bedroomCount"), lambda value: f"{value:g} bd"
    )
    baths_text = _zillow_range_text(
        record.get("bathroomCount"), lambda value: f"{value:g} ba"
    )
    sqft_text = _zillow_range_text(
        record.get("livingAreaSquareFeet"), lambda value: f"{value:,.0f} sqft"
    )
    title = " ".join(str(record.get("title") or "").split())
    home_type = " ".join(str(record.get("homeType") or "").split())
    if not home_type and record.get("_zillow_group"):
        home_type = "Apartment building"
    details = [
        part
        for part in (price_text, beds_text, baths_text, sqft_text, home_type, title)
        if part
    ]
    summary = " · ".join(details)
    address = _zillow_format_address(record)

    geo = record.get("geoRegion")
    geo = geo if isinstance(geo, dict) else {}
    lat_long = geo.get("latLong")
    lat_long = lat_long if isinstance(lat_long, dict) else {}
    latitude = _mcp_number(lat_long.get("latitude"))
    longitude = _mcp_number(lat_long.get("longitude"))

    home_url = str(
        record.get("_zillow_home_url") or record.get("homeDetailsPageUrl") or ""
    ).strip()
    zillow_url = home_url
    if not zillow_url and address:
        zillow_url = (
            "https://www.zillow.com/homes/" + quote_plus(address) + "_rb/"
        )
    return {
        "id": str(record.get("zpid") or record.get("id") or ""),
        "address": address,
        "title": title,
        "price": price_number,
        "price_text": price_text,
        "bedrooms": range_min(record.get("bedroomCount")),
        "bathrooms": range_min(record.get("bathroomCount")),
        "square_footage": range_min(record.get("livingAreaSquareFeet")),
        "property_type": home_type,
        "status": str(
            record.get("homeStatus") or record.get("listingStatus") or ""
        ).strip(),
        "listed_date": str(record.get("listDate") or "").strip(),
        "days_on_market": _mcp_number(
            record.get("daysOnZillow") or record.get("daysOnMarket")
        ),
        "latitude": latitude,
        "longitude": longitude,
        "summary": summary,
        "zillow_url": zillow_url,
    }

def zillow_nearby_listings(
    latitude: float,
    longitude: float,
    radius: float = ZILLOW_MCP_RADIUS_MILES,
    limit: int = ZILLOW_MCP_LIMIT,
) -> dict:
    """Return nearby Zillow for-sale and rental listings for a coordinate."""
    latitude = float(latitude)
    longitude = float(longitude)
    if not (-90 <= latitude <= 90 and -180 <= longitude <= 180):
        raise ValueError("Missing or invalid place coordinates")
    radius = max(0.1, min(float(radius), 25.0))
    limit = max(1, min(int(limit), 20))

    cache_key = (round(latitude, 5), round(longitude, 5), round(radius, 2), limit)
    now = time.monotonic()
    with _ZILLOW_CACHE_LOCK:
        cached = _ZILLOW_CACHE.get(cache_key)
        if cached and now - cached[0] <= ZILLOW_MCP_CACHE_TTL_SECONDS:
            return {
                **cached[1],
                "sale": [dict(item) for item in cached[1].get("sale", [])],
                "rental": [dict(item) for item in cached[1].get("rental", [])],
            }

    region = _zillow_region_for_coordinate(latitude, longitude)
    result = {
        "configured": True,
        "source": "Zillow",
        "region": region["name"],
        "latitude": latitude,
        "longitude": longitude,
        "radius": radius,
        "limit": limit,
        "sale": [],
        "rental": [],
        "sale_error": "",
        "rental_error": "",
    }
    config = _zillow_mcp_config()
    with _RobinhoodMCPClient(
        config,
        timeout=float(config.get("timeout") or ZILLOW_MCP_TIMEOUT),
        label="Zillow",
        error_type=ZillowMCPError,
        setup_help=_zillow_mcp_setup_help,
        allow_no_token=True,
    ) as client:
        tools = client.tools()
        search_tool = _mcp_pick_tool(tools, ("zillow_property_search",))
        if not search_tool:
            available = ", ".join(
                str(tool.get("name"))
                for tool in tools[:20]
                if tool.get("name")
            )
            raise ZillowMCPError(
                "The Zillow MCP server has no property search tool. "
                "Available tools: " + (available or "(none)")
            )
        for key, rental in (("sale", False), ("rental", True)):
            try:
                filters: dict = {
                    "sortSelection": "nearby",
                    "locationForNearbySort": {
                        "latitude": latitude,
                        "longitude": longitude,
                    },
                }
                if rental:
                    filters["homeStatuses"] = ["forRent"]
                else:
                    filters["forSaleListingStatuses"] = ["active"]
                arguments = {
                    "request": {
                        "propertySearchRegionFiltersRequest": {
                            "propertySearchRegions": [
                                {
                                    "name": region["name"],
                                    "regionDisplayName": region[
                                        "regionDisplayName"
                                    ],
                                    "description": region["description"],
                                }
                            ],
                            "userSpecifiedPivotArea": region["pivot"],
                        },
                        "propertyFiltersRequest": filters,
                        "displayLimit": max(limit * 4, 20),
                    }
                }
                tool_result = client.call_tool(
                    "zillow_property_search", arguments
                )
                payload, _raw_text = _mcp_result_payload(
                    tool_result, ZillowMCPError
                )
                records = _zillow_listing_records(payload)
                items: list[dict] = []
                for record in records:
                    item = _zillow_listing_item(record, rental)
                    item_latitude = item.get("latitude")
                    item_longitude = item.get("longitude")
                    if item_latitude is None or item_longitude is None:
                        continue
                    distance = _haversine_miles(
                        latitude,
                        longitude,
                        float(item_latitude),
                        float(item_longitude),
                    )
                    if distance > radius:
                        continue
                    item["distance_miles"] = round(distance, 2)
                    items.append(item)
                items.sort(key=lambda item: item.get("distance_miles", 0.0))
                result[key] = items[:limit]
            except ZillowMCPError as exc:
                result[f"{key}_error"] = str(exc)

    if result["sale"] or result["rental"]:
        with _ZILLOW_CACHE_LOCK:
            _ZILLOW_CACHE[cache_key] = (time.monotonic(), dict(result))
    return result

def zillow_listings_report(
    latitude: float,
    longitude: float,
    radius: float = ZILLOW_MCP_RADIUS_MILES,
    limit: int = ZILLOW_MCP_LIMIT,
) -> str:
    """Render the /zillow report for one coordinate."""
    result = zillow_nearby_listings(latitude, longitude, radius, limit)
    lines = [
        "Zillow · nearby listings",
        f"Location: {result['latitude']:.5f}, {result['longitude']:.5f} "
        f"· radius {result['radius']:.1f} mi",
        f"Region: {result.get('region') or '(unknown)'}",
        "",
    ]
    sections = (
        ("Nearby Properties For Sale", "sale", "sale_error"),
        ("Nearby Property For Rent", "rental", "rental_error"),
    )
    for heading, key, error_key in sections:
        items = result.get(key) or []
        lines.append(f"{heading} ({len(items)})")
        if items:
            for index, item in enumerate(items):
                if index:
                    lines.append("")
                lines.append(
                    "  "
                    + (
                        item.get("summary")
                        or item.get("price_text")
                        or "Listing"
                    )
                )
                if item.get("address"):
                    lines.append("    " + str(item["address"]))
                extra = []
                if item.get("distance_miles") is not None:
                    extra.append(f"{item['distance_miles']:.2f} mi away")
                if item.get("status"):
                    extra.append(str(item["status"]))
                if item.get("listed_date"):
                    extra.append("listed " + str(item["listed_date"]))
                if item.get("days_on_market") is not None:
                    extra.append(f"{item['days_on_market']:g} days on Zillow")
                if extra:
                    lines.append("    " + " · ".join(extra))
        elif result.get(error_key):
            lines.append("  " + str(result[error_key]))
        else:
            lines.append("  No nearby listings found within the radius.")
        lines.append("")
    if not result["sale"] and not result["rental"]:
        errors = [
            str(result.get(key) or "").strip()
            for key in ("sale_error", "rental_error")
            if str(result.get(key) or "").strip()
        ]
        if errors:
            lines.append("Errors: " + " | ".join(dict.fromkeys(errors)))
    lines.append("Source: Zillow MCP")
    return "\n".join(lines)


# Cross-component imports are placed after the definitions so a
# feature module can depend on another feature module safely.
from .map import MAP_USER_AGENT, NOMINATIM_REVERSE_URL
from .robinhood import ZILLOW_MCP_CACHE_TTL_SECONDS, ZILLOW_MCP_REGION_CACHE_TTL_SECONDS, ZILLOW_MCP_TIMEOUT, ZillowMCPError, _RobinhoodMCPClient, _ZILLOW_CACHE, _mcp_collect_dicts, _mcp_field, _mcp_number, _mcp_pick_tool, _mcp_result_payload, _zillow_mcp_config, _zillow_mcp_setup_help
