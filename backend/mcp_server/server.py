"""MCPServer factory wiring tools, resources, and workflow prompts to AppHandler."""

from __future__ import annotations

from typing import TYPE_CHECKING

from mcp.server.mcpserver import MCPServer

from mcp_server.prompts import register_prompts
from mcp_server.resources import register_resources
from mcp_server.tools_assets import register_asset_tools
from mcp_server.tools_discovery import register_discovery_tools
from mcp_server.tools_generation import register_generation_tools
from mcp_server.tools_queue import register_queue_tools

if TYPE_CHECKING:
    from app_handler import AppHandler

SERVER_NAME = "LTX Desktop"
SERVER_INSTRUCTIONS = (
    "Local AI video, image, and audio-synchronized generation server backed by LTX Desktop. "
    "Call `get_system_status` or `list_models` and `list_recipes` before queueing jobs to verify "
    "installed models, supported (resolution, fps, duration, aspect_ratio) combinations, and downloaded LoRAs. "
    "Generation tools enqueue jobs on the shared LTX Desktop queue and return immediately with a `generation_id`. "
    "Use `wait_for_generation` to stream live progress when a subsequent step depends on a clip's output, "
    "and `view_asset` to inspect images or extracted video frames."
)


def build_mcp_server(handler: AppHandler) -> MCPServer:
    """Construct a configured MCPServer bound to the shared AppHandler."""
    mcp = MCPServer(
        name=SERVER_NAME,
        title="LTX Desktop MCP",
        instructions=SERVER_INSTRUCTIONS,
        version="1.0.0",
    )
    register_discovery_tools(mcp, handler)
    register_asset_tools(mcp, handler)
    register_generation_tools(mcp, handler)
    register_queue_tools(mcp, handler)
    register_resources(mcp, handler)
    register_prompts(mcp)
    return mcp
