"""Typed, fail-closed runtime configuration."""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass


class StartupConfigurationError(RuntimeError):
    """Controlled startup failure for untrusted environment configuration."""


def _integer(environment: Mapping[str, str], name: str, default: int, minimum: int, maximum: int) -> int:
    raw = environment.get(name, str(default))
    try:
        value = int(raw)
    except (TypeError, ValueError) as exc:
        raise StartupConfigurationError(f"STARTUP_INVALID_CONFIGURATION: {name} must be an integer.") from exc
    if not minimum <= value <= maximum:
        raise StartupConfigurationError(
            f"STARTUP_INVALID_CONFIGURATION: {name} must be between {minimum} and {maximum}."
        )
    return value


@dataclass(frozen=True)
class RuntimeSettings:
    codex_timeout_seconds: int
    host: str
    port: int
    debug: bool
    search_interval_seconds: int
    log_max_bytes: int
    log_backup_count: int


def load_runtime_settings(environment: Mapping[str, str]) -> RuntimeSettings:
    return RuntimeSettings(
        codex_timeout_seconds=_integer(environment, "CODEX_CLI_TIMEOUT_SECONDS", 270, 1, 3_600),
        host=environment.get("JOB_SEARCH_HOST", "127.0.0.1"),
        port=_integer(environment, "JOB_SEARCH_PORT", 5050, 1, 65_535),
        debug=environment.get("JOB_SEARCH_DEBUG", "0") == "1",
        search_interval_seconds=_integer(environment, "JOB_SEARCH_INTERVAL_SECONDS", 86_400, 60, 31_536_000),
        log_max_bytes=_integer(environment, "JOB_SEARCH_LOG_MAX_BYTES", 1_048_576, 0, 100_000_000),
        log_backup_count=_integer(environment, "JOB_SEARCH_LOG_BACKUP_COUNT", 5, 0, 100),
    )
