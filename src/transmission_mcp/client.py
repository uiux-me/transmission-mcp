"""Minimal async client for Transmission's RPC API.

Transmission listens at ``<base-url>/transmission/rpc`` and protects itself
against CSRF with an ``X-Transmission-Session-Id`` header: the first request
of a session gets an HTTP 409 carrying the correct id, and the client is
expected to store it and retry. That handshake is the only real subtlety here.

This speaks the classic ``{"method", "arguments", "tag"}`` protocol with
kebab/camelCase keys. Transmission 4.1 added a JSON-RPC 2.0 flavour with
snake_case keys but still accepts the classic one, so using it keeps a single
code path working from Transmission 2.80 through 4.1+.
"""

from __future__ import annotations

import itertools
from typing import Any

import httpx

SESSION_ID_HEADER = "X-Transmission-Session-Id"


class TransmissionError(RuntimeError):
    """Raised when Transmission cannot be reached or returns a failed result."""


class TransmissionAuthError(TransmissionError):
    """Raised when Transmission rejects the configured credentials."""


class TransmissionClient:
    """Thin wrapper around Transmission's RPC endpoint."""

    def __init__(
        self,
        rpc_url: str,
        username: str | None = None,
        password: str | None = None,
        timeout: float = 30.0,
        verify_ssl: bool = True,
        transport: httpx.AsyncBaseTransport | None = None,
    ) -> None:
        self._rpc_url = rpc_url
        self._tags = itertools.count(1)
        self._session_id: str | None = None
        self._client = httpx.AsyncClient(
            auth=httpx.BasicAuth(username, password) if username or password else None,
            timeout=timeout,
            verify=verify_ssl,
            headers={"Content-Type": "application/json"},
            follow_redirects=True,
            transport=transport,
        )

    @property
    def rpc_url(self) -> str:
        return self._rpc_url

    async def call(self, method: str, **arguments: Any) -> dict[str, Any]:
        """Invoke an RPC method and return its ``arguments`` object."""
        payload: dict[str, Any] = {"method": method, "tag": next(self._tags)}
        if arguments:
            payload["arguments"] = {
                key: value for key, value in arguments.items() if value is not None
            }

        response = await self._post(payload)
        # A 409 means our session id is missing or stale: take the fresh one and retry once.
        if response.status_code == 409:
            new_id = response.headers.get(SESSION_ID_HEADER)
            if not new_id:
                raise TransmissionError(
                    f"Transmission returned HTTP 409 without a {SESSION_ID_HEADER} header; "
                    "the URL may point at something that is not Transmission's RPC endpoint."
                )
            self._session_id = new_id
            response = await self._post(payload)

        if response.status_code in (401, 403):
            raise TransmissionAuthError(
                f"Transmission rejected the credentials (HTTP {response.status_code}). "
                "Check TRANSMISSION_USERNAME/TRANSMISSION_PASSWORD."
            )
        if response.status_code == 421:
            raise TransmissionError(
                "Transmission refused the request host (HTTP 421, DNS-rebinding "
                "protection). Add this host to Transmission's rpc-host-whitelist, "
                "or set rpc-host-whitelist-enabled=false."
            )
        if response.status_code >= 400:
            raise TransmissionError(
                f"Transmission returned HTTP {response.status_code} for method "
                f"{method!r}: {response.text[:500]}"
            )

        try:
            body = response.json()
        except ValueError as exc:
            raise TransmissionError(
                f"Transmission returned a non-JSON response for method {method!r}: "
                f"{response.text[:500]}"
            ) from exc

        result = body.get("result")
        if result != "success":
            raise TransmissionError(
                f"Transmission failed method {method!r}: {result or 'unknown error'}"
            )
        return body.get("arguments") or {}

    async def _post(self, payload: dict[str, Any]) -> httpx.Response:
        headers = {SESSION_ID_HEADER: self._session_id} if self._session_id else {}
        try:
            return await self._client.post(self._rpc_url, json=payload, headers=headers)
        except httpx.HTTPError as exc:
            raise TransmissionError(
                f"Could not reach Transmission at {self._rpc_url}: {exc}"
            ) from exc

    async def aclose(self) -> None:
        await self._client.aclose()
