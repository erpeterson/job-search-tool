#!/usr/bin/env python3
import hashlib
import json
import logging
import os
import re
import shutil
import tempfile
import textwrap
import time
import uuid
from dataclasses import dataclass, field
from datetime import UTC, datetime
from logging.handlers import RotatingFileHandler
from pathlib import Path
from urllib.parse import quote_plus, urljoin, urlparse

from bs4 import BeautifulSoup
from dotenv import load_dotenv
from flask import (
    Flask,
    Response,
    current_app,
    g,
    has_app_context,
    has_request_context,
    jsonify,
    render_template,
    request,
)
from werkzeug.exceptions import HTTPException

from job_search.application.company_service import CompanyService
from job_search.application.discovery_policy import DiscoveryPolicy
from job_search.application.filtering_service import FilteringService
from job_search.application.job_service import JobService
from job_search.application.level_service import LevelService, normalize_lookup_text
from job_search.application.manual_job_service import ManualJobService
from job_search.application.packet_attachment_service import PacketAttachmentService
from job_search.application.rescrape_service import RescrapeService
from job_search.application.scoring_service import ScoringService
from job_search.application.search_query_service import SearchQueryService
from job_search.application.settings_service import SettingsService
from job_search.config import load_runtime_settings
from job_search.data_access.board_gateway import CallableBoardGateway
from job_search.data_access.codex_cli import CodexCliGateway
from job_search.data_access.company_repository import SqliteCompanyRepository
from job_search.data_access.document_writer import PacketDocumentWriter
from job_search.data_access.filter_repository import SqliteJobFilterRepository
from job_search.data_access.http_gateway import CapturingHttpGateway
from job_search.data_access.job_board_parser import JobBoardParser
from job_search.data_access.job_repository import SqliteJobRepository
from job_search.data_access.level_repository import SqliteLevelRepository
from job_search.data_access.packet_storage import PacketStorage
from job_search.data_access.schema import initialize_schema
from job_search.data_access.search_query_repository import SqliteSearchQueryRepository
from job_search.data_access.search_repository import SqliteSearchRepository
from job_search.data_access.settings_repository import SqliteSettingsRepository
from job_search.data_access.sqlite import open_connection
from job_search.domain.filtering import decide_job_filter
from job_search.errors import ClientInputError, translate_exception
from job_search.http_client import SafeHttpClient
from job_search.redaction import redact_content_metadata, redact_headers, redact_url, redact_value
from job_search.security import authorized, csrf_valid, load_request_security, trusted_proxy_peer
from job_search.task_repository import TaskRepository
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

ROOT = Path(__file__).resolve().parents[2]
APP_DIR = ROOT
DB_PATH = APP_DIR / "job_search.sqlite3"
ENV_PATH = APP_DIR / ".env"
LOG_DIR = APP_DIR / "logs"
API_LOG_PATH = LOG_DIR / "api.log"
APP_LOG_PATH = LOG_DIR / "job-search.log"
CAPTURE_DIR = APP_DIR / "captures"
GUIDANCE_PATH = ROOT / "supporting-documents" / "20260731-job-search-guidance.md"
CAREER_MANUAL_PATH = ROOT / "career-manual" / "Career-Manual.md"
MASTER_RESUME_PATH = ROOT / "resume" / "Master-Resume.md"
APPLICATIONS_DIR = ROOT / "applications"

load_dotenv(ENV_PATH)
RUNTIME_SETTINGS = load_runtime_settings(os.environ)

DEFAULT_MODEL = os.environ.get("CODEX_MODEL", "")
DEFAULT_CODEX_CLI_PATH = os.environ.get("CODEX_CLI_PATH") or shutil.which("codex") or "codex"
CODEX_CLI_TIMEOUT_SECONDS = RUNTIME_SETTINGS.codex_timeout_seconds
HOST = RUNTIME_SETTINGS.host
PORT = RUNTIME_SETTINGS.port
DEBUG = RUNTIME_SETTINGS.debug
AUTORUN = False  # the scheduler is buggy and eats codex credits.. disable it for now; os.environ.get("JOB_SEARCH_AUTORUN", "1") != "0"
SEARCH_INTERVAL_SECONDS = RUNTIME_SETTINGS.search_interval_seconds
LOG_MAX_BYTES = RUNTIME_SETTINGS.log_max_bytes
LOG_BACKUP_COUNT = RUNTIME_SETTINGS.log_backup_count
CONFIG_KEYS = [
    "CODEX_CLI_PATH",
    "CODEX_MODEL",
    "JOB_SEARCH_ENABLE_GPT_SCORING",
    "JOB_SEARCH_USE_CAPTURE_CACHE",
]


@dataclass
class PresentationDependencies:
    """Injected application-service factories for HTTP delivery tests and startup."""

    services: dict[str, object] = field(default_factory=dict)


def create_app(test_config=None, dependencies=None):
    """Create the HTTP application and attach explicitly supplied service factories."""
    flask_app = Flask(__name__)
    if test_config:
        flask_app.config.update(test_config)
    flask_app.extensions["job_search.dependencies"] = dependencies or PresentationDependencies()
    return flask_app


app = create_app()
REQUEST_SECURITY = load_request_security(os.environ)
api_logger = logging.getLogger("job_search.api")
api_logger.setLevel(logging.INFO)
api_logger.propagate = False
event_logger = logging.getLogger("job_search.events")
event_logger.setLevel(logging.INFO)
event_logger.propagate = False


@app.before_request
def enforce_request_security():
    """Protect all external bindings before any route can mutate local state."""
    g.correlation_id = request.headers.get("X-Request-ID") or uuid.uuid4().hex
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


@app.before_request
def assign_request_correlation_id():
    # The security hook initializes this first so rejected requests are traced.
    return None


class CodexCliError(RuntimeError):
    def __init__(self, operation, returncode=None):
        self.operation = operation
        self.returncode = returncode
        suffix = f" exited with code {returncode}" if returncode is not None else " failed"
        super().__init__(f"Codex CLI {operation}{suffix}. See logs and captures for details.")


def configure_logging():
    LOG_DIR.mkdir(parents=True, exist_ok=True)
    for logger, path in ((api_logger, API_LOG_PATH), (event_logger, APP_LOG_PATH)):
        if logger.handlers:
            continue
        handler = RotatingFileHandler(path, maxBytes=LOG_MAX_BYTES, backupCount=LOG_BACKUP_COUNT)
        handler.setFormatter(logging.Formatter("%(message)s"))
        logger.addHandler(handler)


configure_logging()

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
OUTBOUND_HTTP_CLIENT = SafeHttpClient()


def connect():
    return open_connection(DB_PATH)


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
    return dependency("job_service", lambda: JobService(SqliteJobRepository(connect), observe=log_event))


def company_service() -> CompanyService:
    return dependency("company_service", lambda: CompanyService(SqliteCompanyRepository(connect)))


def search_query_service() -> SearchQueryService:
    return dependency("search_query_service", lambda: SearchQueryService(SqliteSearchQueryRepository(connect)))


def settings_service() -> SettingsService:
    return dependency("settings_service", lambda: SettingsService(SqliteSettingsRepository(connect)))


def filtering_service() -> FilteringService:
    return dependency(
        "filtering_service",
        lambda: FilteringService(SqliteJobFilterRepository(connect), now, gpt_scoring_enabled=gpt_scoring_enabled()),
    )


def manual_job_service() -> ManualJobService:
    def score(job_id: int) -> object:
        with connect() as connection:
            return populate_codex_score(connection, job_id)

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
        SqliteJobRepository(connect),
        lambda url, force_refresh: scrape_job_from_url(url, force_refresh=force_refresh),
        fallback_job_from_url,
        filtering_service().refresh_job,
        score,
        scoring_availability,
        report_failure,
    )


def rescrape_service() -> RescrapeService:
    return RescrapeService(
        SqliteJobRepository(connect),
        lambda url, force_refresh: scrape_job_from_url(url, force_refresh=force_refresh),
        filtering_service().refresh_job,
        now,
    )


def packet_attachment_service() -> PacketAttachmentService:
    storage = PacketStorage(ROOT, APPLICATIONS_DIR)
    return PacketAttachmentService(SqliteJobRepository(connect), storage.packet_relative_path, now)


def scoring_service() -> ScoringService:
    def score(job_id: int) -> dict[str, object]:
        with connect() as connection:
            return populate_codex_score(connection, job_id)

    def availability() -> str | None:
        if not gpt_scoring_enabled():
            return "Codex scoring is currently disabled. Set JOB_SEARCH_ENABLE_GPT_SCORING=1 to re-enable it."
        if not codex_cli_available():
            return f"Codex CLI is unavailable at {codex_cli_path()!r}."
        return None

    return ScoringService(job_service().get_job, score, availability)


def search_repository() -> SqliteSearchRepository:
    return SqliteSearchRepository(connect)


def discovery_policy() -> DiscoveryPolicy:
    return DiscoveryPolicy(MIN_ANNUAL_COMPENSATION)


def level_service(connection=None) -> LevelService:
    return LevelService(SqliteLevelRepository(connect, connection), now)


def init_db():
    APP_DIR.mkdir(parents=True, exist_ok=True)
    with connect() as conn:
        initialize_schema(conn)
        defaults = {
            "gpt_threshold": "40",
            "user_threshold": "60",
            "codex_model": DEFAULT_MODEL,
            "last_search_at": "0",
        }
        for key, value in defaults.items():
            conn.execute(
                "INSERT OR IGNORE INTO settings(key, value) VALUES (?, ?)",
                (key, value),
            )
        seed_search_queries(conn)
        remove_hardcoded_level_equivalency_seeds(conn)
        conn.execute(
            """
            UPDATE search_queries
            SET enabled = 0
            WHERE seeded = 0
              AND pipeline IS NULL
              AND criteria IS NULL
              AND keywords IN (
                'Chief Architect',
                'Distinguished Engineer',
                'Principal Architect',
                'Office of the CTO',
                'Engineering Strategy',
                'Developer Experience Principal Engineer'
              )
            """
        )

    recovered_tasks = task_repository().initialize(now())
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
    ts = now()
    for query in DEFAULT_SEARCH_QUERIES:
        existing = conn.execute(
            """
            SELECT id, keywords, criteria FROM search_queries
            WHERE board = ? AND pipeline = ? AND seeded = 1
            LIMIT 1
            """,
            (query["board"], query["pipeline"]),
        ).fetchone()
        if existing:
            conn.execute(
                """
                UPDATE search_queries
                SET criteria = ?,
                    keywords = ?,
                    location = COALESCE(location, ?)
                WHERE id = ?
                """,
                (
                    with_sales_role_exclusion_criteria(existing["criteria"] or query["criteria"]),
                    with_sales_role_exclusion_keywords(existing["keywords"] or query["keywords"]),
                    query["location"],
                    existing["id"],
                ),
            )
            continue
        conn.execute(
            """
            INSERT INTO search_queries(board, pipeline, keywords, location, enabled, created_at, criteria, seeded)
            VALUES (?, ?, ?, ?, 1, ?, ?, 1)
            """,
            (
                query["board"],
                query["pipeline"],
                query["keywords"],
                query["location"],
                ts,
                query["criteria"],
            ),
        )


def remove_hardcoded_level_equivalency_seeds(conn):
    conn.execute(
        """
        DELETE FROM level_equivalencies
        WHERE normalized_company = 'atlassian'
          AND normalized_title_pattern = 'principal engineer'
          AND notes LIKE '%user-provided equivalency%'
        """
    )


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
    return {row["key"]: row["value"] for row in conn.execute("SELECT key, value FROM settings")}


def gpt_scoring_enabled():
    return os.environ.get("JOB_SEARCH_ENABLE_GPT_SCORING", "0") == "1"


def codex_cli_path():
    return os.environ.get("CODEX_CLI_PATH") or DEFAULT_CODEX_CLI_PATH


def codex_cli_available():
    path = codex_cli_path()
    if not path:
        return False
    if Path(path).is_absolute():
        return Path(path).exists() and os.access(path, os.X_OK)
    return shutil.which(path) is not None


def codex_model(conn=None):
    env_model = os.environ.get("CODEX_MODEL", "").strip()
    if env_model:
        return env_model
    if conn is not None:
        return (settings(conn).get("codex_model") or "").strip()
    return DEFAULT_MODEL


def capture_cache_enabled():
    return os.environ.get("JOB_SEARCH_USE_CAPTURE_CACHE", "0") == "1"


def full_capture_enabled():
    return os.environ.get("JOB_SEARCH_ENABLE_FULL_CAPTURE", "0") == "1"


def task_repository():
    return TaskRepository(DB_PATH)


def get_background_task(task_id):
    return task_repository().get(task_id)


def list_background_tasks(limit=10):
    return task_repository().list(limit)


def update_background_task(task_id, **updates):
    return task_repository().update(task_id, now(), **updates)


def update_background_task_item(task_id, job_id, **updates):
    return task_repository().update_item(task_id, job_id, now(), **updates)


def start_background_task(operation, job_ids, worker):
    task_id = uuid.uuid4().hex
    created_at = now()
    task = task_repository().create(task_id, operation, job_ids, created_at)
    log_event("background_task_queued", task_id=task_id, operation=operation, job_ids=job_ids)
    return task


def process_background_task_item(claim):
    """Worker callback kept outside HTTP handlers; invoked by ``job_search.worker``."""
    operation, job_id = claim["operation"], claim["job_id"]
    if operation == "scorecards":
        with connect() as conn:
            if not get_job(conn, job_id):
                return "skipped", "Job not found"
            score = populate_codex_score(conn, job_id)
        return "complete", f"Codex score {int(score.get('total_score', 0))}"
    if operation == "application_packets":
        with connect() as conn:
            job = get_job(conn, job_id)
            if not job:
                return "skipped", "Job not found"
            if job.get("application_packet_path"):
                return "skipped", "Application packet already associated"
            packet = create_application_packet(conn, job_id)
        return "complete", packet.get("path") or "Application packet generated"
    return "error", f"Unsupported task operation: {operation}"


def masked_config():
    config = {}
    for key in CONFIG_KEYS:
        value = os.environ.get(key, "")
        if not value:
            config[key] = {"configured": False, "masked": ""}
        elif len(value) <= 8:
            config[key] = {"configured": True, "masked": "********"}
        else:
            config[key] = {"configured": True, "masked": f"{value[:4]}...{value[-4:]}"}
    return config


def update_env_file(updates):
    existing = {}
    order = []
    if ENV_PATH.exists():
        for line in ENV_PATH.read_text(encoding="utf-8").splitlines():
            stripped = line.strip()
            if not stripped or stripped.startswith("#") or "=" not in line:
                order.append((None, line))
                continue
            key, value = line.split("=", 1)
            existing[key] = value
            order.append((key, None))
    for key, value in updates.items():
        existing[key] = str(value)
        if key not in [item[0] for item in order]:
            order.append((key, None))
    lines = []
    seen = set()
    for key, original in order:
        if key is None:
            lines.append(original)
            continue
        if key in seen:
            continue
        seen.add(key)
        lines.append(f"{key}={existing[key]}")
    ENV_PATH.write_text("\n".join(lines).rstrip() + "\n", encoding="utf-8")


def log_api_call(service, method, url, response=None, error=None, elapsed_ms=None):
    status_code = getattr(response, "status_code", None) if response is not None else None
    response_text = getattr(response, "text", "") if response is not None else ""
    event = {
        "ts": datetime.now(UTC).isoformat(),
        "service": service,
        "method": method,
        "url": redact_url(url),
        "status_code": status_code,
        "ok": response is not None and response.ok and error is None,
        "elapsed_ms": elapsed_ms,
        "error_type": type(error).__name__ if error else None,
        "message": redact_value(str(error)[:1000]) if error else None,
        "response_content": redact_content_metadata(response_text),
    }
    api_logger.info(json.dumps(event, sort_keys=True))


def log_event(event_type, **fields):
    event = {
        "ts": datetime.now(UTC).isoformat(),
        "event": event_type,
        "correlation_id": getattr(g, "correlation_id", None) if has_request_context() else None,
        **redact_value(fields),
    }
    event_logger.info(json.dumps(event, sort_keys=True, default=str))


def stable_json(value):
    return json.dumps(value, sort_keys=True, separators=(",", ":"), default=str)


def capture_path(service, operation, request_payload):
    digest = hashlib.sha256(stable_json(request_payload).encode("utf-8")).hexdigest()
    return CAPTURE_DIR / service / operation / f"{digest}.json"


def read_capture(service, operation, request_payload, force_refresh=False):
    if force_refresh:
        log_event("capture_bypass", service=service, operation=operation, reason="force_refresh")
        return None
    if not capture_cache_enabled():
        return None
    path = capture_path(service, operation, request_payload)
    if not path.exists():
        return None
    try:
        capture = json.loads(path.read_text(encoding="utf-8"))
    except json.JSONDecodeError:
        log_event(
            "capture_corruption_recovered",
            error_code="CAPTURE_CORRUPTION_RECOVERED",
            component="data_access.capture",
            operation="read_capture",
            path=str(path),
        )
        return None
    log_event("capture_replay", service=service, operation=operation, path=str(path))
    return capture


def write_capture(service, operation, request_payload, response_payload, metadata=None):
    if not capture_cache_enabled():
        log_event("capture_write_skipped", service=service, operation=operation, reason="capture_disabled")
        return None
    path = capture_path(service, operation, request_payload)
    path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    capture = {
        "captured_at": datetime.now(UTC).isoformat(),
        "service": service,
        "operation": operation,
        "request": redact_value(request_payload, full_capture=full_capture_enabled()),
        "response": redact_value(response_payload, full_capture=full_capture_enabled()),
        "metadata": redact_value(metadata or {}, full_capture=full_capture_enabled()),
    }
    with path.open("w", encoding="utf-8") as capture_file:
        path.chmod(0o600)
        capture_file.write(json.dumps(capture, indent=2, sort_keys=True, default=str) + "\n")
    log_event("capture_write", service=service, operation=operation, path=str(path))
    return path


def parse_model_json(output_text):
    if not output_text:
        raise json.JSONDecodeError("empty response", "", 0)
    cleaned = output_text.strip()
    if cleaned.startswith("```"):
        cleaned = re.sub(r"^```(?:json)?\s*", "", cleaned, flags=re.IGNORECASE)
        cleaned = re.sub(r"\s*```$", "", cleaned)
    try:
        return json.loads(cleaned)
    except json.JSONDecodeError:
        start = cleaned.find("{")
        end = cleaned.rfind("}")
        if start >= 0 and end > start:
            return json.loads(cleaned[start : end + 1])
        raise


def apply_filter(conn, job_id):
    cfg = settings(conn)
    gpt_threshold = int(cfg.get("gpt_threshold", "40"))
    user_threshold = int(cfg.get("user_threshold", "60"))
    use_gpt_threshold = gpt_scoring_enabled()
    job = conn.execute(
        "SELECT company, title, gpt_score, user_score, downlevel FROM jobs WHERE id = ?", (job_id,)
    ).fetchone()
    decision = decide_job_filter(
        row_to_dict(job),
        gpt_threshold=gpt_threshold,
        user_threshold=user_threshold,
        gpt_scoring_enabled=use_gpt_threshold,
    )
    conn.execute("UPDATE jobs SET filtered = ?, updated_at = ? WHERE id = ?", (int(decision.filtered), now(), job_id))
    if job and decision.filtered:
        log_event(
            "job_filtered",
            job_id=job_id,
            company=job["company"],
            title=job["title"],
            reasons=decision.reasons,
            gpt_score=job["gpt_score"],
            user_score=job["user_score"],
            downlevel=bool(job["downlevel"]),
            gpt_scoring_enabled=use_gpt_threshold,
        )


def list_jobs(conn, include_filtered=False):
    query = "SELECT * FROM jobs"
    params = []
    if not include_filtered:
        query += " WHERE filtered = 0"
    query += " ORDER BY updated_at DESC, created_at DESC"
    jobs = []
    for row in conn.execute(query, params):
        item = row_to_dict(row)
        item["gpt_scorecard"] = parse_json_field(item.pop("gpt_scorecard_json"), {})
        item["user_scorecard"] = parse_json_field(item.pop("user_scorecard_json"), {})
        jobs.append(item)
    return jobs


def list_company_interests(conn):
    rows = []
    for row in conn.execute(
        """
        SELECT ci.*,
               COUNT(j.id) AS tracked_job_count,
               MAX(j.updated_at) AS latest_job_updated_at
        FROM company_interests ci
        LEFT JOIN jobs j ON lower(j.company) = lower(ci.company)
        GROUP BY ci.id
        ORDER BY
          CASE ci.status
            WHEN 'target' THEN 0
            WHEN 'watching' THEN 1
            WHEN 'active_conversation' THEN 2
            WHEN 'paused' THEN 3
            WHEN 'not_interested' THEN 4
            ELSE 5
          END,
          COALESCE(ci.interest_score, -1) DESC,
          ci.updated_at DESC
        """
    ):
        rows.append(row_to_dict(row))
    return rows


def get_company_interest(conn, company_id):
    row = conn.execute("SELECT * FROM company_interests WHERE id = ?", (company_id,)).fetchone()
    if not row:
        return None
    company = row_to_dict(row)
    company["jobs"] = [
        row_to_dict(job)
        for job in conn.execute(
            "SELECT id, company, title, url, location, pipeline, status, gpt_score, user_score, filtered, downlevel FROM jobs WHERE lower(company) = lower(?) ORDER BY updated_at DESC",
            (company["company"],),
        )
    ]
    return company


def get_job(conn, job_id):
    row = conn.execute("SELECT * FROM jobs WHERE id = ?", (job_id,)).fetchone()
    if not row:
        return None
    job = row_to_dict(row)
    job["gpt_scorecard"] = parse_json_field(job.pop("gpt_scorecard_json"), {})
    job["user_scorecard"] = parse_json_field(job.pop("user_scorecard_json"), {})
    job["interactions"] = [
        row_to_dict(r)
        for r in conn.execute(
            "SELECT * FROM interactions WHERE job_id = ? ORDER BY occurred_on DESC, id DESC",
            (job_id,),
        )
    ]
    job["notes_list"] = [
        row_to_dict(r)
        for r in conn.execute(
            "SELECT * FROM notes WHERE job_id = ? ORDER BY created_at DESC, id DESC",
            (job_id,),
        )
    ]
    return job


def list_search_queries(conn):
    return [
        row_to_dict(row)
        for row in conn.execute(
            "SELECT * FROM search_queries ORDER BY enabled DESC, seeded DESC, pipeline, board, keywords, location"
        )
    ]


def list_search_runs(conn):
    return [row_to_dict(row) for row in conn.execute("SELECT * FROM search_runs ORDER BY started_at DESC LIMIT 20")]


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
    rows = conn.execute(
        "SELECT * FROM discovered_jobs ORDER BY created_at DESC, id DESC LIMIT ?",
        (limit,),
    ).fetchall()
    discoveries = []
    for row in rows:
        item = row_to_dict(row)
        item["gpt_scorecard"] = parse_json_field(item.pop("gpt_scorecard_json"), {})
        discoveries.append(item)
    return discoveries


def career_context():
    manual = CAREER_MANUAL_PATH.read_text(encoding="utf-8") if CAREER_MANUAL_PATH.exists() else ""
    guidance = GUIDANCE_PATH.read_text(encoding="utf-8") if GUIDANCE_PATH.exists() else ""
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
    APPLICATIONS_DIR.mkdir(parents=True, exist_ok=True)
    associated_rows = [
        row_to_dict(row)
        for row in conn.execute(
            """
            SELECT id, company, title, application_packet_path
            FROM jobs
            WHERE application_packet_path IS NOT NULL
              AND application_packet_path != ''
            """
        )
    ]
    associated_by_path = {row["application_packet_path"]: row for row in associated_rows}
    packets = []
    for path in sorted(APPLICATIONS_DIR.iterdir()):
        if not path.is_dir():
            continue
        markdown_files = list_markdown_files(path)
        if not markdown_files:
            continue
        relative = repo_relative(path)
        associated_job = associated_by_path.get(relative)
        packets.append(
            {
                "path": relative,
                "name": path.name,
                "markdown_files": markdown_files,
                "associated_job": associated_job,
                "unassociated": associated_job is None,
            }
        )
    return packets


def application_packet_slug(job):
    company = re.sub(r"[^a-z0-9]+", "-", (job.get("company") or "unknown-company").lower()).strip("-")
    title = re.sub(r"[^a-z0-9]+", "-", (job.get("title") or "unknown-role").lower()).strip("-")
    identifier = re.sub(r"[^a-z0-9]+", "-", (job.get("source_job_id") or str(job.get("id") or "job")).lower()).strip(
        "-"
    )
    return f"{datetime.now().strftime('%Y-%m')}-{company[:60]}-{title[:90]}-{identifier[:40]}"


def application_packet_rules():
    if not CAREER_MANUAL_PATH.exists():
        return ""
    manual = CAREER_MANUAL_PATH.read_text(encoding="utf-8")
    start = manual.find("# Downstream Artifact Rules")
    end = manual.find("# Open Questions", start)
    return manual[start : end if end >= 0 else None].strip() if start >= 0 else ""


def application_packet_context(job):
    master_resume = MASTER_RESUME_PATH.read_text(encoding="utf-8") if MASTER_RESUME_PATH.exists() else ""
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
    return PacketDocumentWriter().write(packet_dir, payload)


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
        packet_dir = APPLICATIONS_DIR / application_packet_slug(job)
        if packet_dir.exists():
            raise FileExistsError(f"Application packet directory already exists: {repo_relative(packet_dir)}")
        APPLICATIONS_DIR.mkdir(parents=True, exist_ok=True)
        # Publish only after all Markdown and DOCX files were generated successfully.
        with tempfile.TemporaryDirectory(prefix=".packet-staging-", dir=APPLICATIONS_DIR) as staging_root:
            staged_packet_dir = Path(staging_root) / packet_dir.name
            markdown_files = write_application_packet_documents(staged_packet_dir, payload)
            staged_packet_dir.replace(packet_dir)
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


def create_application_packet(conn, job_id):
    job = get_job(conn, job_id)
    if not job:
        raise ValueError("Job not found.")
    result = generate_application_packet_with_codex(job)
    packet_dir = result.get("packet_dir")
    relative = repo_relative(packet_dir)
    conn.execute(
        "UPDATE jobs SET application_packet_path = ?, updated_at = ? WHERE id = ?",
        (relative, now(), job_id),
    )
    log_event("application_packet_generated", job_id=job_id, path=relative, generator="codex_cli")
    return {
        "path": relative,
        "name": packet_dir.name,
        "markdown_files": result.get("markdown_files", list_markdown_files(packet_dir)),
        "codex_output": result.get("output_text", ""),
    }


def bulk_score_worker(task_id, job_ids):
    update_background_task(task_id, status="running", started_at=now(), message="Codex scorecard population running")
    completed = failed = skipped = 0
    with connect() as conn:
        for job_id in job_ids:
            update_background_task(task_id, current_job_id=job_id)
            update_background_task_item(task_id, job_id, status="running", message="Scoring with Codex")
            try:
                job = get_job(conn, job_id)
                if not job:
                    skipped += 1
                    update_background_task_item(task_id, job_id, status="skipped", message="Job not found")
                else:
                    score = populate_codex_score(conn, job_id)
                    completed += 1
                    update_background_task_item(
                        task_id,
                        job_id,
                        status="complete",
                        message=f"Codex score {int(score.get('total_score', 0))}",
                    )
            except Exception as exc:
                failed += 1
                update_background_task_item(task_id, job_id, status="error", message=str(exc)[:1000])
                log_event(
                    "bulk_codex_score_error",
                    error_code="BULK_CODEX_SCORE_FAILED",
                    component="business.bulk_scoring",
                    operation="populate_codex_score",
                    task_id=task_id,
                    job_id=job_id,
                    error_type=type(exc).__name__,
                    message=str(exc)[:1000],
                )
            finally:
                update_background_task(task_id, completed=completed, failed=failed, skipped=skipped)
    status = "complete" if failed == 0 else "error"
    message = f"Complete: {completed} scored, {skipped} skipped, {failed} failed."
    update_background_task(task_id, status=status, completed_at=now(), current_job_id=None, message=message)
    log_event("background_task_finished", task_id=task_id, operation="scorecards", status=status, message=message)


def bulk_packet_worker(task_id, job_ids):
    update_background_task(task_id, status="running", started_at=now(), message="Application packet generation running")
    completed = failed = skipped = 0
    with connect() as conn:
        for job_id in job_ids:
            update_background_task(task_id, current_job_id=job_id)
            update_background_task_item(
                task_id, job_id, status="running", message="Generating application packet with Codex"
            )
            try:
                job = get_job(conn, job_id)
                if not job:
                    skipped += 1
                    update_background_task_item(task_id, job_id, status="skipped", message="Job not found")
                elif job.get("application_packet_path"):
                    skipped += 1
                    update_background_task_item(
                        task_id, job_id, status="skipped", message="Application packet already associated"
                    )
                else:
                    packet = create_application_packet(conn, job_id)
                    completed += 1
                    update_background_task_item(
                        task_id,
                        job_id,
                        status="complete",
                        message=packet.get("path") or packet.get("warning") or "Codex completed",
                    )
            except Exception as exc:
                failed += 1
                update_background_task_item(task_id, job_id, status="error", message=str(exc)[:1000])
                log_event(
                    "bulk_application_packet_error",
                    error_code="BULK_APPLICATION_PACKET_FAILED",
                    component="business.bulk_packets",
                    operation="create_application_packet",
                    task_id=task_id,
                    job_id=job_id,
                    error_type=type(exc).__name__,
                    message=str(exc)[:1000],
                )
            finally:
                update_background_task(task_id, completed=completed, failed=failed, skipped=skipped)
    status = "complete" if failed == 0 else "error"
    message = f"Complete: {completed} generated, {skipped} skipped, {failed} failed."
    update_background_task(task_id, status=status, completed_at=now(), current_job_id=None, message=message)
    log_event(
        "background_task_finished", task_id=task_id, operation="application_packets", status=status, message=message
    )


def calibration_examples(conn):
    rows = conn.execute(
        """
        SELECT company, title, pipeline, gpt_score, user_score, user_rationale, posting_text
        FROM jobs
        WHERE user_score IS NOT NULL
        ORDER BY updated_at DESC
        LIMIT 8
        """
    ).fetchall()
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


def fetch_linkedin_jobs(keywords, location, force_refresh=False):
    url = (
        "https://www.linkedin.com/jobs-guest/jobs/api/seeMoreJobPostings/search"
        f"?keywords={quote_plus(keywords)}&location={quote_plus(location or 'United States')}&f_TPR=r86400&start=0"
    )
    response = fetch_url("linkedin", url, force_refresh=force_refresh)
    response.raise_for_status()
    parser = JobBoardParser(clean_text, clean_url, lambda href: source_id("linkedin", href))
    return dedupe_results(parser.linkedin(response.text, location or ""))


def fetch_indeed_jobs(keywords, location, force_refresh=False):
    url = f"https://www.indeed.com/jobs?q={quote_plus(keywords)}&l={quote_plus(location or 'United States')}&fromage=1&sort=date"
    response = fetch_url("indeed", url, force_refresh=force_refresh)
    response.raise_for_status()
    soup = BeautifulSoup(response.text, "html.parser")
    jobs = []
    for card in soup.select("[data-jk], .job_seen_beacon"):
        link = card.select_one("a[href*='/viewjob'], a.jcs-JobTitle")
        title = card.select_one("h2 span[title], h2 span, .jobTitle span")
        company = card.select_one("[data-testid='company-name'], .companyName")
        location_el = card.select_one("[data-testid='text-location'], .companyLocation")
        href = link.get("href", "") if link else ""
        if href.startswith("/"):
            href = urljoin("https://www.indeed.com", href)
        if not href or not title:
            continue
        jobs.append(
            {
                "board": "indeed",
                "source_job_id": card.get("data-jk") or source_id("indeed", href),
                "company": clean_text(company.get_text(" ")) if company else "",
                "title": clean_text(title.get("title") or title.get_text(" ")),
                "location": clean_text(location_el.get_text(" ")) if location_el else location or "",
                "url": clean_url(href),
                "snippet": clean_text(card.get_text(" "))[:1200],
            }
        )
    return dedupe_results(jobs)


def scrape_job_from_url(url, force_refresh=False):
    cleaned_url = clean_url(url)
    if not cleaned_url:
        raise ValueError("URL is required.")
    service = posting_service_from_url(cleaned_url)
    response = fetch_url(service, cleaned_url, force_refresh=force_refresh)
    response.raise_for_status()
    soup = BeautifulSoup(response.text, "html.parser")
    json_ld = extract_job_json_ld(soup)
    page_title = clean_text(soup.title.get_text(" ")) if soup.title else ""
    title = (
        nested_value(json_ld, "title")
        or selector_text(
            soup,
            [
                "h1",
                ".top-card-layout__title",
                ".jobsearch-JobInfoHeader-title",
                "[data-testid='jobsearch-JobInfoHeader-title']",
            ],
        )
        or meta_content(soup, ["og:title", "twitter:title"])
        or page_title
    )
    company = (
        nested_value(json_ld, "hiringOrganization", "name")
        or selector_text(
            soup,
            [
                ".topcard__org-name-link",
                ".topcard__flavor",
                "[data-testid='inlineHeader-companyName']",
                "[data-company-name]",
                ".jobsearch-InlineCompanyRating-companyHeader a",
            ],
        )
        or meta_content(soup, ["og:site_name"])
    )
    location = location_from_json_ld(json_ld) or selector_text(
        soup,
        [
            ".topcard__flavor--bullet",
            ".job-search-card__location",
            "[data-testid='job-location']",
            ".jobsearch-JobInfoHeader-subtitle div",
        ],
    )
    description = (
        nested_value(json_ld, "description")
        or selector_text(
            soup,
            [
                "#job-details",
                ".show-more-less-html__markup",
                "#jobDescriptionText",
                "[data-testid='jobDescriptionText']",
            ],
        )
        or clean_text(soup.get_text(" "))[:5000]
    )
    posting_text = clean_text(BeautifulSoup(description or "", "html.parser").get_text(" "))
    return {
        "company": clean_text(company) or "Unknown company",
        "title": clean_text(title) or "Unknown title",
        "location": clean_text(location),
        "url": cleaned_url,
        "posting_text": posting_text[:12000],
        "source_board": service if service in ("linkedin", "indeed") else "manual",
        "source_job_id": source_id(service, cleaned_url),
    }


def fallback_job_from_url(url):
    parsed = urlparse(url)
    host = parsed.netloc.replace("www.", "")
    service = posting_service_from_url(url)
    return {
        "company": host or "Unknown company",
        "title": f"Job posting from {host}" if host else "Unknown title",
        "location": "",
        "url": clean_url(url),
        "posting_text": "",
        "source_board": service if service in ("linkedin", "indeed") else "manual",
        "source_job_id": source_id(service, url),
    }


def posting_service_from_url(url):
    lower = (url or "").lower()
    if "linkedin." in lower:
        return "linkedin"
    if "indeed." in lower:
        return "indeed"
    return "manual_posting"


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


def request_headers():
    return {
        "User-Agent": "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/126 Safari/537.36",
        "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
        "Accept-Language": "en-US,en;q=0.9",
    }


def fetch_url(service, url, force_refresh=False):
    gateway = CapturingHttpGateway(
        OUTBOUND_HTTP_CLIENT, request_headers, read_capture, write_capture, log_api_call, redact_headers
    )
    return gateway.get(service, url, force_refresh=force_refresh)


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


def fetch_jobs_for_query(query, force_refresh=False):
    gateway = CallableBoardGateway(fetch_linkedin_jobs, fetch_indeed_jobs)
    try:
        return gateway.fetch(query["board"], query["keywords"], query["location"], force_refresh=force_refresh)
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
    if conn.execute("SELECT 1 FROM jobs WHERE url = ? LIMIT 1", (url,)).fetchone():
        return True
    return False


def already_seen_reason(conn, url):
    if not url:
        return None
    if conn.execute("SELECT 1 FROM jobs WHERE url = ? LIMIT 1", (url,)).fetchone():
        return "already tracked in jobs"
    return None


def location_filter_decision(result):
    return discovery_policy().location(result)


def compensation_filter_decision(result):
    return discovery_policy().compensation(result)


def sales_role_filter_decision(result):
    return discovery_policy().sales_role(result)


def run_job_search(trigger="manual", force_refresh=False):
    started = now()
    log_event("search_started", trigger=trigger, force_refresh=force_refresh)
    repository = search_repository()
    run_id = repository.start_run(started, trigger)
    queries = repository.enabled_queries()

    found_count = 0
    tracked_count = 0
    rejected_count = 0
    messages = []

    def record_rejection(query, result, reason, connection):
        repository.record_discovery(
            {
                "run_id": run_id,
                "query_id": query["id"],
                "created_at": now(),
                "board": result.get("board"),
                "source_job_id": result.get("source_job_id"),
                "company": result.get("company"),
                "title": result.get("title"),
                "location": result.get("location"),
                "url": result.get("url"),
                "snippet": result.get("snippet"),
                "gpt_score": None,
                "gpt_rationale": None,
                "gpt_scorecard_json": "{}",
                "level_assessment": "",
                "downlevel": 0,
                "decision": "rejected",
                "rejection_reason": reason,
                "tracked_job_id": None,
            },
            connection=connection,
        )

    for query in queries:
        try:
            results = fetch_jobs_for_query(query, force_refresh=force_refresh)
        except Exception as exc:
            messages.append(f"{query['board']}:{query['keywords']}: {exc}")
            log_event(
                "job_search_query_failed",
                error_code="JOB_SEARCH_QUERY_FAILED",
                component="business.search",
                operation="fetch_jobs_for_query",
                query_id=query.get("id"),
                board=query.get("board"),
                error_type=type(exc).__name__,
                message=str(exc)[:1000],
            )
            continue

        repository.mark_query_run(query["id"], now())

        for result in results:
            result["pipeline"] = query.get("pipeline") or result.get("pipeline") or ""
            result["criteria"] = query.get("criteria") or ""
            found_count += 1
            with connect() as conn:
                sales_allowed, sales_reason = sales_role_filter_decision(result)
                if not sales_allowed:
                    rejected_count += 1
                    log_event(
                        "discovery_rejected",
                        reason=sales_reason,
                        filter="sales_role",
                        board=result.get("board"),
                        company=result.get("company"),
                        title=result.get("title"),
                        location=result.get("location"),
                        url=result.get("url"),
                        query_id=query["id"],
                        run_id=run_id,
                    )
                    record_rejection(query, result, sales_reason, conn)
                    continue
                location_allowed, location_reason = location_filter_decision(result)
                if not location_allowed:
                    rejected_count += 1
                    log_event(
                        "discovery_rejected",
                        reason=location_reason,
                        filter="location",
                        board=result.get("board"),
                        company=result.get("company"),
                        title=result.get("title"),
                        location=result.get("location"),
                        url=result.get("url"),
                        query_id=query["id"],
                        run_id=run_id,
                    )
                    record_rejection(query, result, location_reason, conn)
                    continue
                compensation_allowed, compensation_reason = compensation_filter_decision(result)
                if not compensation_allowed:
                    rejected_count += 1
                    log_event(
                        "discovery_rejected",
                        reason=compensation_reason,
                        filter="compensation",
                        board=result.get("board"),
                        company=result.get("company"),
                        title=result.get("title"),
                        location=result.get("location"),
                        url=result.get("url"),
                        query_id=query["id"],
                        run_id=run_id,
                    )
                    record_rejection(query, result, compensation_reason, conn)
                    continue
                level_equivalency = lookup_level_equivalency(conn, result.get("company"), result.get("title"))
                if level_equivalency:
                    level_reason = level_assessment_from_equivalency(level_equivalency)
                    result["cached_level_assessment"] = level_reason
                    result["cached_downlevel"] = bool(level_equivalency["downlevel"])
                    log_event(
                        "level_equivalency_matched",
                        company=result.get("company"),
                        title=result.get("title"),
                        oracle_level=level_equivalency["oracle_level"],
                        oracle_title=level_equivalency["oracle_title"],
                        downlevel=bool(level_equivalency["downlevel"]),
                        source_url=level_equivalency.get("source_url"),
                    )
                seen_reason = already_seen_reason(conn, result.get("url"))
                if seen_reason:
                    log_event(
                        "discovery_skipped",
                        reason=seen_reason,
                        board=result.get("board"),
                        company=result.get("company"),
                        title=result.get("title"),
                        url=result.get("url"),
                        query_id=query["id"],
                        run_id=run_id,
                    )
                    continue
                decision, reason, tracked_job_id, score, scorecard, level_assessment, downlevel = classify_discovery(
                    conn, result, force_refresh=force_refresh
                )
                if decision != "tracked":
                    rejected_count += 1
                else:
                    tracked_count += 1
                log_event(
                    "discovery_decision",
                    decision=decision,
                    reason=reason,
                    board=result.get("board"),
                    company=result.get("company"),
                    title=result.get("title"),
                    url=result.get("url"),
                    query_id=query["id"],
                    run_id=run_id,
                    tracked_job_id=tracked_job_id,
                    gpt_score=score.get("total_score") if score else None,
                    level_assessment=level_assessment,
                    downlevel=downlevel,
                )
                repository.record_discovery(
                    {
                        "run_id": run_id,
                        "query_id": query["id"],
                        "created_at": now(),
                        "board": result.get("board"),
                        "source_job_id": result.get("source_job_id"),
                        "company": result.get("company"),
                        "title": result.get("title"),
                        "location": result.get("location"),
                        "url": result.get("url"),
                        "snippet": result.get("snippet"),
                        "gpt_score": score.get("total_score") if score else None,
                        "gpt_rationale": score.get("rationale") if score else None,
                        "gpt_scorecard_json": json.dumps(scorecard or {}),
                        "level_assessment": level_assessment,
                        "downlevel": int(downlevel),
                        "decision": decision,
                        "rejection_reason": reason,
                        "tracked_job_id": tracked_job_id,
                    },
                    connection=conn,
                )
        with connect() as conn:
            try:
                refine_search_query(conn, query["id"], force_refresh=force_refresh)
            except CodexCliError as exc:
                messages.append(f"{query['board']}:{query['keywords']}: {exc}")
                log_event(
                    "query_refinement_failed",
                    query_id=query["id"],
                    board=query.get("board"),
                    keywords=query.get("keywords"),
                    error_type=type(exc).__name__,
                    message=str(exc),
                )

    completed_at = now()
    message = "\n".join(messages)
    run = repository.finish_run(
        run_id,
        {
            "completed_at": completed_at,
            "message": message,
            "found_count": found_count,
            "tracked_count": tracked_count,
            "rejected_count": rejected_count,
        },
    )
    log_event(
        "search_completed",
        trigger=trigger,
        force_refresh=force_refresh,
        run_id=run_id,
        found_count=found_count,
        tracked_count=tracked_count,
        rejected_count=rejected_count,
        message=message,
    )
    return run


def refine_search_query(conn, query_id, force_refresh=False):
    if not gpt_scoring_enabled():
        log_event("query_refinement_skipped", query_id=query_id, reason="Codex scoring disabled")
        return
    if not codex_cli_available():
        log_event(
            "query_refinement_skipped",
            query_id=query_id,
            reason="Codex CLI unavailable",
            codex_cli_path=codex_cli_path(),
        )
        return
    query = row_to_dict(conn.execute("SELECT * FROM search_queries WHERE id = ?", (query_id,)).fetchone())
    if not query:
        return
    recent = [
        row_to_dict(row)
        for row in conn.execute(
            """
            SELECT company, title, location, gpt_score, level_assessment, downlevel, decision, rejection_reason, gpt_rationale
            FROM discovered_jobs
            WHERE query_id = ?
            ORDER BY created_at DESC
            LIMIT 20
            """,
            (query_id,),
        )
    ]
    if not recent:
        return
    prompt = {
        "task": "Refine a job-board search query for Eric Peterson.",
        "instructions": [
            "Return JSON only.",
            "Keep the same job board and pipeline.",
            "Improve the keywords so the next run is more likely to find high-scoring roles for this pipeline.",
            "Prefer query terms that imply Oracle IC6 Architect-equivalent or higher scope.",
            "Avoid terms that produced downlevel or low-score results.",
            "Explicitly exclude Account Executive and other sales roles.",
            "Keep the query concise enough for LinkedIn or Indeed public search boxes.",
            "Do not use Eric's personal LinkedIn or Indeed profile data.",
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
    output_text = call_codex_json(codex_model(conn), prompt, "refine_search_query", force_refresh=force_refresh)
    if not output_text:
        return
    try:
        refined = parse_model_json(output_text)
    except json.JSONDecodeError as exc:
        log_event(
            "query_refinement_invalid_json",
            error_code="QUERY_REFINEMENT_INVALID_JSON",
            component="business.search_refinement",
            operation="parse_model_json",
            query_id=query_id,
            error_type=type(exc).__name__,
        )
        return
    keywords = clean_text(refined.get("keywords") or query.get("keywords"))
    location = clean_text(refined.get("location") or query.get("location") or "Remote")
    criteria = clean_text(refined.get("criteria") or query.get("criteria") or "")
    notes = clean_text(refined.get("refinement_notes") or "")
    if not keywords:
        return
    conn.execute(
        """
        UPDATE search_queries
        SET keywords = ?, location = ?, criteria = ?, refinement_notes = ?
        WHERE id = ?
        """,
        (keywords, location, criteria, notes, query_id),
    )


def classify_discovery(conn, result, force_refresh=False):
    if not gpt_scoring_enabled():
        reason = "Codex scoring is disabled; discovery tracked without Codex score."
        log_event(
            "discovery_codex_disabled",
            board=result.get("board"),
            company=result.get("company"),
            title=result.get("title"),
            url=result.get("url"),
            reason=reason,
        )
        job_id = track_discovery_without_gpt(conn, result, reason)
        return (
            "tracked",
            reason,
            job_id,
            None,
            {},
            result.get("cached_level_assessment", ""),
            bool(result.get("cached_downlevel")),
        )
    if not codex_cli_available():
        reason = "Codex CLI is unavailable; discovery tracked without Codex score."
        log_event(
            "discovery_codex_cli_unavailable",
            board=result.get("board"),
            company=result.get("company"),
            title=result.get("title"),
            url=result.get("url"),
            reason=reason,
            codex_cli_path=codex_cli_path(),
        )
        job_id = track_discovery_without_gpt(conn, result, reason)
        return (
            "tracked",
            reason,
            job_id,
            None,
            {},
            result.get("cached_level_assessment", ""),
            bool(result.get("cached_downlevel")),
        )

    score = score_discovery_with_codex(conn, result, force_refresh=force_refresh)
    scorecard = score.get("scorecard", {})
    total = int(score.get("total_score", 0))
    downlevel = bool(score.get("downlevel", False))
    level_assessment = (
        score.get("level_assessment", "") or result.get("cached_level_assessment", "") or UNKNOWN_LEVEL_ASSESSMENT
    )
    pipeline = normalize_pipeline(score.get("pipeline"), result.get("pipeline", ""))

    if result.get("cached_downlevel"):
        downlevel = True

    if downlevel and total < 80:
        log_event(
            "discovery_downlevel_tracked",
            reason="Downlevel relative to IC6-equivalent; tracked and hidden by default.",
            company=result.get("company"),
            title=result.get("title"),
            url=result.get("url"),
            gpt_score=total,
            level_assessment=level_assessment,
            downlevel=downlevel,
        )

    ts = now()
    cur = conn.execute(
        """
        INSERT INTO jobs(
            created_at, updated_at, company, title, url, location, pipeline, status, posting_text, notes,
            gpt_score, gpt_rationale, gpt_scorecard_json, filtered, source_board, source_job_id,
            discovered_at, level_assessment, downlevel
        )
        VALUES (?, ?, ?, ?, ?, ?, ?, 'discovered', ?, ?, ?, ?, ?, 0, ?, ?, ?, ?, ?)
        """,
        (
            ts,
            ts,
            result.get("company") or "Unknown company",
            result.get("title") or "Unknown title",
            result.get("url"),
            result.get("location"),
            pipeline,
            result.get("snippet"),
            "Auto-discovered from job search.",
            total,
            score.get("rationale", ""),
            json.dumps(scorecard),
            result.get("board"),
            result.get("source_job_id"),
            ts,
            level_assessment,
            1 if downlevel else 0,
        ),
    )
    job_id = cur.lastrowid
    apply_filter(conn, job_id)
    return "tracked", "", job_id, score, scorecard, level_assessment, downlevel


def track_discovery_without_gpt(conn, result, reason):
    ts = now()
    pipeline = result.get("pipeline", "")
    level_assessment = result.get("cached_level_assessment", "") or UNKNOWN_LEVEL_ASSESSMENT
    downlevel = bool(result.get("cached_downlevel"))
    cur = conn.execute(
        """
        INSERT INTO jobs(
            created_at, updated_at, company, title, url, location, pipeline, status, posting_text, notes,
            filtered, source_board, source_job_id, discovered_at, level_assessment, downlevel
        )
        VALUES (?, ?, ?, ?, ?, ?, ?, 'discovered', ?, ?, 0, ?, ?, ?, ?, ?)
        """,
        (
            ts,
            ts,
            result.get("company") or "Unknown company",
            result.get("title") or "Unknown title",
            result.get("url"),
            result.get("location"),
            pipeline,
            result.get("snippet"),
            f"Auto-discovered from job search. {reason}",
            result.get("board"),
            result.get("source_job_id"),
            ts,
            level_assessment,
            1 if downlevel else 0,
        ),
    )
    job_id = cur.lastrowid
    apply_filter(conn, job_id)
    return job_id


def score_with_codex_cli(conn, job, force_refresh=False):
    if not gpt_scoring_enabled():
        raise RuntimeError("Codex scoring is currently disabled. Set JOB_SEARCH_ENABLE_GPT_SCORING=1 to re-enable it.")
    if not codex_cli_available():
        raise RuntimeError(
            f"Codex CLI is unavailable at {codex_cli_path()!r}. Set CODEX_CLI_PATH or install Codex CLI before scoring."
        )

    model = codex_model(conn)
    prompt = {
        "task": "Score this job for Eric Peterson's job search.",
        "level_reference": {
            "canonical_source": "local Oracle IC6 target definition",
            "oracle_ic6_definition": ORACLE_IC6_LEVEL_REFERENCE,
        },
        "instructions": [
            "Return JSON only.",
            "Use a 0-100 total fit score.",
            "Score each rubric item from 0-10.",
            "Reward cross-cutting architecture, organizational scaling, engineering effectiveness, developer experience, AI-enabled development, technical strategy, and technical decision quality.",
            "Penalize line management, heavy operational ownership, firefighting, incremental feature ownership, narrow service ownership, and roles that only value hands-on coding.",
            "Reject or heavily penalize Account Executive, account management, business development, quota-carrying, and other sales roles.",
            "Use the calibration examples to adjust future scoring toward Eric's own scores.",
            "Classify whether this role appears Oracle IC6-equivalent or higher using the local target definition: Oracle IC-6 is Architect.",
            "Treat Principal Engineer, Architect, Senior Principal Engineer, Distinguished Engineer, Fellow, Chief Architect, CTO advisor, and equivalent strategic IC roles as potentially IC6-equivalent or higher depending on scope.",
            "Treat ordinary software engineer, senior engineer, staff engineer with narrow feature ownership, line-management-heavy manager roles, and single-service owner roles as downlevel unless the posting clearly indicates Architect-equivalent broad cross-org technical influence.",
            "Do not invent facts missing from the posting.",
        ],
        "expected_json_schema": {
            "total_score": "integer 0-100",
            "pipeline": f"one of: {', '.join(PIPELINES)}",
            "scorecard": {field: "integer 0-10" for field in RUBRIC_FIELDS},
            "level_assessment": "short phrase",
            "downlevel": "boolean",
            "rationale": "short paragraph",
            "strengths": ["short bullets"],
            "risks": ["short bullets"],
            "recommended_next_step": "short sentence",
        },
        "career_context": career_context(),
        "calibration_examples": calibration_examples(conn),
        "job": {
            "company": job["company"],
            "title": job["title"],
            "url": job["url"],
            "location": job["location"],
            "pipeline": job["pipeline"],
            "posting_text": job["posting_text"],
            "notes": job["notes"],
        },
    }
    output_text = call_codex_json(model, prompt, "score_job", force_refresh=force_refresh)
    if not output_text:
        output_text = ""
    if not output_text:
        raise RuntimeError("Codex CLI response did not include text output.")
    try:
        parsed = parse_model_json(output_text)
    except json.JSONDecodeError as exc:
        raise RuntimeError(f"Codex CLI response was not valid JSON: {output_text[:1000]}") from exc
    return parsed


def populate_codex_score(conn, job_id, force_refresh=False):
    job = get_job(conn, job_id)
    if not job:
        raise ValueError("Job not found")
    score = score_with_codex_cli(conn, job, force_refresh=force_refresh)
    total = int(score.get("total_score", 0))
    scorecard = score.get("scorecard", {})
    downlevel = bool(score.get("downlevel", False))
    pipeline = normalize_pipeline(score.get("pipeline"), job.get("pipeline", ""))
    conn.execute(
        """
        UPDATE jobs
        SET gpt_score = ?, gpt_rationale = ?, gpt_scorecard_json = ?,
            pipeline = COALESCE(NULLIF(?, ''), pipeline),
            level_assessment = ?, downlevel = ?, updated_at = ?
        WHERE id = ?
        """,
        (
            total,
            score.get("rationale", ""),
            json.dumps(scorecard),
            pipeline,
            score.get("level_assessment", ""),
            1 if downlevel else 0,
            now(),
            job_id,
        ),
    )
    apply_filter(conn, job_id)
    log_event("codex_score_populated", job_id=job_id, total_score=total, downlevel=downlevel)
    return score


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
        result = CodexCliGateway(ROOT).execute(cli_path, model, instruction, CODEX_CLI_TIMEOUT_SECONDS)
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


@app.get("/")
def index():
    return render_template("index.html")


@app.get("/api/state")
def api_state():
    include_filtered = request.args.get("include_filtered") == "1"
    with connect() as conn:
        return jsonify(
            {
                "settings": settings(conn),
                "config": masked_config(),
                "api_log_path": str(API_LOG_PATH),
                "event_log_path": str(APP_LOG_PATH),
                "capture_dir": str(CAPTURE_DIR),
                "gpt_scoring_enabled": gpt_scoring_enabled(),
                "capture_cache_enabled": capture_cache_enabled(),
                "jobs": list_jobs(conn, include_filtered=include_filtered),
                "company_interests": list_company_interests(conn),
                "search_queries": list_search_queries(conn),
                "search_runs": list_search_runs(conn),
                "search_schedule": search_schedule_state(conn),
                "discoveries": list_discoveries(conn),
                "application_packets": list_application_packets(conn),
                "codex_tasks": list_background_tasks(),
                "pipelines": PIPELINES,
                "rubric_fields": RUBRIC_FIELDS,
            }
        )


@app.get("/api/jobs/<int:job_id>")
def api_job(job_id):
    with connect() as conn:
        job = get_job(conn, job_id)
    if not job:
        return jsonify({"error": "Job not found"}), 404
    return jsonify({"job": job})


@app.get("/api/application-packets")
def api_application_packets():
    with connect() as conn:
        return jsonify({"application_packets": list_application_packets(conn)})


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


@app.get("/api/codex-tasks")
def api_codex_tasks():
    return jsonify({"tasks": list_background_tasks()})


@app.get("/api/codex-tasks/<task_id>")
def api_codex_task(task_id):
    task = get_background_task(task_id)
    if not task:
        return jsonify({"error": "Task not found"}), 404
    return jsonify({"task": task})


@app.post("/api/jobs/bulk/score-gpt")
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
    task = start_background_task("scorecards", job_ids, bulk_score_worker)
    return jsonify({"task": task}), 202


@app.post("/api/jobs/bulk/application-packets/generate")
def api_bulk_generate_application_packets():
    payload = request_json_object()
    if not codex_cli_available():
        return jsonify(
            {"error": f"Codex CLI is unavailable at {codex_cli_path()!r}. Set CODEX_CLI_PATH or install Codex CLI."}
        ), 409
    job_ids = clean_job_ids(payload)
    task = start_background_task("application_packets", job_ids, bulk_packet_worker)
    return jsonify({"task": task}), 202


@app.post("/api/jobs/<int:job_id>/application-packet/generate")
def api_generate_application_packet(job_id):
    with connect() as conn:
        job = get_job(conn, job_id)
        if not job:
            return jsonify({"error": "Job not found"}), 404
        if job.get("application_packet_path"):
            return jsonify({"error": "This job already has an associated application packet."}), 409
        packet = create_application_packet(conn, job_id)
        return jsonify(
            {
                "packet": packet,
                "job": get_job(conn, job_id),
                "application_packets": list_application_packets(conn),
            }
        ), 201


@app.post("/api/jobs/<int:job_id>/application-packet/attach")
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
    with connect() as conn:
        return jsonify(
            {
                "job": result["job"],
                "application_packets": list_application_packets(conn),
            }
        )


@app.get("/api/jobs/<int:job_id>/application-packet/content")
def api_application_packet_content(job_id):
    filename = request.args.get("file", "")
    if not filename.endswith(".md") or "/" in filename or "\\" in filename:
        return jsonify({"error": "Select a Markdown file in the associated packet."}), 400
    with connect() as conn:
        job = get_job(conn, job_id)
        if not job:
            return jsonify({"error": "Job not found"}), 404
        if not job.get("application_packet_path"):
            return jsonify({"error": "Job does not have an associated application packet."}), 404
        try:
            packet_dir = application_packet_abs_path(job["application_packet_path"])
        except ValueError as exc:
            log_event(
                "packet_content_path_rejected",
                error_code="PACKET_CONTENT_PATH_REJECTED",
                component="presentation.packets",
                operation="content",
                job_id=job_id,
                error_type=type(exc).__name__,
            )
            return jsonify({"error": str(exc)}), 404
        file_path = (packet_dir / filename).resolve()
        if packet_dir not in file_path.parents or not file_path.exists() or not file_path.is_file():
            return jsonify({"error": "Markdown file not found in associated packet."}), 404
        return jsonify(
            {
                "path": repo_relative(packet_dir),
                "file": filename,
                "content": file_path.read_text(encoding="utf-8"),
                "markdown_files": list_markdown_files(packet_dir),
            }
        )


@app.get("/api/jobs/<int:job_id>/application-packet/render")
def api_application_packet_render(job_id):
    filename = request.args.get("file", "")
    if not filename.endswith(".md") or "/" in filename or "\\" in filename:
        return Response("Select a Markdown file in the associated packet.", status=400, mimetype="text/plain")
    with connect() as conn:
        job = get_job(conn, job_id)
        if not job:
            return Response("Job not found.", status=404, mimetype="text/plain")
        if not job.get("application_packet_path"):
            return Response("Job does not have an associated application packet.", status=404, mimetype="text/plain")
        try:
            packet_dir = application_packet_abs_path(job["application_packet_path"])
        except ValueError as exc:
            log_event(
                "packet_render_path_rejected",
                error_code="PACKET_RENDER_PATH_REJECTED",
                component="presentation.packets",
                operation="render",
                job_id=job_id,
                error_type=type(exc).__name__,
            )
            return Response(str(exc), status=404, mimetype="text/plain")
        file_path = (packet_dir / filename).resolve()
        if packet_dir not in file_path.parents or not file_path.exists() or not file_path.is_file():
            return Response("Markdown file not found in associated packet.", status=404, mimetype="text/plain")
        markdown = file_path.read_text(encoding="utf-8")
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
    <div class="meta">{escape_html(repo_relative(packet_dir))} / {escape_html(filename)}</div>
    {body}
  </main>
</body>
</html>
"""
        return Response(html, mimetype="text/html")


@app.post("/api/jobs/<int:job_id>/scrape")
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


@app.delete("/api/jobs/<int:job_id>")
def api_delete_job(job_id):
    payload = request_json_object()
    if payload.get("confirm") != "DELETE":
        return jsonify({"error": "Type DELETE to confirm job deletion."}), 400
    with connect() as conn:
        job = get_job(conn, job_id)
    if not job:
        return jsonify({"error": "Job not found"}), 404
    job_service().delete_job(job_id)
    log_event("manual_job_deleted", job_id=job_id, company=job["company"], title=job["title"], url=job["url"])
    with connect() as conn:
        return jsonify({"deleted_job_id": job_id, "jobs": list_jobs(conn, include_filtered=True)})


@app.get("/api/companies/<int:company_id>")
def api_company_interest(company_id):
    with connect() as conn:
        company = get_company_interest(conn, company_id)
    if not company:
        return jsonify({"error": "Company interest not found"}), 404
    return jsonify({"company": company})


@app.post("/api/companies")
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
    with connect() as conn:
        return jsonify(
            {"company": get_company_interest(conn, company_id), "companies": list_company_interests(conn)}
        ), 201


@app.post("/api/companies/<int:company_id>")
def api_update_company_interest(company_id):
    payload = require_json_object(request.get_json(silent=True) or {})
    with connect() as conn:
        existing = get_company_interest(conn, company_id)
        if not existing:
            return jsonify({"error": "Company interest not found"}), 404
        company_name = (
            optional_text(payload.get("company", existing["company"]), "company", max_length=300) or existing["company"]
        )
        interest_score = payload.get("interest_score")
        if interest_score not in (None, ""):
            interest_score = integer(interest_score, "interest_score", minimum=0, maximum=100)
        status = choice(payload.get("status", existing["status"]), "status", COMPANY_STATUSES, required=True)
        rationale = optional_text(payload.get("rationale", ""), "rationale", max_length=20_000)
        notes = optional_text(payload.get("notes", ""), "notes", max_length=20_000)
        next_step = optional_text(payload.get("next_step", ""), "next_step", max_length=2_000)
        contacts = optional_text(payload.get("contacts", ""), "contacts", max_length=10_000)
        company_service().update(
            company_id,
            {
                "company": company_name,
                "normalized_company": normalize_lookup_text(company_name),
                "status": status,
                "interest_score": interest_score if interest_score != "" else None,
                "rationale": rationale,
                "notes": notes,
                "next_step": next_step,
                "contacts": contacts,
                "updated_at": now(),
            },
        )
        return jsonify({"company": get_company_interest(conn, company_id), "companies": list_company_interests(conn)})


@app.post("/api/jobs")
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


@app.post("/api/search/run")
def api_run_search():
    payload = require_json_object(request.get_json(silent=True) or {})
    force_refresh = boolean(payload.get("force_refresh"), "force_refresh", default=False)
    run = run_job_search(trigger="manual", force_refresh=force_refresh)
    with connect() as conn:
        return jsonify(
            {
                "run": run,
                "jobs": list_jobs(conn, include_filtered=True),
                "search_runs": list_search_runs(conn),
                "discoveries": list_discoveries(conn),
            }
        )


@app.post("/api/search/queries")
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
    with connect() as conn:
        return jsonify({"search_queries": list_search_queries(conn)}), 201


@app.post("/api/search/queries/<int:query_id>")
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
    with connect() as conn:
        return jsonify({"search_queries": list_search_queries(conn)})


@app.post("/api/config")
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
    update_env_file(updates)
    for key, value in updates.items():
        os.environ[key] = value
    if "CODEX_MODEL" in updates:
        settings_service().save({"codex_model": updates["CODEX_MODEL"]})
    with connect() as conn:
        return jsonify(
            {
                "config": masked_config(),
                "settings": settings(conn),
                "api_log_path": str(API_LOG_PATH),
                "event_log_path": str(APP_LOG_PATH),
                "capture_dir": str(CAPTURE_DIR),
                "gpt_scoring_enabled": gpt_scoring_enabled(),
                "capture_cache_enabled": capture_cache_enabled(),
            }
        )


@app.post("/api/admin/purge-jobs")
def api_purge_jobs():
    payload = require_json_object(request.get_json(silent=True) or {})
    if payload.get("confirm") != "PURGE":
        return jsonify({"error": "Type PURGE to confirm tracked job deletion."}), 400
    before = job_service().purge_jobs()
    log_event("admin_purge_jobs", deleted_jobs=before)
    with connect() as conn:
        return jsonify(
            {
                "deleted_jobs": before,
                "jobs": list_jobs(conn, include_filtered=True),
                "discoveries": list_discoveries(conn),
            }
        )


@app.post("/api/jobs/<int:job_id>/score-gpt")
def api_score_gpt(job_id):
    result = scoring_service().score(job_id)
    if result.state == "missing":
        return jsonify({"error": "Job not found"}), 404
    if result.state == "unavailable":
        return jsonify({"error": result.unavailable_reason}), 409
    return jsonify({"job": result.job, "raw_score": result.raw_score})


@app.post("/api/jobs/<int:job_id>/score-user")
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
    with connect() as conn:
        return jsonify({"job": get_job(conn, job_id)})


@app.post("/api/jobs/<int:job_id>/interactions")
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
    with connect() as conn:
        return jsonify({"job": get_job(conn, job_id)}), 201


@app.post("/api/jobs/<int:job_id>/notes")
def api_add_note(job_id):
    payload = require_json_object(request.get_json(silent=True) or {})
    note = optional_text(payload.get("note", ""), "note", max_length=20_000)
    if not note:
        raise RequestValidationError("note is required.")
    service = job_service()
    if not service.add_note(job_id, note, now()):
        return jsonify({"error": "Job not found"}), 404
    with connect() as conn:
        return jsonify({"job": get_job(conn, job_id)}), 201


@app.post("/api/jobs/<int:job_id>/status")
def api_update_status(job_id):
    payload = require_json_object(request.get_json(silent=True) or {})
    status = choice(payload.get("status", "researching"), "status", JOB_STATUSES, required=True)
    service = job_service()
    if not service.update_status(job_id, status, now()):
        return jsonify({"error": "Job not found"}), 404
    with connect() as conn:
        return jsonify({"job": get_job(conn, job_id)})


@app.post("/api/settings")
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
    with connect() as conn:
        return jsonify({"settings": settings(conn), "jobs": list_jobs(conn, include_filtered=True)})


@app.errorhandler(Exception)
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


def scheduler_loop():
    while True:
        try:
            with connect() as conn:
                cfg = settings(conn)
                last_search_at = int(cfg.get("last_search_at", "0") or 0)
                if last_search_at == 0:
                    conn.execute(
                        "INSERT INTO settings(key, value) VALUES ('last_search_at', ?) ON CONFLICT(key) DO UPDATE SET value = excluded.value",
                        (str(now()),),
                    )
                    last_search_at = now()
            if now() - last_search_at >= SEARCH_INTERVAL_SECONDS:
                run_job_search(trigger="scheduled", force_refresh=True)
        except Exception as exc:
            log_event(
                "scheduled_search_failed",
                error_code="SCHEDULED_SEARCH_FAILED",
                component="business.scheduler",
                operation="scheduler_loop",
                error_type=type(exc).__name__,
                message=str(exc)[:1000],
            )
            with connect() as conn:
                conn.execute(
                    "INSERT INTO search_runs(started_at, completed_at, trigger, status, message) VALUES (?, ?, 'scheduled', 'error', ?)",
                    (now(), now(), str(exc)),
                )
        time.sleep(15 * 60)


def start_scheduler():
    # Scheduling belongs to a separately managed process; web startup never runs it.
    return None


def main():
    init_db()
    start_scheduler()
    print(f"Job Search Console running at http://{HOST}:{PORT}")
    print(f"Database: {DB_PATH}")
    app.run(host=HOST, port=PORT, debug=DEBUG, use_reloader=False)


if __name__ == "__main__":
    main()
