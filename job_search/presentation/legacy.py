#!/usr/bin/env python3
import hashlib
import json
import re
import time
import uuid
from contextlib import nullcontext

from flask import (
    Blueprint,
    Response,
    current_app,
    g,
    has_app_context,
    jsonify,
    render_template,
    request,
)
from werkzeug.exceptions import HTTPException

from job_search.application.company_service import CompanyService
from job_search.application.discovery_policy import DiscoveryPolicy
from job_search.application.filtering_service import FilteringService
from job_search.application.job_scoring_policy import (
    ORACLE_IC6_LEVEL_REFERENCE,
    PIPELINES,
    RUBRIC_FIELDS,
    normalize_pipeline,
)
from job_search.application.job_service import JobService
from job_search.application.level_service import LevelService, normalize_lookup_text
from job_search.application.manual_job_service import ManualJobService
from job_search.application.rescrape_service import RescrapeService
from job_search.application.scoring_service import ScoringService
from job_search.application.search_query_service import SearchQueryService
from job_search.application.settings_service import SettingsService
from job_search.composition import (
    CodexCliError,
    database_session,
    infrastructure,
    observability,
    runtime_configuration,
)
from job_search.composition import background_task_service as compose_background_task_service
from job_search.composition import codex_json_gateway as compose_codex_json_gateway
from job_search.composition import codex_scoring_workflow as compose_codex_scoring_workflow
from job_search.composition import company_service as compose_company_service
from job_search.composition import console_query_service as compose_console_query_service
from job_search.composition import discovery_service as compose_discovery_service
from job_search.composition import filtering_service as compose_filtering_service
from job_search.composition import job_score_service as compose_job_score_service
from job_search.composition import job_service as compose_job_service
from job_search.composition import level_service as compose_level_service
from job_search.composition import outbound_clients as compose_outbound_clients
from job_search.composition import packet_attachment_service as compose_packet_attachment_service
from job_search.composition import packet_catalog as compose_packet_catalog
from job_search.composition import packet_content_service as compose_packet_content_service
from job_search.composition import packet_draft_service as compose_packet_draft_service
from job_search.composition import packet_generation_service as compose_packet_generation_service
from job_search.composition import rescrape_service as compose_rescrape_service
from job_search.composition import search_query_service as compose_search_query_service
from job_search.composition import search_run_service as compose_search_run_service
from job_search.composition import settings_service as compose_settings_service
from job_search.composition import startup_service as compose_startup_service
from job_search.errors import ClientInputError, translate_exception
from job_search.security import authorized, csrf_valid, trusted_proxy_peer
from job_search.validation import (
    RequestValidationError,
    boolean,
    choice,
    environment_value,
    http_url,
    integer,
    optional_text,
    require_json_object,
)

RUNTIME_CONFIG = runtime_configuration()
ROOT = RUNTIME_CONFIG.paths.root
APP_DIR = ROOT
DB_PATH = RUNTIME_CONFIG.paths.database
ENV_PATH = RUNTIME_CONFIG.paths.environment_file
LOG_DIR = RUNTIME_CONFIG.paths.log_dir
API_LOG_PATH = RUNTIME_CONFIG.paths.api_log
APP_LOG_PATH = RUNTIME_CONFIG.paths.app_log
CAPTURE_DIR = RUNTIME_CONFIG.paths.captures
GUIDANCE_PATH = RUNTIME_CONFIG.paths.guidance
CAREER_MANUAL_PATH = RUNTIME_CONFIG.paths.career_manual
MASTER_RESUME_PATH = RUNTIME_CONFIG.paths.master_resume
APPLICATIONS_DIR = RUNTIME_CONFIG.paths.applications

RUNTIME_SETTINGS = RUNTIME_CONFIG.settings
DEFAULT_MODEL = RUNTIME_CONFIG.model()
CODEX_CLI_TIMEOUT_SECONDS = RUNTIME_SETTINGS.codex_timeout_seconds
HOST = RUNTIME_SETTINGS.host
PORT = RUNTIME_SETTINGS.port
DEBUG = RUNTIME_SETTINGS.debug
AUTORUN = False  # the scheduler is buggy and eats codex credits; disable it for now
SEARCH_INTERVAL_SECONDS = RUNTIME_SETTINGS.search_interval_seconds
LOG_MAX_BYTES = RUNTIME_SETTINGS.log_max_bytes
LOG_BACKUP_COUNT = RUNTIME_SETTINGS.log_backup_count
CONFIG_KEYS = [
    "CODEX_CLI_PATH",
    "CODEX_MODEL",
    "JOB_SEARCH_ENABLE_GPT_SCORING",
    "JOB_SEARCH_USE_CAPTURE_CACHE",
]


routes = Blueprint("job_search", __name__)
REQUEST_SECURITY = RUNTIME_CONFIG.security
OBSERVABILITY = observability(RUNTIME_CONFIG)
api_logger = OBSERVABILITY.api_logger
event_logger = OBSERVABILITY.event_logger
telemetry = OBSERVABILITY.telemetry
capture_store = OBSERVABILITY.captures
CODEX_JSON_GATEWAY = compose_codex_json_gateway(RUNTIME_CONFIG, OBSERVABILITY)
log_api_call = telemetry.api_call
log_event = telemetry.event
capture_path = capture_store.path
read_capture = capture_store.read
write_capture = capture_store.write


@routes.before_request
def enforce_request_security():
    """Protect all external bindings before any route can mutate local state."""
    g.correlation_id = request.headers.get("X-Request-ID") or uuid.uuid4().hex
    g.correlation_token = OBSERVABILITY.correlation_ids.set(g.correlation_id)
    if not REQUEST_SECURITY.enabled:
        return None
    if not trusted_proxy_peer(request.remote_addr, REQUEST_SECURITY):
        return jsonify({"error": "Request must arrive through a configured trusted proxy."}), 403
    if request.headers.get("X-Forwarded-Proto", "").lower() != "https":
        return jsonify({"error": "HTTPS is required for externally exposed deployments."}), 400
    if not authorized(request.headers.get("Authorization"), REQUEST_SECURITY.auth_token):
        return jsonify({"error": "Authentication is required."}), 401
    if request.method not in {"GET", "HEAD", "OPTIONS"} and not csrf_valid(
        request.headers.get("X-CSRF-Token"), REQUEST_SECURITY.csrf_token
    ):
        return jsonify({"error": "CSRF validation failed."}), 403
    return None


@routes.teardown_request
def clear_request_correlation_id(_error):
    token = getattr(g, "correlation_token", None)
    if token is not None:
        OBSERVABILITY.correlation_ids.reset(token)


@routes.before_request
def assign_request_correlation_id():
    # The security hook initializes this first so rejected requests are traced.
    return None


JOB_STATUSES = {
    "researching",
    "interested",
    "applied",
    "interviewing",
    "offer",
    "rejected",
    "declined",
    "paused",
}
COMPANY_STATUSES = {"watching", "target", "active_conversation", "paused", "not_interested"}
SUPPORTED_BOARDS = {"linkedin", "indeed"}

MIN_ANNUAL_COMPENSATION = 200_000
UNKNOWN_LEVEL_ASSESSMENT = "Unknown - level not assessed"
INFRASTRUCTURE = infrastructure()


class _DatabaseSessionProvider:
    """Deferred composition provider so tests can replace ``DB_PATH`` safely."""

    def __call__(self):
        return database_session(DB_PATH)


connect = _DatabaseSessionProvider()


def dependency(name, fallback):
    """Resolve a service factory supplied by the composition root when present."""
    if has_app_context():
        services = current_app.extensions["job_search.dependencies"].services
        configured = services.get(name)
        if configured is not None:
            return configured() if callable(configured) else configured
    return fallback()


def job_service() -> JobService:
    """Compose the framework-independent job use case for a request."""
    return dependency("job_service", lambda: compose_job_service(DB_PATH, log_event))


def company_service() -> CompanyService:
    return dependency("company_service", lambda: compose_company_service(DB_PATH))


def search_query_service() -> SearchQueryService:
    return dependency("search_query_service", lambda: compose_search_query_service(DB_PATH))


def settings_service() -> SettingsService:
    return dependency("settings_service", lambda: compose_settings_service(DB_PATH))


def filtering_service() -> FilteringService:
    def observe(job, decision):
        if decision.filtered:
            log_event(
                "job_filtered",
                job_id=job.get("id"),
                company=job["company"],
                title=job["title"],
                reasons=decision.reasons,
                gpt_score=job["gpt_score"],
                user_score=job["user_score"],
                downlevel=bool(job["downlevel"]),
                gpt_scoring_enabled=gpt_scoring_enabled(),
            )

    return dependency(
        "filtering_service",
        lambda: compose_filtering_service(DB_PATH, observe),
    )


def manual_job_service() -> ManualJobService:
    def score(job_id: int) -> object:
        return codex_scoring_workflow().populate_by_id(job_id, force_refresh=False)

    def scoring_availability() -> str | None:
        if not gpt_scoring_enabled():
            return "Codex scoring is disabled."
        if not codex_cli_available():
            return f"Codex CLI is unavailable at {codex_cli_path()!r}."
        return None

    def report_failure(operation: str, error: Exception, context: dict[str, object]) -> None:
        if operation == "scrape":
            log_event(
                "manual_job_scrape_failed",
                error_code="MANUAL_JOB_SCRAPE_FAILED",
                component="business.job_ingestion",
                operation="scrape_job_from_url",
                error_type=type(error).__name__,
                message=str(error)[:1000],
                **context,
            )
        else:
            log_event(
                "manual_job_auto_score_failed",
                error_code="MANUAL_JOB_AUTO_SCORE_FAILED",
                component="business.job_scoring",
                operation="populate_codex_score",
                error_type=type(error).__name__,
                message=str(error)[:1000],
                **context,
            )

    return ManualJobService(
        INFRASTRUCTURE.job_repository(connect),
        lambda url, force_refresh: OUTBOUND_CLIENTS.boards.scrape(url, force_refresh=force_refresh),
        OUTBOUND_CLIENTS.boards.fallback,
        filtering_service().refresh_job,
        score,
        scoring_availability,
        report_failure,
    )


def rescrape_service() -> RescrapeService:
    return compose_rescrape_service(
        INFRASTRUCTURE.job_repository(connect),
        lambda url, force_refresh: OUTBOUND_CLIENTS.boards.scrape(url, force_refresh=force_refresh),
        filtering_service().refresh_job,
        now,
    )


def packet_attachment_service():
    return dependency("packet_attachment_service", lambda: compose_packet_attachment_service(DB_PATH))


def packet_content_service():
    return dependency(
        "packet_content_service",
        lambda: compose_packet_content_service(DB_PATH),
    )


def scoring_service() -> ScoringService:
    def score(job_id: int) -> dict[str, object]:
        return codex_scoring_workflow().populate_by_id(job_id, force_refresh=False)

    def availability() -> str | None:
        if not gpt_scoring_enabled():
            return "Codex scoring is currently disabled. Set JOB_SEARCH_ENABLE_GPT_SCORING=1 to re-enable it."
        if not codex_cli_available():
            return f"Codex CLI is unavailable at {codex_cli_path()!r}."
        return None

    return ScoringService(job_service().get_job, score, availability)


def console_query_service():
    return dependency("console_query_service", lambda: compose_console_query_service(DB_PATH))


def discovery_policy() -> DiscoveryPolicy:
    return DiscoveryPolicy(MIN_ANNUAL_COMPENSATION)


def level_service(connection=None) -> LevelService:
    if connection is not None:
        return compose_level_service(DB_PATH, connection)
    return dependency("level_service", lambda: compose_level_service(DB_PATH))


def startup_service():
    return dependency("startup_service", lambda: compose_startup_service(DB_PATH, telemetry, DEFAULT_MODEL))


def lookup_level_equivalency(conn, company, title):
    equivalency = level_service(conn).lookup(company, title)
    if not equivalency:
        log_event(
            "level_equivalency_unknown",
            company=company,
            title=title,
            reason="No cached calibration or reliable local title estimate.",
        )
    return equivalency


def level_assessment_from_equivalency(equivalency):
    return LevelService.assessment(equivalency)


def now():
    return int(time.time())


def row_to_dict(row):
    return dict(row) if row else None


def parse_json_field(value, fallback):
    if not value:
        return fallback
    try:
        return json.loads(value)
    except json.JSONDecodeError:
        log_event(
            "stored_json_parse_recovered",
            error_code="STORED_JSON_PARSE_RECOVERED",
            component="data_access.serialization",
            operation="parse_json_field",
        )
        return fallback


def settings(conn):
    return INFRASTRUCTURE.read_models.settings(conn)


def gpt_scoring_enabled():
    return RUNTIME_CONFIG.enabled("JOB_SEARCH_ENABLE_GPT_SCORING")


def codex_cli_path():
    return RUNTIME_CONFIG.cli_path()


def codex_cli_available():
    return RUNTIME_CONFIG.cli_available()


def codex_model(conn=None):
    env_model = RUNTIME_CONFIG.model()
    if env_model:
        return env_model
    if conn is not None:
        return (settings(conn).get("codex_model") or "").strip()
    return DEFAULT_MODEL


def capture_cache_enabled():
    return RUNTIME_CONFIG.enabled("JOB_SEARCH_USE_CAPTURE_CACHE")


def full_capture_enabled():
    return RUNTIME_CONFIG.enabled("JOB_SEARCH_ENABLE_FULL_CAPTURE")


def background_task_service():
    return dependency("background_task_service", lambda: compose_background_task_service(DB_PATH, log_event))


def get_background_task(task_id):
    return background_task_service().get(task_id)


def list_background_tasks(limit=10):
    return background_task_service().list(limit)


def start_background_task(operation, job_ids):
    return background_task_service().start(operation, job_ids)


def masked_config():
    return RUNTIME_CONFIG.masked(CONFIG_KEYS)


def apply_filter(_connection, job_id):
    """Compatibility wrapper; filtering decisions live in ``FilteringService``."""
    # Score/discovery callers may hold a write transaction. Commit its score
    # before the repository-backed filtering use case opens its own session.
    _connection.commit()
    return filtering_service().refresh_job(job_id)


def list_jobs(conn, include_filtered=False):
    jobs = []
    for row in INFRASTRUCTURE.read_models.jobs(conn, include_filtered):
        item = dict(row)
        item["gpt_scorecard"] = parse_json_field(item.pop("gpt_scorecard_json"), {})
        item["user_scorecard"] = parse_json_field(item.pop("user_scorecard_json"), {})
        jobs.append(item)
    return jobs


def list_company_interests(conn):
    return list(INFRASTRUCTURE.read_models.company_interests(conn))


def get_company_interest(conn, company_id):
    return INFRASTRUCTURE.read_models.company_interest(conn, company_id)


def get_job(conn, job_id):
    job = INFRASTRUCTURE.read_models.job(conn, job_id)
    if not job:
        return None
    job["gpt_scorecard"] = parse_json_field(job.pop("gpt_scorecard_json"), {})
    job["user_scorecard"] = parse_json_field(job.pop("user_scorecard_json"), {})
    return job


def list_search_queries(conn):
    return list(INFRASTRUCTURE.read_models.search_queries(conn))


def list_search_runs(conn):
    return list(INFRASTRUCTURE.read_models.search_runs(conn))


def search_schedule_state(conn):
    last_search_at = int(settings(conn).get("last_search_at", "0") or 0)
    next_run_at = last_search_at + SEARCH_INTERVAL_SECONDS if last_search_at else now()
    return {
        "autorun_enabled": AUTORUN,
        "interval_seconds": SEARCH_INTERVAL_SECONDS,
        "last_search_at": last_search_at,
        "next_run_at": next_run_at if AUTORUN else None,
    }


def list_discoveries(conn, limit=50):
    discoveries = []
    for row in INFRASTRUCTURE.read_models.discoveries(conn, limit):
        item = dict(row)
        item["gpt_scorecard"] = parse_json_field(item.pop("gpt_scorecard_json"), {})
        discoveries.append(item)
    return discoveries


def render_inline_markdown(text):
    inline = escape_html(text)
    inline = re.sub(r"`([^`]+)`", r"<code>\1</code>", inline)
    inline = re.sub(r"\*\*([^*]+)\*\*", r"<strong>\1</strong>", inline)
    inline = re.sub(r"\[([^\]]+)\]\(([^)]+)\)", r'<a href="\2" target="_blank" rel="noopener">\1</a>', inline)
    return inline


def markdown_to_html(markdown):
    html = []
    in_list = False
    in_code = False
    code_lines = []
    for raw_line in markdown.splitlines():
        line = raw_line.rstrip()
        stripped = line.strip()
        if stripped.startswith("```"):
            if in_code:
                html.append(f"<pre><code>{escape_html(chr(10).join(code_lines))}</code></pre>")
                code_lines = []
                in_code = False
            else:
                if in_list:
                    html.append("</ul>")
                    in_list = False
                in_code = True
            continue
        if in_code:
            code_lines.append(line)
            continue
        if not stripped:
            if in_list:
                html.append("</ul>")
                in_list = False
            continue
        heading = re.match(r"^(#{1,6})\s+(.+)$", stripped)
        if heading:
            if in_list:
                html.append("</ul>")
                in_list = False
            level = len(heading.group(1))
            html.append(f"<h{level}>{render_inline_markdown(heading.group(2))}</h{level}>")
            continue
        if stripped == "---":
            if in_list:
                html.append("</ul>")
                in_list = False
            html.append("<hr>")
            continue
        bullet = re.match(r"^-\s+(.+)$", stripped)
        if bullet:
            if not in_list:
                html.append("<ul>")
                in_list = True
            html.append(f"<li>{render_inline_markdown(bullet.group(1))}</li>")
            continue
        if in_list:
            html.append("</ul>")
            in_list = False
        html.append(f"<p>{render_inline_markdown(stripped)}</p>")
    if in_code:
        html.append(f"<pre><code>{escape_html(chr(10).join(code_lines))}</code></pre>")
    if in_list:
        html.append("</ul>")
    return "\n".join(html)


def escape_html(value):
    return (
        str(value or "")
        .replace("&", "&amp;")
        .replace("<", "&lt;")
        .replace(">", "&gt;")
        .replace('"', "&quot;")
        .replace("'", "&#39;")
    )


def list_application_packets(conn):
    return dependency("packet_catalog", lambda: compose_packet_catalog(DB_PATH)).list(conn)


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


def clean_text(value):
    return " ".join((value or "").split())


def clean_url(value):
    return (value or "").split("?trk=")[0].strip()


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


def source_id(board, url):
    digest = hashlib.sha256((url or "").encode("utf-8")).hexdigest()[:16]
    return f"{board}:{digest}"


def dedupe_results(results):
    seen = set()
    deduped = []
    for result in results:
        key = result.get("url") or result.get("source_job_id")
        if not key or key in seen:
            continue
        seen.add(key)
        deduped.append(result)
    return deduped


OUTBOUND_CLIENTS = compose_outbound_clients(OBSERVABILITY, clean_text, clean_url, source_id, dedupe_results)


def already_seen(conn, url):
    if not url:
        return False
    return INFRASTRUCTURE.read_models.job_exists_url(conn, url)


def already_seen_reason(conn, url):
    if not url:
        return None
    if INFRASTRUCTURE.read_models.job_exists_url(conn, url):
        return "already tracked in jobs"
    return None


def location_filter_decision(result):
    return discovery_policy().location(result)


def compensation_filter_decision(result):
    return discovery_policy().compensation(result)


def sales_role_filter_decision(result):
    return discovery_policy().sales_role(result)


def extract_codex_reported_model(output):
    match = re.search(r"\bmodel:\s*([^\s]+)", output or "", flags=re.IGNORECASE)
    return match.group(1) if match else ""


def call_codex_json(model, prompt, operation, force_refresh=False, return_metadata=False):
    return CODEX_JSON_GATEWAY.complete(
        model, prompt, operation, force_refresh=force_refresh, return_metadata=return_metadata
    )


def packet_generation_service():
    return compose_packet_generation_service(
        DB_PATH,
        compose_packet_draft_service(RUNTIME_CONFIG, OBSERVABILITY, CODEX_JSON_GATEWAY),
        telemetry,
    )


def create_application_packet(_connection, job_id):
    """Compatibility entry point backed by the application workflow."""
    return packet_generation_service().generate(job_id)


def codex_scoring_workflow():
    return compose_codex_scoring_workflow(
        DB_PATH,
        RUNTIME_CONFIG,
        compose_job_score_service(RUNTIME_CONFIG, CODEX_JSON_GATEWAY),
        telemetry,
    )


def populate_codex_score(conn, job_id, force_refresh=False):
    """Compatibility entry point backed by the application workflow."""
    return codex_scoring_workflow().populate(conn, job_id, force_refresh=force_refresh)


def discovery_service():
    scorer = compose_job_score_service(RUNTIME_CONFIG, CODEX_JSON_GATEWAY)
    return compose_discovery_service(
        DB_PATH,
        telemetry,
        UNKNOWN_LEVEL_ASSESSMENT,
        ORACLE_IC6_LEVEL_REFERENCE,
        scoring_enabled=gpt_scoring_enabled,
        scorer_available=codex_cli_available,
        scorer_path=codex_cli_path,
        score=scorer.score_discovery,
        apply_filter=apply_filter,
        normalize_pipeline=normalize_pipeline,
        refine=lambda connection, prompt, *, force_refresh: call_codex_json(
            codex_model(connection), prompt, "refine_search_query", force_refresh=force_refresh
        ),
        clean_text=clean_text,
    )


def refine_search_query(conn, query_id, force_refresh=False):
    """Compatibility entry point backed by the application workflow."""
    return discovery_service().refine_query(conn, query_id, force_refresh=force_refresh)


def classify_discovery(conn, result, force_refresh=False):
    """Compatibility entry point backed by the application workflow."""
    return discovery_service().classify(conn, result, force_refresh=force_refresh)


def search_rejection_reason(result):
    for name, decision in (
        ("sales_role", sales_role_filter_decision),
        ("location", location_filter_decision),
        ("compensation", compensation_filter_decision),
    ):
        allowed, reason = decision(result)
        if not allowed:
            return name, reason
    return None


def run_job_search(trigger="manual", force_refresh=False):
    """Run search through the application-layer orchestration service."""
    discovery = discovery_service()
    return compose_search_run_service(
        DB_PATH,
        OUTBOUND_CLIENTS.search_gateway,
        telemetry,
        reject_reason=search_rejection_reason,
        level_assessment=discovery.assess_level,
        already_seen_reason=already_seen_reason,
        classify=lambda connection, result, *, force_refresh: classify_discovery(
            connection, result, force_refresh=force_refresh
        ),
        refine=lambda connection, query_id, *, force_refresh: refine_search_query(
            connection, query_id, force_refresh=force_refresh
        ),
        is_refinement_error=lambda error: isinstance(error, CodexCliError),
    ).run(trigger=trigger, force_refresh=force_refresh)


@routes.get("/")
def index():
    return render_template("index.html")


@routes.get("/api/state")
def api_state():
    include_filtered = request.args.get("include_filtered") == "1"
    state = dict(console_query_service().state(include_filtered=include_filtered))
    state.update(
        {
            "config": masked_config(),
            "api_log_path": str(API_LOG_PATH),
            "event_log_path": str(APP_LOG_PATH),
            "capture_dir": str(CAPTURE_DIR),
            "gpt_scoring_enabled": gpt_scoring_enabled(),
            "capture_cache_enabled": capture_cache_enabled(),
            "search_schedule": {"interval_seconds": SEARCH_INTERVAL_SECONDS, "managed_by": "job_search.scheduler"},
            "codex_tasks": list_background_tasks(),
            "pipelines": PIPELINES,
            "rubric_fields": RUBRIC_FIELDS,
        }
    )
    return jsonify(state)


@routes.get("/api/jobs/<int:job_id>")
def api_job(job_id):
    job = console_query_service().job(job_id)
    if not job:
        return jsonify({"error": "Job not found"}), 404
    return jsonify({"job": job})


@routes.get("/api/application-packets")
def api_application_packets():
    return jsonify({"application_packets": console_query_service().packets()})


def clean_job_ids(payload):
    raw_ids = payload.get("job_ids", [])
    if not isinstance(raw_ids, list):
        raise RequestValidationError("job_ids must be a list.")
    if not raw_ids:
        raise RequestValidationError("Select at least one job.")
    if len(raw_ids) > 50:
        raise RequestValidationError("job_ids must contain at most 50 jobs.")
    job_ids = []
    seen = set()
    for raw_id in raw_ids:
        job_id = integer(raw_id, "job_ids item", minimum=1, maximum=2_147_483_647)
        if job_id in seen:
            raise RequestValidationError("job_ids must not contain duplicates.")
        seen.add(job_id)
        job_ids.append(job_id)
    return job_ids


def request_json_object():
    """Validate the actual parsed body; do not coerce arrays/null into {}."""
    return require_json_object(request.get_json(silent=True))


@routes.get("/api/codex-tasks")
def api_codex_tasks():
    return jsonify({"tasks": list_background_tasks()})


@routes.get("/api/codex-tasks/<task_id>")
def api_codex_task(task_id):
    task = get_background_task(task_id)
    if not task:
        return jsonify({"error": "Task not found"}), 404
    return jsonify({"task": task})


@routes.post("/api/jobs/bulk/score-gpt")
def api_bulk_score_gpt():
    payload = request_json_object()
    if not gpt_scoring_enabled():
        return jsonify(
            {"error": "Codex scoring is currently disabled. Set JOB_SEARCH_ENABLE_GPT_SCORING=1 to re-enable it."}
        ), 409
    if not codex_cli_available():
        return jsonify(
            {
                "error": f"Codex CLI is unavailable at {codex_cli_path()!r}. Set CODEX_CLI_PATH or install Codex CLI before scoring."
            }
        ), 409
    job_ids = clean_job_ids(payload)
    task = start_background_task("scorecards", job_ids)
    return jsonify({"task": task}), 202


@routes.post("/api/jobs/bulk/application-packets/generate")
def api_bulk_generate_application_packets():
    payload = request_json_object()
    if not codex_cli_available():
        return jsonify(
            {"error": f"Codex CLI is unavailable at {codex_cli_path()!r}. Set CODEX_CLI_PATH or install Codex CLI."}
        ), 409
    job_ids = clean_job_ids(payload)
    task = start_background_task("application_packets", job_ids)
    return jsonify({"task": task}), 202


@routes.post("/api/jobs/<int:job_id>/application-packet/generate")
def api_generate_application_packet(job_id):
    job = console_query_service().job(job_id)
    if not job:
        return jsonify({"error": "Job not found"}), 404
    if job.get("application_packet_path"):
        return jsonify({"error": "This job already has an associated application packet."}), 409
    packet = create_application_packet(None, job_id)
    return jsonify(
        {
            "packet": packet,
            "job": console_query_service().job(job_id),
            "application_packets": console_query_service().packets(),
        }
    ), 201


@routes.post("/api/jobs/<int:job_id>/application-packet/attach")
def api_attach_application_packet(job_id):
    payload = request_json_object()
    packet_path = optional_text(payload.get("path"), "path", max_length=2_000)
    if not packet_path:
        raise RequestValidationError("path is required.")
    try:
        result = packet_attachment_service().attach(job_id, packet_path)
    except ValueError as exc:
        log_event(
            "packet_attachment_rejected",
            error_code="PACKET_ATTACHMENT_REJECTED",
            component="presentation.packets",
            operation="attach",
            job_id=job_id,
            error_type=type(exc).__name__,
        )
        return jsonify({"error": str(exc)}), 400
    if result is None:
        return jsonify({"error": "Job not found"}), 404
    log_event("application_packet_attached", job_id=job_id, path=result["path"])
    return jsonify({"job": result["job"], "application_packets": console_query_service().packets()})


@routes.get("/api/jobs/<int:job_id>/application-packet/content")
def api_application_packet_content(job_id):
    filename = request.args.get("file", "")
    if not filename.endswith(".md") or "/" in filename or "\\" in filename:
        return jsonify({"error": "Select a Markdown file in the associated packet."}), 400
    job = console_query_service().job(job_id)
    if not job:
        return jsonify({"error": "Job not found"}), 404
    if not job.get("application_packet_path"):
        return jsonify({"error": "Job does not have an associated application packet."}), 404
    try:
        packet = packet_content_service().read(job["application_packet_path"], filename)
    except (ValueError, FileNotFoundError) as exc:
        log_event(
            "packet_content_path_rejected",
            error_code="PACKET_CONTENT_PATH_REJECTED",
            component="presentation.packets",
            operation="content",
            job_id=job_id,
            error_type=type(exc).__name__,
        )
        return jsonify({"error": str(exc)}), 404
    return jsonify(packet)


@routes.get("/api/jobs/<int:job_id>/application-packet/render")
def api_application_packet_render(job_id):
    filename = request.args.get("file", "")
    if not filename.endswith(".md") or "/" in filename or "\\" in filename:
        return Response("Select a Markdown file in the associated packet.", status=400, mimetype="text/plain")
    with nullcontext():
        job = console_query_service().job(job_id)
        if not job:
            return Response("Job not found.", status=404, mimetype="text/plain")
        if not job.get("application_packet_path"):
            return Response("Job does not have an associated application packet.", status=404, mimetype="text/plain")
        try:
            packet = packet_content_service().read(job["application_packet_path"], filename)
        except (ValueError, FileNotFoundError) as exc:
            log_event(
                "packet_render_path_rejected",
                error_code="PACKET_RENDER_PATH_REJECTED",
                component="presentation.packets",
                operation="render",
                job_id=job_id,
                error_type=type(exc).__name__,
            )
            return Response(str(exc), status=404, mimetype="text/plain")
        markdown = packet["content"]
        body = markdown_to_html(markdown)
        title = f"{filename} - {job['company']} - {job['title']}"
        html = f"""<!doctype html>
<html lang="en">
<head>
  <meta charset="utf-8">
  <meta name="viewport" content="width=device-width, initial-scale=1">
  <title>{escape_html(title)}</title>
  <style>
    :root {{
      --bg: #f3f1ea;
      --ink: #18211b;
      --muted: #667066;
      --line: #cfc8b8;
      --accent: #0f766e;
    }}
    body {{
      margin: 0;
      background: radial-gradient(circle at top left, rgba(15,118,110,.11), transparent 34%), var(--bg);
      color: var(--ink);
      font-family: "Avenir Next", "Segoe UI", sans-serif;
      line-height: 1.55;
    }}
    main {{
      max-width: 920px;
      margin: 0 auto;
      padding: 36px 24px 64px;
    }}
    .meta {{
      color: var(--muted);
      border-bottom: 1px solid var(--line);
      padding-bottom: 14px;
      margin-bottom: 28px;
      font-size: 13px;
    }}
    h1, h2, h3, h4, h5, h6 {{ line-height: 1.18; margin: 1.35em 0 .45em; }}
    h1 {{ font-size: 34px; margin-top: 0; }}
    h2 {{ font-size: 24px; }}
    h3 {{ font-size: 18px; color: var(--accent); }}
    p {{ margin: .6em 0; }}
    ul {{ padding-left: 1.4em; }}
    li {{ margin: .35em 0; }}
    hr {{ border: 0; border-top: 1px solid var(--line); margin: 24px 0; }}
    code {{
      background: rgba(15,118,110,.09);
      border: 1px solid rgba(15,118,110,.18);
      border-radius: 4px;
      padding: 1px 4px;
      font-family: "SFMono-Regular", Consolas, monospace;
      font-size: .92em;
    }}
    pre {{
      overflow: auto;
      padding: 14px;
      border: 1px solid var(--line);
      border-radius: 8px;
      background: #fbfaf5;
    }}
    a {{ color: var(--accent); }}
  </style>
</head>
<body>
  <main>
    <div class="meta">{escape_html(packet["path"])} / {escape_html(filename)}</div>
    {body}
  </main>
</body>
</html>
"""
        return Response(html, mimetype="text/html")


@routes.post("/api/jobs/<int:job_id>/scrape")
def api_rescrape_job(job_id):
    payload = request_json_object()
    force_refresh = boolean(payload.get("force_refresh"), "force_refresh", default=True)
    result = rescrape_service().rescrape(job_id, force_refresh=force_refresh)
    if result.job is None:
        return jsonify({"error": "Job not found"}), 404
    if result.scraped is None:
        return jsonify({"error": "Job does not have a URL to scrape."}), 400
    log_event(
        "manual_job_rescraped",
        job_id=job_id,
        url=result.job["url"],
        company=result.scraped.get("company"),
        title=result.scraped.get("title"),
        force_refresh=force_refresh,
    )
    return jsonify({"job": job_service().get_job(job_id), "scraped": result.scraped})


@routes.delete("/api/jobs/<int:job_id>")
def api_delete_job(job_id):
    payload = request_json_object()
    if payload.get("confirm") != "DELETE":
        return jsonify({"error": "Type DELETE to confirm job deletion."}), 400
    job = console_query_service().job(job_id)
    if not job:
        return jsonify({"error": "Job not found"}), 404
    job_service().delete_job(job_id)
    log_event("manual_job_deleted", job_id=job_id, company=job["company"], title=job["title"], url=job["url"])
    return jsonify({"deleted_job_id": job_id, "jobs": console_query_service().jobs(include_filtered=True)})


@routes.get("/api/companies/<int:company_id>")
def api_company_interest(company_id):
    company = console_query_service().company(company_id)
    if not company:
        return jsonify({"error": "Company interest not found"}), 404
    return jsonify({"company": company})


@routes.post("/api/companies")
def api_create_company_interest():
    payload = require_json_object(request.get_json(silent=True) or {})
    company_name = optional_text(payload.get("company", ""), "company", max_length=300) or "Unknown company"
    ts = now()
    interest_score = payload.get("interest_score")
    if interest_score not in (None, ""):
        interest_score = integer(interest_score, "interest_score", minimum=0, maximum=100)
    status = choice(payload.get("status", "watching"), "status", COMPANY_STATUSES, required=True)
    rationale = optional_text(payload.get("rationale", ""), "rationale", max_length=20_000)
    notes = optional_text(payload.get("notes", ""), "notes", max_length=20_000)
    next_step = optional_text(payload.get("next_step", ""), "next_step", max_length=2_000)
    contacts = optional_text(payload.get("contacts", ""), "contacts", max_length=10_000)
    company_id = company_service().save(
        {
            "created_at": ts,
            "updated_at": ts,
            "company": company_name,
            "normalized_company": normalize_lookup_text(company_name),
            "status": status,
            "interest_score": interest_score if interest_score != "" else None,
            "rationale": rationale,
            "notes": notes,
            "next_step": next_step,
            "contacts": contacts,
        }
    )
    return jsonify(
        {"company": console_query_service().company(company_id), "companies": console_query_service().companies()}
    ), 201


@routes.post("/api/companies/<int:company_id>")
def api_update_company_interest(company_id):
    payload = require_json_object(request.get_json(silent=True) or {})
    existing = console_query_service().company(company_id)
    if not existing:
        return jsonify({"error": "Company interest not found"}), 404
    company_name = (
        optional_text(payload.get("company", existing["company"]), "company", max_length=300) or existing["company"]
    )
    interest_score = payload.get("interest_score")
    if interest_score not in (None, ""):
        interest_score = integer(interest_score, "interest_score", minimum=0, maximum=100)
    status = choice(payload.get("status", existing["status"]), "status", COMPANY_STATUSES, required=True)
    company_service().update(
        company_id,
        {
            "company": company_name,
            "normalized_company": normalize_lookup_text(company_name),
            "status": status,
            "interest_score": interest_score if interest_score != "" else None,
            "rationale": optional_text(payload.get("rationale", ""), "rationale", max_length=20_000),
            "notes": optional_text(payload.get("notes", ""), "notes", max_length=20_000),
            "next_step": optional_text(payload.get("next_step", ""), "next_step", max_length=2_000),
            "contacts": optional_text(payload.get("contacts", ""), "contacts", max_length=10_000),
            "updated_at": now(),
        },
    )
    return jsonify(
        {"company": console_query_service().company(company_id), "companies": console_query_service().companies()}
    )


@routes.post("/api/jobs")
def api_create_job():
    payload = require_json_object(request.get_json(silent=True) or {})
    ts = now()
    url = http_url(payload.get("url"))
    pipeline = choice(payload.get("pipeline"), "pipeline", PIPELINES, required=True)
    status = choice(payload.get("status", "researching"), "status", JOB_STATUSES, required=True)
    result = manual_job_service().create(
        {
            "created_at": ts,
            "updated_at": ts,
            "company": optional_text(payload.get("company", ""), "company", max_length=300),
            "title": optional_text(payload.get("title", ""), "title", max_length=500),
            "url": url,
            "location": optional_text(payload.get("location", ""), "location", max_length=500),
            "pipeline": pipeline,
            "status": status,
            "posting_text": optional_text(payload.get("posting_text", ""), "posting_text", max_length=100_000),
            "notes": optional_text(payload.get("notes", ""), "notes", max_length=20_000),
        },
        force_refresh=boolean(payload.get("force_refresh"), "force_refresh", default=False),
    )
    if result.job_id is None:
        return jsonify({"error": "This job URL is already tracked.", "job": result.existing_job}), 409
    if result.score_error and result.score_error.startswith("Automatic Codex scoring skipped:"):
        log_event("manual_job_auto_score_skipped", job_id=result.job_id, reason=result.score_error)
    return jsonify(
        {
            "job": job_service().get_job(result.job_id),
            "scrape_error": result.scrape_error,
            "score_error": result.score_error,
        }
    ), 201


@routes.post("/api/search/run")
def api_run_search():
    payload = require_json_object(request.get_json(silent=True) or {})
    force_refresh = boolean(payload.get("force_refresh"), "force_refresh", default=False)
    run = run_job_search(trigger="manual", force_refresh=force_refresh)
    state = console_query_service().state(include_filtered=True)
    return jsonify(
        {"run": run, "jobs": state["jobs"], "search_runs": state["search_runs"], "discoveries": state["discoveries"]}
    )


@routes.post("/api/search/queries")
def api_create_search_query():
    payload = require_json_object(request.get_json(silent=True) or {})
    ts = now()
    board = choice(payload.get("board", "linkedin"), "board", SUPPORTED_BOARDS, required=True)
    pipeline = choice(payload.get("pipeline", ""), "pipeline", PIPELINES)
    keywords = optional_text(payload.get("keywords", ""), "keywords", max_length=2_000)
    if not keywords:
        raise RequestValidationError("keywords is required.")
    search_query_service().create(
        {
            "board": board,
            "pipeline": pipeline,
            "keywords": keywords,
            "location": optional_text(payload.get("location", ""), "location", max_length=500),
            "enabled": 1 if boolean(payload.get("enabled"), "enabled", default=True) else 0,
            "created_at": ts,
            "criteria": optional_text(payload.get("criteria", ""), "criteria", max_length=10_000),
        }
    )
    return jsonify({"search_queries": console_query_service().queries()}), 201


@routes.post("/api/search/queries/<int:query_id>")
def api_update_search_query(query_id):
    payload = require_json_object(request.get_json(silent=True) or {})
    board = choice(payload["board"], "board", SUPPORTED_BOARDS, required=True) if "board" in payload else None
    pipeline = choice(payload["pipeline"], "pipeline", PIPELINES) if "pipeline" in payload else None
    keywords = optional_text(payload["keywords"], "keywords", max_length=2_000) if "keywords" in payload else None
    location = optional_text(payload["location"], "location", max_length=500) if "location" in payload else None
    criteria = optional_text(payload["criteria"], "criteria", max_length=10_000) if "criteria" in payload else None
    search_query_service().update(
        query_id,
        {
            "board": board,
            "pipeline": pipeline,
            "keywords": keywords,
            "location": location,
            "criteria": criteria,
            "enabled": (1 if boolean(payload["enabled"], "enabled") else 0) if "enabled" in payload else None,
        },
    )
    return jsonify({"search_queries": console_query_service().queries()})


@routes.post("/api/config")
def api_update_config():
    payload = require_json_object(request.get_json(silent=True) or {})
    updates = {}
    for key in CONFIG_KEYS:
        if key not in payload:
            continue
        value = environment_value(payload.get(key, ""), key, max_length=4_000)
        if key in {"JOB_SEARCH_ENABLE_GPT_SCORING", "JOB_SEARCH_USE_CAPTURE_CACHE"} and value not in {"0", "1"}:
            raise RequestValidationError(f"{key} must be 0 or 1.")
        if value or key == "CODEX_MODEL":
            updates[key] = value
    if not updates:
        return jsonify(
            {
                "config": masked_config(),
                "api_log_path": str(API_LOG_PATH),
                "event_log_path": str(APP_LOG_PATH),
                "capture_dir": str(CAPTURE_DIR),
                "gpt_scoring_enabled": gpt_scoring_enabled(),
                "capture_cache_enabled": capture_cache_enabled(),
            }
        )
    RUNTIME_CONFIG.update(updates)
    if "CODEX_MODEL" in updates:
        settings_service().save({"codex_model": updates["CODEX_MODEL"]})
    return jsonify(
        {
            "config": masked_config(),
            "settings": console_query_service().settings(),
            "api_log_path": str(API_LOG_PATH),
            "event_log_path": str(APP_LOG_PATH),
            "capture_dir": str(CAPTURE_DIR),
            "gpt_scoring_enabled": gpt_scoring_enabled(),
            "capture_cache_enabled": capture_cache_enabled(),
        }
    )


@routes.post("/api/admin/purge-jobs")
def api_purge_jobs():
    payload = require_json_object(request.get_json(silent=True) or {})
    if payload.get("confirm") != "PURGE":
        return jsonify({"error": "Type PURGE to confirm tracked job deletion."}), 400
    before = job_service().purge_jobs()
    log_event("admin_purge_jobs", deleted_jobs=before)
    state = console_query_service().state(include_filtered=True)
    return jsonify({"deleted_jobs": before, "jobs": state["jobs"], "discoveries": state["discoveries"]})


@routes.post("/api/jobs/<int:job_id>/score-gpt")
def api_score_gpt(job_id):
    result = scoring_service().score(job_id)
    if result.state == "missing":
        return jsonify({"error": "Job not found"}), 404
    if result.state == "unavailable":
        return jsonify({"error": result.unavailable_reason}), 409
    return jsonify({"job": result.job, "raw_score": result.raw_score})


@routes.post("/api/jobs/<int:job_id>/score-user")
def api_score_user(job_id):
    payload = require_json_object(request.get_json(silent=True) or {})
    raw_scorecard = payload.get("scorecard", {})
    if not isinstance(raw_scorecard, dict):
        raise RequestValidationError("scorecard must be a JSON object.")
    scorecard = {field: integer(raw_scorecard.get(field, 0), field, minimum=0, maximum=10) for field in RUBRIC_FIELDS}
    derived_total = round(sum(scorecard.values()) * 100 / (len(RUBRIC_FIELDS) * 10))
    total = integer(payload.get("total_score", derived_total), "total_score", minimum=0, maximum=100)
    rationale = optional_text(payload.get("user_rationale", ""), "user_rationale", max_length=20_000)
    if not job_service().save_user_score(job_id, total, json.dumps(scorecard), rationale, now()):
        return jsonify({"error": "Job not found"}), 404
    filtering_service().refresh_job(job_id)
    return jsonify({"job": console_query_service().job(job_id)})


@routes.post("/api/jobs/<int:job_id>/interactions")
def api_add_interaction(job_id):
    payload = require_json_object(request.get_json(silent=True) or {})
    values = {
        "occurred_on": optional_text(payload.get("occurred_on", ""), "occurred_on", max_length=40),
        "person_name": optional_text(payload.get("person_name", ""), "person_name", max_length=300),
        "person_role": optional_text(payload.get("person_role", ""), "person_role", max_length=300),
        "channel": optional_text(payload.get("channel", ""), "channel", max_length=100),
        "summary": optional_text(payload.get("summary", ""), "summary", max_length=20_000),
        "notes_to_self": optional_text(payload.get("notes_to_self", ""), "notes_to_self", max_length=20_000),
        "next_step": optional_text(payload.get("next_step", ""), "next_step", max_length=2_000),
    }
    if not job_service().add_interaction(job_id, values, now()):
        return jsonify({"error": "Job not found"}), 404
    return jsonify({"job": console_query_service().job(job_id)}), 201


@routes.post("/api/jobs/<int:job_id>/notes")
def api_add_note(job_id):
    payload = require_json_object(request.get_json(silent=True) or {})
    note = optional_text(payload.get("note", ""), "note", max_length=20_000)
    if not note:
        raise RequestValidationError("note is required.")
    service = job_service()
    if not service.add_note(job_id, note, now()):
        return jsonify({"error": "Job not found"}), 404
    return jsonify({"job": console_query_service().job(job_id)}), 201


@routes.post("/api/jobs/<int:job_id>/status")
def api_update_status(job_id):
    payload = require_json_object(request.get_json(silent=True) or {})
    status = choice(payload.get("status", "researching"), "status", JOB_STATUSES, required=True)
    service = job_service()
    if not service.update_status(job_id, status, now()):
        return jsonify({"error": "Job not found"}), 404
    return jsonify({"job": console_query_service().job(job_id)})


@routes.post("/api/settings")
def api_update_settings():
    payload = require_json_object(request.get_json(silent=True) or {})
    validated = {}
    if "gpt_threshold" in payload:
        validated["gpt_threshold"] = str(integer(payload["gpt_threshold"], "gpt_threshold", minimum=0, maximum=100))
    if "user_threshold" in payload:
        validated["user_threshold"] = str(integer(payload["user_threshold"], "user_threshold", minimum=0, maximum=100))
    if "codex_model" in payload:
        validated["codex_model"] = optional_text(payload["codex_model"], "codex_model", max_length=200)
    settings_service().save(validated)
    filtering_service().refresh_all()
    return jsonify(
        {"settings": console_query_service().settings(), "jobs": console_query_service().jobs(include_filtered=True)}
    )


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
    startup_service().initialize()
    print(f"Job Search Console running at http://{HOST}:{PORT}")
    print(f"Database: {DB_PATH}")
    application.run(host=HOST, port=PORT, debug=DEBUG, use_reloader=False)


if __name__ == "__main__":
    main()
