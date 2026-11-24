"""Loopback bind helpers and port resolution for the MCP server."""

from __future__ import annotations

import os
from typing import cast

from runtime_config.port_constant import MCP_PORT

ENV_MCP_PORT = "LTX_MCP_PORT"
ENV_MCP_URL = "LTX_MCP_URL"
ENV_MCP_TOKEN = "LTX_MCP_TOKEN"
MCP_BIND_HOST = "127.0.0.1"
MCP_PATH = "/mcp"


def mcp_port() -> int:
    """Configured listen port.

    ``LTX_MCP_PORT=0`` is a test hook so the kernel assigns an ephemeral port.
    Unset or invalid values use ``MCP_PORT`` (41956).
    """
    raw = os.environ.get(ENV_MCP_PORT, "").strip()
    if not raw:
        return MCP_PORT
    try:
        parsed = int(raw)
    except ValueError:
        return MCP_PORT
    if parsed < 0 or parsed > 65535:
        return MCP_PORT
    return parsed


def bound_server_port(server: object, fallback: int) -> int:
    """Port uvicorn actually bound, including kernel-assigned ``port=0``."""
    for asyncio_server in getattr(server, "servers", ()) or ():
        for sock in getattr(asyncio_server, "sockets", None) or ():
            try:
                host_port = cast(tuple[object, ...], sock.getsockname())
            except OSError:
                continue
            if len(host_port) < 2:
                continue
            port_raw = host_port[1]
            if isinstance(port_raw, int) and port_raw > 0:
                return port_raw
    return fallback


def mcp_endpoint_url(port: int) -> str:
    """Loopback Streamable HTTP URL for the MCP endpoint."""
    return f"http://{MCP_BIND_HOST}:{port}{MCP_PATH}"
