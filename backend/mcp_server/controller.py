"""Start/stop the loopback Streamable HTTP MCP server when Settings mcp_enabled is on."""

from __future__ import annotations

import logging
import sys
import threading
import time
from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING, Any

from logging_policy import log_background_exception
from mcp_server.auth import McpTokenStore
from mcp_server.http_app import create_mcp_http_app
from mcp_server.net import MCP_BIND_HOST, bound_server_port, mcp_endpoint_url, mcp_port
from mcp_server.status import McpClientConfigResponse, McpStatusResponse, McpTokenResponse

if TYPE_CHECKING:
    from fastapi import FastAPI
    import uvicorn

    from app_handler import AppHandler

logger = logging.getLogger(__name__)

_BIND_WAIT_S = 8.0
_BIND_POLL_S = 0.05
_JOIN_TIMEOUT_S = 5.0


@dataclass(frozen=True)
class _Running:
    server: uvicorn.Server
    thread: threading.Thread
    port: int


@dataclass(frozen=True)
class _Stopping:
    server: uvicorn.Server
    thread: threading.Thread


class McpServerController:
    """Lifecycle and control-plane manager for the local MCP Streamable HTTP server."""

    def __init__(self, handler: AppHandler) -> None:
        self._handler = handler
        self._lock = threading.Lock()
        self._ownership: _Running | _Stopping | None = None
        self._enabled: bool = False
        self._last_reason: str = "disabled in Settings"
        self._token_store = McpTokenStore(handler.config.app_data_dir)

    def status(self) -> McpStatusResponse:
        settings = self._handler.settings.get_settings_snapshot()
        token_configured = self._token_store.exists()
        with self._lock:
            self._reap_dead_locked()
            serving = False
            port: int | None = None
            url: str | None = None
            match self._ownership:
                case _Running(server=server, port=configured_port) if server.started:
                    serving = True
                    port = bound_server_port(server, configured_port)
                    url = mcp_endpoint_url(port)
                case _:
                    pass
            reason = None if serving else self._last_reason
        return McpStatusResponse(
            enabled=settings.mcp_enabled,
            allowLocalImport=settings.mcp_allow_local_import,
            serving=serving,
            url=url,
            port=port,
            reason=reason,
            tokenConfigured=token_configured,
        )

    def rotate_token(self) -> McpTokenResponse:
        record = self._token_store.rotate()
        return McpTokenResponse(token=record.token, createdAt=record.created_at)

    def client_config(self) -> McpClientConfigResponse:
        record = self._token_store.get_or_create()
        current_status = self.status()
        port = current_status.port if current_status.port is not None else mcp_port()
        url = current_status.url or mcp_endpoint_url(port)
        backend_dir = str(Path(__file__).resolve().parents[1])
        streamable_http_config: dict[str, Any] = {
            "mcpServers": {
                "ltx-desktop": {
                    "url": url,
                    "headers": {
                        "Authorization": f"Bearer {record.token}",
                    },
                }
            }
        }
        stdio_config: dict[str, Any] = {
            "mcpServers": {
                "ltx-desktop": {
                    "command": sys.executable,
                    "args": ["-B", "-m", "mcp_server.stdio_bridge"],
                    "env": {
                        "PYTHONPATH": backend_dir,
                        "PYTHONDONTWRITEBYTECODE": "1",
                    },
                }
            }
        }
        return McpClientConfigResponse(
            url=url,
            token=record.token,
            streamableHttpConfig=streamable_http_config,
            stdioConfig=stdio_config,
        )

    def sync(self) -> None:
        enabled = self._handler.settings.get_settings_snapshot().mcp_enabled
        stop_target: _Stopping | None = None
        with self._lock:
            self._reap_dead_locked()
            if isinstance(self._ownership, _Stopping):
                if self._ownership.thread.is_alive():
                    return
                self._finish_stop_locked(self._ownership, reason=self._last_reason)
            running = isinstance(self._ownership, _Running)
            if enabled and not running:
                self._start_locked()
            elif not enabled and running:
                stop_target = self._begin_stop_locked()
            elif not enabled:
                self._enabled = False
                self._last_reason = "disabled in Settings"
        if stop_target is None:
            return
        self._join_stop(stop_target)
        with self._lock:
            self._finish_stop_locked(stop_target)
            self._restart_if_desired_locked()

    def stop(self) -> None:
        with self._lock:
            stop_target = self._begin_stop_locked()
        if stop_target is None:
            return
        self._join_stop(stop_target)
        with self._lock:
            self._finish_stop_locked(stop_target)

    def _reap_dead_locked(self) -> None:
        match self._ownership:
            case _Running(thread=thread) if not thread.is_alive():
                self._last_reason = "MCP server stopped unexpectedly"
                logger.error("MCP server thread exited")
                self._clear_locked()
            case _:
                return

    def _start_locked(self) -> None:
        import uvicorn

        if self._ownership is not None:
            return
        settings = self._handler.settings.get_settings_snapshot()
        if not settings.mcp_enabled:
            self._enabled = False
            self._last_reason = "disabled in Settings"
            return

        self._token_store.get_or_create()
        port = mcp_port()
        app = create_mcp_http_app(
            handler=self._handler,
            token_store=self._token_store,
        )
        log_config: dict[str, object] = {
            "version": 1,
            "disable_existing_loggers": False,
            "handlers": {
                "default": {
                    "class": "logging.StreamHandler",
                    "stream": "ext://sys.stdout",
                },
            },
            "loggers": {
                "uvicorn": {"handlers": ["default"], "level": "INFO"},
                "uvicorn.error": {"handlers": ["default"], "level": "INFO", "propagate": False},
                "uvicorn.access": {"handlers": ["default"], "level": "INFO", "propagate": False},
            },
        }
        config = uvicorn.Config(
            app,
            host=MCP_BIND_HOST,
            port=port,
            log_level="info",
            access_log=False,
            log_config=log_config,
            timeout_graceful_shutdown=2,
        )
        server = uvicorn.Server(config)
        thread = threading.Thread(
            target=self._run_server,
            args=(server,),
            name="ltx-mcp",
            daemon=True,
        )
        self._ownership = _Running(server=server, thread=thread, port=port)
        self._enabled = True
        self._last_reason = "starting"
        thread.start()
        threading.Thread(
            target=self._await_bind,
            args=(server, thread, port),
            name="ltx-mcp-bind",
            daemon=True,
        ).start()

    def _await_bind(self, server: uvicorn.Server, thread: threading.Thread, port: int) -> None:
        deadline = time.monotonic() + _BIND_WAIT_S
        while time.monotonic() < deadline:
            if server.started:
                with self._lock:
                    owned = self._ownership
                    if not isinstance(owned, _Running) or owned.server is not server:
                        return
                    bound = bound_server_port(server, owned.port)
                    if bound != owned.port:
                        self._ownership = _Running(
                            server=owned.server,
                            thread=owned.thread,
                            port=bound,
                        )
                logger.info("MCP Streamable HTTP listening on %s", mcp_endpoint_url(bound))
                return
            if not thread.is_alive():
                self._fail_start(
                    server,
                    reason="MCP server failed to start",
                    log_msg="MCP server thread exited before bind",
                )
                return
            time.sleep(_BIND_POLL_S)
        self._fail_start(
            server,
            reason="MCP server timed out waiting to bind",
            log_msg="MCP server bind timed out after %.1fs",
            log_args=(_BIND_WAIT_S,),
        )

    def _fail_start(
        self,
        server: uvicorn.Server,
        *,
        reason: str,
        log_msg: str,
        log_args: tuple[object, ...] = (),
    ) -> None:
        with self._lock:
            owned = self._ownership
            if not isinstance(owned, _Running) or owned.server is not server:
                return
            self._last_reason = reason
            self._clear_locked()
        server.should_exit = True
        logger.error(log_msg, *log_args)

    def _run_server(self, server: uvicorn.Server) -> None:
        try:
            server.run()
        except Exception as exc:
            log_background_exception("mcp-server", exc)
        finally:
            with self._lock:
                owned = self._ownership
                if isinstance(owned, _Running) and owned.server is server:
                    self._last_reason = "MCP server exited"
                    self._clear_locked()

    def _begin_stop_locked(self) -> _Stopping | None:
        match self._ownership:
            case _Running(server=server, thread=thread):
                server.should_exit = True
                stopping = _Stopping(server=server, thread=thread)
                self._ownership = stopping
                return stopping
            case _Stopping() as stopping:
                return stopping
            case None:
                self._enabled = False
                self._last_reason = "disabled in Settings"
                return None

    def _join_stop(self, stopping: _Stopping) -> None:
        stopping.thread.join(timeout=_JOIN_TIMEOUT_S)
        if stopping.thread.is_alive():
            logger.warning("MCP server thread did not exit within %.1fs", _JOIN_TIMEOUT_S)

    def _finish_stop_locked(
        self,
        stopping: _Stopping,
        *,
        reason: str = "disabled in Settings",
    ) -> None:
        if self._ownership is stopping:
            self._clear_locked()
            self._last_reason = reason

    def _restart_if_desired_locked(self) -> None:
        if self._ownership is not None:
            return
        if self._handler.settings.get_settings_snapshot().mcp_enabled:
            self._start_locked()

    def _clear_locked(self) -> None:
        self._ownership = None
        self._enabled = False


def mcp_controller_from_app(app: FastAPI) -> McpServerController | None:
    controller = getattr(app.state, "mcp_controller", None)
    return controller if isinstance(controller, McpServerController) else None
