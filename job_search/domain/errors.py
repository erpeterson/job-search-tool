"""Application error hierarchy.

Every ``AppError`` carries a stable ``error_code`` identifying the raise site and
a user-safe message. Presentation layers map the class to a response code.
"""


class AppError(Exception):
    """Base class for expected, user-reportable failures."""

    def __init__(self, message, error_code):
        super().__init__(message)
        self.message = message
        self.error_code = error_code


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
