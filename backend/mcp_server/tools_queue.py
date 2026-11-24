"""Queue monitoring, progress waiting, and lifecycle MCP tools."""
# pyright: reportUnusedFunction=false

from __future__ import annotations

import asyncio
from typing import TYPE_CHECKING, Annotated, Any, Literal

from mcp.server.mcpserver import Context, MCPServer
from mcp.types import ToolAnnotations
from pydantic import Field

from api_types import GenerationStatus, QueueSnapshot
from mcp_server import domain
from mcp_server.schemas import McpGenerationResult

if TYPE_CHECKING:
    from app_handler import AppHandler

_READ_ONLY = ToolAnnotations(
    read_only_hint=True,
    destructive_hint=False,
    idempotent_hint=True,
    open_world_hint=False,
)
_MANAGE_ANNOTATIONS = ToolAnnotations(
    read_only_hint=False,
    destructive_hint=True,
    idempotent_hint=True,
    open_world_hint=False,
)


def register_queue_tools(mcp: MCPServer, handler: AppHandler) -> None:
    @mcp.tool(name="get_queue", title="Get Generation Queue", annotations=_READ_ONLY)
    async def get_queue() -> QueueSnapshot:
        """Read the current generation queue snapshot (active, queued, done, and failed entries)."""
        try:
            return await asyncio.to_thread(domain.get_queue, handler)
        except Exception as exc:
            raise domain.raise_tool_error(exc) from exc

    @mcp.tool(name="list_generations", title="List Generations", annotations=_READ_ONLY)
    async def list_generations(
        status: GenerationStatus | None = None,
        feature: str | None = None,
        limit: Annotated[int, Field(ge=1, le=100)] = 20,
    ) -> list[McpGenerationResult]:
        """List recent generation jobs with status, timing, error codes, and output assets."""
        try:
            return await asyncio.to_thread(
                domain.list_generations,
                handler,
                status=status,
                feature=feature,
                limit=limit,
            )
        except Exception as exc:
            raise domain.raise_tool_error(exc) from exc

    @mcp.tool(name="get_generation", title="Get Generation Details", annotations=_READ_ONLY)
    async def get_generation(
        generation_id: Annotated[str, Field(min_length=1)],
    ) -> McpGenerationResult:
        """Read live progress, status, error code, and output asset metadata for one generation."""
        try:
            return await asyncio.to_thread(domain.get_generation, handler, generation_id)
        except Exception as exc:
            raise domain.raise_tool_error(exc) from exc

    @mcp.tool(name="wait_for_generation", title="Wait for Generation Completion", annotations=_READ_ONLY)
    async def wait_for_generation(
        generation_id: Annotated[str, Field(min_length=1)],
        ctx: Context[Any, Any],
        timeout_seconds: Annotated[float, Field(ge=1.0, le=3600.0)] = 600.0,
    ) -> McpGenerationResult:
        """Wait for a generation to reach a terminal state while streaming live MCP progress updates.

        Timeout returns the current status without cancelling the underlying GPU job.
        """
        async def _report(progress: float, total: float, message: str) -> None:
            await ctx.report_progress(progress=progress, total=total, message=message)

        try:
            return await domain.wait_for_generation_with_progress(
                handler,
                generation_id,
                timeout_seconds=timeout_seconds,
                report_progress=_report,
            )
        except Exception as exc:
            raise domain.raise_tool_error(exc) from exc

    @mcp.tool(name="manage_generation", title="Manage Queued/Failed Generation", annotations=_MANAGE_ANNOTATIONS)
    async def manage_generation(
        generation_id: Annotated[str, Field(min_length=1)],
        action: Literal["cancel", "retry", "reorder"],
        before_generation_id: Annotated[
            str | None,
            Field(description="Only for action='reorder': move generation_id ahead of this queued job ID (or omit to move to end)."),
        ] = None,
    ) -> McpGenerationResult:
        """Cancel a running/queued job, retry a failed job, or reorder a queued job."""
        try:
            return await asyncio.to_thread(
                domain.manage_generation,
                handler,
                generation_id=generation_id,
                action=action,
                before_generation_id=before_generation_id,
            )
        except Exception as exc:
            raise domain.raise_tool_error(exc) from exc
