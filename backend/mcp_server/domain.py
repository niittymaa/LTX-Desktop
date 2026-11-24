"""Transport-agnostic MCP domain adapter over LTX Desktop's AppHandler."""

from __future__ import annotations

import asyncio
import logging
import re
import time
from collections.abc import Awaitable, Callable
from datetime import datetime
from io import BytesIO
from pathlib import Path
from typing import TYPE_CHECKING, Literal
from uuid import NAMESPACE_URL, uuid5

from mcp.server.mcpserver.exceptions import ResourceError, ToolError
from PIL import Image
from pydantic import ValidationError

from _routes._errors import HTTPError
from api_types import (
    Asset,
    AssetCore,
    AssetListQuery,
    AudioAssetMetadata,
    AudioToVideoInputs,
    CreateAudioToVideoParams,
    CreateAudioToVideoRequest,
    CreateCustomLoraRequest,
    CreateExtendRequest,
    CreateIcLoraRecipeRequest,
    CreateIcLoraSheetRequest,
    CreateImageToVideoRequest,
    CreateLoraRecipeRequest,
    CreateRetakeRequest,
    CreateTextToImageRequest,
    CreateTextToVideoRequest,
    CustomLoraParams,
    DownloadedLocalVideoGenerationModelSpecItem,
    DownloadSpec,
    EnhancePromptRequest,
    ExtendInputs,
    ExtendMode,
    ExtendParams,
    Generation,
    GenerationStatus,
    IcLoraCatalogItem,
    IcLoraControlType,
    IcLoraRecipeInputs,
    IcLoraRecipeResolution,
    IcLoraSheetInputs,
    IcLoraSheetParams,
    IcLoraVideoRecipeParams,
    ImageProvider,
    ImageToVideoInputs,
    ImageToVideoParams,
    IngestAssetRequest,
    InputAssetRef,
    LoraRecipeInputs,
    LoraRecipeParams,
    LTXLocalA2VResolution,
    LTXQueuedAspectRatio,
    LTXVideoGenAspectRatio,
    LTXVideoGenFps,
    LTXVideoGenResolution,
    MediaKind,
    OfferingId,
    PromptProvenance,
    QueueEntry,
    QueueSnapshot,
    RetakeInputs,
    RetakeMode,
    RetakeRequestParams,
    TargetResolution,
    TextToImageParams,
    TextToVideoParams,
    TrimMediaRequest,
    VideoCameraMotion,
    VideoMeta,
)
from handlers.assistant_params import IngredientsDuration, IngredientsResolution, LocalVideoDuration, LocalVideoResolution
from inference.types import EditContextMode
from mcp_server.schemas import (
    McpAssetDetail,
    McpAssetPage,
    McpAssetSource,
    McpAssetSummary,
    McpEnhancePromptResult,
    McpGenerationJobSource,
    McpGenerationResult,
    McpGpuSummary,
    McpModelsResponse,
    McpQueueSummary,
    McpRecipeGuidance,
    McpRecipeSummary,
    McpSystemStatus,
    McpToolError,
    McpVideoModelSpec,
    RecipeKindFilter,
)
from runtime_config.custom_loras import custom_lora_prompt_guidance, read_custom_lora
from services.assistant_media import read_preview
from services.features.ic_lora_recipes import get_ic_lora_recipe, queued_ic_lora_recipes
from services.features.lora_recipes import get_lora_recipe, queued_lora_recipes
from services.prompt_enhancement.resolve_enhance_assets import resolve_enhance_media_assets
from services.prompt_enhancement.system_prompt import (
    enforce_trigger_placement,
    fill_prompt_template,
    render_catalog_item_block,
)

if TYPE_CHECKING:
    from app_handler import AppHandler

logger = logging.getLogger(__name__)

_SOURCE_PARAMS = (
    "prompt",
    "negativePrompt",
    "model",
    "resolution",
    "duration",
    "fps",
    "aspectRatio",
    "cameraMotion",
)
_SOURCE_INPUTS = ("video", "startFrame", "audio")
_INGREDIENTS_RECIPE_ID = "ingredients"
_INGREDIENTS_SCALE = 1.35
_MAX_PREVIEW_BYTES = 64 * 1024


class McpDomainError(Exception):
    """Expected domain/validation error surfaced cleanly to MCP clients."""

    def __init__(self, code: str, message: str, *, hint: str | None = None) -> None:
        super().__init__(message)
        self.code = code
        self.message = message
        self.hint = hint

    def to_payload(self) -> McpToolError:
        return McpToolError(code=self.code, message=self.message, hint=self.hint)

    def to_json(self) -> str:
        return self.to_payload().model_dump_json()


def raise_tool_error(exc: Exception) -> ToolError:
    """Convert any domain/HTTP/validation exception into a safe MCP ToolError."""
    if isinstance(exc, ToolError):
        return exc
    if isinstance(exc, McpDomainError):
        return ToolError(exc.to_json())
    if isinstance(exc, HTTPError):
        code = exc.code or f"HTTP_{exc.status_code}"
        if exc.status_code >= 500:
            logger.error("MCP backend HTTPError %s (%s): %s", exc.status_code, code, exc.detail)
            return ToolError(
                McpToolError(
                    code=code,
                    message="LTX Desktop backend could not complete this operation.",
                    hint="Check LTX Desktop logs or retry if the store/backend is busy.",
                ).model_dump_json()
            )
        return ToolError(
            McpToolError(
                code=code,
                message=exc.detail,
            ).model_dump_json()
        )
    if isinstance(exc, ValidationError):
        first = exc.errors()[0] if exc.errors() else {"msg": str(exc)}
        msg = str(first.get("msg", str(exc))).removeprefix("Value error, ")
        return ToolError(
            McpToolError(
                code="INVALID_ARGUMENTS",
                message=msg,
            ).model_dump_json()
        )
    logger.error("Unexpected MCP tool error (%s): %s", type(exc).__name__, exc)
    return ToolError(
        McpToolError(
            code="INTERNAL_ERROR",
            message="An internal error occurred while executing the tool.",
        ).model_dump_json()
    )


def raise_resource_error(exc: Exception) -> ResourceError:
    """Convert any domain/HTTP exception into a safe MCP ResourceError."""
    if isinstance(exc, ResourceError):
        return exc
    if isinstance(exc, McpDomainError):
        return ResourceError(exc.to_json())
    if isinstance(exc, HTTPError):
        code = exc.code or f"HTTP_{exc.status_code}"
        msg = exc.detail if exc.status_code < 500 else "Resource temporarily unavailable."
        return ResourceError(McpToolError(code=code, message=msg).model_dump_json())
    logger.error("Unexpected MCP resource error (%s): %s", type(exc).__name__, exc)
    return ResourceError(
        McpToolError(code="INTERNAL_ERROR", message="Failed to read resource.").model_dump_json()
    )


def _local_time(epoch_ms: int) -> str:
    return datetime.fromtimestamp(epoch_ms / 1000).astimezone().strftime("%Y-%m-%d %H:%M")


def _asset_summary(asset: AssetCore, *, in_use: bool = False) -> McpAssetSummary:
    return McpAssetSummary(
        id=asset.id,
        media_kind=asset.media_kind,
        origin=asset.origin,
        mime_type=asset.mime_type,
        name=asset.name,
        metadata=asset.metadata,
        created=_local_time(asset.created_at),
        in_use=in_use,
    )


def _to_asset_core(asset: Asset) -> AssetCore:
    return AssetCore.model_validate(asset.model_dump())


def resolve_generation_id(idempotency_key: str | None) -> str | None:
    if idempotency_key is None:
        return None
    cleaned = idempotency_key.strip()
    if not cleaned:
        return None
    return str(uuid5(NAMESPACE_URL, f"ltx-desktop-mcp:{cleaned}"))


def _job_source(generation: Generation, handler: AppHandler) -> McpGenerationJobSource | Literal["unavailable"] | None:
    inputs = generation.spec.get("inputs")
    if not isinstance(inputs, dict):
        return None
    ref = next((inputs[key] for key in _SOURCE_INPUTS if isinstance(inputs.get(key), dict)), None)
    asset_id = ref.get("assetId") if isinstance(ref, dict) else None
    if not isinstance(asset_id, str):
        return None
    try:
        asset = handler.assets.get_asset(asset_id)
    except HTTPError as exc:
        if exc.status_code != 404:
            raise
        return "unavailable"
    return McpGenerationJobSource(id=asset.id, name=asset.name)


def _queue_entry_for(snapshot: QueueSnapshot, generation_id: str) -> QueueEntry | None:
    for group in (snapshot.active, snapshot.queued, snapshot.done, snapshot.failed):
        for entry in group:
            if entry.generation.id == generation_id:
                return entry
    return None


def to_mcp_generation(
    generation: Generation,
    handler: AppHandler,
    *,
    snapshot: QueueSnapshot | None = None,
    note: str | None = None,
) -> McpGenerationResult:
    outputs = [_asset_summary(_to_asset_core(output)) for output in generation.outputs]
    meta = next(
        (output.metadata.metadata for output in outputs if not isinstance(output.metadata, AudioAssetMetadata)),
        None,
    )
    entry = _queue_entry_for(snapshot, generation.id) if snapshot is not None else None
    progress = entry.progress if entry is not None else None
    started, finished = generation.started_at, generation.finished_at
    return McpGenerationResult(
        id=generation.id,
        feature=generation.feature,
        status=generation.status,
        error_code=generation.error_code,
        attempt_count=generation.attempt_count,
        phase=progress.phase if progress is not None else None,
        progress=progress.progress if progress is not None else None,
        current_step=progress.currentStep if progress is not None else None,
        total_steps=progress.totalSteps if progress is not None else None,
        took_seconds=round((finished - started) / 1000, 1) if started is not None and finished is not None else None,
        finished=_local_time(finished) if finished is not None else None,
        source=_job_source(generation, handler),
        clip_seconds=round(meta.durationMs / 1000, 2) if isinstance(meta, VideoMeta) else None,
        size=f"{meta.width}x{meta.height}" if meta is not None else None,
        outputs=outputs,
        note=note,
    )


def get_system_status(handler: AppHandler) -> McpSystemStatus:
    health = handler.health.get_health()
    gpu = handler.health.get_gpu_info()
    policy = handler.runtime_policy.get_runtime_policy()
    hf_status = handler.hf_auth.get_auth_status()
    queue = handler.queued_generations.get_queue_snapshot()
    settings = handler.settings.get_settings_snapshot()
    downloaded_count = sum(1 for item in health.models_status if item.downloaded)
    return McpSystemStatus(
        local_viable=policy.local_viable,
        force_api_generations=policy.force_api_generations,
        local_generations_mode=handler.config.local_generations_mode,
        models_loaded=health.models_loaded,
        active_model=health.active_model,
        models_downloaded_count=downloaded_count,
        hf_authenticated=hf_status.status == "authenticated",
        local_import_allowed=settings.mcp_allow_local_import,
        gpu=McpGpuSummary(
            cuda_available=gpu.cuda_available,
            mps_available=gpu.mps_available,
            gpu_available=gpu.gpu_available,
            gpu_name=gpu.gpu_name,
            vram_gb=gpu.vram_gb,
            system=gpu.system,
            total_ram_bytes=gpu.total_ram_bytes,
        ),
        queue=McpQueueSummary(
            active_count=len(queue.active),
            queued_count=len(queue.queued),
            done_count=len(queue.done),
            failed_count=len(queue.failed),
            active_generation_ids=[entry.generation.id for entry in queue.active],
        ),
    )


def list_models(handler: AppHandler) -> McpModelsResponse:
    specs = handler.video_generation.get_model_specs()
    policy = handler.runtime_policy.get_runtime_policy()
    image_status = handler.image_generation.get_status()
    return McpModelsResponse(
        local_viable=policy.local_viable,
        default_video_model=specs.active_offering,
        video_models=[
            McpVideoModelSpec(
                model=item.model,
                pipeline=item.pipeline,
                display_name=item.spec.display_name,
                default=item.model == specs.active_offering,
                supported_resolutions_durations=item.spec.supported_resolutions_durations,
                a2v_supported_resolutions_durations=item.spec.a2v_supported_resolutions_durations,
                capabilities=item.spec.capabilities,
            )
            for item in specs.downloaded_local_models
        ],
        image_generation=image_status,
    )


def list_recipes(
    handler: AppHandler,
    *,
    kind: RecipeKindFilter | None = None,
) -> list[McpRecipeSummary]:
    items: list[McpRecipeSummary] = []

    if kind in (None, "lora"):
        lora_catalog = {item.lora.id: item for item in handler.catalog.list_loras().loras}
        for recipe in queued_lora_recipes():
            item = lora_catalog.get(recipe.catalog_id)
            if item is None:
                continue
            downloaded = item.lora.download.default_variant().id in item.downloaded_variant_ids
            items.append(
                McpRecipeSummary(
                    recipe_id=recipe.recipe_id,
                    kind="lora",
                    mode=recipe.mode,
                    name=item.lora.name,
                    description=item.lora.description,
                    downloaded=downloaded,
                    requires_hf_login=item.lora.requires_hf_login,
                    requires_start_frame=recipe.mode == "i2v",
                    requires_end_frame=recipe.requires_end_frame,
                    requires_source_video=False,
                    prompt_required=True,
                )
            )

    if kind in (None, "video", "sheet"):
        ic_catalog = {item.ic_lora.id: item for item in handler.catalog.list_ic_loras().ic_loras}
        for ic_recipe in queued_ic_lora_recipes():
            if ic_recipe.kind == "track":
                continue
            item = ic_catalog.get(ic_recipe.catalog_id)
            if item is None:
                continue
            if kind is not None and ic_recipe.kind != kind:
                continue
            if ic_recipe.kind == "sheet":
                items.append(
                    McpRecipeSummary(
                        recipe_id=ic_recipe.recipe_id,
                        kind="sheet",
                        mode="sheet",
                        name=item.ic_lora.name,
                        description=item.ic_lora.description,
                        downloaded=item.downloaded,
                        requires_hf_login=item.ic_lora.requires_hf_login,
                        min_images=1,
                        max_images=10,
                        prompt_required=True,
                    )
                )
            else:
                ref_mode: Literal["none", "optional", "required"] = (
                    "required"
                    if item.ic_lora.reference_image_required
                    else "optional"
                    if item.ic_lora.allows_reference_image
                    else "none"
                )
                items.append(
                    McpRecipeSummary(
                        recipe_id=ic_recipe.recipe_id,
                        kind="video",
                        mode="video",
                        name=item.ic_lora.name,
                        description=item.ic_lora.description,
                        downloaded=item.downloaded,
                        requires_hf_login=item.ic_lora.requires_hf_login,
                        requires_source_video=True,
                        reference_image=ref_mode,
                        prompt_required=not item.ic_lora.allows_empty_prompt,
                        has_prompt_template=item.ic_lora.prompt_template is not None,
                    )
                )

    if kind in (None, "custom"):
        for custom in handler.custom_loras.list_loras().loras:
            items.append(
                McpRecipeSummary(
                    recipe_id=custom.id,
                    kind="custom",
                    mode="t2v",
                    name=custom.name,
                    description=custom.description or "User-imported custom LoRA",
                    downloaded=custom.downloaded,
                    requires_hf_login=False,
                    prompt_required=True,
                )
            )

    return items


def get_recipe_guidance(handler: AppHandler, recipe_id: str) -> McpRecipeGuidance:
    if (ic_recipe := get_ic_lora_recipe(recipe_id)) is not None and ic_recipe.kind != "track":
        ic_catalog = {item.ic_lora.id: item.ic_lora for item in handler.catalog.list_ic_loras().ic_loras}
        ic_lora = ic_catalog.get(ic_recipe.catalog_id)
        if ic_lora is None:
            raise McpDomainError(
                "RECIPE_NOT_FOUND",
                f"IC-LoRA recipe '{recipe_id}' was not found in catalog.",
                hint="Call list_recipes to inspect valid recipe_id values.",
            )
        if ic_recipe.kind == "sheet":
            return McpRecipeGuidance(
                recipe_id=recipe_id,
                kind="sheet",
                recommended_tool="run_ingredients",
                next_step="Queue with run_ingredients, passing 1-10 image_asset_ids and a prompt describing how they interact.",
                guidance=render_catalog_item_block(ic_lora),
            )
        template = ic_lora.prompt_template
        if template is None:
            return McpRecipeGuidance(
                recipe_id=recipe_id,
                kind="video",
                recommended_tool="run_video_recipe",
                next_step=f"Queue with run_video_recipe(recipe_id='{recipe_id}') on a source video.",
                guidance=render_catalog_item_block(ic_lora),
            )
        fields = [
            f"{name}: one of {placeholder.choices}"
            if placeholder.choices is not None
            else f"{name}: short phrase from the user request"
            for name, placeholder in template.placeholders.items()
        ]
        return McpRecipeGuidance(
            recipe_id=recipe_id,
            kind="video",
            recommended_tool="run_video_recipe",
            next_step=(
                f"Queue with run_video_recipe(recipe_id='{recipe_id}') passing template_values ({'; '.join(fields)}). "
                "Leave prompt empty so the app fills the template."
            ),
            guidance=f"{render_catalog_item_block(ic_lora)}\nTemplate: {template.template}",
            template=template.template,
            template_placeholders=list(template.placeholders.keys()),
        )

    lora_recipe = get_lora_recipe(recipe_id)
    if lora_recipe is not None:
        lora_catalog = {item.lora.id: item.lora for item in handler.catalog.list_loras().loras}
        lora = lora_catalog.get(lora_recipe.catalog_id)
        if lora is not None:
            return McpRecipeGuidance(
                recipe_id=recipe_id,
                kind="lora",
                recommended_tool="run_lora_recipe",
                next_step=(
                    f"Queue with run_lora_recipe(recipe_id='{recipe_id}'). "
                    "Describe the scene only — the app inserts the trigger/style scaffold automatically."
                ),
                guidance=render_catalog_item_block(lora, include_trigger=False),
            )

    custom_record = read_custom_lora(handler.models.models_dir, recipe_id)
    if custom_record is not None:
        guidance_info = custom_lora_prompt_guidance(custom_record)
        guidance_lines = [f"Custom LoRA: {custom_record.name}"]
        if guidance_info.trigger:
            guidance_lines.append(f"Trigger word: {guidance_info.trigger}")
        for section in guidance_info.instructions:
            body_text = "\n".join(section.body) if isinstance(section.body, list) else section.body
            guidance_lines.append(f"{section.title}: {body_text}")
        if guidance_info.enhancement_examples:
            guidance_lines.append(
                "Examples:\n" + "\n".join(f"- {ex}" for ex in guidance_info.enhancement_examples)
            )
        return McpRecipeGuidance(
            recipe_id=recipe_id,
            kind="custom",
            recommended_tool="run_lora_recipe",
            next_step=(
                f"Queue with run_lora_recipe(recipe_id='{recipe_id}'). "
                "Include start_frame_asset_id for image-to-video or omit it for text-to-video."
            ),
            guidance="\n".join(guidance_lines),
        )

    raise McpDomainError(
        "RECIPE_NOT_FOUND",
        f"Unknown recipe_id '{recipe_id}'.",
        hint="Call list_recipes to discover installed LoRA, IC-LoRA, Ingredients, and custom LoRA IDs.",
    )


def list_assets(
    handler: AppHandler,
    *,
    media_kind: MediaKind | None = None,
    sort: Literal["newest", "oldest"] = "newest",
    q: str | None = None,
    cursor: str | None = None,
    limit: int = 50,
) -> McpAssetPage:
    query = AssetListQuery(
        media_kind=media_kind,
        sort="created_at-desc" if sort == "newest" else "created_at-asc",
        q=q,
        cursor=cursor,
        limit=limit,
    )
    page = handler.assets.list_assets(query)
    return McpAssetPage(
        items=[_asset_summary(_to_asset_core(item), in_use=item.in_use) for item in page.items],
        next_cursor=page.next_cursor,
    )


def get_asset(handler: AppHandler, asset_id: str) -> McpAssetDetail:
    asset = handler.assets.get_asset(asset_id)
    summary = _asset_summary(_to_asset_core(asset))
    source: McpAssetSource | None = None
    producer_id = handler.assets.get_producer_id(asset_id)
    if producer_id is not None:
        try:
            gen = handler.queued_generations.get_generation(producer_id)
        except HTTPError as exc:
            if exc.status_code != 404:
                raise
        else:
            params = gen.spec.get("params")
            if isinstance(params, dict):
                source = McpAssetSource(
                    generation_id=gen.id,
                    feature=gen.feature,
                    params={key: params[key] for key in _SOURCE_PARAMS if key in params},
                )
    return McpAssetDetail(**summary.model_dump(), source=source)


def _encode_bounded_jpeg(path: Path) -> bytes:
    if not path.is_file():
        raise McpDomainError("PREVIEW_UNAVAILABLE", f"Preview file missing on disk: {path.name}.")
    try:
        return read_preview(str(path))
    except ValueError:
        with Image.open(path) as source:
            image = source.convert("RGB")
        for edge in (512, 256, 160):
            copy = image.copy()
            copy.thumbnail((edge, edge))
            buf = BytesIO()
            copy.save(buf, "JPEG", quality=65)
            if buf.tell() <= _MAX_PREVIEW_BYTES:
                return buf.getvalue()
        raise McpDomainError("PREVIEW_TOO_LARGE", "Could not encode a preview under 64 KB.")


def view_asset_preview(handler: AppHandler, asset_id: str) -> tuple[McpAssetDetail, bytes]:
    detail = get_asset(handler, asset_id)
    asset = handler.assets.get_asset(asset_id)
    if asset.media_kind == "audio":
        raise McpDomainError(
            "UNSUPPORTED_MEDIA",
            "Audio assets have no visual preview.",
            hint="Use get_asset to inspect audio duration and metadata.",
        )
    if asset.media_kind == "video":
        if not asset.thumbnail_path:
            raise McpDomainError(
                "PREVIEW_UNAVAILABLE",
                "Video does not have a thumbnail preview yet.",
                hint="Call extract_frame on this video and view_asset on the extracted image asset.",
            )
        return detail, _encode_bounded_jpeg(Path(asset.thumbnail_path))
    preview_path = Path(asset.thumbnail_path) if asset.thumbnail_path else Path(asset.path)
    return detail, _encode_bounded_jpeg(preview_path)


def import_asset(handler: AppHandler, path: str) -> McpAssetSummary:
    settings = handler.settings.get_settings_snapshot()
    if not settings.mcp_allow_local_import:
        raise McpDomainError(
            "LOCAL_IMPORT_DISABLED",
            "Local media file import via MCP is disabled in LTX Desktop Settings.",
            hint="Enable 'Allow local media import' under Settings > General > MCP Server, or use existing library assets.",
        )
    candidate = Path(path).expanduser()
    if not candidate.is_absolute():
        raise McpDomainError(
            "INVALID_PATH",
            "Path must be an absolute filesystem path to an image, video, or audio file.",
        )
    asset = handler.assets.ingest_asset(IngestAssetRequest(path=str(candidate)))
    return _asset_summary(_to_asset_core(asset))


def _resolve_video_ref(
    handler: AppHandler,
    *,
    generation_id: str | None,
    video_asset_id: str | None,
) -> InputAssetRef:
    if (generation_id is None) == (video_asset_id is None):
        raise McpDomainError(
            "INVALID_SOURCE",
            "Specify exactly one of generation_id or video_asset_id.",
        )
    if generation_id is not None:
        source = handler.queued_generations.get_generation(generation_id)
        if source.status != "succeeded":
            raise McpDomainError(
                "SOURCE_NOT_READY",
                f"Source generation '{generation_id}' has status '{source.status}'.",
                hint="Wait for completion with wait_for_generation before editing or extracting frames.",
            )
        video = next((out for out in source.outputs if out.media_kind == "video"), None)
        if video is None:
            raise McpDomainError("NO_VIDEO_OUTPUT", f"Generation '{generation_id}' produced no video output.")
        return InputAssetRef(assetId=video.id)
    assert video_asset_id is not None
    asset = handler.assets.get_asset(video_asset_id)
    if asset.media_kind != "video":
        raise McpDomainError("UNSUPPORTED_MEDIA", f"Asset '{video_asset_id}' is '{asset.media_kind}', expected 'video'.")
    return InputAssetRef(assetId=asset.id)


def extract_frame(
    handler: AppHandler,
    *,
    at_seconds: float | None = None,
    generation_id: str | None = None,
    video_asset_id: str | None = None,
) -> McpAssetSummary:
    video = _resolve_video_ref(handler, generation_id=generation_id, video_asset_id=video_asset_id)
    asset = handler.assets.extract_frame(video.assetId, at_seconds)
    return _asset_summary(_to_asset_core(asset))


def trim_media(
    handler: AppHandler,
    *,
    asset_id: str,
    start_sec: float,
    end_sec: float,
) -> McpAssetSummary:
    asset = handler.assets.get_asset(asset_id)
    req = TrimMediaRequest(startSec=start_sec, endSec=end_sec)
    if asset.media_kind == "video":
        trimmed = handler.assets.trim_video(asset_id, req)
    elif asset.media_kind == "audio":
        trimmed = handler.assets.trim_audio(asset_id, req)
    else:
        raise McpDomainError("UNSUPPORTED_MEDIA", f"Cannot trim asset of kind '{asset.media_kind}'.")
    return _asset_summary(_to_asset_core(trimmed))


def extract_audio(handler: AppHandler, *, asset_id: str) -> McpAssetSummary:
    asset = handler.assets.extract_audio(asset_id)
    return _asset_summary(_to_asset_core(asset))


def delete_asset(handler: AppHandler, *, asset_id: str) -> dict[str, str]:
    handler.assets.delete_asset(asset_id)
    return {"status": "deleted", "asset_id": asset_id}


def enhance_prompt(
    handler: AppHandler,
    *,
    prompt: str,
    media_type: Literal["video", "image"] = "video",
    provider: Literal["local", "api"] | None = None,
    negative_prompt: str | None = None,
    lora_catalog_ids: list[str] | None = None,
    custom_lora_ids: list[str] | None = None,
    ic_lora_id: str | None = None,
    image_asset_id: str | None = None,
    last_image_asset_id: str | None = None,
    image_asset_ids: list[str] | None = None,
    video_asset_id: str | None = None,
    duration: int | None = None,
    fps: int | None = None,
    aspect_ratio: LTXVideoGenAspectRatio | None = None,
    resolution: LTXVideoGenResolution | None = None,
) -> McpEnhancePromptResult:
    resolved_provider = provider
    if resolved_provider is None:
        resolved_provider = handler.prompt_enhancement.status(prefer_gemini=False).defaultProvider
    desktop_req = EnhancePromptRequest(
        prompt=prompt,
        negativePrompt=negative_prompt,
        loraCatalogIds=lora_catalog_ids or [],
        customLoraIds=custom_lora_ids or [],
        icLoraId=ic_lora_id,
        imageAssetId=image_asset_id,
        lastImageAssetId=last_image_asset_id,
        imageAssetIds=image_asset_ids or [],
        videoAssetId=video_asset_id,
        duration=duration,
        fps=fps,
        aspectRatio=aspect_ratio,
        resolution=resolution,
        provider=resolved_provider,
        mediaType=media_type,
    )
    res = handler.prompt_enhancement.enhance(resolve_enhance_media_assets(handler.assets, desktop_req))
    return McpEnhancePromptResult(enhanced_prompt=res.enhancedPrompt, provider=resolved_provider)


def _require_model(
    handler: AppHandler,
    model: OfferingId | None,
    capability: Literal["t2v", "i2v", "a2v", "ic_lora", "retake", "extend"],
) -> DownloadedLocalVideoGenerationModelSpecItem:
    specs = handler.video_generation.get_model_specs()
    if not specs.downloaded_local_models:
        raise McpDomainError(
            "NO_LOCAL_MODELS",
            "No local video generation models are downloaded or supported on this machine.",
            hint="Call get_system_status or download a model in LTX Desktop Settings > Models.",
        )
    target_id: OfferingId = model or specs.active_offering or specs.downloaded_local_models[0].model
    selected = next((item for item in specs.downloaded_local_models if item.model == target_id), None)
    if selected is None:
        available = ", ".join(item.model for item in specs.downloaded_local_models)
        raise McpDomainError(
            "MODEL_NOT_INSTALLED",
            f"Model '{target_id}' is not installed.",
            hint=f"Installed models: {available}. Call list_models for full specs.",
        )
    if selected.spec.capabilities is None or not getattr(selected.spec.capabilities, capability):
        raise McpDomainError(
            "CAPABILITY_UNSUPPORTED",
            f"Model '{target_id}' does not support '{capability}'.",
            hint="Call list_models to check capabilities per model.",
        )
    return selected


def _validate_video_matrix(
    selected: DownloadedLocalVideoGenerationModelSpecItem,
    *,
    capability: Literal["t2v", "i2v"],
    resolution: LocalVideoResolution,
    duration: LocalVideoDuration | None,
    fps: LTXVideoGenFps,
    aspect_ratio: str,
    camera_motion: VideoCameraMotion,
) -> None:
    supported = selected.spec.supported_resolutions_durations
    res_spec = supported.get(resolution)
    if res_spec is None:
        raise McpDomainError(
            "UNSUPPORTED_RESOLUTION",
            f"{selected.model} does not support resolution '{resolution}'.",
            hint=f"Supported resolutions: {', '.join(supported.keys())}.",
        )
    durations = res_spec.fps_to_durations.get(fps)
    if not durations:
        raise McpDomainError(
            "UNSUPPORTED_FPS",
            f"{selected.model} at {resolution} does not support {fps} fps.",
            hint=f"Supported FPS: {', '.join(str(k) for k in res_spec.fps_to_durations.keys())}.",
        )
    if duration is None:
        if selected.spec.capabilities is None or not selected.spec.capabilities.auto_duration:
            raise McpDomainError(
                "DURATION_REQUIRED",
                f"{selected.model} requires an explicit duration.",
                hint=f"Valid durations at {resolution}/{fps}fps: {', '.join(str(d) for d in durations)}.",
            )
    elif duration not in durations:
        raise McpDomainError(
            "UNSUPPORTED_DURATION",
            f"{selected.model} at {resolution}/{fps}fps does not support {duration}s.",
            hint=f"Valid durations: {', '.join(str(d) for d in durations)}.",
        )
    if camera_motion != "none" and selected.spec.capabilities is not None and not selected.spec.capabilities.camera_motion:
        raise McpDomainError(
            "UNSUPPORTED_CAMERA_MOTION",
            f"{selected.model} does not support camera_motion='{camera_motion}'.",
            hint="Set camera_motion='none' and describe camera movement in prompt instead.",
        )
    if aspect_ratio == "auto" and capability == "t2v":
        raise McpDomainError(
            "INVALID_ASPECT_RATIO",
            "aspect_ratio='auto' is only valid when a start_frame_asset_id is provided.",
            hint=f"Choose one of: {', '.join(res_spec.aspect_ratios)}.",
        )
    if aspect_ratio != "auto" and aspect_ratio not in res_spec.aspect_ratios:
        raise McpDomainError(
            "UNSUPPORTED_ASPECT_RATIO",
            f"aspect_ratio '{aspect_ratio}' is not supported at {resolution}.",
            hint=f"Available aspect ratios: {', '.join(res_spec.aspect_ratios)}.",
        )


def _provenance_for(handler: AppHandler, auto_enhance: bool | None) -> PromptProvenance:
    effective = (
        handler.settings.get_settings_snapshot().explore_auto_enhance_prompts
        if auto_enhance is None
        else auto_enhance
    )
    return "typed" if effective else "enhanced"


def _enqueue_with_idempotency(
    handler: AppHandler,
    idempotency_key: str | None,
    enqueue: Callable[[str | None], Generation],
    *,
    note: str | None = None,
) -> McpGenerationResult:
    gen_id = resolve_generation_id(idempotency_key)
    try:
        generation = enqueue(gen_id)
    except HTTPError as exc:
        if exc.status_code in (400, 409) and gen_id is not None and "already been used" in exc.detail:
            raise McpDomainError(
                "IDEMPOTENCY_CONFLICT",
                f"idempotency_key '{idempotency_key}' was already used with different parameters or was deleted.",
                hint="Use a fresh idempotency_key for new parameters, or reuse exact parameters for retries.",
            ) from exc
        raise
    snapshot = handler.queued_generations.get_queue_snapshot()
    return to_mcp_generation(generation, handler, snapshot=snapshot, note=note)


def generate_video(
    handler: AppHandler,
    *,
    prompt: str,
    model: OfferingId | None = None,
    resolution: LocalVideoResolution = "720p",
    duration: LocalVideoDuration | None = 5,
    fps: LTXVideoGenFps = 24,
    aspect_ratio: LTXVideoGenAspectRatio = "16:9",
    camera_motion: VideoCameraMotion = "none",
    negative_prompt: str = "",
    seed: int | None = None,
    auto_enhance: bool | None = None,
    idempotency_key: str | None = None,
) -> McpGenerationResult:
    selected = _require_model(handler, model, "t2v")
    _validate_video_matrix(
        selected,
        capability="t2v",
        resolution=resolution,
        duration=duration,
        fps=fps,
        aspect_ratio=aspect_ratio,
        camera_motion=camera_motion,
    )
    req = CreateTextToVideoRequest(
        params=TextToVideoParams(
            model=selected.model,
            prompt=prompt,
            promptProvenance=_provenance_for(handler, auto_enhance),
            resolution=resolution,
            duration=duration,
            fps=fps,
            aspectRatio=aspect_ratio,
            cameraMotion=camera_motion,
            negativePrompt=negative_prompt,
            seed=seed,
        )
    )
    return _enqueue_with_idempotency(
        handler,
        idempotency_key,
        lambda gid: handler.queued_generations.create_text_to_video(
            req, analytics_client="mcp", generation_id=gid
        ),
    )


def image_to_video(
    handler: AppHandler,
    *,
    start_frame_asset_id: str,
    prompt: str = "",
    end_frame_asset_id: str | None = None,
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
    selected = _require_model(handler, model, "i2v")
    _validate_video_matrix(
        selected,
        capability="i2v",
        resolution=resolution,
        duration=duration,
        fps=fps,
        aspect_ratio=aspect_ratio,
        camera_motion=camera_motion,
    )
    req = CreateImageToVideoRequest(
        params=ImageToVideoParams(
            model=selected.model,
            prompt=prompt,
            promptProvenance=_provenance_for(handler, auto_enhance),
            resolution=resolution,
            duration=duration,
            fps=fps,
            aspectRatio=aspect_ratio,
            cameraMotion=camera_motion,
            negativePrompt=negative_prompt,
            seed=seed,
        ),
        inputs=ImageToVideoInputs(
            startFrame=InputAssetRef(assetId=start_frame_asset_id),
            endFrame=None if end_frame_asset_id is None else InputAssetRef(assetId=end_frame_asset_id),
        ),
    )
    return _enqueue_with_idempotency(
        handler,
        idempotency_key,
        lambda gid: handler.queued_generations.create_image_to_video(
            req, analytics_client="mcp", generation_id=gid
        ),
    )


def audio_to_video(
    handler: AppHandler,
    *,
    audio_asset_id: str,
    prompt: str = "",
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
    selected = _require_model(handler, model, "a2v")
    cells = selected.spec.a2v_supported_resolutions_durations or {}
    res_spec = cells.get(resolution)
    if res_spec is None:
        raise McpDomainError(
            "UNSUPPORTED_RESOLUTION",
            f"Audio-to-video does not support '{resolution}' on {selected.model}.",
            hint=f"Supported resolutions: {', '.join(cells.keys())}.",
        )
    if aspect_ratio != "auto" and aspect_ratio not in res_spec.aspect_ratios:
        raise McpDomainError(
            "UNSUPPORTED_ASPECT_RATIO",
            f"aspect_ratio '{aspect_ratio}' is not supported at {resolution}.",
            hint=f"Available: {', '.join(res_spec.aspect_ratios)}.",
        )
    req = CreateAudioToVideoRequest(
        params=CreateAudioToVideoParams(
            model=selected.model,
            prompt=prompt,
            promptProvenance=_provenance_for(handler, auto_enhance),
            resolution=resolution,
            fps=24,
            aspectRatio=aspect_ratio,
            cameraMotion=camera_motion,
            negativePrompt=negative_prompt,
            seed=seed,
        ),
        inputs=AudioToVideoInputs(
            audio=InputAssetRef(assetId=audio_asset_id),
            startFrame=None if start_frame_asset_id is None else InputAssetRef(assetId=start_frame_asset_id),
        ),
    )
    return _enqueue_with_idempotency(
        handler,
        idempotency_key,
        lambda gid: handler.queued_generations.create_audio_to_video(
            req, analytics_client="mcp", generation_id=gid
        ),
    )


def generate_image(
    handler: AppHandler,
    *,
    prompt: str,
    width: int = 1024,
    height: int = 1024,
    provider: ImageProvider | None = None,
    seed: int | None = None,
    auto_enhance: bool | None = None,
    idempotency_key: str | None = None,
) -> McpGenerationResult:
    req = CreateTextToImageRequest(
        params=TextToImageParams(
            prompt=prompt,
            promptProvenance=_provenance_for(handler, auto_enhance),
            width=width,
            height=height,
            provider=provider,
            seed=seed,
        )
    )
    return _enqueue_with_idempotency(
        handler,
        idempotency_key,
        lambda gid: handler.queued_generations.create_text_to_image(
            req, analytics_client="mcp", generation_id=gid
        ),
    )


def _model_family(model: str) -> str:
    match = re.match(r"ltx-(\d+(?:\.\d+)?)", model, re.IGNORECASE)
    return f"LTX-{match.group(1)}" if match else model


def _pick_variant(
    download: DownloadSpec,
    installed_ids: list[str],
    model: str,
    name: str,
) -> tuple[str | None, str | None]:
    installed = [variant for variant in download.variants if variant.id in installed_ids]
    if len(download.variants) < 2 or not installed:
        return None, None
    family = _model_family(model)
    matching = next((variant for variant in installed if variant.base_model in (family, "LTX-2")), None)
    if matching is not None:
        return matching.id, None
    return installed[0].id, f"Only the {installed[0].label} variant of {name} is installed; results with {family} may vary."


def run_lora_recipe(
    handler: AppHandler,
    *,
    recipe_id: str,
    prompt: str,
    start_frame_asset_id: str | None = None,
    end_frame_asset_id: str | None = None,
    model: OfferingId | None = None,
    resolution: LocalVideoResolution = "720p",
    duration: LocalVideoDuration | None = 5,
    fps: LTXVideoGenFps = 24,
    aspect_ratio: LTXQueuedAspectRatio = "16:9",
    camera_motion: VideoCameraMotion = "none",
    negative_prompt: str = "",
    scale: float = 1.0,
    seed: int | None = None,
    auto_enhance: bool | None = None,
    idempotency_key: str | None = None,
) -> McpGenerationResult:
    provenance = _provenance_for(handler, auto_enhance)
    recipe = get_lora_recipe(recipe_id)
    if recipe is not None and recipe.mode in ("t2v", "i2v"):
        selected = _require_model(handler, model, recipe.mode)
        if recipe.mode == "i2v" and start_frame_asset_id is None:
            raise McpDomainError(
                "START_FRAME_REQUIRED",
                f"Recipe '{recipe_id}' requires start_frame_asset_id.",
            )
        if recipe.requires_end_frame and end_frame_asset_id is None:
            raise McpDomainError(
                "END_FRAME_REQUIRED",
                f"Recipe '{recipe_id}' requires end_frame_asset_id.",
            )
        _validate_video_matrix(
            selected,
            capability=recipe.mode,
            resolution=resolution,
            duration=duration,
            fps=fps,
            aspect_ratio=aspect_ratio,
            camera_motion=camera_motion,
        )
        listed = next((item for item in handler.catalog.list_loras().loras if item.lora.id == recipe.catalog_id), None)
        final_prompt = prompt
        if listed is not None and listed.lora.trigger and listed.lora.trigger_placement:
            if not (recipe.prompt_template and listed.lora.trigger.lower() in recipe.prompt_template.lower()):
                final_prompt = enforce_trigger_placement(prompt, listed.lora.trigger, listed.lora.trigger_placement)
        variant, note = (
            (None, None)
            if listed is None
            else _pick_variant(listed.lora.download, listed.downloaded_variant_ids, selected.model, listed.lora.name)
        )
        req = CreateLoraRecipeRequest(
            params=LoraRecipeParams.model_validate(
                {
                    "model": selected.model,
                    "prompt": final_prompt,
                    "promptProvenance": provenance,
                    "resolution": resolution,
                    "duration": duration,
                    "fps": fps,
                    "aspectRatio": aspect_ratio,
                    "cameraMotion": camera_motion,
                    "negativePrompt": negative_prompt,
                    "scale": scale,
                    "seed": seed,
                    "catalogId": recipe.catalog_id,
                    **({"variantId": variant} if variant else {}),
                }
            ),
            inputs=(
                None
                if start_frame_asset_id is None
                else LoraRecipeInputs(
                    startFrame=InputAssetRef(assetId=start_frame_asset_id),
                    endFrame=None if end_frame_asset_id is None else InputAssetRef(assetId=end_frame_asset_id),
                )
            ),
        )
        return _enqueue_with_idempotency(
            handler,
            idempotency_key,
            lambda gid: handler.queued_generations.create_lora_recipe(
                recipe_id, req, analytics_client="mcp", generation_id=gid
            ),
            note=note,
        )

    custom_record = read_custom_lora(handler.models.models_dir, recipe_id)
    if custom_record is not None:
        cap: Literal["t2v", "i2v"] = "i2v" if start_frame_asset_id is not None else "t2v"
        selected = _require_model(handler, model, cap)
        _validate_video_matrix(
            selected,
            capability=cap,
            resolution=resolution,
            duration=duration,
            fps=fps,
            aspect_ratio=aspect_ratio,
            camera_motion=camera_motion,
        )
        custom_req = CreateCustomLoraRequest(
            params=CustomLoraParams(
                model=selected.model,
                prompt=prompt,
                promptProvenance=provenance,
                resolution=resolution,
                duration=duration,
                fps=fps,
                aspectRatio=aspect_ratio,
                cameraMotion=camera_motion,
                negativePrompt=negative_prompt,
                customLoraId=recipe_id,
                scale=scale,
                seed=seed,
            ),
            inputs=(
                None
                if start_frame_asset_id is None
                else LoraRecipeInputs(
                    startFrame=InputAssetRef(assetId=start_frame_asset_id),
                    endFrame=None if end_frame_asset_id is None else InputAssetRef(assetId=end_frame_asset_id),
                )
            ),
        )
        return _enqueue_with_idempotency(
            handler,
            idempotency_key,
            lambda gid: handler.queued_generations.create_custom_lora(
                custom_req, analytics_client="mcp", generation_id=gid
            ),
        )

    raise McpDomainError(
        "RECIPE_NOT_FOUND",
        f"Unknown LoRA or custom LoRA recipe_id '{recipe_id}'.",
        hint="Call list_recipes to see valid recipe IDs.",
    )


def run_video_recipe(
    handler: AppHandler,
    *,
    recipe_id: str,
    generation_id: str | None = None,
    video_asset_id: str | None = None,
    reference_image_asset_id: str | None = None,
    prompt: str = "",
    template_values: dict[str, str] | None = None,
    negative_prompt: str = "",
    model: OfferingId | None = None,
    resolution: IcLoraRecipeResolution = "720p",
    fps: float | None = None,
    control_type: IcLoraControlType | None = None,
    auto_enhance: bool | None = None,
    idempotency_key: str | None = None,
) -> McpGenerationResult:
    ic_recipe = get_ic_lora_recipe(recipe_id)
    if ic_recipe is None or ic_recipe.kind == "track":
        raise McpDomainError("RECIPE_NOT_FOUND", f"Unknown video recipe '{recipe_id}'.", hint="Call list_recipes(kind='video').")
    if ic_recipe.kind == "sheet":
        raise McpDomainError("WRONG_RECIPE_TOOL", f"'{recipe_id}' takes images, not a video. Use run_ingredients.")
    selected = _require_model(handler, model, "ic_lora")
    listed = next((item for item in handler.catalog.list_ic_loras().ic_loras if item.ic_lora.id == ic_recipe.catalog_id), None)
    if listed is None:
        raise McpDomainError("RECIPE_NOT_FOUND", f"IC-LoRA catalog entry for '{recipe_id}' not found.")
    ic_lora: IcLoraCatalogItem = listed.ic_lora
    variant, note = _pick_variant(ic_lora.download, listed.downloaded_variant_ids, selected.model, ic_lora.name)
    effective_prompt = prompt
    if ic_lora.prompt_template is not None:
        try:
            effective_prompt = fill_prompt_template(ic_lora.prompt_template, template_values or {})
        except ValueError as exc:
            raise McpDomainError(
                "INVALID_TEMPLATE_VALUES",
                str(exc),
                hint=f"Call get_recipe_guidance('{recipe_id}') to see required template_values keys.",
            ) from exc
    elif template_values:
        raise McpDomainError(
            "UNEXPECTED_TEMPLATE_VALUES",
            f"Recipe '{recipe_id}' does not use template_values; pass text in prompt instead.",
        )

    video = _resolve_video_ref(handler, generation_id=generation_id, video_asset_id=video_asset_id)
    req = CreateIcLoraRecipeRequest(
        params=IcLoraVideoRecipeParams.model_validate(
            {
                "model": selected.model,
                "prompt": effective_prompt,
                "promptProvenance": _provenance_for(handler, auto_enhance),
                "negativePrompt": negative_prompt,
                "resolution": resolution,
                "fps": fps,
                "controlType": control_type,
                **({"variantId": variant} if variant else {}),
            }
        ),
        inputs=IcLoraRecipeInputs(
            video=video,
            image=None if reference_image_asset_id is None else InputAssetRef(assetId=reference_image_asset_id),
        ),
    )
    return _enqueue_with_idempotency(
        handler,
        idempotency_key,
        lambda gid: handler.queued_generations.create_ic_lora_recipe(
            recipe_id, req, analytics_client="mcp", generation_id=gid
        ),
        note=note,
    )


def run_ingredients(
    handler: AppHandler,
    *,
    image_asset_ids: list[str],
    prompt: str,
    model: OfferingId | None = None,
    resolution: IngredientsResolution = "720p",
    duration: IngredientsDuration = 5,
    aspect_ratio: LTXVideoGenAspectRatio = "16:9",
    negative_prompt: str = "",
    auto_enhance: bool | None = None,
    idempotency_key: str | None = None,
) -> McpGenerationResult:
    selected = _require_model(handler, model, "ic_lora")
    ic_recipe = get_ic_lora_recipe(_INGREDIENTS_RECIPE_ID)
    listed = (
        None
        if ic_recipe is None
        else next((item for item in handler.catalog.list_ic_loras().ic_loras if item.ic_lora.id == ic_recipe.catalog_id), None)
    )
    variant, note = (
        (None, None)
        if listed is None
        else _pick_variant(listed.ic_lora.download, listed.downloaded_variant_ids, selected.model, listed.ic_lora.name)
    )
    req = CreateIcLoraSheetRequest(
        params=IcLoraSheetParams.model_validate(
            {
                "model": selected.model,
                "prompt": prompt,
                "promptProvenance": _provenance_for(handler, auto_enhance),
                "negativePrompt": negative_prompt,
                "resolution": resolution,
                "duration": duration,
                "aspectRatio": aspect_ratio,
                "scale": _INGREDIENTS_SCALE,
                **({"variantId": variant} if variant else {}),
            }
        ),
        inputs=IcLoraSheetInputs(images=[InputAssetRef(assetId=asset_id) for asset_id in image_asset_ids]),
    )
    return _enqueue_with_idempotency(
        handler,
        idempotency_key,
        lambda gid: handler.queued_generations.create_ic_lora_recipe(
            _INGREDIENTS_RECIPE_ID, req, analytics_client="mcp", generation_id=gid
        ),
        note=note,
    )


def extend_video(
    handler: AppHandler,
    *,
    generation_id: str | None = None,
    video_asset_id: str | None = None,
    added_seconds: float | None = None,
    total_seconds: float | None = None,
    mode: ExtendMode = "end",
    context_mode: EditContextMode = "long",
    resolution: TargetResolution | None = None,
    prompt: str = "",
    negative_prompt: str = "",
    model: OfferingId | None = None,
    auto_enhance: bool | None = None,
    idempotency_key: str | None = None,
) -> McpGenerationResult:
    selected = _require_model(handler, model, "extend")
    if (added_seconds is None) == (total_seconds is None):
        raise McpDomainError(
            "INVALID_DURATION",
            "Specify exactly one of added_seconds or total_seconds.",
        )
    video = _resolve_video_ref(handler, generation_id=generation_id, video_asset_id=video_asset_id)
    added = added_seconds
    if added is None:
        assert total_seconds is not None
        meta = handler.assets.get_asset(video.assetId).metadata
        if meta.mediaType != "video":
            raise McpDomainError("UNSUPPORTED_MEDIA", "Source asset must be a video.")
        source_sec = meta.metadata.durationMs / 1000.0
        added = round(total_seconds - source_sec, 2)
        if not 2 <= added <= 20:
            raise McpDomainError(
                "INVALID_EXTEND_DURATION",
                f"Source is {source_sec:.1f}s, so total_seconds={total_seconds:g}s adds {added:g}s (must be 2–20s).",
            )
    req = CreateExtendRequest(
        params=ExtendParams.model_validate(
            {
                "model": selected.model,
                "prompt": prompt,
                "promptProvenance": _provenance_for(handler, auto_enhance),
                "negativePrompt": negative_prompt,
                "duration": added,
                "mode": mode,
                "contextMode": context_mode,
                "resolution": resolution,
            }
        ),
        inputs=ExtendInputs(video=video),
    )
    return _enqueue_with_idempotency(
        handler,
        idempotency_key,
        lambda gid: handler.queued_generations.create_extend(
            req, analytics_client="mcp", generation_id=gid
        ),
    )


def retake_video(
    handler: AppHandler,
    *,
    start_time: float,
    duration: float,
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
    selected = _require_model(handler, model, "retake")
    video = _resolve_video_ref(handler, generation_id=generation_id, video_asset_id=video_asset_id)
    req = CreateRetakeRequest(
        params=RetakeRequestParams.model_validate(
            {
                "model": selected.model,
                "prompt": prompt,
                "promptProvenance": _provenance_for(handler, auto_enhance),
                "negativePrompt": negative_prompt,
                "startTime": start_time,
                "duration": duration,
                "mode": mode,
                "contextMode": context_mode,
            }
        ),
        inputs=RetakeInputs(video=video),
    )
    return _enqueue_with_idempotency(
        handler,
        idempotency_key,
        lambda gid: handler.queued_generations.create_retake(
            req, analytics_client="mcp", generation_id=gid
        ),
    )


def get_queue(handler: AppHandler) -> QueueSnapshot:
    return handler.queued_generations.get_queue_snapshot()


def list_generations(
    handler: AppHandler,
    *,
    status: GenerationStatus | None = None,
    feature: str | None = None,
    limit: int = 20,
) -> list[McpGenerationResult]:
    gens = handler.queued_generations.list_generations(feature=feature, limit=limit if status is None else 100)
    if status is not None:
        gens = [gen for gen in gens if gen.status == status][:limit]
    snapshot = handler.queued_generations.get_queue_snapshot()
    return [to_mcp_generation(gen, handler, snapshot=snapshot) for gen in gens]


def get_generation(handler: AppHandler, generation_id: str) -> McpGenerationResult:
    gen = handler.queued_generations.get_generation(generation_id)
    snapshot = handler.queued_generations.get_queue_snapshot()
    return to_mcp_generation(gen, handler, snapshot=snapshot)


def manage_generation(
    handler: AppHandler,
    *,
    generation_id: str,
    action: Literal["cancel", "retry", "reorder"],
    before_generation_id: str | None = None,
) -> McpGenerationResult:
    if action == "cancel":
        gen = handler.queued_generations.cancel_generation(generation_id)
    elif action == "retry":
        gen = handler.queued_generations.retry_generation(generation_id)
    else:
        snapshot = handler.queued_generations.reorder_queue(generation_id, before_generation_id)
        gen = handler.queued_generations.get_generation(generation_id)
        return to_mcp_generation(gen, handler, snapshot=snapshot)
    snapshot = handler.queued_generations.get_queue_snapshot()
    return to_mcp_generation(gen, handler, snapshot=snapshot)


def _progress_from_snapshot(snapshot: QueueSnapshot, generation_id: str, status: GenerationStatus) -> tuple[float, str]:
    entry = _queue_entry_for(snapshot, generation_id)
    if entry is not None and entry.progress is not None:
        pct = float(max(0, min(100, entry.progress.progress)))
        phase = entry.progress.phase
        if entry.progress.currentStep is not None and entry.progress.totalSteps:
            return pct, f"{phase} (step {entry.progress.currentStep}/{entry.progress.totalSteps})"
        return pct, phase
    if status == "queued":
        for idx, queued_entry in enumerate(snapshot.queued, start=1):
            if queued_entry.generation.id == generation_id:
                return 0.0, f"queued (position {idx}/{len(snapshot.queued)})"
    return 0.0, status


async def wait_for_generation_with_progress(
    handler: AppHandler,
    generation_id: str,
    *,
    timeout_seconds: float,
    report_progress: Callable[[float, float, str], Awaitable[None]],
    poll_interval: float = 0.25,
) -> McpGenerationResult:
    deadline = time.monotonic() + timeout_seconds
    last_signature: tuple[object, ...] | None = None
    while True:
        gen = await asyncio.to_thread(handler.queued_generations.get_generation, generation_id)
        snap = await asyncio.to_thread(handler.queued_generations.get_queue_snapshot)
        if gen.status in ("succeeded", "failed", "cancelled"):
            await report_progress(100.0, 100.0, f"Generation {gen.status}")
            return to_mcp_generation(gen, handler, snapshot=snap)
        percent, message = _progress_from_snapshot(snap, generation_id, gen.status)
        signature = (gen.status, round(percent, 1), message)
        if signature != last_signature:
            await report_progress(percent, 100.0, message)
            last_signature = signature
        if time.monotonic() >= deadline:
            return to_mcp_generation(
                gen,
                handler,
                snapshot=snap,
                note=(
                    f"Timed out after {timeout_seconds:g}s; generation is still {gen.status}. "
                    "Call wait_for_generation again to keep waiting."
                ),
            )
        await asyncio.sleep(poll_interval)
