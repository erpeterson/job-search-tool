"""Central redaction and classification for telemetry and replay artifacts."""

from __future__ import annotations

import re
from typing import Any
from urllib.parse import urlsplit, urlunsplit


SENSITIVE_KEY = re.compile(r"(authorization|cookie|token|secret|password|api.?key|prompt|posting_text|output_text)", re.I)
SENSITIVE_VALUE = re.compile(r"(?i)(bearer\s+|api[_-]?key\s*[=:]\s*|token\s*[=:]\s*)[^\s,;]+")
REDACTED = "[REDACTED]"


def redact_url(value: str) -> str:
    """Remove URL credentials and query data before persistence."""
    parsed = urlsplit(value)
    hostname = parsed.hostname or ""
    netloc = hostname if parsed.port is None else f"{hostname}:{parsed.port}"
    return urlunsplit((parsed.scheme, netloc, parsed.path, "", ""))


def redact_value(value: Any, *, full_capture: bool = False) -> Any:
    """Return a safe, serializable view of nested telemetry data."""
    if full_capture:
        return value
    if isinstance(value, dict):
        return {
            str(key): REDACTED if SENSITIVE_KEY.search(str(key)) else redact_value(item, full_capture=False)
            for key, item in value.items()
        }
    if isinstance(value, (list, tuple)):
        return [redact_value(item, full_capture=False) for item in value]
    if isinstance(value, str):
        if value.startswith(("http://", "https://")):
            return redact_url(value)
        return SENSITIVE_VALUE.sub(REDACTED, value)
    return value


def redact_headers(headers: dict[str, Any]) -> dict[str, Any]:
    return {key: REDACTED if SENSITIVE_KEY.search(key) else redact_value(value) for key, value in headers.items()}
