"""Container healthcheck: is the MCP port listening and Transmission reachable?"""

from __future__ import annotations

import asyncio
import socket
import sys

from .client import TransmissionClient
from .config import load_settings


async def _check() -> None:
    settings = load_settings()

    if settings.transport != "stdio":
        host = "127.0.0.1" if settings.host in ("0.0.0.0", "::") else settings.host
        with socket.create_connection((host, settings.port), timeout=5):
            pass

    client = TransmissionClient(
        settings.rpc_url,
        username=settings.username,
        password=settings.password,
        timeout=min(settings.timeout, 10.0),
        verify_ssl=settings.verify_ssl,
    )
    try:
        await client.call("session-get", fields=["version"])
    finally:
        await client.aclose()


def main() -> int:
    try:
        asyncio.run(_check())
    except Exception as exc:  # noqa: BLE001 - any failure means unhealthy
        print(f"unhealthy: {exc}", file=sys.stderr)
        return 1
    print("ok")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
