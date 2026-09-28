"""Typed, fail-closed runtime configuration."""

from __future__ import annotations

from collections.abc import Callable, Mapping
from dataclasses import dataclass
from pathlib import Path

from job_search.security import RequestSecurity


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


@dataclass(frozen=True)
class RuntimePaths:
    root: Path
    database: Path
    environment_file: Path
    log_dir: Path
    api_log: Path
    app_log: Path
    captures: Path
    guidance: Path
    career_manual: Path
    master_resume: Path
    applications: Path

    @classmethod
    def from_root(cls, root: Path) -> RuntimePaths:
        root = root.resolve()
        log_dir = root / "logs"
        return cls(
            root=root,
            database=root / "job_search.sqlite3",
            environment_file=root / ".env",
            log_dir=log_dir,
            api_log=log_dir / "api.log",
            app_log=log_dir / "job-search.log",
            captures=root / "captures",
            guidance=root / "supporting-documents" / "20260731-job-search-guidance.md",
            career_manual=root / "career-manual" / "Career-Manual.md",
            master_resume=root / "resume" / "Master-Resume.md",
            applications=root / "applications",
        )


@dataclass
class RuntimeConfiguration:
    """Validated startup settings and a private, updateable environment snapshot."""

    paths: RuntimePaths
    settings: RuntimeSettings
    security: RequestSecurity
    environment: dict[str, str]
    which: Callable[[str], str | None]
    executable: Callable[[str], bool]
    persist: Callable[[Path, Mapping[str, str]], None]

    def enabled(self, key: str) -> bool:
        return self.environment.get(key, "0") == "1"

    def cli_path(self) -> str:
        return self.environment.get("CODEX_CLI_PATH") or self.which("codex") or "codex"

    def cli_available(self) -> bool:
        path = self.cli_path()
        if Path(path).is_absolute():
            return Path(path).exists() and self.executable(path)
        return self.which(path) is not None

    def model(self) -> str:
        return self.environment.get("CODEX_MODEL", "").strip()

    def masked(self, keys: list[str]) -> dict[str, dict[str, str | bool]]:
        result: dict[str, dict[str, str | bool]] = {}
        for key in keys:
            value = self.environment.get(key, "")
            masked = "" if not value else "********" if len(value) <= 8 else f"{value[:4]}...{value[-4:]}"
            result[key] = {"configured": bool(value), "masked": masked}
        return result

    def update(self, updates: Mapping[str, str]) -> None:
        self.persist(self.paths.environment_file, updates)
        self.environment.update(updates)


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
