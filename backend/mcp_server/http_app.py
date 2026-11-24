"""Authenticated loopback Streamable HTTP ASGI app for LTX Desktop MCP."""

from __future__ import annotations

from collections.abc import AsyncIterator, Awaitable, Callable
from contextlib import asynccontextmanager
from typing import TYPE_CHECKING

from fastapi import FastAPI, Request
from mcp.server.transport_security import TransportSecuritySettings
from starlette.middleware.trustedhost import TrustedHostMiddleware
from starlette.responses import JSONResponse
from starlette.responses import Response as StarletteResponse

from _routes._errors import build_http_error_response
from mcp_server.auth import McpTokenStore
from mcp_server.net import MCP_BIND_HOST, MCP_PATH
from mcp_server.server import build_mcp_server
from security_headers import install_security_headers

if TYPE_CHECKING:
    from app_handler import AppHandler

_ALLOWED_TRUSTED_HOSTS = [
    "127.0.0.1",
    "localhost",
    "::1",
    "[::1]",
    "testserver",
]
_ALLOWED_TRANSPORT_HOSTS = [
    "127.0.0.1:*",
    "localhost:*",
    "[::1]:*",
    "testserver",
    "testserver:*",
]


def _extract_bearer(request: Request) -> str | None:
    header = request.headers.get("authorization", "")
    if header.startswith("Bearer "):
        token = header[7:].strip()
        return token or None
    return None


def _unauthorized() -> JSONResponse:
    return JSONResponse(
        status_code=401,
        content=build_http_error_response(401, "Unauthorized").model_dump(),
        headers={"WWW-Authenticate": "Bearer"},
    )


def _forbidden(detail: str) -> JSONResponse:
    return JSONResponse(
        status_code=403,
        content=build_http_error_response(403, detail).model_dump(),
    )


def create_mcp_http_app(
    *,
    handler: AppHandler,
    token_store: McpTokenStore,
) -> FastAPI:
    """Create the loopback-only Streamable HTTP ASGI application for MCP."""
    mcp = build_mcp_server(handler)
    mcp_asgi = mcp.streamable_http_app(
        streamable_http_path=MCP_PATH,
        host=MCP_BIND_HOST,
        transport_security=TransportSecuritySettings(
            enable_dns_rebinding_protection=True,
            allowed_hosts=_ALLOWED_TRANSPORT_HOSTS,
            allowed_origins=[],
        ),
    )

    @asynccontextmanager
    async def lifespan(app: FastAPI) -> AsyncIterator[None]:
        async with mcp_asgi.router.lifespan_context(app):
            yield

    app = FastAPI(
        title="LTX Desktop MCP",
        docs_url=None,
        redoc_url=None,
        openapi_url=None,
        lifespan=lifespan,
    )

    @app.get("/health")
    async def route_health() -> dict[str, str]:  # pyright: ignore[reportUnusedFunction]
        return {"status": "ok", "service": "ltx-desktop-mcp"}

    app.router.routes.extend(mcp_asgi.routes)
    app.add_middleware(TrustedHostMiddleware, allowed_hosts=_ALLOWED_TRUSTED_HOSTS)

    @app.middleware("http")
    async def _mcp_security_middleware(  # pyright: ignore[reportUnusedFunction]
        request: Request,
        call_next: Callable[[Request], Awaitable[StarletteResponse]],
    ) -> StarletteResponse:
        if request.url.path == "/health":
            return await call_next(request)
        # Reject browser cross-origin requests outright to prevent localhost CSRF / DNS-rebinding abuse.
        if "origin" in request.headers:
            return _forbidden("Browser Origin header is not permitted on local MCP endpoint")
        token = _extract_bearer(request)
        if token is None or not token_store.verify(token):
            return _unauthorized()
        return await call_next(request)

    install_security_headers(app)
    return app
