"""Configuration loaded from environment variables.

``AppConfig`` is read once at startup and validated. ``RuntimeSettings`` holds the
subset of settings the UI can change while the app runs, in memory.
"""

import ipaddress
import logging
import os
import re
import shutil
import threading
from dataclasses import dataclass
from pathlib import Path

from job_search.domain.errors import ConfigurationError
from job_search.domain.runtime_policy import RUNTIME_CONFIG_KEYS
from job_search.observability import record_exception

DEFAULT_APP_DIR = Path(__file__).resolve().parent.parent

_INT_PATTERN = re.compile(r"^\s*-?\d+\s*$")

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


def is_loopback_host(host):
    host = host.strip("[]").lower()
    if host == "localhost":
        return True
    try:
        return ipaddress.ip_address(host).is_loopback
    except ValueError as exc:
        record_exception(
            "config_host_not_ip_literal",
            "config",
            "is_loopback_host",
            exc,
            level=logging.DEBUG,
            recovery="Hostnames other than localhost are treated as non-loopback.",
            host=host,
        )
        return False


def _check_bind_safety(environ, host, debug):
    """The API is unauthenticated and debug mode enables remote code execution, so bind locally by default."""
    if is_loopback_host(host):
        return
    if debug:
        raise ConfigurationError(
            f"JOB_SEARCH_DEBUG=1 is not allowed with non-loopback JOB_SEARCH_HOST={host!r}; "
            "the Werkzeug debugger allows remote code execution.",
            "config_debug_on_remote_host",
        )
    if environ.get("JOB_SEARCH_ALLOW_REMOTE", "0") != "1":
        raise ConfigurationError(
            f"JOB_SEARCH_HOST={host!r} exposes the unauthenticated API to the network. "
            "Use 127.0.0.1, or set JOB_SEARCH_ALLOW_REMOTE=1 to accept the risk.",
            "config_remote_bind_not_allowed",
        )


_PATH_CONTROL_CHARS = re.compile(r"[\x00-\x1f\x7f]")


def _path_env(environ, key, default, base, kind):
    """Resolve a configured path (relative values resolve against ``base``) and check its type if it exists."""
    raw = environ.get(key, "").strip()
    if not raw:
        path = default
    elif _PATH_CONTROL_CHARS.search(raw):
        raise ConfigurationError(f"{key} must not contain control characters.", "config_path_invalid")
    else:
        candidate = Path(raw).expanduser()
        path = candidate if candidate.is_absolute() else base / candidate
    path = path.resolve()
    if kind == "dir" and path.exists() and not path.is_dir():
        raise ConfigurationError(f"{key} must be a directory; {path} is not.", "config_path_not_directory")
    if kind == "file" and path.exists() and not path.is_file():
        raise ConfigurationError(f"{key} must be a file; {path} is not.", "config_path_not_file")
    return path


_HOST_ENTRY_PATTERN = re.compile(r"^[a-z0-9.\-\[\]:]{1,262}$")


def _allowed_hosts(environ, host, port):
    """Host header values the web app accepts, guarding against DNS rebinding."""
    raw = environ.get("JOB_SEARCH_ALLOWED_HOSTS", "").strip()
    if not raw:
        defaults = [f"127.0.0.1:{port}", f"localhost:{port}", f"{host.lower()}:{port}"]
        return tuple(dict.fromkeys(defaults))
    entries = [entry.strip().lower() for entry in raw.split(",") if entry.strip()]
    invalid = [entry for entry in entries if not _HOST_ENTRY_PATTERN.match(entry)]
    if not entries or invalid:
        raise ConfigurationError(
            "JOB_SEARCH_ALLOWED_HOSTS must be a comma-separated list of host[:port] values; "
            f"invalid entries: {invalid or [raw]}.",
            "config_invalid_allowed_hosts",
        )
    return tuple(entries)


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
    allowed_hosts: tuple
    max_request_bytes: int
    http_max_response_bytes: int
    pandoc_timeout_seconds: int
    http_timeout_seconds: int
    db_timeout_seconds: int
    max_retained_tasks: int

    @classmethod
    def from_env(cls, environ=None, app_dir=None):
        environ = os.environ if environ is None else environ
        app_dir = Path(app_dir or DEFAULT_APP_DIR).resolve()
        workspace_root = _path_env(environ, "JOB_SEARCH_WORKSPACE_ROOT", app_dir.parent, app_dir, "dir")
        log_dir = _path_env(environ, "JOB_SEARCH_LOG_DIR", app_dir / "logs", app_dir, "dir")
        host = environ.get("JOB_SEARCH_HOST", "127.0.0.1").strip()
        if not host:
            raise ConfigurationError("JOB_SEARCH_HOST must not be empty.", "config_empty_host")
        port = _int_env(environ, "JOB_SEARCH_PORT", 5050, 1, 65535)
        debug = environ.get("JOB_SEARCH_DEBUG", "0") == "1"
        _check_bind_safety(environ, host, debug)
        return cls(
            app_dir=app_dir,
            workspace_root=workspace_root,
            db_path=_path_env(environ, "JOB_SEARCH_DB_PATH", app_dir / "job_search.sqlite3", app_dir, "file"),
            env_path=app_dir / ".env",
            log_dir=log_dir,
            api_log_path=log_dir / "api.log",
            event_log_path=log_dir / "job-search.log",
            capture_dir=_path_env(environ, "JOB_SEARCH_CAPTURE_DIR", app_dir / "captures", app_dir, "dir"),
            guidance_path=_path_env(
                environ,
                "JOB_SEARCH_GUIDANCE_PATH",
                workspace_root / "supporting-documents" / "20260731-job-search-guidance.md",
                workspace_root,
                "file",
            ),
            career_manual_path=_path_env(
                environ,
                "JOB_SEARCH_CAREER_MANUAL_PATH",
                workspace_root / "career-manual" / "Career-Manual.md",
                workspace_root,
                "file",
            ),
            master_resume_path=_path_env(
                environ,
                "JOB_SEARCH_MASTER_RESUME_PATH",
                workspace_root / "resume" / "Master-Resume.md",
                workspace_root,
                "file",
            ),
            applications_dir=workspace_root / "applications",
            host=host,
            port=port,
            debug=debug,
            autorun=SCHEDULER_SUPPORTED and environ.get("JOB_SEARCH_AUTORUN", "1") != "0",
            search_interval_seconds=_int_env(environ, "JOB_SEARCH_INTERVAL_SECONDS", 24 * 60 * 60, 60, 365 * 86400),
            log_max_bytes=_int_env(environ, "JOB_SEARCH_LOG_MAX_BYTES", 1024 * 1024, 1024, 1024**3),
            log_backup_count=_int_env(environ, "JOB_SEARCH_LOG_BACKUP_COUNT", 5, 0, 1000),
            codex_cli_timeout_seconds=_int_env(environ, "CODEX_CLI_TIMEOUT_SECONDS", 270, 1, 24 * 3600),
            default_codex_model=environ.get("CODEX_MODEL", ""),
            default_codex_cli_path=environ.get("CODEX_CLI_PATH") or shutil.which("codex") or "codex",
            allowed_hosts=_allowed_hosts(environ, host, port),
            pandoc_timeout_seconds=_int_env(environ, "PANDOC_TIMEOUT_SECONDS", 120, 1, 3600),
            http_timeout_seconds=_int_env(environ, "JOB_SEARCH_HTTP_TIMEOUT_SECONDS", 30, 1, 600),
            db_timeout_seconds=_int_env(environ, "JOB_SEARCH_DB_TIMEOUT_SECONDS", 30, 1, 600),
            max_retained_tasks=_int_env(environ, "JOB_SEARCH_MAX_RETAINED_TASKS", 50, 1, 10_000),
            http_max_response_bytes=_int_env(
                environ, "JOB_SEARCH_HTTP_MAX_RESPONSE_BYTES", 5 * 1024**2, 1024, 256 * 1024**2
            ),
            max_request_bytes=_int_env(environ, "JOB_SEARCH_MAX_REQUEST_BYTES", 1024 * 1024, 1024, 64 * 1024**2),
        )


class RuntimeSettings:
    """In-memory, thread-safe runtime settings, seeded once from the environment at startup.

    The UI can change these values while the app runs; persistence to ``.env`` is the
    caller's job. The process environment is never modified.
    """

    def __init__(self, values, default_codex_cli_path, default_codex_model=""):
        self._values = {key: str(values[key]) for key in RUNTIME_CONFIG_KEYS if values.get(key) is not None}
        self._default_codex_cli_path = default_codex_cli_path
        self._default_codex_model = default_codex_model
        self._lock = threading.Lock()

    def _get(self, key, default=""):
        with self._lock:
            return self._values.get(key, default)

    def update(self, updates):
        with self._lock:
            self._values.update({key: str(value) for key, value in updates.items() if key in RUNTIME_CONFIG_KEYS})

    def gpt_scoring_enabled(self):
        return self._get("JOB_SEARCH_ENABLE_GPT_SCORING", "0") == "1"

    def capture_cache_enabled(self):
        return self._get("JOB_SEARCH_USE_CAPTURE_CACHE", "1") != "0"

    def codex_cli_path(self):
        return self._get("CODEX_CLI_PATH") or self._default_codex_cli_path

    def codex_cli_available(self):
        path = self.codex_cli_path()
        if not path:
            return False
        if Path(path).is_absolute():
            return Path(path).exists() and os.access(path, os.X_OK)
        return shutil.which(path) is not None

    def codex_model(self, stored_model=None):
        """Configured override first, then the stored setting, then the startup default."""
        configured = self._get("CODEX_MODEL").strip()
        if configured:
            return configured
        if stored_model is not None:
            return stored_model.strip()
        return self._default_codex_model

    def masked(self):
        config = {}
        for key in RUNTIME_CONFIG_KEYS:
            value = self._get(key)
            if not value:
                config[key] = {"configured": False, "masked": ""}
            elif len(value) <= 8:
                config[key] = {"configured": True, "masked": "********"}
            else:
                config[key] = {"configured": True, "masked": f"{value[:4]}...{value[-4:]}"}
        return config
