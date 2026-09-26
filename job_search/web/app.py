"""Flask application factory with request correlation and top-level exception handling."""

import logging
import re
import time

from flask import Flask, g, jsonify, request
from werkzeug.exceptions import HTTPException

from job_search.domain.errors import (
    AppError,
    ConflictError,
    DependencyUnavailableError,
    ExternalServiceError,
    ForbiddenError,
    NotFoundError,
    UnsupportedMediaTypeError,
    ValidationError,
)
from job_search.observability import (
    bind_correlation_id,
    current_correlation_id,
    log_event,
    new_correlation_id,
    record_exception,
    reset_correlation_id,
)
from job_search.web.routes import bp
from job_search.web.validation import require_json_content_type

REQUEST_ID_HEADER = "X-Request-ID"
# 'unsafe-inline' for scripts is required until the inline UI script and handlers are extracted (T-35).
SECURITY_HEADERS = {
    "Content-Security-Policy": (
        "default-src 'self'; script-src 'self' 'unsafe-inline'; style-src 'self' 'unsafe-inline'; "
        "frame-ancestors 'none'"
    ),
    "X-Content-Type-Options": "nosniff",
    "X-Frame-Options": "DENY",
    "Referrer-Policy": "no-referrer",
}
STATE_CHANGING_METHODS = frozenset({"POST", "PUT", "PATCH", "DELETE"})
_REQUEST_ID_PATTERN = re.compile(r"^[A-Za-z0-9._-]{1,64}$")

STATUS_BY_ERROR = (
    (ValidationError, 400),
    (ForbiddenError, 403),
    (NotFoundError, 404),
    (ConflictError, 409),
    (UnsupportedMediaTypeError, 415),
    (DependencyUnavailableError, 409),
    (ExternalServiceError, 502),
)


def status_for(exc):
    for error_type, status in STATUS_BY_ERROR:
        if isinstance(exc, error_type):
            return status
    return 500


def check_request_origin(allowed_hosts):
    """Reject unexpected Host headers (DNS rebinding) and cross-origin state changes."""
    host = request.host.lower()
    if host not in allowed_hosts:
        raise ForbiddenError("Request host is not allowed.", "request_host_not_allowed")
    origin = request.headers.get("Origin")
    if request.method in STATE_CHANGING_METHODS and origin is not None:
        allowed_origins = {f"{scheme}://{entry}" for entry in allowed_hosts for scheme in ("http", "https")}
        if origin.lower() not in allowed_origins:
            raise ForbiddenError("Cross-origin requests are not allowed.", "request_origin_not_allowed")


def create_app(container):
    app = Flask(__name__)
    app.config["MAX_CONTENT_LENGTH"] = container.config.max_request_bytes
    app.extensions["job_search"] = container
    app.register_blueprint(bp)
    allowed_hosts = frozenset(container.config.allowed_hosts)

    @app.before_request
    def bind_request_id():
        supplied = request.headers.get(REQUEST_ID_HEADER, "")
        request_id = supplied if _REQUEST_ID_PATTERN.match(supplied) else new_correlation_id()
        g.correlation_token = bind_correlation_id(request_id)
        g.request_started = time.monotonic()
        check_request_origin(allowed_hosts)
        if request.method in STATE_CHANGING_METHODS:
            require_json_content_type()

    @app.after_request
    def log_request(response):
        request_id = current_correlation_id()
        if request_id:
            response.headers[REQUEST_ID_HEADER] = request_id
        for name, value in SECURITY_HEADERS.items():
            response.headers.setdefault(name, value)
        started = g.get("request_started")
        log_event(
            "http_request_completed",
            method=request.method,
            path=request.path,
            status=response.status_code,
            elapsed_ms=int((time.monotonic() - started) * 1000) if started else None,
        )
        return response

    @app.teardown_request
    def reset_request_id(_exc):
        token = g.pop("correlation_token", None)
        if token is not None:
            reset_correlation_id(token)

    @app.errorhandler(AppError)
    def handle_app_error(exc):
        status = status_for(exc)
        record_exception(
            exc.error_code,
            "web",
            request.endpoint or request.path,
            exc,
            level=logging.WARNING if status < 500 else logging.ERROR,
            recovery=f"Returned HTTP {status} to the client.",
            method=request.method,
            path=request.path,
        )
        body = {**exc.response_fields, "error": exc.message, "request_id": current_correlation_id()}
        return jsonify(body), status

    @app.errorhandler(HTTPException)
    def handle_http_exception(exc):
        record_exception(
            f"http_{exc.code}",
            "web",
            request.endpoint or request.path,
            exc,
            level=logging.INFO,
            recovery=f"Returned HTTP {exc.code} to the client.",
            method=request.method,
            path=request.path,
        )
        if request.path.startswith("/api/"):
            return jsonify({"error": exc.description, "request_id": current_correlation_id()}), exc.code
        return exc

    @app.errorhandler(Exception)
    def handle_unexpected(exc):
        record_exception(
            "http_unhandled_exception",
            "web",
            request.endpoint or request.path,
            exc,
            recovery="Returned HTTP 500 without internal details; see this log entry.",
            method=request.method,
            path=request.path,
        )
        request_id = current_correlation_id()
        return jsonify({"error": f"Internal server error. Reference: {request_id}", "request_id": request_id}), 500

    return app
