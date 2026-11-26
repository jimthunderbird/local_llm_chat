"""Robinhood MCP client and holdings reports."""

from __future__ import annotations

from .core import *
from .core import SCRIPT_DIR

# ---------------------------------------------------------------------------
# /robinhood -- read holdings, pending orders, and recent transactions from a
# Robinhood Model Context Protocol (MCP) server.  The official endpoint
# (https://agent.robinhood.com/mcp/trading) uses OAuth, while community servers
# such as robinhood-mcp run locally over stdio.  Both transports are supported
# so the chat can use whichever server the user has already connected.
#
# Configuration is read from .chat/robinhood_mcp.json first, with environment
# variables taking precedence for one-off overrides.  The JSON config may
# contain any of:
#   {"url": "https://agent.robinhood.com/mcp/trading", "token": "..."}
#   {"command": "uvx robinhood-mcp", "args": [],
#    "env": {"ROBINHOOD_USERNAME": "...", "ROBINHOOD_PASSWORD": "..."}}
# Environment variables: ROBINHOOD_MCP_COMMAND, ROBINHOOD_MCP_URL,
# ROBINHOOD_MCP_TOKEN, ROBINHOOD_MCP_TOKEN_FILE, and ROBINHOOD_MCP_CONFIG
# (which points at an alternative JSON file).  The token may also be stored in
# ~/.robinhood_mcp_token or .chat/robinhood_mcp_token.
# ---------------------------------------------------------------------------

ROBINHOOD_MCP_DEFAULT_URL = "https://agent.robinhood.com/mcp/trading"

ROBINHOOD_MCP_PROTOCOL_VERSIONS = (
    "2025-11-25",
    "2025-06-18",
    "2024-11-05",
)

ROBINHOOD_MCP_TIMEOUT = 90.0

ROBINHOOD_MCP_PENDING_LIMIT = 20

ROBINHOOD_MCP_HISTORY_LIMIT = 10

ROBINHOOD_MCP_CONFIG_FILE = SCRIPT_DIR / ".chat" / "robinhood_mcp.json"

# /zillow -- nearby for-sale and rental listings from Zillow's official MCP
# server. The server is a public streamable-HTTP endpoint and needs no auth.
# The MCP search API takes named regions, not latitude/longitude, so the flow
# is: reverse-geocode the coordinate to a city/neighborhood with Nominatim,
# request nearby-sorted Zillow listings for that region, then filter the
# results by exact Haversine distance.
ZILLOW_MCP_DEFAULT_URL = "https://mcp.zillow.com/api/v2"

ZILLOW_MCP_TIMEOUT = 90.0

ZILLOW_MCP_RADIUS_MILES = 1.0

ZILLOW_MCP_LIMIT = 5

ZILLOW_MCP_CACHE_TTL_SECONDS = 300

ZILLOW_MCP_REGION_CACHE_TTL_SECONDS = 86400

# Placeholder values shipped in the sample config.  They must be treated as
# "not configured" so the caller gets setup instructions instead of a failed
# Robinhood login or a 401 from the official endpoint.
ROBINHOOD_MCP_PLACEHOLDERS = {
    "",
    "...",
    "you@example.com",
    "your-password",
    "your_password",
    "changeme",
    "optional-base32-secret",
    "your_2fa_secret",
    "your-2fa-secret",
    "<oauth access token>",
    "<username>",
    "<password>",
}

class RobinhoodMCPError(RuntimeError):
    """A configuration, transport, or tool error from the Robinhood MCP."""

class ZillowMCPError(RuntimeError):
    """A transport, region-resolution, or tool error from the Zillow MCP."""

_POSITION_TOOL_CANDIDATES = (
    "get_position",
    "robinhood_get_position",
    "get_stock_positions",
    "robinhood_get_positions",
    "get_positions",
    "get_holdings",
    "robinhood_get_holdings",
    "get_portfolio",
)

_OPEN_ORDER_TOOL_CANDIDATES = (
    "get_open_orders",
    "robinhood_get_open_orders",
    "get_pending_orders",
    "robinhood_get_pending_orders",
    "get_orders",
)

_ORDER_HISTORY_TOOL_CANDIDATES = (
    "get_order_history",
    "robinhood_get_order_history",
    "get_recent_stock_orders",
    "robinhood_get_recent_stock_orders",
    "get_recent_orders",
    "get_orders",
    "get_transactions",
)

_ACCOUNT_TOOL_CANDIDATES = (
    "get_accounts",
    "robinhood_get_accounts",
    "get_account",
    "robinhood_get_account",
    "get_account_profile",
    "robinhood_get_account_profile",
)

def _robinhood_configured_value(value: object) -> str:
    """Return a config value, or "" when it is empty or a placeholder."""
    if value is None:
        return ""
    text = str(value).strip()
    return "" if text.lower() in ROBINHOOD_MCP_PLACEHOLDERS else text

def _robinhood_mcp_config() -> dict:
    """Resolve the MCP transport and credentials without exposing secrets."""
    config_path = Path(
        os.environ.get("ROBINHOOD_MCP_CONFIG") or ROBINHOOD_MCP_CONFIG_FILE
    ).expanduser()
    data: dict = {}
    file_found = config_path.is_file()
    if file_found:
        try:
            loaded = json.loads(config_path.read_text(encoding="utf-8"))
            if isinstance(loaded, dict):
                data = loaded
        except (OSError, ValueError):
            data = {}

    env_command = os.environ.get("ROBINHOOD_MCP_COMMAND")
    command_value = env_command or data.get("command")
    args_value = data.get("args") if env_command is None else None
    command: list[str] = []
    if isinstance(command_value, (list, tuple)):
        command = [str(part) for part in command_value if str(part)]
    elif command_value:
        command = shlex.split(str(command_value))
    if command and isinstance(args_value, (list, tuple)):
        command.extend(str(part) for part in args_value)

    url = (
        os.environ.get("ROBINHOOD_MCP_URL")
        or data.get("url")
        or ROBINHOOD_MCP_DEFAULT_URL
    )
    token = _robinhood_configured_value(
        os.environ.get("ROBINHOOD_MCP_TOKEN") or data.get("token")
    )
    token_file = os.environ.get("ROBINHOOD_MCP_TOKEN_FILE")
    token_paths = []
    if token_file:
        token_paths.append(Path(token_file).expanduser())
    token_paths.extend(
        [
            Path.home() / ".robinhood_mcp_token",
            SCRIPT_DIR / ".chat" / "robinhood_mcp_token",
        ]
    )
    if not token:
        for path in token_paths:
            try:
                if path.is_file():
                    token = _robinhood_configured_value(
                        path.read_text(encoding="utf-8")
                    )
                    if token:
                        break
            except OSError:
                continue

    # Start from the process environment so exported credentials still work,
    # then apply non-placeholder values from the JSON file.  Placeholder values
    # are skipped rather than copying them into the child process.
    env = dict(os.environ)
    configured_env = data.get("env")
    if isinstance(configured_env, dict):
        for key, value in configured_env.items():
            key_text = str(key)
            value_text = "" if value is None else str(value)
            if value_text.strip().lower() in ROBINHOOD_MCP_PLACEHOLDERS:
                continue
            env[key_text] = value_text

    credentials_missing: list[str] = []
    if command:
        for name in ("ROBINHOOD_USERNAME", "ROBINHOOD_PASSWORD"):
            if not _robinhood_configured_value(env.get(name)):
                credentials_missing.append(name)

    return {
        "command": command,
        "url": str(url).strip(),
        "token": token,
        "env": env,
        "config_path": str(config_path),
        "file_found": file_found,
        "credentials_missing": credentials_missing,
    }

def _robinhood_mcp_setup_help(
    config_path: str = "", missing: list[str] | None = None
) -> str:
    path = config_path or str(ROBINHOOD_MCP_CONFIG_FILE)
    lines = ["Robinhood MCP is not configured."]
    if missing:
        lines.append("Missing: " + ", ".join(missing) + ".")
    lines.extend(
        [
            f"Fill in {path}, or export the variables below, then retry:",
            "  local stdio server (recommended for community robinhood-mcp):",
            "    export ROBINHOOD_MCP_COMMAND='uvx robinhood-mcp'",
            "    export ROBINHOOD_USERNAME='you@example.com'",
            "    export ROBINHOOD_PASSWORD='...'",
            "  official streamable-HTTP endpoint:",
            "    export ROBINHOOD_MCP_TOKEN='<OAuth access token>'",
            f"    export ROBINHOOD_MCP_URL='{ROBINHOOD_MCP_DEFAULT_URL}'",
        ]
    )
    return "\n".join(lines)

def _zillow_mcp_config() -> dict:
    """Resolve the public Zillow MCP endpoint."""
    try:
        timeout = max(
            10.0,
            float(os.environ.get("ZILLOW_MCP_TIMEOUT", ZILLOW_MCP_TIMEOUT)),
        )
    except (TypeError, ValueError):
        timeout = ZILLOW_MCP_TIMEOUT
    return {
        "command": [],
        "url": str(
            os.environ.get("ZILLOW_MCP_URL") or ZILLOW_MCP_DEFAULT_URL
        ).strip(),
        "token": "",
        "env": dict(os.environ),
        "config_path": "",
        "file_found": False,
        "credentials_missing": [],
        "timeout": timeout,
        "allow_no_token": True,
    }

def _zillow_mcp_setup_help(
    config_path: str = "", missing: list[str] | None = None
) -> str:
    return (
        "Zillow MCP is unavailable. The official endpoint is "
        f"{ZILLOW_MCP_DEFAULT_URL} and needs no sign-in. "
        "Check network access, or set ZILLOW_MCP_URL to a compatible endpoint."
    )

class _RobinhoodMCPClient:
    """A minimal MCP client for stdio and streamable-HTTP servers."""

    def __init__(
        self,
        config: dict,
        timeout: float = ROBINHOOD_MCP_TIMEOUT,
        label: str = "Robinhood",
        error_type: type[Exception] = RobinhoodMCPError,
        setup_help: Callable[[str, list[str] | None], str] | None = None,
        allow_no_token: bool = False,
    ):
        self.label = label
        self.error_type = error_type
        self.setup_help = setup_help or _robinhood_mcp_setup_help
        self.allow_no_token = allow_no_token
        self.command = list(config.get("command") or [])
        self.url = str(config.get("url") or "")
        self.token = str(config.get("token") or "")
        self.env = dict(config.get("env") or os.environ)
        self.config_path = str(config.get("config_path") or "")
        self.credentials_missing = list(config.get("credentials_missing") or [])
        self.timeout = timeout
        self.transport = "stdio" if self.command else "http"
        self.session_id = ""
        self.protocol_version = ROBINHOOD_MCP_PROTOCOL_VERSIONS[0]
        self._next_id = 0
        self._tools: list[dict] = []
        self._proc: subprocess.Popen | None = None
        self._inbox: queue.Queue = queue.Queue()
        self._stderr_lines: list[str] = []
        self._closed = False

    def __enter__(self) -> "_RobinhoodMCPClient":
        self.connect()
        return self

    def __exit__(self, exc_type, exc, tb) -> None:
        self.close()

    def close(self) -> None:
        if self._closed:
            return
        self._closed = True
        proc = self._proc
        if proc is None:
            return
        try:
            proc.terminate()
            proc.wait(timeout=2)
        except Exception:
            try:
                proc.kill()
            except Exception:
                pass

    def connect(self) -> None:
        if (
            (not self.command and not self.token and not self.allow_no_token)
            or (not self.command and not self.url)
            or (self.transport == "stdio" and self.credentials_missing)
        ):
            raise self.error_type(
                self.setup_help(self.config_path, self.credentials_missing)
            )
        if self.transport == "stdio":
            self._start_stdio()

        last_error: Exception | None = None
        for version in ROBINHOOD_MCP_PROTOCOL_VERSIONS:
            self.protocol_version = version
            try:
                result = self._rpc(
                    "initialize",
                    {
                        "protocolVersion": version,
                        "capabilities": {},
                        "clientInfo": {"name": "personal-chatbot", "version": "1.0"},
                    },
                )
                negotiated = result.get("protocolVersion") if isinstance(result, dict) else ""
                if negotiated:
                    self.protocol_version = str(negotiated)
                last_error = None
                break
            except self.error_type as exc:
                last_error = exc
                if self.transport == "http" and "401" in str(exc):
                    break
        if last_error is not None:
            raise last_error
        try:
            self._notify("notifications/initialized", {})
        except self.error_type:
            # Some servers do not implement notifications; initialization has
            # already succeeded, so continue to the tool list.
            pass
        result = self._rpc("tools/list", {})
        tools = result.get("tools") if isinstance(result, dict) else None
        self._tools = [tool for tool in tools or [] if isinstance(tool, dict)]

    def tools(self) -> list[dict]:
        return list(self._tools)

    def call_tool(self, name: str, arguments: dict | None = None) -> dict:
        result = self._rpc("tools/call", {"name": name, "arguments": arguments or {}})
        return result if isinstance(result, dict) else {"content": result}

    # -- transport plumbing -------------------------------------------------

    def _start_stdio(self) -> None:
        try:
            self._proc = subprocess.Popen(
                self.command,
                stdin=subprocess.PIPE,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                text=True,
                bufsize=1,
                env=self.env,
            )
        except (OSError, ValueError) as exc:
            raise self.error_type(
                f"could not start the {self.label} MCP command {self.command!r}: {exc}"
            ) from exc

        def read_stdout() -> None:
            assert self._proc is not None and self._proc.stdout is not None
            for line in self._proc.stdout:
                line = line.strip()
                if line:
                    self._inbox.put(line)
            self._inbox.put(None)

        def read_stderr() -> None:
            assert self._proc is not None and self._proc.stderr is not None
            for line in self._proc.stderr:
                self._stderr_lines.append(line.strip())
                del self._stderr_lines[:-20]

        threading.Thread(target=read_stdout, daemon=True).start()
        threading.Thread(target=read_stderr, daemon=True).start()

    def _stdio_stderr(self) -> str:
        return "\n".join(line for line in self._stderr_lines if line)[-1200:]

    def _stdio_write(self, message: dict) -> None:
        proc = self._proc
        if proc is None or proc.stdin is None:
            raise self.error_type(f"the {self.label} MCP process is not running")
        try:
            proc.stdin.write(json.dumps(message, separators=(",", ":")) + "\n")
            proc.stdin.flush()
        except (BrokenPipeError, OSError) as exc:
            detail = self._stdio_stderr()
            raise self.error_type(
                f"the {self.label} MCP process exited"
                + (f":\n{detail}" if detail else "")
            ) from exc

    def _stdio_rpc(self, method: str, params: dict, message_id: int | None) -> dict:
        message = {"jsonrpc": "2.0", "method": method}
        if params:
            message["params"] = params
        if message_id is not None:
            message["id"] = message_id
        self._stdio_write(message)
        if message_id is None:
            return {}
        deadline = time.monotonic() + self.timeout
        while True:
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                detail = self._stdio_stderr()
                raise self.error_type(
                    f"{self.label} MCP timed out waiting for {method}"
                    + (f":\n{detail}" if detail else "")
                )
            try:
                line = self._inbox.get(timeout=min(remaining, 1.0))
            except queue.Empty:
                continue
            if line is None:
                detail = self._stdio_stderr()
                raise self.error_type(
                    f"the {self.label} MCP process exited"
                    + (f":\n{detail}" if detail else "")
                )
            try:
                message = json.loads(line)
            except ValueError:
                continue
            if not isinstance(message, dict) or message.get("id") != message_id:
                continue
            error = message.get("error")
            if isinstance(error, dict):
                raise self.error_type(
                    str(error.get("message") or error.get("code") or error)
                )
            result = message.get("result")
            return result if isinstance(result, dict) else {"result": result}

    def _http_rpc(self, method: str, params: dict, message_id: int | None) -> dict:
        message = {"jsonrpc": "2.0", "method": method}
        if params:
            message["params"] = params
        if message_id is not None:
            message["id"] = message_id
        headers = {
            "Content-Type": "application/json",
            "Accept": "application/json, text/event-stream",
            "MCP-Protocol-Version": self.protocol_version,
            "User-Agent": "personal-chatbot/1.0",
        }
        if self.token:
            headers["Authorization"] = f"Bearer {self.token}"
        if self.session_id:
            headers["Mcp-Session-Id"] = self.session_id
        try:
            response = requests.post(
                self.url, json=message, headers=headers, timeout=self.timeout
            )
        except requests.RequestException as exc:
            raise self.error_type(
                f"could not reach {self.label} MCP at {self.url}: {exc}"
            ) from exc
        session_id = response.headers.get("Mcp-Session-Id")
        if session_id:
            self.session_id = session_id
        if response.status_code == 401:
            raise self.error_type(
                f"{self.label} MCP rejected the token (HTTP 401). "
                "Refresh ROBINHOOD_MCP_TOKEN or reconnect the server in your "
                "MCP client. See /help for the supported setup."
            )
        if response.status_code >= 400:
            body = (response.text or "").strip()[:800]
            raise self.error_type(
                f"{self.label} MCP returned HTTP {response.status_code}"
                + (f": {body}" if body else "")
            )
        if message_id is None:
            return {}
        content_type = str(response.headers.get("Content-Type") or "").lower()
        payloads: list[str] = []
        if "text/event-stream" in content_type:
            data_lines: list[str] = []
            for raw in (response.text or "").splitlines():
                line = raw.rstrip("\r")
                if not line:
                    if data_lines:
                        payloads.append("\n".join(data_lines))
                        data_lines = []
                    continue
                if line.startswith(":"):
                    continue
                if line.startswith("data:"):
                    data_lines.append(line[5:].lstrip())
            if data_lines:
                payloads.append("\n".join(data_lines))
        else:
            payloads.append(response.text or "")
        fallback: dict | None = None
        for payload in payloads:
            payload = payload.strip()
            if not payload or payload == "[DONE]":
                continue
            try:
                incoming = json.loads(payload)
            except ValueError:
                continue
            messages = incoming if isinstance(incoming, list) else [incoming]
            for item in messages:
                if not isinstance(item, dict):
                    continue
                if item.get("id") == message_id:
                    error = item.get("error")
                    if isinstance(error, dict):
                        raise self.error_type(
                            str(error.get("message") or error.get("code") or error)
                        )
                    result = item.get("result")
                    return result if isinstance(result, dict) else {"result": result}
                fallback = item
        if fallback is not None:
            error = fallback.get("error")
            if isinstance(error, dict):
                raise self.error_type(
                    str(error.get("message") or error.get("code") or error)
                )
            result = fallback.get("result")
            return result if isinstance(result, dict) else {"result": result}
        raise self.error_type(f"{self.label} MCP returned no response for {method}")

    def _rpc(self, method: str, params: dict) -> dict:
        self._next_id += 1
        message_id = self._next_id
        if self.transport == "stdio":
            return self._stdio_rpc(method, params, message_id)
        return self._http_rpc(method, params, message_id)

    def _notify(self, method: str, params: dict) -> None:
        if self.transport == "stdio":
            self._stdio_rpc(method, params, None)
        else:
            self._http_rpc(method, params, None)

def _robinhood_norm_key(value: object) -> str:
    return re.sub(r"[^a-z0-9]", "", str(value).lower())

def _mcp_field(record: dict, names: tuple[str, ...], *, depth: int = 0):
    """Return the first non-empty field matching one of ``names``."""
    if not isinstance(record, dict) or depth > 3:
        return None
    wanted = {_robinhood_norm_key(name) for name in names}
    for key, value in record.items():
        if _robinhood_norm_key(key) in wanted and value not in (None, "", [], {}):
            return value
    for key, value in record.items():
        if isinstance(value, dict):
            found = _mcp_field(value, names, depth=depth + 1)
            if found not in (None, "", [], {}):
                return found
    return None

def _mcp_parse_text(text: str):
    text = str(text or "").strip()
    if not text:
        return ""
    try:
        return json.loads(text)
    except ValueError:
        pass
    try:
        return ast.literal_eval(text)
    except (ValueError, SyntaxError):
        return text

def _mcp_result_payload(
    result: dict, error_type: type[Exception] = RobinhoodMCPError
) -> tuple[object, str]:
    """Turn one tools/call result into structured data and its raw text."""
    parts: list[str] = []
    content = result.get("content") if isinstance(result, dict) else None
    if isinstance(content, list):
        for item in content:
            if isinstance(item, dict) and item.get("type") == "text":
                parts.append(str(item.get("text") or ""))
    raw_text = "\n".join(part for part in parts if part).strip()
    if isinstance(result, dict) and result.get("isError"):
        raise error_type(raw_text or "the MCP tool returned an error")
    structured = result.get("structuredContent") if isinstance(result, dict) else None
    if structured not in (None, "", [], {}):
        return structured, raw_text
    return _mcp_parse_text(raw_text), raw_text

def _mcp_collect_dicts(value: object, *, depth: int = 0) -> list[dict]:
    if depth > 12:
        return []
    found: list[dict] = []
    if isinstance(value, dict):
        found.append(value)
        for item in value.values():
            found.extend(_mcp_collect_dicts(item, depth=depth + 1))
    elif isinstance(value, (list, tuple)):
        for item in value:
            found.extend(_mcp_collect_dicts(item, depth=depth + 1))
    return found

def _mcp_symbol(record: dict) -> str:
    value = _mcp_field(
        record,
        (
            "symbol",
            "ticker",
            "instrument_symbol",
            "underlying_symbol",
            "stock_symbol",
            "chain_symbol",
        ),
    )
    return str(value or "").strip().upper()

def _mcp_symbol_matches(record: dict, symbol: str) -> bool:
    recorded = _mcp_symbol(record)
    if not recorded:
        return False
    return recorded.split(".")[0] == symbol.split(".")[0]

def _mcp_dedupe(records: list[dict]) -> list[dict]:
    seen: set[str] = set()
    unique: list[dict] = []
    for record in records:
        identifier = _mcp_field(
            record, ("id", "order_id", "position_id", "instrument_id", "uuid")
        )
        if identifier not in (None, ""):
            key = str(identifier)
        else:
            try:
                key = json.dumps(record, sort_keys=True, default=str)
            except (TypeError, ValueError):
                key = repr(record)
        if key in seen:
            continue
        seen.add(key)
        unique.append(record)
    return unique

def _mcp_position_records(payload: object) -> list[dict]:
    quantity_keys = (
        "quantity",
        "shares",
        "quantity_held",
        "total_quantity",
        "shares_held",
        "position_quantity",
    )
    value_keys = (
        "average_buy_price",
        "average_price",
        "average_cost",
        "market_value",
        "cost_basis",
        "equity",
        "current_value",
    )
    records: list[dict] = []
    for record in _mcp_collect_dicts(payload):
        has_quantity = _mcp_field(record, quantity_keys) is not None
        has_value = _mcp_field(record, value_keys) is not None
        if _mcp_field(record, ("side",)) is not None:
            continue
        if has_quantity or has_value:
            records.append(record)
    return _mcp_dedupe(records)

def _mcp_order_records(payload: object) -> list[dict]:
    records: list[dict] = []
    for record in _mcp_collect_dicts(payload):
        side = _mcp_field(record, ("side", "order_side", "transaction_type"))
        has_order_shape = (
            _mcp_field(record, ("state", "status", "order_state")) is not None
            or _mcp_field(record, ("order_type", "type")) is not None
            or _mcp_field(record, ("order_id", "order_uuid")) is not None
        )
        if side is not None and has_order_shape:
            records.append(record)
    return _mcp_dedupe(records)

def _mcp_tool_schema(tool: dict) -> dict:
    schema = tool.get("inputSchema") or tool.get("input_schema")
    return schema if isinstance(schema, dict) else {}

def _mcp_pick_tool(tools: list[dict], candidates: tuple[str, ...]) -> dict | None:
    by_name = {
        str(tool.get("name") or ""): tool
        for tool in tools
        if isinstance(tool, dict) and tool.get("name")
    }
    for candidate in candidates:
        if candidate in by_name:
            return by_name[candidate]
    normalized = {_robinhood_norm_key(name): tool for name, tool in by_name.items()}
    for candidate in candidates:
        wanted = set(candidate.split("_"))
        for name, tool in normalized.items():
            tokens = set(re.findall(r"[a-z0-9]+", name))
            if wanted and wanted.issubset(tokens):
                return tool
    return None

def _mcp_arguments_for_tool(tool: dict, symbol: str, account: str) -> dict:
    schema = _mcp_tool_schema(tool)
    properties = schema.get("properties")
    if not isinstance(properties, dict):
        properties = {}
    arguments: dict = {}
    for key in properties:
        normalized = _robinhood_norm_key(key)
        if normalized in ("symbol", "ticker", "stocksymbol"):
            arguments[key] = symbol
        elif normalized in ("symbols", "tickers"):
            arguments[key] = [symbol]
        elif normalized == "accountnumber" and account:
            arguments[key] = account
    name = str(tool.get("name") or "").lower()
    if not properties and re.search(r"position|holding", name) and "s" not in name:
        arguments["symbol"] = symbol
    return arguments

def _mcp_call_tool(
    client: _RobinhoodMCPClient,
    tool: dict,
    symbol: str,
    account: str,
) -> tuple[object, str, str]:
    name = str(tool.get("name") or "")
    arguments = _mcp_arguments_for_tool(tool, symbol, account)
    try:
        result = client.call_tool(name, arguments)
    except client.error_type as exc:
        # Some servers only reveal a required argument when the call fails.
        if "symbol" not in arguments and re.search(r"symbol", str(exc), re.I):
            result = client.call_tool(name, {**arguments, "symbol": symbol})
        else:
            raise
    payload, raw_text = _mcp_result_payload(result, client.error_type)
    return payload, raw_text, name

def _mcp_first_account_number(payload: object) -> str:
    for record in _mcp_collect_dicts(payload):
        value = _mcp_field(
            record, ("account_number", "account_id", "account", "url"), depth=0
        )
        if value not in (None, ""):
            text = str(value).strip().rstrip("/").rsplit("/", 1)[-1]
            if (
                text
                and not text.lower().startswith("http")
                and text.lower() not in {"account", "accounts", "api", "v1"}
            ):
                return text
    return ""

def _mcp_number(value: object) -> float | None:
    if value in (None, ""):
        return None
    if isinstance(value, (int, float)):
        return float(value)
    text = str(value).strip().replace(",", "")
    text = re.sub(r"[^0-9eE+\-.]", "", text)
    if not text:
        return None
    try:
        return float(text)
    except ValueError:
        return None

def _mcp_quantity(record: dict) -> float | None:
    return _mcp_number(
        _mcp_field(
            record,
            (
                "quantity",
                "shares",
                "shares_held",
                "quantity_held",
                "total_quantity",
                "executed_quantity",
                "filled_quantity",
                "position_quantity",
            ),
        )
    )

def _mcp_price(record: dict) -> float | None:
    return _mcp_number(
        _mcp_field(
            record,
            (
                "average_price",
                "average_fill_price",
                "average_buy_price",
                "executed_price",
                "last_transaction_price",
                "price",
                "limit_price",
                "stop_price",
            ),
        )
    )

def _mcp_state(record: dict) -> str:
    value = _mcp_field(
        record, ("state", "status", "order_state", "order_status")
    )
    return re.sub(r"[^a-z]", "", str(value or "").lower())

_ROBINHOOD_OPEN_STATES = {
    "queued",
    "confirmed",
    "unconfirmed",
    "pending",
    "new",
    "open",
    "working",
    "placed",
    "partiallyfilled",
}

_ROBINHOOD_CLOSED_STATES = {
    "filled",
    "executed",
    "completed",
    "cancelled",
    "canceled",
    "rejected",
    "failed",
    "expired",
    "voided",
}

def _mcp_time(record: dict) -> str:
    value = _mcp_field(
        record,
        (
            "created_at",
            "createdAt",
            "order_created_at",
            "last_transaction_at",
            "updated_at",
            "updatedAt",
            "executed_at",
            "time",
        ),
    )
    return _mcp_iso_time(value)

def _mcp_sort_key(record: dict) -> float:
    value = _mcp_field(
        record,
        (
            "created_at",
            "createdAt",
            "order_created_at",
            "last_transaction_at",
            "updated_at",
            "updatedAt",
            "executed_at",
        ),
    )
    if isinstance(value, (int, float)):
        number = float(value)
        return number / 1000 if number > 10_000_000_000 else number
    text = str(value or "").strip().replace("Z", "+00:00")
    try:
        return datetime.fromisoformat(text).timestamp()
    except ValueError:
        return 0.0

def _mcp_iso_time(value: object) -> str:
    if value in (None, ""):
        return ""
    if isinstance(value, (int, float)):
        number = float(value)
        if number > 10_000_000_000:
            number /= 1000
        try:
            return datetime.fromtimestamp(number, timezone.utc).astimezone().strftime(
                "%Y-%m-%d %H:%M"
            )
        except (OverflowError, OSError, ValueError):
            return str(value)
    text = str(value).strip()
    normalized = text.replace("Z", "+00:00")
    if re.search(r"\.\d{7,}", normalized):
        normalized = re.sub(r"(\.\d{6})\d+", r"\1", normalized)
    try:
        parsed = datetime.fromisoformat(normalized)
    except ValueError:
        return text
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed.astimezone().strftime("%Y-%m-%d %H:%M")

def _mcp_money(value: float | None) -> str:
    if value is None:
        return ""
    if abs(value) < 1:
        return f"${value:,.4f}"
    return f"${value:,.2f}"

def _mcp_quantity_text(value: float | None) -> str:
    if value is None:
        return ""
    if abs(value - round(value)) < 1e-9:
        return f"{int(round(value)):,}"
    return f"{value:,.6f}".rstrip("0").rstrip(".")

def _robinhood_order_line(record: dict) -> str:
    side = str(
        _mcp_field(record, ("side", "order_side", "transaction_type")) or ""
    ).upper()
    order_type = str(_mcp_field(record, ("order_type", "type")) or "").lower()
    quantity = _mcp_quantity(record)
    price = _mcp_price(record)
    state = _mcp_state(record)
    when = _mcp_time(record)
    details: list[str] = []
    if when:
        details.append(when)
    if side:
        details.append(side)
    if order_type:
        details.append(order_type)
    if quantity is not None:
        details.append(f"qty {_mcp_quantity_text(quantity)}")
    if price is not None:
        details.append(f"@ {_mcp_money(price)}")
    if state:
        details.append(state.replace("_", " "))
    identifier = _mcp_field(record, ("order_id", "id", "uuid"))
    if identifier not in (None, ""):
        details.append(f"id {identifier}")
    return "  " + "  ".join(details)

def _robinhood_position_lines(record: dict) -> list[str]:
    quantity = _mcp_quantity(record)
    average = _mcp_number(
        _mcp_field(
            record,
            (
                "average_buy_price",
                "average_price",
                "average_cost",
                "cost_basis_per_share",
            ),
        )
    )
    last_price = _mcp_number(
        _mcp_field(
            record,
            ("last_price", "current_price", "mark_price", "price", "last_trade_price"),
        )
    )
    market_value = _mcp_number(
        _mcp_field(record, ("market_value", "equity", "current_value", "value"))
    )
    cost_basis = _mcp_number(_mcp_field(record, ("cost_basis", "total_cost")))
    lines: list[str] = []
    if quantity is not None:
        lines.append(f"  Quantity: {_mcp_quantity_text(quantity)}")
    if average is not None:
        lines.append(f"  Average price: {_mcp_money(average)}")
    if last_price is not None:
        lines.append(f"  Last price: {_mcp_money(last_price)}")
    if market_value is not None:
        lines.append(f"  Market value: {_mcp_money(market_value)}")
    if cost_basis is not None:
        lines.append(f"  Cost basis: {_mcp_money(cost_basis)}")
    if not lines:
        lines.append("  (the MCP server returned no holding fields)")
    return lines

def _robinhood_average_from_history(records: list[dict]) -> float | None:
    quantity = 0.0
    cost = 0.0
    for record in records:
        side = str(
            _mcp_field(record, ("side", "order_side", "transaction_type")) or ""
        ).lower()
        if side not in ("buy", "bought", "buy_to_open"):
            continue
        if _mcp_state(record) in _ROBINHOOD_OPEN_STATES:
            continue
        record_quantity = _mcp_quantity(record)
        record_price = _mcp_price(record)
        if record_quantity is None or record_price is None:
            continue
        quantity += record_quantity
        cost += record_quantity * record_price
    if quantity <= 0:
        return None
    return cost / quantity

def robinhood_holdings_report(symbol: str) -> str:
    """Return the /robinhood report for one symbol from a Robinhood MCP."""
    symbol = str(symbol or "").strip().upper().lstrip("$")
    if not symbol:
        raise RobinhoodMCPError("Usage: /robinhood <stock_symbol>")
    config = _robinhood_mcp_config()
    with _RobinhoodMCPClient(config) as client:
        tools = client.tools()
        if not tools:
            raise RobinhoodMCPError(
                "the Robinhood MCP server exposed no tools. Check its login "
                "state and credentials."
            )
        tools_used: list[str] = []
        raw_texts: list[str] = []

        account = ""
        account_tool = _mcp_pick_tool(tools, _ACCOUNT_TOOL_CANDIDATES)
        needs_account = False
        for candidate in (
            _mcp_pick_tool(tools, _POSITION_TOOL_CANDIDATES),
            _mcp_pick_tool(tools, _OPEN_ORDER_TOOL_CANDIDATES),
            _mcp_pick_tool(tools, _ORDER_HISTORY_TOOL_CANDIDATES),
        ):
            if not candidate:
                continue
            required = _mcp_tool_schema(candidate).get("required") or []
            if any(_robinhood_norm_key(item) == "accountnumber" for item in required):
                needs_account = True
        if account_tool and needs_account:
            try:
                payload, raw_text, name = _mcp_call_tool(
                    client, account_tool, symbol, ""
                )
                raw_texts.append(raw_text)
                account = _mcp_first_account_number(payload)
                tools_used.append(name)
            except RobinhoodMCPError:
                account = ""

        positions: list[dict] = []
        position_tool = _mcp_pick_tool(tools, _POSITION_TOOL_CANDIDATES)
        if position_tool:
            payload, raw_text, name = _mcp_call_tool(
                client, position_tool, symbol, account
            )
            raw_texts.append(raw_text)
            tools_used.append(name)
            records = _mcp_position_records(payload)
            matched = [
                record
                for record in records
                if _mcp_symbol_matches(record, symbol)
            ]
            positions = matched or (records if len(records) == 1 else [])

        pending: list[dict] = []
        history: list[dict] = []
        open_tool = _mcp_pick_tool(tools, _OPEN_ORDER_TOOL_CANDIDATES)
        history_tool = _mcp_pick_tool(tools, _ORDER_HISTORY_TOOL_CANDIDATES)
        if open_tool:
            payload, raw_text, name = _mcp_call_tool(
                client, open_tool, symbol, account
            )
            raw_texts.append(raw_text)
            tools_used.append(name)
            records = _mcp_order_records(payload)
            with_symbols = [
                record for record in records if _mcp_symbol(record)
            ]
            matched = [
                record
                for record in records
                if _mcp_symbol_matches(record, symbol)
                or (not with_symbols)
            ]
            if history_tool is None or history_tool is open_tool:
                # One generic get_orders tool may return both open and closed
                # orders, so classify each record by its state.
                for record in matched:
                    state = _mcp_state(record)
                    if state in _ROBINHOOD_CLOSED_STATES:
                        history.append(record)
                    else:
                        pending.append(record)
            else:
                pending.extend(matched)

        if history_tool and history_tool is not open_tool:
            payload, raw_text, name = _mcp_call_tool(
                client, history_tool, symbol, account
            )
            raw_texts.append(raw_text)
            tools_used.append(name)
            records = _mcp_order_records(payload)
            with_symbols = [
                record for record in records if _mcp_symbol(record)
            ]
            matched = [
                record
                for record in records
                if _mcp_symbol_matches(record, symbol)
                or (not with_symbols)
            ]
            for record in matched:
                state = _mcp_state(record)
                if state in _ROBINHOOD_OPEN_STATES:
                    pending.append(record)
                else:
                    history.append(record)

        pending = _mcp_dedupe(pending)
        history = _mcp_dedupe(history)
        pending.sort(key=_mcp_sort_key, reverse=True)
        history.sort(key=_mcp_sort_key, reverse=True)

        # If no tool produced records but a server returned a plain-text table,
        # show that response instead of pretending there is no data.
        average = None
        if positions:
            average = _mcp_number(
                _mcp_field(
                    positions[0],
                    (
                        "average_buy_price",
                        "average_price",
                        "average_cost",
                        "cost_basis_per_share",
                    ),
                )
            )
        if average is None:
            average = _robinhood_average_from_history(history)

    lines = [f"Robinhood · {symbol}", ""]
    lines.append("Holdings")
    if positions:
        for index, record in enumerate(positions):
            if index:
                lines.append("")
            lines.extend(_robinhood_position_lines(record))
        if average is not None and not any(
            _mcp_field(
                record,
                (
                    "average_buy_price",
                    "average_price",
                    "average_cost",
                    "cost_basis_per_share",
                ),
            )
            for record in positions
        ):
            lines.append(f"  Average price (from recent buy fills): {_mcp_money(average)}")
    else:
        lines.append(f"  No open position found for {symbol}.")

    lines.extend(["", f"Pending orders ({len(pending)})"])
    if pending:
        lines.extend(
            _robinhood_order_line(record)
            for record in pending[:ROBINHOOD_MCP_PENDING_LIMIT]
        )
        if len(pending) > ROBINHOOD_MCP_PENDING_LIMIT:
            lines.append(
                "  ... "
                f"{len(pending) - ROBINHOOD_MCP_PENDING_LIMIT} more pending order(s)"
            )
    else:
        lines.append("  None.")

    lines.extend(["", f"Recent transactions ({len(history)})"])
    if history:
        lines.extend(
            _robinhood_order_line(record)
            for record in history[:ROBINHOOD_MCP_HISTORY_LIMIT]
        )
        if len(history) > ROBINHOOD_MCP_HISTORY_LIMIT:
            lines.append(
                "  ... "
                f"{len(history) - ROBINHOOD_MCP_HISTORY_LIMIT} more transaction(s)"
            )
    else:
        lines.append("  None.")

    if not positions and not pending and not history:
        text = next((item for item in raw_texts if item.strip()), "")
        if text and not text.lstrip().startswith(("{", "[")):
            lines.extend(["", "MCP response", text[:4000]])
        elif not position_tool and not open_tool and not history_tool:
            available = ", ".join(
                str(tool.get("name")) for tool in tools[:20] if tool.get("name")
            )
            lines.extend(
                [
                    "",
                    "The Robinhood MCP server has no position, order, or history "
                    "tool. Available tools: " + (available or "(none)"),
                ]
            )

    if tools_used:
        lines.extend(
            [
                "",
                "Source: Robinhood MCP ("
                + ", ".join(dict.fromkeys(tools_used))
                + ")",
            ]
        )
    return "\n".join(lines)

_ZILLOW_CACHE: dict[tuple, tuple[float, dict]] = {}
