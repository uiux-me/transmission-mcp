"""Environment-driven configuration for the Transmission MCP server."""

from __future__ import annotations

import os
from dataclasses import dataclass, field
from urllib.parse import urlsplit, urlunsplit

DEFAULT_PORT = 9091
DEFAULT_RPC_PATH = "/transmission/rpc"

_TRUE = {"1", "true", "yes", "on"}
_FALSE = {"0", "false", "no", "off"}


def _env_bool(name: str, default: bool) -> bool:
    raw = os.getenv(name)
    if raw is None or not raw.strip():
        return default
    value = raw.strip().lower()
    if value in _TRUE:
        return True
    if value in _FALSE:
        return False
    raise ValueError(f"{name} must be a boolean (got {raw!r})")


def _env_int(name: str, default: int) -> int:
    raw = os.getenv(name)
    if raw is None or not raw.strip():
        return default
    try:
        return int(raw.strip())
    except ValueError as exc:
        raise ValueError(f"{name} must be an integer (got {raw!r})") from exc


def _env_float(name: str, default: float) -> float:
    raw = os.getenv(name)
    if raw is None or not raw.strip():
        return default
    try:
        return float(raw.strip())
    except ValueError as exc:
        raise ValueError(f"{name} must be a number (got {raw!r})") from exc


def _env_list(name: str) -> list[str]:
    raw = os.getenv(name) or ""
    return [item.strip() for item in raw.split(",") if item.strip()]


@dataclass(frozen=True)
class Settings:
    """Everything the server needs to reach Transmission, search, and expose itself."""

    # Transmission
    rpc_url: str
    username: str | None
    password: str | None
    timeout: float
    verify_ssl: bool
    download_dir: str | None

    # Safety
    read_only: bool
    allow_remove_data: bool
    allow_shutdown: bool

    # Torrent search
    search_enabled: bool
    search_sources: list[str] = field(default_factory=list)
    search_exclude: list[str] = field(default_factory=list)
    search_timeout: float = 20.0
    search_cache_ttl: int = 900
    trackers_url: str | None = None

    # MCP
    transport: str = "http"
    host: str = "0.0.0.0"
    port: int = 8000
    path: str = "/mcp"

    @property
    def base_url(self) -> str:
        """The RPC URL without the RPC path, useful for messages."""
        parts = urlsplit(self.rpc_url)
        return urlunsplit((parts.scheme, parts.netloc, "", "", ""))


def _resolve_rpc_url() -> tuple[str, str | None, str | None]:
    """Return (rpc_url, username, password) — credentials only if embedded in the URL."""
    raw = os.getenv("TRANSMISSION_URL", "").strip()
    if not raw:
        scheme = os.getenv("TRANSMISSION_SCHEME", "http").strip() or "http"
        host = os.getenv("TRANSMISSION_HOST", "localhost").strip() or "localhost"
        port = _env_int("TRANSMISSION_PORT", DEFAULT_PORT)
        raw = f"{scheme}://{host}:{port}"

    if "://" not in raw:
        raw = f"http://{raw}"

    parts = urlsplit(raw)
    if not parts.hostname:
        raise ValueError(f"TRANSMISSION_URL is missing a hostname: {raw!r}")

    netloc = parts.hostname
    if ":" in netloc:  # IPv6 literal
        netloc = f"[{netloc}]"
    if parts.port:
        netloc = f"{netloc}:{parts.port}"

    # Accept both "http://host:9091" and a full "http://host:9091/transmission/rpc",
    # and keep any sub-path a reverse proxy mounts Transmission under.
    rpc_path = parts.path.rstrip("/")
    if not rpc_path.endswith("/rpc"):
        rpc_path = f"{rpc_path}{DEFAULT_RPC_PATH}"

    rpc_url = urlunsplit((parts.scheme, netloc, rpc_path, "", ""))
    return rpc_url, parts.username, parts.password


def load_settings() -> Settings:
    """Build settings from the environment, raising ValueError on bad input."""
    rpc_url, url_user, url_password = _resolve_rpc_url()

    transport = (os.getenv("MCP_TRANSPORT") or "http").strip().lower()
    if transport not in {"stdio", "http", "sse", "streamable-http"}:
        raise ValueError(
            f"MCP_TRANSPORT must be one of stdio/http/sse/streamable-http (got {transport!r})"
        )

    download_dir = (os.getenv("TRANSMISSION_DOWNLOAD_DIR") or "").strip()

    return Settings(
        rpc_url=rpc_url,
        username=os.getenv("TRANSMISSION_USERNAME") or url_user or None,
        password=os.getenv("TRANSMISSION_PASSWORD") or url_password or None,
        timeout=_env_float("TRANSMISSION_TIMEOUT", 30.0),
        verify_ssl=_env_bool("TRANSMISSION_VERIFY_SSL", True),
        download_dir=download_dir or None,
        read_only=_env_bool("TRANSMISSION_READ_ONLY", False),
        allow_remove_data=_env_bool("TRANSMISSION_ALLOW_REMOVE_DATA", False),
        allow_shutdown=_env_bool("TRANSMISSION_ALLOW_SHUTDOWN", False),
        search_enabled=_env_bool("SEARCH_ENABLED", True),
        search_sources=_env_list("SEARCH_SOURCES"),
        search_exclude=_env_list("SEARCH_EXCLUDE_SOURCES"),
        search_timeout=_env_float("SEARCH_TIMEOUT", 20.0),
        search_cache_ttl=_env_int("SEARCH_CACHE_TTL", 900),
        trackers_url=(os.getenv("SEARCH_TRACKERS_URL") or "").strip() or None,
        transport=transport,
        host=os.getenv("MCP_HOST", "0.0.0.0").strip() or "0.0.0.0",
        port=_env_int("MCP_PORT", 8000),
        path=os.getenv("MCP_PATH", "/mcp").strip() or "/mcp",
    )
