"""Environment parsing."""

from __future__ import annotations

import os

import pytest

from transmission_mcp.config import load_settings


def test_defaults_point_at_localhost(monkeypatch):
    for key in list(os.environ):
        if key.startswith(("TRANSMISSION_", "SEARCH_", "MCP_")):
            monkeypatch.delenv(key, raising=False)

    settings = load_settings()
    assert settings.rpc_url == "http://localhost:9091/transmission/rpc"
    assert settings.username is None
    assert settings.transport == "http"
    assert settings.search_enabled is True


def test_rpc_path_is_appended_only_when_missing(monkeypatch):
    monkeypatch.setenv("TRANSMISSION_URL", "http://box:9091")
    assert load_settings().rpc_url == "http://box:9091/transmission/rpc"

    monkeypatch.setenv("TRANSMISSION_URL", "http://box:9091/transmission/rpc")
    assert load_settings().rpc_url == "http://box:9091/transmission/rpc"


def test_reverse_proxy_subpath_is_preserved(monkeypatch):
    monkeypatch.setenv("TRANSMISSION_URL", "https://home.example/tm")
    assert load_settings().rpc_url == "https://home.example/tm/transmission/rpc"


def test_scheme_is_assumed_when_absent(monkeypatch):
    monkeypatch.setenv("TRANSMISSION_URL", "192.168.1.10:9091")
    assert load_settings().rpc_url == "http://192.168.1.10:9091/transmission/rpc"


def test_credentials_embedded_in_the_url_are_used(monkeypatch):
    monkeypatch.delenv("TRANSMISSION_USERNAME", raising=False)
    monkeypatch.delenv("TRANSMISSION_PASSWORD", raising=False)
    monkeypatch.setenv("TRANSMISSION_URL", "http://joe:hunter2@box:9091")

    settings = load_settings()
    assert (settings.username, settings.password) == ("joe", "hunter2")
    assert "joe" not in settings.rpc_url  # credentials stay out of the URL we post to


def test_explicit_credentials_win_over_embedded_ones(monkeypatch):
    monkeypatch.setenv("TRANSMISSION_URL", "http://joe:hunter2@box:9091")
    monkeypatch.setenv("TRANSMISSION_USERNAME", "admin")
    monkeypatch.setenv("TRANSMISSION_PASSWORD", "s3cret")

    settings = load_settings()
    assert (settings.username, settings.password) == ("admin", "s3cret")


def test_host_and_port_are_used_without_a_url(monkeypatch):
    monkeypatch.delenv("TRANSMISSION_URL", raising=False)
    monkeypatch.setenv("TRANSMISSION_HOST", "nas.local")
    monkeypatch.setenv("TRANSMISSION_PORT", "9092")
    assert load_settings().rpc_url == "http://nas.local:9092/transmission/rpc"


def test_search_lists_are_split_on_commas(monkeypatch):
    monkeypatch.setenv("SEARCH_SOURCES", "nyaa, yts ,eztv")
    monkeypatch.setenv("SEARCH_EXCLUDE_SOURCES", "eztv")

    settings = load_settings()
    assert settings.search_sources == ["nyaa", "yts", "eztv"]
    assert settings.search_exclude == ["eztv"]


@pytest.mark.parametrize("value", ["maybe", "2", ""])
def test_bad_booleans_are_rejected(monkeypatch, value):
    monkeypatch.setenv("TRANSMISSION_READ_ONLY", value or " x ")
    with pytest.raises(ValueError, match="TRANSMISSION_READ_ONLY"):
        load_settings()


def test_bad_transport_is_rejected(monkeypatch):
    monkeypatch.setenv("MCP_TRANSPORT", "carrier-pigeon")
    with pytest.raises(ValueError, match="MCP_TRANSPORT"):
        load_settings()


def test_base_url_strips_the_rpc_path(monkeypatch):
    monkeypatch.setenv("TRANSMISSION_URL", "http://box:9091")
    assert load_settings().base_url == "http://box:9091"
