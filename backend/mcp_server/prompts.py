"""Reusable MCP workflow prompts for multi-step creative video tasks."""
# pyright: reportUnusedFunction=false

from __future__ import annotations

from typing import Annotated

from mcp.server.mcpserver import MCPServer
from mcp.types import PromptMessage, TextContent
from pydantic import Field


def register_prompts(mcp: MCPServer) -> None:
    @mcp.prompt(
        name="storyboard_to_video",
        title="Storyboard to Continuous Video",
        description="Plan and generate a multi-shot continuous video sequence by chaining last-frame extraction.",
    )
    def storyboard_to_video(
        concept: Annotated[str, Field(description="Story or visual concept for the multi-shot sequence.")],
        shot_count: Annotated[int, Field(ge=2, le=8, description="Number of connected shots to generate.")] = 3,
        resolution: Annotated[str, Field(description="Target resolution (e.g. 540p, 720p, 1080p).")] = "720p",
    ) -> list[PromptMessage]:
        text = (
            f"Create a {shot_count}-shot continuous video sequence for: {concept}\n\n"
            "Follow this LTX Desktop MCP workflow:\n"
            "1. Call `list_models` to inspect installed local video models and valid dimensions.\n"
            f"2. Generate Shot 1 at {resolution} using `generate_video` (or `run_lora_recipe` if a style fits).\n"
            "3. Wait for Shot 1 with `wait_for_generation`.\n"
            "4. Call `extract_frame` (omit `at_seconds` to capture the final frame) on Shot 1's output, "
            "then call `view_asset` on that frame to verify continuity.\n"
            "5. Queue Shot 2 with `image_to_video` using that extracted frame as `start_frame_asset_id`.\n"
            "6. Repeat steps 3-5 until all shots are complete, summarizing every generation ID and output asset ID."
        )
        return [PromptMessage(role="user", content=TextContent(type="text", text=text))]

    @mcp.prompt(
        name="restyle_video",
        title="Restyle Existing Video with IC-LoRA",
        description="Apply an IC-LoRA transformation (Day to Night, Restore, Deblur, Colorize, etc.) safely.",
    )
    def restyle_video(
        video_asset_id: Annotated[str, Field(description="Target video asset ID to restyle.")],
        style_goal: Annotated[str, Field(description="Desired visual effect or restoration goal.")],
    ) -> list[PromptMessage]:
        text = (
            f"Restyle video asset `{video_asset_id}` to achieve: {style_goal}\n\n"
            "Follow this workflow:\n"
            f"1. Call `get_asset(asset_id='{video_asset_id}')` and `view_asset` to inspect duration and framing.\n"
            "2. Call `list_recipes(kind='video')` to pick the matching IC-LoRA recipe and check download status.\n"
            "3. Call `get_recipe_guidance` for the chosen `recipe_id` to inspect prompt or `template_values` rules.\n"
            "4. If the clip exceeds IC-LoRA duration limits, use `trim_media` first.\n"
            "5. Queue `run_video_recipe`, then call `wait_for_generation` and `view_asset` on the output."
        )
        return [PromptMessage(role="user", content=TextContent(type="text", text=text))]

    @mcp.prompt(
        name="consistent_character_video",
        title="Consistent Character / Ingredients Video",
        description="Create or gather reference stills and synthesize a scene using the Ingredients recipe.",
    )
    def consistent_character_video(
        character_description: Annotated[str, Field(description="Subject, character, or prop appearance.")],
        scene_prompt: Annotated[str, Field(description="Action and setting for the final video.")],
    ) -> list[PromptMessage]:
        text = (
            f"Produce a consistent character video.\nSubject: {character_description}\nScene: {scene_prompt}\n\n"
            "Follow this workflow:\n"
            "1. Call `list_models` and `list_recipes(kind='sheet')` to verify Ingredients and image generation readiness.\n"
            "2. Locate existing character assets with `list_assets(media_kind='image')` or generate reference stills "
            "with `generate_image` + `wait_for_generation`.\n"
            "3. Inspect each candidate reference image with `view_asset`.\n"
            "4. Call `get_recipe_guidance(recipe_id='ingredients')`, then queue `run_ingredients` with 1-10 image asset IDs."
        )
        return [PromptMessage(role="user", content=TextContent(type="text", text=text))]

    @mcp.prompt(
        name="refine_and_extend_clip",
        title="Refine or Extend a Generated Clip",
        description="Inspect a clip and apply targeted Retake segment replacement or Extend continuation.",
    )
    def refine_and_extend_clip(
        generation_id: Annotated[str, Field(description="Existing generation ID to inspect and edit.")],
        edit_goal: Annotated[str, Field(description="What to fix (retake) or how to continue the clip (extend).")],
    ) -> list[PromptMessage]:
        text = (
            f"Refine or extend generation `{generation_id}` for goal: {edit_goal}\n\n"
            "Follow this workflow:\n"
            f"1. Call `get_generation(generation_id='{generation_id}')` and inspect its output video asset with `get_asset`.\n"
            "2. Choose `retake_video` to replace a specific `start_time` + `duration` window (2-10s) inside the clip, "
            "or `extend_video` (`added_seconds` or `total_seconds`, `mode='start'|'end'`) to add 2-20s.\n"
            "3. Queue the edit and monitor completion via `wait_for_generation`."
        )
        return [PromptMessage(role="user", content=TextContent(type="text", text=text))]
