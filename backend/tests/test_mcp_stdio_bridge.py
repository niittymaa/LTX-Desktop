"""Tests for the thin stdio-to-Streamable-HTTP MCP bridge."""

from __future__ import annotations

import asyncio
import json
import time

import anyio
from mcp.client.session import ClientSession
from mcp.shared.message import SessionMessage
from starlette.testclient import TestClient

from mcp_server.stdio_bridge import run_stdio_bridge


def _wait_mcp_ready(client: TestClient, *, timeout: float = 8.0) -> tuple[str, str]:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        status = client.get("/api/mcp/status").json()
        if status.get("serving") is True and status.get("url"):
            cfg = client.get("/api/mcp/client-config").json()
            return str(status["url"]), str(cfg["token"])
        time.sleep(0.05)
    raise AssertionError("MCP server did not start in time")


def test_stdio_bridge_forwards_tool_call_and_auth_to_streamable_http(client, monkeypatch) -> None:
    monkeypatch.setenv("LTX_MCP_PORT", "0")
    assert client.post("/api/settings", json={"mcpEnabled": True}).status_code == 200
    url, token = _wait_mcp_ready(client)

    async def _run() -> None:
        c2s_send, c2s_recv = anyio.create_memory_object_stream[SessionMessage | Exception](16)
        s2c_send, s2c_recv = anyio.create_memory_object_stream[SessionMessage](16)

        async with anyio.create_task_group() as tg:
            tg.start_soon(
                lambda: run_stdio_bridge(
                    url=url,
                    token=token,
                    read_stream=c2s_recv,
                    write_stream=s2c_send,
                )
            )
            async with ClientSession(s2c_recv, c2s_send) as session:
                await session.initialize()
                tools_res = await session.list_tools()
                assert any(tool.name == "get_system_status" for tool in tools_res.tools)

                status_res = await session.call_tool("get_system_status")
                assert not status_res.is_error
                payload = json.loads(status_res.content[0].text)
                assert payload["status"] == "ok"

            await c2s_send.aclose()
            await s2c_send.aclose()
            tg.cancel_scope.cancel()

    asyncio.run(_run())


def test_stdio_bridge_reports_clear_error_when_server_unreachable() -> None:
    async def _run() -> None:
        c2s_send, c2s_recv = anyio.create_memory_object_stream[SessionMessage | Exception](16)
        s2c_send, s2c_recv = anyio.create_memory_object_stream[SessionMessage](16)

        async with anyio.create_task_group() as tg:
            tg.start_soon(
                lambda: run_stdio_bridge(
                    url="http://127.0.0.1:1/mcp",
                    token="ltxm_unreachable_test_token",
                    read_stream=c2s_recv,
                    write_stream=s2c_send,
                )
            )
            async with ClientSession(s2c_recv, c2s_send) as session:
                await session.initialize()
                res = await session.call_tool("get_system_status")
                assert res.is_error is True
                payload = json.loads(res.content[0].text)
                assert payload["code"] == "MCP_UNREACHABLE"
                assert "Enable MCP Server" in payload["message"]

            await c2s_send.aclose()
            await s2c_send.aclose()
            tg.cancel_scope.cancel()

    asyncio.run(_run())
