"""Discovery and prompt-enhancement MCP tools."""
# pyright: reportUnusedFunction=false

from __future__ import annotations

import asyncio
from typing import TYPE_CHECKING, Annotated, Literal

from mcp.server.mcpserver import MCPServer
from mcp.types import ToolAnnotations
from pydantic import Field

from api_types import LTXVideoGenAspectRatio, LTXVideoGenResolution
from mcp_server import domain
from mcp_server.schemas import (
    McpEnhancePromptResult,
    McpModelsResponse,
    McpRecipeGuidance,
    McpRecipeSummary,
    McpSystemStatus,
    RecipeKindFilter,
)

if TYPE_CHECKING:
    from app_handler import AppHandler

_READ_ONLY = ToolAnnotations(
    read_only_hint=True,
    destructive_hint=False,
    idempotent_hint=True,
    open_world_hint=False,
)
_ENHANCE_ANNOTATIONS = ToolAnnotations(
    read_only_hint=True,
    destructive_hint=False,
    idempotent_hint=False,
    open_world_hint=True,
)


def register_discovery_tools(mcp: MCPServer, handler: AppHandler) -> None:
    @mcp.tool(
        name="get_system_status",
        title="Get System Status",
        annotations=_READ_ONLY,
    )
    async def get_system_status() -> McpSystemStatus:
        """Read GPU/MPS/CUDA hardware, local viability, model readiness, HF auth, and queue counts."""
        try:
            return await asyncio.to_thread(domain.get_system_status, handler)
        except Exception as exc:
            raise domain.raise_tool_error(exc) from exc

    @mcp.tool(
        name="list_models",
        title="List Installed Models",
        annotations=_READ_ONLY,
    )
    async def list_models() -> McpModelsResponse:
        """List downloaded local video models with supported resolution/FPS/duration/aspect matrices and image generation provider status."""
        try:
            return await asyncio.to_thread(domain.list_models, handler)
        except Exception as exc:
            raise domain.raise_tool_error(exc) from exc

    @mcp.tool(
        name="list_recipes",
        title="List Recipes and LoRAs",
        annotations=_READ_ONLY,
    )
    async def list_recipes(
        kind: Annotated[
            RecipeKindFilter | None,
            Field(description="Optional filter: 'lora' (style/motion), 'video' (IC-LoRA video-to-video), 'sheet' (Ingredients), or 'custom'."),
        ] = None,
    ) -> list[McpRecipeSummary]:
        """List available LoRA style/motion recipes, IC-LoRA video recipes, Ingredients, and custom LoRAs with download status."""
        try:
            return await asyncio.to_thread(domain.list_recipes, handler, kind=kind)
        except Exception as exc:
            raise domain.raise_tool_error(exc) from exc

    @mcp.tool(
        name="get_recipe_guidance",
        title="Get Recipe Guidance",
        annotations=_READ_ONLY,
    )
    async def get_recipe_guidance(
        recipe_id: Annotated[str, Field(min_length=1, description="Recipe ID or custom LoRA ID from list_recipes.")],
    ) -> McpRecipeGuidance:
        """Read prompting instructions, template placeholders, and which generation tool to use for a recipe."""
        try:
            return await asyncio.to_thread(domain.get_recipe_guidance, handler, recipe_id)
        except Exception as exc:
            raise domain.raise_tool_error(exc) from exc

    @mcp.tool(
        name="enhance_prompt",
        title="Enhance Prompt",
        annotations=_ENHANCE_ANNOTATIONS,
    )
    async def enhance_prompt(
        prompt: Annotated[str, Field(description="Draft prompt text (can be empty if conditioned on an image or video asset).")],
        media_type: Annotated[Literal["video", "image"], Field(description="Target media type: 'video' or 'image'.")] = "video",
        provider: Annotated[Literal["local", "api"] | None, Field(description="Optional enhancer backend: 'local' (Gemma 4) or 'api' (Gemini).")] = None,
        negative_prompt: str | None = None,
        lora_catalog_ids: list[str] | None = None,
        custom_lora_ids: list[str] | None = None,
        ic_lora_id: str | None = None,
        image_asset_id: str | None = None,
        last_image_asset_id: str | None = None,
        image_asset_ids: list[str] | None = None,
        video_asset_id: str | None = None,
        duration: Annotated[int | None, Field(gt=0)] = None,
        fps: Annotated[int | None, Field(gt=0)] = None,
        aspect_ratio: LTXVideoGenAspectRatio | None = None,
        resolution: LTXVideoGenResolution | None = None,
    ) -> McpEnhancePromptResult:
        """Expand and refine a video or image prompt using LTX Desktop's catalog-aware enhancer."""
        try:
            return await asyncio.to_thread(
                domain.enhance_prompt,
                handler,
                prompt=prompt,
                media_type=media_type,
                provider=provider,
                negative_prompt=negative_prompt,
                lora_catalog_ids=lora_catalog_ids,
                custom_lora_ids=custom_lora_ids,
                ic_lora_id=ic_lora_id,
                image_asset_id=image_asset_id,
                last_image_asset_id=last_image_asset_id,
                image_asset_ids=image_asset_ids,
                video_asset_id=video_asset_id,
                duration=duration,
                fps=fps,
                aspect_ratio=aspect_ratio,
                resolution=resolution,
            )
        except Exception as exc:
            raise domain.raise_tool_error(exc) from exc
