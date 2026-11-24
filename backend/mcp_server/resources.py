"""MCP Resource and Resource Template registrations (ltx:// URIs)."""
# pyright: reportUnusedFunction=false

from __future__ import annotations

import asyncio
import json
from typing import TYPE_CHECKING

from mcp.server.mcpserver import MCPServer

from mcp_server import domain

if TYPE_CHECKING:
    from app_handler import AppHandler


def register_resources(mcp: MCPServer, handler: AppHandler) -> None:
    @mcp.resource(
        "ltx://status",
        name="System Status",
        description="Live GPU/MPS hardware, local generation policy, HF auth, and queue counts.",
        mime_type="application/json",
    )
    async def resource_status() -> str:
        try:
            status = await asyncio.to_thread(domain.get_system_status, handler)
            return status.model_dump_json(indent=2)
        except Exception as exc:
            raise domain.raise_resource_error(exc) from exc

    @mcp.resource(
        "ltx://models",
        name="Installed Models",
        description="Downloaded local video model capability matrices and image provider status.",
        mime_type="application/json",
    )
    async def resource_models() -> str:
        try:
            models = await asyncio.to_thread(domain.list_models, handler)
            return models.model_dump_json(indent=2)
        except Exception as exc:
            raise domain.raise_resource_error(exc) from exc

    @mcp.resource(
        "ltx://recipes",
        name="Recipes & LoRAs",
        description="Available LoRA, IC-LoRA, Ingredients, and custom LoRA catalog with download status.",
        mime_type="application/json",
    )
    async def resource_recipes() -> str:
        try:
            recipes = await asyncio.to_thread(domain.list_recipes, handler)
            return json.dumps([item.model_dump(mode="json") for item in recipes], indent=2)
        except Exception as exc:
            raise domain.raise_resource_error(exc) from exc

    @mcp.resource(
        "ltx://recipes/{recipe_id}/guidance",
        name="Recipe Prompting Guidance",
        description="Prompting rules, template fields, and recommended tool for a recipe.",
        mime_type="text/markdown",
    )
    async def resource_recipe_guidance(recipe_id: str) -> str:
        try:
            guidance = await asyncio.to_thread(domain.get_recipe_guidance, handler, recipe_id)
            lines = [
                f"# {guidance.recipe_id} ({guidance.kind})",
                "",
                f"**Recommended tool:** `{guidance.recommended_tool}`",
                "",
                f"**Next step:** {guidance.next_step}",
                "",
                guidance.guidance,
            ]
            return "\n".join(lines)
        except Exception as exc:
            raise domain.raise_resource_error(exc) from exc

    @mcp.resource(
        "ltx://queue",
        name="Generation Queue",
        description="Live queue snapshot of active, queued, done, and failed jobs.",
        mime_type="application/json",
    )
    async def resource_queue() -> str:
        try:
            snap = await asyncio.to_thread(domain.get_queue, handler)
            return snap.model_dump_json(indent=2)
        except Exception as exc:
            raise domain.raise_resource_error(exc) from exc

    @mcp.resource(
        "ltx://generations/{generation_id}",
        name="Generation Detail",
        description="Status, progress, error code, and output assets for a generation job.",
        mime_type="application/json",
    )
    async def resource_generation(generation_id: str) -> str:
        try:
            gen = await asyncio.to_thread(domain.get_generation, handler, generation_id)
            return gen.model_dump_json(indent=2)
        except Exception as exc:
            raise domain.raise_resource_error(exc) from exc

    @mcp.resource(
        "ltx://assets/{asset_id}",
        name="Asset Detail",
        description="Asset metadata and generating job provenance.",
        mime_type="application/json",
    )
    async def resource_asset(asset_id: str) -> str:
        try:
            asset = await asyncio.to_thread(domain.get_asset, handler, asset_id)
            return asset.model_dump_json(indent=2)
        except Exception as exc:
            raise domain.raise_resource_error(exc) from exc

    @mcp.resource(
        "ltx://assets/{asset_id}/preview",
        name="Asset JPEG Preview",
        description="Bounded JPEG thumbnail preview for an image or video asset.",
        mime_type="image/jpeg",
    )
    async def resource_asset_preview(asset_id: str) -> bytes:
        try:
            _, jpeg_bytes = await asyncio.to_thread(domain.view_asset_preview, handler, asset_id)
            return jpeg_bytes
        except Exception as exc:
            raise domain.raise_resource_error(exc) from exc
