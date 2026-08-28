"""Source parsing, merging, and the result cache."""

from __future__ import annotations

import base64

import pytest

from transmission_mcp.search import TorrentSearch, UnknownSourceError
from transmission_mcp.search.common import (
    build_magnet,
    hash_from_magnet,
    human_size,
    normalize_info_hash,
    parse_date,
    parse_size,
    trackers_from_magnet,
)
from transmission_mcp.search.registry import ResultCache
from transmission_mcp.search.sources import SOURCES

HASH_A = "4a3f5e08bcef825718eda30637230585e3330599"
HASH_B = "88c480f8e6b52c57fbe11c55701003f7152d0abb"

APIBAY = [
    {
        "id": "1",
        "name": "Ubuntu 24.04 LTS",
        "info_hash": HASH_A.upper(),
        "seeders": "53",
        "leechers": "7",
        "size": "6205698048",
        "added": "1725782400",
        "category": "300",
    },
    # The "no results" placeholder row apibay returns for a miss.
    {"id": "0", "name": "No results returned", "info_hash": "0" * 40, "seeders": "0"},
]

NYAA_RSS = f"""<rss><channel>
<item>
<title>[Judas] Sousou no Frieren</title>
<pubDate>Wed, 19 Aug 2026 00:36:55 -0000</pubDate>
<nyaa:seeders>456</nyaa:seeders>
<nyaa:leechers>8</nyaa:leechers>
<nyaa:downloads>1200</nyaa:downloads>
<nyaa:infoHash>{HASH_B}</nyaa:infoHash>
<nyaa:category>Anime - English-translated</nyaa:category>
<nyaa:size>5.50 GiB</nyaa:size>
</item>
</channel></rss>"""


async def test_apibay_skips_the_no_results_placeholder(indexers):
    indexers.add("apibay.org", APIBAY)
    search = indexers.search(sources=["thepiratebay"])

    results, report = await search.search("ubuntu")
    await search.aclose()

    assert report == {"thepiratebay": {"ok": True, "results": 1}}
    assert len(results) == 1
    result = results[0]
    assert result.info_hash == HASH_A  # uppercase from the API, lowercased here
    assert result.seeders == 53
    assert result.size_bytes == 6205698048
    assert result.published == "2024-09-08"
    assert result.category == "Applications"


async def test_nyaa_parses_its_rss_namespace(indexers):
    indexers.add("nyaa.si", NYAA_RSS)
    search = indexers.search(sources=["nyaa"])

    results, _ = await search.search("frieren")
    await search.aclose()

    assert len(results) == 1
    result = results[0]
    assert result.info_hash == HASH_B
    assert result.seeders == 456 and result.leechers == 8
    assert result.completed == 1200
    assert result.size_bytes == int(5.5 * 1024**3)
    assert result.published == "2026-08-19"


async def test_subsplease_base32_magnets_become_hex_hashes(indexers):
    b32 = base64.b32encode(bytes.fromhex(HASH_A)).decode()
    indexers.add(
        "subsplease.org",
        {
            "Show - 01": {
                "show": "Show",
                "episode": "01",
                "release_date": "Fri, 29 May 2026 17:05:35 +0000",
                "downloads": [
                    {"res": "480", "magnet": f"magnet:?xt=urn:btih:{b32}&xl=100"},
                    {"res": "1080", "magnet": f"magnet:?xt=urn:btih:{b32}&xl=999&tr=udp://t.test"},
                ],
            }
        },
    )
    search = indexers.search(sources=["subsplease"])

    results, _ = await search.search("show")
    await search.aclose()

    assert len(results) == 1
    result = results[0]
    assert result.info_hash == HASH_A  # base32 decoded to hex
    assert result.name == "Show - 01 [1080p]"  # 1080p preferred over 480p
    assert result.size_bytes == 999
    assert result.trackers == ("udp://t.test",)


async def test_eztv_filters_recent_releases_client_side(indexers):
    indexers.add(
        "eztvx.to",
        {
            "torrents": [
                {"hash": HASH_A, "filename": "Some.Show.S01E01.1080p", "seeds": 10, "peers": 1},
                {"hash": HASH_B, "filename": "Other.Show.S02E03.720p", "seeds": 5, "peers": 0},
            ]
        },
    )
    search = indexers.search(sources=["eztv"])

    results, _ = await search.search("some show s01e01")
    await search.aclose()

    assert [r.info_hash for r in results] == [HASH_A]


async def test_bittorrented_ignores_queries_under_three_characters(indexers):
    indexers.add("bittorrented.com", {"results": []})
    search = indexers.search(sources=["bittorrented"])

    results, report = await search.search("ab")
    await search.aclose()

    assert results == [] and report["bittorrented"]["ok"] is True
    assert indexers.requests == []  # never left the process


async def test_a_failing_source_is_reported_not_fatal(indexers):
    indexers.add("apibay.org", APIBAY)
    # nyaa.si has no route, so the fake serves it a 503.
    search = indexers.search(sources=["thepiratebay", "nyaa"])

    results, report = await search.search("ubuntu")
    await search.aclose()

    assert len(results) == 1
    assert report["thepiratebay"]["ok"] is True
    assert report["nyaa"]["ok"] is False and "503" in report["nyaa"]["error"]


async def test_results_dedupe_across_sources_and_sort_by_health(indexers):
    indexers.add("apibay.org", APIBAY)
    indexers.add("nyaa.si", NYAA_RSS.replace(HASH_B, HASH_A))  # same torrent, other source
    indexers.add(
        "eztvx.to",
        {"torrents": [{"hash": HASH_B, "filename": "ubuntu thing", "seeds": 900, "peers": 2}]},
    )
    search = indexers.search(sources=["thepiratebay", "nyaa", "eztv"])

    results, _ = await search.search("ubuntu")
    await search.aclose()

    assert [r.info_hash for r in results] == [HASH_B, HASH_A]  # 900 seeds first
    assert len({r.info_hash for r in results}) == 2  # HASH_A appeared twice, kept once


async def test_unknown_sources_are_rejected_at_construction():
    with pytest.raises(UnknownSourceError, match="tarpit"):
        TorrentSearch(sources=["nyaa", "tarpit"])


async def test_per_call_sources_must_be_enabled(indexers):
    search = indexers.search(sources=["nyaa"])
    with pytest.raises(UnknownSourceError, match="disabled"):
        await search.search("x", ["yts"])
    await search.aclose()


async def test_excluded_sources_drop_out_of_the_enabled_set(indexers):
    search = indexers.search(exclude=["nyaa", "yts"])
    enabled = search.enabled
    await search.aclose()

    assert "nyaa" not in enabled and "yts" not in enabled
    assert len(enabled) == len(SOURCES) - 2


def test_cache_returns_results_then_expires_them():
    from transmission_mcp.search.common import SearchResult

    cache = ResultCache(ttl=900)
    cache.put([SearchResult(info_hash=HASH_A, name="thing", source="nyaa")])
    assert cache.get(HASH_A.upper()).name == "thing"  # lookup is case-insensitive

    expired = ResultCache(ttl=0)
    expired.put([SearchResult(info_hash=HASH_A, name="thing", source="nyaa")])
    assert expired.get(HASH_A) is None


@pytest.mark.parametrize(
    "value,expected",
    [
        (HASH_A.upper(), HASH_A),
        ("0" * 40, None),
        (base64.b32encode(bytes.fromhex(HASH_A)).decode(), HASH_A),
        ("not-a-hash", None),
        ("", None),
        (None, None),
    ],
)
def test_info_hash_normalization(value, expected):
    assert normalize_info_hash(value) == expected


@pytest.mark.parametrize(
    "value,expected",
    [
        ("5.50 GiB", int(5.5 * 1024**3)),
        ("700 MB", 700 * 1024**2),
        (1024, 1024),
        ("1024", 1024),
        (0, None),
        ("unknown", None),
        (None, None),
    ],
)
def test_size_parsing(value, expected):
    assert parse_size(value) == expected


@pytest.mark.parametrize(
    "value,expected",
    [
        (1700000000, "2023-11-14"),
        ("2026-01-26T16:49:05.927426+00:00", "2026-01-26"),
        ("Wed, 19 Aug 2026 00:36:55 -0000", "2026-08-19"),
        (0, None),
        ("someday", None),
    ],
)
def test_date_parsing(value, expected):
    assert parse_date(value) == expected


def test_magnet_round_trips_through_its_hash():
    magnet = build_magnet(HASH_A, "some name")
    assert hash_from_magnet(magnet) == HASH_A
    assert "dn=some%20name" in magnet
    assert len(trackers_from_magnet(magnet)) > 5


@pytest.mark.parametrize(
    "value,expected", [(0, None), (None, None), (512, "512 B"), (1536, "1.50 KiB")]
)
def test_human_size(value, expected):
    assert human_size(value) == expected
