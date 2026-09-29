"""HTTP route assembly, request security, correlation, and error mapping."""

import uuid

from flask import Blueprint, g, jsonify, request
from werkzeug.exceptions import HTTPException

from job_search.errors import ClientInputError, translate_exception
from job_search.presentation.company_routes import register_company_routes
from job_search.presentation.config_routes import register_config_routes
from job_search.presentation.job_routes import register_job_routes
from job_search.presentation.packet_routes import register_packet_routes
from job_search.presentation.read_routes import dependency, register_read_routes
from job_search.presentation.search_routes import register_search_routes
from job_search.presentation.task_routes import register_task_routes
from job_search.security import authorized, csrf_valid, trusted_proxy_peer
from job_search.validation import RequestValidationError

routes = Blueprint("job_search", __name__)
register_read_routes(routes)
register_company_routes(routes)
register_job_routes(routes)
register_config_routes(routes)
register_task_routes(routes)
register_search_routes(routes)
register_packet_routes(routes)


@routes.before_request
def enforce_request_security():
    """Protect external bindings and establish correlation before route work."""
    observed = dependency("observability")
    security = dependency("configuration").security
    g.correlation_id = request.headers.get("X-Request-ID") or uuid.uuid4().hex
    g.correlation_token = observed.correlation_ids.set(g.correlation_id)
    if not security.enabled:
        return None
    if not trusted_proxy_peer(request.remote_addr, security):
        return jsonify({"error": "Request must arrive through a configured trusted proxy."}), 403
    if request.headers.get("X-Forwarded-Proto", "").lower() != "https":
        return jsonify({"error": "HTTPS is required for externally exposed deployments."}), 400
    if not authorized(request.headers.get("Authorization"), security.auth_token):
        return jsonify({"error": "Authentication is required."}), 401
    if request.method not in {"GET", "HEAD", "OPTIONS"} and not csrf_valid(
        request.headers.get("X-CSRF-Token"), security.csrf_token
    ):
        return jsonify({"error": "CSRF validation failed."}), 403
    return None


@routes.teardown_request
def clear_request_correlation_id(_error):
    token = getattr(g, "correlation_token", None)
    if token is not None:
        dependency("observability").correlation_ids.reset(token)


def log_event(event_type, **fields):
    dependency("observability").telemetry.event(event_type, **fields)


@routes.errorhandler(Exception)
def api_error(exc):
    if isinstance(exc, RequestValidationError):
        exc = ClientInputError(str(exc))
    if isinstance(exc, ClientInputError):
        mapped = translate_exception(exc)
        log_event(
            "api_request_validation_failed",
            error_code=mapped.error_code,
            component="presentation.api",
            operation=request.endpoint,
            path=request.path,
            message=str(exc),
        )
        return jsonify(mapped.body), mapped.status_code
    if isinstance(exc, HTTPException):
        log_event(
            "api_http_exception",
            error_code="API_HTTP_EXCEPTION",
            component="presentation.api",
            operation=request.endpoint,
            path=request.path,
            status_code=exc.code,
        )
        return exc
    mapped = translate_exception(exc)
    log_event(
        "api_unhandled_exception",
        error_code=mapped.error_code,
        component="presentation.api",
        operation=request.endpoint,
        path=request.path,
        error_type=type(exc).__name__,
        message=str(exc)[:1000],
    )
    return jsonify(mapped.body), mapped.status_code
