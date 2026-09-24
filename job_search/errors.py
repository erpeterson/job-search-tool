"""Framework-independent exception classification for presentation adapters."""

from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True)
class ErrorResponse:
    status_code: int
    body: dict[str, str]
    error_code: str


class ApplicationError(Exception):
    error_code = "APPLICATION_ERROR"
    status_code = 500
    public_message = "An unexpected server error occurred."


class ClientInputError(ApplicationError):
    error_code = "API_CLIENT_INPUT_INVALID"
    status_code = 400

    def __init__(self, message: str):
        self.public_message = message
        super().__init__(message)


def translate_exception(exception: Exception) -> ErrorResponse:
    """Map known typed errors without exposing untrusted implementation details."""
    if isinstance(exception, ApplicationError):
        return ErrorResponse(
            exception.status_code,
            {"error": exception.public_message, "error_code": exception.error_code},
            exception.error_code,
        )
    return ErrorResponse(
        500,
        {"error": "An unexpected server error occurred.", "error_code": "API_UNHANDLED_EXCEPTION"},
        "API_UNHANDLED_EXCEPTION",
    )
