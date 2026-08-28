"""Fan search out across the enabled indexers and merge what comes back."""

from __future__ import annotations

import asyncio
import logging
import time
from typing import Any

from .common import Fetcher, SearchResult
from .sources import SOURCE_DESCRIPTIONS, SOURCES

logger = logging.getLogger("transmission_mcp.search")


class UnknownSourceError(ValueError):
    """Raised when a caller names a source that does not exist."""


class ResultCache:
    """TTL cache of results by info hash, so add_torrent can recover names."""

    def __init__(self, ttl: int = 900):
        self._ttl = ttl
        self._entries: dict[str, tuple[float, SearchResult]] = {}

    def put(self, results: list[SearchResult]) -> None:
        now = time.monotonic()
        for result in results:
            self._entries[result.info_hash] = (now, result)
        self._evict(now)

    def get(self, info_hash: str) -> SearchResult | None:
        entry = self._entries.get(info_hash.lower())
        if not entry:
            return None
        stored_at, result = entry
        if time.monotonic() - stored_at > self._ttl:
            self._entries.pop(info_hash.lower(), None)
            return None
        return result

    def _evict(self, now: float) -> None:
        deadline = now - self._ttl
        self._entries = {
            key: entry for key, entry in self._entries.items() if entry[0] > deadline
        }

    def __len__(self) -> int:
        return len(self._entries)


class TorrentSearch:
    """Searches every enabled indexer in parallel and merges the results."""

    def __init__(
        self,
        sources: list[str] | None = None,
        exclude: list[str] | None = None,
        timeout: float = 20.0,
        cache_ttl: int = 900,
        fetcher: Fetcher | None = None,
    ) -> None:
        enabled = list(sources) if sources else list(SOURCES)
        unknown = [name for name in enabled + list(exclude or []) if name not in SOURCES]
        if unknown:
            raise UnknownSourceError(
                f"Unknown torrent source(s): {', '.join(sorted(unknown))}. "
                f"Available: {', '.join(SOURCES)}"
            )
        self.enabled = [name for name in enabled if name not in set(exclude or [])]
        self._fetcher = fetcher or Fetcher(timeout=timeout)
        self.cache = ResultCache(cache_ttl)

    def available_sources(self) -> list[dict[str, str]]:
        return [
            {
                "name": name,
                "description": SOURCE_DESCRIPTIONS[name],
                "enabled": name in self.enabled,
            }
            for name in SOURCES
        ]

    def resolve(self, requested: list[str] | None) -> list[str]:
        """Validate an explicit source list against what this instance enables."""
        if not requested:
            return self.enabled
        unknown = [name for name in requested if name not in SOURCES]
        if unknown:
            raise UnknownSourceError(
                f"Unknown torrent source(s): {', '.join(sorted(unknown))}. "
                f"Available: {', '.join(SOURCES)}"
            )
        disabled = [name for name in requested if name not in self.enabled]
        if disabled:
            raise UnknownSourceError(
                f"Source(s) disabled by configuration: {', '.join(sorted(disabled))}"
            )
        return requested

    async def search(
        self, query: str, sources: list[str] | None = None
    ) -> tuple[list[SearchResult], dict[str, Any]]:
        """Search the given sources, returning (results, per-source report)."""
        names = self.resolve(sources)
        query = query.strip().lower()

        gathered = await asyncio.gather(
            *(SOURCES[name](self._fetcher, query) for name in names),
            return_exceptions=True,
        )

        report: dict[str, Any] = {}
        merged: dict[str, SearchResult] = {}
        for name, outcome in zip(names, gathered):
            if isinstance(outcome, BaseException):
                logger.warning("source %s failed for %r: %s", name, query, outcome)
                report[name] = {"ok": False, "error": str(outcome)[:200]}
                continue
            report[name] = {"ok": True, "results": len(outcome)}
            for result in outcome:
                merged.setdefault(result.info_hash, result)

        results = sorted(merged.values(), key=lambda r: (r.health, r.seeders), reverse=True)
        self.cache.put(results)
        return results, report

    async def aclose(self) -> None:
        await self._fetcher.aclose()
