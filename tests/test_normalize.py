"""Turning Transmission's structs into readable output."""

from __future__ import annotations

import pytest

from transmission_mcp.normalize import (
    detail_torrent,
    eta_seconds,
    percent,
    ratio,
    summarize_files,
    summarize_session,
    summarize_stats,
    summarize_torrent,
    to_iso,
)

TORRENT = {
    "id": 3,
    "name": "ubuntu-24.04.iso",
    "hashString": "4a3f5e08bcef825718eda30637230585e3330599",
    "status": 4,
    "percentDone": 0.7512,
    "sizeWhenDone": 6_000_000_000,
    "leftUntilDone": 1_500_000_000,
    "downloadedEver": 4_500_000_000,
    "uploadedEver": 900_000_000,
    "uploadRatio": 0.2,
    "rateDownload": 5_242_880,
    "rateUpload": 131_072,
    "eta": 300,
    "peersConnected": 22,
    "error": 0,
    "addedDate": 1700000000,
    "doneDate": 0,
    "labels": ["linux"],
    "bandwidthPriority": 1,
}


def test_summary_names_enums_and_converts_units():
    summary = summarize_torrent(TORRENT)
    assert summary["status"] == "downloading"
    assert summary["progress_percent"] == 75.12
    assert summary["priority"] == "high"
    assert summary["eta_seconds"] == 300
    assert summary["added_at"].startswith("2023-11-14T")
    assert summary["completed_at"] is None  # doneDate of 0 means "not done"
    assert "error" not in summary


def test_summary_surfaces_errors_when_present():
    summary = summarize_torrent({**TORRENT, "error": 3, "errorString": "No data found!"})
    assert summary["error"] == "local_error"
    assert summary["error_message"] == "No data found!"


def test_detail_drops_empty_fields_but_keeps_the_basics():
    detail = detail_torrent({**TORRENT, "comment": "", "isPrivate": False, "file-count": 1})
    assert "comment" not in detail  # empty string became None and was dropped
    assert detail["file_count"] == 1
    assert detail["name"] == "ubuntu-24.04.iso"


def test_detail_reports_speed_limits_only_when_enabled():
    limited = detail_torrent({**TORRENT, "downloadLimit": 500, "downloadLimited": True})
    unlimited = detail_torrent({**TORRENT, "downloadLimit": 500, "downloadLimited": False})
    assert limited["download_limit_kbps"] == 500
    assert "download_limit_kbps" not in unlimited


@pytest.mark.parametrize(
    "value,expected", [(-1, None), (-2, None), (0, 0), (600, 600), ("x", None)]
)
def test_eta_sentinels_become_none(value, expected):
    assert eta_seconds(value) == expected


@pytest.mark.parametrize(
    "value,expected", [(-1, None), (-2, None), (0.0, 0.0), (1.5, 1.5), (None, None)]
)
def test_ratio_sentinels_become_none(value, expected):
    assert ratio(value) == expected


@pytest.mark.parametrize(
    "value,expected", [(0.5, 50.0), (1.0, 100.0), (1.4, 100.0), (-0.1, 0.0), (None, None)]
)
def test_percent_clamps_to_the_valid_range(value, expected):
    assert percent(value) == expected


@pytest.mark.parametrize("value", [0, -5, None, "no"])
def test_to_iso_rejects_non_timestamps(value):
    assert to_iso(value) is None


def test_files_merge_with_their_stats():
    files = summarize_files(
        {
            "files": [
                {"name": "a.mkv", "length": 1000, "bytesCompleted": 500},
                {"name": "b.nfo", "length": 100, "bytesCompleted": 100},
            ],
            "fileStats": [
                {"wanted": True, "priority": 1},
                {"wanted": False, "priority": -1},
            ],
        }
    )
    assert files[0] == {
        "index": 0,
        "name": "a.mkv",
        "total_bytes": 1000,
        "completed_bytes": 500,
        "progress_percent": 50.0,
        "wanted": True,
        "priority": "high",
    }
    assert files[1]["wanted"] is False and files[1]["priority"] == "low"


def test_files_survive_a_missing_stats_array():
    files = summarize_files({"files": [{"name": "a.mkv", "length": 0, "bytesCompleted": 0}]})
    assert files[0]["wanted"] is True
    assert files[0]["progress_percent"] is None  # zero-length file, no division


def test_session_reports_limits_only_when_enabled():
    session = summarize_session(
        {
            "version": "4.0.6",
            "download-dir": "/downloads",
            "speed-limit-down": 1000,
            "speed-limit-down-enabled": False,
            "speed-limit-up": 200,
            "speed-limit-up-enabled": True,
            "incomplete-dir": "/incomplete",
            "incomplete-dir-enabled": False,
        }
    )
    assert session["speed_limit_down_kbps"] is None
    assert session["speed_limit_up_kbps"] == 200
    assert session["incomplete_dir"] is None
    assert session["version"] == "4.0.6"


def test_stats_split_current_from_cumulative():
    stats = summarize_stats(
        {
            "torrentCount": 5,
            "downloadSpeed": 1024,
            "current-stats": {"downloadedBytes": 10, "secondsActive": 60},
            "cumulative-stats": {"downloadedBytes": 999, "sessionCount": 12},
        }
    )
    assert stats["torrent_count"] == 5
    assert stats["current_session"]["downloaded_bytes"] == 10
    assert stats["cumulative"]["session_count"] == 12
