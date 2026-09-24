"""Boundary validation shared by Flask handlers and business workflows.

This module deliberately has no Flask or database dependency, which keeps the
application rules testable outside the presentation layer.
"""

from __future__ import annotations

import ipaddress
from typing import Any, Iterable
from urllib.parse import urlparse


class RequestValidationError(ValueError):
    """An actionable validation error safe to return to an API caller."""


def require_json_object(payload: Any) -> dict[str, Any]:
    """Return a JSON object or reject arrays, scalars, and malformed bodies."""
    if not isinstance(payload, dict):
        raise RequestValidationError("Request body must be a JSON object.")
    return payload


def optional_text(value: Any, field: str, *, max_length: int = 10_000) -> str:
    """Normalize an optional text input and enforce an intentional size limit."""
    if value is None:
        return ""
    if not isinstance(value, str):
        raise RequestValidationError(f"{field} must be text.")
    normalized = value.strip()
    if len(normalized) > max_length:
        raise RequestValidationError(f"{field} must be at most {max_length} characters.")
    return normalized


def required_text(value: Any, field: str, *, max_length: int = 10_000) -> str:
    normalized = optional_text(value, field, max_length=max_length)
    if not normalized:
        raise RequestValidationError(f"{field} is required.")
    return normalized


def choice(value: Any, field: str, allowed: Iterable[str], *, required: bool = False) -> str:
    normalized = optional_text(value, field, max_length=200)
    allowed_values = set(allowed)
    if not normalized and not required:
        return ""
    if normalized not in allowed_values:
        options = ", ".join(sorted(allowed_values))
        raise RequestValidationError(f"{field} must be one of: {options}.")
    return normalized


def http_url(value: Any, field: str = "URL") -> str:
    normalized = required_text(value, field, max_length=4_000)
    parsed = urlparse(normalized)
    if parsed.scheme not in {"http", "https"} or not parsed.netloc:
        raise RequestValidationError(f"{field} must be an absolute http or https URL.")
    if parsed.username or parsed.password:
        raise RequestValidationError(f"{field} must not contain user credentials.")
    hostname = parsed.hostname
    if not hostname or hostname.lower() == "localhost":
        raise RequestValidationError(f"{field} must not target localhost.")
    try:
        address = ipaddress.ip_address(hostname)
    except ValueError:
        return normalized
    if not address.is_global:
        raise RequestValidationError(f"{field} must not target a private or reserved address.")
    return normalized


def integer(value: Any, field: str, *, minimum: int, maximum: int) -> int:
    if isinstance(value, bool):
        raise RequestValidationError(f"{field} must be an integer.")
    try:
        parsed = int(value)
    except (TypeError, ValueError) as exc:
        raise RequestValidationError(f"{field} must be an integer.") from exc
    if not minimum <= parsed <= maximum:
        raise RequestValidationError(f"{field} must be between {minimum} and {maximum}.")
    return parsed


def environment_value(value: Any, field: str, *, max_length: int = 4_000) -> str:
    """Validate a value before serializing it into a dotenv configuration file."""
    normalized = optional_text(value, field, max_length=max_length)
    if any(character in normalized for character in ("\x00", "\r", "\n")):
        raise RequestValidationError(f"{field} must not contain control characters.")
    return normalized


def boolean(value: Any, field: str, *, default: bool | None = None) -> bool:
    """Accept only JSON booleans, avoiding truthy-string coercion."""
    if value is None and default is not None:
        return default
    if not isinstance(value, bool):
        raise RequestValidationError(f"{field} must be a JSON boolean.")
    return value
