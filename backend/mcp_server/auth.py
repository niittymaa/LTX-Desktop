"""Persistent owner-only Bearer token store for the local MCP server."""

from __future__ import annotations

import hmac
import json
import os
import secrets
import threading
import time
from pathlib import Path
from typing import cast

from pydantic import BaseModel, ConfigDict

TOKEN_FILENAME = "mcp-token.json"
TOKEN_PREFIX = "ltxm_"


class McpTokenRecord(BaseModel):
    model_config = ConfigDict(frozen=True)

    token: str
    created_at: int


def _generate_token() -> str:
    return f"{TOKEN_PREFIX}{secrets.token_urlsafe(32)}"


def _now_ms() -> int:
    return int(time.time() * 1000)


class McpTokenStore:
    """Thread-safe, atomic 0600 store for the local MCP Bearer token."""

    def __init__(self, app_data_dir: Path) -> None:
        self._path = app_data_dir / TOKEN_FILENAME
        self._lock = threading.Lock()
        self._cached: McpTokenRecord | None = None

    @property
    def token_path(self) -> Path:
        return self._path

    def exists(self) -> bool:
        with self._lock:
            return self._cached is not None or self._path.is_file()

    def get_or_create(self) -> McpTokenRecord:
        with self._lock:
            if self._cached is not None:
                return self._cached
            loaded = self._read_from_disk_locked()
            if loaded is not None:
                self._cached = loaded
                return loaded
            created = McpTokenRecord(token=_generate_token(), created_at=_now_ms())
            self._write_atomic_locked(created)
            self._cached = created
            return created

    def rotate(self) -> McpTokenRecord:
        with self._lock:
            rotated = McpTokenRecord(token=_generate_token(), created_at=_now_ms())
            self._write_atomic_locked(rotated)
            self._cached = rotated
            return rotated

    def verify(self, candidate: str) -> bool:
        if not candidate:
            return False
        record = self.get_or_create()
        return hmac.compare_digest(candidate, record.token)

    def _read_from_disk_locked(self) -> McpTokenRecord | None:
        if not self._path.is_file():
            return None
        try:
            raw = json.loads(self._path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            return None
        if not isinstance(raw, dict):
            return None
        data = cast(dict[str, object], raw)
        token = data.get("token")
        created_at = data.get("created_at")
        if not isinstance(token, str) or not token.strip():
            return None
        ts = int(created_at) if isinstance(created_at, (int, float)) and not isinstance(created_at, bool) else _now_ms()
        return McpTokenRecord(token=token.strip(), created_at=ts)

    def _write_atomic_locked(self, record: McpTokenRecord) -> None:
        self._path.parent.mkdir(parents=True, exist_ok=True)
        tmp_path = self._path.with_name(f".{self._path.name}.{os.getpid()}.tmp")
        payload = json.dumps(record.model_dump(), indent=2) + "\n"
        fd = os.open(
            tmp_path,
            os.O_WRONLY | os.O_CREAT | os.O_TRUNC,
            0o600,
        )
        try:
            with os.fdopen(fd, "w", encoding="utf-8") as fh:
                fh.write(payload)
            if os.name == "posix":
                os.chmod(tmp_path, 0o600)
            os.replace(tmp_path, self._path)
            if os.name == "posix":
                os.chmod(self._path, 0o600)
        except Exception:
            try:
                tmp_path.unlink(missing_ok=True)
            except OSError:
                pass
            raise
