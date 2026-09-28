"""Application error hierarchy.

Every ``AppError`` carries a stable ``error_code`` identifying the raise site and
a user-safe message. Presentation layers map the class to a response code.
"""


class AppError(Exception):
    """Base class for expected, user-reportable failures."""

    def __init__(self, message, error_code, response_fields=None, detail=None):
        super().__init__(message)
        self.message = message
        self.error_code = error_code
        # Diagnostic specifics (paths, stderr); logged by record_exception, never sent to clients.
        self.detail = detail
        # Extra user-safe fields presentation layers may include in the error response.
        self.response_fields = response_fields or {}


def public_error_code(exc, default):
    """The stable code to show users for ``exc``: its own code for AppErrors, else ``default``."""
    return exc.error_code if isinstance(exc, AppError) else default


class ValidationError(AppError):
    """Input failed validation at a system boundary."""


class NotFoundError(AppError):
    """A requested entity does not exist."""


class ForbiddenError(AppError):
    """The request is not allowed from its origin or host."""


class UnsupportedMediaTypeError(AppError):
    """A request body was sent with an unsupported content type."""


class ConflictError(AppError):
    """The request conflicts with current state."""


class DuplicateJobError(ConflictError):
    """A job with the same URL is already tracked; carries the existing job."""

    def __init__(self, existing_job):
        super().__init__("This job URL is already tracked.", "job_url_already_tracked", {"job": existing_job})
        self.existing_job = existing_job


class CapacityError(AppError):
    """Too much work is already in progress; retry later."""


class DuplicateUrlError(ConflictError):
    """A job with this URL was inserted concurrently (the unique URL index rejected the insert)."""


class TaskStartError(AppError):
    """A background task could not be started (maps to HTTP 500 with this message)."""


class DependencyUnavailableError(AppError):
    """A required local dependency (Codex CLI, feature flag) is unavailable."""


class ExternalServiceError(AppError):
    """An external process or service failed or returned unusable output."""


class ConfigurationError(AppError):
    """Startup configuration is invalid."""


class CodexCliError(ExternalServiceError):
    """The Codex CLI subprocess exited unsuccessfully."""

    def __init__(self, operation, returncode=None):
        suffix = f" exited with code {returncode}" if returncode is not None else " failed"
        super().__init__(
            f"Codex CLI {operation}{suffix}. See logs and captures for details.",
            "codex_cli_nonzero_exit",
        )
        self.operation = operation
        self.returncode = returncode
