"""FastMCP server for torrent search and the Transmission BitTorrent client."""

from .server import build_server, run

__all__ = ["build_server", "run"]
__version__ = "0.1.0"
