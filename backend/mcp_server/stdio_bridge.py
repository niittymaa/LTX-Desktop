"""Lightweight stdio-to-Streamable-HTTP MCP bridge for LTX Desktop.

Intentionally avoids importing torch, diffusers, or AppHandler so legacy stdio
MCP hosts can spawn it instantaneously and share the single running LTX Desktop
instance over loopback Streamable HTTP.
"""

from __future__ import annotations

import asyncio
import json
import os
import sys
from contextlib import AsyncExitStack, suppress
from pathlib import Path
from typing import Any, cast

import httpx2
import mcp.types as types
from mcp.client.session import ClientSession
from mcp.client.streamable_http import streamable_http_client
from mcp.server.context import ServerRequestContext
from mcp.server.lowlevel.server import NotificationOptions, Server
from mcp.server.stdio import stdio_server

from mcp_server.net import ENV_MCP_TOKEN, ENV_MCP_URL, mcp_endpoint_url, mcp_port

_TOKEN_FILENAME = "mcp-token.json"
_APP_DIR_NAME = "LTXDesktop"


def _candidate_token_paths() -> list[Path]:
    paths: list[Path] = []
    env_dir = os.environ.get("LTX_APP_DATA_DIR", "").strip()
    if env_dir:
        paths.append(Path(env_dir) / _TOKEN_FILENAME)
    home = Path.home()
    if sys.platform == "darwin":
        paths.append(home / "Library" / "Application Support" / _APP_DIR_NAME / _TOKEN_FILENAME)
    elif sys.platform == "win32":
        appdata = os.environ.get("APPDATA", "").strip()
        if appdata:
            paths.append(Path(appdata) / _APP_DIR_NAME / _TOKEN_FILENAME)
    else:
        xdg = os.environ.get("XDG_CONFIG_HOME", "").strip()
        base = Path(xdg) if xdg else home / ".config"
        paths.append(base / _APP_DIR_NAME / _TOKEN_FILENAME)
    return paths


def resolve_mcp_url() -> str:
    env_url = os.environ.get(ENV_MCP_URL, "").strip()
    if env_url:
        return env_url
    return mcp_endpoint_url(mcp_port())


def resolve_mcp_token() -> str | None:
    env_token = os.environ.get(ENV_MCP_TOKEN, "").strip()
    if env_token:
        return env_token
    for candidate in _candidate_token_paths():
        if not candidate.is_file():
            continue
        try:
            raw = json.loads(candidate.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            continue
        if isinstance(raw, dict):
            data = cast(dict[str, object], raw)
            token = data.get("token")
            if isinstance(token, str) and token.strip():
                return token.strip()
    return None


def _describe_error(exc: BaseException) -> str:
    if isinstance(exc, BaseExceptionGroup):
        group = cast(BaseExceptionGroup[BaseException], exc)
        parts = [_describe_error(sub) for sub in group.exceptions]
        unique = [part for i, part in enumerate(parts) if part and part not in parts[:i]]
        return "; ".join(unique) or type(group).__name__
    text = str(exc).strip()
    return text or type(exc).__name__


def _connect_error_message(url: str, reason: str) -> str:
    return (
        f"Cannot reach LTX Desktop MCP server at {url} ({reason}). "
        "Ensure LTX Desktop is running and 'Enable MCP Server' is turned on in Settings."
    )


class _SessionOwner:
    """Owns the StreamableHTTP + ClientSession anyio context inside one dedicated task."""

    def __init__(self, url: str, token: str) -> None:
        self._url = url
        self._token = token
        self._ready = asyncio.Event()
        self._stop = asyncio.Event()
        self._session: ClientSession | None = None
        self._error: Exception | None = None
        self._task = asyncio.create_task(self._run())

    async def _run(self) -> None:
        try:
            async with AsyncExitStack() as stack:
                http_client = httpx2.AsyncClient(
                    headers={"Authorization": f"Bearer {self._token}"},
                    timeout=httpx2.Timeout(30.0, read=None),
                    follow_redirects=True,
                )
                await stack.enter_async_context(http_client)
                read_stream, write_stream = await stack.enter_async_context(
                    streamable_http_client(self._url, http_client=http_client)
                )
                session = await stack.enter_async_context(ClientSession(read_stream, write_stream))
                await session.initialize()
                self._session = session
                self._ready.set()
                await self._stop.wait()
        except BaseException as exc:
            if self._stop.is_set() and isinstance(exc, asyncio.CancelledError):
                return
            self._error = (
                exc
                if isinstance(exc, Exception)
                else ConnectionError(f"Failed to connect to {self._url}: {exc}")
            )
        finally:
            self._session = None
            self._ready.set()

    async def wait_ready(self) -> ClientSession:
        await self._ready.wait()
        if self._error is not None:
            raise self._error
        assert self._session is not None
        return self._session

    async def close(self) -> None:
        self._stop.set()
        with suppress(BaseException):
            await self._task


class _UpstreamBridgeClient:
    """Lazy, reconnect-capable Streamable HTTP client session to LTX Desktop."""

    def __init__(self, url: str, token: str | None) -> None:
        self._url = url
        self._static_token = token
        self._lock = asyncio.Lock()
        self._owner: _SessionOwner | None = None

    def _current_token(self) -> str:
        token = self._static_token or resolve_mcp_token()
        if not token:
            raise RuntimeError(
                "No MCP Bearer token found. Start LTX Desktop with 'Enable MCP Server' turned on, "
                f"or set {ENV_MCP_TOKEN}."
            )
        return token

    async def get_session(self) -> ClientSession:
        async with self._lock:
            if self._owner is None:
                owner = _SessionOwner(self._url, self._current_token())
                self._owner = owner
                try:
                    return await owner.wait_ready()
                except BaseException:
                    self._owner = None
                    await owner.close()
                    raise
            return await self._owner.wait_ready()

    async def reset(self) -> None:
        async with self._lock:
            owner = self._owner
            self._owner = None
        if owner is not None:
            await owner.close()

    async def close(self) -> None:
        await self.reset()


def _format_tool_error(message: str) -> types.CallToolResult:
    return types.CallToolResult(
        is_error=True,
        content=[types.TextContent(type="text", text=json.dumps({"code": "MCP_UNREACHABLE", "message": message}))],
    )


async def run_stdio_bridge(
    *,
    url: str | None = None,
    token: str | None = None,
    read_stream: Any = None,
    write_stream: Any = None,
) -> None:
    target_url = url or resolve_mcp_url()
    upstream = _UpstreamBridgeClient(target_url, token)

    async def on_list_tools(
        _ctx: ServerRequestContext[Any],
        params: types.PaginatedRequestParams | None,
    ) -> types.ListToolsResult:
        try:
            session = await upstream.get_session()
            return await session.list_tools(params=params)
        except Exception:
            await upstream.reset()
            raise

    async def on_call_tool(
        ctx: ServerRequestContext[Any],
        params: types.CallToolRequestParams,
    ) -> types.CallToolResult | types.InputRequiredResult:
        progress_token = ctx.meta.get("progress_token") if ctx.meta else None
        if progress_token is None and ctx.meta:
            progress_token = cast(str | int | None, ctx.meta.get("progressToken"))

        async def _forward_progress(progress: float, total: float | None, message: str | None) -> None:
            if progress_token is None:
                return
            await ctx.session.send_progress_notification(
                progress_token=progress_token,
                progress=progress,
                total=total,
                message=message,
                related_request_id=str(ctx.request_id) if ctx.request_id is not None else None,
            )

        try:
            session = await upstream.get_session()
            return await session.call_tool(
                params.name,
                arguments=params.arguments,
                progress_callback=_forward_progress if progress_token is not None else None,
            )
        except Exception as exc:
            await upstream.reset()
            return _format_tool_error(_connect_error_message(target_url, _describe_error(exc)))

    async def on_list_resources(
        _ctx: ServerRequestContext[Any],
        params: types.PaginatedRequestParams | None,
    ) -> types.ListResourcesResult:
        try:
            session = await upstream.get_session()
            return await session.list_resources(params=params)
        except Exception:
            await upstream.reset()
            raise

    async def on_list_resource_templates(
        _ctx: ServerRequestContext[Any],
        params: types.PaginatedRequestParams | None,
    ) -> types.ListResourceTemplatesResult:
        try:
            session = await upstream.get_session()
            return await session.list_resource_templates(params=params)
        except Exception:
            await upstream.reset()
            raise

    async def on_read_resource(
        _ctx: ServerRequestContext[Any],
        params: types.ReadResourceRequestParams,
    ) -> types.ReadResourceResult | types.InputRequiredResult:
        try:
            session = await upstream.get_session()
            return await session.read_resource(params.uri)
        except Exception:
            await upstream.reset()
            raise

    async def on_list_prompts(
        _ctx: ServerRequestContext[Any],
        params: types.PaginatedRequestParams | None,
    ) -> types.ListPromptsResult:
        try:
            session = await upstream.get_session()
            return await session.list_prompts(params=params)
        except Exception:
            await upstream.reset()
            raise

    async def on_get_prompt(
        _ctx: ServerRequestContext[Any],
        params: types.GetPromptRequestParams,
    ) -> types.GetPromptResult | types.InputRequiredResult:
        try:
            session = await upstream.get_session()
            return await session.get_prompt(params.name, arguments=params.arguments)
        except Exception:
            await upstream.reset()
            raise

    server: Server[Any] = Server(
        "LTX Desktop",
        version="1.0.0",
        on_list_tools=on_list_tools,
        on_call_tool=on_call_tool,
        on_list_resources=on_list_resources,
        on_list_resource_templates=on_list_resource_templates,
        on_read_resource=on_read_resource,
        on_list_prompts=on_list_prompts,
        on_get_prompt=on_get_prompt,
    )
    init_options = server.create_initialization_options(notification_options=NotificationOptions())

    try:
        if read_stream is not None and write_stream is not None:
            await server.run(read_stream, write_stream, init_options)
        else:
            async with stdio_server() as (stdin_stream, stdout_stream):
                await server.run(stdin_stream, stdout_stream, init_options)
    finally:
        await upstream.close()


def main() -> None:
    try:
        asyncio.run(run_stdio_bridge())
    except KeyboardInterrupt:
        pass
    except Exception as exc:
        print(f"[ltx-desktop-mcp-bridge] {exc}", file=sys.stderr)
        raise SystemExit(1) from exc


if __name__ == "__main__":
    main()
