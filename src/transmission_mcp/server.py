"""FastMCP server exposing torrent search and Transmission's RPC API as tools."""

from __future__ import annotations

import base64
import os
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Annotated, Any, Literal

from fastmcp import FastMCP
from fastmcp.exceptions import ToolError
from pydantic import Field

from .client import TransmissionClient, TransmissionError
from .config import Settings, load_settings
from .normalize import (
    DETAIL_FIELDS,
    PRIORITY_VALUES,
    STATUSES,
    SUMMARY_FIELDS,
    detail_torrent,
    summarize_files,
    summarize_peers,
    summarize_session,
    summarize_stats,
    summarize_torrent,
    summarize_trackers,
)
from .search import TorrentSearch, UnknownSourceError, build_magnet, normalize_info_hash

INSTRUCTIONS = """\
Find torrents on public indexers and download them with Transmission.

The usual flow is two steps: `search_torrents` to find candidates, then
`add_torrent(info_hash=...)` with the `info_hash` of the one you picked — no
need to pass magnet links around. From there `list_torrents` shows progress,
`manage_torrents` starts/stops/verifies, and `remove_torrents` deletes.

When choosing between search results, prefer high seeders, then the quality the
user asked for (1080p/4K, h265/HEVC for a better size-to-quality ratio), then
the smaller file within the same quality. Recommend a few options rather than
silently picking one, unless the user has said to just grab the best.

Sizes are bytes, rates are bytes per second, speed limits are kB/s (Transmission's
own unit), and timestamps are ISO-8601 UTC unless a field name says otherwise.
"""

TORRENT_ACTIONS = {
    "start": "torrent-start",
    "start_now": "torrent-start-now",
    "stop": "torrent-stop",
    "verify": "torrent-verify",
    "reannounce": "torrent-reannounce",
    "queue_top": "queue-move-top",
    "queue_up": "queue-move-up",
    "queue_down": "queue-move-down",
    "queue_bottom": "queue-move-bottom",
}

TorrentAction = Literal[tuple(TORRENT_ACTIONS)]  # type: ignore[valid-type]
StatusName = Literal[tuple(sorted(set(STATUSES.values())))]  # type: ignore[valid-type]

TorrentIds = Annotated[
    list[int | str],
    Field(
        description=(
            "Torrent ids from list_torrents, or 40-character info hashes. "
            "Pass the single string 'recently-active' for torrents that changed recently."
        )
    ),
]


def _ids(raw: list[int | str]) -> list[int | str] | str:
    """Normalize the ids argument into what the RPC expects."""
    if len(raw) == 1 and str(raw[0]).strip().lower() == "recently-active":
        return "recently-active"
    return [item if isinstance(item, int) else str(item).strip().lower() for item in raw]


def build_server(
    settings: Settings | None = None,
    client: TransmissionClient | None = None,
    search: TorrentSearch | None = None,
) -> FastMCP:
    """Create the MCP server, wiring tools to Transmission and the search sources."""
    settings = settings or load_settings()
    client = client or TransmissionClient(
        settings.rpc_url,
        username=settings.username,
        password=settings.password,
        timeout=settings.timeout,
        verify_ssl=settings.verify_ssl,
    )
    if search is None and settings.search_enabled:
        try:
            search = TorrentSearch(
                sources=settings.search_sources,
                exclude=settings.search_exclude,
                timeout=settings.search_timeout,
                cache_ttl=settings.search_cache_ttl,
            )
        except UnknownSourceError as exc:
            raise ValueError(f"Bad SEARCH_SOURCES/SEARCH_EXCLUDE_SOURCES: {exc}") from exc

    @asynccontextmanager
    async def lifespan(_server: FastMCP):
        try:
            yield
        finally:
            await client.aclose()
            if search is not None:
                await search.aclose()

    mcp = FastMCP(
        name="transmission",
        version="0.1.0",
        instructions=INSTRUCTIONS,
        website_url="https://transmissionbt.com/",
        lifespan=lifespan,
    )

    async def call(method: str, **arguments: Any) -> dict[str, Any]:
        """Invoke Transmission, surfacing failures as clean tool errors."""
        try:
            return await client.call(method, **arguments)
        except TransmissionError as exc:
            raise ToolError(str(exc)) from exc

    async def fetch_torrents(
        ids: list[int | str] | str | None, fields: list[str]
    ) -> list[dict[str, Any]]:
        result = await call("torrent-get", ids=ids, fields=fields)
        return result.get("torrents") or []

    # ------------------------------------------------------------------
    # Torrent search
    # ------------------------------------------------------------------

    if search is not None:

        @mcp.tool(annotations={"readOnlyHint": True, "openWorldHint": True})
        async def search_torrents(
            query: Annotated[
                str,
                Field(
                    description=(
                        "Lowercase, space-separated keywords. Drop filler words and "
                        "generic terms like 'torrent' or 'download'. Use 'show sXXeYY' "
                        "for one episode, 'show sXX' for a season. Only add quality tags "
                        "like 1080p or h265 if the user asked for them."
                    )
                ),
            ],
            sources: Annotated[
                list[str] | None,
                Field(
                    description=(
                        "Restrict the search to these indexers. Omit to search all "
                        "enabled ones; see the search://sources resource for the list."
                    )
                ),
            ] = None,
            limit: Annotated[
                int, Field(ge=1, le=100, description="Max results to return.")
            ] = 20,
            include_magnets: Annotated[
                bool,
                Field(
                    description=(
                        "Include full magnet links. Off by default because they are "
                        "long and add_torrent only needs the info_hash."
                    )
                ),
            ] = False,
        ) -> dict[str, Any]:
            """Search public torrent indexers in parallel, merged and ranked by health.

            Results are deduplicated across sources by info hash and sorted by
            seeders and leechers. Pass a result's `info_hash` to `add_torrent`
            to start downloading it.
            """
            try:
                results, report = await search.search(query, sources)
            except UnknownSourceError as exc:
                raise ToolError(str(exc)) from exc

            shown = results[:limit]
            failed = [name for name, info in report.items() if not info["ok"]]
            payload: dict[str, Any] = {
                "query": query,
                "total": len(results),
                "returned": len(shown),
                "sources_searched": [name for name, info in report.items() if info["ok"]],
                "results": [r.to_dict(include_magnet=include_magnets) for r in shown],
            }
            if failed:
                # Surfaced rather than hidden: a blocked or down indexer changes
                # how much the caller should trust an empty result set.
                payload["sources_failed"] = {name: report[name]["error"] for name in failed}
            return payload

        @mcp.resource("search://sources", mime_type="application/json")
        def sources_resource() -> list[dict[str, str]]:
            """The torrent indexers this server can search."""
            return search.available_sources()

    # ------------------------------------------------------------------
    # Read-only Transmission tools
    # ------------------------------------------------------------------

    @mcp.tool(annotations={"readOnlyHint": True})
    async def list_torrents(
        status_filter: Annotated[
            StatusName | None,
            Field(description="Only return torrents in this state."),
        ] = None,
        name_contains: Annotated[
            str | None, Field(description="Case-insensitive substring match on the name.")
        ] = None,
        limit: Annotated[
            int, Field(ge=1, le=500, description="Max torrents to return.")
        ] = 50,
        verbose: Annotated[
            bool, Field(description="Return every raw RPC field instead of the summary.")
        ] = False,
    ) -> dict[str, Any]:
        """List torrents in Transmission with their status, progress, and speeds."""
        torrents = await fetch_torrents(None, SUMMARY_FIELDS)

        if status_filter:
            torrents = [
                t for t in torrents if STATUSES.get(t.get("status")) == status_filter
            ]
        if name_contains:
            needle = name_contains.lower()
            torrents = [t for t in torrents if needle in (t.get("name") or "").lower()]

        shown = torrents[:limit]
        return {
            "total": len(torrents),
            "returned": len(shown),
            "torrents": shown if verbose else [summarize_torrent(t) for t in shown],
        }

    @mcp.tool(annotations={"readOnlyHint": True})
    async def get_torrent(
        torrent_id: Annotated[
            int | str, Field(description="Torrent id from list_torrents, or its info hash.")
        ],
        include_files: Annotated[
            bool, Field(description="Include the file list with per-file progress.")
        ] = False,
        include_trackers: Annotated[
            bool, Field(description="Include tracker announce results and peer counts.")
        ] = False,
        include_peers: Annotated[
            bool, Field(description="Include the list of currently connected peers.")
        ] = False,
    ) -> dict[str, Any]:
        """Everything Transmission knows about one torrent, optionally with files and peers."""
        fields = list(DETAIL_FIELDS)
        if include_files:
            fields += ["files", "fileStats"]
        if include_trackers:
            fields += ["trackerStats"]
        if include_peers:
            fields += ["peers"]

        torrents = await fetch_torrents(_ids([torrent_id]), fields)
        if not torrents:
            raise ToolError(f"No torrent matches {torrent_id!r}.")

        torrent = torrents[0]
        detail = detail_torrent(torrent)
        if include_files:
            detail["files"] = summarize_files(torrent)
        if include_trackers:
            detail["trackers"] = summarize_trackers(torrent)
        if include_peers:
            detail["peers"] = summarize_peers(torrent)
        return detail

    @mcp.tool(annotations={"readOnlyHint": True})
    async def get_session(
        verbose: Annotated[
            bool, Field(description="Return every raw session field instead of the summary.")
        ] = False,
    ) -> dict[str, Any]:
        """Transmission's session settings: version, directories, limits, and protocols."""
        session = await call("session-get")
        return session if verbose else summarize_session(session)

    @mcp.tool(annotations={"readOnlyHint": True})
    async def get_session_stats() -> dict[str, Any]:
        """Torrent counts, current speeds, and cumulative transfer totals."""
        return summarize_stats(await call("session-stats"))

    @mcp.tool(annotations={"readOnlyHint": True})
    async def get_free_space(
        path: Annotated[
            str | None,
            Field(description="Directory to check. Defaults to the session download dir."),
        ] = None,
    ) -> dict[str, Any]:
        """Free and total disk space for a directory Transmission can see."""
        target = path or settings.download_dir
        if not target:
            session = await call("session-get")
            target = session.get("download-dir")
        if not target:
            raise ToolError("No path given and Transmission reported no download-dir.")

        try:
            result = await call("free-space", path=target)
        except ToolError as exc:
            # Transmission reports a missing directory as a bare errno string,
            # which is useless without knowing which path was asked about.
            raise ToolError(
                f"Could not read free space for {target!r}: {exc}. The path must "
                "exist inside the Transmission container, not on this host."
            ) from exc
        return {
            "path": result.get("path", target),
            "free_bytes": result.get("size-bytes"),
            "total_bytes": result.get("total_size"),
        }

    @mcp.resource("transmission://torrents", mime_type="application/json")
    async def torrents_resource() -> list[dict[str, Any]]:
        """Current torrent list."""
        return [summarize_torrent(t) for t in await fetch_torrents(None, SUMMARY_FIELDS)]

    @mcp.resource("transmission://session", mime_type="application/json")
    async def session_resource() -> dict[str, Any]:
        """Transmission session settings."""
        return summarize_session(await call("session-get"))

    @mcp.resource("transmission://stats", mime_type="application/json")
    async def stats_resource() -> dict[str, Any]:
        """Transfer statistics."""
        return summarize_stats(await call("session-stats"))

    if settings.read_only:
        return mcp

    # ------------------------------------------------------------------
    # Mutating Transmission tools
    # ------------------------------------------------------------------

    @mcp.tool
    async def add_torrent(
        info_hash: Annotated[
            str | None,
            Field(
                description=(
                    "info_hash from a search_torrents result — the normal way to add "
                    "something that was just searched for."
                )
            ),
        ] = None,
        torrent: Annotated[
            str | None,
            Field(
                description=(
                    "A magnet URI, an http(s) URL to a .torrent file, or the path to a "
                    ".torrent file readable by this MCP server."
                )
            ),
        ] = None,
        content_base64: Annotated[
            str | None, Field(description="Base64-encoded contents of a .torrent file.")
        ] = None,
        download_dir: Annotated[
            str | None,
            Field(
                description=(
                    "Where to save the files, as a path inside the Transmission "
                    "container. Defaults to Transmission's own download directory."
                )
            ),
        ] = None,
        paused: Annotated[
            bool, Field(description="Add without starting the download.")
        ] = False,
        labels: Annotated[
            list[str] | None, Field(description="Labels to tag the torrent with.")
        ] = None,
        priority: Annotated[
            Literal["low", "normal", "high"],
            Field(description="Bandwidth priority relative to other torrents."),
        ] = "normal",
        peer_limit: Annotated[
            int | None, Field(ge=1, description="Maximum peers for this torrent.")
        ] = None,
    ) -> dict[str, Any]:
        """Add a torrent to Transmission by search result, magnet, URL, or file.

        Give exactly one of `info_hash`, `torrent`, or `content_base64`. Adding a
        torrent Transmission already has is reported back rather than duplicated.
        """
        given = [value for value in (info_hash, torrent, content_base64) if value]
        if len(given) != 1:
            raise ToolError("Provide exactly one of info_hash, torrent, or content_base64.")

        arguments: dict[str, Any] = {
            "download-dir": download_dir or settings.download_dir,
            "paused": paused,
            "labels": labels or None,
            "bandwidthPriority": PRIORITY_VALUES[priority],
            "peer-limit": peer_limit,
        }

        if info_hash:
            normalized = normalize_info_hash(info_hash)
            if not normalized:
                raise ToolError(
                    f"{info_hash!r} is not a valid info hash "
                    "(expected 40 hex characters or 32 base32 characters)."
                )
            # A cached search hit supplies the display name and the trackers the
            # indexer advertised; without it a bare hash still works over DHT.
            cached = search.cache.get(normalized) if search else None
            arguments["filename"] = (
                cached.magnet() if cached else build_magnet(normalized)
            )
        elif torrent:
            value = torrent.strip()
            if value.startswith(("magnet:", "http://", "https://")):
                arguments["filename"] = value
            else:
                path = Path(value).expanduser()
                if not path.is_file():
                    raise ToolError(
                        f"{value!r} is neither a magnet, an http(s) URL, nor a file "
                        "this server can read."
                    )
                arguments["metainfo"] = base64.b64encode(path.read_bytes()).decode("ascii")
        else:
            arguments["metainfo"] = content_base64

        result = await call("torrent-add", **arguments)

        if added := result.get("torrent-added"):
            return {
                "id": added.get("id"),
                "name": added.get("name"),
                "info_hash": added.get("hashString"),
                "duplicate": False,
                "paused": paused,
            }
        if duplicate := result.get("torrent-duplicate"):
            return {
                "id": duplicate.get("id"),
                "name": duplicate.get("name"),
                "info_hash": duplicate.get("hashString"),
                "duplicate": True,
                "message": "Transmission already has this torrent; nothing was added.",
            }
        raise ToolError("Transmission accepted the request but returned no torrent.")

    @mcp.tool
    async def manage_torrents(
        action: Annotated[TorrentAction, Field(description="What to do with the torrents.")],
        torrent_ids: TorrentIds,
    ) -> dict[str, Any]:
        """Start, stop, verify, reannounce, or reorder torrents in the queue.

        Transmission applies these asynchronously and can take a second or so to
        reflect the new state, so a `list_torrents` immediately afterwards may
        still show the old status. That is not a failure; do not retry the action.
        """
        if not torrent_ids:
            raise ToolError(f"Action '{action}' requires at least one torrent id.")
        await call(TORRENT_ACTIONS[action], ids=_ids(torrent_ids))
        return {"action": action, "torrent_ids": torrent_ids, "success": True}

    @mcp.tool(annotations={"destructiveHint": True})
    async def remove_torrents(
        torrent_ids: TorrentIds,
        delete_data: Annotated[
            bool,
            Field(
                description=(
                    "Also delete the downloaded files from disk. Irreversible, and "
                    "only permitted when TRANSMISSION_ALLOW_REMOVE_DATA=true."
                )
            ),
        ] = False,
    ) -> dict[str, Any]:
        """Remove torrents from Transmission, optionally deleting their files."""
        if not torrent_ids:
            raise ToolError("remove_torrents requires at least one torrent id.")
        if delete_data and not settings.allow_remove_data:
            raise ToolError(
                "Deleting local data is disabled. Set TRANSMISSION_ALLOW_REMOVE_DATA=true "
                "to permit it, or call again with delete_data=false to remove the torrent "
                "but keep the files."
            )
        await call("torrent-remove", ids=_ids(torrent_ids), **{"delete-local-data": delete_data})
        return {"torrent_ids": torrent_ids, "deleted_data": delete_data, "success": True}

    @mcp.tool
    async def set_torrent_properties(
        torrent_ids: TorrentIds,
        download_limit_kbps: Annotated[
            int | None,
            Field(ge=0, description="Per-torrent download cap in kB/s. 0 removes the cap."),
        ] = None,
        upload_limit_kbps: Annotated[
            int | None,
            Field(ge=0, description="Per-torrent upload cap in kB/s. 0 removes the cap."),
        ] = None,
        priority: Annotated[
            Literal["low", "normal", "high"] | None,
            Field(description="Bandwidth priority relative to other torrents."),
        ] = None,
        labels: Annotated[
            list[str] | None, Field(description="Replace the torrent's labels with these.")
        ] = None,
        seed_ratio_limit: Annotated[
            float | None,
            Field(ge=0, description="Stop seeding at this ratio (also sets mode to 'single')."),
        ] = None,
        use_global_seed_ratio: Annotated[
            bool | None,
            Field(description="True restores the session-wide seed ratio for these torrents."),
        ] = None,
        honors_session_limits: Annotated[
            bool | None, Field(description="Whether the global speed limits apply.")
        ] = None,
        peer_limit: Annotated[
            int | None, Field(ge=1, description="Maximum peers for these torrents.")
        ] = None,
        queue_position: Annotated[
            int | None,
            Field(ge=0, description="Absolute queue position; only sensible for one torrent."),
        ] = None,
    ) -> dict[str, Any]:
        """Change speed limits, priority, labels, seeding rules, or queue position."""
        if not torrent_ids:
            raise ToolError("set_torrent_properties requires at least one torrent id.")

        arguments: dict[str, Any] = {"ids": _ids(torrent_ids)}
        if download_limit_kbps is not None:
            arguments["downloadLimited"] = download_limit_kbps > 0
            arguments["downloadLimit"] = download_limit_kbps
        if upload_limit_kbps is not None:
            arguments["uploadLimited"] = upload_limit_kbps > 0
            arguments["uploadLimit"] = upload_limit_kbps
        if priority is not None:
            arguments["bandwidthPriority"] = PRIORITY_VALUES[priority]
        if labels is not None:
            arguments["labels"] = labels
        if seed_ratio_limit is not None:
            arguments["seedRatioLimit"] = seed_ratio_limit
            arguments["seedRatioMode"] = 1  # 1 = use this torrent's own limit
        if use_global_seed_ratio:
            arguments["seedRatioMode"] = 0
        if honors_session_limits is not None:
            arguments["honorsSessionLimits"] = honors_session_limits
        if peer_limit is not None:
            arguments["peer-limit"] = peer_limit
        if queue_position is not None:
            arguments["queuePosition"] = queue_position

        if len(arguments) == 1:
            raise ToolError("Nothing to change: pass at least one property.")
        await call("torrent-set", **arguments)
        return {"torrent_ids": torrent_ids, "changed": sorted(set(arguments) - {"ids"})}

    @mcp.tool
    async def set_file_priorities(
        torrent_id: Annotated[
            int | str, Field(description="Torrent id or info hash from list_torrents.")
        ],
        wanted: Annotated[
            list[int] | None, Field(description="File indices to download (see get_torrent).")
        ] = None,
        unwanted: Annotated[
            list[int] | None, Field(description="File indices to skip.")
        ] = None,
        high: Annotated[list[int] | None, Field(description="File indices to prioritise.")] = None,
        normal: Annotated[list[int] | None, Field(description="File indices at normal priority.")] = None,
        low: Annotated[list[int] | None, Field(description="File indices to deprioritise.")] = None,
    ) -> dict[str, Any]:
        """Choose which files inside a torrent to download, and at what priority.

        File indices come from `get_torrent(include_files=true)`.
        """
        arguments: dict[str, Any] = {"ids": _ids([torrent_id])}
        for key, value in (
            ("files-wanted", wanted),
            ("files-unwanted", unwanted),
            ("priority-high", high),
            ("priority-normal", normal),
            ("priority-low", low),
        ):
            if value is not None:
                arguments[key] = value
        if len(arguments) == 1:
            raise ToolError("Nothing to change: pass at least one list of file indices.")
        await call("torrent-set", **arguments)
        return {"torrent_id": torrent_id, "changed": sorted(set(arguments) - {"ids"})}

    @mcp.tool
    async def move_torrent(
        torrent_ids: TorrentIds,
        location: Annotated[
            str, Field(description="New directory, as a path inside the Transmission container.")
        ],
        move_data: Annotated[
            bool,
            Field(
                description=(
                    "True moves the existing files there. False tells Transmission the "
                    "files are already at the new location."
                )
            ),
        ] = True,
    ) -> dict[str, Any]:
        """Move a torrent's files to a new directory, or point Transmission at them."""
        if not torrent_ids:
            raise ToolError("move_torrent requires at least one torrent id.")
        await call(
            "torrent-set-location", ids=_ids(torrent_ids), location=location, move=move_data
        )
        return {"torrent_ids": torrent_ids, "location": location, "moved_data": move_data}

    @mcp.tool
    async def rename_torrent_path(
        torrent_id: Annotated[int | str, Field(description="A single torrent id or info hash.")],
        path: Annotated[
            str,
            Field(
                description=(
                    "Current path of the file or folder to rename, relative to the "
                    "torrent root (the torrent's own name renames the top folder)."
                )
            ),
        ],
        new_name: Annotated[str, Field(description="The new name, without any directory part.")],
    ) -> dict[str, Any]:
        """Rename a file or folder inside a torrent."""
        result = await call(
            "torrent-rename-path", ids=_ids([torrent_id]), path=path, name=new_name
        )
        return {
            "torrent_id": result.get("id", torrent_id),
            "path": result.get("path", path),
            "name": result.get("name", new_name),
        }

    @mcp.tool
    async def set_session_properties(
        download_dir: Annotated[
            str | None, Field(description="Default download directory for new torrents.")
        ] = None,
        speed_limit_down_kbps: Annotated[
            int | None,
            Field(ge=0, description="Global download cap in kB/s. 0 removes the cap."),
        ] = None,
        speed_limit_up_kbps: Annotated[
            int | None, Field(ge=0, description="Global upload cap in kB/s. 0 removes the cap.")
        ] = None,
        alt_speed_enabled: Annotated[
            bool | None, Field(description="Switch the alternative ('turtle') speed limits on or off.")
        ] = None,
        alt_speed_down_kbps: Annotated[
            int | None, Field(ge=0, description="Alternative download limit in kB/s.")
        ] = None,
        alt_speed_up_kbps: Annotated[
            int | None, Field(ge=0, description="Alternative upload limit in kB/s.")
        ] = None,
        download_queue_size: Annotated[
            int | None,
            Field(ge=0, description="Max simultaneous downloads. 0 disables the queue limit."),
        ] = None,
        seed_ratio_limit: Annotated[
            float | None,
            Field(ge=0, description="Default seed ratio for all torrents. 0 removes the limit."),
        ] = None,
        start_added_torrents: Annotated[
            bool | None, Field(description="Whether newly added torrents start automatically.")
        ] = None,
        peer_limit_global: Annotated[
            int | None, Field(ge=1, description="Maximum peers across all torrents.")
        ] = None,
        encryption: Annotated[
            Literal["required", "preferred", "tolerated"] | None,
            Field(description="Peer connection encryption policy."),
        ] = None,
        blocklist_url: Annotated[
            str | None,
            Field(
                description=(
                    "URL of a peer blocklist to fetch. Transmission ships with a "
                    "placeholder URL that does not resolve, so this has to be set to "
                    "a real one before update_blocklist can work."
                )
            ),
        ] = None,
        blocklist_enabled: Annotated[
            bool | None, Field(description="Whether the blocklist is applied to peers.")
        ] = None,
    ) -> dict[str, Any]:
        """Change Transmission's global settings: directories, speed limits, queue, encryption."""
        arguments: dict[str, Any] = {}
        if download_dir is not None:
            arguments["download-dir"] = download_dir
        if speed_limit_down_kbps is not None:
            arguments["speed-limit-down-enabled"] = speed_limit_down_kbps > 0
            arguments["speed-limit-down"] = speed_limit_down_kbps
        if speed_limit_up_kbps is not None:
            arguments["speed-limit-up-enabled"] = speed_limit_up_kbps > 0
            arguments["speed-limit-up"] = speed_limit_up_kbps
        if alt_speed_enabled is not None:
            arguments["alt-speed-enabled"] = alt_speed_enabled
        if alt_speed_down_kbps is not None:
            arguments["alt-speed-down"] = alt_speed_down_kbps
        if alt_speed_up_kbps is not None:
            arguments["alt-speed-up"] = alt_speed_up_kbps
        if download_queue_size is not None:
            arguments["download-queue-enabled"] = download_queue_size > 0
            arguments["download-queue-size"] = download_queue_size
        if seed_ratio_limit is not None:
            arguments["seedRatioLimited"] = seed_ratio_limit > 0
            arguments["seedRatioLimit"] = seed_ratio_limit
        if start_added_torrents is not None:
            arguments["start-added-torrents"] = start_added_torrents
        if peer_limit_global is not None:
            arguments["peer-limit-global"] = peer_limit_global
        if encryption is not None:
            arguments["encryption"] = encryption
        if blocklist_url is not None:
            arguments["blocklist-url"] = blocklist_url
        if blocklist_enabled is not None:
            arguments["blocklist-enabled"] = blocklist_enabled

        if not arguments:
            raise ToolError("Nothing to change: pass at least one setting.")
        await call("session-set", **arguments)
        return {"changed": sorted(arguments)}

    @mcp.tool
    async def test_port() -> dict[str, Any]:
        """Check whether Transmission's incoming peer port is reachable from the internet."""
        result = await call("port-test")
        return {"port_is_open": result.get("port-is-open")}

    @mcp.tool
    async def update_blocklist() -> dict[str, Any]:
        """Re-download the configured peer blocklist and report its new size.

        A fresh Transmission ships with a placeholder `blocklist-url` that 404s,
        so this fails until a real list URL is set with `set_session_properties`.
        """
        result = await call("blocklist-update")
        return {"blocklist_size": result.get("blocklist-size")}

    if settings.allow_shutdown:

        @mcp.tool(annotations={"destructiveHint": True})
        async def shutdown_session() -> dict[str, Any]:
            """Shut Transmission down. It will not restart unless something else restarts it."""
            await call("session-close")
            return {"shutdown": True}

    return mcp


def run() -> None:
    """Entry point: build the server and serve it over the configured transport."""
    settings = load_settings()
    mcp = build_server(settings)
    if settings.transport == "stdio":
        # The banner would otherwise share stdout with the JSON-RPC stream.
        mcp.run(transport="stdio", show_banner=False)
    else:
        mcp.run(
            transport=settings.transport,
            host=settings.host,
            port=settings.port,
            path=settings.path,
            log_level=os.getenv("LOG_LEVEL", "info").lower(),
        )
