"""One fetcher per public torrent indexer.

Each fetcher takes a query and returns :class:`SearchResult` objects. They are
all plain HTTP — JSON APIs and RSS feeds — so the container needs no headless
browser. Two indexers the reference project crawls, 1337x and The Pirate Bay's
HTML site, sit behind Cloudflare and refuse non-browser clients; TPB's content
is reached here through its own JSON API at apibay.org instead.

Every fetcher is best-effort: a source that is down, blocked, or has changed
shape raises, and :mod:`.registry` drops it from that search.
"""

from __future__ import annotations

import asyncio
import re
from typing import Any

from .common import (
    Fetcher,
    SearchResult,
    hash_from_magnet,
    normalize_info_hash,
    parse_date,
    parse_size,
    rss_field,
    size_from_magnet,
    trackers_from_magnet,
)

# ---------------------------------------------------------------------------
# apibay.org - The Pirate Bay's JSON API
# ---------------------------------------------------------------------------
APIBAY_CATEGORIES = {
    100: "Audio",
    101: "Audio - Music",
    102: "Audio - Audio books",
    200: "Video",
    201: "Video - Movies",
    202: "Video - Movies DVDR",
    205: "Video - TV shows",
    207: "Video - Movies HD",
    208: "Video - Movies HD x265",
    209: "Video - 3D",
    300: "Applications",
    400: "Games",
    600: "Other",
}


def _apibay_category(raw: Any) -> str:
    try:
        code = int(raw)
    except (TypeError, ValueError):
        return "Other"
    return APIBAY_CATEGORIES.get(code) or APIBAY_CATEGORIES.get(code // 100 * 100, "Other")


async def apibay(fetcher: Fetcher, query: str) -> list[SearchResult]:
    if query:
        data = await fetcher.json("https://apibay.org/q.php", {"q": query})
    else:
        pages = await asyncio.gather(
            fetcher.json("https://apibay.org/precompiled/data_top100_207.json"),
            fetcher.json("https://apibay.org/precompiled/data_top100_208.json"),
            return_exceptions=True,
        )
        data = [item for page in pages if isinstance(page, list) for item in page]

    results = []
    for item in data if isinstance(data, list) else []:
        # A miss is reported as a single placeholder row with id "0".
        info_hash = normalize_info_hash(item.get("info_hash"))
        if not info_hash or str(item.get("id")) == "0":
            continue
        results.append(
            SearchResult(
                info_hash=info_hash,
                name=item.get("name") or info_hash,
                source="thepiratebay",
                category=_apibay_category(item.get("category")),
                size_bytes=parse_size(item.get("size")),
                seeders=int(item.get("seeders") or 0),
                leechers=int(item.get("leechers") or 0),
                published=parse_date(item.get("added")),
            )
        )
    return results


# ---------------------------------------------------------------------------
# yts - JSON API for movies
# ---------------------------------------------------------------------------
# yts.mx stopped resolving; these are the hosts the API itself now redirects to.
YTS_HOSTS = ["yts.gg", "movies-api.accel.li", "yts.am"]


async def yts(fetcher: Fetcher, query: str) -> list[SearchResult]:
    import json as json_mod

    params = {"limit": "50"}
    if query:
        params["query_term"] = query
    else:
        params["sort_by"] = "date_added"
    data = json_mod.loads(
        await fetcher.first_of(YTS_HOSTS, "/api/v2/list_movies.json", params)
    )

    results = []
    for movie in (data.get("data") or {}).get("movies") or []:
        title = movie.get("title_long") or movie.get("title") or "Unknown"
        for torrent in movie.get("torrents") or []:
            info_hash = normalize_info_hash(torrent.get("hash"))
            if not info_hash:
                continue
            tag = " ".join(
                part for part in (torrent.get("quality"), torrent.get("type")) if part
            )
            results.append(
                SearchResult(
                    info_hash=info_hash,
                    name=f"{title} [{tag}]" if tag else title,
                    source="yts",
                    category="Video - Movies",
                    size_bytes=parse_size(torrent.get("size_bytes")),
                    seeders=int(torrent.get("seeds") or 0),
                    leechers=int(torrent.get("peers") or 0),
                    published=parse_date(movie.get("date_uploaded_unix")),
                )
            )
    return results


# ---------------------------------------------------------------------------
# nyaa.si - anime, RSS feed
# ---------------------------------------------------------------------------
async def nyaa(fetcher: Fetcher, query: str) -> list[SearchResult]:
    xml = await fetcher.text(
        "https://nyaa.si/", {"page": "rss", "q": query, "c": "0_0", "f": "0"}
    )
    results = []
    for item in xml.split("<item>")[1:]:
        info_hash = normalize_info_hash(rss_field(item, "nyaa:infoHash"))
        name = rss_field(item, "title")
        if not info_hash or not name:
            continue
        downloads = rss_field(item, "nyaa:downloads")
        results.append(
            SearchResult(
                info_hash=info_hash,
                name=name,
                source="nyaa",
                category=rss_field(item, "nyaa:category") or "Anime",
                size_bytes=parse_size(rss_field(item, "nyaa:size")),
                seeders=int(rss_field(item, "nyaa:seeders") or 0),
                leechers=int(rss_field(item, "nyaa:leechers") or 0),
                completed=int(downloads) if downloads.isdigit() else None,
                published=parse_date(rss_field(item, "pubDate")),
            )
        )
    return results


# ---------------------------------------------------------------------------
# eztvx.to - TV, JSON API
# ---------------------------------------------------------------------------
EZTV_PAGES = 2
EZTV_PAGE_SIZE = 100


async def eztv(fetcher: Fetcher, query: str) -> list[SearchResult]:
    # The API has no text search — it only pages through recent releases — so
    # the query is matched client-side over the newest few hundred entries.
    pages = await asyncio.gather(
        *(
            fetcher.json(
                "https://eztvx.to/api/get-torrents",
                {"limit": str(EZTV_PAGE_SIZE), "page": str(page)},
            )
            for page in range(1, EZTV_PAGES + 1)
        ),
        return_exceptions=True,
    )

    tokens = [token for token in query.lower().split() if token]
    results = []
    for page in pages:
        if not isinstance(page, dict):
            continue
        for torrent in page.get("torrents") or []:
            info_hash = normalize_info_hash(torrent.get("hash"))
            name = torrent.get("filename") or torrent.get("title")
            if not info_hash or not name:
                continue
            if tokens and not all(token in name.lower() for token in tokens):
                continue
            results.append(
                SearchResult(
                    info_hash=info_hash,
                    name=name,
                    source="eztv",
                    category="Video - TV shows",
                    size_bytes=parse_size(torrent.get("size_bytes")),
                    seeders=int(torrent.get("seeds") or 0),
                    leechers=int(torrent.get("peers") or 0),
                    published=parse_date(torrent.get("date_released_unix")),
                    trackers=trackers_from_magnet(torrent.get("magnet_url")),
                )
            )
    return results


# ---------------------------------------------------------------------------
# subsplease.org - anime, JSON API
# ---------------------------------------------------------------------------
RESOLUTION_PREFERENCE = ("1080", "720", "480")


def _best_download(downloads: list[dict[str, Any]]) -> dict[str, Any] | None:
    for resolution in RESOLUTION_PREFERENCE:
        for download in downloads:
            if download.get("res") == resolution and download.get("magnet"):
                return download
    return next((d for d in downloads if d.get("magnet")), None)


async def subsplease(fetcher: Fetcher, query: str) -> list[SearchResult]:
    params = {"tz": "UTC", "f": "search", "s": query} if query else {"tz": "UTC", "f": "latest"}
    data = await fetcher.json("https://subsplease.org/api/", params)
    # A miss returns an empty list rather than an empty object.
    entries = data.values() if isinstance(data, dict) else []

    results = []
    for entry in entries:
        download = _best_download(entry.get("downloads") or [])
        if not download:
            continue
        magnet = download["magnet"]
        info_hash = hash_from_magnet(magnet)
        if not info_hash:
            continue
        show = entry.get("show") or "Unknown"
        episode = f" - {entry['episode']}" if entry.get("episode") else ""
        results.append(
            SearchResult(
                info_hash=info_hash,
                name=f"{show}{episode} [{download.get('res') or '?'}p]",
                source="subsplease",
                category="Anime",
                size_bytes=size_from_magnet(magnet),
                published=parse_date(entry.get("release_date")),
                trackers=trackers_from_magnet(magnet),
            )
        )
    return results


# ---------------------------------------------------------------------------
# fitgirl-repacks.site - games, WordPress RSS
# ---------------------------------------------------------------------------
_MAGNET_HREF = re.compile(r'href="(magnet:\?xt=urn:btih:[^"]+)"', re.IGNORECASE)


async def fitgirl(fetcher: Fetcher, query: str) -> list[SearchResult]:
    from urllib.parse import quote

    base = "https://fitgirl-repacks.site"
    url = f"{base}/?s={quote(query, safe='')}&feed=rss2" if query else f"{base}/feed/"
    xml = await fetcher.text(url)

    results = []
    for item in xml.split("<item>")[1:]:
        match = _MAGNET_HREF.search(item)
        if not match:
            continue
        import html as html_mod

        magnet = html_mod.unescape(match.group(1))
        info_hash = hash_from_magnet(magnet)
        if not info_hash:
            continue
        results.append(
            SearchResult(
                info_hash=info_hash,
                name=rss_field(item, "title") or info_hash,
                source="fitgirl",
                category="Games",
                published=parse_date(rss_field(item, "pubDate")),
                trackers=trackers_from_magnet(magnet),
            )
        )
    return results


# ---------------------------------------------------------------------------
# bittorrented.com - DHT-indexed video, JSON API
# ---------------------------------------------------------------------------
async def bittorrented(fetcher: Fetcher, query: str) -> list[SearchResult]:
    # The API rejects queries shorter than three characters.
    if len(query) < 3:
        return []
    data = await fetcher.json(
        "https://bittorrented.com/api/search/torrents",
        {
            "q": query,
            "type": "video",
            "limit": "50",
            "sortBy": "seeders",
            "sortOrder": "desc",
        },
    )

    results = []
    for item in (data.get("results") or []) if isinstance(data, dict) else []:
        info_hash = normalize_info_hash(item.get("torrent_infohash"))
        if not info_hash:
            continue
        results.append(
            SearchResult(
                info_hash=info_hash,
                name=item.get("torrent_name") or info_hash,
                source="bittorrented",
                category="Video",
                size_bytes=parse_size(item.get("torrent_total_size")),
                seeders=int(item.get("torrent_seeders") or 0),
                leechers=int(item.get("torrent_leechers") or 0),
                published=parse_date(item.get("torrent_created_at")),
            )
        )
    return results


#: Source name -> fetcher, in the order results are gathered.
SOURCES = {
    "thepiratebay": apibay,
    "yts": yts,
    "nyaa": nyaa,
    "eztv": eztv,
    "subsplease": subsplease,
    "fitgirl": fitgirl,
    "bittorrented": bittorrented,
}

#: What each source is good for, surfaced to the model so it can pick sensibly.
SOURCE_DESCRIPTIONS = {
    "thepiratebay": "General-purpose: movies, TV, music, games, software (via apibay.org)",
    "yts": "Movies only, small high-quality encodes",
    "nyaa": "Anime and Asian media",
    "eztv": "TV episodes; matches the query against recent releases only",
    "subsplease": "Currently-airing anime, fresh episodes",
    "fitgirl": "PC game repacks; no seeder counts published",
    "bittorrented": "DHT-indexed video, broad long-tail coverage",
}
