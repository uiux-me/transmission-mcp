# transmission-mcp

A [FastMCP](https://gofastmcp.com) server that searches public torrent indexers
and drives [Transmission](https://transmissionbt.com/), packaged for Docker.

It exists so you can hold one conversation — *"find me the latest episode of X
and download it"* — and have an MCP client (Claude Code, Claude Desktop, or
anything else that speaks MCP) do the searching, the choosing, and the
downloading without you touching a web UI.

```
search_torrents("shogun s01e05")   →  ranked results, each with an info_hash
add_torrent(info_hash="a1b2…")     →  queued in Transmission
list_torrents()                    →  progress, speed, ETA, peers
```

The compose file can run Transmission for you, or the server can point at a
Transmission you already have.

---

## Contents

- [How it works](#how-it-works)
- [Quick start](#quick-start)
- [Connecting a client](#connecting-a-client)
- [Search sources](#search-sources)
- [Tool reference](#tool-reference)
- [Resources](#resources)
- [Response shaping](#response-shaping)
- [Configuration](#configuration)
- [Deployment options](#deployment-options)
- [Safety and permissions](#safety-and-permissions)
- [Troubleshooting](#troubleshooting)
- [Local development](#local-development)
- [Project layout](#project-layout)
- [Compatibility](#compatibility)
- [A note on what you download](#a-note-on-what-you-download)

---

## How it works

```
        MCP client (Claude Code / Desktop)
                     │  MCP over HTTP or stdio
                     ▼
        ┌────────────────────────────┐
        │      transmission-mcp      │
        │                            │
        │  search layer ──► 7 public torrent indexers
        │      │                     │      (plain HTTPS, JSON + RSS)
        │      │ info_hash           │
        │      ▼                     │
        │  RPC client  ──────────────┼──►  Transmission
        └────────────────────────────┘      /transmission/rpc
```

**The info hash is the pivot.** Every search result is keyed by a 40-character
hex info hash rather than a magnet link. A magnet URI is a kilobyte or more of
tracker parameters; an info hash is 40 characters, and a magnet can always be
rebuilt from it. So results stay cheap in the model's context, and
`add_torrent(info_hash=…)` reconstructs the magnet at the moment it is needed.

Results are also cached for 15 minutes (`SEARCH_CACHE_TTL`), which lets
`add_torrent` recover the display name and the trackers the indexer advertised.
If the cache has expired, adding still works — the hash alone is enough to find
peers over DHT, and a standard tracker list is appended.

Because every indexer is reached over plain HTTPS — JSON APIs and RSS feeds, no
headless browser — the image is 351 MB and starts instantly.

---

## Quick start

The bundled stack runs Transmission and this server together, wired up:

```bash
git clone <this repo> && cd transmission-mcp
cp .env.example .env
```

Edit `.env` and set `PUID`/`PGID` to your own user so downloaded files belong to
you rather than to root:

```bash
id -u    # → PUID
id -g    # → PGID
```

Then:

```bash
docker compose up -d
```

That gives you:

| | |
|---|---|
| MCP endpoint | <http://localhost:8000/mcp> |
| Transmission web UI | <http://localhost:9091> |
| Downloads | `./downloads/complete` |
| Transmission state | `./config` |

Check both containers came up healthy:

```bash
docker compose ps
```

`transmission-mcp` reports `healthy` only once the MCP port is listening **and**
Transmission answers an RPC call, so a `healthy` here means the whole path
works — not just that the process started.

To use a different port, set `MCP_PORT` in `.env` (for example `MCP_PORT=8069`)
and run `docker compose up -d` again.

---

## Connecting a client

### Claude Code

```bash
claude mcp add --transport http transmission http://localhost:8000/mcp
```

### Claude Desktop / any stdio client

Add to your MCP config, running the container per session:

```json
{
  "mcpServers": {
    "transmission": {
      "command": "docker",
      "args": [
        "run", "--rm", "-i",
        "-e", "MCP_TRANSPORT=stdio",
        "-e", "TRANSMISSION_URL=http://host.docker.internal:9091",
        "-e", "TRANSMISSION_PASSWORD=your-password",
        "transmission-mcp"
      ]
    }
  }
}
```

If you are already running the HTTP stack, pointing a second client at
`http://localhost:8000/mcp` is simpler and cheaper than launching a container
per session.

### Verifying the connection

```bash
curl -s -X POST http://localhost:8000/mcp \
  -H 'Content-Type: application/json' \
  -H 'Accept: application/json, text/event-stream' \
  -d '{"jsonrpc":"2.0","id":1,"method":"initialize","params":{"protocolVersion":"2024-11-05","capabilities":{},"clientInfo":{"name":"curl","version":"1"}}}'
```

---

## Search sources

Seven indexers, all queried in parallel on every search:

| Source | Covers | Transport | Notes |
| --- | --- | --- | --- |
| `thepiratebay` | Movies, TV, music, games, software | JSON API | Reached through TPB's own `apibay.org` API |
| `yts` | Movies, small high-quality encodes | JSON API | Mirrors `yts.gg`, `movies-api.accel.li`, `yts.am` |
| `nyaa` | Anime and Asian media | RSS | Rich metadata: seeders, leechers, completed count |
| `eztv` | TV episodes | JSON API | See caveat below |
| `subsplease` | Currently-airing anime | JSON API | Prefers 1080p, falls back to 720p then 480p |
| `fitgirl` | PC game repacks | RSS | Publishes no seeder counts, so these rank last |
| `bittorrented` | DHT-indexed video, long tail | JSON API | Ignores queries under 3 characters |

**EZTV caveat.** The EZTV API has no text search — it only pages through recent
releases. This server fetches the newest 200 entries and matches your query
against them client-side. That makes EZTV excellent for *this week's* episodes
and useless for anything older. A back-catalogue episode will come from
`thepiratebay` or `bittorrented` instead.

**FitGirl caveat.** No seeder or leecher counts are published, so those results
sort to the bottom by health even when they are the best available. For PC game
repacks specifically, look past the ranking.

### What's deliberately missing

**1337x** and The Pirate Bay's **HTML site** both sit behind Cloudflare and
return HTTP 403 to any non-browser client — verified with a full browser header
set, not just a bare user agent. Reaching them means shipping Chromium via
crawl4ai, which takes the image from ~350 MB to several gigabytes. The Pirate
Bay's catalogue is available through its own JSON API regardless, which is what
`thepiratebay` uses here.

If you need those two specifically, [torrent-search-mcp](https://github.com/philogicae/torrent-search-mcp)
runs them with a real browser and is the better tool for that job.

### Merging and ranking

Results from all sources are deduplicated by info hash — the same torrent found
on three indexers appears once — and sorted by health, defined as
`seeders × 3 + leechers`. A source that is down, blocked, or has changed shape
is dropped from that search and named in the `sources_failed` field, so an
empty result set is always distinguishable from a broken indexer.

### Restricting sources

Globally, via environment:

```bash
SEARCH_SOURCES=nyaa,subsplease          # allowlist
SEARCH_EXCLUDE_SOURCES=fitgirl          # denylist, applied on top
```

Or per call, with the `sources` argument to `search_torrents`.

---

## Tool reference

17 tools. `shutdown_session` only registers when explicitly enabled, so a
default install exposes 16.

### `search_torrents`

Search every enabled indexer in parallel.

| Parameter | Type | Default | Description |
| --- | --- | --- | --- |
| `query` | string | *required* | Lowercase, space-separated keywords |
| `sources` | string[] | all enabled | Restrict to these indexers |
| `limit` | int 1–100 | `20` | Max results returned |
| `include_magnets` | bool | `false` | Include full magnet links |

**Writing good queries.** Drop filler words and generic terms — no "torrent",
"download", "movie", "the", "a". Use `show sXXeYY` for one episode, `show sXX`
for a season, bare `show` for the whole series. Only add quality tags like
`1080p` or `h265` when the user actually asked for them; adding them
pre-emptively excludes better results.

```json
{
  "query": "ubuntu 24.04",
  "total": 2,
  "returned": 1,
  "sources_searched": ["thepiratebay", "yts", "nyaa", "eztv",
                       "subsplease", "fitgirl", "bittorrented"],
  "results": [
    {
      "info_hash": "4a3f5e08bcef825718eda30637230585e3330599",
      "name": "ubuntu-24.04.1-desktop-amd64.iso",
      "source": "thepiratebay",
      "size_bytes": 6203355136,
      "size_human": "5.78 GiB",
      "seeders": 53,
      "leechers": 7,
      "published": "2024-09-08",
      "category": "Applications"
    }
  ]
}
```

When an indexer fails, a `sources_failed` object is added naming each one and
why. Magnets are omitted unless `include_magnets` is set, because
`add_torrent` only needs the `info_hash`.

### `add_torrent`

Add a torrent by search result, magnet, URL, or file. Give **exactly one** of
`info_hash`, `torrent`, or `content_base64`.

| Parameter | Type | Default | Description |
| --- | --- | --- | --- |
| `info_hash` | string | — | From a `search_torrents` result — the normal path |
| `torrent` | string | — | A magnet URI, an `http(s)` URL to a `.torrent`, or a local file path |
| `content_base64` | string | — | Base64-encoded `.torrent` contents |
| `download_dir` | string | session default | Path **inside the Transmission container** |
| `paused` | bool | `false` | Add without starting |
| `labels` | string[] | — | Tags to apply |
| `priority` | `low`/`normal`/`high` | `normal` | Bandwidth priority |
| `peer_limit` | int | — | Max peers for this torrent |

The `torrent` parameter auto-detects what it was given: anything starting
`magnet:`, `http://`, or `https://` is handed to Transmission as-is; anything
else is treated as a path this server can read, and its bytes are sent inline.

```json
{
  "id": 2,
  "name": "ubuntu-24.04.1-desktop-amd64.iso",
  "info_hash": "4a3f5e08bcef825718eda30637230585e3330599",
  "duplicate": false,
  "paused": false
}
```

Adding a torrent Transmission already has is **not** an error — you get
`"duplicate": true` and the existing torrent's id, so the caller can decide
what to do rather than accidentally creating a second copy.

### `list_torrents`

| Parameter | Type | Default | Description |
| --- | --- | --- | --- |
| `status_filter` | status name | — | One of the seven status values below |
| `name_contains` | string | — | Case-insensitive substring match |
| `limit` | int 1–500 | `50` | Max torrents returned |
| `verbose` | bool | `false` | Return raw RPC fields instead of the summary |

Status values: `stopped`, `check_pending`, `checking`, `download_pending`,
`downloading`, `seed_pending`, `seeding`.

```json
{
  "total": 2,
  "returned": 2,
  "torrents": [
    {
      "id": 2,
      "name": "ubuntu-24.04.1-desktop-amd64.iso",
      "info_hash": "4a3f5e08bcef825718eda30637230585e3330599",
      "status": "downloading",
      "progress_percent": 2.15,
      "total_bytes": 6203355136,
      "remaining_bytes": 6069878784,
      "downloaded_bytes": 133476352,
      "uploaded_bytes": 0,
      "upload_ratio": 0.0,
      "download_rate_bytes_per_sec": 14073856,
      "upload_rate_bytes_per_sec": 0,
      "eta_seconds": 748,
      "peers_connected": 35,
      "peers_sending_to_us": 32,
      "peers_getting_from_us": 0,
      "download_dir": "/downloads/complete",
      "queue_position": 1,
      "priority": "normal",
      "labels": [],
      "is_finished": false,
      "is_stalled": false,
      "added_at": "2026-08-21T03:52:18+00:00",
      "completed_at": null
    }
  ]
}
```

`total` always reports the full count before `limit` was applied, so a caller
can tell truncation from exhaustion. A torrent in error state gains `error`
(`tracker_warning`, `tracker_error`, or `local_error`) and `error_message`.

### `get_torrent`

Everything Transmission knows about one torrent.

| Parameter | Type | Default | Description |
| --- | --- | --- | --- |
| `torrent_id` | int or string | *required* | Id from `list_torrents`, or an info hash |
| `include_files` | bool | `false` | File list with per-file progress and priority |
| `include_trackers` | bool | `false` | Tracker announce results and peer counts |
| `include_peers` | bool | `false` | Currently connected peers |

The three `include_*` flags control what is requested from Transmission, not
just what is filtered afterwards — leaving them off makes the RPC call cheaper
as well as the response smaller.

Beyond the summary fields, the response adds `magnet_link`, `is_private`,
`file_count`, `piece_count`, `verified_bytes`, `corrupt_bytes`,
`seconds_downloading`, `seconds_seeding`, per-torrent speed limits, seed-ratio
rules, and more. Fields Transmission left empty are dropped rather than
returned as nulls.

With `include_files`, each file gets `index`, `name`, `total_bytes`,
`completed_bytes`, `progress_percent`, `wanted`, and `priority`. Those `index`
values are what `set_file_priorities` takes.

### `manage_torrents`

| Parameter | Type | Default | Description |
| --- | --- | --- | --- |
| `action` | enum | *required* | See below |
| `torrent_ids` | (int\|string)[] | *required* | Ids, info hashes, or `["recently-active"]` |

Actions: `start`, `start_now` (ignores the queue), `stop`, `verify`,
`reannounce`, `queue_top`, `queue_up`, `queue_down`, `queue_bottom`.

**Transmission applies these asynchronously** and can take a second or so to
reflect the new state. A `list_torrents` immediately afterwards may still show
the old status — that is not a failure, and retrying the action is wrong.

### `remove_torrents`

| Parameter | Type | Default | Description |
| --- | --- | --- | --- |
| `torrent_ids` | (int\|string)[] | *required* | Ids or info hashes |
| `delete_data` | bool | `false` | Also delete downloaded files from disk |

`delete_data: true` is refused unless `TRANSMISSION_ALLOW_REMOVE_DATA=true`, and
the refusal happens before anything reaches Transmission. See
[Safety and permissions](#safety-and-permissions).

### `set_torrent_properties`

Change one or more properties on one or more torrents. At least one property
must be given.

| Parameter | Type | Description |
| --- | --- | --- |
| `torrent_ids` | (int\|string)[] | *required* |
| `download_limit_kbps` | int ≥ 0 | Per-torrent download cap; `0` removes it |
| `upload_limit_kbps` | int ≥ 0 | Per-torrent upload cap; `0` removes it |
| `priority` | `low`/`normal`/`high` | Bandwidth priority |
| `labels` | string[] | **Replaces** the label set |
| `seed_ratio_limit` | float ≥ 0 | Stop seeding at this ratio |
| `use_global_seed_ratio` | bool | Restore the session-wide ratio |
| `honors_session_limits` | bool | Whether global speed limits apply |
| `peer_limit` | int ≥ 1 | Max peers |
| `queue_position` | int ≥ 0 | Absolute position; only sensible for one torrent |

Setting `seed_ratio_limit` also switches the torrent to its own ratio mode;
`use_global_seed_ratio: true` switches it back. Setting a speed limit to `0`
disables the limit rather than throttling to zero.

### `set_file_priorities`

Choose which files inside a torrent to download. Indices come from
`get_torrent(include_files=true)`.

| Parameter | Type | Description |
| --- | --- | --- |
| `torrent_id` | int or string | *required* |
| `wanted` | int[] | Indices to download |
| `unwanted` | int[] | Indices to skip |
| `high` / `normal` / `low` | int[] | Indices at each priority |

Useful for a season pack where only some episodes are wanted, or for skipping
sample files and extras.

### `move_torrent`

| Parameter | Type | Default | Description |
| --- | --- | --- | --- |
| `torrent_ids` | (int\|string)[] | *required* | |
| `location` | string | *required* | New directory inside the Transmission container |
| `move_data` | bool | `true` | `true` moves the files; `false` says they are already there |

`move_data: false` is how you tell Transmission you moved files yourself and it
should look for them in the new place.

### `rename_torrent_path`

| Parameter | Type | Description |
| --- | --- | --- |
| `torrent_id` | int or string | *required*, a single torrent |
| `path` | string | Current path relative to the torrent root |
| `new_name` | string | New name, no directory part |

Passing the torrent's own name as `path` renames the top-level folder.

### `set_session_properties`

Global Transmission settings. At least one must be given.

| Parameter | Type | Description |
| --- | --- | --- |
| `download_dir` | string | Default directory for new torrents |
| `speed_limit_down_kbps` | int ≥ 0 | Global download cap; `0` removes it |
| `speed_limit_up_kbps` | int ≥ 0 | Global upload cap; `0` removes it |
| `alt_speed_enabled` | bool | Turtle mode on/off |
| `alt_speed_down_kbps` | int ≥ 0 | Turtle download limit |
| `alt_speed_up_kbps` | int ≥ 0 | Turtle upload limit |
| `download_queue_size` | int ≥ 0 | Max simultaneous downloads; `0` disables the limit |
| `seed_ratio_limit` | float ≥ 0 | Default ratio for all torrents |
| `start_added_torrents` | bool | Whether new torrents auto-start |
| `peer_limit_global` | int ≥ 1 | Max peers across all torrents |
| `encryption` | `required`/`preferred`/`tolerated` | Peer encryption policy |
| `blocklist_url` | string | URL of a peer blocklist to fetch |
| `blocklist_enabled` | bool | Whether the blocklist is applied |

### `get_session`

Version, directories, limits, and protocol settings. `verbose: true` returns
the raw struct.

```json
{
  "version": "4.1.3 (838877323f)",
  "rpc_version": 19,
  "rpc_version_semver": "6.0.1",
  "download_dir": "/downloads/complete",
  "incomplete_dir": "/downloads/incomplete",
  "peer_port": 51413,
  "port_forwarding_enabled": true,
  "encryption": "preferred",
  "speed_limit_down_kbps": null,
  "speed_limit_up_kbps": null,
  "alt_speed_enabled": false,
  "download_queue_size": 5,
  "start_added_torrents": true,
  "peer_limit_global": 200,
  "dht_enabled": true,
  "pex_enabled": true,
  "blocklist_enabled": false,
  "blocklist_size": 0,
  "config_dir": "/config"
}
```

A limit reads as `null` when its enable flag is off, so `null` means
"unlimited" rather than "unknown" — you never have to cross-reference a
separate `*-enabled` field.

### `get_session_stats`

```json
{
  "torrent_count": 2,
  "active_torrent_count": 1,
  "paused_torrent_count": 1,
  "download_rate_bytes_per_sec": 15581184,
  "upload_rate_bytes_per_sec": 0,
  "current_session": {
    "downloaded_bytes": 2241028644,
    "uploaded_bytes": 0,
    "files_added": 2,
    "session_count": 1,
    "seconds_active": 1483
  },
  "cumulative": { "…": "same shape, since first run" }
}
```

### `get_free_space`

| Parameter | Type | Default | Description |
| --- | --- | --- | --- |
| `path` | string | session download dir | Directory to check |

Falls back to `TRANSMISSION_DOWNLOAD_DIR`, then to Transmission's own
`download-dir`. The path must exist **inside the Transmission container**.

### `test_port`

No parameters. Returns `{"port_is_open": bool}` — whether the incoming peer
port is reachable from the internet. `false` usually means port forwarding is
not set up, which slows downloads but does not break them.

### `update_blocklist`

No parameters. Re-downloads the configured peer blocklist and returns its new
size.

A fresh Transmission ships with the placeholder `blocklist-url`
`http://www.example.com/blocklist`, which 404s — so this fails until you set a
real one:

```
set_session_properties(blocklist_url="https://…/bt_blocklists.gz",
                       blocklist_enabled=true)
update_blocklist()   →  {"blocklist_size": 462562}
```

### `shutdown_session`

No parameters. Shuts Transmission down. Only registered when
`TRANSMISSION_ALLOW_SHUTDOWN=true`. Nothing restarts Transmission afterwards
unless your container restart policy does.

---

## Resources

For clients that prefer resources to tools:

| URI | Contents |
| --- | --- |
| `transmission://torrents` | Current torrent list, same shape as `list_torrents` |
| `transmission://session` | Session settings, same shape as `get_session` |
| `transmission://stats` | Transfer statistics |
| `search://sources` | Available indexers, their descriptions, and whether each is enabled |

---

## Response shaping

Transmission's raw RPC is not pleasant to read. It uses numeric enums, `0.0`–`1.0`
fractions, negative sentinels for "unknown", Unix timestamps, and returns
dozens of fields per torrent whether you want them or not.

This server:

- **Names the enums.** `"status": 4` becomes `"status": "downloading"`;
  `"error": 3` becomes `"error": "local_error"`; `bandwidthPriority: 1` becomes
  `"priority": "high"`.
- **Converts fractions to percentages.** `percentDone: 0.7512` becomes
  `progress_percent: 75.12`, clamped to 0–100.
- **Turns sentinels into `null`.** Transmission's `eta` of `-1` ("unknown") and
  `-2` ("not applicable") both become `null`, so arithmetic on the field is
  safe.
- **Converts timestamps to ISO-8601 UTC.** `addedDate: 1700000000` becomes
  `"added_at": "2023-11-14T22:13:20+00:00"`. A `0` timestamp means "never" and
  becomes `null`.
- **Folds enable flags into their values.** A speed limit reads as `null` when
  disabled instead of returning a stale number alongside a separate boolean.
- **Drops empty fields** from detail responses rather than returning nulls.
- **Names units in the field itself** — `total_bytes`,
  `download_rate_bytes_per_sec`, `download_limit_kbps`, `eta_seconds` — so
  there is nothing to infer.

Sizes are bytes, rates are bytes per second, and speed limits are kB/s
(Transmission's own unit for limits — the asymmetry is theirs, not ours).

Pass `verbose: true` to `list_torrents` or `get_session` when you want the raw
struct instead.

---

## Configuration

Everything is environment variables. Copy `.env.example` to `.env` and edit.

### Transmission connection

| Variable | Default | Purpose |
| --- | --- | --- |
| `TRANSMISSION_URL` | — | Base URL, e.g. `http://transmission:9091`. `/transmission/rpc` is appended if absent. Credentials may be embedded. Takes precedence over the three below. |
| `TRANSMISSION_SCHEME` | `http` | Used only when `TRANSMISSION_URL` is unset |
| `TRANSMISSION_HOST` | `localhost` | Used only when `TRANSMISSION_URL` is unset |
| `TRANSMISSION_PORT` | `9091` | Used only when `TRANSMISSION_URL` is unset |
| `TRANSMISSION_USERNAME` | — | RPC username, if authentication is on |
| `TRANSMISSION_PASSWORD` | — | RPC password |
| `TRANSMISSION_TIMEOUT` | `30` | HTTP timeout in seconds |
| `TRANSMISSION_VERIFY_SSL` | `true` | Set `false` for a self-signed HTTPS certificate |
| `TRANSMISSION_DOWNLOAD_DIR` | — | Default `download_dir` for `add_torrent`, as a path **inside the Transmission container** |

`TRANSMISSION_URL` is forgiving. All of these work:

```
http://transmission:9091                    → http://transmission:9091/transmission/rpc
transmission:9091                           → http://transmission:9091/transmission/rpc
http://transmission:9091/transmission/rpc   → unchanged
https://home.example/tm                     → https://home.example/tm/transmission/rpc
http://joe:hunter2@box:9091                 → credentials extracted, kept out of the request URL
```

The sub-path form is for Transmission behind a reverse proxy. Explicit
`TRANSMISSION_USERNAME`/`TRANSMISSION_PASSWORD` win over credentials embedded
in the URL.

### Safety

| Variable | Default | Purpose |
| --- | --- | --- |
| `TRANSMISSION_READ_ONLY` | `false` | Register only the read tools and search |
| `TRANSMISSION_ALLOW_REMOVE_DATA` | `false` | Permit `remove_torrents(delete_data=true)` |
| `TRANSMISSION_ALLOW_SHUTDOWN` | `false` | Register `shutdown_session` |

### Search

| Variable | Default | Purpose |
| --- | --- | --- |
| `SEARCH_ENABLED` | `true` | `false` exposes Transmission control only |
| `SEARCH_SOURCES` | all | Comma-separated allowlist |
| `SEARCH_EXCLUDE_SOURCES` | — | Comma-separated denylist, applied on top |
| `SEARCH_TIMEOUT` | `20` | Per-request timeout for indexers, in seconds |
| `SEARCH_CACHE_TTL` | `900` | Seconds a result stays cached for `add_torrent` |

An unknown name in either list is a startup error rather than a silent no-op,
so a typo surfaces immediately instead of quietly dropping a source.

### MCP server

| Variable | Default | Purpose |
| --- | --- | --- |
| `MCP_TRANSPORT` | `http` | `http` or `stdio` |
| `MCP_HOST` | `0.0.0.0` | HTTP bind address |
| `MCP_PORT` | `8000` | Host port published by compose |
| `MCP_PATH` | `/mcp` | HTTP endpoint path |
| `LOG_LEVEL` | `info` | Uvicorn log level |

### Bundled Transmission container

| Variable | Default | Purpose |
| --- | --- | --- |
| `PUID` / `PGID` | `1000` | User/group owning downloaded files — set to your own |
| `TZ` | `UTC` | Timezone for Transmission's scheduling |
| `TRANSMISSION_WEB_PORT` | `9091` | Host port for the web UI |
| `TRANSMISSION_PEER_PORT` | `51413` | Host port for peer traffic, TCP and UDP |

---

## Deployment options

### Bundled stack (default)

```bash
docker compose up -d
```

Runs both containers on a private network. The MCP server reaches Transmission
as `http://transmission:9091`.

### Against an existing Transmission

Set `TRANSMISSION_URL` in `.env` to your instance, then start only the MCP
service:

```bash
docker compose up -d mcp
```

Use `http://host.docker.internal:9091` for a Transmission on the Docker host
(macOS and Windows), or its LAN address.

### Plain docker run

```bash
docker build -t transmission-mcp .
docker run --rm -p 8000:8000 \
  -e TRANSMISSION_URL=http://host.docker.internal:9091 \
  -e TRANSMISSION_USERNAME=transmission \
  -e TRANSMISSION_PASSWORD=your-password \
  transmission-mcp
```

### Without Docker

```bash
python -m venv .venv && .venv/bin/pip install -e .
TRANSMISSION_URL=http://localhost:9091 .venv/bin/transmission-mcp
```

---

## Safety and permissions

The destructive operations are gated, because an agent acting on a
misunderstanding should not be able to delete your media library.

**Deleting files is off by default.** `remove_torrents` will remove a torrent
from Transmission freely, but `delete_data: true` is refused unless
`TRANSMISSION_ALLOW_REMOVE_DATA=true`. The refusal happens in this server,
before any RPC call is made, and the error explains both how to enable it and
how to proceed without it.

**Shutting Transmission down is off by default.** `shutdown_session` is not
registered at all unless `TRANSMISSION_ALLOW_SHUTDOWN=true` — the model cannot
call a tool it cannot see.

**Read-only mode.** `TRANSMISSION_READ_ONLY=true` registers exactly six tools:
`search_torrents`, `list_torrents`, `get_torrent`, `get_session`,
`get_session_stats`, and `get_free_space`. Nothing can add, modify, or remove
anything. Useful for a client you want to be able to *look* at your setup.

**Tool annotations.** Read tools carry `readOnlyHint`; `remove_torrents` and
`shutdown_session` carry `destructiveHint`; `search_torrents` carries
`openWorldHint`. Clients that surface these can prompt accordingly.

**Credentials.** Transmission's RPC uses HTTP basic auth, which is base64
encoding, not encryption. Do not expose it across an untrusted network without
TLS in front.

---

## Troubleshooting

### `HTTP 421` / "Transmission refused the request host"

Transmission's DNS-rebinding protection rejects requests whose `Host:` header
is not an IP address, `localhost`, or an entry in `rpc-host-whitelist`.
Reaching it as `http://transmission:9091` over a compose network trips exactly
this.

The bundled service already sets `HOST_WHITELIST: "transmission,localhost,127.0.0.1"`.
For a Transmission you run yourself, either add the hostname to
`rpc-host-whitelist` in its `settings.json`, or set
`rpc-host-whitelist-enabled: false`.

### Every search returns nothing at once

Several torrent indexers are blocked at the DNS level by consumer ISPs. If all
seven sources fail simultaneously — check `sources_failed` in the response —
that is almost always why. `docker-compose.yml` has a commented-out `dns:`
block pinned to Quad9:

```yaml
dns:
  - 9.9.9.9
  - 149.112.112.112
```

Uncomment it and `docker compose up -d` again.

If only *some* sources fail, that is normal — indexers go down, change shape,
and come back. The search still returns what the others found.

### `get_free_space` says "No such file or directory"

The path must exist inside the *Transmission* container, not on your host.
`/downloads/complete` inside the container is `./downloads/complete` here. The
compose file names both download directories as mounts so Docker creates them
at startup; if you changed `download-dir` to somewhere else, create it first.

### `update_blocklist` returns a 404

Expected on a fresh install — Transmission's stock `blocklist-url` is the
placeholder `http://www.example.com/blocklist`. Set a real one with
`set_session_properties(blocklist_url=…, blocklist_enabled=true)` first.

### `test_port` reports the port is closed

Peer port forwarding is not set up on your router. Downloads still work — you
can connect out to peers, they just cannot connect in to you, which reduces the
peer pool. Enable UPnP on the router, or forward `TRANSMISSION_PEER_PORT`
manually.

### A `stop` or `start` seems not to take effect

Transmission applies queue actions asynchronously; the status field catches up
within a second or so. Read it again rather than repeating the action.

### Container is `unhealthy`

The healthcheck tests two things — that the MCP port is listening, and that
Transmission answers `session-get`. Check the logs:

```bash
docker compose logs mcp
docker compose exec mcp python -m transmission_mcp.healthcheck
```

The healthcheck prints the specific failure to stderr.

### Downloaded files are owned by the wrong user

`PUID`/`PGID` in `.env` did not match your user. Set them from `id -u` and
`id -g`, then `docker compose up -d` and `chown` anything already downloaded.

---

## Local development

```bash
python -m venv .venv
.venv/bin/pip install -e ".[dev]"
.venv/bin/pytest
```

134 tests, no network access required — they run against a fake Transmission
RPC endpoint and fake indexer responses over `httpx.MockTransport`, including
the CSRF handshake.

Run the server against a local Transmission:

```bash
TRANSMISSION_URL=http://localhost:9091 MCP_TRANSPORT=stdio .venv/bin/transmission-mcp
```

Try the search layer on its own, no Transmission needed:

```bash
.venv/bin/python -c "
import asyncio
from transmission_mcp.search import TorrentSearch
async def main():
    s = TorrentSearch()
    results, report = await s.search('ubuntu 24.04')
    print(report)
    for r in results[:5]:
        print(r.seeders, r.source, r.name)
    await s.aclose()
asyncio.run(main())"
```

---

## Project layout

```
src/transmission_mcp/
├── client.py        Async Transmission RPC client; owns the 409 CSRF handshake
├── config.py        Environment parsing into a frozen Settings dataclass
├── normalize.py     Raw RPC structs → readable summaries
├── server.py        FastMCP server: every tool and resource
├── healthcheck.py   Container healthcheck entry point
└── search/
    ├── common.py    SearchResult, magnet/info-hash handling, shared HTTP fetcher
    ├── sources.py   One fetcher per indexer
    └── registry.py  Parallel fan-out, dedup, ranking, TTL cache

tests/
├── conftest.py        Fake Transmission RPC + fake indexers
├── test_client.py     CSRF handshake, auth, error mapping
├── test_config.py     Environment parsing
├── test_normalize.py  Enum naming, unit conversion, sentinels
├── test_search.py     Per-source parsing, merging, caching
└── test_server.py     End-to-end tool calls through an MCP client
```

The dependency surface is deliberately small: **fastmcp** and **httpx**, nothing
else. Python 3.10+.

---

## Compatibility

Speaks Transmission's classic RPC protocol (kebab/camelCase keys), which works
from Transmission 2.80 through 4.1+. Transmission 4.1 added a JSON-RPC 2.0
flavour with snake_case keys and deprecated the classic one, but still accepts
it; when that support is eventually dropped, `client.py` is the only file that
needs to change.

Verified end to end against **Transmission 4.1.3** (RPC version 19, semver
6.0.1), which is what the bundled `linuxserver/transmission` image currently
ships.

---

## A note on what you download

This server searches public indexers and will find whatever is on them. What
you are entitled to download is between you and the law where you live.

---

## Sources

- [Transmission](https://transmissionbt.com/)
- [Transmission RPC specification](https://github.com/transmission/transmission/blob/main/docs/rpc-spec.md)
- [linuxserver/transmission](https://docs.linuxserver.io/images/docker-transmission/) — the bundled image
- [torrent-search-mcp](https://github.com/philogicae/torrent-search-mcp) — the reference for which indexers are worth querying
- [FastMCP](https://gofastmcp.com)
