"""Desktop-only control-plane payloads for the MCP server."""

from __future__ import annotations

from typing import Any

from pydantic import BaseModel, ConfigDict


class McpStatusResponse(BaseModel):
    model_config = ConfigDict(extra="forbid")

    enabled: bool
    allowLocalImport: bool
    serving: bool
    url: str | None = None
    port: int | None = None
    reason: str | None = None
    tokenConfigured: bool = False


class McpTokenResponse(BaseModel):
    model_config = ConfigDict(extra="forbid")

    token: str
    createdAt: int


class McpClientConfigResponse(BaseModel):
    model_config = ConfigDict(extra="forbid")

    url: str
    token: str
    streamableHttpConfig: dict[str, Any]
    stdioConfig: dict[str, Any]
