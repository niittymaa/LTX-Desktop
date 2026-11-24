"""Security and authentication tests for the LTX Desktop loopback MCP server."""

from __future__ import annotations

import os
import stat
from pathlib import Path

import pytest
from starlette.testclient import TestClient

from mcp_server.auth import McpTokenStore
from mcp_server.http_app import create_mcp_http_app


def _mcp_init_payload() -> dict:
    return {
        "jsonrpc": "2.0",
        "id": 1,
        "method": "initialize",
        "params": {
            "protocolVersion": "2025-03-26",
            "capabilities": {},
            "clientInfo": {"name": "pytest", "version": "1.0"},
        },
    }


def _mcp_headers(token: str) -> dict[str, str]:
    return {
        "Authorization": f"Bearer {token}",
        "Accept": "application/json, text/event-stream",
        "Content-Type": "application/json",
    }


def test_mcp_rejects_missing_bearer_token_with_401(test_state) -> None:
    store = McpTokenStore(test_state.config.app_data_dir)
    app = create_mcp_http_app(handler=test_state, token_store=store)
    with TestClient(app) as client:
        resp = client.post(
            "/mcp",
            json=_mcp_init_payload(),
            headers={"Accept": "application/json, text/event-stream"},
        )
        assert resp.status_code == 401
        assert resp.headers.get("www-authenticate") == "Bearer"


def test_mcp_rejects_wrong_bearer_token_with_401(test_state) -> None:
    store = McpTokenStore(test_state.config.app_data_dir)
    store.get_or_create()
    app = create_mcp_http_app(handler=test_state, token_store=store)
    with TestClient(app) as client:
        resp = client.post("/mcp", json=_mcp_init_payload(), headers=_mcp_headers("wrong-token"))
        assert resp.status_code == 401


def test_mcp_token_rotation_immediately_invalidates_previous_token(test_state) -> None:
    store = McpTokenStore(test_state.config.app_data_dir)
    old_token = store.get_or_create().token
    app = create_mcp_http_app(handler=test_state, token_store=store)
    with TestClient(app) as client:
        ok_resp = client.post("/mcp", json=_mcp_init_payload(), headers=_mcp_headers(old_token))
        assert ok_resp.status_code == 200

        new_token = store.rotate().token
        assert new_token != old_token

        stale_resp = client.post("/mcp", json=_mcp_init_payload(), headers=_mcp_headers(old_token))
        assert stale_resp.status_code == 401

        rotated_resp = client.post("/mcp", json=_mcp_init_payload(), headers=_mcp_headers(new_token))
        assert rotated_resp.status_code == 200


def test_mcp_rejects_browser_origin_header_with_403(test_state) -> None:
    store = McpTokenStore(test_state.config.app_data_dir)
    token = store.get_or_create().token
    app = create_mcp_http_app(handler=test_state, token_store=store)
    with TestClient(app) as client:
        headers = {**_mcp_headers(token), "Origin": "https://evil.example"}
        resp = client.post("/mcp", json=_mcp_init_payload(), headers=headers)
        assert resp.status_code == 403


def test_mcp_rejects_untrusted_host_header(test_state) -> None:
    store = McpTokenStore(test_state.config.app_data_dir)
    token = store.get_or_create().token
    app = create_mcp_http_app(handler=test_state, token_store=store)
    with TestClient(app) as client:
        headers = {**_mcp_headers(token), "Host": "attacker.example:41956"}
        resp = client.post("/mcp", json=_mcp_init_payload(), headers=headers)
        assert resp.status_code in (400, 403, 421)


@pytest.mark.skipif(os.name != "posix", reason="POSIX file mode bits apply on macOS/Linux")
def test_mcp_token_file_created_with_owner_only_permissions(test_state) -> None:
    store = McpTokenStore(test_state.config.app_data_dir)
    record = store.get_or_create()
    assert record.token.startswith("ltxm_")
    mode = stat.S_IMODE(Path(store.token_path).stat().st_mode)
    assert mode == 0o600


def test_mcp_never_leaks_api_keys_in_status_or_resources(test_state) -> None:
    from mcp_server import domain

    secret_ltx = "ltx-secret-value-999"
    secret_fal = "fal-secret-value-888"
    secret_gemini = "gemini-secret-value-777"
    test_state.state.app_settings.ltx_api_key = secret_ltx
    test_state.state.app_settings.fal_api_key = secret_fal
    test_state.state.app_settings.gemini_api_key = secret_gemini

    serialized = domain.get_system_status(test_state).model_dump_json()
    for secret in (secret_ltx, secret_fal, secret_gemini, "fake-hf-token"):
        assert secret not in serialized
