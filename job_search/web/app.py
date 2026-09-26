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
    NotFoundError,
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

REQUEST_ID_HEADER = "X-Request-ID"
_REQUEST_ID_PATTERN = re.compile(r"^[A-Za-z0-9._-]{1,64}$")

STATUS_BY_ERROR = (
    (ValidationError, 400),
    (NotFoundError, 404),
    (ConflictError, 409),
    (DependencyUnavailableError, 409),
    (ExternalServiceError, 502),
)


def status_for(exc):
    for error_type, status in STATUS_BY_ERROR:
        if isinstance(exc, error_type):
            return status
    return 500


def create_app(container):
    app = Flask(__name__)
    app.extensions["job_search"] = container
    app.register_blueprint(bp)

    @app.before_request
    def bind_request_id():
        supplied = request.headers.get(REQUEST_ID_HEADER, "")
        request_id = supplied if _REQUEST_ID_PATTERN.match(supplied) else new_correlation_id()
        g.correlation_token = bind_correlation_id(request_id)
        g.request_started = time.monotonic()

    @app.after_request
    def log_request(response):
        request_id = current_correlation_id()
        if request_id:
            response.headers[REQUEST_ID_HEADER] = request_id
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
        return jsonify({"error": exc.message, "request_id": current_correlation_id()}), status

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
