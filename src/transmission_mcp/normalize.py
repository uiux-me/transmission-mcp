"""Turn Transmission's RPC structs into something an LLM can read cheaply.

The raw API answers with numeric enums, ratios expressed as fractions, ETAs
encoded as negative sentinels, and dozens of fields per torrent. These helpers
name the enums, convert to ISO-8601 and percentages, and return a curated
subset by default.
"""

from __future__ import annotations

from datetime import datetime, timezone
from typing import Any

STATUSES = {
    0: "stopped",
    1: "check_pending",
    2: "checking",
    3: "download_pending",
    4: "downloading",
    5: "seed_pending",
    6: "seeding",
}

ERRORS = {
    0: None,
    1: "tracker_warning",
    2: "tracker_error",
    3: "local_error",
}

PRIORITIES = {-1: "low", 0: "normal", 1: "high"}
PRIORITY_VALUES = {"low": -1, "normal": 0, "high": 1}

RATIO_MODES = {0: "global", 1: "single", 2: "unlimited"}
IDLE_MODES = {0: "global", 1: "single", 2: "unlimited"}

#: Fields fetched for the default (summary) torrent listing.
SUMMARY_FIELDS = [
    "id",
    "name",
    "hashString",
    "status",
    "percentDone",
    "totalSize",
    "sizeWhenDone",
    "leftUntilDone",
    "downloadedEver",
    "uploadedEver",
    "uploadRatio",
    "rateDownload",
    "rateUpload",
    "eta",
    "peersConnected",
    "peersSendingToUs",
    "peersGettingFromUs",
    "error",
    "errorString",
    "isFinished",
    "isStalled",
    "downloadDir",
    "addedDate",
    "doneDate",
    "queuePosition",
    "labels",
    "bandwidthPriority",
]

#: Extra fields fetched when a caller asks for one torrent in detail.
DETAIL_FIELDS = SUMMARY_FIELDS + [
    "comment",
    "creator",
    "dateCreated",
    "activityDate",
    "startDate",
    "secondsDownloading",
    "secondsSeeding",
    "corruptEver",
    "haveValid",
    "haveUnchecked",
    "desiredAvailable",
    "recheckProgress",
    "metadataPercentComplete",
    "isPrivate",
    "magnetLink",
    "torrentFile",
    "pieceCount",
    "pieceSize",
    "file-count",
    "primary-mime-type",
    "downloadLimit",
    "downloadLimited",
    "uploadLimit",
    "uploadLimited",
    "honorsSessionLimits",
    "seedRatioLimit",
    "seedRatioMode",
    "seedIdleLimit",
    "seedIdleMode",
    "peer-limit",
    "group",
    "webseeds",
    "webseedsSendingToUs",
]


def to_iso(timestamp: Any) -> str | None:
    """Convert a Unix timestamp to an ISO-8601 UTC string."""
    if not isinstance(timestamp, (int, float)) or timestamp <= 0:
        return None
    return datetime.fromtimestamp(int(timestamp), tz=timezone.utc).isoformat()


def eta_seconds(value: Any) -> int | None:
    """Transmission uses -1 for 'unknown' and -2 for 'not applicable'."""
    return int(value) if isinstance(value, int) and value >= 0 else None


def percent(fraction: Any) -> float | None:
    """Convert Transmission's 0.0-1.0 fractions to a rounded percentage."""
    if not isinstance(fraction, (int, float)):
        return None
    return round(max(0.0, min(1.0, float(fraction))) * 100, 2)


def ratio(value: Any) -> float | None:
    """Upload ratio; -1 means 'not available' and -2 means 'infinite'."""
    if not isinstance(value, (int, float)) or value < 0:
        return None
    return round(float(value), 3)


def summarize_torrent(torrent: dict[str, Any]) -> dict[str, Any]:
    """Curated view of one ``torrent-get`` entry."""
    summary: dict[str, Any] = {
        "id": torrent.get("id"),
        "name": torrent.get("name"),
        "info_hash": torrent.get("hashString"),
        "status": STATUSES.get(torrent.get("status"), "unknown"),
        "progress_percent": percent(torrent.get("percentDone")),
        "total_bytes": torrent.get("sizeWhenDone") or torrent.get("totalSize"),
        "remaining_bytes": torrent.get("leftUntilDone"),
        "downloaded_bytes": torrent.get("downloadedEver"),
        "uploaded_bytes": torrent.get("uploadedEver"),
        "upload_ratio": ratio(torrent.get("uploadRatio")),
        "download_rate_bytes_per_sec": torrent.get("rateDownload"),
        "upload_rate_bytes_per_sec": torrent.get("rateUpload"),
        "eta_seconds": eta_seconds(torrent.get("eta")),
        "peers_connected": torrent.get("peersConnected"),
        "peers_sending_to_us": torrent.get("peersSendingToUs"),
        "peers_getting_from_us": torrent.get("peersGettingFromUs"),
        "download_dir": torrent.get("downloadDir"),
        "queue_position": torrent.get("queuePosition"),
        "priority": PRIORITIES.get(torrent.get("bandwidthPriority")),
        "labels": torrent.get("labels") or [],
        "is_finished": torrent.get("isFinished"),
        "is_stalled": torrent.get("isStalled"),
        "added_at": to_iso(torrent.get("addedDate")),
        "completed_at": to_iso(torrent.get("doneDate")),
    }
    if error := ERRORS.get(torrent.get("error")):
        summary["error"] = error
        summary["error_message"] = torrent.get("errorString")
    return summary


def detail_torrent(torrent: dict[str, Any]) -> dict[str, Any]:
    """Summary plus the fields that only matter when inspecting one torrent."""
    detail = summarize_torrent(torrent)
    detail.update(
        {
            "comment": torrent.get("comment") or None,
            "creator": torrent.get("creator") or None,
            "created_at": to_iso(torrent.get("dateCreated")),
            "started_at": to_iso(torrent.get("startDate")),
            "last_activity_at": to_iso(torrent.get("activityDate")),
            "seconds_downloading": torrent.get("secondsDownloading"),
            "seconds_seeding": torrent.get("secondsSeeding"),
            "corrupt_bytes": torrent.get("corruptEver"),
            "verified_bytes": torrent.get("haveValid"),
            "unverified_bytes": torrent.get("haveUnchecked"),
            "available_bytes": torrent.get("desiredAvailable"),
            "recheck_percent": percent(torrent.get("recheckProgress")),
            "metadata_percent": percent(torrent.get("metadataPercentComplete")),
            "is_private": torrent.get("isPrivate"),
            "magnet_link": torrent.get("magnetLink"),
            "torrent_file": torrent.get("torrentFile") or None,
            "file_count": torrent.get("file-count"),
            "piece_count": torrent.get("pieceCount"),
            "piece_bytes": torrent.get("pieceSize"),
            "primary_mime_type": torrent.get("primary-mime-type") or None,
            "download_limit_kbps": torrent.get("downloadLimit")
            if torrent.get("downloadLimited")
            else None,
            "upload_limit_kbps": torrent.get("uploadLimit")
            if torrent.get("uploadLimited")
            else None,
            "honors_session_limits": torrent.get("honorsSessionLimits"),
            "seed_ratio_limit": torrent.get("seedRatioLimit"),
            "seed_ratio_mode": RATIO_MODES.get(torrent.get("seedRatioMode")),
            "seed_idle_limit_minutes": torrent.get("seedIdleLimit"),
            "seed_idle_mode": IDLE_MODES.get(torrent.get("seedIdleMode")),
            "peer_limit": torrent.get("peer-limit"),
            "bandwidth_group": torrent.get("group") or None,
            "webseeds": torrent.get("webseeds") or [],
        }
    )
    return {key: value for key, value in detail.items() if value is not None or key == "error"}


def summarize_files(torrent: dict[str, Any]) -> list[dict[str, Any]]:
    """Merge ``files`` with ``fileStats`` into one list of file records."""
    files = torrent.get("files") or []
    stats = torrent.get("fileStats") or []
    merged = []
    for index, item in enumerate(files):
        stat = stats[index] if index < len(stats) else {}
        length = item.get("length") or 0
        completed = item.get("bytesCompleted") or 0
        merged.append(
            {
                "index": index,
                "name": item.get("name"),
                "total_bytes": length,
                "completed_bytes": completed,
                "progress_percent": round(completed / length * 100, 2) if length else None,
                "wanted": bool(stat.get("wanted", True)),
                "priority": PRIORITIES.get(stat.get("priority"), "normal"),
            }
        )
    return merged


def summarize_trackers(torrent: dict[str, Any]) -> list[dict[str, Any]]:
    """Curated view of ``trackerStats``."""
    return [
        {
            "id": tracker.get("id"),
            "host": tracker.get("host") or tracker.get("sitename"),
            "announce": tracker.get("announce"),
            "tier": tracker.get("tier"),
            "seeders": tracker.get("seederCount"),
            "leechers": tracker.get("leecherCount"),
            "downloads": tracker.get("downloadCount"),
            "last_announce_ok": tracker.get("lastAnnounceSucceeded"),
            "last_announce_result": tracker.get("lastAnnounceResult") or None,
            "last_announce_at": to_iso(tracker.get("lastAnnounceTime")),
            "next_announce_at": to_iso(tracker.get("nextAnnounceTime")),
        }
        for tracker in torrent.get("trackerStats") or []
    ]


def summarize_peers(torrent: dict[str, Any]) -> list[dict[str, Any]]:
    """Curated view of the ``peers`` array."""
    return [
        {
            "address": peer.get("address"),
            "client": peer.get("clientName"),
            "progress_percent": percent(peer.get("progress")),
            "download_rate_bytes_per_sec": peer.get("rateToClient"),
            "upload_rate_bytes_per_sec": peer.get("rateToPeer"),
            "encrypted": peer.get("isEncrypted"),
            "incoming": peer.get("isIncoming"),
            "flags": peer.get("flagStr"),
        }
        for peer in torrent.get("peers") or []
    ]


def summarize_session(session: dict[str, Any]) -> dict[str, Any]:
    """Curated view of ``session-get``."""
    return {
        "version": session.get("version"),
        "rpc_version": session.get("rpc-version"),
        "rpc_version_semver": session.get("rpc-version-semver"),
        "download_dir": session.get("download-dir"),
        "incomplete_dir": session.get("incomplete-dir")
        if session.get("incomplete-dir-enabled")
        else None,
        "peer_port": session.get("peer-port"),
        "port_forwarding_enabled": session.get("port-forwarding-enabled"),
        "encryption": session.get("encryption"),
        "speed_limit_down_kbps": session.get("speed-limit-down")
        if session.get("speed-limit-down-enabled")
        else None,
        "speed_limit_up_kbps": session.get("speed-limit-up")
        if session.get("speed-limit-up-enabled")
        else None,
        "alt_speed_enabled": session.get("alt-speed-enabled"),
        "alt_speed_down_kbps": session.get("alt-speed-down"),
        "alt_speed_up_kbps": session.get("alt-speed-up"),
        "alt_speed_scheduled": session.get("alt-speed-time-enabled"),
        "download_queue_size": session.get("download-queue-size")
        if session.get("download-queue-enabled")
        else None,
        "seed_queue_size": session.get("seed-queue-size")
        if session.get("seed-queue-enabled")
        else None,
        "seed_ratio_limit": session.get("seedRatioLimit")
        if session.get("seedRatioLimited")
        else None,
        "idle_seeding_limit_minutes": session.get("idle-seeding-limit")
        if session.get("idle-seeding-limit-enabled")
        else None,
        "start_added_torrents": session.get("start-added-torrents"),
        "peer_limit_global": session.get("peer-limit-global"),
        "peer_limit_per_torrent": session.get("peer-limit-per-torrent"),
        "dht_enabled": session.get("dht-enabled"),
        "pex_enabled": session.get("pex-enabled"),
        "lpd_enabled": session.get("lpd-enabled"),
        "utp_enabled": session.get("utp-enabled"),
        "blocklist_enabled": session.get("blocklist-enabled"),
        "blocklist_size": session.get("blocklist-size"),
        "config_dir": session.get("config-dir"),
    }


def summarize_stats(stats: dict[str, Any]) -> dict[str, Any]:
    """Curated view of ``session-stats``."""

    def block(raw: Any) -> dict[str, Any]:
        raw = raw or {}
        return {
            "downloaded_bytes": raw.get("downloadedBytes"),
            "uploaded_bytes": raw.get("uploadedBytes"),
            "files_added": raw.get("filesAdded"),
            "session_count": raw.get("sessionCount"),
            "seconds_active": raw.get("secondsActive"),
        }

    return {
        "torrent_count": stats.get("torrentCount"),
        "active_torrent_count": stats.get("activeTorrentCount"),
        "paused_torrent_count": stats.get("pausedTorrentCount"),
        "download_rate_bytes_per_sec": stats.get("downloadSpeed"),
        "upload_rate_bytes_per_sec": stats.get("uploadSpeed"),
        "current_session": block(stats.get("current-stats")),
        "cumulative": block(stats.get("cumulative-stats")),
    }
