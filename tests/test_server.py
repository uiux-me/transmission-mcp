"""End-to-end tool calls against the fakes, driven through an MCP client."""

from __future__ import annotations

import base64

import pytest
from fastmcp import Client
from fastmcp.exceptions import ToolError

HASH_A = "4a3f5e08bcef825718eda30637230585e3330599"
HASH_B = "88c480f8e6b52c57fbe11c55701003f7152d0abb"

TORRENTS = [
    {
        "id": 1,
        "name": "Ubuntu 24.04",
        "hashString": HASH_A,
        "status": 4,
        "percentDone": 0.5,
        "sizeWhenDone": 2000,
        "leftUntilDone": 1000,
        "rateDownload": 1_048_576,
        "eta": 120,
        "error": 0,
        "addedDate": 1700000000,
        "labels": [],
    },
    {
        "id": 2,
        "name": "Some Show S01E01",
        "hashString": HASH_B,
        "status": 0,
        "percentDone": 1.0,
        "sizeWhenDone": 500,
        "leftUntilDone": 0,
        "error": 0,
        "addedDate": 1700000001,
        "labels": ["tv"],
    },
]

APIBAY = [
    {
        "id": "1",
        "name": "Ubuntu 24.04 LTS",
        "info_hash": HASH_A,
        "seeders": "53",
        "leechers": "7",
        "size": "6205698048",
        "added": "1725782400",
        "category": "300",
    }
]


# ---------------------------------------------------------------------------
# Reading
# ---------------------------------------------------------------------------


async def test_list_torrents_summarizes(fake, build):
    fake.results["torrent-get"] = {"torrents": TORRENTS}
    async with Client(build()) as mcp:
        data = (await mcp.call_tool("list_torrents")).data

    assert data["total"] == 2
    first = data["torrents"][0]
    assert first["status"] == "downloading"
    assert first["progress_percent"] == 50.0
    assert first["eta_seconds"] == 120


async def test_list_torrents_filters_by_status_and_name(fake, build):
    fake.results["torrent-get"] = {"torrents": TORRENTS}
    async with Client(build()) as mcp:
        stopped = (await mcp.call_tool("list_torrents", {"status_filter": "stopped"})).data
        named = (await mcp.call_tool("list_torrents", {"name_contains": "ubuntu"})).data

    assert [t["id"] for t in stopped["torrents"]] == [2]
    assert [t["id"] for t in named["torrents"]] == [1]  # match is case-insensitive


async def test_list_torrents_limit_reports_the_full_total(fake, build):
    fake.results["torrent-get"] = {"torrents": TORRENTS}
    async with Client(build()) as mcp:
        data = (await mcp.call_tool("list_torrents", {"limit": 1})).data

    assert (data["total"], data["returned"]) == (2, 1)


async def test_list_torrents_verbose_returns_the_raw_struct(fake, build):
    fake.results["torrent-get"] = {"torrents": TORRENTS}
    async with Client(build()) as mcp:
        data = (await mcp.call_tool("list_torrents", {"verbose": True})).data

    assert data["torrents"][0]["percentDone"] == 0.5  # not converted to a percentage


async def test_get_torrent_requests_extras_only_when_asked(fake, build):
    fake.results["torrent-get"] = {
        "torrents": [
            {
                **TORRENTS[0],
                "files": [{"name": "a.iso", "length": 2000, "bytesCompleted": 1000}],
                "fileStats": [{"wanted": True, "priority": 0}],
            }
        ]
    }
    async with Client(build()) as mcp:
        plain = (await mcp.call_tool("get_torrent", {"torrent_id": 1})).data
        assert "files" not in plain
        assert "files" not in fake.arguments_for("torrent-get")["fields"]

        fake.calls.clear()
        with_files = (
            await mcp.call_tool("get_torrent", {"torrent_id": 1, "include_files": True})
        ).data

    assert "files" in fake.arguments_for("torrent-get")["fields"]
    assert with_files["files"][0]["progress_percent"] == 50.0


async def test_get_torrent_accepts_an_info_hash(fake, build):
    fake.results["torrent-get"] = {"torrents": [TORRENTS[0]]}
    async with Client(build()) as mcp:
        await mcp.call_tool("get_torrent", {"torrent_id": HASH_A.upper()})

    assert fake.arguments_for("torrent-get")["ids"] == [HASH_A]  # lowercased for the RPC


async def test_get_torrent_errors_when_nothing_matches(fake, build):
    fake.results["torrent-get"] = {"torrents": []}
    async with Client(build()) as mcp:
        with pytest.raises(ToolError, match="No torrent matches"):
            await mcp.call_tool("get_torrent", {"torrent_id": 99})


async def test_get_free_space_falls_back_to_the_session_download_dir(fake, build):
    fake.results["session-get"] = {"download-dir": "/downloads"}
    fake.results["free-space"] = {"path": "/downloads", "size-bytes": 100, "total_size": 500}
    async with Client(build()) as mcp:
        data = (await mcp.call_tool("get_free_space")).data

    assert data == {"path": "/downloads", "free_bytes": 100, "total_bytes": 500}
    assert fake.arguments_for("free-space") == {"path": "/downloads"}


async def test_get_free_space_prefers_the_configured_download_dir(fake, build):
    fake.results["free-space"] = {"size-bytes": 1, "total_size": 2}
    async with Client(build(download_dir="/data")) as mcp:
        await mcp.call_tool("get_free_space")

    assert fake.arguments_for("free-space") == {"path": "/data"}
    assert "session-get" not in fake.methods()  # no extra round trip


async def test_resources_mirror_the_read_tools(fake, build):
    fake.results["torrent-get"] = {"torrents": TORRENTS}
    fake.results["session-get"] = {"version": "4.0.6"}
    async with Client(build()) as mcp:
        torrents = await mcp.read_resource("transmission://torrents")
        session = await mcp.read_resource("transmission://session")

    assert '"downloading"' in torrents[0].text
    assert '"4.0.6"' in session[0].text


# ---------------------------------------------------------------------------
# Searching
# ---------------------------------------------------------------------------


async def test_search_returns_results_without_magnets_by_default(fake, build, indexers):
    indexers.add("apibay.org", APIBAY)
    async with Client(build(search_kwargs={"sources": ["thepiratebay"]})) as mcp:
        data = (await mcp.call_tool("search_torrents", {"query": "ubuntu"})).data

    assert data["total"] == 1
    result = data["results"][0]
    assert result["info_hash"] == HASH_A
    assert result["size_human"] == "5.78 GiB"
    assert "magnet" not in result


async def test_search_includes_magnets_on_request(fake, build, indexers):
    indexers.add("apibay.org", APIBAY)
    async with Client(build(search_kwargs={"sources": ["thepiratebay"]})) as mcp:
        data = (
            await mcp.call_tool(
                "search_torrents", {"query": "ubuntu", "include_magnets": True}
            )
        ).data

    assert data["results"][0]["magnet"].startswith(f"magnet:?xt=urn:btih:{HASH_A}")


async def test_search_reports_which_sources_failed(fake, build, indexers):
    indexers.add("apibay.org", APIBAY)
    async with Client(build(search_kwargs={"sources": ["thepiratebay", "nyaa"]})) as mcp:
        data = (await mcp.call_tool("search_torrents", {"query": "ubuntu"})).data

    assert data["sources_searched"] == ["thepiratebay"]
    assert "nyaa" in data["sources_failed"]


async def test_search_rejects_an_unknown_source(fake, build):
    async with Client(build()) as mcp:
        with pytest.raises(ToolError, match="Unknown torrent source"):
            await mcp.call_tool("search_torrents", {"query": "x", "sources": ["tarpit"]})


async def test_search_tools_vanish_when_search_is_disabled(fake, build):
    async with Client(build(search_enabled=False)) as mcp:
        names = {tool.name for tool in await mcp.list_tools()}

    assert "search_torrents" not in names
    assert "list_torrents" in names


# ---------------------------------------------------------------------------
# Adding
# ---------------------------------------------------------------------------


async def test_add_by_info_hash_uses_the_cached_name_and_trackers(fake, build, indexers):
    indexers.add("apibay.org", APIBAY)
    fake.results["torrent-add"] = {
        "torrent-added": {"id": 7, "name": "Ubuntu 24.04 LTS", "hashString": HASH_A}
    }
    async with Client(build(search_kwargs={"sources": ["thepiratebay"]})) as mcp:
        await mcp.call_tool("search_torrents", {"query": "ubuntu"})
        data = (await mcp.call_tool("add_torrent", {"info_hash": HASH_A})).data

    magnet = fake.arguments_for("torrent-add")["filename"]
    assert magnet.startswith(f"magnet:?xt=urn:btih:{HASH_A}")
    assert "dn=Ubuntu%2024.04%20LTS" in magnet  # name recovered from the cache
    assert data == {
        "id": 7,
        "name": "Ubuntu 24.04 LTS",
        "info_hash": HASH_A,
        "duplicate": False,
        "paused": False,
    }


async def test_add_by_info_hash_works_without_a_cache_hit(fake, build):
    fake.results["torrent-add"] = {"torrent-added": {"id": 8, "hashString": HASH_A}}
    async with Client(build()) as mcp:
        await mcp.call_tool("add_torrent", {"info_hash": HASH_A.upper()})

    magnet = fake.arguments_for("torrent-add")["filename"]
    assert magnet.startswith(f"magnet:?xt=urn:btih:{HASH_A}")  # lowercased, trackers appended
    assert "&tr=" in magnet


async def test_add_rejects_a_malformed_info_hash(fake, build):
    async with Client(build()) as mcp:
        with pytest.raises(ToolError, match="not a valid info hash"):
            await mcp.call_tool("add_torrent", {"info_hash": "deadbeef"})


async def test_add_passes_magnets_and_urls_straight_through(fake, build):
    fake.results["torrent-add"] = {"torrent-added": {"id": 9}}
    async with Client(build()) as mcp:
        await mcp.call_tool("add_torrent", {"torrent": "magnet:?xt=urn:btih:abc"})
        assert fake.arguments_for("torrent-add")["filename"] == "magnet:?xt=urn:btih:abc"

        fake.calls.clear()
        await mcp.call_tool("add_torrent", {"torrent": "https://example.test/x.torrent"})

    assert fake.arguments_for("torrent-add")["filename"] == "https://example.test/x.torrent"


async def test_add_reads_a_local_torrent_file(fake, build, tmp_path):
    path = tmp_path / "thing.torrent"
    path.write_bytes(b"d8:announce4:teste")
    fake.results["torrent-add"] = {"torrent-added": {"id": 10}}

    async with Client(build()) as mcp:
        await mcp.call_tool("add_torrent", {"torrent": str(path)})

    metainfo = fake.arguments_for("torrent-add")["metainfo"]
    assert base64.b64decode(metainfo) == b"d8:announce4:teste"


async def test_add_rejects_a_path_that_is_not_there(fake, build):
    async with Client(build()) as mcp:
        with pytest.raises(ToolError, match="nor a file"):
            await mcp.call_tool("add_torrent", {"torrent": "/no/such/file.torrent"})


async def test_add_requires_exactly_one_source(fake, build):
    async with Client(build()) as mcp:
        with pytest.raises(ToolError, match="exactly one"):
            await mcp.call_tool("add_torrent", {})
        with pytest.raises(ToolError, match="exactly one"):
            await mcp.call_tool(
                "add_torrent", {"info_hash": HASH_A, "torrent": "magnet:?xt=urn:btih:abc"}
            )


async def test_add_reports_a_duplicate_rather_than_claiming_success(fake, build):
    fake.results["torrent-add"] = {
        "torrent-duplicate": {"id": 3, "name": "Ubuntu", "hashString": HASH_A}
    }
    async with Client(build()) as mcp:
        data = (await mcp.call_tool("add_torrent", {"info_hash": HASH_A})).data

    assert data["duplicate"] is True and data["id"] == 3


async def test_add_applies_the_default_download_dir(fake, build):
    fake.results["torrent-add"] = {"torrent-added": {"id": 11}}
    async with Client(build(download_dir="/downloads/complete")) as mcp:
        await mcp.call_tool("add_torrent", {"info_hash": HASH_A})
        assert fake.arguments_for("torrent-add")["download-dir"] == "/downloads/complete"

        fake.calls.clear()
        await mcp.call_tool(
            "add_torrent", {"info_hash": HASH_A, "download_dir": "/downloads/movies"}
        )

    assert fake.arguments_for("torrent-add")["download-dir"] == "/downloads/movies"


async def test_add_maps_priority_names_to_transmission_numbers(fake, build):
    fake.results["torrent-add"] = {"torrent-added": {"id": 12}}
    async with Client(build()) as mcp:
        await mcp.call_tool(
            "add_torrent", {"info_hash": HASH_A, "priority": "high", "labels": ["iso"]}
        )

    arguments = fake.arguments_for("torrent-add")
    assert arguments["bandwidthPriority"] == 1
    assert arguments["labels"] == ["iso"]


# ---------------------------------------------------------------------------
# Managing
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "action,method",
    [
        ("start", "torrent-start"),
        ("start_now", "torrent-start-now"),
        ("stop", "torrent-stop"),
        ("verify", "torrent-verify"),
        ("reannounce", "torrent-reannounce"),
        ("queue_top", "queue-move-top"),
        ("queue_bottom", "queue-move-bottom"),
    ],
)
async def test_manage_torrents_maps_actions_to_rpc_methods(fake, build, action, method):
    async with Client(build()) as mcp:
        await mcp.call_tool("manage_torrents", {"action": action, "torrent_ids": [1, 2]})

    assert fake.methods() == [method]
    assert fake.arguments_for(method)["ids"] == [1, 2]


async def test_manage_torrents_needs_at_least_one_id(fake, build):
    async with Client(build()) as mcp:
        with pytest.raises(ToolError, match="at least one torrent id"):
            await mcp.call_tool("manage_torrents", {"action": "stop", "torrent_ids": []})


async def test_recently_active_is_passed_as_the_bare_string(fake, build):
    async with Client(build()) as mcp:
        await mcp.call_tool(
            "manage_torrents", {"action": "stop", "torrent_ids": ["recently-active"]}
        )

    assert fake.arguments_for("torrent-stop")["ids"] == "recently-active"


async def test_remove_keeps_data_by_default(fake, build):
    async with Client(build()) as mcp:
        data = (await mcp.call_tool("remove_torrents", {"torrent_ids": [1]})).data

    assert fake.arguments_for("torrent-remove")["delete-local-data"] is False
    assert data["deleted_data"] is False


async def test_deleting_data_is_refused_unless_explicitly_allowed(fake, build):
    async with Client(build()) as mcp:
        with pytest.raises(ToolError, match="TRANSMISSION_ALLOW_REMOVE_DATA"):
            await mcp.call_tool(
                "remove_torrents", {"torrent_ids": [1], "delete_data": True}
            )

    assert fake.calls == []  # nothing reached Transmission


async def test_deleting_data_works_once_allowed(fake, build):
    async with Client(build(allow_remove_data=True)) as mcp:
        await mcp.call_tool("remove_torrents", {"torrent_ids": [1], "delete_data": True})

    assert fake.arguments_for("torrent-remove")["delete-local-data"] is True


async def test_speed_limits_toggle_their_enabled_flag(fake, build):
    async with Client(build()) as mcp:
        await mcp.call_tool(
            "set_torrent_properties",
            {"torrent_ids": [1], "download_limit_kbps": 500, "upload_limit_kbps": 0},
        )

    arguments = fake.arguments_for("torrent-set")
    assert (arguments["downloadLimited"], arguments["downloadLimit"]) == (True, 500)
    assert (arguments["uploadLimited"], arguments["uploadLimit"]) == (False, 0)


async def test_seed_ratio_switches_the_torrent_to_its_own_limit(fake, build):
    async with Client(build()) as mcp:
        await mcp.call_tool(
            "set_torrent_properties", {"torrent_ids": [1], "seed_ratio_limit": 2.0}
        )

    arguments = fake.arguments_for("torrent-set")
    assert arguments["seedRatioLimit"] == 2.0 and arguments["seedRatioMode"] == 1


async def test_set_properties_needs_something_to_change(fake, build):
    async with Client(build()) as mcp:
        with pytest.raises(ToolError, match="at least one property"):
            await mcp.call_tool("set_torrent_properties", {"torrent_ids": [1]})


async def test_file_priorities_map_to_the_rpc_keys(fake, build):
    async with Client(build()) as mcp:
        await mcp.call_tool(
            "set_file_priorities",
            {"torrent_id": 1, "unwanted": [2, 3], "high": [0]},
        )

    arguments = fake.arguments_for("torrent-set")
    assert arguments["files-unwanted"] == [2, 3]
    assert arguments["priority-high"] == [0]
    assert "files-wanted" not in arguments


async def test_move_torrent_defaults_to_moving_the_data(fake, build):
    async with Client(build()) as mcp:
        await mcp.call_tool("move_torrent", {"torrent_ids": [1], "location": "/other"})

    arguments = fake.arguments_for("torrent-set-location")
    assert arguments == {"ids": [1], "location": "/other", "move": True}


async def test_rename_returns_what_transmission_confirmed(fake, build):
    fake.results["torrent-rename-path"] = {"id": 1, "path": "old", "name": "new"}
    async with Client(build()) as mcp:
        data = (
            await mcp.call_tool(
                "rename_torrent_path", {"torrent_id": 1, "path": "old", "new_name": "new"}
            )
        ).data

    assert data == {"torrent_id": 1, "path": "old", "name": "new"}


async def test_session_speed_limits_toggle_their_enabled_flag(fake, build):
    async with Client(build()) as mcp:
        await mcp.call_tool(
            "set_session_properties",
            {"speed_limit_down_kbps": 2000, "download_queue_size": 0},
        )

    arguments = fake.arguments_for("session-set")
    assert (arguments["speed-limit-down-enabled"], arguments["speed-limit-down"]) == (True, 2000)
    assert arguments["download-queue-enabled"] is False


async def test_session_set_needs_something_to_change(fake, build):
    async with Client(build()) as mcp:
        with pytest.raises(ToolError, match="at least one setting"):
            await mcp.call_tool("set_session_properties", {})


# ---------------------------------------------------------------------------
# Gating
# ---------------------------------------------------------------------------


async def test_read_only_mode_registers_no_mutating_tools(fake, build):
    async with Client(build(read_only=True)) as mcp:
        names = {tool.name for tool in await mcp.list_tools()}

    assert names == {
        "search_torrents",
        "list_torrents",
        "get_torrent",
        "get_session",
        "get_session_stats",
        "get_free_space",
    }


async def test_shutdown_is_hidden_unless_allowed(fake, build):
    async with Client(build()) as mcp:
        assert "shutdown_session" not in {t.name for t in await mcp.list_tools()}

    async with Client(build(allow_shutdown=True)) as mcp:
        assert "shutdown_session" in {t.name for t in await mcp.list_tools()}
        await mcp.call_tool("shutdown_session")

    assert "session-close" in fake.methods()


async def test_rpc_failures_surface_as_tool_errors(fake, build):
    fake.result_status = "invalid argument"
    async with Client(build()) as mcp:
        with pytest.raises(ToolError, match="invalid argument"):
            await mcp.call_tool("list_torrents")


async def test_session_blocklist_settings_round_trip(fake, build):
    async with Client(build()) as mcp:
        await mcp.call_tool(
            "set_session_properties",
            {"blocklist_url": "https://lists.test/bt.gz", "blocklist_enabled": True},
        )

    arguments = fake.arguments_for("session-set")
    assert arguments["blocklist-url"] == "https://lists.test/bt.gz"
    assert arguments["blocklist-enabled"] is True


async def test_update_blocklist_reports_the_new_size(fake, build):
    fake.results["blocklist-update"] = {"blocklist-size": 462562}
    async with Client(build()) as mcp:
        data = (await mcp.call_tool("update_blocklist")).data

    assert data == {"blocklist_size": 462562}


async def test_free_space_errors_name_the_path(fake, build):
    fake.result_status = "No such file or directory"
    async with Client(build()) as mcp:
        with pytest.raises(ToolError, match="'/nope'"):
            await mcp.call_tool("get_free_space", {"path": "/nope"})
