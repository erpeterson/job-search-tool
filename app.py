#!/usr/bin/env python3
import json
import hashlib
import logging
import os
import re
import shutil
import sqlite3
import subprocess
import tempfile
import threading
import textwrap
import time
import uuid
from datetime import datetime, timezone
from logging.handlers import RotatingFileHandler
from pathlib import Path
from urllib.parse import quote_plus, urljoin, urlparse

import requests
from bs4 import BeautifulSoup
from dotenv import load_dotenv
from flask import Flask, Response, jsonify, request
from werkzeug.exceptions import HTTPException

ROOT = Path(__file__).resolve().parents[1]
APP_DIR = Path(__file__).resolve().parent
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

DEFAULT_MODEL = os.environ.get("CODEX_MODEL", "")
DEFAULT_CODEX_CLI_PATH = os.environ.get("CODEX_CLI_PATH") or shutil.which("codex") or "codex"
CODEX_CLI_TIMEOUT_SECONDS = int(os.environ.get("CODEX_CLI_TIMEOUT_SECONDS", "270"))
HOST = os.environ.get("JOB_SEARCH_HOST", "127.0.0.1")
PORT = int(os.environ.get("JOB_SEARCH_PORT", "5050"))
DEBUG = os.environ.get("JOB_SEARCH_DEBUG", "0") == "1"
AUTORUN = False # the scheduler is buggy and eats codex credits.. disable it for now; os.environ.get("JOB_SEARCH_AUTORUN", "1") != "0"
SEARCH_INTERVAL_SECONDS = int(os.environ.get("JOB_SEARCH_INTERVAL_SECONDS", str(24 * 60 * 60)))
LOG_MAX_BYTES = int(os.environ.get("JOB_SEARCH_LOG_MAX_BYTES", str(1024 * 1024)))
LOG_BACKUP_COUNT = int(os.environ.get("JOB_SEARCH_LOG_BACKUP_COUNT", "5"))
CONFIG_KEYS = [
    "CODEX_CLI_PATH",
    "CODEX_MODEL",
    "JOB_SEARCH_ENABLE_GPT_SCORING",
    "JOB_SEARCH_USE_CAPTURE_CACHE",
]

app = Flask(__name__)
api_logger = logging.getLogger("job_search.api")
api_logger.setLevel(logging.INFO)
api_logger.propagate = False
event_logger = logging.getLogger("job_search.events")
event_logger.setLevel(logging.INFO)
event_logger.propagate = False


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

SALES_ROLE_EXCLUSION_QUERY = '-"Account Executive" -"Sales Executive" -"Sales Director" -"Account Manager" -"Business Development" -sales'
SALES_ROLE_EXCLUSION_CRITERIA = "Exclude Account Executive and other sales roles."
SALES_ROLE_TITLE_TERMS = (
    "account executive",
    "sales executive",
    "sales director",
    "sales manager",
    "sales representative",
    "account manager",
    "account director",
    "business development",
)

DEFAULT_SEARCH_QUERIES = [
    {
        "board": board,
        "pipeline": pipeline,
        "keywords": f'{config["keywords"]} {SALES_ROLE_EXCLUSION_QUERY}',
        "location": "Remote",
        "criteria": f'{config["description"]} {SALES_ROLE_EXCLUSION_CRITERIA}',
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
BACKGROUND_TASKS = {}
BACKGROUND_TASK_LOCK = threading.Lock()


def connect():
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON")
    return conn


def init_db():
    APP_DIR.mkdir(parents=True, exist_ok=True)
    with connect() as conn:
        conn.executescript(
            """
            CREATE TABLE IF NOT EXISTS settings (
                key TEXT PRIMARY KEY,
                value TEXT NOT NULL
            );

            CREATE TABLE IF NOT EXISTS jobs (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                created_at INTEGER NOT NULL,
                updated_at INTEGER NOT NULL,
                company TEXT NOT NULL,
                title TEXT NOT NULL,
                url TEXT,
                location TEXT,
                pipeline TEXT,
                status TEXT NOT NULL DEFAULT 'researching',
                posting_text TEXT,
                notes TEXT,
                gpt_score INTEGER,
                gpt_rationale TEXT,
                gpt_scorecard_json TEXT,
                user_score INTEGER,
                user_scorecard_json TEXT,
                user_rationale TEXT,
                filtered INTEGER NOT NULL DEFAULT 0,
                source_board TEXT,
                source_job_id TEXT,
                discovered_at INTEGER,
                level_assessment TEXT,
                downlevel INTEGER NOT NULL DEFAULT 0,
                application_packet_path TEXT
            );

            CREATE TABLE IF NOT EXISTS interactions (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                job_id INTEGER NOT NULL REFERENCES jobs(id) ON DELETE CASCADE,
                occurred_on TEXT NOT NULL,
                person_name TEXT,
                person_role TEXT,
                channel TEXT,
                summary TEXT,
                notes_to_self TEXT,
                next_step TEXT,
                created_at INTEGER NOT NULL
            );

            CREATE TABLE IF NOT EXISTS notes (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                job_id INTEGER NOT NULL REFERENCES jobs(id) ON DELETE CASCADE,
                created_at INTEGER NOT NULL,
                note TEXT NOT NULL
            );

            CREATE TABLE IF NOT EXISTS search_queries (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                board TEXT NOT NULL,
                pipeline TEXT,
                keywords TEXT NOT NULL,
                location TEXT,
                enabled INTEGER NOT NULL DEFAULT 1,
                created_at INTEGER NOT NULL,
                last_run_at INTEGER,
                criteria TEXT,
                refinement_notes TEXT,
                seeded INTEGER NOT NULL DEFAULT 0
            );

            CREATE TABLE IF NOT EXISTS search_runs (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                started_at INTEGER NOT NULL,
                completed_at INTEGER,
                trigger TEXT NOT NULL,
                status TEXT NOT NULL,
                message TEXT,
                found_count INTEGER NOT NULL DEFAULT 0,
                tracked_count INTEGER NOT NULL DEFAULT 0,
                rejected_count INTEGER NOT NULL DEFAULT 0
            );

            CREATE TABLE IF NOT EXISTS discovered_jobs (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                run_id INTEGER REFERENCES search_runs(id) ON DELETE SET NULL,
                query_id INTEGER REFERENCES search_queries(id) ON DELETE SET NULL,
                created_at INTEGER NOT NULL,
                board TEXT NOT NULL,
                source_job_id TEXT,
                company TEXT,
                title TEXT,
                location TEXT,
                url TEXT,
                snippet TEXT,
                gpt_score INTEGER,
                gpt_rationale TEXT,
                gpt_scorecard_json TEXT,
                level_assessment TEXT,
                downlevel INTEGER NOT NULL DEFAULT 0,
                decision TEXT NOT NULL,
                rejection_reason TEXT,
                tracked_job_id INTEGER REFERENCES jobs(id) ON DELETE SET NULL
            );

            CREATE TABLE IF NOT EXISTS level_equivalencies (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                company TEXT NOT NULL,
                normalized_company TEXT NOT NULL,
                title_pattern TEXT NOT NULL,
                normalized_title_pattern TEXT NOT NULL,
                source_level TEXT,
                source_level_title TEXT,
                oracle_level TEXT NOT NULL,
                oracle_title TEXT NOT NULL,
                downlevel INTEGER NOT NULL,
                source_url TEXT,
                notes TEXT,
                created_at INTEGER NOT NULL,
                updated_at INTEGER NOT NULL,
                UNIQUE(normalized_company, normalized_title_pattern)
            );

            CREATE TABLE IF NOT EXISTS company_interests (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                created_at INTEGER NOT NULL,
                updated_at INTEGER NOT NULL,
                company TEXT NOT NULL,
                normalized_company TEXT NOT NULL UNIQUE,
                status TEXT NOT NULL DEFAULT 'watching',
                interest_score INTEGER,
                rationale TEXT,
                notes TEXT,
                next_step TEXT,
                contacts TEXT
            );

            """
        )
        ensure_column(conn, "jobs", "source_board", "TEXT")
        ensure_column(conn, "jobs", "source_job_id", "TEXT")
        ensure_column(conn, "jobs", "discovered_at", "INTEGER")
        ensure_column(conn, "jobs", "level_assessment", "TEXT")
        ensure_column(conn, "jobs", "downlevel", "INTEGER NOT NULL DEFAULT 0")
        ensure_column(conn, "jobs", "application_packet_path", "TEXT")
        ensure_column(conn, "search_queries", "pipeline", "TEXT")
        ensure_column(conn, "search_queries", "criteria", "TEXT")
        ensure_column(conn, "search_queries", "refinement_notes", "TEXT")
        ensure_column(conn, "search_queries", "seeded", "INTEGER NOT NULL DEFAULT 0")
        ensure_column(conn, "discovered_jobs", "query_id", "INTEGER")
        conn.execute("CREATE UNIQUE INDEX IF NOT EXISTS idx_jobs_url ON jobs(url) WHERE url IS NOT NULL AND url != ''")
        conn.execute("CREATE INDEX IF NOT EXISTS idx_level_equivalencies_company ON level_equivalencies(normalized_company)")
        conn.execute("CREATE INDEX IF NOT EXISTS idx_company_interests_status ON company_interests(status)")
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


def ensure_column(conn, table, column, definition):
    columns = {row["name"] for row in conn.execute(f"PRAGMA table_info({table})")}
    if column not in columns:
        conn.execute(f"ALTER TABLE {table} ADD COLUMN {column} {definition}")


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
    cached = lookup_cached_level_equivalency(conn, company, title)
    if cached:
        return cached
    return estimate_and_cache_level_equivalency(conn, company, title)


def estimate_level_equivalency(company, title):
    normalized_title = normalize_lookup_text(title)
    if not normalized_title:
        return None

    downlevel_patterns = (
        r"^(new grad|entry level|junior|intern)\b",
        r"^(software engineer|senior software engineer|staff software engineer|staff engineer)(\b|$)",
        r"^engineering manager\b",
    )
    ic6_plus_patterns = (
        r"\b(distinguished engineer|technical fellow|fellow|chief architect)\b",
        r"\b(senior principal engineer|senior principal software engineer|senior principal architect|principal architect)\b",
        r"\b(architect|enterprise architect|platform architect)\b",
    )

    if any(re.search(pattern, normalized_title) for pattern in ic6_plus_patterns):
        return {
            "source_level": "",
            "source_level_title": clean_text(title),
            "oracle_level": "IC6+",
            "oracle_title": "Architect-equivalent or higher",
            "downlevel": False,
            "source_url": "",
            "notes": "Estimated locally from title taxonomy because Levels.fyi runtime data is unavailable.",
        }
    if any(re.search(pattern, normalized_title) for pattern in downlevel_patterns):
        return {
            "source_level": "",
            "source_level_title": clean_text(title),
            "oracle_level": "BELOW_IC6",
            "oracle_title": "Below Architect-equivalent",
            "downlevel": True,
            "source_url": "",
            "notes": "Estimated locally from title taxonomy because Levels.fyi runtime data is unavailable.",
        }
    return None


def estimate_and_cache_level_equivalency(conn, company, title):
    equivalency = estimate_level_equivalency(company, title)
    if not equivalency:
        log_event("level_equivalency_unknown", company=company, title=title, reason="No cached calibration or reliable local title estimate.")
        return None
    ts = now()
    conn.execute(
        """
        INSERT INTO level_equivalencies(
            company, normalized_company, title_pattern, normalized_title_pattern,
            source_level, source_level_title, oracle_level, oracle_title, downlevel,
            source_url, notes, created_at, updated_at
        )
        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        ON CONFLICT(normalized_company, normalized_title_pattern) DO UPDATE SET
            company = excluded.company,
            title_pattern = excluded.title_pattern,
            source_level = excluded.source_level,
            source_level_title = excluded.source_level_title,
            oracle_level = excluded.oracle_level,
            oracle_title = excluded.oracle_title,
            downlevel = excluded.downlevel,
            source_url = excluded.source_url,
            notes = excluded.notes,
            updated_at = excluded.updated_at
        """,
        (
            clean_text(company),
            normalize_lookup_text(company),
            equivalency["source_level_title"],
            normalize_lookup_text(equivalency["source_level_title"]),
            equivalency["source_level"],
            equivalency["source_level_title"],
            equivalency["oracle_level"],
            equivalency["oracle_title"],
            1 if equivalency["downlevel"] else 0,
            equivalency["source_url"],
            equivalency["notes"],
            ts,
            ts,
        ),
    )
    log_event(
        "level_equivalency_cached",
        company=company,
        title=title,
        source_level=equivalency["source_level"],
        source_level_title=equivalency["source_level_title"],
        oracle_level=equivalency["oracle_level"],
        oracle_title=equivalency["oracle_title"],
        downlevel=bool(equivalency["downlevel"]),
        source_url=equivalency["source_url"],
        source="local_title_taxonomy",
    )
    return lookup_cached_level_equivalency(conn, company, title)


def lookup_cached_level_equivalency(conn, company, title):
    normalized_company = normalize_lookup_text(company)
    normalized_title = normalize_lookup_text(title)
    rows = [
        row_to_dict(row)
        for row in conn.execute(
            """
            SELECT * FROM level_equivalencies
            WHERE normalized_company = ?
            ORDER BY LENGTH(normalized_title_pattern) DESC
            """,
            (normalized_company,),
        )
    ]
    for row in rows:
        pattern = row["normalized_title_pattern"]
        if pattern and (normalized_title == pattern or normalized_title.startswith(f"{pattern} ")):
            return row
    return None


def level_assessment_from_equivalency(equivalency):
    if not equivalency:
        return ""
    source_level = f" {equivalency['source_level']}" if equivalency.get("source_level") else ""
    return (
        f"{equivalency['company']} {equivalency['source_level_title'] or equivalency['title_pattern']}{source_level} "
        f"maps to Oracle {equivalency['oracle_level']} {equivalency['oracle_title']} per cached level calibration."
    )


def now():
    return int(time.time())


def normalize_lookup_text(value):
    return re.sub(r"[^a-z0-9]+", " ", (value or "").lower()).strip()


def row_to_dict(row):
    return dict(row) if row else None


def parse_json_field(value, fallback):
    if not value:
        return fallback
    try:
        return json.loads(value)
    except json.JSONDecodeError:
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
    return os.environ.get("JOB_SEARCH_USE_CAPTURE_CACHE", "1") != "0"


def background_task_snapshot(task):
    snapshot = dict(task)
    snapshot["items"] = [dict(item) for item in task.get("items", [])]
    return snapshot


def get_background_task(task_id):
    with BACKGROUND_TASK_LOCK:
        task = BACKGROUND_TASKS.get(task_id)
        return background_task_snapshot(task) if task else None


def list_background_tasks(limit=10):
    with BACKGROUND_TASK_LOCK:
        tasks = sorted(BACKGROUND_TASKS.values(), key=lambda task: task["created_at"], reverse=True)
        return [background_task_snapshot(task) for task in tasks[:limit]]


def update_background_task(task_id, **updates):
    with BACKGROUND_TASK_LOCK:
        task = BACKGROUND_TASKS.get(task_id)
        if not task:
            return None
        task.update(updates)
        task["updated_at"] = now()
        return background_task_snapshot(task)


def update_background_task_item(task_id, job_id, **updates):
    with BACKGROUND_TASK_LOCK:
        task = BACKGROUND_TASKS.get(task_id)
        if not task:
            return None
        for item in task["items"]:
            if item["job_id"] == job_id:
                item.update(updates)
                item["updated_at"] = now()
                break
        task["updated_at"] = now()
        return background_task_snapshot(task)


def start_background_task(operation, job_ids, worker):
    task_id = uuid.uuid4().hex
    created_at = now()
    task = {
        "id": task_id,
        "operation": operation,
        "status": "queued",
        "created_at": created_at,
        "updated_at": created_at,
        "started_at": None,
        "completed_at": None,
        "total": len(job_ids),
        "completed": 0,
        "failed": 0,
        "skipped": 0,
        "current_job_id": None,
        "message": "",
        "items": [
            {
                "job_id": job_id,
                "status": "queued",
                "message": "",
                "updated_at": created_at,
            }
            for job_id in job_ids
        ],
    }
    with BACKGROUND_TASK_LOCK:
        BACKGROUND_TASKS[task_id] = task
    thread = threading.Thread(target=worker, args=(task_id, job_ids), daemon=True)
    thread.start()
    log_event("background_task_started", task_id=task_id, operation=operation, job_ids=job_ids)
    return background_task_snapshot(task)


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
        "ts": datetime.now(timezone.utc).isoformat(),
        "service": service,
        "method": method,
        "url": url,
        "status_code": status_code,
        "ok": response is not None and response.ok and error is None,
        "elapsed_ms": elapsed_ms,
        "error_type": type(error).__name__ if error else None,
        "message": str(error)[:1000] if error else None,
        "response_excerpt": clean_text(response_text)[:2000] if response_text else None,
    }
    api_logger.info(json.dumps(event, sort_keys=True))


def log_event(event_type, **fields):
    event = {
        "ts": datetime.now(timezone.utc).isoformat(),
        "event": event_type,
        **fields,
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
        return None
    log_event("capture_replay", service=service, operation=operation, path=str(path))
    return capture


def write_capture(service, operation, request_payload, response_payload, metadata=None):
    path = capture_path(service, operation, request_payload)
    path.parent.mkdir(parents=True, exist_ok=True)
    capture = {
        "captured_at": datetime.now(timezone.utc).isoformat(),
        "service": service,
        "operation": operation,
        "request": request_payload,
        "response": response_payload,
        "metadata": metadata or {},
    }
    path.write_text(json.dumps(capture, indent=2, sort_keys=True, default=str) + "\n", encoding="utf-8")
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
    job = conn.execute("SELECT company, title, gpt_score, user_score, downlevel FROM jobs WHERE id = ?", (job_id,)).fetchone()
    filtered = 0
    reasons = []
    if job:
        if job["downlevel"]:
            filtered = 1
            reasons.append("downlevel relative to Oracle IC6-equivalent target")
        if use_gpt_threshold and job["gpt_score"] is not None and job["gpt_score"] < gpt_threshold:
            filtered = 1
            reasons.append(f"gpt_score {job['gpt_score']} below threshold {gpt_threshold}")
        if job["user_score"] is not None and job["user_score"] < user_threshold:
            filtered = 1
            reasons.append(f"user_score {job['user_score']} below threshold {user_threshold}")
    conn.execute("UPDATE jobs SET filtered = ?, updated_at = ? WHERE id = ?", (filtered, now(), job_id))
    if job and filtered:
        log_event(
            "job_filtered",
            job_id=job_id,
            company=job["company"],
            title=job["title"],
            reasons=reasons,
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
        for row in conn.execute("SELECT * FROM search_queries ORDER BY enabled DESC, seeded DESC, pipeline, board, keywords, location")
    ]


def list_search_runs(conn):
    return [
        row_to_dict(row)
        for row in conn.execute("SELECT * FROM search_runs ORDER BY started_at DESC LIMIT 20")
    ]


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
    identifier = re.sub(r"[^a-z0-9]+", "-", (job.get("source_job_id") or str(job.get("id") or "job")).lower()).strip("-")
    return f"{datetime.now().strftime('%Y-%m')}-{company[:60]}-{title[:90]}-{identifier[:40]}"


def application_packet_rules():
    if not CAREER_MANUAL_PATH.exists():
        return ""
    manual = CAREER_MANUAL_PATH.read_text(encoding="utf-8")
    start = manual.find("# Downstream Artifact Rules")
    end = manual.find("# Open Questions", start)
    return manual[start:end if end >= 0 else None].strip() if start >= 0 else ""


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
            "posting_text": job.get("posting_text") or "No posting text was captured. Do not invent requirements beyond the role title and metadata.",
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


def write_application_packet_documents(packet_dir, payload):
    packet_dir.mkdir(parents=True, exist_ok=False)
    markdown_files = {
        "Job-Brief.md": payload["job_brief_markdown"],
        "Resume.md": payload["resume_markdown"],
        "Cover-Letter.md": payload["cover_letter_markdown"],
    }
    for filename, content in markdown_files.items():
        (packet_dir / filename).write_text(content, encoding="utf-8")

    pandoc_path = shutil.which("pandoc")
    if not pandoc_path:
        raise RuntimeError("Pandoc is required to generate packet DOCX deliverables but was not found on PATH.")
    for filename in markdown_files:
        source_path = packet_dir / filename
        output_path = source_path.with_suffix(".docx")
        completed = subprocess.run(
            [pandoc_path, "--from", "markdown", "--to", "docx", "--output", str(output_path), str(source_path)],
            text=True,
            capture_output=True,
            check=False,
        )
        if completed.returncode != 0:
            raise RuntimeError(f"Pandoc failed for {filename}: {(completed.stderr or completed.stdout).strip()[:1000]}")
    return list(markdown_files)


def generate_application_packet_with_codex(job):
    if not job.get("url"):
        raise ValueError("Job does not have a URL for Codex packet generation.")
    if not codex_cli_available():
        raise RuntimeError(f"Codex CLI is unavailable at {codex_cli_path()!r}. Set CODEX_CLI_PATH or install Codex CLI.")

    model = codex_model()
    if not model:
        raise ValueError("An explicit CODEX_MODEL is required for application packets so Codex can provide exact AI-generation attribution.")
    context = application_packet_context(job)
    context["codex_generation_metadata"] = {
        "generation_date": datetime.now(timezone.utc).date().isoformat(),
        "model": model,
    }
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
    try:
        output_text = call_codex_json(model, prompt, "generate_application_packet")
        payload = validate_application_packet_payload(parse_model_json(output_text))
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
            error_type=type(error).__name__ if error else None,
            message=str(error)[:1000] if error else None,
        )

    packet_dir = infer_generated_packet(job, before_dirs)
    return {
        "output_text": output_text,
        "packet_dir": packet_dir,
    }


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
                log_event("bulk_codex_score_error", task_id=task_id, job_id=job_id, error_type=type(exc).__name__, message=str(exc)[:1000])
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
            update_background_task_item(task_id, job_id, status="running", message="Generating application packet with Codex")
            try:
                job = get_job(conn, job_id)
                if not job:
                    skipped += 1
                    update_background_task_item(task_id, job_id, status="skipped", message="Job not found")
                elif job.get("application_packet_path"):
                    skipped += 1
                    update_background_task_item(task_id, job_id, status="skipped", message="Application packet already associated")
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
                log_event("bulk_application_packet_error", task_id=task_id, job_id=job_id, error_type=type(exc).__name__, message=str(exc)[:1000])
            finally:
                update_background_task(task_id, completed=completed, failed=failed, skipped=skipped)
    status = "complete" if failed == 0 else "error"
    message = f"Complete: {completed} generated, {skipped} skipped, {failed} failed."
    update_background_task(task_id, status=status, completed_at=now(), current_job_id=None, message=message)
    log_event("background_task_finished", task_id=task_id, operation="application_packets", status=status, message=message)


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
    soup = BeautifulSoup(response.text, "html.parser")
    jobs = []
    for card in soup.select("li"):
        link = card.select_one("a.base-card__full-link, a")
        title = card.select_one(".base-search-card__title, h3")
        company = card.select_one(".base-search-card__subtitle, h4")
        location_el = card.select_one(".job-search-card__location")
        if not link or not title:
            continue
        href = clean_url(link.get("href", ""))
        jobs.append(
            {
                "board": "linkedin",
                "source_job_id": source_id("linkedin", href),
                "company": clean_text(company.get_text(" ")) if company else "",
                "title": clean_text(title.get_text(" ")),
                "location": clean_text(location_el.get_text(" ")) if location_el else location or "",
                "url": href,
                "snippet": clean_text(card.get_text(" "))[:1200],
            }
        )
    return dedupe_results(jobs)


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
        or selector_text(soup, ["h1", ".top-card-layout__title", ".jobsearch-JobInfoHeader-title", "[data-testid='jobsearch-JobInfoHeader-title']"])
        or meta_content(soup, ["og:title", "twitter:title"])
        or page_title
    )
    company = (
        nested_value(json_ld, "hiringOrganization", "name")
        or selector_text(soup, [".topcard__org-name-link", ".topcard__flavor", "[data-testid='inlineHeader-companyName']", "[data-company-name]", ".jobsearch-InlineCompanyRating-companyHeader a"])
        or meta_content(soup, ["og:site_name"])
    )
    location = (
        location_from_json_ld(json_ld)
        or selector_text(soup, [".topcard__flavor--bullet", ".job-search-card__location", "[data-testid='job-location']", ".jobsearch-JobInfoHeader-subtitle div"])
    )
    description = (
        nested_value(json_ld, "description")
        or selector_text(soup, ["#job-details", ".show-more-less-html__markup", "#jobDescriptionText", "[data-testid='jobDescriptionText']"])
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
    request_payload = {
        "method": "GET",
        "url": url,
        "headers": request_headers(),
    }
    cached = read_capture(service, "http_get", request_payload, force_refresh=force_refresh)
    if cached:
        return CapturedResponse(cached["response"])

    started = time.monotonic()
    response = None
    error = None
    try:
        response = requests.get(url, headers=request_headers(), timeout=30)
        return response
    except requests.RequestException as exc:
        error = exc
        raise
    finally:
        elapsed_ms = int((time.monotonic() - started) * 1000)
        log_api_call(service, "GET", url, response=response, error=error, elapsed_ms=elapsed_ms)
        response_payload = {
            "status_code": getattr(response, "status_code", None),
            "headers": dict(getattr(response, "headers", {}) or {}),
            "text": getattr(response, "text", None),
            "error_type": type(error).__name__ if error else None,
            "error_message": str(error) if error else None,
        }
        write_capture(service, "http_get", request_payload, response_payload, {"elapsed_ms": elapsed_ms})


class CapturedResponse:
    def __init__(self, payload):
        self.status_code = payload.get("status_code")
        self.headers = payload.get("headers") or {}
        self.text = payload.get("text") or ""
        self.ok = self.status_code is not None and 200 <= int(self.status_code) < 400

    def raise_for_status(self):
        if not self.ok:
            raise requests.HTTPError(f"{self.status_code} Error replayed from capture")


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
    except (TypeError, ValueError):
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
    board = query["board"].lower()
    if board == "linkedin":
        return fetch_linkedin_jobs(query["keywords"], query["location"], force_refresh=force_refresh)
    if board == "indeed":
        return fetch_indeed_jobs(query["keywords"], query["location"], force_refresh=force_refresh)
    raise RuntimeError(f"Unsupported board: {query['board']}")


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


SEATTLE_LOCATION_TERMS = (
    "seattle",
    "bellevue",
    "redmond",
    "kirkland",
    "renton",
    "mercer island",
    "tukwila",
    "puget sound",
    "greater seattle",
    "seattle metropolitan",
)

US_LOCATION_TERMS = (
    "united states",
    "usa",
    "u.s.",
    " us ",
    "us-based",
    "anywhere in the us",
    "anywhere in us",
)

NON_US_LOCATION_TERMS = (
    "canada",
    "united kingdom",
    "uk",
    "europe",
    "emea",
    "india",
    "australia",
    "germany",
    "france",
    "netherlands",
    "singapore",
    "mexico",
)


def location_filter_decision(result):
    location = clean_text(result.get("location", ""))
    combined = f" {location} {result.get('snippet', '')[:500]} ".lower()
    if any(term in combined for term in SEATTLE_LOCATION_TERMS):
        return True, "Seattle-based or Seattle-area role"
    remote = "remote" in combined
    non_us = any(term in combined for term in NON_US_LOCATION_TERMS)
    us_based = any(term in combined for term in US_LOCATION_TERMS) or "remote" == location.lower()
    if remote and us_based and not non_us:
        return True, "US-based remote role"
    if remote and not non_us and not location:
        return True, "Remote role with no non-US location signal"
    return False, f"Location is not US-based remote or Seattle-based: {location or 'unknown'}"


def normalize_money_value(raw_value, suffix=""):
    value = float(raw_value.replace(",", ""))
    if suffix and suffix.lower() == "k":
        value *= 1000
    return value


def annualize_compensation(value, period):
    period = (period or "year").lower()
    if period in ("hour", "hr"):
        return value * 2080
    if period in ("month", "mo"):
        return value * 12
    return value


def extract_annual_compensation_values(text):
    values = []
    money = r"\$?\s*([0-9]{2,3}(?:,[0-9]{3})?(?:\.\d+)?)\s*([kK]?)"
    range_pattern = re.compile(
        rf"{money}\s*(?:-|–|—|to)\s*{money}\s*(?:per\s+|/)?(year|yr|annually|annual|hour|hr|month|mo)?",
        re.IGNORECASE,
    )
    single_pattern = re.compile(
        rf"{money}\s*(?:per\s+|/)(year|yr|annually|annual|hour|hr|month|mo)",
        re.IGNORECASE,
    )
    for match in range_pattern.finditer(text):
        low_value = normalize_money_value(match.group(1), match.group(2))
        high_value = normalize_money_value(match.group(3), match.group(4))
        period = match.group(5) or "year"
        values.append(annualize_compensation(low_value, period))
        values.append(annualize_compensation(high_value, period))
    for match in single_pattern.finditer(text):
        value = normalize_money_value(match.group(1), match.group(2))
        period = match.group(3)
        values.append(annualize_compensation(value, period))
    return values


def compensation_filter_decision(result):
    text = clean_text(" ".join(str(result.get(field) or "") for field in ("title", "location", "snippet")))
    values = extract_annual_compensation_values(text)
    if not values:
        return True, "No explicit compensation below threshold found"
    high = max(values)
    if high < MIN_ANNUAL_COMPENSATION:
        return False, f"Explicit compensation below ${MIN_ANNUAL_COMPENSATION:,}/year; highest parsed annualized value is ${int(high):,}"
    return True, f"Explicit compensation meets threshold; highest parsed annualized value is ${int(high):,}"


def sales_role_filter_decision(result):
    title = clean_text(result.get("title", "")).lower()
    if any(term in title for term in SALES_ROLE_TITLE_TERMS):
        return False, f"Sales role excluded by title: {result.get('title') or 'unknown'}"
    return True, "Not a sales-role title"


def run_job_search(trigger="manual", force_refresh=False):
    started = now()
    log_event("search_started", trigger=trigger, force_refresh=force_refresh)
    with connect() as conn:
        cur = conn.execute(
            "INSERT INTO search_runs(started_at, trigger, status) VALUES (?, ?, 'running')",
            (started, trigger),
        )
        run_id = cur.lastrowid
        queries = [
            row_to_dict(row)
            for row in conn.execute("SELECT * FROM search_queries WHERE enabled = 1 ORDER BY seeded DESC, pipeline, board, keywords")
        ]

    found_count = 0
    tracked_count = 0
    rejected_count = 0
    messages = []

    for query in queries:
        try:
            results = fetch_jobs_for_query(query, force_refresh=force_refresh)
        except Exception as exc:
            messages.append(f"{query['board']}:{query['keywords']}: {exc}")
            continue

        with connect() as conn:
            conn.execute("UPDATE search_queries SET last_run_at = ? WHERE id = ?", (now(), query["id"]))

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
                    conn.execute(
                        """
                        INSERT INTO discovered_jobs(
                            run_id, query_id, created_at, board, source_job_id, company, title, location, url, snippet,
                            gpt_score, gpt_rationale, gpt_scorecard_json, level_assessment, downlevel,
                            decision, rejection_reason, tracked_job_id
                        )
                        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, NULL, NULL, '{}', '', 0, 'rejected', ?, NULL)
                        """,
                        (
                            run_id,
                            query["id"],
                            now(),
                            result.get("board"),
                            result.get("source_job_id"),
                            result.get("company"),
                            result.get("title"),
                            result.get("location"),
                            result.get("url"),
                            result.get("snippet"),
                            sales_reason,
                        ),
                    )
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
                    conn.execute(
                        """
                        INSERT INTO discovered_jobs(
                            run_id, query_id, created_at, board, source_job_id, company, title, location, url, snippet,
                            gpt_score, gpt_rationale, gpt_scorecard_json, level_assessment, downlevel,
                            decision, rejection_reason, tracked_job_id
                        )
                        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, NULL, NULL, '{}', '', 0, 'rejected', ?, NULL)
                        """,
                        (
                            run_id,
                            query["id"],
                            now(),
                            result.get("board"),
                            result.get("source_job_id"),
                            result.get("company"),
                            result.get("title"),
                            result.get("location"),
                            result.get("url"),
                            result.get("snippet"),
                            location_reason,
                        ),
                    )
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
                    conn.execute(
                        """
                        INSERT INTO discovered_jobs(
                            run_id, query_id, created_at, board, source_job_id, company, title, location, url, snippet,
                            gpt_score, gpt_rationale, gpt_scorecard_json, level_assessment, downlevel,
                            decision, rejection_reason, tracked_job_id
                        )
                        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, NULL, NULL, '{}', '', 0, 'rejected', ?, NULL)
                        """,
                        (
                            run_id,
                            query["id"],
                            now(),
                            result.get("board"),
                            result.get("source_job_id"),
                            result.get("company"),
                            result.get("title"),
                            result.get("location"),
                            result.get("url"),
                            result.get("snippet"),
                            compensation_reason,
                        ),
                    )
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
                decision, reason, tracked_job_id, score, scorecard, level_assessment, downlevel = classify_discovery(conn, result, force_refresh=force_refresh)
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
                conn.execute(
                    """
                    INSERT INTO discovered_jobs(
                        run_id, query_id, created_at, board, source_job_id, company, title, location, url, snippet,
                        gpt_score, gpt_rationale, gpt_scorecard_json, level_assessment, downlevel,
                        decision, rejection_reason, tracked_job_id
                    )
                    VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                    """,
                    (
                        run_id,
                        query["id"],
                        now(),
                        result.get("board"),
                        result.get("source_job_id"),
                        result.get("company"),
                        result.get("title"),
                        result.get("location"),
                        result.get("url"),
                        result.get("snippet"),
                        score.get("total_score") if score else None,
                        score.get("rationale") if score else None,
                        json.dumps(scorecard or {}),
                        level_assessment,
                        1 if downlevel else 0,
                        decision,
                        reason,
                        tracked_job_id,
                    ),
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

    with connect() as conn:
        conn.execute(
            """
            UPDATE search_runs
            SET completed_at = ?, status = 'complete', message = ?, found_count = ?, tracked_count = ?, rejected_count = ?
            WHERE id = ?
            """,
            (now(), "\n".join(messages), found_count, tracked_count, rejected_count, run_id),
        )
        conn.execute(
            "INSERT INTO settings(key, value) VALUES ('last_search_at', ?) ON CONFLICT(key) DO UPDATE SET value = excluded.value",
            (str(now()),),
        )
        run = row_to_dict(conn.execute("SELECT * FROM search_runs WHERE id = ?", (run_id,)).fetchone())
        log_event(
            "search_completed",
            trigger=trigger,
            force_refresh=force_refresh,
            run_id=run_id,
            found_count=found_count,
            tracked_count=tracked_count,
            rejected_count=rejected_count,
            message="\n".join(messages),
        )
        return run


def refine_search_query(conn, query_id, force_refresh=False):
    if not gpt_scoring_enabled():
        log_event("query_refinement_skipped", query_id=query_id, reason="Codex scoring disabled")
        return
    if not codex_cli_available():
        log_event("query_refinement_skipped", query_id=query_id, reason="Codex CLI unavailable", codex_cli_path=codex_cli_path())
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
        "pipeline_criteria": query.get("criteria") or PIPELINE_CRITERIA.get(query.get("pipeline"), {}).get("description", ""),
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
    except json.JSONDecodeError:
        log_event("query_refinement_invalid_json", query_id=query_id, response_excerpt=output_text[:1000])
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
        return "tracked", reason, job_id, None, {}, result.get("cached_level_assessment", ""), bool(result.get("cached_downlevel"))
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
        return "tracked", reason, job_id, None, {}, result.get("cached_level_assessment", ""), bool(result.get("cached_downlevel"))

    score = score_discovery_with_codex(conn, result, force_refresh=force_refresh)
    scorecard = score.get("scorecard", {})
    total = int(score.get("total_score", 0))
    downlevel = bool(score.get("downlevel", False))
    level_assessment = score.get("level_assessment", "") or result.get("cached_level_assessment", "") or UNKNOWN_LEVEL_ASSESSMENT
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
        raise RuntimeError(f"Codex CLI is unavailable at {codex_cli_path()!r}. Set CODEX_CLI_PATH or install Codex CLI before scoring.")

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


def call_codex_json(model, prompt, operation, force_refresh=False):
    cli_path = codex_cli_path()
    request_payload = {
        "adapter_version": 2,
        "cli_path": cli_path,
        "model": model,
        "prompt": prompt,
    }
    cached = read_capture("codex_cli", operation, request_payload, force_refresh=force_refresh)
    if cached:
        return cached["response"].get("output_text", "")

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
    instruction = (
        "You are a JSON-only engine for a local job-search app.\n"
        "Return only one valid JSON object. Do not include markdown fences, prose, or explanations outside JSON.\n\n"
        f"{json.dumps(prompt, indent=2, sort_keys=True, default=str)}\n"
    )
    try:
        with tempfile.TemporaryDirectory(prefix="job-search-codex-") as tmpdir:
            output_path = Path(tmpdir) / "last-message.txt"
            command = [
                cli_path,
                "exec",
                "-C",
                str(ROOT),
                "--sandbox",
                "read-only",
                "-o",
                str(output_path),
                "-",
            ]
            if model:
                command[2:2] = ["-m", model]
            completed = subprocess.run(
                command,
                input=instruction,
                text=True,
                capture_output=True,
                timeout=CODEX_CLI_TIMEOUT_SECONDS,
                check=False,
            )
            if output_path.exists():
                output_text = output_path.read_text(encoding="utf-8").strip()
            if not output_text:
                output_text = (completed.stdout or "").strip()
            if completed.returncode != 0:
                raise CodexCliError(operation, completed.returncode)
        return output_text
    except Exception as exc:
        error = exc
        raise
    finally:
        elapsed_ms = int((time.monotonic() - started) * 1000)
        response_payload = {
            "output_text": output_text,
            "returncode": completed.returncode if completed is not None else None,
            "stdout_excerpt": clean_text(completed.stdout)[:2000] if completed is not None and completed.stdout else None,
            "stderr_excerpt": clean_text(completed.stderr)[:2000] if completed is not None and completed.stderr else None,
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
            error_type=type(error).__name__ if error else None,
            message=str(error)[:1000] if error else None,
        )
        write_capture("codex_cli", operation, request_payload, response_payload, {"elapsed_ms": elapsed_ms})


@app.get("/")
def index():
    return INDEX_HTML


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
        raise ValueError("job_ids must be a list.")
    job_ids = []
    seen = set()
    for raw_id in raw_ids:
        try:
            job_id = int(raw_id)
        except (TypeError, ValueError) as exc:
            raise ValueError("job_ids must contain only integers.") from exc
        if job_id > 0 and job_id not in seen:
            seen.add(job_id)
            job_ids.append(job_id)
    if not job_ids:
        raise ValueError("Select at least one job.")
    return job_ids


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
    payload = request.get_json(silent=True) or {}
    if not gpt_scoring_enabled():
        return jsonify({"error": "Codex scoring is currently disabled. Set JOB_SEARCH_ENABLE_GPT_SCORING=1 to re-enable it."}), 409
    if not codex_cli_available():
        return jsonify({"error": f"Codex CLI is unavailable at {codex_cli_path()!r}. Set CODEX_CLI_PATH or install Codex CLI before scoring."}), 409
    try:
        job_ids = clean_job_ids(payload)
    except ValueError as exc:
        return jsonify({"error": str(exc)}), 400
    task = start_background_task("scorecards", job_ids, bulk_score_worker)
    return jsonify({"task": task}), 202


@app.post("/api/jobs/bulk/application-packets/generate")
def api_bulk_generate_application_packets():
    payload = request.get_json(silent=True) or {}
    if not codex_cli_available():
        return jsonify({"error": f"Codex CLI is unavailable at {codex_cli_path()!r}. Set CODEX_CLI_PATH or install Codex CLI."}), 409
    try:
        job_ids = clean_job_ids(payload)
    except ValueError as exc:
        return jsonify({"error": str(exc)}), 400
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
    payload = request.get_json(silent=True) or {}
    packet_path = clean_text(payload.get("path", ""))
    with connect() as conn:
        job = get_job(conn, job_id)
        if not job:
            return jsonify({"error": "Job not found"}), 404
        try:
            packet_dir = application_packet_abs_path(packet_path)
        except ValueError as exc:
            return jsonify({"error": str(exc)}), 400
        conn.execute(
            "UPDATE jobs SET application_packet_path = ?, updated_at = ? WHERE id = ?",
            (repo_relative(packet_dir), now(), job_id),
        )
        log_event("application_packet_attached", job_id=job_id, path=repo_relative(packet_dir))
        return jsonify(
            {
                "job": get_job(conn, job_id),
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
    payload = request.get_json(silent=True) or {}
    force_refresh = bool(payload.get("force_refresh", True))
    with connect() as conn:
        job = get_job(conn, job_id)
        if not job:
            return jsonify({"error": "Job not found"}), 404
        if not job.get("url"):
            return jsonify({"error": "Job does not have a URL to scrape."}), 400
        scraped = scrape_job_from_url(job["url"], force_refresh=force_refresh)
        conn.execute(
            """
            UPDATE jobs
            SET company = ?, title = ?, location = ?, posting_text = ?,
                source_board = ?, source_job_id = ?, discovered_at = COALESCE(discovered_at, ?),
                notes = ?, updated_at = ?
            WHERE id = ?
            """,
            (
                scraped.get("company") or job["company"],
                scraped.get("title") or job["title"],
                scraped.get("location") or job["location"],
                scraped.get("posting_text") or job["posting_text"],
                scraped.get("source_board") or job["source_board"],
                scraped.get("source_job_id") or job["source_job_id"],
                now(),
                append_note_text(job.get("notes"), "Re-scraped posting URL."),
                now(),
                job_id,
            ),
        )
        apply_filter(conn, job_id)
        log_event(
            "manual_job_rescraped",
            job_id=job_id,
            url=job["url"],
            company=scraped.get("company"),
            title=scraped.get("title"),
            force_refresh=force_refresh,
        )
        return jsonify({"job": get_job(conn, job_id), "scraped": scraped})


@app.delete("/api/jobs/<int:job_id>")
def api_delete_job(job_id):
    payload = request.get_json(silent=True) or {}
    if payload.get("confirm") != "DELETE":
        return jsonify({"error": "Type DELETE to confirm job deletion."}), 400
    with connect() as conn:
        job = get_job(conn, job_id)
        if not job:
            return jsonify({"error": "Job not found"}), 404
        conn.execute("UPDATE discovered_jobs SET tracked_job_id = NULL WHERE tracked_job_id = ?", (job_id,))
        conn.execute("DELETE FROM jobs WHERE id = ?", (job_id,))
        log_event("manual_job_deleted", job_id=job_id, company=job["company"], title=job["title"], url=job["url"])
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
    payload = request.get_json(silent=True) or {}
    company_name = clean_text(payload.get("company", "")) or "Unknown company"
    ts = now()
    interest_score = payload.get("interest_score")
    with connect() as conn:
        cur = conn.execute(
            """
            INSERT INTO company_interests(
                created_at, updated_at, company, normalized_company, status,
                interest_score, rationale, notes, next_step, contacts
            )
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT(normalized_company) DO UPDATE SET
                company = excluded.company,
                status = excluded.status,
                interest_score = excluded.interest_score,
                rationale = excluded.rationale,
                notes = excluded.notes,
                next_step = excluded.next_step,
                contacts = excluded.contacts,
                updated_at = excluded.updated_at
            RETURNING id
            """,
            (
                ts,
                ts,
                company_name,
                normalize_lookup_text(company_name),
                payload.get("status", "watching"),
                interest_score if interest_score != "" else None,
                payload.get("rationale", "").strip(),
                payload.get("notes", "").strip(),
                payload.get("next_step", "").strip(),
                payload.get("contacts", "").strip(),
            ),
        )
        company_id = cur.fetchone()["id"]
        return jsonify({"company": get_company_interest(conn, company_id), "companies": list_company_interests(conn)}), 201


@app.post("/api/companies/<int:company_id>")
def api_update_company_interest(company_id):
    payload = request.get_json(silent=True) or {}
    with connect() as conn:
        existing = get_company_interest(conn, company_id)
        if not existing:
            return jsonify({"error": "Company interest not found"}), 404
        company_name = clean_text(payload.get("company", existing["company"])) or existing["company"]
        interest_score = payload.get("interest_score")
        conn.execute(
            """
            UPDATE company_interests
            SET company = ?, normalized_company = ?, status = ?, interest_score = ?,
                rationale = ?, notes = ?, next_step = ?, contacts = ?, updated_at = ?
            WHERE id = ?
            """,
            (
                company_name,
                normalize_lookup_text(company_name),
                payload.get("status", existing["status"]),
                interest_score if interest_score != "" else None,
                payload.get("rationale", ""),
                payload.get("notes", ""),
                payload.get("next_step", ""),
                payload.get("contacts", ""),
                now(),
                company_id,
            ),
        )
        return jsonify({"company": get_company_interest(conn, company_id), "companies": list_company_interests(conn)})


@app.post("/api/jobs")
def api_create_job():
    payload = request.get_json(silent=True) or {}
    ts = now()
    url = payload.get("url", "").strip()
    pipeline = payload.get("pipeline", "").strip()
    if not url:
        return jsonify({"error": "URL is required."}), 400
    if not pipeline:
        return jsonify({"error": "Pipeline is required."}), 400
    scrape_error = None
    try:
        scraped = scrape_job_from_url(url, force_refresh=bool(payload.get("force_refresh")))
    except Exception as exc:
        scrape_error = str(exc)
        scraped = fallback_job_from_url(url)
        log_event(
            "manual_job_scrape_failed",
            url=url,
            pipeline=pipeline,
            error_type=type(exc).__name__,
            message=scrape_error[:1000],
        )
    with connect() as conn:
        existing = conn.execute("SELECT id FROM jobs WHERE url = ? LIMIT 1", (scraped.get("url") or url,)).fetchone()
        if existing:
            return jsonify({"error": "This job URL is already tracked.", "job": get_job(conn, existing["id"])}), 409
        cur = conn.execute(
            """
            INSERT INTO jobs(
                created_at, updated_at, company, title, url, location, pipeline, status, posting_text, notes,
                source_board, source_job_id, discovered_at
            )
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                ts,
                ts,
                payload.get("company", "").strip() or scraped.get("company") or "Unknown company",
                payload.get("title", "").strip() or scraped.get("title") or "Unknown title",
                scraped.get("url") or url,
                payload.get("location", "").strip() or scraped.get("location", ""),
                pipeline,
                payload.get("status", "researching"),
                payload.get("posting_text", "").strip() or scraped.get("posting_text", ""),
                payload.get("notes", "").strip() or f"Added manually from URL.{f' Scrape failed: {scrape_error[:500]}' if scrape_error else ''}",
                scraped.get("source_board"),
                scraped.get("source_job_id"),
                ts if scraped else None,
            ),
        )
        job_id = cur.lastrowid
        apply_filter(conn, job_id)
        score_error = None
        if gpt_scoring_enabled() and codex_cli_available():
            try:
                populate_codex_score(conn, job_id)
            except Exception as exc:
                score_error = str(exc)
                log_event(
                    "manual_job_auto_score_failed",
                    job_id=job_id,
                    error_type=type(exc).__name__,
                    message=score_error[:1000],
                )
        else:
            unavailable_reason = "Codex scoring is disabled." if not gpt_scoring_enabled() else f"Codex CLI is unavailable at {codex_cli_path()!r}."
            score_error = f"Automatic Codex scoring skipped: {unavailable_reason}"
            log_event("manual_job_auto_score_skipped", job_id=job_id, reason=unavailable_reason)
        return jsonify({"job": get_job(conn, job_id), "scrape_error": scrape_error, "score_error": score_error}), 201


@app.post("/api/search/run")
def api_run_search():
    payload = request.get_json(silent=True) or {}
    force_refresh = bool(payload.get("force_refresh"))
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
    payload = request.get_json(silent=True) or {}
    ts = now()
    with connect() as conn:
        conn.execute(
            """
            INSERT INTO search_queries(board, pipeline, keywords, location, enabled, created_at, criteria, seeded)
            VALUES (?, ?, ?, ?, ?, ?, ?, 0)
            """,
            (
                payload.get("board", "linkedin"),
                payload.get("pipeline", ""),
                payload.get("keywords", "").strip(),
                payload.get("location", "").strip(),
                1 if payload.get("enabled", True) else 0,
                ts,
                payload.get("criteria", "").strip(),
            ),
        )
        return jsonify({"search_queries": list_search_queries(conn)}), 201


@app.post("/api/search/queries/<int:query_id>")
def api_update_search_query(query_id):
    payload = request.get_json(silent=True) or {}
    with connect() as conn:
        conn.execute(
            """
            UPDATE search_queries
            SET board = COALESCE(?, board),
                pipeline = COALESCE(?, pipeline),
                keywords = COALESCE(?, keywords),
                location = COALESCE(?, location),
                criteria = COALESCE(?, criteria),
                enabled = COALESCE(?, enabled)
            WHERE id = ?
            """,
            (
                payload.get("board"),
                payload.get("pipeline"),
                payload.get("keywords"),
                payload.get("location"),
                payload.get("criteria"),
                1 if payload.get("enabled") is True else 0 if payload.get("enabled") is False else None,
                query_id,
            ),
        )
        return jsonify({"search_queries": list_search_queries(conn)})


@app.post("/api/config")
def api_update_config():
    payload = request.get_json(silent=True) or {}
    updates = {}
    for key in CONFIG_KEYS:
        if key not in payload:
            continue
        value = str(payload.get(key, "")).strip()
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
    with connect() as conn:
        if "CODEX_MODEL" in updates:
            conn.execute(
                "INSERT INTO settings(key, value) VALUES ('codex_model', ?) ON CONFLICT(key) DO UPDATE SET value = excluded.value",
                (updates["CODEX_MODEL"],),
            )
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
    payload = request.get_json(silent=True) or {}
    if payload.get("confirm") != "PURGE":
        return jsonify({"error": "Type PURGE to confirm tracked job deletion."}), 400
    with connect() as conn:
        before = conn.execute("SELECT COUNT(*) AS count FROM jobs").fetchone()["count"]
        conn.execute("UPDATE discovered_jobs SET tracked_job_id = NULL WHERE tracked_job_id IS NOT NULL")
        conn.execute("DELETE FROM jobs")
        log_event("admin_purge_jobs", deleted_jobs=before)
        return jsonify(
            {
                "deleted_jobs": before,
                "jobs": list_jobs(conn, include_filtered=True),
                "discoveries": list_discoveries(conn),
            }
        )


@app.post("/api/jobs/<int:job_id>/score-gpt")
def api_score_gpt(job_id):
    with connect() as conn:
        job = get_job(conn, job_id)
        if not job:
            return jsonify({"error": "Job not found"}), 404
        if not gpt_scoring_enabled():
            return jsonify({"error": "Codex scoring is currently disabled. Set JOB_SEARCH_ENABLE_GPT_SCORING=1 to re-enable it."}), 409
        score = populate_codex_score(conn, job_id)
        return jsonify({"job": get_job(conn, job_id), "raw_score": score})


@app.post("/api/jobs/<int:job_id>/score-user")
def api_score_user(job_id):
    payload = request.get_json(silent=True) or {}
    scorecard = {field: clamp_score(payload.get("scorecard", {}).get(field, 0)) for field in RUBRIC_FIELDS}
    total = int(payload.get("total_score") or round(sum(scorecard.values()) * 100 / (len(RUBRIC_FIELDS) * 10)))
    with connect() as conn:
        conn.execute(
            """
            UPDATE jobs
            SET user_score = ?, user_scorecard_json = ?, user_rationale = ?, updated_at = ?
            WHERE id = ?
            """,
            (total, json.dumps(scorecard), payload.get("user_rationale", ""), now(), job_id),
        )
        apply_filter(conn, job_id)
        return jsonify({"job": get_job(conn, job_id)})


@app.post("/api/jobs/<int:job_id>/interactions")
def api_add_interaction(job_id):
    payload = request.get_json(silent=True) or {}
    with connect() as conn:
        conn.execute(
            """
            INSERT INTO interactions(job_id, occurred_on, person_name, person_role, channel, summary, notes_to_self, next_step, created_at)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                job_id,
                payload.get("occurred_on", ""),
                payload.get("person_name", ""),
                payload.get("person_role", ""),
                payload.get("channel", ""),
                payload.get("summary", ""),
                payload.get("notes_to_self", ""),
                payload.get("next_step", ""),
                now(),
            ),
        )
        conn.execute("UPDATE jobs SET updated_at = ? WHERE id = ?", (now(), job_id))
        return jsonify({"job": get_job(conn, job_id)}), 201


@app.post("/api/jobs/<int:job_id>/notes")
def api_add_note(job_id):
    payload = request.get_json(silent=True) or {}
    with connect() as conn:
        conn.execute(
            "INSERT INTO notes(job_id, created_at, note) VALUES (?, ?, ?)",
            (job_id, now(), payload.get("note", "")),
        )
        conn.execute("UPDATE jobs SET updated_at = ? WHERE id = ?", (now(), job_id))
        return jsonify({"job": get_job(conn, job_id)}), 201


@app.post("/api/jobs/<int:job_id>/status")
def api_update_status(job_id):
    payload = request.get_json(silent=True) or {}
    with connect() as conn:
        conn.execute(
            "UPDATE jobs SET status = ?, updated_at = ? WHERE id = ?",
            (payload.get("status", "researching"), now(), job_id),
        )
        return jsonify({"job": get_job(conn, job_id)})


@app.post("/api/settings")
def api_update_settings():
    payload = request.get_json(silent=True) or {}
    with connect() as conn:
        for key in ("gpt_threshold", "user_threshold", "codex_model"):
            if key in payload:
                conn.execute(
                    "INSERT INTO settings(key, value) VALUES (?, ?) ON CONFLICT(key) DO UPDATE SET value = excluded.value",
                    (key, str(payload[key])),
                )
        for row in conn.execute("SELECT id FROM jobs"):
            apply_filter(conn, row["id"])
        return jsonify({"settings": settings(conn), "jobs": list_jobs(conn, include_filtered=True)})


@app.errorhandler(Exception)
def api_error(exc):
    if isinstance(exc, HTTPException):
        return exc
    return jsonify({"error": str(exc)}), 500


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
            with connect() as conn:
                conn.execute(
                    "INSERT INTO search_runs(started_at, completed_at, trigger, status, message) VALUES (?, ?, 'scheduled', 'error', ?)",
                    (now(), now(), str(exc)),
                )
        time.sleep(15 * 60)


def start_scheduler():
    if not AUTORUN:
        return
    thread = threading.Thread(target=scheduler_loop, name="job-search-scheduler", daemon=True)
    thread.start()


INDEX_HTML = r"""<!doctype html>
<html lang="en">
<head>
  <meta charset="utf-8">
  <meta name="viewport" content="width=device-width, initial-scale=1">
  <title>Job Search Console</title>
  <style>
    :root {
      --bg: #eef2ef;
      --ink: #15201a;
      --muted: #5e6a61;
      --line: #c6d0c8;
      --panel: #fbfcfa;
      --accent: #0f766e;
      --accent-2: #8a4b20;
      --danger: #9f2436;
      --ok: #2f7d32;
      --warn: #b26a00;
    }
    * { box-sizing: border-box; }
    body {
      margin: 0;
      color: var(--ink);
      background:
        linear-gradient(135deg, rgba(15,118,110,.12), transparent 34%),
        linear-gradient(225deg, rgba(138,75,32,.10), transparent 40%),
        var(--bg);
      font-family: "Avenir Next", "Segoe UI", sans-serif;
    }
    header {
      padding: 10px 18px;
      border-bottom: 1px solid var(--line);
      background: rgba(251,252,250,.82);
      position: sticky;
      top: 0;
      z-index: 5;
      backdrop-filter: blur(14px);
    }
    .header-inner {
      display: grid;
      grid-template-columns: auto 1fr auto auto;
      gap: 14px;
      align-items: center;
    }
    h1 { margin: 0; font-size: 20px; line-height: 1.05; white-space: nowrap; }
    .subtitle {
      color: var(--muted);
      font-size: 13px;
      line-height: 1.25;
      max-width: 850px;
    }
    nav { display: flex; gap: 6px; margin: 0; }
    nav button { width: auto; padding: 6px 10px; }
    .activity-pill {
      display: none;
      align-items: center;
      gap: 7px;
      justify-content: center;
      min-width: 150px;
      border: 1px solid rgba(15,118,110,.35);
      border-radius: 999px;
      padding: 6px 10px;
      background: rgba(15,118,110,.10);
      color: var(--accent);
      font-size: 12px;
      font-weight: 750;
      white-space: nowrap;
    }
    .activity-pill.visible { display: inline-flex; }
    .activity-pill.error {
      border-color: rgba(159,36,54,.35);
      background: rgba(159,36,54,.09);
      color: var(--danger);
    }
    main {
      display: grid;
      grid-template-columns: 300px 1fr;
      gap: 18px;
      padding: 12px 18px 18px;
      align-items: start;
    }
    section, .panel {
      background: var(--panel);
      border: 1px solid var(--line);
      border-radius: 8px;
      padding: 16px;
    }
    h2 { margin: 0 0 12px; font-size: 18px; }
    h3 { margin: 16px 0 8px; font-size: 15px; }
    label { display: block; font-size: 12px; color: var(--muted); margin: 10px 0 4px; }
    input, textarea, select, button {
      width: 100%;
      font: inherit;
      border: 1px solid var(--line);
      border-radius: 6px;
      padding: 9px 10px;
      background: white;
      color: var(--ink);
    }
    textarea { min-height: 100px; resize: vertical; }
    button {
      cursor: pointer;
      background: var(--accent);
      color: white;
      border-color: var(--accent);
      font-weight: 650;
    }
    button.secondary { background: white; color: var(--ink); border-color: var(--line); }
    button.warn { background: var(--accent-2); border-color: var(--accent-2); }
    button.danger { background: var(--danger); border-color: var(--danger); }
    details {
      border-top: 1px solid var(--line);
      margin-top: 12px;
      padding-top: 10px;
    }
    summary {
      cursor: pointer;
      color: var(--muted);
      font-weight: 650;
      font-size: 13px;
    }
    .grid2 { display: grid; grid-template-columns: 1fr 1fr; gap: 10px; }
    .row { display: flex; gap: 8px; align-items: center; }
    .row > * { flex: 1; }
    .page { display: grid; gap: 14px; }
    #jobs_page {
      height: var(--jobs-page-height, calc(100vh - 132px));
      min-height: 420px;
      display: flex;
      flex-direction: column;
      gap: 0;
    }
    .jobs-master-panel {
      flex: 1 1 auto;
      min-height: 180px;
      overflow: hidden;
      display: flex;
      flex-direction: column;
    }
    .jobs-master-panel .table-wrap {
      flex: 1 1 auto;
      min-height: 0;
      overflow: auto;
    }
    .split-divider {
      flex: 0 0 12px;
      cursor: row-resize;
      display: grid;
      place-items: center;
      touch-action: none;
    }
    .split-divider::before {
      content: "";
      width: 72px;
      height: 4px;
      border-radius: 999px;
      background: var(--line);
      box-shadow: 0 1px 0 rgba(255,255,255,.7);
    }
    .split-divider:hover::before,
    .split-divider.dragging::before {
      background: var(--accent);
    }
    .hidden { display: none; }
    .table-wrap { overflow-x: auto; }
    table { width: 100%; border-collapse: collapse; background: white; border: 1px solid var(--line); }
    th, td { border-bottom: 1px solid var(--line); padding: 10px; text-align: left; vertical-align: top; }
    th { color: var(--muted); font-size: 12px; font-weight: 750; background: #f6f8f5; }
    tr { cursor: pointer; }
    tr.active { outline: 2px solid var(--accent); outline-offset: -2px; }
    tr.filtered { opacity: .62; }
    .job-title { font-weight: 750; }
    .meta { color: var(--muted); font-size: 13px; margin-top: 3px; }
    .chips { display: flex; flex-wrap: wrap; gap: 6px; margin-top: 8px; }
    .chip {
      border: 1px solid var(--line);
      border-radius: 99px;
      padding: 3px 8px;
      font-size: 12px;
      background: #f6f8f5;
    }
    .score-good { color: var(--ok); font-weight: 750; }
    .score-warn { color: var(--warn); font-weight: 750; }
    .score-bad { color: var(--danger); font-weight: 750; }
    .status-pill {
      display: inline-flex;
      align-items: center;
      gap: 6px;
      border: 1px solid var(--line);
      border-radius: 999px;
      padding: 5px 9px;
      margin: 10px 0;
      background: #f6f8f5;
      font-size: 12px;
      font-weight: 700;
    }
    .status-pill.running { color: var(--accent); }
    .status-pill.complete { color: var(--ok); }
    .status-pill.error { color: var(--danger); }
    .spinner {
      width: 10px;
      height: 10px;
      border: 2px solid rgba(15,118,110,.25);
      border-top-color: var(--accent);
      border-radius: 50%;
      animation: spin .8s linear infinite;
    }
    @keyframes spin { to { transform: rotate(360deg); } }
    .detail {
      flex: 0 0 var(--detail-height, 50%);
      min-height: 180px;
      overflow: auto;
      display: grid;
      gap: 14px;
      padding-bottom: 18px;
    }
    .score-grid { display: grid; grid-template-columns: repeat(4, minmax(130px, 1fr)); gap: 8px; }
    .score-grid input { text-align: right; }
    .empty {
      min-height: 420px;
      display: grid;
      place-items: center;
      color: var(--muted);
      text-align: center;
    }
    .note, .interaction {
      border-top: 1px solid var(--line);
      padding-top: 10px;
      margin-top: 10px;
    }
    .toolbar { display: flex; gap: 10px; align-items: end; margin-bottom: 8px; }
    .toolbar.wrap { flex-wrap: wrap; }
    .toolbar label { margin-top: 0; }
    .toolbar .grow { flex: 1 1 auto; }
    .action-cluster {
      display: grid;
      grid-template-columns: minmax(160px, 220px) auto;
      gap: 8px;
      align-items: end;
      flex: 0 1 320px;
    }
    .action-cluster button { white-space: nowrap; }
    .compact-heading {
      display: flex;
      gap: 10px;
      align-items: baseline;
      flex-wrap: wrap;
    }
    .compact-heading h2 { margin: 0; }
    .filter-rollup {
      width: 100%;
      margin-top: 0;
      padding-top: 6px;
    }
    .filter-rollup[open] {
      display: grid;
      gap: 10px;
    }
    .filter-controls {
      display: grid;
      grid-template-columns: repeat(4, minmax(150px, 1fr));
      gap: 10px;
      width: 100%;
      align-items: end;
    }
    .status-filter-list {
      display: flex;
      flex-wrap: wrap;
      gap: 6px;
      margin-top: 6px;
      max-width: 700px;
    }
    .status-filter-list label {
      display: inline-flex;
      align-items: center;
      gap: 5px;
      margin: 0;
      padding: 4px 7px;
      border: 1px solid var(--line);
      border-radius: 999px;
      font-size: 12px;
      color: var(--muted);
      background: rgba(255,255,255,.55);
    }
    .status-filter-list input { width: auto; margin: 0; }
    .small { font-size: 12px; color: var(--muted); }
    .checkbox-row {
      display: flex;
      gap: 8px;
      align-items: center;
      margin: 10px 0;
      color: var(--muted);
      font-size: 12px;
    }
    .checkbox-row input { width: auto; }
    .sidebar-block { margin-top: 14px; }
    .enabled-query {
      border-top: 1px solid var(--line);
      padding: 8px 0;
    }
    .packet-row {
      display: grid;
      grid-template-columns: minmax(160px, 1fr) auto;
      gap: 8px;
      align-items: end;
    }
    .bulk-actions {
      display: flex;
      gap: 8px;
      align-items: center;
      flex-wrap: nowrap;
      width: 100%;
      padding-top: 6px;
      border-top: 1px solid var(--line);
      overflow-x: auto;
    }
    .bulk-actions button {
      white-space: nowrap;
      padding: 6px 8px;
      font-size: 12px;
      width: auto;
      min-height: 0;
    }
    .select-cell { width: 34px; text-align: center; }
    .select-cell input { width: auto; }
    .level-cell { max-width: 190px; }
    .level-preview {
      display: -webkit-box;
      -webkit-line-clamp: 3;
      -webkit-box-orient: vertical;
      overflow: hidden;
      line-height: 1.25;
    }
    .task-status {
      flex: 1 1 auto;
      min-width: 220px;
      border: 1px solid var(--line);
      border-radius: 999px;
      background: rgba(255,255,255,.65);
      padding: 5px 9px;
      font-size: 12px;
      color: var(--muted);
      white-space: nowrap;
      overflow: hidden;
      text-overflow: ellipsis;
    }
    @media (max-width: 980px) {
      .header-inner { grid-template-columns: 1fr; gap: 8px; }
      .activity-pill { justify-content: flex-start; width: fit-content; }
      h1 { white-space: normal; }
      main { grid-template-columns: 1fr; }
      #jobs_page { height: auto; min-height: 0; }
      .jobs-master-panel, .detail { min-height: 0; overflow: visible; }
      .split-divider { display: none; }
      .score-grid { grid-template-columns: 1fr 1fr; }
    }
  </style>
</head>
<body>
  <header>
    <div class="header-inner">
      <h1>Job Search Console</h1>
      <div class="subtitle">Score opportunities against the ideal problem set, track applications like a CRM, and preserve learning from every conversation.</div>
      <nav>
        <button id="jobs_nav" onclick="showPage('jobs')">Jobs</button>
        <button id="companies_nav" class="secondary" onclick="showPage('companies')">Companies</button>
        <button id="queries_nav" class="secondary" onclick="showPage('queries')">Queries</button>
      </nav>
      <div id="global_activity" class="activity-pill" role="status" aria-live="polite"></div>
    </div>
  </header>
  <main>
    <aside>
      <section>
        <h2>Filters</h2>
        <div class="grid2">
          <div><label>Codex threshold</label><input id="gpt_threshold" type="number" min="0" max="100"></div>
          <div><label>User threshold</label><input id="user_threshold" type="number" min="0" max="100"></div>
        </div>
        <label>Codex model override</label><input id="codex_model" placeholder="required for attributable packet generation">
        <div class="row" style="margin-top: 10px;">
          <button onclick="saveSettings()">Save</button>
        </div>
        <p class="small">Jobs are filtered when user score is below threshold. Codex threshold applies only when Codex scoring is enabled.</p>
      </section>
      <section class="sidebar-block">
        <h2>Search</h2>
        <button id="run_search_button" class="warn" onclick="runSearch()">Run job search now</button>
        <label class="checkbox-row"><input id="force_refresh" type="checkbox"> Force refresh, bypass replay cache</label>
        <div id="search_status" class="status-pill">Idle</div>
        <p class="small">Daily search runs use saved LinkedIn and Indeed queries. Downlevel jobs are tracked but hidden by default.</p>
        <details>
          <summary>Show enabled queries and recent runs</summary>
          <h3>Enabled Queries</h3>
          <div id="enabled_queries" class="small"></div>
          <h3>Recent Runs</h3>
          <div id="search_runs" class="small"></div>
        </details>
      </section>
      <section class="sidebar-block">
        <h2>Configuration</h2>
        <p class="small">Saved to <code>job-search-tool/.env</code>. Existing keys are masked.</p>
        <details>
          <summary>Codex CLI and model</summary>
          <label>Codex CLI path</label><input id="config_CODEX_CLI_PATH" placeholder="codex">
          <label>Codex model</label><input id="config_CODEX_MODEL" placeholder="required for attributable packet generation">
          <label>Enable Codex scoring</label><select id="config_JOB_SEARCH_ENABLE_GPT_SCORING"><option value="0">disabled</option><option value="1">enabled</option></select>
          <label>Use captured responses</label><select id="config_JOB_SEARCH_USE_CAPTURE_CACHE"><option value="1">enabled</option><option value="0">disabled</option></select>
          <button class="secondary" onclick="saveConfig()">Save configuration</button>
          <div id="config_status" class="small"></div>
        </details>
        <details>
          <summary>Advanced commands</summary>
          <p class="small">For user-acceptance testing. This deletes tracked jobs and their CRM notes/interactions, but keeps searches, settings, logs, captures, and discovery history.</p>
          <button class="danger" onclick="purgeTrackedJobs()">Purge tracked jobs</button>
        </details>
      </section>
      <section class="sidebar-block">
        <h2>Manual Entry</h2>
        <p class="small">Use this for referrals, hidden roles, or postings found outside automated search.</p>
        <details>
          <summary>Add job from URL</summary>
          <label>URL</label><input id="url">
          <label>Pipeline</label><select id="pipeline"></select>
          <label class="checkbox-row"><input id="manual_force_refresh" type="checkbox"> Force fresh scrape</label>
          <button onclick="createJob()">Scrape and add job</button>
          <p class="small">The tool will fetch the posting and populate company, title, location, and posting text from the URL.</p>
        </details>
      </section>
    </aside>
    <div>
      <section id="jobs_page" class="page">
        <div class="panel jobs-master-panel">
          <div class="toolbar wrap">
            <div class="compact-heading">
              <h2>Tracked Jobs</h2>
              <div id="job_filter_summary" class="small">Master list of tracked jobs. Select a row to edit CRM details below.</div>
            </div>
            <details id="job_filter_rollup" class="filter-rollup" ontoggle="saveJobFilterRollupState()">
              <summary>Edit table filters</summary>
              <div class="filter-controls">
                <div>
                  <label>Pipeline</label>
                  <select id="pipeline_view_filter" onchange="saveJobTableFilters(); renderJobs()"></select>
                </div>
                <div>
                  <label>Source</label>
                  <select id="source_view_filter" onchange="saveJobTableFilters(); renderJobs()"></select>
                </div>
                <div>
                  <label>Visibility</label>
                  <select id="visibility_filter" onchange="saveJobTableFilters(); renderJobs()">
                    <option value="active">Hide filtered/downlevel</option>
                    <option value="all">Show all</option>
                    <option value="filtered">Only filtered</option>
                    <option value="downlevel">Only downlevel</option>
                  </select>
                </div>
                <div>
                  <label>Search</label>
                  <input id="job_text_filter" placeholder="Company, title, location" oninput="saveJobTableFilters(); renderJobs()">
                </div>
              </div>
              <div>
                <label>Status</label>
                <div id="status_filter_list" class="status-filter-list"></div>
              </div>
              <button class="secondary" onclick="resetJobTableFilters()">Reset table filters</button>
            </details>
            <div class="bulk-actions">
              <button class="secondary" onclick="selectVisibleJobs()">Select visible</button>
              <button class="secondary" onclick="clearBulkSelection()">Clear selection</button>
              <button id="bulk_score_button" class="warn" onclick="startBulkScorecards()">Bulk Codex scorecards</button>
              <button id="bulk_packet_button" class="warn" onclick="startBulkApplicationPackets()">Bulk generate packets</button>
              <span id="bulk_selection_summary" class="small">0 selected</span>
              <div id="bulk_task_status" class="task-status">No bulk Codex task running.</div>
            </div>
          </div>
          <div id="jobs" class="table-wrap"></div>
        </div>
        <div id="jobs_split_divider" class="split-divider" title="Drag to resize job detail pane"></div>
        <div id="detail" class="detail">
          <div class="empty">Select a job from the table.</div>
        </div>
      </section>
      <section id="companies_page" class="page hidden">
        <div class="panel">
          <div class="toolbar wrap">
            <div>
              <h2>Company Interest</h2>
              <div class="small">Track company-level interest separately from individual roles.</div>
            </div>
          </div>
          <details>
            <summary>Add company interest</summary>
            <div class="grid2">
              <div><label>Company</label><input id="company_interest_name"></div>
              <div><label>Status</label><select id="company_interest_status"><option>watching</option><option>target</option><option>active_conversation</option><option>paused</option><option>not_interested</option></select></div>
            </div>
            <label>Interest score</label><input id="company_interest_score" type="number" min="0" max="100">
            <label>Rationale</label><textarea id="company_interest_rationale"></textarea>
            <label>Contacts</label><textarea id="company_interest_contacts" placeholder="People, teams, recruiters, referrals"></textarea>
            <label>Next step</label><input id="company_interest_next_step">
            <label>Notes</label><textarea id="company_interest_notes"></textarea>
            <button class="secondary" onclick="createCompanyInterest()">Track company</button>
          </details>
          <div id="company_table" class="table-wrap"></div>
          <div id="company_detail" class="detail">
            <div class="empty">Select a company.</div>
          </div>
        </div>
      </section>
      <section id="queries_page" class="page hidden">
        <div class="panel">
          <h2>Search Queries</h2>
          <p class="small">Manage saved searches. Seeded searches cover every pipeline/job-board pair; custom searches can be added here.</p>
          <details>
            <summary>Add search query</summary>
            <div class="grid2">
              <div><label>Board</label><select id="search_board"><option>linkedin</option><option>indeed</option></select></div>
              <div><label>Pipeline</label><select id="search_pipeline"></select></div>
            </div>
            <label>Location</label><input id="search_location" value="Remote">
            <label>Keywords</label><input id="search_keywords" placeholder="Office of the CTO">
            <label>Criteria</label><textarea id="search_criteria" placeholder="What this search is intended to find."></textarea>
            <button class="secondary" onclick="addSearchQuery()">Add search query</button>
          </details>
          <div id="query_table" class="table-wrap"></div>
        </div>
      </section>
    </div>
  </main>
  <datalist id="score_options">
    <option value="0"></option>
    <option value="1"></option>
    <option value="2"></option>
    <option value="3"></option>
    <option value="4"></option>
    <option value="5"></option>
    <option value="6"></option>
    <option value="7"></option>
    <option value="8"></option>
    <option value="9"></option>
    <option value="10"></option>
  </datalist>
  <script>
    const rubric = [
      "interesting_technical_problems",
      "organizational_influence",
      "cross_functional_work",
      "opportunity_to_mentor",
      "work_life_balance",
      "low_operational_burden",
      "compensation",
      "mission",
    ];
    const statusOptions = ["researching","interested","applied","interviewing","offer","rejected","declined","paused"];
    const defaultVisibleStatuses = statusOptions.filter(s => !["rejected", "declined"].includes(s));
    const companyStatuses = ["watching","target","active_conversation","paused","not_interested"];
    let state = { jobs: [], settings: {}, pipelines: [], rubric_fields: rubric, application_packets: [] };
    let selectedId = null;
    let selectedJob = null;
    let selectedCompanyId = null;
    let jobTableFilters = loadJobTableFilters();
    let jobFilterRollupOpen = localStorage.getItem("jobFilterRollupOpen") === "1";
    let currentPage = "jobs";
    let searchRunning = false;
    let splitInitialized = false;
    let pendingCallCount = 0;
    let pendingCallLabels = [];
    let bulkSelectedJobIds = new Set();
    let activeBulkTaskId = localStorage.getItem("activeBulkTaskId") || "";
    let bulkTaskPollTimer = null;

    const pretty = s => s.replace(/_/g, " ").replace(/\b\w/g, c => c.toUpperCase());
    const scoreClass = n => n == null ? "" : n >= 70 ? "score-good" : n >= 40 ? "score-warn" : "score-bad";
    const levelStatus = item => item.level_assessment || "Unknown - level not assessed";
    const levelPreview = item => {
      const value = levelStatus(item);
      return value.length > 100 ? `${value.slice(0, 97).trim()}...` : value;
    };
    const scoreText = value => value == null ? "n/a" : value;
    const fieldValue = (object, field, fallback) => object && object[field] != null ? object[field] : fallback;
    const elementValue = (id, fallback = "") => {
      const element = document.getElementById(id);
      return element ? element.value : fallback;
    };
    const formatTimestamp = ts => ts ? new Date(ts * 1000).toLocaleString() : "not scheduled";
    const normalizeCompanyName = value => String(value || "").toLowerCase().replace(/[^a-z0-9]+/g, " ").trim();

    function findCompanyInterestByName(companyName) {
      const normalized = normalizeCompanyName(companyName);
      return (state.company_interests || []).find(company => normalizeCompanyName(company.company) === normalized);
    }

    function loadJobTableFilters() {
      try {
        const parsed = JSON.parse(localStorage.getItem("jobTableFilters") || "{}");
        return {
          pipeline: parsed.pipeline || "",
          source: parsed.source || "",
          visibility: parsed.visibility || "active",
          text: parsed.text || "",
          statuses: Array.isArray(parsed.statuses) && parsed.statuses.length ? parsed.statuses : defaultVisibleStatuses,
        };
      } catch (err) {
        return { pipeline: "", source: "", visibility: "active", text: "", statuses: defaultVisibleStatuses };
      }
    }

    function saveJobTableFilters() {
      const checkedStatuses = [...document.querySelectorAll("#status_filter_list input:checked")].map(input => input.value);
      jobTableFilters = {
        pipeline: elementValue("pipeline_view_filter"),
        source: elementValue("source_view_filter"),
        visibility: elementValue("visibility_filter", "active"),
        text: elementValue("job_text_filter"),
        statuses: checkedStatuses,
      };
      localStorage.setItem("jobTableFilters", JSON.stringify(jobTableFilters));
    }

    function resetJobTableFilters() {
      jobTableFilters = { pipeline: "", source: "", visibility: "active", text: "", statuses: defaultVisibleStatuses };
      localStorage.setItem("jobTableFilters", JSON.stringify(jobTableFilters));
      renderJobTableFilterControls();
      renderJobs();
    }

    function saveJobFilterRollupState() {
      const rollup = document.getElementById("job_filter_rollup");
      if (!rollup) return;
      jobFilterRollupOpen = rollup.open;
      localStorage.setItem("jobFilterRollupOpen", rollup.open ? "1" : "0");
    }

    function activityLabel(path) {
      if (path.includes("/bulk/score-gpt")) return "Starting bulk Codex scoring";
      if (path.includes("/bulk/application-packets")) return "Starting bulk packet generation";
      if (path.includes("/codex-tasks")) return "Checking Codex task";
      if (path.includes("/score-gpt")) return "Codex scoring";
      if (path.includes("/application-packet/generate")) return "Generating application packet";
      if (path.includes("/application-packet/attach")) return "Attaching application packet";
      if (path.includes("/application-packet/content")) return "Loading application content";
      if (path === "/api/search/run") return "Job search running";
      if (path === "/api/config") return "Saving configuration";
      if (path === "/api/state?include_filtered=1") return "Refreshing data";
      return "Working";
    }

    function renderGlobalActivity() {
      const activity = document.getElementById("global_activity");
      if (!activity) return;
      if (pendingCallCount <= 0) {
        activity.className = "activity-pill";
        activity.innerHTML = "";
        return;
      }
      const label = pendingCallLabels[pendingCallLabels.length - 1] || "Working";
      activity.className = "activity-pill visible";
      activity.innerHTML = `<span class="spinner"></span>${escapeHtml(label)}${pendingCallCount > 1 ? ` (${pendingCallCount})` : ""}`;
    }

    async function api(path, options = {}) {
      const { activityLabel: explicitActivityLabel, ...fetchOptions } = options;
      const label = explicitActivityLabel || activityLabel(path);
      pendingCallCount += 1;
      pendingCallLabels.push(label);
      renderGlobalActivity();
      try {
        const response = await fetch(path, {
          headers: { "Content-Type": "application/json" },
          ...fetchOptions,
        });
        const data = await response.json();
        if (!response.ok) throw new Error(data.error || "Request failed");
        return data;
      } finally {
        pendingCallCount = Math.max(0, pendingCallCount - 1);
        const labelIndex = pendingCallLabels.lastIndexOf(label);
        if (labelIndex >= 0) pendingCallLabels.splice(labelIndex, 1);
        renderGlobalActivity();
      }
    }

    async function load() {
      state = await api("/api/state?include_filtered=1");
      document.getElementById("gpt_threshold").value = state.settings.gpt_threshold;
      document.getElementById("user_threshold").value = state.settings.user_threshold;
      document.getElementById("codex_model").value = state.settings.codex_model || "";
      const pipeline = document.getElementById("pipeline");
      pipeline.innerHTML = '<option value=""></option>' + state.pipelines.map(p => `<option>${p}</option>`).join("");
      document.getElementById("search_pipeline").innerHTML = state.pipelines.map(p => `<option>${p}</option>`).join("");
      const pipelineView = document.getElementById("pipeline_view_filter");
      pipelineView.innerHTML = '<option value="">All pipelines</option>' + state.pipelines.map(p => `<option>${p}</option>`).join("");
      const filterRollup = document.getElementById("job_filter_rollup");
      if (filterRollup) filterRollup.open = jobFilterRollupOpen;
      renderJobTableFilterControls();
      renderSearchState();
      renderConfigStatus();
      renderJobs();
      renderBulkControls();
      renderCompanyTable();
      renderQueryTable();
      initializeJobSplit();
      if (selectedId) await selectJob(selectedId, false);
      if (selectedCompanyId) await selectCompany(selectedCompanyId, false);
    }

    function renderSearchState() {
      const enabled = (state.search_queries || []).filter(q => q.enabled);
      const latest = (state.search_runs || [])[0];
      const schedule = state.search_schedule || {};
      const nextSearchText = schedule.autorun_enabled
        ? `Next scheduled search: ${formatTimestamp(schedule.next_run_at)}`
        : "Scheduled search disabled";
      const status = document.getElementById("search_status");
      const runButton = document.getElementById("run_search_button");
      if (searchRunning) {
        status.className = "status-pill running";
        status.innerHTML = '<span class="spinner"></span> Search running';
        runButton.disabled = true;
      } else if (latest) {
        status.className = `status-pill ${latest.status}`;
        status.textContent = `${latest.status}: found ${latest.found_count}, tracked ${latest.tracked_count}, rejected ${latest.rejected_count}`;
        runButton.disabled = false;
      } else {
        status.className = "status-pill";
        status.textContent = "Idle";
        runButton.disabled = false;
      }
      const enabledHtml = enabled.map(q => `
        <div class="enabled-query">
          <b>${escapeHtml(q.pipeline || "Custom")}</b>
          <br>${escapeHtml(q.board)} · ${escapeHtml(q.location || "")}
          <br>${escapeHtml(q.keywords)}
        </div>
      `).join("") || '<div class="enabled-query">No enabled queries.</div>';
      document.getElementById("enabled_queries").innerHTML = `${enabledHtml}<div class="enabled-query"><b>${escapeHtml(nextSearchText)}</b></div>`;
      document.getElementById("search_runs").innerHTML = (state.search_runs || []).slice(0, 5).map(r => `
        <div class="note">
          <b>${escapeHtml(r.trigger)}</b> · ${escapeHtml(r.status)}
          <br>found ${r.found_count}, tracked ${r.tracked_count}, rejected ${r.rejected_count}
          <br>${new Date(r.started_at * 1000).toLocaleString()}
          ${r.message ? `<br>${escapeHtml(r.message)}` : ""}
        </div>
      `).join("") || "No search runs yet.";
    }

    function renderConfigStatus() {
      const config = state.config || {};
      const keyLine = key => {
        const item = config[key] || {};
        return `${key}: ${item.configured ? item.masked : "not set"}`;
      };
      document.getElementById("config_status").innerHTML = `
        ${["CODEX_CLI_PATH","CODEX_MODEL"].map(keyLine).join("<br>")}
        <br>API log: ${escapeHtml(state.api_log_path || "")}
        <br>Decision log: ${escapeHtml(state.event_log_path || "")}
        <br>Captures: ${escapeHtml(state.capture_dir || "")}
        <br>Codex scoring: ${state.gpt_scoring_enabled ? "enabled" : "disabled"}
        <br>Replay cache: ${state.capture_cache_enabled ? "enabled" : "disabled"}
      `;
      if (!document.getElementById("config_CODEX_CLI_PATH").value) {
        document.getElementById("config_CODEX_CLI_PATH").value = (state.config.CODEX_CLI_PATH || {}).configured ? "" : "codex";
      }
      if (!document.getElementById("config_CODEX_MODEL").value) {
        document.getElementById("config_CODEX_MODEL").value = state.settings.codex_model || "";
      }
      document.getElementById("config_JOB_SEARCH_ENABLE_GPT_SCORING").value = state.gpt_scoring_enabled ? "1" : "0";
      document.getElementById("config_JOB_SEARCH_USE_CAPTURE_CACHE").value = state.capture_cache_enabled ? "1" : "0";
    }

    function renderJobTableFilterControls() {
      const pipelineView = document.getElementById("pipeline_view_filter");
      const sourceView = document.getElementById("source_view_filter");
      const visibility = document.getElementById("visibility_filter");
      const text = document.getElementById("job_text_filter");
      const statusList = document.getElementById("status_filter_list");
      if (!pipelineView || !sourceView || !visibility || !text || !statusList) return;

      pipelineView.value = state.pipelines.includes(jobTableFilters.pipeline) ? jobTableFilters.pipeline : "";
      const sources = [...new Set((state.jobs || []).map(job => job.source_board || "manual"))].sort();
      sourceView.innerHTML = '<option value="">All sources</option>' + sources.map(source => `<option>${escapeHtml(source)}</option>`).join("");
      sourceView.value = sources.includes(jobTableFilters.source) ? jobTableFilters.source : "";
      visibility.value = ["active", "all", "filtered", "downlevel"].includes(jobTableFilters.visibility) ? jobTableFilters.visibility : "active";
      text.value = jobTableFilters.text || "";
      const statuses = [...new Set([...statusOptions, ...(state.jobs || []).map(job => job.status || "").filter(Boolean)])];
      statusList.innerHTML = statuses.map(status => `
        <label><input type="checkbox" value="${escapeAttr(status)}" ${jobTableFilters.statuses.includes(status) ? "checked" : ""} onchange="saveJobTableFilters(); renderJobs()"> ${escapeHtml(status)}</label>
      `).join("");
    }

    function jobMatchesTableFilters(job) {
      if (jobTableFilters.pipeline && (job.pipeline || "") !== jobTableFilters.pipeline) return false;
      if (jobTableFilters.source && (job.source_board || "manual") !== jobTableFilters.source) return false;
      if (!jobTableFilters.statuses.includes(job.status || "")) return false;
      if (jobTableFilters.visibility === "active" && (job.filtered || job.downlevel)) return false;
      if (jobTableFilters.visibility === "filtered" && !job.filtered) return false;
      if (jobTableFilters.visibility === "downlevel" && !job.downlevel) return false;
      const text = (jobTableFilters.text || "").trim().toLowerCase();
      if (text) {
        const haystack = [job.company, job.title, job.location, job.pipeline, job.status, job.source_board].join(" ").toLowerCase();
        if (!haystack.includes(text)) return false;
      }
      return true;
    }

    function visibleJobIds() {
      return state.jobs.filter(jobMatchesTableFilters).map(job => job.id);
    }

    function selectedJobIds() {
      return [...bulkSelectedJobIds].filter(id => state.jobs.some(job => job.id === id));
    }

    function toggleBulkJobSelection(id, checked) {
      if (checked) bulkSelectedJobIds.add(id);
      else bulkSelectedJobIds.delete(id);
      renderBulkControls();
    }

    function selectVisibleJobs() {
      visibleJobIds().forEach(id => bulkSelectedJobIds.add(id));
      renderJobs();
      renderBulkControls();
    }

    function clearBulkSelection() {
      bulkSelectedJobIds.clear();
      renderJobs();
      renderBulkControls();
    }

    function renderBulkControls(task = null) {
      const selected = selectedJobIds();
      bulkSelectedJobIds = new Set(selected);
      const summary = document.getElementById("bulk_selection_summary");
      if (summary) summary.textContent = `${selected.length} selected`;
      const scoreButton = document.getElementById("bulk_score_button");
      if (scoreButton) scoreButton.disabled = !state.gpt_scoring_enabled || selected.length === 0;
      const packetButton = document.getElementById("bulk_packet_button");
      if (packetButton) packetButton.disabled = selected.length === 0;
      const status = document.getElementById("bulk_task_status");
      if (!status) return;
      const currentTask = task || (state.codex_tasks || []).find(t => t.id === activeBulkTaskId);
      if (!currentTask) {
        status.textContent = "No bulk Codex task running.";
        return;
      }
      const parts = [
        `${currentTask.operation}: ${currentTask.status}`,
        `${currentTask.completed || 0}/${currentTask.total || 0} complete`,
        `${currentTask.skipped || 0} skipped`,
        `${currentTask.failed || 0} failed`,
      ];
      if (currentTask.message) parts.push(currentTask.message);
      status.innerHTML = `${["running", "queued"].includes(currentTask.status) ? '<span class="spinner"></span> ' : ""}${escapeHtml(parts.join(" · "))}`;
    }

    function startBulkTaskPolling(taskId) {
      activeBulkTaskId = taskId;
      localStorage.setItem("activeBulkTaskId", taskId);
      if (bulkTaskPollTimer) clearInterval(bulkTaskPollTimer);
      bulkTaskPollTimer = setInterval(() => pollBulkTask(taskId), 3000);
      pollBulkTask(taskId);
    }

    async function pollBulkTask(taskId) {
      try {
        const { task } = await api(`/api/codex-tasks/${taskId}`, { activityLabel: "Checking Codex task" });
        renderBulkControls(task);
        if (!["queued", "running"].includes(task.status)) {
          clearInterval(bulkTaskPollTimer);
          bulkTaskPollTimer = null;
          await load();
        }
      } catch (err) {
        if (bulkTaskPollTimer) clearInterval(bulkTaskPollTimer);
        bulkTaskPollTimer = null;
        renderBulkControls({ operation: "bulk", status: "error", completed: 0, total: 0, skipped: 0, failed: 1, message: err.message });
      }
    }

    function renderJobs() {
      const jobs = document.getElementById("jobs");
      const visibleJobs = state.jobs.filter(jobMatchesTableFilters);
      const hiddenCount = state.jobs.length - visibleJobs.length;
      document.getElementById("job_filter_summary").textContent = `${visibleJobs.length} visible, ${hiddenCount} hidden by table filters. Select a row to edit CRM details below.`;
      if (!visibleJobs.length) {
        jobs.innerHTML = '<div class="small">No jobs match the current filter.</div>';
        return;
      }
      jobs.innerHTML = `
        <table>
          <thead>
            <tr>
              <th class="select-cell">Pick</th>
              <th>Company</th>
              <th>Role</th>
              <th>Pipeline</th>
              <th>Status</th>
              <th>Codex</th>
              <th>Mine</th>
              <th>Level</th>
              <th>Source</th>
            </tr>
          </thead>
          <tbody>
            ${visibleJobs.map(job => `
              <tr class="${job.filtered ? "filtered" : ""} ${job.id === selectedId ? "active" : ""}" onclick="selectJob(${job.id})">
                <td class="select-cell"><input type="checkbox" ${bulkSelectedJobIds.has(job.id) ? "checked" : ""} onclick="event.stopPropagation()" onchange="toggleBulkJobSelection(${job.id}, this.checked)"></td>
                <td><b>${escapeHtml(job.company)}</b><div class="small">${escapeHtml(job.location || "")}</div></td>
                <td><span class="job-title">${escapeHtml(job.title)}</span>${job.url ? `<div class="small"><a href="${escapeAttr(job.url)}" target="_blank">posting</a></div>` : ""}</td>
                <td>${escapeHtml(job.pipeline || "Unassigned")}</td>
                <td>${escapeHtml(job.status || "")}${job.application_packet_path ? '<div class="small">packet attached</div>' : ""}${job.filtered ? '<div class="small">filtered/downlevel hidden by default</div>' : ""}</td>
                <td><b class="${scoreClass(job.gpt_score)}">${scoreText(job.gpt_score)}</b></td>
                <td><b class="${scoreClass(job.user_score)}">${scoreText(job.user_score)}</b></td>
                <td class="level-cell" title="${escapeAttr(levelStatus(job))}"><span class="level-preview">${escapeHtml(levelPreview(job))}</span>${job.downlevel ? '<div class="small">downlevel</div>' : ""}</td>
                <td>${escapeHtml(job.source_board || "manual")}</td>
              </tr>
            `).join("")}
          </tbody>
        </table>
      `;
    }

    function renderQueryTable() {
      const el = document.getElementById("query_table");
      const queries = state.search_queries || [];
      if (!queries.length) {
        el.innerHTML = '<div class="small">No saved queries.</div>';
        return;
      }
      el.innerHTML = `
        <table>
          <thead>
            <tr>
              <th>Enabled</th>
              <th>Board</th>
              <th>Pipeline</th>
              <th>Keywords</th>
              <th>Location</th>
              <th>Refinement</th>
            </tr>
          </thead>
          <tbody>
            ${queries.map(q => `
              <tr>
                <td><button class="secondary" onclick="toggleQuery(${q.id}, ${q.enabled ? "false" : "true"})">${q.enabled ? "Disable" : "Enable"}</button></td>
                <td>${escapeHtml(q.board)}</td>
                <td>${escapeHtml(q.pipeline || "Custom")}<div class="small">${q.seeded ? "seeded" : "custom"}</div></td>
                <td>${escapeHtml(q.keywords)}<div class="small">${escapeHtml(q.criteria || "")}</div></td>
                <td>${escapeHtml(q.location || "")}</td>
                <td>${q.last_run_at ? new Date(q.last_run_at * 1000).toLocaleString() : "never"}<div class="small">${escapeHtml(q.refinement_notes || "")}</div></td>
              </tr>
            `).join("")}
          </tbody>
        </table>
      `;
    }

    function renderCompanyTable() {
      const el = document.getElementById("company_table");
      const companies = state.company_interests || [];
      if (!el) return;
      if (!companies.length) {
        el.innerHTML = '<div class="small">No company interests tracked yet.</div>';
        return;
      }
      el.innerHTML = `
        <table>
          <thead>
            <tr>
              <th>Company</th>
              <th>Status</th>
              <th>Interest</th>
              <th>Tracked Jobs</th>
              <th>Next Step</th>
            </tr>
          </thead>
          <tbody>
            ${companies.map(company => `
              <tr class="${company.id === selectedCompanyId ? "active" : ""}" onclick="selectCompany(${company.id})">
                <td><b>${escapeHtml(company.company)}</b><div class="small">${escapeHtml(company.contacts || "")}</div></td>
                <td>${escapeHtml(company.status || "")}</td>
                <td><b class="${scoreClass(company.interest_score)}">${scoreText(company.interest_score)}</b></td>
                <td>${company.tracked_job_count || 0}</td>
                <td>${escapeHtml(company.next_step || "")}</td>
              </tr>
            `).join("")}
          </tbody>
        </table>
      `;
    }

    async function selectCompany(id, rerender = true) {
      selectedCompanyId = id;
      const { company } = await api(`/api/companies/${id}`);
      renderCompanyDetail(company);
      if (rerender) renderCompanyTable();
    }

    function renderCompanyDetail(company) {
      const detail = document.getElementById("company_detail");
      detail.innerHTML = `
        <div class="panel">
          <div class="toolbar">
            <div>
              <h2>${escapeHtml(company.company)}</h2>
              <div class="meta">${company.jobs.length} tracked role${company.jobs.length === 1 ? "" : "s"} for this company.</div>
            </div>
            <button onclick="saveCompanyInterest(${company.id})">Save company</button>
          </div>
          <div class="grid2">
            <div><label>Company</label><input id="edit_company_name" value="${escapeAttr(company.company)}"></div>
            <div><label>Status</label><select id="edit_company_status">${companyStatuses.map(s => `<option ${company.status === s ? "selected" : ""}>${s}</option>`).join("")}</select></div>
          </div>
          <label>Interest score</label><input id="edit_company_interest_score" type="number" min="0" max="100" value="${company.interest_score == null ? "" : company.interest_score}">
          <label>Rationale</label><textarea id="edit_company_rationale">${escapeHtml(company.rationale || "")}</textarea>
          <label>Contacts</label><textarea id="edit_company_contacts">${escapeHtml(company.contacts || "")}</textarea>
          <label>Next step</label><input id="edit_company_next_step" value="${escapeAttr(company.next_step || "")}">
          <label>Notes</label><textarea id="edit_company_notes">${escapeHtml(company.notes || "")}</textarea>
        </div>
        <div class="panel">
          <h2>Tracked Roles At ${escapeHtml(company.company)}</h2>
          ${company.jobs.length ? `
            <table>
              <thead><tr><th>Role</th><th>Status</th><th>Pipeline</th><th>Scores</th></tr></thead>
              <tbody>${company.jobs.map(job => `
                <tr onclick="showPage('jobs'); selectJob(${job.id})">
                  <td>${escapeHtml(job.title)}${job.url ? `<div class="small"><a href="${escapeAttr(job.url)}" target="_blank">posting</a></div>` : ""}</td>
                  <td>${escapeHtml(job.status || "")}${job.downlevel ? '<div class="small">downlevel</div>' : ""}${job.filtered ? '<div class="small">filtered</div>' : ""}</td>
                  <td>${escapeHtml(job.pipeline || "Unassigned")}</td>
                  <td>Codex ${scoreText(job.gpt_score)} · Mine ${scoreText(job.user_score)}</td>
                </tr>
              `).join("")}</tbody>
            </table>
          ` : '<div class="small">No tracked jobs for this company yet.</div>'}
        </div>
      `;
    }

    async function selectJob(id, rerender = true) {
      selectedId = id;
      const { job } = await api(`/api/jobs/${id}`);
      selectedJob = job;
      renderDetail(job);
      if (rerender) renderJobs();
    }

    function renderDetail(job) {
      const detail = document.getElementById("detail");
      const companyInterest = findCompanyInterestByName(job.company);
      const packet = applicationPacketForJob(job);
      const unassociatedPackets = (state.application_packets || []).filter(packet => packet.unassociated);
      const markdownFiles = packet ? packet.markdown_files || [] : [];
      detail.innerHTML = `
        <div class="panel">
          <div class="toolbar">
            <div class="grow">
              <h2>${escapeHtml(job.company)} - ${escapeHtml(job.title)}</h2>
              <div class="meta">${escapeHtml(job.location || "")} ${job.url ? `· <a href="${escapeAttr(job.url)}" target="_blank">posting</a>` : ""}</div>
            </div>
            <div>
              <label>Status</label>
              <select id="status">${statusOptions.map(s => `<option ${job.status === s ? "selected" : ""}>${s}</option>`).join("")}</select>
            </div>
            <div class="action-cluster">
              <div>
                <label>Action</label>
                <select id="job_action">
                  <option value="save_status">Save status</option>
                  <option value="track_company">${companyInterest ? "View company interest" : "Track company interest"}</option>
                  <option value="generate_packet" ${job.application_packet_path ? "disabled" : ""}>Generate packet with Codex</option>
                  <option value="rescrape" ${job.url ? "" : "disabled"}>Re-scrape posting</option>
                  <option value="delete">Delete job</option>
                </select>
              </div>
              <button onclick="runJobAction(${job.id})">Go</button>
            </div>
          </div>
          <div class="chips">
            <span class="chip">Pipeline: ${escapeHtml(job.pipeline || "Unassigned")}</span>
            <span class="chip">Codex: <b class="${scoreClass(job.gpt_score)}">${scoreText(job.gpt_score)}</b></span>
            <span class="chip">Mine: <b class="${scoreClass(job.user_score)}">${scoreText(job.user_score)}</b></span>
            <span class="chip">Level: ${escapeHtml(levelStatus(job))}</span>
            ${job.application_packet_path ? `<span class="chip">Packet: ${escapeHtml(job.application_packet_path)}</span>` : '<span class="chip">No packet</span>'}
            ${job.downlevel ? '<span class="chip">Downlevel</span>' : ""}
            ${job.filtered ? '<span class="chip">Filtered</span>' : ""}
          </div>
          <p>${escapeHtml(job.gpt_rationale || "No Codex rationale yet.")}</p>
          <button class="warn" onclick="scoreGpt(${job.id})" ${state.gpt_scoring_enabled ? "" : "disabled"}>${state.gpt_scoring_enabled ? "Populate Codex scorecard" : "Codex scoring disabled"}</button>
        </div>

        <div class="panel">
          <h2>Application Packet</h2>
          ${packet ? `
            <div class="meta">${escapeHtml(packet.path)}</div>
            <div class="packet-row">
              <div>
                <label>Markdown file</label>
                <select id="application_markdown_file">${markdownFiles.map(file => `<option>${escapeHtml(file)}</option>`).join("")}</select>
              </div>
              <button class="secondary" onclick="openApplicationMarkdown(${job.id})">Open rendered view</button>
            </div>
            <p class="small">Opens the selected Markdown file in a new rendered browser window.</p>
          ` : `
            <p class="small">No application packet is associated with this job.</p>
            <div class="row">
              <button class="secondary" onclick="generateApplicationPacket(${job.id})">Generate packet with Codex</button>
            </div>
            <div class="packet-row">
              <div>
                <label>Attach existing unassociated packet</label>
                <select id="application_packet_attach">
                  <option value="">Select packet...</option>
                  ${unassociatedPackets.map(packet => `<option value="${escapeAttr(packet.path)}">${escapeHtml(packet.name)}</option>`).join("")}
                </select>
              </div>
              <button class="secondary" onclick="attachApplicationPacket(${job.id})" ${unassociatedPackets.length ? "" : "disabled"}>Attach</button>
            </div>
          `}
        </div>

        <div class="panel">
          <h2>My Scorecard</h2>
          <div class="score-grid">${rubric.map(field => `
            <label>${pretty(field)}<input id="user_${field}" type="number" list="score_options" min="0" max="10" step="1" value="${fieldValue(job.user_scorecard, field, "")}"></label>
          `).join("")}</div>
          <label>Rationale</label><textarea id="user_rationale">${escapeHtml(job.user_rationale || "")}</textarea>
          <button onclick="saveUserScore(${job.id})">Save my score</button>
        </div>

        <div class="panel">
          <h2>Codex Scorecard</h2>
          <div class="score-grid">${rubric.map(field => `
            <div><span class="small">${pretty(field)}</span><br><b>${fieldValue(job.gpt_scorecard, field, "n/a")}</b></div>
          `).join("")}</div>
        </div>

        <div class="panel">
          <h2>Interactions</h2>
          <div class="grid2">
            <div><label>Date</label><input id="occurred_on" type="date"></div>
            <div><label>Channel</label><input id="channel" placeholder="intro, email, call, interview"></div>
          </div>
          <div class="grid2">
            <div><label>Person</label><input id="person_name"></div>
            <div><label>Role</label><input id="person_role"></div>
          </div>
          <label>What we talked about</label><textarea id="summary"></textarea>
          <label>Notes to self</label><textarea id="notes_to_self"></textarea>
          <label>Next step</label><input id="next_step">
          <button onclick="addInteraction(${job.id})">Add interaction</button>
          <div>${job.interactions.map(i => `
            <div class="interaction">
              <b>${escapeHtml(i.occurred_on)}</b> · ${escapeHtml(i.person_name || "Unknown")} ${i.person_role ? `(${escapeHtml(i.person_role)})` : ""} · ${escapeHtml(i.channel || "")}
              <p>${escapeHtml(i.summary || "")}</p>
              <p class="small">${escapeHtml(i.notes_to_self || "")}</p>
              <p class="small">Next: ${escapeHtml(i.next_step || "")}</p>
            </div>
          `).join("")}</div>
        </div>

        <div class="panel">
          <h2>Notes</h2>
          <textarea id="new_note" placeholder="Learning journal, concerns, outreach ideas, reminders."></textarea>
          <button onclick="addNote(${job.id})">Add note</button>
          <div>${job.notes_list.map(n => `<div class="note">${escapeHtml(n.note)}</div>`).join("")}</div>
        </div>

        <div class="panel">
          <h2>Recent Discoveries</h2>
          <div>${(state.discoveries || []).slice(0, 12).map(d => `
            <div class="interaction">
              <b>${escapeHtml(d.company || "Unknown")}</b> - ${escapeHtml(d.title || "Unknown")}
              <div class="meta">${escapeHtml(d.board)} · ${escapeHtml(d.location || "")} · ${d.url ? `<a href="${escapeAttr(d.url)}" target="_blank">posting</a>` : ""}</div>
              <div class="chips">
                <span class="chip">Decision: ${escapeHtml(d.decision)}</span>
                <span class="chip">Codex: <b class="${scoreClass(d.gpt_score)}">${scoreText(d.gpt_score)}</b></span>
                <span class="chip">Level: ${escapeHtml(levelStatus(d))}</span>
                ${d.downlevel ? '<span class="chip">Downlevel</span>' : ""}
              </div>
              <p class="small">${escapeHtml(d.rejection_reason || d.gpt_rationale || "")}</p>
            </div>
          `).join("") || '<div class="small">No discoveries yet.</div>'}</div>
        </div>
      `;
    }

    function applicationPacketForJob(job) {
      if (!job.application_packet_path) return null;
      return (state.application_packets || []).find(packet => packet.path === job.application_packet_path) || {
        path: job.application_packet_path,
        name: job.application_packet_path.split("/").pop(),
        markdown_files: ["Job-Brief.md", "Resume.md", "Cover-Letter.md"],
      };
    }

    async function runJobAction(id) {
      const action = document.getElementById("job_action").value;
      if (action === "save_status") return saveStatus(id);
      if (action === "track_company") return trackCompanyFromSelectedJob();
      if (action === "generate_packet") return generateApplicationPacket(id);
      if (action === "rescrape") return rescrapeJob(id);
      if (action === "delete") return deleteJob(id);
    }

    async function generateApplicationPacket(id) {
      try {
        const result = await api(`/api/jobs/${id}/application-packet/generate`, {
          method: "POST",
          body: "{}",
          activityLabel: "Generating application packet",
        });
        const job = result.job;
        if (result.packet && result.packet.warning) alert(result.packet.warning);
        selectedId = job.id;
        selectedJob = job;
        await load();
      } catch (err) {
        alert(err.message);
      }
    }

    async function attachApplicationPacket(id) {
      const selector = document.getElementById("application_packet_attach");
      const path = selector ? selector.value : "";
      if (!path) return;
      try {
        const { job } = await api(`/api/jobs/${id}/application-packet/attach`, {
          method: "POST",
          body: JSON.stringify({ path }),
          activityLabel: "Attaching application packet",
        });
        selectedId = job.id;
        selectedJob = job;
        await load();
      } catch (err) {
        alert(err.message);
      }
    }

    function openApplicationMarkdown(id) {
      const selector = document.getElementById("application_markdown_file");
      if (!selector || !selector.value) return;
      window.open(`/api/jobs/${id}/application-packet/render?file=${encodeURIComponent(selector.value)}`, "_blank", "noopener");
    }

    async function createJob() {
      const payload = {
        url: document.getElementById("url").value,
        pipeline: document.getElementById("pipeline").value,
        force_refresh: document.getElementById("manual_force_refresh").checked,
      };
      const { job, score_error: scoreError } = await api("/api/jobs", { method: "POST", body: JSON.stringify(payload), activityLabel: "Adding and scoring job" });
      selectedId = job.id;
      document.getElementById("url").value = "";
      document.getElementById("manual_force_refresh").checked = false;
      await load();
      if (scoreError) alert(scoreError);
    }

    async function createCompanyInterest() {
      const payload = {
        company: document.getElementById("company_interest_name").value,
        status: document.getElementById("company_interest_status").value,
        interest_score: nullableNumber(document.getElementById("company_interest_score").value),
        rationale: document.getElementById("company_interest_rationale").value,
        contacts: document.getElementById("company_interest_contacts").value,
        next_step: document.getElementById("company_interest_next_step").value,
        notes: document.getElementById("company_interest_notes").value,
      };
      const { company } = await api("/api/companies", { method: "POST", body: JSON.stringify(payload) });
      selectedCompanyId = company.id;
      ["company_interest_name","company_interest_score","company_interest_rationale","company_interest_contacts","company_interest_next_step","company_interest_notes"].forEach(id => document.getElementById(id).value = "");
      await load();
    }

    async function trackCompanyFromSelectedJob() {
      if (!selectedJob) return;
      await trackCompanyFromJob(selectedJob.company, selectedJob.title);
    }

    async function trackCompanyFromJob(companyName, title) {
      const existing = findCompanyInterestByName(companyName);
      if (existing) {
        selectedCompanyId = existing.id;
        showPage("companies");
        await selectCompany(existing.id);
        return;
      }
      const payload = {
        company: companyName,
        status: "watching",
        interest_score: null,
        rationale: `Interested via tracked role: ${title}`,
        contacts: "",
        next_step: "",
        notes: "",
      };
      const { company } = await api("/api/companies", { method: "POST", body: JSON.stringify(payload) });
      selectedCompanyId = company.id;
      await load();
      showPage("companies");
      await selectCompany(company.id);
    }

    async function saveCompanyInterest(id) {
      const payload = {
        company: document.getElementById("edit_company_name").value,
        status: document.getElementById("edit_company_status").value,
        interest_score: nullableNumber(document.getElementById("edit_company_interest_score").value),
        rationale: document.getElementById("edit_company_rationale").value,
        contacts: document.getElementById("edit_company_contacts").value,
        next_step: document.getElementById("edit_company_next_step").value,
        notes: document.getElementById("edit_company_notes").value,
      };
      await api(`/api/companies/${id}`, { method: "POST", body: JSON.stringify(payload) });
      await load();
    }

    async function scoreGpt(id) {
      try {
        await api(`/api/jobs/${id}/score-gpt`, { method: "POST", body: "{}" });
        await load();
      } catch (err) {
        alert(err.message);
      }
    }

    async function startBulkScorecards() {
      const ids = selectedJobIds();
      if (!ids.length) return alert("Select at least one job first.");
      try {
        const { task } = await api("/api/jobs/bulk/score-gpt", {
          method: "POST",
          body: JSON.stringify({ job_ids: ids }),
          activityLabel: "Starting bulk Codex scoring",
        });
        startBulkTaskPolling(task.id);
      } catch (err) {
        alert(err.message);
      }
    }

    async function startBulkApplicationPackets() {
      const ids = selectedJobIds();
      if (!ids.length) return alert("Select at least one job first.");
      try {
        const { task } = await api("/api/jobs/bulk/application-packets/generate", {
          method: "POST",
          body: JSON.stringify({ job_ids: ids }),
          activityLabel: "Starting bulk packet generation",
        });
        startBulkTaskPolling(task.id);
      } catch (err) {
        alert(err.message);
      }
    }

    async function rescrapeJob(id) {
      try {
        const { job } = await api(`/api/jobs/${id}/scrape`, {
          method: "POST",
          body: JSON.stringify({ force_refresh: true }),
          activityLabel: "Re-scraping job",
        });
        selectedId = job.id;
        selectedJob = job;
        await load();
      } catch (err) {
        alert(err.message);
      }
    }

    async function deleteJob(id) {
      const confirmText = prompt("Type DELETE to remove this tracked job and its CRM notes/interactions.");
      if (confirmText !== "DELETE") return;
      await api(`/api/jobs/${id}`, {
        method: "DELETE",
        body: JSON.stringify({ confirm: confirmText }),
        activityLabel: "Deleting job",
      });
      selectedId = null;
      selectedJob = null;
      document.getElementById("detail").innerHTML = '<div class="empty">Select a job from the table.</div>';
      await load();
    }

    async function saveUserScore(id) {
      const scorecard = {};
      rubric.forEach(field => scorecard[field] = normalizedScore(document.getElementById(`user_${field}`).value));
      await api(`/api/jobs/${id}/score-user`, {
        method: "POST",
        body: JSON.stringify({ scorecard, user_rationale: document.getElementById("user_rationale").value })
      });
      await load();
    }

    function normalizedScore(value) {
      const parsed = Number(value);
      if (!Number.isFinite(parsed)) return 0;
      return Math.max(0, Math.min(10, Math.round(parsed)));
    }

    function nullableNumber(value) {
      const parsed = Number(value);
      if (!Number.isFinite(parsed)) return null;
      return parsed;
    }

    async function addInteraction(id) {
      const payload = {};
      ["occurred_on","person_name","person_role","channel","summary","notes_to_self","next_step"].forEach(k => payload[k] = document.getElementById(k).value);
      await api(`/api/jobs/${id}/interactions`, { method: "POST", body: JSON.stringify(payload) });
      await load();
    }

    async function addNote(id) {
      await api(`/api/jobs/${id}/notes`, { method: "POST", body: JSON.stringify({ note: document.getElementById("new_note").value }) });
      await load();
    }

    async function saveStatus(id) {
      await api(`/api/jobs/${id}/status`, { method: "POST", body: JSON.stringify({ status: document.getElementById("status").value }) });
      await load();
    }

    async function saveSettings() {
      await api("/api/settings", {
        method: "POST",
        body: JSON.stringify({
          gpt_threshold: document.getElementById("gpt_threshold").value,
          user_threshold: document.getElementById("user_threshold").value,
          codex_model: document.getElementById("codex_model").value,
        })
      });
      await load();
    }

    async function saveConfig() {
      const payload = {};
      ["CODEX_CLI_PATH","CODEX_MODEL","JOB_SEARCH_ENABLE_GPT_SCORING","JOB_SEARCH_USE_CAPTURE_CACHE"].forEach(key => {
        const value = document.getElementById(`config_${key}`).value.trim();
        if (value || key === "CODEX_MODEL") payload[key] = value;
      });
      await api("/api/config", { method: "POST", body: JSON.stringify(payload) });
      await load();
    }

    async function purgeTrackedJobs() {
      const confirmText = prompt("Type PURGE to delete all tracked jobs and their CRM notes/interactions.");
      if (confirmText !== "PURGE") return;
      await api("/api/admin/purge-jobs", {
        method: "POST",
        body: JSON.stringify({ confirm: confirmText })
      });
      selectedId = null;
      selectedJob = null;
      document.getElementById("detail").innerHTML = '<div class="empty">Select a job from the table.</div>';
      await load();
    }

    async function runSearch() {
      searchRunning = true;
      renderSearchState();
      try {
        await api("/api/search/run", {
          method: "POST",
          body: JSON.stringify({ force_refresh: document.getElementById("force_refresh").checked })
        });
        searchRunning = false;
        await load();
      } catch (err) {
        searchRunning = false;
        renderSearchState();
        alert(err.message);
      }
    }

    async function addSearchQuery() {
      await api("/api/search/queries", {
        method: "POST",
        body: JSON.stringify({
          board: document.getElementById("search_board").value,
          pipeline: document.getElementById("search_pipeline").value,
          keywords: document.getElementById("search_keywords").value,
          location: document.getElementById("search_location").value,
          criteria: document.getElementById("search_criteria").value,
        })
      });
      document.getElementById("search_keywords").value = "";
      document.getElementById("search_criteria").value = "";
      await load();
    }

    async function toggleQuery(id, enabled) {
      await api(`/api/search/queries/${id}`, {
        method: "POST",
        body: JSON.stringify({ enabled })
      });
      await load();
    }

    function showPage(page) {
      currentPage = page;
      setPageVisible("jobs", page === "jobs");
      setPageVisible("companies", page === "companies");
      setPageVisible("queries", page === "queries");
      document.getElementById("jobs_nav").classList.toggle("secondary", page !== "jobs");
      document.getElementById("companies_nav").classList.toggle("secondary", page !== "companies");
      document.getElementById("queries_nav").classList.toggle("secondary", page !== "queries");
      if (page === "jobs") requestAnimationFrame(() => {
        syncJobsPageHeight();
        initializeJobSplit();
      });
    }

    function setPageVisible(pageName, visible) {
      const element = document.getElementById(`${pageName}_page`);
      if (!element) return;
      element.classList.toggle("hidden", !visible);
      element.style.display = visible ? "" : "none";
    }

    function syncJobsPageHeight() {
      if (window.matchMedia("(max-width: 980px)").matches) return;
      const sidebar = document.querySelector("main > aside");
      const page = document.getElementById("jobs_page");
      if (!sidebar || !page || page.classList.contains("hidden")) return;
      const sidebarHeight = Math.ceil(sidebar.getBoundingClientRect().height);
      if (sidebarHeight > 0) {
        page.style.setProperty("--jobs-page-height", `${sidebarHeight}px`);
      }
    }

    function initializeJobSplit() {
      if (splitInitialized || window.matchMedia("(max-width: 980px)").matches) return;
      syncJobsPageHeight();
      const page = document.getElementById("jobs_page");
      const detail = document.getElementById("detail");
      const divider = document.getElementById("jobs_split_divider");
      if (!page || !detail || !divider || page.classList.contains("hidden")) return;
      const height = page.getBoundingClientRect().height;
      if (height > 0) {
        detail.style.setProperty("--detail-height", `${Math.round(height * 0.5)}px`);
        splitInitialized = true;
      }
    }

    function configureJobSplitDrag() {
      const page = document.getElementById("jobs_page");
      const detail = document.getElementById("detail");
      const divider = document.getElementById("jobs_split_divider");
      if (!page || !detail || !divider) return;
      const resize = event => {
        const rect = page.getBoundingClientRect();
        const minPane = 180;
        const dividerHeight = divider.getBoundingClientRect().height || 12;
        const rawDetailHeight = rect.bottom - event.clientY - dividerHeight / 2;
        const maxDetailHeight = Math.max(minPane, rect.height - minPane - dividerHeight);
        const nextHeight = Math.max(minPane, Math.min(maxDetailHeight, rawDetailHeight));
        detail.style.setProperty("--detail-height", `${Math.round(nextHeight)}px`);
        splitInitialized = true;
      };
      divider.addEventListener("pointerdown", event => {
        if (window.matchMedia("(max-width: 980px)").matches) return;
        event.preventDefault();
        divider.classList.add("dragging");
        divider.setPointerCapture(event.pointerId);
        resize(event);
      });
      divider.addEventListener("pointermove", event => {
        if (!divider.classList.contains("dragging")) return;
        resize(event);
      });
      const stop = event => {
        if (!divider.classList.contains("dragging")) return;
        divider.classList.remove("dragging");
        if (divider.hasPointerCapture(event.pointerId)) divider.releasePointerCapture(event.pointerId);
      };
      divider.addEventListener("pointerup", stop);
      divider.addEventListener("pointercancel", stop);
      window.addEventListener("resize", () => {
        splitInitialized = false;
        syncJobsPageHeight();
        initializeJobSplit();
      });
      const sidebar = document.querySelector("main > aside");
      if (sidebar && "ResizeObserver" in window) {
        new ResizeObserver(() => {
          syncJobsPageHeight();
          if (!splitInitialized) initializeJobSplit();
        }).observe(sidebar);
      }
    }

    function escapeHtml(s) {
      return String(s == null ? "" : s).replace(/[&<>"']/g, c => ({ "&":"&amp;", "<":"&lt;", ">":"&gt;", '"':"&quot;", "'":"&#39;" }[c]));
    }
    function escapeAttr(s) { return escapeHtml(s).replace(/`/g, "&#96;"); }
    configureJobSplitDrag();
    load().then(() => {
      if (activeBulkTaskId) startBulkTaskPolling(activeBulkTaskId);
    });
  </script>
</body>
</html>
"""


def main():
    init_db()
    start_scheduler()
    print(f"Job Search Console running at http://{HOST}:{PORT}")
    print(f"Database: {DB_PATH}")
    app.run(host=HOST, port=PORT, debug=DEBUG, use_reloader=False)


if __name__ == "__main__":
    main()
