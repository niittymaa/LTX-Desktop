"""Lifecycle and isolation tests for McpServerController."""

from __future__ import annotations

import time
from urllib.parse import urlsplit

import httpx
from starlette.testclient import TestClient

from app_factory import create_app
from mcp_server.controller import mcp_controller_from_app
from remote.app import create_remote_app


def _wait_mcp_serving(client: TestClient, *, expected: bool, timeout: float = 8.0) -> dict:
    deadline = time.monotonic() + timeout
    payload: dict | None = None
    while time.monotonic() < deadline:
        payload = client.get("/api/mcp/status").json()
        if payload.get("serving") is expected:
            return payload
        time.sleep(0.05)
    raise AssertionError(f"MCP serving state did not reach {expected}: {payload}")


def test_mcp_disabled_by_default_does_not_bind_port(client, monkeypatch) -> None:
    monkeypatch.setenv("LTX_MCP_PORT", "0")
    status = client.get("/api/mcp/status").json()
    assert status["enabled"] is False
    assert status["allowLocalImport"] is False
    assert status["serving"] is False
    assert status["url"] is None


def test_toggling_mcp_enabled_setting_starts_and_stops_server(client, monkeypatch) -> None:
    monkeypatch.setenv("LTX_MCP_PORT", "0")

    resp = client.post("/api/settings", json={"mcpEnabled": True})
    assert resp.status_code == 200
    serving_status = _wait_mcp_serving(client, expected=True)
    assert serving_status["enabled"] is True
    assert serving_status["url"] is not None

    health = httpx.get(serving_status["url"].replace("/mcp", "/health"), timeout=2.0)
    assert health.status_code == 200
    assert health.json()["status"] == "ok"

    resp_off = client.post("/api/settings", json={"mcpEnabled": False})
    assert resp_off.status_code == 200
    stopped_status = _wait_mcp_serving(client, expected=False)
    assert stopped_status["enabled"] is False
    assert stopped_status["serving"] is False


def test_mcp_status_reports_bound_ephemeral_port_and_loopback_only_url(client, monkeypatch) -> None:
    monkeypatch.setenv("LTX_MCP_PORT", "0")
    assert client.post("/api/settings", json={"mcpEnabled": True}).status_code == 200
    status = _wait_mcp_serving(client, expected=True)

    parsed = urlsplit(status["url"])
    assert parsed.scheme == "http"
    assert parsed.hostname == "127.0.0.1"
    assert parsed.port is not None and parsed.port > 0
    assert parsed.path == "/mcp"
    assert status["port"] == parsed.port

    cfg = client.get("/api/mcp/client-config").json()
    assert cfg["url"] == status["url"]
    assert cfg["token"].startswith("ltxm_")
    assert cfg["streamableHttpConfig"]["mcpServers"]["ltx-desktop"]["url"] == status["url"]


def test_mcp_controller_stops_cleanly_on_app_lifespan_exit(test_state, monkeypatch) -> None:
    monkeypatch.setenv("LTX_MCP_PORT", "0")
    test_state.state.app_settings.mcp_enabled = True
    app = create_app(handler=test_state)
    health_url: str
    with TestClient(app) as local_client:
        status = _wait_mcp_serving(local_client, expected=True)
        health_url = status["url"].replace("/mcp", "/health")
        assert httpx.get(health_url, timeout=2.0).status_code == 200
        controller = mcp_controller_from_app(app)
        assert controller is not None

    assert controller.status().serving is False
    try:
        httpx.get(health_url, timeout=0.5)
    except httpx.HTTPError:
        pass
    else:
        raise AssertionError("MCP HTTP endpoint still accepted connections after lifespan exit")


def test_mcp_routes_not_exposed_on_remote_lan_server(test_state) -> None:
    remote_app = create_remote_app(handler=test_state, remote_token="remote-pair-token")
    with TestClient(remote_app) as remote_client:
        headers = {"Authorization": "Bearer remote-pair-token"}
        assert remote_client.get("/api/mcp/status", headers=headers).status_code == 404
        assert remote_client.get("/api/mcp/client-config", headers=headers).status_code == 404
        assert remote_client.post("/api/mcp/token/rotate", headers=headers).status_code == 404
