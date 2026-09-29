#!/usr/bin/env python3
import json
import re
import uuid

from flask import (
    Blueprint,
    current_app,
    g,
    jsonify,
    request,
)
from werkzeug.exceptions import HTTPException

from job_search.application.discovery_utils import clean_text
from job_search.application.filtering_service import FilteringService
from job_search.application.job_service import JobService
from job_search.composition import observability, runtime_configuration
from job_search.errors import ClientInputError, translate_exception
from job_search.presentation.company_routes import register_company_routes
from job_search.presentation.config_routes import register_config_routes
from job_search.presentation.job_routes import register_job_routes
from job_search.presentation.packet_routes import register_packet_routes
from job_search.presentation.read_routes import register_read_routes
from job_search.presentation.search_routes import register_search_routes
from job_search.presentation.task_routes import register_task_routes
from job_search.security import authorized, csrf_valid, trusted_proxy_peer
from job_search.validation import RequestValidationError

RUNTIME_CONFIG = runtime_configuration()
ROOT = RUNTIME_CONFIG.paths.root
DB_PATH = RUNTIME_CONFIG.paths.database
ENV_PATH = RUNTIME_CONFIG.paths.environment_file
CAPTURE_DIR = RUNTIME_CONFIG.paths.captures
APPLICATIONS_DIR = RUNTIME_CONFIG.paths.applications

routes = Blueprint("job_search", __name__)
register_read_routes(routes)
register_company_routes(routes)
register_job_routes(routes)
register_config_routes(routes)
register_task_routes(routes)
register_search_routes(routes)
register_packet_routes(routes)
OBSERVABILITY = observability(RUNTIME_CONFIG)
api_logger = OBSERVABILITY.api_logger
event_logger = OBSERVABILITY.event_logger
telemetry = OBSERVABILITY.telemetry
capture_store = OBSERVABILITY.captures
log_api_call = telemetry.api_call
capture_path = capture_store.path
read_capture = capture_store.read
write_capture = capture_store.write


@routes.before_request
def enforce_request_security():
    """Protect all external bindings before any route can mutate local state."""
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


@routes.before_request
def assign_request_correlation_id():
    # The security hook initializes this first so rejected requests are traced.
    return None


def dependency(name):
    """Resolve a required service supplied by the composition root."""
    return getattr(current_app.extensions["job_search.dependencies"], name)


def log_event(event_type, **fields):
    dependency("observability").telemetry.event(event_type, **fields)


def job_service() -> JobService:
    """Compose the framework-independent job use case for a request."""
    return dependency("job_service")


def filtering_service() -> FilteringService:
    return dependency("filtering_service")


def console_query_service():
    return dependency("console_query_service")


def startup_service():
    return dependency("startup_service")


def row_to_dict(row):
    return dict(row) if row else None


def gpt_scoring_enabled():
    return dependency("configuration").enabled("JOB_SEARCH_ENABLE_GPT_SCORING")


def codex_cli_path():
    return dependency("configuration").cli_path()


def codex_cli_available():
    return dependency("configuration").cli_available()


def full_capture_enabled():
    return dependency("configuration").enabled("JOB_SEARCH_ENABLE_FULL_CAPTURE")


def background_task_service():
    return dependency("background_task_service")


def get_background_task(task_id):
    return background_task_service().get(task_id)


def list_background_tasks(limit=10):
    return background_task_service().list(limit)


def start_background_task(operation, job_ids):
    return background_task_service().start(operation, job_ids)


def apply_filter(_connection, job_id):
    """Compatibility wrapper; filtering decisions live in ``FilteringService``."""
    # Score/discovery callers may hold a write transaction. Commit its score
    # before the repository-backed filtering use case opens its own session.
    _connection.commit()
    return filtering_service().refresh_job(job_id)


def selector_text(soup, selectors):
    for selector in selectors:
        element = soup.select_one(selector)
        if element:
            value = clean_text(element.get("title") or element.get_text(" "))
            if value:
                return value
    return ""


def meta_content(soup, properties):
    for prop in properties:
        element = soup.find("meta", attrs={"property": prop}) or soup.find("meta", attrs={"name": prop})
        if element and element.get("content"):
            return clean_text(element["content"])
    return ""


def nested_value(value, *keys):
    current = value
    for key in keys:
        if not isinstance(current, dict):
            return ""
        current = current.get(key)
    if isinstance(current, str):
        return current
    return ""


def extract_job_json_ld(soup):
    for script in soup.find_all("script", type="application/ld+json"):
        text = script.string or script.get_text()
        if not text:
            continue
        try:
            payload = json.loads(text)
        except json.JSONDecodeError:
            log_event(
                "json_ld_parse_recovered",
                error_code="JSON_LD_PARSE_RECOVERED",
                component="business.posting_parser",
                operation="extract_job_json_ld",
            )
            continue
        candidates = payload if isinstance(payload, list) else [payload]
        for candidate in candidates:
            if not isinstance(candidate, dict):
                continue
            graph = candidate.get("@graph")
            if isinstance(graph, list):
                candidates.extend(graph)
            type_value = candidate.get("@type")
            types = type_value if isinstance(type_value, list) else [type_value]
            if any("JobPosting" in str(item) for item in types):
                return candidate
    return {}


def location_from_json_ld(payload):
    location = payload.get("jobLocation") if isinstance(payload, dict) else None
    if isinstance(location, list):
        location = location[0] if location else None
    if not isinstance(location, dict):
        return ""
    address = location.get("address")
    if isinstance(address, dict):
        parts = [address.get("addressLocality"), address.get("addressRegion"), address.get("addressCountry")]
        return clean_text(", ".join(str(part) for part in parts if part))
    return nested_value(location, "name")


def append_note_text(existing, addition):
    existing = clean_text(existing)
    addition = clean_text(addition)
    if not existing:
        return addition
    if not addition:
        return existing
    return f"{existing} {addition}"


def clamp_score(value, low=0, high=10):
    try:
        parsed = int(round(float(value)))
    except (TypeError, ValueError) as exc:
        log_event(
            "score_value_coercion_recovered",
            error_code="SCORE_VALUE_COERCION_RECOVERED",
            component="business.scoring",
            operation="clamp_score",
            value_type=type(value).__name__,
            error_type=type(exc).__name__,
        )
        return low
    return max(low, min(high, parsed))


def extract_codex_reported_model(output):
    match = re.search(r"\bmodel:\s*([^\s]+)", output or "", flags=re.IGNORECASE)
    return match.group(1) if match else ""


def codex_scoring_workflow():
    return dependency("codex_scoring_workflow")


def populate_codex_score(conn, job_id, force_refresh=False):
    """Compatibility entry point backed by the application workflow."""
    return codex_scoring_workflow().populate(conn, job_id, force_refresh=force_refresh)


def discovery_service():
    return dependency("discovery_service")


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


def main(application):
    with application.app_context():
        startup_service().initialize()
        configuration = dependency("configuration")
        database_path = dependency("database_path")
    print(f"Job Search Console running at http://{configuration.settings.host}:{configuration.settings.port}")
    print(f"Database: {database_path}")
    application.run(
        host=configuration.settings.host,
        port=configuration.settings.port,
        debug=configuration.settings.debug,
        use_reloader=False,
    )


if __name__ == "__main__":
    main()
