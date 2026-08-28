"""Shared plumbing for the torrent-source fetchers.

Every source ends up producing :class:`SearchResult` objects keyed by a 40-hex
info hash. That hash is the only identity the rest of the server needs: a
magnet URI can always be rebuilt from it, so search results stay small and
``add_torrent`` never has to be handed a kilobyte-long magnet link.
"""

from __future__ import annotations

import base64
import binascii
import json
import re
from dataclasses import dataclass
from datetime import datetime, timezone
from email.utils import parsedate_to_datetime
from typing import Any

import httpx

USER_AGENT = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/126.0.0.0 Safari/537.36"
)

# Public trackers appended to magnets rebuilt from a bare info hash. Without
# them a magnet only works for peers already reachable over DHT.
TRACKERS = (
    "udp://tracker.opentrackr.org:1337/announce",
    "udp://open.demonii.com:1337/announce",
    "udp://tracker.openbittorrent.com:6969/announce",
    "udp://tracker.torrent.eu.org:451/announce",
    "udp://exodus.desync.com:6969/announce",
    "udp://open.stealth.si:80/announce",
    "udp://tracker.dler.org:6969/announce",
    "http://tracker.opentrackr.org:1337/announce",
    "https://tracker.tamersunion.org:443/announce",
)

_HEX40 = re.compile(r"^[0-9a-f]{40}$")
_BTIH = re.compile(r"xt=urn:btih:([0-9a-zA-Z]+)", re.IGNORECASE)
_XL = re.compile(r"[?&]xl=(\d+)")


@dataclass(slots=True)
class SearchResult:
    """One torrent found on one indexer."""

    info_hash: str
    name: str
    source: str
    category: str | None = None
    size_bytes: int | None = None
    seeders: int = 0
    leechers: int = 0
    completed: int | None = None
    published: str | None = None
    trackers: tuple[str, ...] = ()

    @property
    def health(self) -> int:
        return self.seeders * 3 + self.leechers

    def magnet(self, extra_trackers: tuple[str, ...] = TRACKERS) -> str:
        """Rebuild a magnet URI from the info hash, name, and known trackers."""
        return build_magnet(self.info_hash, self.name, self.trackers or extra_trackers)

    def to_dict(self, include_magnet: bool = False) -> dict[str, Any]:
        data: dict[str, Any] = {
            "info_hash": self.info_hash,
            "name": self.name,
            "source": self.source,
            "size_bytes": self.size_bytes,
            "size_human": human_size(self.size_bytes),
            "seeders": self.seeders,
            "leechers": self.leechers,
            "published": self.published,
        }
        if self.category:
            data["category"] = self.category
        if self.completed is not None:
            data["completed"] = self.completed
        if include_magnet:
            data["magnet"] = self.magnet()
        return data


def build_magnet(
    info_hash: str, name: str | None = None, trackers: tuple[str, ...] = TRACKERS
) -> str:
    """Build a magnet URI from an info hash, display name, and tracker list."""
    from urllib.parse import quote

    magnet = f"magnet:?xt=urn:btih:{info_hash}"
    if name:
        magnet += f"&dn={quote(name, safe='')}"
    return magnet + "".join(f"&tr={quote(t, safe='')}" for t in trackers)


def normalize_info_hash(value: str | None) -> str | None:
    """Return a lowercase 40-hex info hash, decoding base32 forms on the way.

    Some indexers (SubsPlease among them) publish base32 hashes in their
    magnets; those have to become hex so results from different sources
    deduplicate against each other and against Transmission's own hashes.
    """
    if not value:
        return None
    candidate = value.strip().lower()
    if _HEX40.match(candidate):
        return None if candidate == "0" * 40 else candidate
    if len(candidate) == 32:
        try:
            raw = base64.b32decode(candidate.upper())
        except (binascii.Error, ValueError):
            return None
        return raw.hex()
    return None


def hash_from_magnet(magnet: str | None) -> str | None:
    """Pull the info hash out of a magnet URI, in whatever encoding it uses."""
    if not magnet:
        return None
    match = _BTIH.search(magnet)
    return normalize_info_hash(match.group(1)) if match else None


def trackers_from_magnet(magnet: str | None) -> tuple[str, ...]:
    """Collect the ``tr=`` announce URLs a magnet already carries."""
    if not magnet:
        return ()
    from urllib.parse import parse_qsl, urlsplit

    query = urlsplit(magnet).query or magnet.partition("?")[2]
    return tuple(value for key, value in parse_qsl(query) if key == "tr" and value)


def size_from_magnet(magnet: str | None) -> int | None:
    """Read the exact-length (``xl``) hint some magnets carry."""
    if not magnet:
        return None
    match = _XL.search(magnet)
    return int(match.group(1)) if match else None


def parse_size(value: Any) -> int | None:
    """Coerce a byte count or a human string like '5.7 GiB' into bytes."""
    if isinstance(value, bool) or value is None:
        return None
    if isinstance(value, (int, float)):
        return int(value) or None
    text = str(value).strip()
    if not text:
        return None
    if text.isdigit():
        return int(text) or None
    match = re.match(r"([\d.,]+)\s*([KMGTP]?)i?B", text, re.IGNORECASE)
    if not match:
        return None
    try:
        number = float(match.group(1).replace(",", ""))
    except ValueError:
        return None
    scale = {"": 0, "K": 1, "M": 2, "G": 3, "T": 4, "P": 5}[match.group(2).upper()]
    return int(number * (1024**scale)) or None


def human_size(num_bytes: Any) -> str | None:
    """Format a byte count the way a person would read it."""
    if not isinstance(num_bytes, (int, float)) or num_bytes <= 0:
        return None
    value = float(num_bytes)
    for unit in ("B", "KiB", "MiB", "GiB", "TiB"):
        if value < 1024 or unit == "TiB":
            return f"{int(value)} B" if unit == "B" else f"{value:.2f} {unit}"
        value /= 1024
    return None  # pragma: no cover - the TiB branch always returns


def parse_date(value: Any) -> str | None:
    """Normalize a unix timestamp, ISO-8601 or RFC-822 date to ``YYYY-MM-DD``."""
    if isinstance(value, bool) or value is None:
        return None
    if isinstance(value, (int, float)) or (isinstance(value, str) and value.isdigit()):
        stamp = int(value)
        if stamp <= 0:
            return None
        return datetime.fromtimestamp(stamp, tz=timezone.utc).strftime("%Y-%m-%d")
    text = str(value).strip()
    if not text:
        return None
    try:
        return datetime.fromisoformat(text.replace("Z", "+00:00")).strftime("%Y-%m-%d")
    except ValueError:
        pass
    try:
        return parsedate_to_datetime(text).strftime("%Y-%m-%d")
    except (TypeError, ValueError, IndexError):
        return None


def rss_field(item: str, name: str) -> str:
    """Extract one tag's text from a raw RSS ``<item>`` blob, unwrapping CDATA."""
    import html as html_mod

    match = re.search(
        rf"<{re.escape(name)}>(?:<!\[CDATA\[)?(.*?)(?:\]\]>)?</{re.escape(name)}>",
        item,
        re.DOTALL | re.IGNORECASE,
    )
    return html_mod.unescape(match.group(1).strip()) if match else ""


class Fetcher:
    """A shared httpx client handed to every source fetcher."""

    def __init__(self, timeout: float = 20.0, client: httpx.AsyncClient | None = None):
        self._owned = client is None
        self._client = client or httpx.AsyncClient(
            timeout=timeout,
            headers={"User-Agent": USER_AGENT, "Accept": "*/*"},
            follow_redirects=True,
        )

    async def text(self, url: str, params: dict[str, str] | None = None) -> str:
        response = await self._client.get(url, params=params)
        response.raise_for_status()
        return response.text

    async def json(self, url: str, params: dict[str, str] | None = None) -> Any:
        # Several of these APIs answer with text/html or text/plain content
        # types, so decode the body ourselves rather than trusting the header.
        return json.loads(await self.text(url, params))

    async def first_of(
        self, hosts: list[str], path: str, params: dict[str, str] | None = None
    ) -> str:
        """Try a mirror rotation in order, returning the first host that answers."""
        last_error: Exception | None = None
        for host in hosts:
            try:
                return await self.text(f"https://{host}{path}", params)
            except (httpx.HTTPError, ValueError) as exc:
                last_error = exc
        raise last_error or RuntimeError(f"no hosts to try for {path}")

    async def aclose(self) -> None:
        if self._owned:
            await self._client.aclose()
