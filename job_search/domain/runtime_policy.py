"""Validation policy for settings the UI may change while the app runs."""

import os
import re
import shutil
from pathlib import Path

from job_search.domain.errors import ValidationError

RUNTIME_CONFIG_KEYS = (
    "CODEX_CLI_PATH",
    "CODEX_MODEL",
    "JOB_SEARCH_ENABLE_GPT_SCORING",
    "JOB_SEARCH_USE_CAPTURE_CACHE",
)
BOOLEAN_RUNTIME_KEYS = ("JOB_SEARCH_ENABLE_GPT_SCORING", "JOB_SEARCH_USE_CAPTURE_CACHE")
CODEX_EXECUTABLE_NAME = "codex"
MAX_VALUE_CHARS = 1024
_CONTROL_CHARS = re.compile(r"[\x00-\x1f\x7f]")


def validate_codex_cli_path(value):
    """Accept only ``codex`` on PATH or an absolute path to an executable named ``codex``.

    The value is executed as a subprocess, so the web UI must not be able to point it
    at an arbitrary program.
    """
    if value == CODEX_EXECUTABLE_NAME:
        if shutil.which(value) is None:
            raise ValidationError("CODEX_CLI_PATH 'codex' was not found on PATH.", "config_codex_not_on_path")
        return value
    path = Path(value)
    if not path.is_absolute():
        raise ValidationError(
            "CODEX_CLI_PATH must be 'codex' or an absolute path to the codex executable.",
            "config_codex_path_not_absolute",
        )
    if path.name != CODEX_EXECUTABLE_NAME:
        raise ValidationError(
            "CODEX_CLI_PATH must point to an executable named 'codex'.", "config_codex_path_wrong_name"
        )
    if not path.is_file() or not os.access(path, os.X_OK):
        raise ValidationError("CODEX_CLI_PATH must be an existing executable file.", "config_codex_path_not_executable")
    return value


class RuntimeSettingsPolicy:
    @staticmethod
    def validate(payload):
        """Return the subset of runtime config keys to persist, validated."""
        updates = {}
        for key in RUNTIME_CONFIG_KEYS:
            if key not in payload:
                continue
            value = str(payload.get(key) if payload.get(key) is not None else "").strip()
            if _CONTROL_CHARS.search(value):
                raise ValidationError(f"{key} must not contain control characters.", "config_value_control_chars")
            if len(value) > MAX_VALUE_CHARS:
                raise ValidationError(f"{key} must be at most {MAX_VALUE_CHARS} characters.", "config_value_too_long")
            if key in BOOLEAN_RUNTIME_KEYS and value not in ("", "0", "1"):
                raise ValidationError(f"{key} must be 0 or 1.", "config_value_not_boolean")
            if key == "CODEX_CLI_PATH" and value:
                validate_codex_cli_path(value)
            if value or key == "CODEX_MODEL":
                updates[key] = value
        return updates
