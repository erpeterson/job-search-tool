"""Configuration loaded from environment variables.

``AppConfig`` is read once at startup and validated. ``RuntimeSettings`` wraps the
subset of settings the UI can change while the app runs; those remain backed by
``os.environ`` (so child processes inherit them) and are persisted to ``.env``.
"""

import os
import re
import shutil
from dataclasses import dataclass
from pathlib import Path

from job_search.domain.errors import ConfigurationError, ValidationError

DEFAULT_APP_DIR = Path(__file__).resolve().parent.parent

RUNTIME_CONFIG_KEYS = (
    "CODEX_CLI_PATH",
    "CODEX_MODEL",
    "JOB_SEARCH_ENABLE_GPT_SCORING",
    "JOB_SEARCH_USE_CAPTURE_CACHE",
)
_BOOLEAN_RUNTIME_KEYS = ("JOB_SEARCH_ENABLE_GPT_SCORING", "JOB_SEARCH_USE_CAPTURE_CACHE")
_INT_PATTERN = re.compile(r"^\s*-?\d+\s*$")
_CONTROL_CHARS = re.compile(r"[\x00-\x1f\x7f]")

# Scheduled searches are disabled in code, independent of JOB_SEARCH_AUTORUN:
# the scheduler has known bugs and consumes Codex credits unattended. Remove
# this gate once the scheduler is fixed.
SCHEDULER_SUPPORTED = False


def _int_env(environ, key, default, minimum, maximum):
    raw = environ.get(key)
    if raw is None or raw.strip() == "":
        return default
    if not _INT_PATTERN.match(raw):
        raise ConfigurationError(f"{key} must be an integer; got {raw!r}.", "config_invalid_integer")
    value = int(raw)
    if not minimum <= value <= maximum:
        raise ConfigurationError(
            f"{key} must be between {minimum} and {maximum}; got {value}.", "config_integer_out_of_range"
        )
    return value


@dataclass(frozen=True)
class AppConfig:
    app_dir: Path
    workspace_root: Path
    db_path: Path
    env_path: Path
    log_dir: Path
    api_log_path: Path
    event_log_path: Path
    capture_dir: Path
    guidance_path: Path
    career_manual_path: Path
    master_resume_path: Path
    applications_dir: Path
    host: str
    port: int
    debug: bool
    autorun: bool
    search_interval_seconds: int
    log_max_bytes: int
    log_backup_count: int
    codex_cli_timeout_seconds: int
    default_codex_model: str
    default_codex_cli_path: str

    @classmethod
    def from_env(cls, environ=None, app_dir=None):
        environ = os.environ if environ is None else environ
        app_dir = Path(app_dir or DEFAULT_APP_DIR).resolve()
        workspace_root = Path(environ.get("JOB_SEARCH_WORKSPACE_ROOT") or app_dir.parent).resolve()
        log_dir = app_dir / "logs"
        host = environ.get("JOB_SEARCH_HOST", "127.0.0.1").strip()
        if not host:
            raise ConfigurationError("JOB_SEARCH_HOST must not be empty.", "config_empty_host")
        return cls(
            app_dir=app_dir,
            workspace_root=workspace_root,
            db_path=app_dir / "job_search.sqlite3",
            env_path=app_dir / ".env",
            log_dir=log_dir,
            api_log_path=log_dir / "api.log",
            event_log_path=log_dir / "job-search.log",
            capture_dir=app_dir / "captures",
            guidance_path=workspace_root / "supporting-documents" / "20260731-job-search-guidance.md",
            career_manual_path=workspace_root / "career-manual" / "Career-Manual.md",
            master_resume_path=workspace_root / "resume" / "Master-Resume.md",
            applications_dir=workspace_root / "applications",
            host=host,
            port=_int_env(environ, "JOB_SEARCH_PORT", 5050, 1, 65535),
            debug=environ.get("JOB_SEARCH_DEBUG", "0") == "1",
            autorun=SCHEDULER_SUPPORTED and environ.get("JOB_SEARCH_AUTORUN", "1") != "0",
            search_interval_seconds=_int_env(environ, "JOB_SEARCH_INTERVAL_SECONDS", 24 * 60 * 60, 60, 365 * 86400),
            log_max_bytes=_int_env(environ, "JOB_SEARCH_LOG_MAX_BYTES", 1024 * 1024, 1024, 1024**3),
            log_backup_count=_int_env(environ, "JOB_SEARCH_LOG_BACKUP_COUNT", 5, 0, 1000),
            codex_cli_timeout_seconds=_int_env(environ, "CODEX_CLI_TIMEOUT_SECONDS", 270, 1, 24 * 3600),
            default_codex_model=environ.get("CODEX_MODEL", ""),
            default_codex_cli_path=environ.get("CODEX_CLI_PATH") or shutil.which("codex") or "codex",
        )


class RuntimeSettings:
    """Settings editable from the UI, backed by the process environment."""

    def __init__(self, env_file, default_codex_cli_path, default_codex_model="", environ=None):
        self._env_file = env_file
        self._default_codex_cli_path = default_codex_cli_path
        self._default_codex_model = default_codex_model
        self._environ = os.environ if environ is None else environ

    def gpt_scoring_enabled(self):
        return self._environ.get("JOB_SEARCH_ENABLE_GPT_SCORING", "0") == "1"

    def capture_cache_enabled(self):
        return self._environ.get("JOB_SEARCH_USE_CAPTURE_CACHE", "1") != "0"

    def codex_cli_path(self):
        return self._environ.get("CODEX_CLI_PATH") or self._default_codex_cli_path

    def codex_cli_available(self):
        path = self.codex_cli_path()
        if not path:
            return False
        if Path(path).is_absolute():
            return Path(path).exists() and os.access(path, os.X_OK)
        return shutil.which(path) is not None

    def codex_model(self, stored_model=None):
        """Environment override first, then the stored setting, then the startup default."""
        env_model = self._environ.get("CODEX_MODEL", "").strip()
        if env_model:
            return env_model
        if stored_model is not None:
            return stored_model.strip()
        return self._default_codex_model

    def masked(self):
        config = {}
        for key in RUNTIME_CONFIG_KEYS:
            value = self._environ.get(key, "")
            if not value:
                config[key] = {"configured": False, "masked": ""}
            elif len(value) <= 8:
                config[key] = {"configured": True, "masked": "********"}
            else:
                config[key] = {"configured": True, "masked": f"{value[:4]}...{value[-4:]}"}
        return config

    @staticmethod
    def validate_updates(payload):
        """Return the subset of runtime config keys to persist, validated."""
        updates = {}
        for key in RUNTIME_CONFIG_KEYS:
            if key not in payload:
                continue
            value = str(payload.get(key) if payload.get(key) is not None else "").strip()
            if _CONTROL_CHARS.search(value):
                raise ValidationError(f"{key} must not contain control characters.", "config_value_control_chars")
            if len(value) > 1024:
                raise ValidationError(f"{key} must be at most 1024 characters.", "config_value_too_long")
            if key in _BOOLEAN_RUNTIME_KEYS and value not in ("", "0", "1"):
                raise ValidationError(f"{key} must be 0 or 1.", "config_value_not_boolean")
            if value or key == "CODEX_MODEL":
                updates[key] = value
        return updates

    def apply(self, updates):
        self._env_file.update(updates)
        for key, value in updates.items():
            self._environ[key] = value
