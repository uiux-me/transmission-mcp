"""A fake Transmission RPC endpoint the tests can drive."""

from __future__ import annotations

import json
from typing import Any, Callable

import httpx
import pytest

from transmission_mcp.client import SESSION_ID_HEADER, TransmissionClient
from transmission_mcp.config import Settings
from transmission_mcp.search import TorrentSearch
from transmission_mcp.search.common import Fetcher
from transmission_mcp.server import build_server


def make_settings(**overrides: Any) -> Settings:
    defaults = dict(
        rpc_url="http://transmission.test:9091/transmission/rpc",
        username=None,
        password=None,
        timeout=5.0,
        verify_ssl=True,
        download_dir=None,
        read_only=False,
        allow_remove_data=False,
        allow_shutdown=False,
        search_enabled=True,
        transport="stdio",
        host="0.0.0.0",
        port=8000,
        path="/mcp",
    )
    defaults.update(overrides)
    return Settings(**defaults)


class FakeTransmission:
    """Records calls and replies with canned results, including the CSRF handshake."""

    SESSION_ID = "fake-session-id"

    def __init__(self, results: dict[str, Any] | None = None):
        self.calls: list[tuple[str, dict[str, Any]]] = []
        self.results: dict[str, Any] = dict(results or {})
        self.status_code = 200
        self.result_status = "success"
        self.require_session_id = True
        self.handshakes = 0

    def handler(self, request: httpx.Request) -> httpx.Response:
        if self.require_session_id and request.headers.get(SESSION_ID_HEADER) != self.SESSION_ID:
            self.handshakes += 1
            return httpx.Response(409, headers={SESSION_ID_HEADER: self.SESSION_ID})

        payload = json.loads(request.content)
        method, arguments = payload["method"], payload.get("arguments", {})
        self.calls.append((method, arguments))

        if self.status_code != 200:
            return httpx.Response(self.status_code, text="denied")
        if self.result_status != "success":
            return httpx.Response(200, json={"result": self.result_status, "tag": payload.get("tag")})

        result = self.results.get(method, {})
        if isinstance(result, Callable):  # type: ignore[arg-type]
            result = result(arguments)
        return httpx.Response(
            200, json={"result": "success", "tag": payload.get("tag"), "arguments": result}
        )

    def arguments_for(self, method: str) -> dict[str, Any]:
        for name, arguments in self.calls:
            if name == method:
                return arguments
        raise AssertionError(f"{method} was never called; saw {[c[0] for c in self.calls]}")

    def methods(self) -> list[str]:
        return [name for name, _ in self.calls]

    def client(self, settings: Settings | None = None) -> TransmissionClient:
        settings = settings or make_settings()
        return TransmissionClient(
            settings.rpc_url,
            username=settings.username,
            password=settings.password,
            timeout=settings.timeout,
            transport=httpx.MockTransport(self.handler),
        )


class FakeIndexers:
    """Serves canned indexer responses over a mock transport."""

    def __init__(self) -> None:
        self.routes: dict[str, Any] = {}
        self.requests: list[httpx.Request] = []

    def add(self, host: str, body: Any) -> None:
        self.routes[host] = body

    def handler(self, request: httpx.Request) -> httpx.Response:
        self.requests.append(request)
        body = self.routes.get(request.url.host)
        if body is None:
            return httpx.Response(503, text="source down")
        if isinstance(body, Callable):  # type: ignore[arg-type]
            body = body(request)
        if isinstance(body, str):
            return httpx.Response(200, text=body)
        return httpx.Response(200, json=body)

    def search(self, **overrides: Any) -> TorrentSearch:
        fetcher = Fetcher(client=httpx.AsyncClient(transport=httpx.MockTransport(self.handler)))
        return TorrentSearch(fetcher=fetcher, **overrides)


@pytest.fixture
def fake() -> FakeTransmission:
    return FakeTransmission()


@pytest.fixture
def indexers() -> FakeIndexers:
    return FakeIndexers()


@pytest.fixture
def build(fake: FakeTransmission, indexers: FakeIndexers):
    """Build an MCP server wired to the fakes, with optional setting overrides."""

    def _build(**overrides: Any):
        search_kwargs = overrides.pop("search_kwargs", {})
        settings = make_settings(**overrides)
        search = indexers.search(**search_kwargs) if settings.search_enabled else None
        return build_server(settings, client=fake.client(settings), search=search)

    return _build
