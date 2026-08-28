"""Torrent search across public indexers."""

from .common import (
    SearchResult,
    build_magnet,
    hash_from_magnet,
    human_size,
    normalize_info_hash,
)
from .registry import TorrentSearch, UnknownSourceError
from .sources import SOURCE_DESCRIPTIONS, SOURCES

__all__ = [
    "SOURCES",
    "SOURCE_DESCRIPTIONS",
    "SearchResult",
    "TorrentSearch",
    "UnknownSourceError",
    "build_magnet",
    "hash_from_magnet",
    "human_size",
    "normalize_info_hash",
]
