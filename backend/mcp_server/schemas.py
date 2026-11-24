"""Strict Pydantic input and output schemas for LTX Desktop MCP tools."""

from __future__ import annotations

from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, JsonValue

from api_types import (
    AssetMetadata,
    GenerationErrorCode,
    GenerationStatus,
    ImageGenerationStatusResponse,
    LTXOfferingCapabilitiesSpec,
    LTXVideoGenPipeline,
    LTXVideoGenResolution,
    LTXVideoGenerationResolutionSpec,
    MediaKind,
    OfferingId,
    Origin,
)
from runtime_config.runtime_policy import LocalGenerationMode


class McpToolError(BaseModel):
    model_config = ConfigDict(extra="forbid")

    code: str
    message: str
    hint: str | None = None


class McpGpuSummary(BaseModel):
    model_config = ConfigDict(extra="forbid")

    cuda_available: bool
    mps_available: bool
    gpu_available: bool
    gpu_name: str | None
    vram_gb: int | None
    system: str | None
    total_ram_bytes: int | None = None


class McpQueueSummary(BaseModel):
    model_config = ConfigDict(extra="forbid")

    active_count: int
    queued_count: int
    done_count: int
    failed_count: int
    active_generation_ids: list[str]


class McpSystemStatus(BaseModel):
    model_config = ConfigDict(extra="forbid")

    status: Literal["ok"] = "ok"
    local_viable: bool
    force_api_generations: bool
    local_generations_mode: LocalGenerationMode
    models_loaded: bool
    active_model: str | None
    models_downloaded_count: int
    hf_authenticated: bool
    local_import_allowed: bool
    gpu: McpGpuSummary
    queue: McpQueueSummary


class McpVideoModelSpec(BaseModel):
    """Compact model capability summary safe for MCP tool and resource payloads."""

    model_config = ConfigDict(extra="forbid")

    model: OfferingId
    pipeline: LTXVideoGenPipeline
    display_name: str
    default: bool
    supported_resolutions_durations: dict[LTXVideoGenResolution, LTXVideoGenerationResolutionSpec]
    a2v_supported_resolutions_durations: dict[LTXVideoGenResolution, LTXVideoGenerationResolutionSpec] | None = None
    capabilities: LTXOfferingCapabilitiesSpec | None = None


class McpModelsResponse(BaseModel):
    model_config = ConfigDict(extra="forbid")

    local_viable: bool
    default_video_model: OfferingId | None
    video_models: list[McpVideoModelSpec]
    image_generation: ImageGenerationStatusResponse


RecipeKindFilter = Literal["lora", "video", "sheet", "custom"]


class McpRecipeSummary(BaseModel):
    model_config = ConfigDict(extra="forbid")

    recipe_id: str
    kind: RecipeKindFilter
    mode: Literal["t2v", "i2v", "a2v", "video", "sheet"]
    name: str
    description: str
    downloaded: bool
    requires_hf_login: bool
    requires_start_frame: bool = False
    requires_end_frame: bool = False
    requires_source_video: bool = False
    reference_image: Literal["none", "optional", "required"] = "none"
    prompt_required: bool = True
    min_images: int | None = None
    max_images: int | None = None
    has_prompt_template: bool = False


class McpRecipeGuidance(BaseModel):
    model_config = ConfigDict(extra="forbid")

    recipe_id: str
    kind: RecipeKindFilter
    recommended_tool: str
    next_step: str
    guidance: str
    template: str | None = None
    template_placeholders: list[str] = Field(default_factory=list)


class McpAssetSummary(BaseModel):
    model_config = ConfigDict(extra="forbid")

    id: str
    media_kind: MediaKind
    origin: Origin
    mime_type: str
    name: str
    metadata: AssetMetadata
    created: str
    in_use: bool = False


class McpAssetSource(BaseModel):
    model_config = ConfigDict(extra="forbid")

    generation_id: str
    feature: str
    params: dict[str, JsonValue]


class McpAssetDetail(McpAssetSummary):
    source: McpAssetSource | None = None


class McpAssetPage(BaseModel):
    model_config = ConfigDict(extra="forbid")

    items: list[McpAssetSummary]
    next_cursor: str | None


class McpGenerationJobSource(BaseModel):
    model_config = ConfigDict(extra="forbid")

    id: str
    name: str


class McpGenerationResult(BaseModel):
    model_config = ConfigDict(extra="forbid")

    id: str
    feature: str
    status: GenerationStatus
    error_code: GenerationErrorCode | None = None
    attempt_count: int
    phase: str | None = None
    progress: int | None = None
    current_step: int | None = None
    total_steps: int | None = None
    took_seconds: float | None = None
    finished: str | None = None
    source: McpGenerationJobSource | Literal["unavailable"] | None = None
    clip_seconds: float | None = None
    size: str | None = None
    outputs: list[McpAssetSummary]
    note: str | None = None


class McpEnhancePromptResult(BaseModel):
    model_config = ConfigDict(extra="forbid")

    enhanced_prompt: str
    provider: Literal["local", "api"]
