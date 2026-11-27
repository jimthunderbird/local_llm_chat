"""HTTP routing, file serving, and shared response helpers."""

from __future__ import annotations

from .core import *

class HandlerCoreMixin:
    def __init__(self, *args, **kwargs):
        # BaseHTTPRequestHandler.__init__ processes the request, so this lock
        # must exist before super().__init__() is called. Status annotations
        # may stream from a background worker while the request thread is
        # emitting search logs and result cards.
        self._sse_write_lock = threading.Lock()
        super().__init__(*args, **kwargs)

    # ---- basic helpers ------------------------------------------------
    def log_message(self, fmt, *args):
        # Keep the terminal quiet; errors still surface through SSE.
        if args and str(args[0]).startswith("4"):
            super().log_message(fmt, *args)

    def _send_json(self, payload: dict, status: int = 200) -> None:
        data = json.dumps(payload, ensure_ascii=False).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(data)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        try:
            self.wfile.write(data)
        except (BrokenPipeError, ConnectionResetError):
            pass

    def _send_error_json(self, message: str, status: int = 400) -> None:
        self._send_json({"error": message}, status)

    def _send_redirect(self, location: str, status: int = 302) -> None:
        """Send the browser to a validated external URL without a Google hop."""
        if (
            not re.match(r"^https?://", location, re.IGNORECASE)
            or "\r" in location
            or "\n" in location
        ):
            self._send_error_json("Invalid redirect URL", 400)
            return
        self.send_response(status)
        self.send_header("Location", location)
        self.send_header("Cache-Control", "no-store")
        self.send_header("Content-Length", "0")
        self.end_headers()

    def _send_wav(self, data: bytes) -> None:
        self.send_response(200)
        self.send_header("Content-Type", "audio/wav")
        self.send_header("Content-Length", str(len(data)))
        self.send_header("Cache-Control", "private, max-age=86400")
        self.send_header("X-Content-Type-Options", "nosniff")
        self.end_headers()
        try:
            self.wfile.write(data)
        except (BrokenPipeError, ConnectionResetError):
            pass

    def _read_json(self) -> dict | None:
        try:
            length = int(self.headers.get("Content-Length", "0"))
        except ValueError:
            self._send_error_json("Invalid Content-Length", 400)
            return None
        if length > MAX_REQUEST_BYTES:
            self._send_error_json("Request body too large", 413)
            return None
        raw = self.rfile.read(length) if length else b""
        if not raw:
            return {}
        try:
            data = json.loads(raw.decode("utf-8"))
        except (UnicodeDecodeError, json.JSONDecodeError):
            self._send_error_json("Request body must be valid JSON", 400)
            return None
        return data if isinstance(data, dict) else None

    def _begin_sse(self) -> None:
        self.send_response(200)
        self.send_header("Content-Type", "text/event-stream; charset=utf-8")
        self.send_header("Cache-Control", "no-cache, no-store, must-revalidate")
        self.send_header("X-Accel-Buffering", "no")
        self.send_header("Connection", "close")
        self.end_headers()
        self.close_connection = True

    def _sse(self, event: str, data) -> None:
        payload = json.dumps(data, ensure_ascii=False)
        message = f"event: {event}\ndata: {payload}\n\n".encode("utf-8")
        lock = getattr(self, "_sse_write_lock", None)
        if lock is None:
            lock = threading.Lock()
            self._sse_write_lock = lock
        try:
            with lock:
                self.wfile.write(message)
                self.wfile.flush()
        except (BrokenPipeError, ConnectionResetError, OSError) as exc:
            raise ClientDisconnected() from exc

    def _sse_text(self, event: str, text: str) -> None:
        if text:
            self._sse(event, {"text": text})

    def _sse_answer_source(
        self,
        *,
        language: str = "",
        source: str,
        label: str,
        model: str = "",
        url: str = "",
        detail: str = "",
    ) -> None:
        """Tell the browser where the currently streaming answer comes from."""
        self._sse(
            "source",
            {
                "language": language,
                "source": source,
                "label": label,
                "model": model,
                "url": url,
                "detail": detail,
            },
        )
        self._sse_text("log", f"[answer source: {label}]")

    def _sse_doc_source(self, payload: dict) -> None:
        self._sse("source", payload)
        label = payload.get("label") or payload.get("source") or "unknown"
        self._sse_text("log", f"[answer source: {label}]")

    # ---- GET ----------------------------------------------------------
    def do_OPTIONS(self) -> None:  # noqa: N802 - BaseHTTPRequestHandler API
        self.send_response(204)
        self.send_header("Access-Control-Allow-Origin", "*")
        self.send_header("Access-Control-Allow-Methods", "GET, POST, OPTIONS")
        self.send_header("Access-Control-Allow-Headers", "Content-Type")
        self.end_headers()

    def do_GET(self) -> None:  # noqa: N802 - BaseHTTPRequestHandler API
        parsed = urlparse(self.path)
        path = parsed.path
        if path in {"/", "/index.html", "/chat.html", "/app/chat.html"}:
            self._serve_chat_html()
            return
        if path == "/favicon.ico":
            self.send_response(204)
            self.end_headers()
            return
        if path == "/api/state":
            params = parse_qs(parsed.query)
            with self.state.lock:
                if (params.get("initial") or [""])[0] == "1":
                    self.state.default_to_online_mode()
                self._send_json(self.state.snapshot(include_history=True))
            return
        if path == "/api/english_words":
            params = parse_qs(parsed.query)
            spec = " ".join((params.get("range") or [""])[0].split())
            if not spec:
                spec = self.state.english_words_to_learn
            try:
                words, label = english_corner_vocabulary(spec)
            except (ValueError, RuntimeError, OSError) as exc:
                self._send_error_json(str(exc), 400)
                return
            self._send_json(
                {
                    "range": spec,
                    "label": label,
                    "count": len(words),
                    "words": list(words),
                }
            )
            return
        if path == "/api/video_learn_status":
            params = parse_qs(parsed.query)
            video_id = (params.get("id") or [""])[0].strip()
            if video_id and not re.fullmatch(r"[A-Za-z0-9_-]{11}", video_id):
                self._send_error_json("Invalid YouTube video id", 400)
                return
            if video_id:
                job = _video_learn_jobs_snapshot(self.state, video_id)
                if job is not None:
                    self._send_json(job)
                    return
                # Match the disk-backed readiness rule used by
                # get_video_chat_state. A transcript-only video is also
                # chat-ready because the chat request rebuilds its trunks.
                learned = video_is_chat_ready(video_id)
                self._send_json(
                    {
                        "id": video_id,
                        "status": "learned" if learned else "idle",
                        "progress": 100.0 if learned else 0.0,
                        "learned": learned,
                    }
                )
                return
            self._send_json(
                {"jobs": _video_learn_jobs_snapshot(self.state) or []}
            )
            return
        if path == "/api/news_status":
            params = parse_qs(parsed.query)
            news_id = (params.get("id") or [""])[0].strip()
            if not NEWS_ID_PATTERN.fullmatch(news_id):
                self._send_error_json("Missing or invalid news story id", 400)
                return
            self._send_json(
                {
                    "id": news_id,
                    "learned": news_is_learned(news_id),
                    "title": news_title(news_id),
                }
            )
            return
        if path == "/api/news_item":
            params = parse_qs(parsed.query)
            news_id = (params.get("id") or [""])[0].strip()
            if not NEWS_ID_PATTERN.fullmatch(news_id):
                self._send_error_json("Missing or invalid news story id", 400)
                return
            meta = _read_news_meta(news_id)
            if not meta:
                self._send_error_json("News story not found", 404)
                return
            item = dict(meta)
            item["id"] = news_id
            item["news_id"] = news_id
            item["learned"] = news_is_learned(news_id)
            item["complete"] = True
            self._send_json(item)
            return
        if path == "/api/news_open":
            params = parse_qs(parsed.query)
            url = (params.get("url") or [""])[0].strip()
            if not re.match(r"^https?://", url, re.IGNORECASE):
                self._send_error_json("Missing or invalid article URL", 400)
                return
            target = url
            if GOOGLE_NEWS_ARTICLE_RE.match(url):
                target = resolve_google_news_url(url)
                if not target:
                    self._send_error_json(
                        "Could not resolve the Google News link", 502
                    )
                    return
            self._send_redirect(target)
            return
        if path == "/api/news_article":
            params = parse_qs(parsed.query)
            url = (params.get("url") or [""])[0].strip()
            title = (params.get("title") or [""])[0].strip()
            try:
                article = fetch_news_article(url, title)
            except ValueError as exc:
                self._send_error_json(str(exc), 400)
                return
            except (OSError, subprocess.SubprocessError, RuntimeError) as exc:
                self._send_error_json(str(exc), 502)
                return
            self._send_json(article)
            return
        if path == "/api/uhc_provider":
            params = parse_qs(parsed.query)
            practitioner_id = (
                params.get("practitioner_id") or [""]
            )[0].strip()
            role_id = (params.get("role_id") or [""])[0].strip()
            organization_id = (
                params.get("organization_id") or [""]
            )[0].strip()
            npi = (params.get("npi") or [""])[0].strip()
            name = (params.get("title") or [""])[0].strip()
            url = (params.get("url") or [""])[0].strip()
            if not practitioner_id and url:
                match = re.search(r"/Practitioner/([^/?#]+)", url)
                if match:
                    practitioner_id = match.group(1)
            if not practitioner_id and (npi or name):
                practitioner = _uhc_find_practitioner(
                    {"npi": npi, "doctor_name": name}
                )
                if practitioner is not None:
                    practitioner_id = str(
                        practitioner.get("id") or ""
                    )
            if not practitioner_id:
                self._send_error_json(
                    "Missing or invalid UnitedHealthcare practitioner id",
                    400,
                )
                return
            roles_bundle = _uhc_roles_for_practitioner(practitioner_id)
            by_type, by_id = _uhc_bundle_index(roles_bundle)
            practitioner = by_id.get(("Practitioner", practitioner_id))
            if not practitioner and (npi or name):
                practitioner = _uhc_find_practitioner(
                    {"npi": npi, "doctor_name": name}
                )
            if not practitioner and name:
                practitioner = {
                    "resourceType": "Practitioner",
                    "id": practitioner_id,
                    "name": [{"text": name}],
                }
            if not practitioner:
                self._send_error_json(
                    "UnitedHealthcare practitioner not found", 404
                )
                return
            item = _uhc_detail_item(
                practitioner,
                roles_bundle,
                role_id=role_id,
                organization_id=organization_id,
            )
            self._send_json(item)
            return
        if path == "/api/anthem_provider":
            params = parse_qs(parsed.query)
            npi = (params.get("npi") or [""])[0].strip()
            url = (params.get("url") or [""])[0].strip()
            name = (params.get("title") or [""])[0].strip()
            specialty = (params.get("specialty") or [""])[0].strip()
            institution = (params.get("institution") or [""])[0].strip()
            summary = (params.get("summary") or [""])[0].strip()
            network_labels = (
                params.get("network_labels") or [""]
            )[0].strip()
            anthem_provider_id = (
                params.get("anthem_provider_id") or [""]
            )[0].strip()
            anthem_address_id = (
                params.get("anthem_address_id") or [""]
            )[0].strip()
            if not npi:
                match = re.search(
                    r"/provider-view/(\d{10})", url
                )
                if match:
                    npi = match.group(1)
            if not specialty and url:
                url_params = parse_qs(urlparse(url).query)
                specialty = (url_params.get("specialty") or [""])[0]
                specialty = specialty.strip()
            if not institution and url:
                url_params = parse_qs(urlparse(url).query)
                institution = (url_params.get("institution") or [""])[0]
                institution = institution.strip()
            if not anthem_provider_id and url:
                url_params = parse_qs(urlparse(url).query)
                anthem_provider_id = (
                    url_params.get("anthem_provider_id") or [""]
                )[0].strip()
                anthem_address_id = (
                    url_params.get("anthem_address_id") or [""]
                )[0].strip()
            if not (npi or name or anthem_provider_id):
                self._send_error_json(
                    "Missing provider NPI, name, or Anthem provider id",
                    400,
                )
                return
            try:
                item = _anthem_provider_detail(
                    npi,
                    name,
                    specialty=specialty,
                    institution=institution,
                    summary=summary,
                    network_labels=network_labels,
                    anthem_provider_id=anthem_provider_id,
                    anthem_address_id=anthem_address_id,
                )
            except Exception as exc:
                self._send_error_json(
                    f"Could not load provider detail: {exc}", 502
                )
                return
            self._send_json(item)
            return
        if path == "/api/reddit_article":
            params = parse_qs(parsed.query)
            url = (params.get("url") or [""])[0].strip()
            title = (params.get("title") or [""])[0].strip()
            try:
                article = fetch_reddit_post(url, title)
            except ValueError as exc:
                self._send_error_json(str(exc), 400)
                return
            except RuntimeError as exc:
                # Even when every server-side extraction endpoint is blocked,
                # Reddit's embed page may still render in the browser. Return
                # its URL so the modal can fall back to an iframe.
                payload: dict = {"error": str(exc)}
                iframe_url = _reddit_iframe_url(url)
                if iframe_url:
                    payload["iframe_url"] = iframe_url
                self._send_json(payload, 502)
                return
            self._send_json(article)
            return
        if path == "/api/stock_reddit":
            params = parse_qs(parsed.query)
            symbol = (params.get("symbol") or [""])[0].strip().upper()
            if not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9._-]{0,19}", symbol):
                self._send_error_json("Missing or invalid stock symbol", 400)
                return
            try:
                items = fetch_stock_reddit_items(symbol)
            except RuntimeError as exc:
                self._send_error_json(str(exc), 502)
                return
            self._send_json({"symbol": symbol, "items": items})
            return
        if path == "/api/game_geocode":
            params = parse_qs(parsed.query)
            requested = str((params.get("game") or [""])[0]).strip()
            normalized = re.sub(
                r"[^a-z0-9]+", "_", requested.lower()
            ).strip("_")
            compact = normalized.replace("_", "")
            game = next(
                (
                    candidate
                    for candidate in list_game_prompts()
                    if candidate["name"] == normalized
                    or candidate["name"].replace("_", "") == compact
                ),
                None,
            )
            if game is None:
                self._send_error_json("Unknown game", 400)
                return
            query = " ".join((params.get("q") or [""])[0].split())
            if len(query) < 2:
                self._send_json({"game": game["name"], "items": []})
                return
            raw_limit = (params.get("limit") or [""])[0]
            try:
                limit = int(float(raw_limit)) if raw_limit else MAP_GEOCODE_LIMIT
            except (TypeError, ValueError):
                limit = MAP_GEOCODE_LIMIT
            try:
                items = search_game_geocode_suggestions(game, query, limit)
            except RuntimeError as exc:
                self._send_error_json(str(exc), 502)
                return
            self._send_json({"game": game["name"], "items": items})
            return
        if path == "/api/map_geocode":
            params = parse_qs(parsed.query)
            query = " ".join((params.get("q") or [""])[0].split())
            if len(query) < 2:
                self._send_json({"items": []})
                return
            raw_limit = (params.get("limit") or [""])[0]
            try:
                limit = int(float(raw_limit)) if raw_limit else MAP_GEOCODE_LIMIT
            except (TypeError, ValueError):
                limit = MAP_GEOCODE_LIMIT
            raw_bounded = (params.get("bounded") or [""])[0].strip().lower()
            bounded = raw_bounded in {"1", "true", "yes", "on"}
            viewbox = (params.get("viewbox") or [""])[0]
            countrycodes = (params.get("countrycodes") or [""])[0]
            try:
                items = geocode_locations(
                    query,
                    limit,
                    viewbox=viewbox,
                    bounded=bounded,
                    countrycodes=countrycodes,
                )
            except RuntimeError as exc:
                self._send_error_json(str(exc), 502)
                return
            self._send_json({"items": items})
            return
        if path == "/api/map_restaurants":
            params = parse_qs(parsed.query)
            try:
                latitude = float((params.get("lat") or [""])[0])
                longitude = float((params.get("lon") or [""])[0])
            except (TypeError, ValueError):
                self._send_error_json("Missing or invalid map coordinates", 400)
                return
            if not (-90 <= latitude <= 90 and -180 <= longitude <= 180):
                self._send_error_json("Missing or invalid map coordinates", 400)
                return
            raw_radius = (params.get("radius") or [""])[0]
            try:
                radius = (
                    int(float(raw_radius))
                    if raw_radius
                    else MAP_RESTAURANT_RADIUS_METERS
                )
            except (TypeError, ValueError):
                radius = MAP_RESTAURANT_RADIUS_METERS
            try:
                items = search_nearby_restaurants(
                    latitude, longitude, radius
                )
            except RuntimeError as exc:
                self._send_error_json(str(exc), 502)
                return
            self._send_json({"items": items})
            return
        if path == "/api/game_place_properties":
            params = parse_qs(parsed.query)
            try:
                latitude = float((params.get("lat") or [""])[0])
                longitude = float((params.get("lon") or [""])[0])
            except (TypeError, ValueError):
                self._send_error_json(
                    "Missing or invalid place coordinates", 400
                )
                return
            if not (-90 <= latitude <= 90 and -180 <= longitude <= 180):
                self._send_error_json(
                    "Missing or invalid place coordinates", 400
                )
                return
            raw_radius = (params.get("radius") or [""])[0]
            try:
                radius = (
                    float(raw_radius)
                    if raw_radius
                    else ZILLOW_MCP_RADIUS_MILES
                )
            except (TypeError, ValueError):
                radius = ZILLOW_MCP_RADIUS_MILES
            radius = max(0.1, min(radius, 25.0))
            raw_limit = (params.get("limit") or [""])[0]
            try:
                limit = (
                    int(float(raw_limit)) if raw_limit else ZILLOW_MCP_LIMIT
                )
            except (TypeError, ValueError):
                limit = ZILLOW_MCP_LIMIT
            limit = max(1, min(limit, 20))
            try:
                result = zillow_nearby_listings(
                    latitude, longitude, radius, limit
                )
            except ValueError as exc:
                self._send_error_json(str(exc), 400)
                return
            except ZillowMCPError as exc:
                message = str(exc)
                self._send_json(
                    {
                        "configured": True,
                        "source": "Zillow",
                        "latitude": latitude,
                        "longitude": longitude,
                        "radius": radius,
                        "limit": limit,
                        "sale": [],
                        "rental": [],
                        "sale_error": message,
                        "rental_error": message,
                        "error": message,
                    }
                )
                return
            self._send_json(result)
            return
        if path == "/api/game_nearby":
            params = parse_qs(parsed.query)
            try:
                latitude = float((params.get("lat") or [""])[0])
                longitude = float((params.get("lon") or [""])[0])
            except (TypeError, ValueError):
                self._send_error_json("Missing or invalid game coordinates", 400)
                return
            if not (-90 <= latitude <= 90 and -180 <= longitude <= 180):
                self._send_error_json("Missing or invalid game coordinates", 400)
                return
            raw_radius = (params.get("radius") or [""])[0]
            raw_limit = (params.get("limit") or [""])[0]
            try:
                radius = (
                    int(float(raw_radius))
                    if raw_radius
                    else GAME_NEARBY_RADIUS_METERS
                )
            except (TypeError, ValueError):
                radius = GAME_NEARBY_RADIUS_METERS
            try:
                limit = (
                    int(float(raw_limit))
                    if raw_limit
                    else GAME_NEARBY_LIMIT
                )
            except (TypeError, ValueError):
                limit = GAME_NEARBY_LIMIT
            restaurant_radius = min(radius, 5_000)
            cache_key = _game_nearby_cache_key(
                latitude,
                longitude,
                radius,
                limit,
                GAME_NEARBY_RESTAURANT_LIMIT,
            )
            cached = cached_game_nearby(cache_key)
            if cached is not None:
                cached["cached"] = True
                self._send_json(cached)
                return
            # Run places and restaurants independently so one Overpass failure
            # does not hide the other result group.
            places_failed = False
            restaurants_failed = False
            with ThreadPoolExecutor(max_workers=2) as executor:
                places_future = executor.submit(
                    search_game_places, latitude, longitude, radius, limit
                )
                restaurants_future = executor.submit(
                    search_nearby_restaurants,
                    latitude,
                    longitude,
                    restaurant_radius,
                    GAME_NEARBY_RESTAURANT_LIMIT,
                )
                try:
                    items = places_future.result()
                except Exception:
                    items = []
                    places_failed = True
                try:
                    restaurants = restaurants_future.result()
                except Exception:
                    restaurants = []
                    restaurants_failed = True
            if places_failed:
                try:
                    items = search_game_places(
                        latitude, longitude, radius, limit
                    )
                    places_failed = False
                except Exception:
                    items = []
            if restaurants_failed:
                try:
                    restaurants = search_nearby_restaurants(
                        latitude,
                        longitude,
                        restaurant_radius,
                        GAME_NEARBY_RESTAURANT_LIMIT,
                    )
                    restaurants_failed = False
                except Exception:
                    restaurants = []
            payload = {"items": items, "restaurants": restaurants}
            if not places_failed and not restaurants_failed:
                store_game_nearby(cache_key, payload)
            elif places_failed and restaurants_failed:
                self._send_error_json(
                    "OpenStreetMap nearby search is temporarily unavailable; "
                    "please try again in a moment.",
                    502,
                )
                return
            elif places_failed:
                payload["places_unavailable"] = True
            elif restaurants_failed:
                payload["restaurants_unavailable"] = True
            self._send_json(payload)
            return
        if path == "/api/game_place_news":
            params = parse_qs(parsed.query)
            try:
                latitude = float((params.get("lat") or [""])[0])
                longitude = float((params.get("lon") or [""])[0])
            except (TypeError, ValueError):
                self._send_error_json(
                    "Missing or invalid place coordinates", 400
                )
                return
            if not (-90 <= latitude <= 90 and -180 <= longitude <= 180):
                self._send_error_json(
                    "Missing or invalid place coordinates", 400
                )
                return
            raw_limit = (params.get("limit") or [""])[0]
            try:
                limit = int(float(raw_limit)) if raw_limit else GAME_NEWS_LIMIT
            except (TypeError, ValueError):
                limit = GAME_NEWS_LIMIT
            try:
                result = search_game_place_news(latitude, longitude, limit)
            except ValueError as exc:
                self._send_error_json(str(exc), 400)
                return
            except RuntimeError as exc:
                self._send_error_json(str(exc), 502)
                return
            self._send_json(result)
            return
        if path == "/api/game_place":
            params = parse_qs(parsed.query)
            osm_type = (params.get("type") or [""])[0].strip()
            osm_id = (params.get("id") or [""])[0].strip()
            name = (
                params.get("name")
                or params.get("title")
                or [""]
            )[0].strip()
            link = (params.get("url") or params.get("link") or [""])[0].strip()
            raw_latitude = (params.get("lat") or [""])[0].strip()
            raw_longitude = (params.get("lon") or [""])[0].strip()
            latitude = None
            longitude = None
            if raw_latitude or raw_longitude:
                try:
                    latitude = float(raw_latitude)
                    longitude = float(raw_longitude)
                except (TypeError, ValueError):
                    self._send_error_json(
                        "Missing or invalid game place coordinates", 400
                    )
                    return
                if not (
                    -90 <= latitude <= 90 and -180 <= longitude <= 180
                ):
                    self._send_error_json(
                        "Missing or invalid game place coordinates", 400
                    )
                    return
            if not (osm_type and osm_id) and not name and not link and latitude is None:
                self._send_error_json(
                    "Missing game place identity", 400
                )
                return
            try:
                details = fetch_game_place_details(
                    osm_type=osm_type,
                    osm_id=osm_id,
                    name=name,
                    latitude=latitude,
                    longitude=longitude,
                    link=link,
                )
            except ValueError as exc:
                self._send_error_json(str(exc), 400)
                return
            except RuntimeError as exc:
                self._send_error_json(str(exc), 502)
                return
            self._send_json(details)
            return
        if path == "/api/driving_instructions":
            params = parse_qs(parsed.query)
            try:
                from_latitude = float((params.get("from_lat") or [""])[0])
                from_longitude = float((params.get("from_lon") or [""])[0])
                to_latitude = float((params.get("to_lat") or [""])[0])
                to_longitude = float((params.get("to_lon") or [""])[0])
            except (TypeError, ValueError):
                self._send_error_json(
                    "Missing or invalid driving-route coordinates", 400
                )
                return
            try:
                result = fetch_driving_instructions(
                    from_latitude,
                    from_longitude,
                    to_latitude,
                    to_longitude,
                )
            except ValueError as exc:
                self._send_error_json(str(exc), 400)
                return
            except RuntimeError as exc:
                self._send_error_json(str(exc), 502)
                return
            self._send_json(result)
            return
        if path == "/api/osm_restaurant":
            params = parse_qs(parsed.query)
            osm_type = (params.get("type") or [""])[0].strip().lower()
            osm_id = (params.get("id") or [""])[0].strip()
            if osm_type not in OSM_RESTAURANT_TYPES or not osm_id.isdigit():
                self._send_error_json("Missing or invalid OSM identity", 400)
                return
            try:
                result = fetch_osm_restaurant(osm_type, osm_id)
            except ValueError as exc:
                self._send_error_json(str(exc), 400)
                return
            except RuntimeError as exc:
                self._send_error_json(str(exc), 502)
                return
            self._send_json(result)
            return
        if path == "/api/yelp_restaurant":
            params = parse_qs(parsed.query)
            name = " ".join((params.get("name") or [""])[0].split())
            if not name:
                self._send_error_json("Missing restaurant name", 400)
                return
            try:
                latitude = float((params.get("lat") or [""])[0])
                longitude = float((params.get("lon") or [""])[0])
            except (TypeError, ValueError):
                self._send_error_json("Missing or invalid map coordinates", 400)
                return
            if not (-90 <= latitude <= 90 and -180 <= longitude <= 180):
                self._send_error_json("Missing or invalid map coordinates", 400)
                return
            if not yelp_api_key():
                self._send_error_json(
                    "Yelp API key is not configured. Set YELP_API_KEY or "
                    "add ~/.yelp_api_key, then restart the server.",
                    503,
                )
                return
            try:
                result = fetch_yelp_restaurant(latitude, longitude, name)
            except ValueError as exc:
                self._send_error_json(str(exc), 400)
                return
            except RuntimeError as exc:
                self._send_error_json(str(exc), 502)
                return
            self._send_json(result)
            return
        if path == "/api/stock_summary":
            params = parse_qs(parsed.query)
            symbol = (params.get("symbol") or [""])[0].strip().upper()
            if not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9._-]{0,19}", symbol):
                self._send_error_json("Missing or invalid stock symbol", 400)
                return
            try:
                summary = fetch_stock_company_summary(symbol)
            except RuntimeError as exc:
                self._send_error_json(str(exc), 502)
                return
            self._send_json(summary)
            return
        if path == "/api/stackoverflow_answers":
            params = parse_qs(parsed.query)
            question_id = (params.get("id") or [""])[0].strip()
            try:
                answers = fetch_stackoverflow_answers(question_id)
            except ValueError as exc:
                self._send_error_json(str(exc), 400)
                return
            except RuntimeError as exc:
                self._send_error_json(str(exc), 502)
                return
            self._send_json(answers)
            return
        if path == "/api/wikivoyage_article":
            params = parse_qs(parsed.query)
            url = (params.get("url") or [""])[0].strip()
            title = (params.get("title") or [""])[0].strip()
            try:
                article = fetch_wikivoyage_article(url, title)
            except ValueError as exc:
                self._send_error_json(str(exc), 400)
                return
            except RuntimeError as exc:
                self._send_error_json(str(exc), 502)
                return
            self._send_json(article)
            return
        if path == "/api/autocomplete":
            params = parse_qs(parsed.query)
            kind = (params.get("kind") or ["commands"])[0]
            query = (params.get("q") or [""])[0]
            self._send_json({"items": self._autocomplete(kind, query)})
            return
        if path == "/api/changelog_file":
            params = parse_qs(parsed.query)
            name = (params.get("name") or [""])[0]
            self._send_changelog_file(name)
            return
        if path == "/api/book_file":
            params = parse_qs(parsed.query)
            name = (params.get("name") or [""])[0]
            directory = (params.get("dir") or [""])[0]
            self._send_written_book(name, directory)
            return
        if path == "/api/pdf_book_file":
            params = parse_qs(parsed.query)
            name = (params.get("name") or [""])[0]
            self._send_pdf_book(name)
            return
        if path == "/api/pdf_book_page":
            params = parse_qs(parsed.query)
            name = (params.get("name") or [""])[0]
            try:
                page = int((params.get("page") or ["1"])[0])
            except (TypeError, ValueError):
                self._send_error_json("Missing or invalid PDF page", 400)
                return
            try:
                payload = read_pdf_book_page(name, page)
            except ValueError as exc:
                self._send_error_json(str(exc), 404)
                return
            except (OSError, RuntimeError) as exc:
                self._send_error_json(
                    f"Could not read the PDF book: {exc}", 502
                )
                return
            self._send_json(payload)
            return
        if path == "/api/pdf_status":
            params = parse_qs(parsed.query)
            name = (params.get("name") or [""])[0]
            try:
                self._send_json(pdf_book_status(name))
            except ValueError as exc:
                self._send_error_json(str(exc), 404)
            except (OSError, RuntimeError) as exc:
                self._send_error_json(
                    f"Could not read the PDF book status: {exc}", 502
                )
            return
        if path == "/api/pdf_chat_history":
            params = parse_qs(parsed.query)
            name = (params.get("name") or [""])[0]
            try:
                self._send_pdf_chat_history(name)
            except ValueError as exc:
                self._send_error_json(str(exc), 404)
            return
        if path == "/api/pdf_book_page_image":
            params = parse_qs(parsed.query)
            name = (params.get("name") or [""])[0]
            try:
                page = int((params.get("page") or ["1"])[0])
            except (TypeError, ValueError):
                self._send_error_json("Missing or invalid PDF page", 400)
                return
            self._send_pdf_page_image(name, page)
            return
        if path == "/api/pdf_concept":
            params = parse_qs(parsed.query)
            name = (params.get("name") or [""])[0]
            node_id = (params.get("id") or [""])[0]
            try:
                payload = read_pdf_concept(name, node_id)
            except ValueError as exc:
                self._send_error_json(str(exc), 404)
                return
            except (OSError, RuntimeError) as exc:
                self._send_error_json(
                    f"Could not read the concept: {exc}", 502
                )
                return
            self._send_json(payload)
            return
        if path == "/api/pdf_knowledge_graph":
            params = parse_qs(parsed.query)
            name = (params.get("name") or [""])[0]
            graph_path = pdf_knowledge_graph_path(name)
            if graph_path is None or not graph_path.is_file():
                self._send_error_json("Knowledge graph not built", 404)
                return
            try:
                payload = json.loads(
                    graph_path.read_text(encoding="utf-8")
                )
            except (OSError, json.JSONDecodeError) as exc:
                self._send_error_json(
                    f"Cannot read the knowledge graph: {exc}", 500
                )
                return
            self._send_json(payload)
            return
        if path == "/api/wiki":
            params = parse_qs(parsed.query)
            word = (params.get("word") or [""])[0].strip()
            self._wiki_json(word)
            return
        if path == "/api/transcripts":
            params = parse_qs(parsed.query)
            keyword = (params.get("q") or [""])[0].strip()
            self._transcript_search_json(keyword)
            return
        if path == "/api/gutenberg_book":
            params = parse_qs(parsed.query)
            book_id = (params.get("id") or [""])[0].strip()
            text_url = (params.get("url") or [""])[0].strip()
            title = (params.get("title") or [""])[0].strip()
            self._gutenberg_book_json(book_id, text_url, title)
            return
        self._send_error_json("Not found", 404)

    def _serve_chat_html(self) -> None:
        try:
            data = CHAT_HTML.read_bytes()
        except OSError as exc:
            self._send_error_json(f"Cannot read {CHAT_HTML}: {exc}", 500)
            return
        self.send_response(200)
        self.send_header("Content-Type", "text/html; charset=utf-8")
        self.send_header("Content-Length", str(len(data)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        try:
            self.wfile.write(data)
        except (BrokenPipeError, ConnectionResetError):
            pass

    def _send_changelog_file(self, name: str) -> None:
        """Serve one completed-task summary page to the changelog modal."""
        path = changelog_file_path(name)
        if path is None:
            self._send_error_json("Changelog entry not found", 404)
            return
        try:
            data = path.read_bytes()
        except OSError as exc:
            self._send_error_json(f"Cannot read changelog entry: {exc}", 500)
            return
        self.send_response(200)
        self.send_header("Content-Type", "text/html; charset=utf-8")
        self.send_header("Content-Length", str(len(data)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        try:
            self.wfile.write(data)
        except (BrokenPipeError, ConnectionResetError):
            pass

    def _send_written_book(
        self, name: str, directory: str | None = None
    ) -> None:
        """Serve one HTML book written by /write_a_book."""
        path = written_book_path(name, directory)
        if path is None or not path.is_file():
            self._send_error_json("Written book not found", 404)
            return
        try:
            data = path.read_bytes()
        except OSError as exc:
            self._send_error_json(f"Cannot read written book: {exc}", 500)
            return
        self.send_response(200)
        self.send_header("Content-Type", "text/html; charset=utf-8")
        self.send_header("Content-Length", str(len(data)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        try:
            self.wfile.write(data)
        except (BrokenPipeError, ConnectionResetError):
            pass

    def _send_pdf_book(self, name: str) -> None:
        """Serve one PDF book from .chat/pdf_books."""
        path = pdf_book_path(name)
        if path is None or not path.is_file():
            self._send_error_json("PDF book not found", 404)
            return
        try:
            data = path.read_bytes()
        except OSError as exc:
            self._send_error_json(f"Cannot read PDF book: {exc}", 500)
            return
        self.send_response(200)
        self.send_header("Content-Type", "application/pdf")
        self.send_header("Content-Length", str(len(data)))
        self.send_header("Cache-Control", "no-store")
        self.send_header(
            "Content-Disposition",
            f"inline; filename*=UTF-8''{quote(path.name)}",
        )
        self.end_headers()
        try:
            self.wfile.write(data)
        except (BrokenPipeError, ConnectionResetError):
            pass

    def _send_pdf_page_image(self, name: str, page: int) -> None:
        """Render and serve one PDF page as a PNG image."""
        try:
            path = render_pdf_book_page(name, page)
        except ValueError as exc:
            self._send_error_json(str(exc), 404)
            return
        except (OSError, RuntimeError) as exc:
            self._send_error_json(
                f"Could not render the PDF page: {exc}", 502
            )
            return
        try:
            data = path.read_bytes()
        except OSError as exc:
            self._send_error_json(
                f"Cannot read the rendered PDF page: {exc}", 500
            )
            return
        self.send_response(200)
        self.send_header("Content-Type", "image/png")
        self.send_header("Content-Length", str(len(data)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        try:
            self.wfile.write(data)
        except (BrokenPipeError, ConnectionResetError):
            pass

    def _handle_pdf_knowledge_graph(
        self, name: str, rebuild: bool = False
    ) -> None:
        """Stream knowledge-graph build progress and the finished graph."""
        self._begin_sse()
        self._sse("kg_start", {"name": name})

        def progress(percent: int, message: str) -> None:
            self._sse(
                "kg_progress",
                {"progress": percent, "message": message},
            )

        try:
            graph = build_pdf_knowledge_graph(
                name, progress, rebuild=rebuild
            )
        except ValueError as exc:
            self._sse("kg_error", {"message": str(exc)})
            return
        except ClientDisconnected:
            return
        except (OSError, RuntimeError) as exc:
            self._sse(
                "kg_error",
                {"message": f"Could not build the knowledge graph: {exc}"},
            )
            return
        self._sse("kg_done", {"graph": graph})

    def _autocomplete(self, kind: str, query: str) -> list[dict]:
        query = query.strip()
        if kind == "commands":
            items = []
            for name, description in sorted(WEB_COMMANDS.items()):
                if name.startswith(query or "/"):
                    items.append({"value": name, "description": description})
            return items

        if kind == "games":
            lowered = query.lower()
            return [
                {
                    "value": game["name"],
                    "description": game["title"],
                }
                for game in list_game_prompts()
                if game["name"].startswith(lowered)
            ]

        if kind == "youtube_subcommands":
            lowered = query.lower()
            return [
                {"value": name, "description": description}
                for name, description in YOUTUBE_SUBCOMMANDS.items()
                if name.startswith(lowered)
            ]

        if kind == "gutenberg_subcommands":
            lowered = query.lower()
            return [
                {"value": name, "description": description}
                for name, description in GUTENBERG_SUBCOMMANDS.items()
                if name.startswith(lowered)
            ]

        if kind == "openlibrary_subcommands":
            lowered = query.lower()
            return [
                {"value": name, "description": description}
                for name, description in OPENLIBRARY_SUBCOMMANDS.items()
                if name.startswith(lowered)
            ]

        if kind == "stackoverflow_subcommands":
            lowered = query.lower()
            return [
                {"value": name, "description": description}
                for name, description in STACKOVERFLOW_SUBCOMMANDS.items()
                if name.startswith(lowered)
            ]

        if kind == "youtube_bookmark_actions":
            lowered = query.lower()
            actions = {
                "list": "list bookmarked videos, latest first",
            }
            return [
                {"value": name, "description": description}
                for name, description in actions.items()
                if name.startswith(lowered)
            ]

        if kind in {"news_sources", "news_subcommands"}:
            lowered = query.lower()
            items = []
            if kind == "news_subcommands" and "search".startswith(lowered):
                items.append(
                    {
                        "value": "search",
                        "description": "search Google News by keywords",
                    }
                )
            items.extend(
                {"value": name, "description": label}
                for name, label in sorted(NEWS_SOURCES.items())
                if name.startswith(lowered)
            )
            return items

        if kind == "paths":
            return self._path_completions(query)

        if kind in {"transcripts", "transcript_titles"}:
            if kind == "transcript_titles" and query:
                matches = chat.search_transcript_titles(query)
                available = set(chat.available_video_ids())
                return [
                    {
                        "value": match.video_id,
                        "description": match.title,
                    }
                    for match in matches
                    if match.video_id in available
                ][:30]

            available = chat.available_video_ids()
            lowered = query.lower()
            items = []
            for video_id in available:
                title = chat.video_title(video_id)
                if (
                    not lowered
                    or lowered in video_id.lower()
                    or lowered in title.lower()
                ):
                    items.append(
                        {"value": video_id, "description": title or video_id}
                    )
                    if len(items) >= 30:
                        break
            return items

        return []

    def _path_completions(self, query: str) -> list[dict]:
        """Complete a local filesystem path for commands such as /article.

        The typed text is treated the way a shell would read it: the part up
        to the last slash selects the directory to list and the remainder is
        the name prefix.  ``~`` is expanded for reading the directory while
        the returned value keeps the user's spelling, so completing ``~/do``
        yields ``~/documents/`` instead of an absolute path.
        """
        raw = query or ""
        if raw == "~":
            prefix, tail = "~/", ""
        elif "/" in raw:
            head, tail = raw.rsplit("/", 1)
            prefix = head + "/"
        else:
            prefix, tail = "", raw

        if prefix:
            directory = Path(os.path.expanduser(prefix))
        else:
            directory = Path.cwd()
        if not directory.is_absolute():
            directory = Path.cwd() / directory

        # Hidden entries only appear once the user starts typing a dot.
        show_hidden = tail.startswith(".")
        try:
            entries = list(directory.iterdir())
        except OSError:
            return []

        directories: list[dict] = []
        files: list[dict] = []
        for entry in entries:
            name = entry.name
            if not name.startswith(tail):
                continue
            if name.startswith(".") and not show_hidden:
                continue
            try:
                is_dir = entry.is_dir()
            except OSError:
                is_dir = False
            value = prefix + name + ("/" if is_dir else "")
            if is_dir:
                directories.append({"value": value, "description": "dir"})
            else:
                try:
                    size = entry.stat().st_size
                    description = f"{size:,} bytes"
                except OSError:
                    description = "file"
                files.append({"value": value, "description": description})

        def sort_key(item: dict) -> tuple[str, str]:
            return (item["value"].rsplit("/", 1)[-1].lower(), item["value"])

        directories.sort(key=sort_key)
        files.sort(key=sort_key)
        return (directories + files)[:PATH_COMPLETION_LIMIT]

    def _wiki_json(self, word: str) -> None:
        if not word:
            self._send_error_json("Usage: /wiki <word>", 400)
            return
        url = chat.WIKI_SUMMARY_URL.format(word=quote(word, safe=""))
        try:
            response = requests.get(
                url, headers=chat.HTTP_HEADERS, timeout=(10, 30)
            )
        except requests.RequestException as exc:
            self._send_error_json(f"Wikipedia lookup failed: {exc}", 502)
            return
        if response.status_code == 404:
            self._send_error_json(f"Wikipedia: no page found for {word!r}.", 404)
            return
        try:
            response.raise_for_status()
            data = response.json()
        except (requests.RequestException, ValueError) as exc:
            self._send_error_json(f"Wikipedia lookup failed: {exc}", 502)
            return
        if not isinstance(data, dict):
            self._send_error_json("Wikipedia returned an unexpected response.", 502)
            return
        page = ((data.get("content_urls") or {}).get("desktop") or {}).get(
            "page"
        )
        self._send_json(
            {
                "word": word,
                "title": data.get("title") or word,
                "extract": data.get("extract") or data.get("description") or "",
                "source": page or "",
            }
        )

    def _transcript_search_json(self, keyword: str) -> None:
        if not keyword:
            self._send_error_json("Usage: /youtube transcript <keyword>", 400)
            return
        matches = chat.search_transcript_titles(keyword)
        available = set(chat.available_video_ids())
        self._send_json(
            {
                "items": [
                    {
                        "video_id": match.video_id,
                        "title": match.title,
                        "has_trunks": match.video_id in available,
                    }
                    for match in matches
                ]
            }
        )

    def _gutenberg_book_json(
        self, book_id: str, text_url: str = "", title: str = ""
    ) -> None:
        """Return one book's cached plaintext for the two-column modal."""
        if not GUTENBERG_BOOK_ID_RE.fullmatch(book_id):
            self._send_error_json(
                "Usage: /api/gutenberg_book?id=<book_id>", 400
            )
            return
        try:
            text, _rag_path = ensure_gutenberg_book(
                book_id, text_url=text_url, title=title
            )
        except (ValueError, RuntimeError) as exc:
            self._send_error_json(str(exc), 502)
            return
        catalog = GUTENBERG_CATALOG_CACHE.get(book_id) or {}
        resolved_title = (
            title
            or str(catalog.get("title") or "")
            or next(
                (line.strip() for line in text.splitlines() if line.strip()),
                f"Book {book_id}",
            )[:200]
        )
        self._send_json(
            {
                "id": book_id,
                "title": resolved_title,
                "authors": str(catalog.get("authors") or ""),
                "languages": catalog.get("languages") or [],
                "subjects": catalog.get("subjects") or [],
                "download_count": int(catalog.get("download_count") or 0),
                "url": GUTENBERG_BOOK_URL.format(book_id=book_id),
                "text_url": text_url
                or str(catalog.get("text_url") or ""),
                "text": text,
                "characters": len(text),
            }
        )

    # ---- POST ---------------------------------------------------------
    def do_POST(self) -> None:  # noqa: N802 - BaseHTTPRequestHandler API
        parsed = urlparse(self.path)
        if parsed.path == "/api/ask":
            payload = self._read_json()
            if payload is None:
                return
            question = str(payload.get("question", "")).strip()
            raw_image = payload.get("image")
            image: str | None = None
            if raw_image:
                try:
                    image = normalize_attachment_image(raw_image)
                except ValueError as exc:
                    self._send_error_json(str(exc), 400)
                    return
            if not question and image is None:
                self._send_error_json("Missing question", 400)
                return
            if image is not None and question.startswith("/"):
                self._send_error_json(
                    "Images can only be attached to a question, not a command",
                    400,
                )
                return
            self._handle_ask(question, image=image)
            return
        if parsed.path == "/api/pdf_knowledge_graph":
            payload = self._read_json()
            if payload is None:
                return
            name = str(payload.get("name") or "").strip()
            rebuild = bool(payload.get("rebuild"))
            self._handle_pdf_knowledge_graph(name, rebuild)
            return
        if parsed.path == "/api/pdf_books/download":
            payload = self._read_json()
            if payload is None:
                return
            slug = str(payload.get("slug") or "").strip()
            try:
                result = download_manning_free_ebook(slug)
            except ValueError as exc:
                self._send_error_json(str(exc), 400)
                return
            except (requests.RequestException, OSError, RuntimeError) as exc:
                self._send_error_json(
                    f"Could not download the Manning ebook: {exc}", 502
                )
                return
            self._send_json(result)
            return
        if parsed.path == "/api/pdf_learn":
            payload = self._read_json()
            if payload is None:
                return
            name = str(payload.get("name") or "").strip()
            if not name:
                self._send_error_json("Missing PDF book name", 400)
                return
            self._handle_pdf_learn(name)
            return
        if parsed.path == "/api/pdf_chat":
            payload = self._read_json()
            if payload is None:
                return
            name = str(payload.get("name") or "").strip()
            question = str(payload.get("question") or "").strip()
            if not name:
                self._send_error_json("Missing PDF book name", 400)
                return
            if not question:
                self._send_error_json("Missing question", 400)
                return
            self._handle_pdf_chat(
                name, question, reset=bool(payload.get("reset"))
            )
            return
        if parsed.path == "/api/dict":
            payload = self._read_json()
            if payload is None:
                return
            word = str(payload.get("word", "")).strip()
            answer = payload.get("answer")
            self._handle_dict(word, answer if isinstance(answer, str) else None)
            return
        if parsed.path == "/api/translate":
            payload = self._read_json()
            if payload is None:
                return
            text = payload.get("text")
            if not isinstance(text, str) or not text.strip():
                self._send_error_json("Missing text to translate", 400)
                return
            self._handle_translate(text)
            return
        if parsed.path == "/api/simplify":
            payload = self._read_json()
            if payload is None:
                return
            text = payload.get("text")
            if not isinstance(text, str) or not text.strip():
                self._send_error_json("Missing article text to simplify", 400)
                return
            self._handle_simplify(text)
            return
        if parsed.path == "/api/tts":
            payload = self._read_json()
            if payload is None:
                return
            text = payload.get("text")
            if not isinstance(text, str) or not text.strip():
                self._send_error_json("Missing text to read aloud", 400)
                return
            voice = str(payload.get("voice") or "")
            try:
                self._send_wav(synthesize_speech(text, voice=voice))
            except ValueError as exc:
                self._send_error_json(str(exc), 400)
            except (OSError, subprocess.SubprocessError, RuntimeError) as exc:
                self._send_error_json(
                    f"Could not read the text aloud: {exc}", 502
                )
            return
        if parsed.path == "/api/ai_news_podcast":
            payload = self._read_json()
            if payload is None:
                return
            self._handle_ai_news_podcast(payload)
            return
        if parsed.path == "/api/explain_line":
            payload = self._read_json()
            if payload is None:
                return
            self._handle_explain_line(payload)
            return
        if parsed.path == "/api/snippet_question":
            payload = self._read_json()
            if payload is None:
                return
            self._handle_snippet_question(payload)
            return
        if parsed.path == "/api/video_learn":
            payload = self._read_json()
            if payload is None:
                return
            video_id = str(payload.get("id") or "").strip()
            if not re.fullmatch(r"[A-Za-z0-9_-]{11}", video_id):
                self._send_error_json("Missing or invalid YouTube video id", 400)
                return
            title = str(payload.get("title") or "").strip()
            relearn = bool(payload.get("relearn"))
            status, job = start_video_learn_job(
                self.state, video_id, title=title, relearn=relearn
            )
            response = {"id": video_id, "status": status}
            if status == "learned":
                response.update(progress=100.0, learned=True)
            else:
                response["job"] = job
            self._send_json(response)
            return
        if parsed.path == "/api/news_learn":
            payload = self._read_json()
            if payload is None:
                return
            try:
                result = learn_news_article(payload)
            except ValueError as exc:
                self._send_error_json(str(exc), 400)
                return
            except OSError as exc:
                self._send_error_json(
                    f"Could not store the story: {exc}", 500
                )
                return
            refresh_news_corpus(self.state, result["id"])
            self._send_json(result)
            return
        if parsed.path == "/api/news_chat":
            payload = self._read_json()
            if payload is None:
                return
            news_id = str(payload.get("id") or "").strip()
            if not NEWS_ID_PATTERN.fullmatch(news_id):
                self._send_error_json("Missing or invalid news story id", 400)
                return
            question = str(payload.get("question") or "").strip()
            if not question:
                self._send_error_json("Missing question", 400)
                return
            self._handle_news_chat(
                news_id, question, reset=bool(payload.get("reset"))
            )
            return
        if parsed.path == "/api/news_chat_close":
            payload = self._read_json()
            if payload is None:
                return
            news_id = str(payload.get("id") or "").strip()
            if not NEWS_ID_PATTERN.fullmatch(news_id):
                self._send_error_json("Missing or invalid news story id", 400)
                return
            forget_news_chat_state(news_id)
            self._send_json({"closed": True, "id": news_id})
            return
        if parsed.path == "/api/video_summary":
            payload = self._read_json()
            if payload is None:
                return
            self._handle_video_summary(payload)
            return
        if parsed.path == "/api/video_chat":
            payload = self._read_json()
            if payload is None:
                return
            video_id = str(payload.get("id") or "").strip()
            if not re.fullmatch(r"[A-Za-z0-9_-]{11}", video_id):
                self._send_error_json("Missing or invalid YouTube video id", 400)
                return
            question = str(payload.get("question") or "").strip()
            if not question:
                self._send_error_json("Missing question", 400)
                return
            self._handle_video_chat(
                video_id, question, reset=bool(payload.get("reset"))
            )
            return
        if parsed.path == "/api/game_current_location":
            payload = self._read_json()
            if payload is None:
                return
            requested = str(payload.get("game") or "").strip()
            normalized = re.sub(
                r"[^a-z0-9]+", "_", requested.lower()
            ).strip("_")
            compact = normalized.replace("_", "")
            game = next(
                (
                    candidate
                    for candidate in list_game_prompts()
                    if candidate["name"] == normalized
                    or candidate["name"].replace("_", "") == compact
                ),
                None,
            )
            if game is None:
                self._send_error_json("Unknown game", 400)
                return
            try:
                latitude = float(payload.get("latitude"))
                longitude = float(payload.get("longitude"))
            except (TypeError, ValueError):
                self._send_error_json(
                    "Missing or invalid game coordinates", 400
                )
                return
            if not _valid_game_coordinates(latitude, longitude):
                self._send_error_json(
                    "Missing or invalid game coordinates", 400
                )
                return
            if not _game_bounds_contain(
                game.get("bounds"), latitude, longitude
            ):
                self._send_error_json(
                    "That location is outside this game's exploration area",
                    400,
                )
                return
            name = " ".join(
                str(
                    payload.get("name")
                    or payload.get("title")
                    or "Saved location"
                ).split()
            )[:200]
            link = str(payload.get("link") or "").strip()
            category = " ".join(
                str(payload.get("category") or "Custom location").split()
            )[:80]
            current_location = {
                "name": name or "Saved location",
                "latitude": latitude,
                "longitude": longitude,
                "link": link or _osm_point_link(latitude, longitude),
                "category": category or "Custom location",
                "saved_at": datetime.now(timezone.utc)
                .replace(microsecond=0)
                .isoformat(),
            }
            data = load_game_data(game["name"])
            data["current_location"] = current_location
            try:
                save_game_data(game["name"], data)
            except OSError as exc:
                self._send_error_json(
                    f"Could not save the starting location: {exc}", 500
                )
                return
            self._send_json(
                {
                    "ok": True,
                    "game": game["name"],
                    "current_location": current_location,
                }
            )
            return
        if parsed.path == "/api/youtube_bookmark":
            payload = self._read_json()
            if payload is None:
                return
            try:
                entry = add_youtube_bookmark(payload)
            except ValueError as exc:
                self._send_error_json(str(exc), 400)
                return
            self._send_json({"ok": True, "bookmark": entry})
            return
        if parsed.path == "/api/youtube_more":
            payload = self._read_json()
            if payload is None:
                return
            keyword = strip_surrounding_search_quotes(
                str(payload.get("keyword") or "")
            )
            raw_exclude = payload.get("exclude")
            exclude = (
                {str(value) for value in raw_exclude}
                if isinstance(raw_exclude, list)
                else set()
            )
            self._handle_youtube_more(keyword, exclude)
            return
        if parsed.path == "/api/gutenberg_chat":
            payload = self._read_json()
            if payload is None:
                return
            book_id = str(payload.get("id") or "").strip()
            if not GUTENBERG_BOOK_ID_RE.fullmatch(book_id):
                self._send_error_json(
                    "Missing or invalid Project Gutenberg book id", 400
                )
                return
            question = str(payload.get("question") or "").strip()
            if not question:
                self._send_error_json("Missing question", 400)
                return
            self._handle_gutenberg_chat(
                book_id,
                question,
                title=str(payload.get("title") or "").strip(),
                text_url=str(payload.get("url") or "").strip(),
                reset=bool(payload.get("reset")),
            )
            return
        if parsed.path == "/api/mathtutor_upload":
            payload = self._read_json()
            if payload is None:
                return
            raw_files = payload.get("files")
            if not isinstance(raw_files, list) or not raw_files:
                self._send_error_json("Upload at least one Word document", 400)
                return
            try:
                session = _mathtutor_build_session(raw_files)
            except ValueError as exc:
                self._send_error_json(str(exc), 400)
                return
            except RuntimeError as exc:
                self._send_error_json(
                    f"Could not generate practice problems: {exc}", 502
                )
                return
            except OSError as exc:
                self._send_error_json(
                    f"Could not store the uploaded documents: {exc}", 500
                )
                return
            with self.state.lock:
                self.state.mathtutor_session_id = str(session["id"])
            self._send_json({"session": _mathtutor_public_session(session)})
            return
        if parsed.path == "/api/mathtutor_more":
            payload = self._read_json()
            if payload is None:
                return
            session_id = str(payload.get("id") or "").strip()
            try:
                with MATHUTOR_LOCK:
                    session = _mathtutor_load_session(session_id)
                    start_id = len(session.get("problems", [])) + 1
                    added = _mathtutor_generate_problems(
                        session,
                        count=MATHUTOR_PROBLEMS_PER_BATCH,
                        start_id=start_id,
                    )
                    session.setdefault("problems", []).extend(added)
                    _mathtutor_save_session(session)
            except ValueError as exc:
                self._send_error_json(str(exc), 400)
                return
            except RuntimeError as exc:
                self._send_error_json(
                    f"Could not generate more practice problems: {exc}", 502
                )
                return
            except OSError as exc:
                self._send_error_json(
                    f"Could not update the mathtutor session: {exc}", 500
                )
                return
            with self.state.lock:
                self.state.mathtutor_session_id = str(session.get("id", ""))
            self._send_json(
                {
                    "session": _mathtutor_public_session(session),
                    "added": len(added),
                }
            )
            return
        if parsed.path == "/api/mathtutor_remove_docs":
            payload = self._read_json()
            if payload is None:
                return
            session_id = str(payload.get("id") or "").strip() or None
            try:
                removed = _mathtutor_remove_all_documents(session_id)
            except ValueError as exc:
                self._send_error_json(str(exc), 400)
                return
            except OSError as exc:
                self._send_error_json(
                    f"Could not remove the uploaded documents: {exc}", 500
                )
                return
            with self.state.lock:
                if not session_id or self.state.mathtutor_session_id == session_id:
                    self.state.mathtutor_session_id = None
            self._send_json({"removed": removed})
            return
        if parsed.path == "/api/mathtutor_answer":
            payload = self._read_json()
            if payload is None:
                return
            session_id = str(payload.get("id") or "").strip()
            try:
                index = int(payload.get("index", -1))
            except (TypeError, ValueError):
                index = -1
            student_answer = str(payload.get("answer") or "").strip()
            self._handle_mathtutor_answer(session_id, index, student_answer)
            return
        self._send_error_json("Not found", 404)


# Cross-component imports are placed after the definitions so a
# feature module can depend on another feature module safely.
from .article import GUTENBERG_CATALOG_CACHE
from .books import build_pdf_knowledge_graph, download_manning_free_ebook, pdf_book_path, pdf_knowledge_graph_path, read_pdf_book_page, read_pdf_concept, render_pdf_book_page, written_book_path
from .changelog import changelog_file_path
from .command_config import GUTENBERG_BOOK_ID_RE, GUTENBERG_BOOK_URL, GUTENBERG_SUBCOMMANDS, OPENLIBRARY_SUBCOMMANDS, STACKOVERFLOW_SUBCOMMANDS, YOUTUBE_SUBCOMMANDS, strip_surrounding_search_quotes
from .core import CHAT_HTML, MAX_REQUEST_BYTES, PATH_COMPLETION_LIMIT, WEB_COMMANDS
from .directory_anthem import _anthem_provider_detail
from .directory_uhc import _uhc_bundle_index, _uhc_detail_item, _uhc_find_practitioner, _uhc_roles_for_practitioner
from .english_corner import english_corner_vocabulary
from .game import _game_bounds_contain, _game_nearby_cache_key, _osm_point_link, _valid_game_coordinates, cached_game_nearby, fetch_game_place_details, list_game_prompts, load_game_data, save_game_data, search_game_geocode_suggestions, search_game_place_news, search_game_places, store_game_nearby
from .image import normalize_attachment_image
from .library import ensure_gutenberg_book
from .map import GAME_NEARBY_LIMIT, GAME_NEARBY_RADIUS_METERS, GAME_NEARBY_RESTAURANT_LIMIT, GAME_NEWS_LIMIT, MAP_GEOCODE_LIMIT, MAP_RESTAURANT_RADIUS_METERS, OSM_RESTAURANT_TYPES, fetch_driving_instructions, fetch_osm_restaurant, fetch_yelp_restaurant, geocode_locations, search_nearby_restaurants, yelp_api_key
from .mathtutor import MATHUTOR_LOCK, MATHUTOR_PROBLEMS_PER_BATCH, _mathtutor_build_session, _mathtutor_generate_problems, _mathtutor_load_session, _mathtutor_public_session, _mathtutor_remove_all_documents, _mathtutor_save_session
from .news import fetch_news_article
from .news_research import NEWS_SOURCES
from .pdfbook import pdf_book_status
from .reddit import GOOGLE_NEWS_ARTICLE_RE, _reddit_iframe_url, fetch_reddit_post, fetch_stock_reddit_items, resolve_google_news_url
from .retrieval import ClientDisconnected, NEWS_ID_PATTERN, _read_news_meta, learn_news_article, news_is_learned, news_title
from .robinhood import ZILLOW_MCP_LIMIT, ZILLOW_MCP_RADIUS_MILES, ZillowMCPError
from .stackoverflow import fetch_stackoverflow_answers
from .stock import fetch_stock_company_summary
from .tts import synthesize_speech
from .video import _video_learn_jobs_snapshot, forget_news_chat_state, refresh_news_corpus, start_video_learn_job, video_is_chat_ready
from .wikivoyage import fetch_wikivoyage_article
from .youtube import add_youtube_bookmark
from .zillow import zillow_nearby_listings
