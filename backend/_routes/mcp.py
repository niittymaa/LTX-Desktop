"""Desktop-only MCP server control-plane endpoints.

Mounted on the Desktop backend (:41954) only; never mounted on the Remote LAN app.
"""

from __future__ import annotations

from fastapi import APIRouter, Request

from _routes._errors import HTTPError
from mcp_server.controller import McpServerController, mcp_controller_from_app
from mcp_server.status import McpClientConfigResponse, McpStatusResponse, McpTokenResponse

router = APIRouter(prefix="/api", tags=["mcp"])


def _require_controller(request: Request) -> McpServerController:
    controller = mcp_controller_from_app(request.app)
    if controller is None:
        raise HTTPError(503, "MCP controller is not initialized")
    return controller


@router.get("/mcp/status", response_model=McpStatusResponse)
def route_mcp_status(request: Request) -> McpStatusResponse:
    controller = mcp_controller_from_app(request.app)
    if controller is None:
        return McpStatusResponse(
            enabled=False,
            allowLocalImport=False,
            serving=False,
            reason="MCP controller is not initialized",
            tokenConfigured=False,
        )
    return controller.status()


@router.get("/mcp/client-config", response_model=McpClientConfigResponse)
def route_mcp_client_config(request: Request) -> McpClientConfigResponse:
    return _require_controller(request).client_config()


@router.post("/mcp/token/rotate", response_model=McpTokenResponse)
def route_mcp_rotate_token(request: Request) -> McpTokenResponse:
    return _require_controller(request).rotate_token()
