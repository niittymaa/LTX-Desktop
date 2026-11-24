"""Asset management, media transforms, and multimodal preview MCP tools."""
# pyright: reportUnusedFunction=false

from __future__ import annotations

import asyncio
import base64
from typing import TYPE_CHECKING, Annotated, Literal

from mcp.server.mcpserver import MCPServer
from mcp.types import ImageContent, TextContent, ToolAnnotations
from pydantic import Field

from api_types import MediaKind
from mcp_server import domain
from mcp_server.schemas import McpAssetDetail, McpAssetPage, McpAssetSummary

if TYPE_CHECKING:
    from app_handler import AppHandler

_READ_ONLY = ToolAnnotations(
    read_only_hint=True,
    destructive_hint=False,
    idempotent_hint=True,
    open_world_hint=False,
)
_MUTATE = ToolAnnotations(
    read_only_hint=False,
    destructive_hint=False,
    idempotent_hint=False,
    open_world_hint=False,
)
_DELETE = ToolAnnotations(
    read_only_hint=False,
    destructive_hint=True,
    idempotent_hint=True,
    open_world_hint=False,
)


def register_asset_tools(mcp: MCPServer, handler: AppHandler) -> None:
    @mcp.tool(name="list_assets", title="List Library Assets", annotations=_READ_ONLY)
    async def list_assets(
        media_kind: MediaKind | None = None,
        sort: Literal["newest", "oldest"] = "newest",
        q: Annotated[str | None, Field(description="Optional substring filter on asset name.")] = None,
        cursor: str | None = None,
        limit: Annotated[int, Field(ge=1, le=200)] = 50,
    ) -> McpAssetPage:
        """Browse or search images, videos, and audio files in the LTX Desktop asset library."""
        try:
            return await asyncio.to_thread(
                domain.list_assets,
                handler,
                media_kind=media_kind,
                sort=sort,
                q=q,
                cursor=cursor,
                limit=limit,
            )
        except Exception as exc:
            raise domain.raise_tool_error(exc) from exc

    @mcp.tool(name="get_asset", title="Get Asset Metadata", annotations=_READ_ONLY)
    async def get_asset(
        asset_id: Annotated[str, Field(min_length=1)],
    ) -> McpAssetDetail:
        """Read full metadata and generating-job provenance for one asset ID."""
        try:
            return await asyncio.to_thread(domain.get_asset, handler, asset_id)
        except Exception as exc:
            raise domain.raise_tool_error(exc) from exc

    @mcp.tool(name="view_asset", title="View Asset Preview Image", annotations=_READ_ONLY)
    async def view_asset(
        asset_id: Annotated[str, Field(min_length=1)],
    ) -> list[TextContent | ImageContent]:
        """Return a visual JPEG preview (<=64 KB) for an image asset or video thumbnail."""
        try:
            detail, jpeg_bytes = await asyncio.to_thread(domain.view_asset_preview, handler, asset_id)
        except Exception as exc:
            raise domain.raise_tool_error(exc) from exc
        return [
            TextContent(type="text", text=detail.model_dump_json()),
            ImageContent(
                type="image",
                data=base64.b64encode(jpeg_bytes).decode("ascii"),
                mime_type="image/jpeg",
            ),
        ]

    @mcp.tool(name="import_asset", title="Import Local Media File", annotations=_MUTATE)
    async def import_asset(
        path: Annotated[str, Field(min_length=1, description="Absolute path to a local image, video, or audio file.")],
    ) -> McpAssetSummary:
        """Import a local image, video, or audio file into the LTX Desktop asset library (requires opt-in setting)."""
        try:
            return await asyncio.to_thread(domain.import_asset, handler, path)
        except Exception as exc:
            raise domain.raise_tool_error(exc) from exc

    @mcp.tool(name="extract_frame", title="Extract Video Frame", annotations=_MUTATE)
    async def extract_frame(
        at_seconds: Annotated[float | None, Field(ge=0, description="Timestamp in seconds (0 = first frame; omit for last frame).")] = None,
        generation_id: str | None = None,
        video_asset_id: str | None = None,
    ) -> McpAssetSummary:
        """Save a single frame from a video asset or succeeded generation as a new image asset."""
        try:
            return await asyncio.to_thread(
                domain.extract_frame,
                handler,
                at_seconds=at_seconds,
                generation_id=generation_id,
                video_asset_id=video_asset_id,
            )
        except Exception as exc:
            raise domain.raise_tool_error(exc) from exc

    @mcp.tool(name="trim_media", title="Trim Video or Audio Asset", annotations=_MUTATE)
    async def trim_media(
        asset_id: Annotated[str, Field(min_length=1)],
        start_sec: Annotated[float, Field(ge=0)],
        end_sec: Annotated[float, Field(gt=0)],
    ) -> McpAssetSummary:
        """Trim a video or audio asset to [start_sec, end_sec] and save the result as a new asset."""
        try:
            return await asyncio.to_thread(
                domain.trim_media,
                handler,
                asset_id=asset_id,
                start_sec=start_sec,
                end_sec=end_sec,
            )
        except Exception as exc:
            raise domain.raise_tool_error(exc) from exc

    @mcp.tool(name="extract_audio", title="Extract Audio Track", annotations=_MUTATE)
    async def extract_audio(
        asset_id: Annotated[str, Field(min_length=1, description="Source video asset ID.")],
    ) -> McpAssetSummary:
        """Extract the audio track from a video asset into a standalone audio asset."""
        try:
            return await asyncio.to_thread(domain.extract_audio, handler, asset_id=asset_id)
        except Exception as exc:
            raise domain.raise_tool_error(exc) from exc

    @mcp.tool(name="delete_asset", title="Delete Asset", annotations=_DELETE)
    async def delete_asset(
        asset_id: Annotated[str, Field(min_length=1)],
    ) -> dict[str, str]:
        """Permanently delete an asset that is not referenced by an active/queued generation."""
        try:
            return await asyncio.to_thread(domain.delete_asset, handler, asset_id=asset_id)
        except Exception as exc:
            raise domain.raise_tool_error(exc) from exc
