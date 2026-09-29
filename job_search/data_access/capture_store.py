"""Filesystem-backed, redacted HTTP and model capture storage."""

from __future__ import annotations

import hashlib
import json
from collections.abc import Callable, Mapping
from datetime import UTC, datetime
from pathlib import Path
from typing import Any


class CaptureStore:
    """Persist deterministic captures without coupling storage to Flask."""

    def __init__(
        self,
        root: Path | Callable[[], Path],
        enabled: Callable[[], bool],
        full_capture: Callable[[], bool],
        redact: Callable[..., Any],
        observe: Callable[..., None],
    ):
        self._root, self._enabled, self._full_capture = root, enabled, full_capture
        self._redact, self._observe = redact, observe

    def _capture_root(self) -> Path:
        return self._root() if callable(self._root) else self._root

    def path(self, service: str, operation: str, payload: Mapping[str, Any]) -> Path:
        digest = hashlib.sha256(json.dumps(payload, sort_keys=True, default=str).encode()).hexdigest()
        return self._capture_root() / service / operation / f"{digest}.json"

    def read(
        self, service: str, operation: str, payload: Mapping[str, Any], *, force_refresh: bool = False
    ) -> Mapping[str, Any] | None:
        if force_refresh:
            self._observe("capture_bypass", service=service, operation=operation, reason="force_refresh")
            return None
        if not self._enabled():
            return None
        path = self.path(service, operation, payload)
        if not path.exists():
            return None
        try:
            capture = json.loads(path.read_text(encoding="utf-8"))
        except json.JSONDecodeError as exc:
            self._observe(
                "capture_corruption_recovered",
                error_code="CAPTURE_CORRUPTION_RECOVERED",
                component="data_access.capture",
                operation="read_capture",
                service=service,
                capture_operation=operation,
                capture_key=path.stem,
                cause=type(exc).__name__,
            )
            return None
        self._observe("capture_replay", service=service, operation=operation, path=str(path))
        return capture

    def write(
        self,
        service: str,
        operation: str,
        request: Mapping[str, Any],
        response: Mapping[str, Any],
        metadata: Mapping[str, Any] | None = None,
    ) -> Path | None:
        if not self._enabled():
            return None
        path = self.path(service, operation, request)
        path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
        payload = {
            "captured_at": datetime.now(UTC).isoformat(),
            "service": service,
            "operation": operation,
            "request": self._redact(request, full_capture=self._full_capture()),
            "response": self._redact(response, full_capture=self._full_capture()),
            "metadata": self._redact(metadata or {}, full_capture=self._full_capture()),
        }
        path.write_text(json.dumps(payload, indent=2, sort_keys=True, default=str) + "\n", encoding="utf-8")
        path.chmod(0o600)
        self._observe("capture_write", service=service, operation=operation, path=str(path))
        return path
