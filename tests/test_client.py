"""The RPC client, especially the CSRF handshake."""

from __future__ import annotations

import httpx
import pytest

from transmission_mcp.client import (
    SESSION_ID_HEADER,
    TransmissionAuthError,
    TransmissionError,
)


async def test_first_call_performs_the_409_handshake_and_retries(fake):
    fake.results["session-get"] = {"version": "4.0.6"}
    client = fake.client()
    try:
        result = await client.call("session-get")
    finally:
        await client.aclose()

    assert result == {"version": "4.0.6"}
    assert fake.handshakes == 1  # exactly one 409, then the retry succeeded
    assert fake.methods() == ["session-get"]


async def test_session_id_is_reused_for_later_calls(fake):
    client = fake.client()
    try:
        await client.call("session-get")
        await client.call("session-stats")
    finally:
        await client.aclose()

    assert fake.handshakes == 1
    assert fake.methods() == ["session-get", "session-stats"]


async def test_expired_session_id_triggers_a_fresh_handshake(fake):
    client = fake.client()
    try:
        await client.call("session-get")
        fake.SESSION_ID = "rotated-id"
        await client.call("session-stats")
    finally:
        await client.aclose()

    assert fake.handshakes == 2


async def test_409_without_a_session_id_header_is_an_error():
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(409, text="nope")

    from transmission_mcp.client import TransmissionClient

    client = TransmissionClient(
        "http://not-transmission.test/transmission/rpc",
        transport=httpx.MockTransport(handler),
    )
    try:
        with pytest.raises(TransmissionError, match=SESSION_ID_HEADER):
            await client.call("session-get")
    finally:
        await client.aclose()


async def test_401_is_reported_as_an_auth_error(fake):
    fake.status_code = 401
    client = fake.client()
    try:
        with pytest.raises(TransmissionAuthError, match="TRANSMISSION_USERNAME"):
            await client.call("session-get")
    finally:
        await client.aclose()


async def test_421_explains_host_whitelisting(fake):
    fake.status_code = 421
    client = fake.client()
    try:
        with pytest.raises(TransmissionError, match="rpc-host-whitelist"):
            await client.call("session-get")
    finally:
        await client.aclose()


async def test_non_success_result_is_an_error(fake):
    fake.result_status = "invalid argument"
    client = fake.client()
    try:
        with pytest.raises(TransmissionError, match="invalid argument"):
            await client.call("torrent-get")
    finally:
        await client.aclose()


async def test_none_arguments_are_dropped_from_the_payload(fake):
    client = fake.client()
    try:
        await client.call("torrent-add", filename="magnet:?x", **{"download-dir": None})
    finally:
        await client.aclose()

    assert fake.arguments_for("torrent-add") == {"filename": "magnet:?x"}


async def test_unreachable_host_names_the_url():
    def handler(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("connection refused")

    from transmission_mcp.client import TransmissionClient

    client = TransmissionClient(
        "http://gone.test:9091/transmission/rpc", transport=httpx.MockTransport(handler)
    )
    try:
        with pytest.raises(TransmissionError, match="gone.test:9091"):
            await client.call("session-get")
    finally:
        await client.aclose()
