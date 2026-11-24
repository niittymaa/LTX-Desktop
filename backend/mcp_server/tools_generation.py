"""Video and image generation MCP tools."""
# pyright: reportUnusedFunction=false

from __future__ import annotations

import asyncio
from typing import TYPE_CHECKING, Annotated

from mcp.server.mcpserver import MCPServer
from mcp.types import ToolAnnotations
from pydantic import Field

from api_types import (
    ExtendMode,
    IC_LORA_SHEET_MAX_IMAGES,
    IMAGE_MAX_SIDE,
    IMAGE_MIN_SIDE,
    IcLoraControlType,
    IcLoraRecipeResolution,
    ImageProvider,
    LTXLocalA2VResolution,
    LTXQueuedAspectRatio,
    LTXVideoGenAspectRatio,
    LTXVideoGenFps,
    OfferingId,
    RETAKE_MAX_DURATION_SECONDS,
    RetakeMode,
    TargetResolution,
    VideoCameraMotion,
)
from handlers.assistant_params import IngredientsDuration, IngredientsResolution, LocalVideoDuration, LocalVideoResolution
from inference.types import EditContextMode
from mcp_server import domain
from mcp_server.schemas import McpGenerationResult

if TYPE_CHECKING:
    from app_handler import AppHandler

_LOCAL_GEN_ANNOTATIONS = ToolAnnotations(
    read_only_hint=False,
    destructive_hint=False,
    idempotent_hint=False,
    open_world_hint=False,
)
_IMAGE_GEN_ANNOTATIONS = ToolAnnotations(
    read_only_hint=False,
    destructive_hint=False,
    idempotent_hint=False,
    open_world_hint=True,
)


def register_generation_tools(mcp: MCPServer, handler: AppHandler) -> None:
    @mcp.tool(name="generate_video", title="Generate Video (Text-to-Video)", annotations=_LOCAL_GEN_ANNOTATIONS)
    async def generate_video(
        prompt: Annotated[str, Field(min_length=1, max_length=4000, description="Detailed visual and audio description of the video.")],
        model: OfferingId | None = None,
        resolution: LocalVideoResolution = "720p",
        duration: LocalVideoDuration | None = 5,
        fps: LTXVideoGenFps = 24,
        aspect_ratio: LTXVideoGenAspectRatio = "16:9",
        camera_motion: VideoCameraMotion = "none",
        negative_prompt: str = "",
        seed: int | None = None,
        auto_enhance: Annotated[bool | None, Field(description="Override auto prompt enhancement (None uses Desktop setting).")] = None,
        idempotency_key: Annotated[str | None, Field(description="Optional deduplication key so retries return the existing job instead of queueing twice.")] = None,
    ) -> McpGenerationResult:
        """Queue a local text-to-video generation and return immediately with its generation ID."""
        try:
            return await asyncio.to_thread(
                domain.generate_video,
                handler,
                prompt=prompt,
                model=model,
                resolution=resolution,
                duration=duration,
                fps=fps,
                aspect_ratio=aspect_ratio,
                camera_motion=camera_motion,
                negative_prompt=negative_prompt,
                seed=seed,
                auto_enhance=auto_enhance,
                idempotency_key=idempotency_key,
            )
        except Exception as exc:
            raise domain.raise_tool_error(exc) from exc

    @mcp.tool(name="image_to_video", title="Generate Video from Image", annotations=_LOCAL_GEN_ANNOTATIONS)
    async def image_to_video(
        start_frame_asset_id: Annotated[str, Field(min_length=1, description="Asset ID of the start image frame.")],
        prompt: Annotated[str, Field(max_length=4000, description="Motion, camera, and audio prompt for animating the image.")] = "",
        end_frame_asset_id: Annotated[str | None, Field(description="Optional end image frame asset ID for keyframe interpolation.")] = None,
        model: OfferingId | None = None,
        resolution: LocalVideoResolution = "720p",
        duration: LocalVideoDuration | None = 5,
        fps: LTXVideoGenFps = 24,
        aspect_ratio: LTXQueuedAspectRatio = "auto",
        camera_motion: VideoCameraMotion = "none",
        negative_prompt: str = "",
        seed: int | None = None,
        auto_enhance: bool | None = None,
        idempotency_key: str | None = None,
    ) -> McpGenerationResult:
        """Queue an image-to-video animation (or start+end frame interpolation) and return immediately."""
        try:
            return await asyncio.to_thread(
                domain.image_to_video,
                handler,
                start_frame_asset_id=start_frame_asset_id,
                prompt=prompt,
                end_frame_asset_id=end_frame_asset_id,
                model=model,
                resolution=resolution,
                duration=duration,
                fps=fps,
                aspect_ratio=aspect_ratio,
                camera_motion=camera_motion,
                negative_prompt=negative_prompt,
                seed=seed,
                auto_enhance=auto_enhance,
                idempotency_key=idempotency_key,
            )
        except Exception as exc:
            raise domain.raise_tool_error(exc) from exc

    @mcp.tool(name="audio_to_video", title="Generate Video from Audio", annotations=_LOCAL_GEN_ANNOTATIONS)
    async def audio_to_video(
        audio_asset_id: Annotated[str, Field(min_length=1, description="Asset ID of the driving audio track (duration >= 2s).")],
        prompt: Annotated[str, Field(max_length=4000)] = "",
        start_frame_asset_id: str | None = None,
        model: OfferingId | None = None,
        resolution: LTXLocalA2VResolution = "540p",
        aspect_ratio: LTXQueuedAspectRatio = "16:9",
        camera_motion: VideoCameraMotion = "none",
        negative_prompt: str = "",
        seed: int | None = None,
        auto_enhance: bool | None = None,
        idempotency_key: str | None = None,
    ) -> McpGenerationResult:
        """Queue an audio-synchronized video generation and return immediately."""
        try:
            return await asyncio.to_thread(
                domain.audio_to_video,
                handler,
                audio_asset_id=audio_asset_id,
                prompt=prompt,
                start_frame_asset_id=start_frame_asset_id,
                model=model,
                resolution=resolution,
                aspect_ratio=aspect_ratio,
                camera_motion=camera_motion,
                negative_prompt=negative_prompt,
                seed=seed,
                auto_enhance=auto_enhance,
                idempotency_key=idempotency_key,
            )
        except Exception as exc:
            raise domain.raise_tool_error(exc) from exc

    @mcp.tool(name="generate_image", title="Generate Image (Text-to-Image)", annotations=_IMAGE_GEN_ANNOTATIONS)
    async def generate_image(
        prompt: Annotated[str, Field(min_length=1, max_length=4000)],
        width: Annotated[int, Field(ge=IMAGE_MIN_SIDE, le=IMAGE_MAX_SIDE)] = 1024,
        height: Annotated[int, Field(ge=IMAGE_MIN_SIDE, le=IMAGE_MAX_SIDE)] = 1024,
        provider: ImageProvider | None = None,
        seed: int | None = None,
        auto_enhance: bool | None = None,
        idempotency_key: str | None = None,
    ) -> McpGenerationResult:
        """Queue a Z-Image Turbo (local) or fal API text-to-image generation and return immediately."""
        try:
            return await asyncio.to_thread(
                domain.generate_image,
                handler,
                prompt=prompt,
                width=width,
                height=height,
                provider=provider,
                seed=seed,
                auto_enhance=auto_enhance,
                idempotency_key=idempotency_key,
            )
        except Exception as exc:
            raise domain.raise_tool_error(exc) from exc

    @mcp.tool(name="run_lora_recipe", title="Run LoRA Style/Motion Recipe", annotations=_LOCAL_GEN_ANNOTATIONS)
    async def run_lora_recipe(
        recipe_id: Annotated[str, Field(min_length=1, description="Style/motion recipe ID or custom LoRA ID from list_recipes.")],
        prompt: Annotated[str, Field(min_length=1, max_length=4000)],
        start_frame_asset_id: str | None = None,
        end_frame_asset_id: str | None = None,
        model: OfferingId | None = None,
        resolution: LocalVideoResolution = "720p",
        duration: LocalVideoDuration | None = 5,
        fps: LTXVideoGenFps = 24,
        aspect_ratio: LTXQueuedAspectRatio = "16:9",
        camera_motion: VideoCameraMotion = "none",
        negative_prompt: str = "",
        scale: Annotated[float, Field(ge=0.0, le=4.0)] = 1.0,
        seed: int | None = None,
        auto_enhance: bool | None = None,
        idempotency_key: str | None = None,
    ) -> McpGenerationResult:
        """Queue a style, camera-motion, or custom LoRA video generation."""
        try:
            return await asyncio.to_thread(
                domain.run_lora_recipe,
                handler,
                recipe_id=recipe_id,
                prompt=prompt,
                start_frame_asset_id=start_frame_asset_id,
                end_frame_asset_id=end_frame_asset_id,
                model=model,
                resolution=resolution,
                duration=duration,
                fps=fps,
                aspect_ratio=aspect_ratio,
                camera_motion=camera_motion,
                negative_prompt=negative_prompt,
                scale=scale,
                seed=seed,
                auto_enhance=auto_enhance,
                idempotency_key=idempotency_key,
            )
        except Exception as exc:
            raise domain.raise_tool_error(exc) from exc

    @mcp.tool(name="run_video_recipe", title="Run IC-LoRA Video Recipe", annotations=_LOCAL_GEN_ANNOTATIONS)
    async def run_video_recipe(
        recipe_id: Annotated[str, Field(min_length=1, description="Video recipe ID from list_recipes(kind='video').")],
        generation_id: str | None = None,
        video_asset_id: str | None = None,
        reference_image_asset_id: str | None = None,
        prompt: str = "",
        template_values: dict[str, str] | None = None,
        negative_prompt: str = "",
        model: OfferingId | None = None,
        resolution: IcLoraRecipeResolution = "720p",
        fps: Annotated[float | None, Field(gt=0)] = None,
        control_type: IcLoraControlType | None = None,
        auto_enhance: bool | None = None,
        idempotency_key: str | None = None,
    ) -> McpGenerationResult:
        """Queue a video-to-video IC-LoRA effect (e.g. day-to-night, deblur, restore, colorization)."""
        try:
            return await asyncio.to_thread(
                domain.run_video_recipe,
                handler,
                recipe_id=recipe_id,
                generation_id=generation_id,
                video_asset_id=video_asset_id,
                reference_image_asset_id=reference_image_asset_id,
                prompt=prompt,
                template_values=template_values,
                negative_prompt=negative_prompt,
                model=model,
                resolution=resolution,
                fps=fps,
                control_type=control_type,
                auto_enhance=auto_enhance,
                idempotency_key=idempotency_key,
            )
        except Exception as exc:
            raise domain.raise_tool_error(exc) from exc

    @mcp.tool(name="run_ingredients", title="Run Ingredients Reference Video", annotations=_LOCAL_GEN_ANNOTATIONS)
    async def run_ingredients(
        image_asset_ids: Annotated[list[str], Field(min_length=1, max_length=IC_LORA_SHEET_MAX_IMAGES)],
        prompt: Annotated[str, Field(min_length=1, max_length=4000)],
        model: OfferingId | None = None,
        resolution: IngredientsResolution = "720p",
        duration: IngredientsDuration = 5,
        aspect_ratio: LTXVideoGenAspectRatio = "16:9",
        negative_prompt: str = "",
        auto_enhance: bool | None = None,
        idempotency_key: str | None = None,
    ) -> McpGenerationResult:
        """Queue an Ingredients video keeping characters, props, or places from 1-10 reference images consistent."""
        try:
            return await asyncio.to_thread(
                domain.run_ingredients,
                handler,
                image_asset_ids=image_asset_ids,
                prompt=prompt,
                model=model,
                resolution=resolution,
                duration=duration,
                aspect_ratio=aspect_ratio,
                negative_prompt=negative_prompt,
                auto_enhance=auto_enhance,
                idempotency_key=idempotency_key,
            )
        except Exception as exc:
            raise domain.raise_tool_error(exc) from exc

    @mcp.tool(name="extend_video", title="Extend Video", annotations=_LOCAL_GEN_ANNOTATIONS)
    async def extend_video(
        generation_id: str | None = None,
        video_asset_id: str | None = None,
        added_seconds: Annotated[float | None, Field(ge=2, le=20)] = None,
        total_seconds: Annotated[float | None, Field(gt=0)] = None,
        mode: ExtendMode = "end",
        context_mode: EditContextMode = "long",
        resolution: TargetResolution | None = None,
        prompt: str = "",
        negative_prompt: str = "",
        model: OfferingId | None = None,
        auto_enhance: bool | None = None,
        idempotency_key: str | None = None,
    ) -> McpGenerationResult:
        """Queue a video extension adding 2-20 seconds before ('start') or after ('end') a clip."""
        try:
            return await asyncio.to_thread(
                domain.extend_video,
                handler,
                generation_id=generation_id,
                video_asset_id=video_asset_id,
                added_seconds=added_seconds,
                total_seconds=total_seconds,
                mode=mode,
                context_mode=context_mode,
                resolution=resolution,
                prompt=prompt,
                negative_prompt=negative_prompt,
                model=model,
                auto_enhance=auto_enhance,
                idempotency_key=idempotency_key,
            )
        except Exception as exc:
            raise domain.raise_tool_error(exc) from exc

    @mcp.tool(name="retake_video", title="Retake Video Segment", annotations=_LOCAL_GEN_ANNOTATIONS)
    async def retake_video(
        start_time: Annotated[float, Field(ge=0)],
        duration: Annotated[float, Field(ge=2, le=RETAKE_MAX_DURATION_SECONDS)],
        generation_id: str | None = None,
        video_asset_id: str | None = None,
        mode: RetakeMode = "replace_audio_and_video",
        context_mode: EditContextMode = "long",
        prompt: str = "",
        negative_prompt: str = "",
        model: OfferingId | None = None,
        auto_enhance: bool | None = None,
        idempotency_key: str | None = None,
    ) -> McpGenerationResult:
        """Queue a local retake regenerating a selected time window of a clip."""
        try:
            return await asyncio.to_thread(
                domain.retake_video,
                handler,
                start_time=start_time,
                duration=duration,
                generation_id=generation_id,
                video_asset_id=video_asset_id,
                mode=mode,
                context_mode=context_mode,
                prompt=prompt,
                negative_prompt=negative_prompt,
                model=model,
                auto_enhance=auto_enhance,
                idempotency_key=idempotency_key,
            )
        except Exception as exc:
            raise domain.raise_tool_error(exc) from exc
