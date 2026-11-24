"""End-to-end MCP protocol tests for tools, resources, prompts, and progress streaming."""

from __future__ import annotations

import asyncio
import json
import time
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from pathlib import Path

import httpx2
from mcp.client.session import ClientSession
from mcp.client.streamable_http import streamable_http_client
from mcp.types import ImageContent, TextContent
from PIL import Image
from starlette.testclient import TestClient

from api_types import IngestAssetRequest


def _wait_mcp_ready(client: TestClient, *, timeout: float = 8.0) -> tuple[str, str]:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        status = client.get("/api/mcp/status").json()
        if status.get("serving") is True and status.get("url"):
            cfg = client.get("/api/mcp/client-config").json()
            return str(status["url"]), str(cfg["token"])
        time.sleep(0.05)
    raise AssertionError("MCP server did not start in time")


@asynccontextmanager
async def _mcp_session(url: str, token: str) -> AsyncIterator[ClientSession]:
    async with httpx2.AsyncClient(
        headers={"Authorization": f"Bearer {token}"},
        timeout=httpx2.Timeout(30.0, read=None),
    ) as http_client:
        async with streamable_http_client(url, http_client=http_client) as (read_stream, write_stream):
            async with ClientSession(read_stream, write_stream) as session:
                await session.initialize()
                yield session


def _parse_tool_json(result) -> dict:
    assert result.content
    first = result.content[0]
    assert isinstance(first, TextContent)
    return json.loads(first.text)


def test_mcp_initialize_lists_all_tools_with_valid_annotations_and_schemas(client, monkeypatch) -> None:
    monkeypatch.setenv("LTX_MCP_PORT", "0")
    assert client.post("/api/settings", json={"mcpEnabled": True}).status_code == 200
    url, token = _wait_mcp_ready(client)

    async def _run() -> None:
        async with _mcp_session(url, token) as session:
            tools_result = await session.list_tools()
            by_name = {tool.name: tool for tool in tools_result.tools}
            expected = {
                "get_system_status",
                "list_models",
                "list_recipes",
                "get_recipe_guidance",
                "enhance_prompt",
                "list_assets",
                "get_asset",
                "view_asset",
                "import_asset",
                "extract_frame",
                "trim_media",
                "extract_audio",
                "delete_asset",
                "generate_video",
                "image_to_video",
                "audio_to_video",
                "generate_image",
                "run_lora_recipe",
                "run_video_recipe",
                "run_ingredients",
                "extend_video",
                "retake_video",
                "get_queue",
                "list_generations",
                "get_generation",
                "wait_for_generation",
                "manage_generation",
            }
            assert expected.issubset(by_name.keys())
            for name in expected:
                tool = by_name[name]
                assert tool.annotations is not None
                assert tool.input_schema is not None
            assert by_name["get_system_status"].annotations.read_only_hint is True
            assert by_name["delete_asset"].annotations.destructive_hint is True
            assert by_name["generate_video"].annotations.read_only_hint is False

    asyncio.run(_run())


def test_mcp_list_models_and_resources_reflect_installed_specs(
    client, create_fake_model_files, monkeypatch
) -> None:
    create_fake_model_files(include_zit=True)
    monkeypatch.setenv("LTX_MCP_PORT", "0")
    assert client.post("/api/settings", json={"mcpEnabled": True}).status_code == 200
    url, token = _wait_mcp_ready(client)

    async def _run() -> None:
        async with _mcp_session(url, token) as session:
            tool_res = await session.call_tool("list_models")
            assert not tool_res.is_error
            tool_payload = _parse_tool_json(tool_res)
            assert tool_payload["local_viable"] is True
            assert len(tool_payload["video_models"]) >= 1

            resource_res = await session.read_resource("ltx://models")
            resource_payload = json.loads(resource_res.contents[0].text)
            assert resource_payload == tool_payload

    asyncio.run(_run())


def test_mcp_generate_video_rejects_unsupported_resolution_with_actionable_error(
    client, create_fake_model_files, monkeypatch
) -> None:
    create_fake_model_files()
    monkeypatch.setenv("LTX_MCP_PORT", "0")
    assert client.post("/api/settings", json={"mcpEnabled": True}).status_code == 200
    url, token = _wait_mcp_ready(client)

    async def _run() -> None:
        async with _mcp_session(url, token) as session:
            res = await session.call_tool(
                "generate_video",
                arguments={
                    "prompt": "Waves crashing",
                    "resolution": "2160p",
                    "duration": 5,
                    "aspect_ratio": "21:9",
                },
            )
            assert res.is_error is True
            text = res.content[0].text
            assert "UNSUPPORTED_ASPECT_RATIO" in text or "UNSUPPORTED_RESOLUTION" in text
            assert "hint" in text

    asyncio.run(_run())


def test_mcp_generate_video_idempotency_key_deduplicates_retries(
    test_state, client, create_fake_model_files, monkeypatch
) -> None:
    create_fake_model_files()
    test_state.generation_queue.stop()
    monkeypatch.setenv("LTX_MCP_PORT", "0")
    assert client.post("/api/settings", json={"mcpEnabled": True}).status_code == 200
    url, token = _wait_mcp_ready(client)

    async def _run() -> None:
        async with _mcp_session(url, token) as session:
            args = {
                "prompt": "A golden retriever running on a beach",
                "resolution": "270p",
                "duration": 2,
                "fps": 24,
                "aspect_ratio": "16:9",
                "idempotency_key": "shot-1-key",
            }
            first = await session.call_tool("generate_video", arguments=args)
            second = await session.call_tool("generate_video", arguments=args)
            assert not first.is_error
            assert not second.is_error
            first_data = _parse_tool_json(first)
            second_data = _parse_tool_json(second)
            assert first_data["id"] == second_data["id"]

    asyncio.run(_run())


def test_mcp_generate_video_idempotency_key_rejects_mismatched_params(
    test_state, client, create_fake_model_files, monkeypatch
) -> None:
    create_fake_model_files()
    test_state.generation_queue.stop()
    monkeypatch.setenv("LTX_MCP_PORT", "0")
    assert client.post("/api/settings", json={"mcpEnabled": True}).status_code == 200
    url, token = _wait_mcp_ready(client)

    async def _run() -> None:
        async with _mcp_session(url, token) as session:
            base = {
                "prompt": "Prompt A",
                "resolution": "270p",
                "duration": 2,
                "fps": 24,
                "aspect_ratio": "16:9",
                "idempotency_key": "shared-key",
            }
            first = await session.call_tool("generate_video", arguments=base)
            assert not first.is_error

            conflict = await session.call_tool(
                "generate_video",
                arguments={**base, "prompt": "Prompt B"},
            )
            assert conflict.is_error is True
            assert "IDEMPOTENCY_CONFLICT" in conflict.content[0].text

    asyncio.run(_run())


def test_mcp_wait_for_generation_streams_progress_and_returns_outputs(
    client, create_fake_model_files, monkeypatch
) -> None:
    create_fake_model_files()
    monkeypatch.setenv("LTX_MCP_PORT", "0")
    assert client.post("/api/settings", json={"mcpEnabled": True}).status_code == 200
    url, token = _wait_mcp_ready(client)

    progress_events: list[tuple[float, float | None, str | None]] = []

    async def _on_progress(progress: float, total: float | None, message: str | None) -> None:
        progress_events.append((progress, total, message))

    async def _run() -> None:
        async with _mcp_session(url, token) as session:
            created = await session.call_tool(
                "generate_video",
                arguments={
                    "prompt": "City skyline at dusk",
                    "resolution": "270p",
                    "duration": 2,
                    "fps": 24,
                    "aspect_ratio": "16:9",
                },
            )
            assert not created.is_error
            gen_id = _parse_tool_json(created)["id"]

            waited = await session.call_tool(
                "wait_for_generation",
                arguments={"generation_id": gen_id, "timeout_seconds": 15.0},
                progress_callback=_on_progress,
            )
            assert not waited.is_error
            final = _parse_tool_json(waited)
            assert final["status"] == "succeeded"
            assert len(final["outputs"]) == 1
            assert any(p == 100.0 for p, _, _ in progress_events)

    asyncio.run(_run())


def test_mcp_wait_for_generation_timeout_does_not_cancel_running_job(
    test_state, client, create_fake_model_files, monkeypatch
) -> None:
    create_fake_model_files()
    # Keep job queued so waiting times out without completing, then confirm job stays queued.
    test_state.generation_queue.stop()
    monkeypatch.setenv("LTX_MCP_PORT", "0")
    assert client.post("/api/settings", json={"mcpEnabled": True}).status_code == 200
    url, token = _wait_mcp_ready(client)

    async def _run() -> None:
        async with _mcp_session(url, token) as session:
            created = await session.call_tool(
                "generate_video",
                arguments={
                    "prompt": "Ocean storm",
                    "resolution": "270p",
                    "duration": 2,
                    "fps": 24,
                    "aspect_ratio": "16:9",
                },
            )
            gen_id = _parse_tool_json(created)["id"]

            waited = await session.call_tool(
                "wait_for_generation",
                arguments={"generation_id": gen_id, "timeout_seconds": 1.0},
            )
            assert not waited.is_error
            payload = _parse_tool_json(waited)
            assert payload["status"] == "queued"
            assert "Timed out" in (payload.get("note") or "")

            check = await session.call_tool("get_generation", arguments={"generation_id": gen_id})
            assert _parse_tool_json(check)["status"] == "queued"

    asyncio.run(_run())


def test_mcp_view_asset_returns_bounded_jpeg_image_content(
    test_state, client, tmp_path: Path, monkeypatch
) -> None:
    img_path = tmp_path / "sample.png"
    Image.new("RGB", (640, 360), color=(30, 120, 200)).save(img_path)
    asset = test_state.assets.ingest_asset(IngestAssetRequest(path=str(img_path)))

    monkeypatch.setenv("LTX_MCP_PORT", "0")
    assert client.post("/api/settings", json={"mcpEnabled": True}).status_code == 200
    url, token = _wait_mcp_ready(client)

    async def _run() -> None:
        async with _mcp_session(url, token) as session:
            res = await session.call_tool("view_asset", arguments={"asset_id": asset.id})
            assert not res.is_error
            image_parts = [part for part in res.content if isinstance(part, ImageContent)]
            assert len(image_parts) == 1
            assert image_parts[0].mime_type == "image/jpeg"
            assert len(image_parts[0].data) > 0

            missing = await session.call_tool("view_asset", arguments={"asset_id": "nonexistent-asset"})
            assert missing.is_error is True

    asyncio.run(_run())


def test_mcp_import_asset_rejects_non_media_file_and_missing_path(
    client, tmp_path: Path, monkeypatch
) -> None:
    txt_file = tmp_path / "notes.txt"
    txt_file.write_text("not an image", encoding="utf-8")

    monkeypatch.setenv("LTX_MCP_PORT", "0")
    assert client.post("/api/settings", json={"mcpEnabled": True}).status_code == 200
    url, token = _wait_mcp_ready(client)

    async def _run() -> None:
        async with _mcp_session(url, token) as session:
            # Opt-in gate off by default
            gated = await session.call_tool("import_asset", arguments={"path": str(txt_file)})
            assert gated.is_error is True
            assert "LOCAL_IMPORT_DISABLED" in gated.content[0].text

            # Enable opt-in gate, verify non-media and missing files are still rejected
            assert client.post("/api/settings", json={"mcpAllowLocalImport": True}).status_code == 200

            bad_media = await session.call_tool("import_asset", arguments={"path": str(txt_file)})
            assert bad_media.is_error is True

            missing = await session.call_tool(
                "import_asset", arguments={"path": str(tmp_path / "does-not-exist.png")}
            )
            assert missing.is_error is True
            assert "FILE_NOT_FOUND" in missing.content[0].text

    asyncio.run(_run())


def test_mcp_manage_generation_cancel_and_retry_state_transitions(
    test_state, client, create_fake_model_files, monkeypatch
) -> None:
    create_fake_model_files()
    test_state.generation_queue.stop()
    monkeypatch.setenv("LTX_MCP_PORT", "0")
    assert client.post("/api/settings", json={"mcpEnabled": True}).status_code == 200
    url, token = _wait_mcp_ready(client)

    async def _run() -> None:
        async with _mcp_session(url, token) as session:
            created = await session.call_tool(
                "generate_video",
                arguments={
                    "prompt": "Mountain sunrise",
                    "resolution": "270p",
                    "duration": 2,
                    "fps": 24,
                    "aspect_ratio": "16:9",
                },
            )
            gen_id = _parse_tool_json(created)["id"]

            cancelled = await session.call_tool(
                "manage_generation",
                arguments={"generation_id": gen_id, "action": "cancel"},
            )
            assert not cancelled.is_error
            assert _parse_tool_json(cancelled)["status"] == "cancelled"

            retry_cancelled = await session.call_tool(
                "manage_generation",
                arguments={"generation_id": gen_id, "action": "retry"},
            )
            assert retry_cancelled.is_error is True

    asyncio.run(_run())


def test_mcp_resources_and_prompts_readable_via_protocol(
    test_state, client, tmp_path: Path, monkeypatch
) -> None:
    img_path = tmp_path / "thumb.png"
    Image.new("RGB", (64, 64), color=(255, 0, 0)).save(img_path)
    asset = test_state.assets.ingest_asset(IngestAssetRequest(path=str(img_path)))

    monkeypatch.setenv("LTX_MCP_PORT", "0")
    assert client.post("/api/settings", json={"mcpEnabled": True}).status_code == 200
    url, token = _wait_mcp_ready(client)

    async def _run() -> None:
        async with _mcp_session(url, token) as session:
            resources = await session.list_resources()
            uris = {str(item.uri) for item in resources.resources}
            assert {"ltx://status", "ltx://models", "ltx://recipes", "ltx://queue"}.issubset(uris)

            status_res = await session.read_resource("ltx://status")
            assert json.loads(status_res.contents[0].text)["status"] == "ok"

            queue_res = await session.read_resource("ltx://queue")
            assert "queued" in json.loads(queue_res.contents[0].text)

            asset_res = await session.read_resource(f"ltx://assets/{asset.id}")
            assert json.loads(asset_res.contents[0].text)["id"] == asset.id

            preview_res = await session.read_resource(f"ltx://assets/{asset.id}/preview")
            assert preview_res.contents[0].mime_type == "image/jpeg"

            prompts_res = await session.list_prompts()
            prompt_names = {p.name for p in prompts_res.prompts}
            assert {
                "storyboard_to_video",
                "restyle_video",
                "consistent_character_video",
                "refine_and_extend_clip",
            }.issubset(prompt_names)

            prompt_body = await session.get_prompt(
                "storyboard_to_video", arguments={"concept": "A fox exploring a snowy forest"}
            )
            assert "generate_video" in prompt_body.messages[0].content.text

    asyncio.run(_run())
