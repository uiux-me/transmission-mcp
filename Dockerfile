# syntax=docker/dockerfile:1

FROM python:3.12-slim AS builder

ENV PIP_DISABLE_PIP_VERSION_CHECK=1 \
    PIP_NO_CACHE_DIR=1 \
    PATH="/opt/venv/bin:$PATH"

WORKDIR /build
RUN python -m venv /opt/venv
COPY pyproject.toml README.md ./
COPY src ./src
RUN pip install .

FROM python:3.12-slim

LABEL org.opencontainers.image.title="transmission-mcp" \
      org.opencontainers.image.description="FastMCP server for torrent search and the Transmission BitTorrent client" \
      org.opencontainers.image.source="https://transmissionbt.com/"

ENV PATH="/opt/venv/bin:$PATH" \
    PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    MCP_TRANSPORT=http \
    MCP_HOST=0.0.0.0 \
    MCP_PORT=8000 \
    MCP_PATH=/mcp \
    TRANSMISSION_HOST=localhost \
    TRANSMISSION_PORT=9091

COPY --from=builder /opt/venv /opt/venv

RUN useradd --create-home --uid 10001 --shell /usr/sbin/nologin mcp
USER mcp
WORKDIR /home/mcp

EXPOSE 8000

# Verifies both that the MCP port is accepting connections and that Transmission answers.
HEALTHCHECK --interval=30s --timeout=10s --start-period=10s --retries=3 \
    CMD ["python", "-m", "transmission_mcp.healthcheck"]

ENTRYPOINT ["transmission-mcp"]
