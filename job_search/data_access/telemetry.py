"""Structured logging adapters for external and application events."""

from __future__ import annotations

import json
import logging
from collections.abc import Callable, Mapping
from datetime import UTC, datetime
from logging.handlers import RotatingFileHandler
from pathlib import Path
from typing import Any


def configure_json_file_logging(loggers: Mapping[logging.Logger, Path], *, max_bytes: int, backup_count: int) -> None:
    """Attach one JSON-lines rotating handler to each supplied logger."""
    for logger, path in loggers.items():
        if logger.handlers:
            continue
        path.parent.mkdir(parents=True, exist_ok=True)
        handler = RotatingFileHandler(path, maxBytes=max_bytes, backupCount=backup_count)
        handler.setFormatter(logging.Formatter("%(message)s"))
        logger.addHandler(handler)


class StructuredTelemetry:
    """Redact and emit API and application events without Flask dependencies."""

    def __init__(
        self,
        api_logger: logging.Logger,
        event_logger: logging.Logger,
        correlation_id: Callable[[], str | None],
        redact_url: Callable[[str], str],
        redact_value: Callable[..., Any],
        redact_content_metadata: Callable[[str], Any],
    ) -> None:
        self._api_logger = api_logger
        self._event_logger = event_logger
        self._correlation_id = correlation_id
        self._redact_url = redact_url
        self._redact_value = redact_value
        self._redact_content_metadata = redact_content_metadata

    def api_call(
        self,
        service: str,
        method: str,
        url: str,
        response: Any = None,
        error: Exception | None = None,
        elapsed_ms: int | None = None,
    ) -> None:
        status_code = getattr(response, "status_code", None) if response is not None else None
        response_text = getattr(response, "text", "") if response is not None else ""
        event = {
            "ts": datetime.now(UTC).isoformat(),
            "service": service,
            "method": method,
            "url": self._redact_url(url),
            "status_code": status_code,
            "ok": response is not None and response.ok and error is None,
            "elapsed_ms": elapsed_ms,
            "error_type": type(error).__name__ if error else None,
            "message": self._redact_value(str(error)[:1000]) if error else None,
            "response_content": self._redact_content_metadata(response_text),
        }
        self._api_logger.info(json.dumps(event, sort_keys=True))

    def event(self, event_type: str, **fields: Any) -> None:
        event = {
            "ts": datetime.now(UTC).isoformat(),
            "event": event_type,
            "correlation_id": self._correlation_id(),
            **self._redact_value(fields),
        }
        self._event_logger.info(json.dumps(event, sort_keys=True, default=str))
