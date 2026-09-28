#!/usr/bin/env python3
import hashlib
import json
import re
import textwrap
import time
import uuid
from contextlib import nullcontext
from datetime import UTC, datetime

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
from job_search.application.job_scoring_policy import JobScoringPolicy
from job_search.application.job_service import JobService
from job_search.application.level_service import LevelService, normalize_lookup_text
from job_search.application.manual_job_service import ManualJobService
from job_search.application.rescrape_service import RescrapeService
from job_search.application.scoring_service import ScoringService
from job_search.application.search_query_service import SearchQueryService
from job_search.application.settings_service import SettingsService
from job_search.composition import background_task_service as compose_background_task_service
from job_search.composition import codex_scoring_workflow as compose_codex_scoring_workflow
from job_search.composition import company_service as compose_company_service
from job_search.composition import console_query_service as compose_console_query_service
from job_search.composition import (
    database_session,
    infrastructure,
    observability,
    parse_model_json,
    read_optional_text,
    runtime_configuration,
)
from job_search.composition import discovery_service as compose_discovery_service
from job_search.composition import filtering_service as compose_filtering_service
from job_search.composition import initialization_service as compose_initialization_service
from job_search.composition import job_service as compose_job_service
from job_search.composition import level_service as compose_level_service
from job_search.composition import outbound_clients as compose_outbound_clients
from job_search.composition import packet_attachment_service as compose_packet_attachment_service
from job_search.composition import packet_catalog as compose_packet_catalog
from job_search.composition import packet_content_service as compose_packet_content_service
from job_search.composition import packet_document_writer as compose_packet_document_writer
from job_search.composition import packet_generation_service as compose_packet_generation_service
from job_search.composition import rescrape_service as compose_rescrape_service
from job_search.composition import search_query_service as compose_search_query_service
from job_search.composition import search_repository as compose_search_repository
from job_search.composition import search_run_service as compose_search_run_service
from job_search.composition import settings_service as compose_settings_service
from job_search.composition import task_execution_service as compose_task_execution_service
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


class CodexCliError(RuntimeError):
    def __init__(self, operation, returncode=None):
        self.operation = operation
        self.returncode = returncode
        suffix = f" exited with code {returncode}" if returncode is not None else " failed"
        super().__init__(f"Codex CLI {operation}{suffix}. See logs and captures for details.")


RUBRIC_FIELDS = [
    "interesting_technical_problems",
    "organizational_influence",
    "cross_functional_work",
    "opportunity_to_mentor",
    "work_life_balance",
    "low_operational_burden",
    "compensation",
    "mission",
]

PIPELINES = [
    "Executive IC",
    "Office of the CTO",
    "Adjacent industries",
    "Wildcards",
]

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

PIPELINE_CRITERIA = {
    "Executive IC": {
        "description": "Distinguished Engineer, Chief Architect, Technical Fellow, Principal Architect, Senior Principal Engineer roles at cloud, infrastructure, enterprise software, and AI platform companies.",
        "keywords": '("Distinguished Engineer" OR "Chief Architect" OR "Technical Fellow" OR "Principal Architect" OR "Senior Principal Engineer") (cloud OR infrastructure OR platform OR enterprise OR AI)',
    },
    "Office of the CTO": {
        "description": "Office of CTO, technical strategy, engineering strategy, CTO advisor, strategic initiatives, technical incubation, emerging technology roles hidden inside executive descriptions.",
        "keywords": '("Office of the CTO" OR "Technical Strategy" OR "Engineering Strategy" OR "CTO Advisor" OR "Strategic Initiatives" OR "Technical Incubation" OR "Emerging Technology")',
    },
    "Adjacent industries": {
        "description": "Architectural roles in healthcare, defense, climate, industrial automation, and scientific computing organizations with complicated technical organizations.",
        "keywords": '("Chief Architect" OR "Principal Architect" OR "Distinguished Engineer" OR "Technical Strategy") (healthcare OR defense OR climate OR "industrial automation" OR "scientific computing")',
    },
    "Wildcards": {
        "description": "Intellectually interesting roles in national labs, Disney Imagineering, Apple Vision, NVIDIA research operations, NASA contractors, AI safety, and robotics platforms.",
        "keywords": '("AI safety" OR robotics OR "research operations" OR "national lab" OR NASA OR "Apple Vision" OR Imagineering OR NVIDIA) ("Principal Engineer" OR Architect OR "Technical Strategy")',
    },
}

SALES_ROLE_EXCLUSION_QUERY = (
    '-"Account Executive" -"Sales Executive" -"Sales Director" -"Account Manager" -"Business Development" -sales'
)
SALES_ROLE_EXCLUSION_CRITERIA = "Exclude Account Executive and other sales roles."
DEFAULT_SEARCH_QUERIES = [
    {
        "board": board,
        "pipeline": pipeline,
        "keywords": f"{config['keywords']} {SALES_ROLE_EXCLUSION_QUERY}",
        "location": "Remote",
        "criteria": f"{config['description']} {SALES_ROLE_EXCLUSION_CRITERIA}",
        "seeded": 1,
    }
    for pipeline, config in PIPELINE_CRITERIA.items()
    for board in ("linkedin", "indeed")
]

ORACLE_IC6_LEVEL_REFERENCE = (
    "Oracle Software Engineer IC-6 is Architect. "
    "Treat IC6-equivalent as Architect / Principal-plus / Staff-plus scope with broad technical influence, "
    "cross-team architecture, durable technical direction, or organization-level engineering judgment."
)
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


def search_repository():
    return dependency("search_repository", lambda: compose_search_repository(DB_PATH))


def console_query_service():
    return dependency("console_query_service", lambda: compose_console_query_service(DB_PATH))


def discovery_policy() -> DiscoveryPolicy:
    return DiscoveryPolicy(MIN_ANNUAL_COMPENSATION)


def level_service(connection=None) -> LevelService:
    if connection is not None:
        return compose_level_service(DB_PATH, connection)
    return dependency("level_service", lambda: compose_level_service(DB_PATH))


def initialization_service():
    return dependency("initialization_service", lambda: compose_initialization_service(DB_PATH))


def init_db():
    APP_DIR.mkdir(parents=True, exist_ok=True)
    initialization_service().initialize_database(
        {"gpt_threshold": "40", "user_threshold": "60", "codex_model": DEFAULT_MODEL, "last_search_at": "0"}
    )

    recovered_tasks = background_task_service().initialize()
    if recovered_tasks:
        log_event(
            "background_tasks_recovered",
            error_code="BACKGROUND_TASKS_RECOVERED",
            component="data_access.tasks",
            operation="initialize",
            recovered_count=recovered_tasks,
        )


def with_sales_role_exclusion_keywords(keywords):
    keywords = clean_text(keywords or "")
    if "Account Executive" in keywords or SALES_ROLE_EXCLUSION_QUERY in keywords:
        return keywords
    return clean_text(f"{keywords} {SALES_ROLE_EXCLUSION_QUERY}")


def with_sales_role_exclusion_criteria(criteria):
    criteria = clean_text(criteria or "")
    if "Account Executive" in criteria and "sales roles" in criteria.lower():
        return criteria
    return clean_text(f"{criteria} {SALES_ROLE_EXCLUSION_CRITERIA}")


def normalize_pipeline(value, fallback=""):
    """Return a valid pipeline string from model or request data."""
    if isinstance(value, str):
        candidate = value.strip()
        return candidate if candidate in PIPELINES else fallback
    if isinstance(value, (list, tuple, set)):
        for candidate in value:
            if isinstance(candidate, str) and candidate.strip() in PIPELINES:
                return candidate.strip()
    return fallback


def seed_search_queries(conn):
    prepared_queries = [
        {
            **query,
            "criteria": with_sales_role_exclusion_criteria(query["criteria"]),
            "keywords": with_sales_role_exclusion_keywords(query["keywords"]),
        }
        for query in DEFAULT_SEARCH_QUERIES
    ]
    INFRASTRUCTURE.search_mutations.seed_queries(conn, prepared_queries, now())


def remove_hardcoded_level_equivalency_seeds(conn):
    INFRASTRUCTURE.search_mutations.remove_legacy_level_equivalency_seeds(conn)


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


def update_background_task(task_id, **updates):
    return background_task_service().update(task_id, **updates)


def update_background_task_item(task_id, job_id, **updates):
    return background_task_service().update_item(task_id, job_id, **updates)


def start_background_task(operation, job_ids):
    return background_task_service().start(operation, job_ids)


def process_background_task_item(claim):
    """Worker callback delegated to the application-layer task dispatcher."""
    return compose_task_execution_service(
        console_query_service(), codex_scoring_workflow(), packet_generation_service()
    ).process(claim)


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


def career_context():
    manual = read_optional_text(CAREER_MANUAL_PATH)
    guidance = read_optional_text(GUIDANCE_PATH)
    return textwrap.shorten(manual, width=9000, placeholder="\n[manual truncated]\n") + "\n\n" + guidance


def repo_relative(path):
    return path.resolve().relative_to(ROOT.resolve()).as_posix()


def application_packet_abs_path(relative_path):
    if not relative_path:
        return None
    candidate = (ROOT / relative_path).resolve()
    applications_root = APPLICATIONS_DIR.resolve()
    if candidate != applications_root and applications_root not in candidate.parents:
        raise ValueError("Application packet path must be under applications/.")
    if not candidate.exists() or not candidate.is_dir():
        raise ValueError("Application packet folder does not exist.")
    return candidate


def list_markdown_files(packet_dir):
    files = []
    for path in sorted(packet_dir.glob("*.md")):
        if path.is_file():
            files.append(path.name)
    return files


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


def application_packet_slug(job):
    company = re.sub(r"[^a-z0-9]+", "-", (job.get("company") or "unknown-company").lower()).strip("-")
    title = re.sub(r"[^a-z0-9]+", "-", (job.get("title") or "unknown-role").lower()).strip("-")
    identifier = re.sub(r"[^a-z0-9]+", "-", (job.get("source_job_id") or str(job.get("id") or "job")).lower()).strip(
        "-"
    )
    return f"{datetime.now().strftime('%Y-%m')}-{company[:60]}-{title[:90]}-{identifier[:40]}"


def application_packet_rules():
    manual = read_optional_text(CAREER_MANUAL_PATH)
    if not manual:
        return ""
    start = manual.find("# Downstream Artifact Rules")
    end = manual.find("# Open Questions", start)
    return manual[start : end if end >= 0 else None].strip() if start >= 0 else ""


def application_packet_context(job):
    master_resume = read_optional_text(MASTER_RESUME_PATH)
    return {
        "packet_creation_date": datetime.now().date().isoformat(),
        "job": {
            "id": job.get("id"),
            "company": job.get("company"),
            "title": job.get("title"),
            "location": job.get("location"),
            "url": job.get("url"),
            "pipeline": job.get("pipeline"),
            "source_board": job.get("source_board"),
            "posting_text": job.get("posting_text")
            or "No posting text was captured. Do not invent requirements beyond the role title and metadata.",
        },
        "application_packet_rules": application_packet_rules(),
        "master_resume": master_resume,
    }


def validate_application_packet_payload(payload):
    if not isinstance(payload, dict):
        raise ValueError("Codex packet response must be a JSON object.")
    required = ("job_brief_markdown", "resume_markdown", "cover_letter_markdown")
    missing = [field for field in required if not isinstance(payload.get(field), str) or not payload[field].strip()]
    if missing:
        raise ValueError(f"Codex packet response is missing required Markdown fields: {', '.join(missing)}.")
    return {field: payload[field].strip() + "\n" for field in required}


def application_packet_has_model_attribution(payload, model):
    return all(model in content for content in payload.values())


def write_application_packet_documents(packet_dir, payload):
    return compose_packet_document_writer().write(packet_dir, payload)


def generate_application_packet_with_codex(job):
    if not job.get("url"):
        raise ValueError("Job does not have a URL for Codex packet generation.")
    if not codex_cli_available():
        raise RuntimeError(
            f"Codex CLI is unavailable at {codex_cli_path()!r}. Set CODEX_CLI_PATH or install Codex CLI."
        )

    context = application_packet_context(job)
    prompt = {
        "task": "Generate exactly one application packet as JSON. Do not access the network or filesystem; use only the supplied context.",
        "workflow": [
            "First formulate the job brief, including high-signal requirements, tailoring strategy, achievement map, and likely objections.",
            "Then draft one tailored resume and one cover letter using only source-backed evidence from the supplied master resume and rules.",
            "Finally append an objection remediation outcome to the job brief. Perform this remediation cycle once only.",
        ],
        "output_contract": {
            "job_brief_markdown": "Complete Job-Brief.md content. Include source trace naming the supplied Career Manual, Master Resume, and local tracked job.",
            "resume_markdown": "Complete Resume.md content. One employer-facing, ATS-readable tailored resume.",
            "cover_letter_markdown": "Complete Cover-Letter.md content. Direct, practical, evidence-oriented, and low hype.",
        },
        "constraints": [
            "Return only one valid JSON object with exactly the three output_contract keys.",
            "Do not use Markdown fences around the JSON.",
            "Do not create files, propose filenames, or discuss this instruction.",
            "Do not invent accomplishments, metrics, technologies, dates, or domain experience.",
            "Do not generate separate ATS resume artifacts.",
        ],
        "context": context,
    }
    cli_path = codex_cli_path()
    started = time.monotonic()
    error = None
    output_text = ""
    model = codex_model()
    try:
        output_text, model = call_codex_json(
            model, prompt, "generate_application_packet", force_refresh=True, return_metadata=True
        )
        if not model:
            raise RuntimeError("Codex CLI did not report the model used to generate the application packet.")
        payload = validate_application_packet_payload(parse_model_json(output_text))
        if not application_packet_has_model_attribution(payload, model):
            context["codex_generation_metadata"] = {
                "generation_date": datetime.now(UTC).date().isoformat(),
                "model": model,
            }
            output_text, retry_model = call_codex_json(
                model, prompt, "generate_application_packet", force_refresh=True, return_metadata=True
            )
            if retry_model != model:
                raise RuntimeError(
                    "Codex CLI used a different model while regenerating the application packet attribution."
                )
            payload = validate_application_packet_payload(parse_model_json(output_text))
            if not application_packet_has_model_attribution(payload, model):
                raise RuntimeError(
                    "Codex did not include the exact invoked model in every application-packet attribution."
                )
        packet_dir, markdown_files = INFRASTRUCTURE.packet_storage(ROOT, APPLICATIONS_DIR).publish(
            application_packet_slug(job), payload, write_application_packet_documents
        )
        return {"output_text": output_text, "packet_dir": packet_dir, "markdown_files": markdown_files}
    except Exception as exc:
        error = exc
        raise
    finally:
        elapsed_ms = int((time.monotonic() - started) * 1000)
        log_event(
            "codex_cli_application_packet",
            job_id=job.get("id"),
            url=job.get("url"),
            cli_path=cli_path,
            ok=error is None,
            elapsed_ms=elapsed_ms,
            output_excerpt=clean_text(output_text)[:2000] if output_text else None,
            model=model,
            error_code="APPLICATION_PACKET_GENERATION_FAILED" if error else None,
            error_type=type(error).__name__ if error else None,
            message=str(error)[:1000] if error else None,
        )


def _bulk_task_worker(task_id, job_ids, operation, *, running_message, success_verb, error_code, component):
    update_background_task(task_id, status="running", started_at=now(), message=running_message)
    completed = failed = skipped = 0
    for job_id in job_ids:
        update_background_task(task_id, current_job_id=job_id)
        update_background_task_item(task_id, job_id, status="running", message=running_message)
        try:
            status, message = process_background_task_item({"operation": operation, "job_id": job_id})
            if status == "complete":
                completed += 1
            elif status == "skipped":
                skipped += 1
            else:
                failed += 1
            update_background_task_item(task_id, job_id, status=status, message=message)
        except Exception as exc:
            failed += 1
            update_background_task_item(task_id, job_id, status="error", message=str(exc)[:1000])
            log_event(
                error_code.lower(),
                error_code=error_code,
                component=component,
                operation=operation,
                task_id=task_id,
                job_id=job_id,
                error_type=type(exc).__name__,
                message=str(exc)[:1000],
            )
        finally:
            update_background_task(task_id, completed=completed, failed=failed, skipped=skipped)
    status = "complete" if failed == 0 else "error"
    message = f"Complete: {completed} {success_verb}, {skipped} skipped, {failed} failed."
    update_background_task(task_id, status=status, completed_at=now(), current_job_id=None, message=message)
    log_event("background_task_finished", task_id=task_id, operation=operation, status=status, message=message)


def bulk_score_worker(task_id, job_ids):
    return _bulk_task_worker(
        task_id,
        job_ids,
        "scorecards",
        running_message="Scoring with Codex",
        success_verb="scored",
        error_code="BULK_CODEX_SCORE_FAILED",
        component="business.bulk_scoring",
    )


def bulk_packet_worker(task_id, job_ids):
    return _bulk_task_worker(
        task_id,
        job_ids,
        "application_packets",
        running_message="Generating application packet with Codex",
        success_verb="generated",
        error_code="BULK_APPLICATION_PACKET_FAILED",
        component="business.bulk_packets",
    )


def calibration_examples(conn):
    rows = INFRASTRUCTURE.read_models.calibration_examples(conn)
    examples = []
    for row in rows:
        examples.append(
            {
                "company": row["company"],
                "title": row["title"],
                "pipeline": row["pipeline"],
                "gpt_score": row["gpt_score"],
                "user_score": row["user_score"],
                "user_rationale": row["user_rationale"],
                "posting_excerpt": textwrap.shorten(row["posting_text"] or "", width=800, placeholder="..."),
            }
        )
    return examples


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


def fetch_jobs_for_query(query, force_refresh=False):
    try:
        return OUTBOUND_CLIENTS.search_gateway.fetch(
            query["board"], query["keywords"], query["location"], force_refresh=force_refresh
        )
    except ValueError as exc:
        raise RuntimeError(str(exc)) from exc


def score_discovery_with_codex(conn, discovery, force_refresh=False):
    job = {
        "company": discovery.get("company"),
        "title": discovery.get("title"),
        "url": discovery.get("url"),
        "location": discovery.get("location"),
        "pipeline": discovery.get("pipeline") or "",
        "posting_text": discovery.get("snippet"),
        "notes": f"Source board: {discovery.get('board')}. Search criteria: {discovery.get('criteria', '')}",
    }
    return score_with_codex_cli(conn, job, force_refresh=force_refresh)


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
    cli_path = codex_cli_path()
    request_payload = {
        "adapter_version": 2,
        "cli_path": cli_path,
        "model": model,
        "prompt": prompt,
    }
    cached = read_capture("codex_cli", operation, request_payload, force_refresh=force_refresh)
    if cached:
        output_text = cached["response"].get("output_text", "")
        if return_metadata:
            return output_text, cached["response"].get("effective_model", "")
        return output_text

    started = time.monotonic()
    log_event(
        "codex_cli_call_started",
        operation=operation,
        model=model,
        cli_path=cli_path,
        timeout_seconds=CODEX_CLI_TIMEOUT_SECONDS,
    )
    completed = None
    error = None
    output_text = ""
    effective_model = ""
    instruction = (
        "You are a JSON-only engine for a local job-search app.\n"
        "Return only one valid JSON object. Do not include markdown fences, prose, or explanations outside JSON.\n\n"
        f"{json.dumps(prompt, indent=2, sort_keys=True, default=str)}\n"
    )
    try:
        result = INFRASTRUCTURE.codex_gateway(ROOT).execute(cli_path, model, instruction, CODEX_CLI_TIMEOUT_SECONDS)
        completed = result
        output_text = result.output_text
        effective_model = result.effective_model
        if result.returncode != 0:
            raise CodexCliError(operation, result.returncode)
        return (output_text, effective_model) if return_metadata else output_text
    except Exception as exc:
        error = exc
        raise
    finally:
        elapsed_ms = int((time.monotonic() - started) * 1000)
        response_payload = {
            "output_text": output_text,
            "effective_model": effective_model,
            "returncode": completed.returncode if completed is not None else None,
            "stdout_excerpt": clean_text(completed.stdout)[:2000]
            if completed is not None and completed.stdout
            else None,
            "stderr_excerpt": clean_text(completed.stderr)[:2000]
            if completed is not None and completed.stderr
            else None,
            "error_type": type(error).__name__ if error else None,
            "error_message": str(error) if error else None,
        }
        log_event(
            "codex_cli_call_completed",
            operation=operation,
            model=model,
            cli_path=cli_path,
            ok=error is None,
            elapsed_ms=elapsed_ms,
            elapsed_seconds=round(elapsed_ms / 1000, 3),
            returncode=completed.returncode if completed is not None else None,
            error_code="CODEX_CLI_CALL_FAILED" if error else None,
            error_type=type(error).__name__ if error else None,
            message=str(error)[:1000] if error else None,
        )
        write_capture("codex_cli", operation, request_payload, response_payload, {"elapsed_ms": elapsed_ms})


def score_with_codex_cli(conn, job, force_refresh=False):
    """Invoke the transport adapter with application-owned score policy."""
    if not gpt_scoring_enabled():
        raise RuntimeError("Codex scoring is currently disabled. Set JOB_SEARCH_ENABLE_GPT_SCORING=1 to re-enable it.")
    if not codex_cli_available():
        raise RuntimeError(
            f"Codex CLI is unavailable at {codex_cli_path()!r}. Set CODEX_CLI_PATH or install Codex CLI before scoring."
        )
    policy = JobScoringPolicy(
        pipelines=PIPELINES, rubric_fields=RUBRIC_FIELDS, level_reference=ORACLE_IC6_LEVEL_REFERENCE
    )
    return policy.score(
        job,
        career_context=career_context(),
        calibration_examples=calibration_examples(conn),
        invoke=lambda prompt: call_codex_json(codex_model(conn), prompt, "score_job", force_refresh=force_refresh),
    )


def packet_generation_service():
    def generate(job):
        result = generate_application_packet_with_codex(job)
        packet_dir = result["packet_dir"]
        return {
            "path": repo_relative(packet_dir),
            "name": packet_dir.name,
            "markdown_files": result.get("markdown_files", list_markdown_files(packet_dir)),
            "codex_output": result.get("output_text", ""),
        }

    return compose_packet_generation_service(
        connect,
        get_job,
        generate,
        INFRASTRUCTURE.search_mutations.save_application_packet_path,
        now,
        log_event,
    )


def create_application_packet(_connection, job_id):
    """Compatibility entry point backed by the application workflow."""
    return packet_generation_service().generate(job_id)


def codex_scoring_workflow():
    return compose_codex_scoring_workflow(
        connect,
        get_job,
        score_with_codex_cli,
        normalize_pipeline,
        INFRASTRUCTURE.search_mutations.save_codex_score,
        apply_filter,
        now,
        log_event,
    )


def populate_codex_score(conn, job_id, force_refresh=False):
    """Compatibility entry point backed by the application workflow."""
    return codex_scoring_workflow().populate(conn, job_id, force_refresh=force_refresh)


class _DiscoveryAdapter:
    @staticmethod
    def scoring_enabled():
        return gpt_scoring_enabled()

    @staticmethod
    def scorer_available():
        return codex_cli_available()

    @staticmethod
    def scorer_path():
        return codex_cli_path()

    @staticmethod
    def log(event, **fields):
        log_event(event, **fields)

    @staticmethod
    def score(connection, result, *, force_refresh):
        return score_discovery_with_codex(connection, result, force_refresh=force_refresh)

    @staticmethod
    def create(connection, values):
        return INFRASTRUCTURE.search_mutations.create_discovery_job(connection, values)

    @staticmethod
    def apply_filter(connection, job_id):
        apply_filter(connection, job_id)

    @staticmethod
    def now():
        return now()

    @staticmethod
    def normalize_pipeline(value, fallback):
        return normalize_pipeline(value, fallback)

    @staticmethod
    def refinement_context(connection, query_id):
        return INFRASTRUCTURE.read_models.query_refinement_context(connection, query_id)

    @staticmethod
    def refinement_prompt(query, recent):
        return {
            "task": "Refine a job-board search query for Eric Peterson.",
            "instructions": [
                "Return JSON only.",
                "Keep the same job board and pipeline.",
                "Improve the keywords for high-scoring roles at IC6-equivalent or higher scope.",
                "Avoid downlevel, low-score, Account Executive, and other sales results.",
            ],
            "level_reference": {
                "canonical_source": "local Oracle IC6 target definition",
                "oracle_ic6_definition": ORACLE_IC6_LEVEL_REFERENCE,
            },
            "pipeline": query.get("pipeline"),
            "pipeline_criteria": query.get("criteria")
            or PIPELINE_CRITERIA.get(query.get("pipeline"), {}).get("description", ""),
            "current_keywords": query.get("keywords"),
            "location": query.get("location"),
            "recent_results": recent,
            "expected_json_schema": {
                "keywords": "updated search query string",
                "location": "updated location string or current location",
                "criteria": "updated short criteria description",
                "refinement_notes": "what changed and why",
            },
        }

    @staticmethod
    def refine(connection, prompt, *, force_refresh):
        output = call_codex_json(codex_model(connection), prompt, "refine_search_query", force_refresh=force_refresh)
        if not output:
            return None
        try:
            return parse_model_json(output)
        except json.JSONDecodeError as exc:
            log_event(
                "query_refinement_invalid_json",
                error_code="QUERY_REFINEMENT_INVALID_JSON",
                component="business.search_refinement",
                operation="parse_model_json",
                error_type=type(exc).__name__,
            )
            return None

    @staticmethod
    def clean_text(value):
        return clean_text(value)

    @staticmethod
    def update_query(connection, query_id, values):
        INFRASTRUCTURE.search_mutations.update_query(connection, query_id, values)


def discovery_service():
    adapter = _DiscoveryAdapter()
    return compose_discovery_service(
        UNKNOWN_LEVEL_ASSESSMENT,
        scoring_enabled=adapter.scoring_enabled,
        scorer_available=adapter.scorer_available,
        scorer_path=adapter.scorer_path,
        log=adapter.log,
        score=adapter.score,
        create=adapter.create,
        apply_filter=adapter.apply_filter,
        now=adapter.now,
        normalize_pipeline=adapter.normalize_pipeline,
        refinement_context=adapter.refinement_context,
        refinement_prompt=adapter.refinement_prompt,
        refine=adapter.refine,
        clean_text=adapter.clean_text,
        update_query=adapter.update_query,
    )


def refine_search_query(conn, query_id, force_refresh=False):
    """Compatibility entry point backed by the application workflow."""
    return discovery_service().refine_query(conn, query_id, force_refresh=force_refresh)


def classify_discovery(conn, result, force_refresh=False):
    """Compatibility entry point backed by the application workflow."""
    return discovery_service().classify(conn, result, force_refresh=force_refresh)


class _SearchRunAdapter:
    """Composition-edge adapter for the framework-independent search workflow."""

    @staticmethod
    def now():
        return now()

    @staticmethod
    def log(event, **fields):
        log_event(event, **fields)

    @staticmethod
    def repository():
        return search_repository()

    @staticmethod
    def connection():
        return connect()

    @staticmethod
    def fetch(query, *, force_refresh):
        return fetch_jobs_for_query(query, force_refresh=force_refresh)

    @staticmethod
    def reject_reason(result):
        for name, decision in (
            ("sales_role", sales_role_filter_decision),
            ("location", location_filter_decision),
            ("compensation", compensation_filter_decision),
        ):
            allowed, reason = decision(result)
            if not allowed:
                return name, reason
        return None

    @staticmethod
    def level_assessment(connection, result):
        equivalency = lookup_level_equivalency(connection, result.get("company"), result.get("title"))
        if not equivalency:
            return
        result["cached_level_assessment"] = level_assessment_from_equivalency(equivalency)
        result["cached_downlevel"] = bool(equivalency["downlevel"])
        log_event(
            "level_equivalency_matched",
            company=result.get("company"),
            title=result.get("title"),
            oracle_level=equivalency["oracle_level"],
            oracle_title=equivalency["oracle_title"],
            downlevel=bool(equivalency["downlevel"]),
            source_url=equivalency.get("source_url"),
        )

    @staticmethod
    def already_seen_reason(connection, url):
        return already_seen_reason(connection, url)

    @staticmethod
    def classify(connection, result, *, force_refresh):
        return classify_discovery(connection, result, force_refresh=force_refresh)

    @staticmethod
    def refine(connection, query_id, *, force_refresh):
        return refine_search_query(connection, query_id, force_refresh=force_refresh)

    @staticmethod
    def is_refinement_error(error):
        return isinstance(error, CodexCliError)


def run_job_search(trigger="manual", force_refresh=False):
    """Run search through the application-layer orchestration service."""
    adapter = _SearchRunAdapter()
    return compose_search_run_service(
        now=adapter.now,
        log=adapter.log,
        repository=adapter.repository,
        connection=adapter.connection,
        fetch=adapter.fetch,
        reject_reason=adapter.reject_reason,
        level_assessment=adapter.level_assessment,
        already_seen_reason=adapter.already_seen_reason,
        classify=adapter.classify,
        refine=adapter.refine,
        is_refinement_error=adapter.is_refinement_error,
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
    init_db()
    print(f"Job Search Console running at http://{HOST}:{PORT}")
    print(f"Database: {DB_PATH}")
    application.run(host=HOST, port=PORT, debug=DEBUG, use_reloader=False)


if __name__ == "__main__":
    main()
