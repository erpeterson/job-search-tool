#!/usr/bin/env python3
import json
import hashlib
import logging
import os
import re
import sqlite3
import threading
import textwrap
import time
from datetime import datetime, timezone
from logging.handlers import RotatingFileHandler
from pathlib import Path
from urllib.parse import quote_plus, urljoin

import requests
from bs4 import BeautifulSoup
from dotenv import load_dotenv
from flask import Flask, jsonify, request
from openai import OpenAI
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

load_dotenv(ENV_PATH)

DEFAULT_MODEL = os.environ.get("OPENAI_MODEL", "gpt-5")
HOST = os.environ.get("JOB_SEARCH_HOST", "127.0.0.1")
PORT = int(os.environ.get("JOB_SEARCH_PORT", "5050"))
DEBUG = os.environ.get("JOB_SEARCH_DEBUG", "0") == "1"
AUTORUN = os.environ.get("JOB_SEARCH_AUTORUN", "1") != "0"
SEARCH_INTERVAL_SECONDS = int(os.environ.get("JOB_SEARCH_INTERVAL_SECONDS", str(24 * 60 * 60)))
LOG_MAX_BYTES = int(os.environ.get("JOB_SEARCH_LOG_MAX_BYTES", str(1024 * 1024)))
LOG_BACKUP_COUNT = int(os.environ.get("JOB_SEARCH_LOG_BACKUP_COUNT", "5"))
CONFIG_KEYS = [
    "OPENAI_API_KEY",
    "OPENAI_MODEL",
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

LEVELS_FYI_ORACLE_IC6_URL = "https://www.levels.fyi/companies/oracle/salaries/software-engineer/levels/ic-6"
ORACLE_IC6_LEVEL_REFERENCE = (
    "Use Levels.fyi as canonical source for Oracle level equivalence. "
    "Oracle Software Engineer IC-6 is Architect. IC-5 is Consulting MTS; IC-7 is Distinguished Engineer. "
    "Treat IC6-equivalent as Architect / Principal-plus / Staff-plus scope with broad technical influence, "
    "cross-team architecture, durable technical direction, or organization-level engineering judgment."
)
MIN_ANNUAL_COMPENSATION = 200_000


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
                downlevel INTEGER NOT NULL DEFAULT 0
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

            """
        )
        ensure_column(conn, "jobs", "source_board", "TEXT")
        ensure_column(conn, "jobs", "source_job_id", "TEXT")
        ensure_column(conn, "jobs", "discovered_at", "INTEGER")
        ensure_column(conn, "jobs", "level_assessment", "TEXT")
        ensure_column(conn, "jobs", "downlevel", "INTEGER NOT NULL DEFAULT 0")
        ensure_column(conn, "search_queries", "pipeline", "TEXT")
        ensure_column(conn, "search_queries", "criteria", "TEXT")
        ensure_column(conn, "search_queries", "refinement_notes", "TEXT")
        ensure_column(conn, "search_queries", "seeded", "INTEGER NOT NULL DEFAULT 0")
        ensure_column(conn, "discovered_jobs", "query_id", "INTEGER")
        conn.execute("CREATE UNIQUE INDEX IF NOT EXISTS idx_jobs_url ON jobs(url) WHERE url IS NOT NULL AND url != ''")
        defaults = {
            "gpt_threshold": "40",
            "user_threshold": "60",
            "model": DEFAULT_MODEL,
            "last_search_at": "0",
        }
        for key, value in defaults.items():
            conn.execute(
                "INSERT OR IGNORE INTO settings(key, value) VALUES (?, ?)",
                (key, value),
            )
        seed_search_queries(conn)
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
        return fallback


def settings(conn):
    return {row["key"]: row["value"] for row in conn.execute("SELECT key, value FROM settings")}


def gpt_scoring_enabled():
    return os.environ.get("JOB_SEARCH_ENABLE_GPT_SCORING", "0") == "1"


def capture_cache_enabled():
    return os.environ.get("JOB_SEARCH_USE_CAPTURE_CACHE", "1") != "0"


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


def apply_filter(conn, job_id):
    cfg = settings(conn)
    gpt_threshold = int(cfg.get("gpt_threshold", "40"))
    user_threshold = int(cfg.get("user_threshold", "60"))
    use_gpt_threshold = gpt_scoring_enabled()
    job = conn.execute("SELECT company, title, gpt_score, user_score FROM jobs WHERE id = ?", (job_id,)).fetchone()
    filtered = 0
    reasons = []
    if job:
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


def score_discovery_with_openai(conn, discovery, force_refresh=False):
    job = {
        "company": discovery.get("company"),
        "title": discovery.get("title"),
        "url": discovery.get("url"),
        "location": discovery.get("location"),
        "pipeline": discovery.get("pipeline") or "",
        "posting_text": discovery.get("snippet"),
        "notes": f"Source board: {discovery.get('board')}. Search criteria: {discovery.get('criteria', '')}",
    }
    return score_with_openai(conn, job, force_refresh=force_refresh)


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
            refine_search_query(conn, query["id"], force_refresh=force_refresh)

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
        log_event("query_refinement_skipped", query_id=query_id, reason="GPT scoring disabled")
        return
    if not os.environ.get("OPENAI_API_KEY"):
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
    cfg = settings(conn)
    client = OpenAI(api_key=os.environ["OPENAI_API_KEY"])
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
            "canonical_source": "Levels.fyi",
            "oracle_ic6_url": LEVELS_FYI_ORACLE_IC6_URL,
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
    output_text = call_openai_json(client, cfg.get("model") or DEFAULT_MODEL, prompt, "refine_search_query", force_refresh=force_refresh)
    if not output_text:
        return
    try:
        refined = json.loads(output_text)
    except json.JSONDecodeError:
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
        reason = "GPT scoring is disabled; discovery tracked without GPT score."
        log_event(
            "discovery_gpt_disabled",
            board=result.get("board"),
            company=result.get("company"),
            title=result.get("title"),
            url=result.get("url"),
            reason=reason,
        )
        job_id = track_discovery_without_gpt(conn, result, reason)
        return "tracked", reason, job_id, None, {}, "", False
    if not os.environ.get("OPENAI_API_KEY"):
        reason = "OPENAI_API_KEY is unavailable; discovery tracked without GPT score."
        log_event(
            "discovery_openai_key_unavailable",
            board=result.get("board"),
            company=result.get("company"),
            title=result.get("title"),
            url=result.get("url"),
            reason=reason,
        )
        job_id = track_discovery_without_gpt(conn, result, reason)
        return "tracked", reason, job_id, None, {}, "", False

    score = score_discovery_with_openai(conn, result, force_refresh=force_refresh)
    scorecard = score.get("scorecard", {})
    total = int(score.get("total_score", 0))
    downlevel = bool(score.get("downlevel", False))
    level_assessment = score.get("level_assessment", "")
    pipeline = score.get("pipeline", "")
    if not pipeline:
        pipeline = result.get("pipeline", "")

    if downlevel and total < 80:
        log_event(
            "discovery_rejected",
            reason="Downlevel relative to IC6-equivalent and GPT score is below 80.",
            company=result.get("company"),
            title=result.get("title"),
            url=result.get("url"),
            gpt_score=total,
            level_assessment=level_assessment,
            downlevel=downlevel,
        )
        return "rejected", "Downlevel relative to IC6-equivalent and GPT score is below 80.", None, score, scorecard, level_assessment, downlevel

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
    cur = conn.execute(
        """
        INSERT INTO jobs(
            created_at, updated_at, company, title, url, location, pipeline, status, posting_text, notes,
            filtered, source_board, source_job_id, discovered_at, level_assessment, downlevel
        )
        VALUES (?, ?, ?, ?, ?, ?, ?, 'discovered', ?, ?, 0, ?, ?, ?, '', 0)
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
        ),
    )
    job_id = cur.lastrowid
    apply_filter(conn, job_id)
    return job_id


def score_with_openai(conn, job, force_refresh=False):
    if not gpt_scoring_enabled():
        raise RuntimeError("GPT scoring is currently disabled. Set JOB_SEARCH_ENABLE_GPT_SCORING=1 to re-enable it.")
    api_key = os.environ.get("OPENAI_API_KEY")
    if not api_key:
        raise RuntimeError("OPENAI_API_KEY is not set. Add the job manually or export OPENAI_API_KEY before scoring.")

    cfg = settings(conn)
    model = cfg.get("model") or DEFAULT_MODEL
    client = OpenAI(api_key=api_key)
    prompt = {
        "task": "Score this job for Eric Peterson's job search.",
        "level_reference": {
            "canonical_source": "Levels.fyi",
            "oracle_ic6_url": LEVELS_FYI_ORACLE_IC6_URL,
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
            "Classify whether this role appears Oracle IC6-equivalent or higher using the Levels.fyi reference: Oracle IC-6 is Architect.",
            "Treat Principal Engineer, Architect, Senior Principal Engineer, Distinguished Engineer, Fellow, Chief Architect, CTO advisor, and equivalent strategic IC roles as potentially IC6-equivalent or higher depending on scope.",
            "Treat ordinary software engineer, senior engineer, staff engineer with narrow feature ownership, line-management-heavy manager roles, and single-service owner roles as downlevel unless the posting clearly indicates Architect-equivalent broad cross-org technical influence.",
            "Do not invent facts missing from the posting.",
        ],
        "expected_json_schema": {
            "total_score": "integer 0-100",
            "pipeline": PIPELINES,
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
    output_text = call_openai_json(client, model, prompt, "score_job", force_refresh=force_refresh)
    if not output_text:
        output_text = ""
    if not output_text:
        raise RuntimeError("OpenAI response did not include text output.")
    try:
        parsed = json.loads(output_text)
    except json.JSONDecodeError as exc:
        raise RuntimeError(f"OpenAI response was not valid JSON: {output_text[:1000]}") from exc
    return parsed


def call_openai_json(client, model, prompt, operation, force_refresh=False):
    request_payload = {
        "model": model,
        "input": prompt,
        "text": {"format": {"type": "json_object"}},
    }
    cached = read_capture("openai", operation, request_payload, force_refresh=force_refresh)
    if cached:
        return cached["response"].get("output_text", "")

    started = time.monotonic()
    response = None
    error = None
    try:
        response = client.responses.create(
            model=model,
            input=json.dumps(prompt),
            text={"format": {"type": "json_object"}},
        )
        return response.output_text or ""
    except Exception as exc:
        error = exc
        raise
    finally:
        elapsed_ms = int((time.monotonic() - started) * 1000)
        response_payload = {
            "output_text": getattr(response, "output_text", "") if response is not None else "",
            "raw": response.model_dump(mode="json") if response is not None and hasattr(response, "model_dump") else None,
            "error_type": type(error).__name__ if error else None,
            "error_message": str(error) if error else None,
        }
        write_capture("openai", operation, request_payload, response_payload, {"elapsed_ms": elapsed_ms})
        log_event(
            "openai_call",
            operation=operation,
            model=model,
            ok=error is None,
            elapsed_ms=elapsed_ms,
            error_type=type(error).__name__ if error else None,
            message=str(error)[:1000] if error else None,
        )


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
                "search_queries": list_search_queries(conn),
                "search_runs": list_search_runs(conn),
                "discoveries": list_discoveries(conn),
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


@app.post("/api/jobs")
def api_create_job():
    payload = request.get_json(silent=True) or {}
    ts = now()
    with connect() as conn:
        cur = conn.execute(
            """
            INSERT INTO jobs(created_at, updated_at, company, title, url, location, pipeline, status, posting_text, notes)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                ts,
                ts,
                payload.get("company", "").strip() or "Unknown company",
                payload.get("title", "").strip() or "Unknown title",
                payload.get("url", "").strip(),
                payload.get("location", "").strip(),
                payload.get("pipeline", "").strip(),
                payload.get("status", "researching"),
                payload.get("posting_text", "").strip(),
                payload.get("notes", "").strip(),
            ),
        )
        job_id = cur.lastrowid
        apply_filter(conn, job_id)
        return jsonify({"job": get_job(conn, job_id)}), 201


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
        value = str(payload.get(key, "")).strip()
        if value:
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
        if "OPENAI_MODEL" in updates:
            conn.execute(
                "INSERT INTO settings(key, value) VALUES ('model', ?) ON CONFLICT(key) DO UPDATE SET value = excluded.value",
                (updates["OPENAI_MODEL"],),
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
            return jsonify({"error": "GPT scoring is currently disabled. Set JOB_SEARCH_ENABLE_GPT_SCORING=1 to re-enable it."}), 409
        score = score_with_openai(conn, job)
        total = int(score.get("total_score", 0))
        scorecard = score.get("scorecard", {})
        downlevel = bool(score.get("downlevel", False))
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
                score.get("pipeline", ""),
                score.get("level_assessment", ""),
                1 if downlevel else 0,
                now(),
                job_id,
            ),
        )
        apply_filter(conn, job_id)
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
        for key in ("gpt_threshold", "user_threshold", "model"):
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
      padding: 22px 28px 12px;
      border-bottom: 1px solid var(--line);
      background: rgba(251,252,250,.82);
      position: sticky;
      top: 0;
      z-index: 5;
      backdrop-filter: blur(14px);
    }
    h1 { margin: 0 0 6px; font-size: 28px; }
    .subtitle { color: var(--muted); max-width: 950px; }
    nav { display: flex; gap: 8px; margin-top: 14px; }
    nav button { width: auto; padding: 8px 12px; }
    main {
      display: grid;
      grid-template-columns: 300px 1fr;
      gap: 18px;
      padding: 18px;
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
    .toolbar { display: flex; gap: 10px; align-items: end; margin-bottom: 12px; }
    .toolbar label { margin-top: 0; }
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
    @media (max-width: 980px) {
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
    <h1>Job Search Console</h1>
    <div class="subtitle">Score opportunities against the ideal problem set, track applications like a CRM, and preserve learning from every conversation.</div>
    <nav>
      <button id="jobs_nav" onclick="showPage('jobs')">Jobs</button>
      <button id="queries_nav" class="secondary" onclick="showPage('queries')">Queries</button>
    </nav>
  </header>
  <main>
    <aside>
      <section>
        <h2>Filters</h2>
        <div class="grid2">
          <div><label>GPT threshold</label><input id="gpt_threshold" type="number" min="0" max="100"></div>
          <div><label>User threshold</label><input id="user_threshold" type="number" min="0" max="100"></div>
        </div>
        <label>Model</label><input id="model">
        <div class="row" style="margin-top: 10px;">
          <button onclick="saveSettings()">Save</button>
          <button class="secondary" onclick="toggleFiltered()">Show/Hide filtered</button>
        </div>
        <p class="small">Jobs are filtered when user score is below threshold. GPT threshold applies only when GPT scoring is enabled.</p>
      </section>
      <section class="sidebar-block">
        <h2>Search</h2>
        <button id="run_search_button" class="warn" onclick="runSearch()">Run job search now</button>
        <label class="checkbox-row"><input id="force_refresh" type="checkbox"> Force refresh, bypass replay cache</label>
        <div id="search_status" class="status-pill">Idle</div>
        <p class="small">Daily search runs use saved LinkedIn and Indeed queries. Downlevel jobs are rejected unless GPT score is 80+.</p>
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
          <summary>API keys and model</summary>
          <label>OpenAI API key</label><input id="config_OPENAI_API_KEY" type="password" placeholder="Leave blank to keep existing key">
          <label>OpenAI model</label><input id="config_OPENAI_MODEL" placeholder="gpt-5">
          <label>Enable GPT scoring</label><select id="config_JOB_SEARCH_ENABLE_GPT_SCORING"><option value="0">disabled</option><option value="1">enabled</option></select>
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
          <summary>Add job manually</summary>
          <label>Company</label><input id="company">
          <label>Title</label><input id="title">
          <label>URL</label><input id="url">
          <label>Location</label><input id="location">
          <label>Pipeline</label><select id="pipeline"></select>
          <label>Posting Text</label><textarea id="posting_text" placeholder="Paste the job description here for GPT scoring."></textarea>
          <label>Initial Notes</label><textarea id="notes" placeholder="Why this is interesting, concerns, people to contact."></textarea>
          <button onclick="createJob()">Add job</button>
        </details>
      </section>
    </aside>
    <div>
      <section id="jobs_page" class="page">
        <div class="panel jobs-master-panel">
          <div class="toolbar">
            <div>
              <h2>Tracked Jobs</h2>
              <div class="small">Master list of tracked jobs. Select a row to edit CRM details below.</div>
            </div>
            <div>
              <label>Pipeline view</label>
              <select id="pipeline_view_filter" onchange="renderJobs()"></select>
            </div>
            <button class="secondary" onclick="toggleFiltered()">Show/Hide filtered</button>
          </div>
          <div id="jobs" class="table-wrap"></div>
        </div>
        <div id="jobs_split_divider" class="split-divider" title="Drag to resize job detail pane"></div>
        <div id="detail" class="detail">
          <div class="empty">Select a job from the table.</div>
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
    let state = { jobs: [], settings: {}, pipelines: [], rubric_fields: rubric };
    let selectedId = null;
    let includeFiltered = false;
    let currentPage = "jobs";
    let searchRunning = false;
    let splitInitialized = false;

    const pretty = s => s.replaceAll("_", " ").replace(/\b\w/g, c => c.toUpperCase());
    const scoreClass = n => n == null ? "" : n >= 70 ? "score-good" : n >= 40 ? "score-warn" : "score-bad";

    async function api(path, options = {}) {
      const response = await fetch(path, {
        headers: { "Content-Type": "application/json" },
        ...options,
      });
      const data = await response.json();
      if (!response.ok) throw new Error(data.error || "Request failed");
      return data;
    }

    async function load() {
      state = await api(`/api/state?include_filtered=${includeFiltered ? "1" : "0"}`);
      document.getElementById("gpt_threshold").value = state.settings.gpt_threshold;
      document.getElementById("user_threshold").value = state.settings.user_threshold;
      document.getElementById("model").value = state.settings.model;
      const pipeline = document.getElementById("pipeline");
      pipeline.innerHTML = '<option value=""></option>' + state.pipelines.map(p => `<option>${p}</option>`).join("");
      document.getElementById("search_pipeline").innerHTML = state.pipelines.map(p => `<option>${p}</option>`).join("");
      const pipelineView = document.getElementById("pipeline_view_filter");
      const currentPipelineView = pipelineView.value || "";
      pipelineView.innerHTML = '<option value="">All pipelines</option>' + state.pipelines.map(p => `<option>${p}</option>`).join("");
      pipelineView.value = state.pipelines.includes(currentPipelineView) ? currentPipelineView : "";
      renderSearchState();
      renderConfigStatus();
      renderJobs();
      renderQueryTable();
      initializeJobSplit();
      if (selectedId) await selectJob(selectedId, false);
    }

    function renderSearchState() {
      const enabled = (state.search_queries || []).filter(q => q.enabled);
      const latest = (state.search_runs || [])[0];
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
      document.getElementById("enabled_queries").innerHTML = enabled.map(q => `
        <div class="enabled-query">
          <b>${escapeHtml(q.pipeline || "Custom")}</b>
          <br>${escapeHtml(q.board)} · ${escapeHtml(q.location || "")}
          <br>${escapeHtml(q.keywords)}
        </div>
      `).join("") || "No enabled queries.";
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
        ${["OPENAI_API_KEY","OPENAI_MODEL"].map(keyLine).join("<br>")}
        <br>API log: ${escapeHtml(state.api_log_path || "")}
        <br>Decision log: ${escapeHtml(state.event_log_path || "")}
        <br>Captures: ${escapeHtml(state.capture_dir || "")}
        <br>GPT scoring: ${state.gpt_scoring_enabled ? "enabled" : "disabled"}
        <br>Replay cache: ${state.capture_cache_enabled ? "enabled" : "disabled"}
      `;
      if (!document.getElementById("config_OPENAI_MODEL").value) {
        document.getElementById("config_OPENAI_MODEL").value = state.settings.model || "";
      }
      document.getElementById("config_JOB_SEARCH_ENABLE_GPT_SCORING").value = state.gpt_scoring_enabled ? "1" : "0";
      document.getElementById("config_JOB_SEARCH_USE_CAPTURE_CACHE").value = state.capture_cache_enabled ? "1" : "0";
    }

    function renderJobs() {
      const jobs = document.getElementById("jobs");
      const pipelineFilter = document.getElementById("pipeline_view_filter")?.value || "";
      const visibleJobs = pipelineFilter ? state.jobs.filter(job => (job.pipeline || "") === pipelineFilter) : state.jobs;
      if (!visibleJobs.length) {
        jobs.innerHTML = '<div class="small">No jobs match the current filter.</div>';
        return;
      }
      jobs.innerHTML = `
        <table>
          <thead>
            <tr>
              <th>Company</th>
              <th>Role</th>
              <th>Pipeline</th>
              <th>Status</th>
              <th>GPT</th>
              <th>Mine</th>
              <th>Level</th>
              <th>Source</th>
            </tr>
          </thead>
          <tbody>
            ${visibleJobs.map(job => `
              <tr class="${job.filtered ? "filtered" : ""} ${job.id === selectedId ? "active" : ""}" onclick="selectJob(${job.id})">
                <td><b>${escapeHtml(job.company)}</b><div class="small">${escapeHtml(job.location || "")}</div></td>
                <td><span class="job-title">${escapeHtml(job.title)}</span>${job.url ? `<div class="small"><a href="${escapeAttr(job.url)}" target="_blank">posting</a></div>` : ""}</td>
                <td>${escapeHtml(job.pipeline || "Unassigned")}</td>
                <td>${escapeHtml(job.status || "")}${job.filtered ? '<div class="small">filtered</div>' : ""}</td>
                <td><b class="${scoreClass(job.gpt_score)}">${job.gpt_score ?? "n/a"}</b></td>
                <td><b class="${scoreClass(job.user_score)}">${job.user_score ?? "n/a"}</b></td>
                <td>${escapeHtml(job.level_assessment || "n/a")}${job.downlevel ? '<div class="small">downlevel</div>' : ""}</td>
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

    async function selectJob(id, rerender = true) {
      selectedId = id;
      const { job } = await api(`/api/jobs/${id}`);
      renderDetail(job);
      if (rerender) renderJobs();
    }

    function renderDetail(job) {
      const detail = document.getElementById("detail");
      detail.innerHTML = `
        <div class="panel">
          <div class="toolbar">
            <div>
              <h2>${escapeHtml(job.company)} - ${escapeHtml(job.title)}</h2>
              <div class="meta">${escapeHtml(job.location || "")} ${job.url ? `· <a href="${escapeAttr(job.url)}" target="_blank">posting</a>` : ""}</div>
            </div>
            <div>
              <label>Status</label>
              <select id="status">${["researching","interested","applied","interviewing","offer","rejected","declined","paused"].map(s => `<option ${job.status === s ? "selected" : ""}>${s}</option>`).join("")}</select>
            </div>
            <button onclick="saveStatus(${job.id})">Save status</button>
          </div>
          <div class="chips">
            <span class="chip">Pipeline: ${escapeHtml(job.pipeline || "Unassigned")}</span>
            <span class="chip">GPT: <b class="${scoreClass(job.gpt_score)}">${job.gpt_score ?? "n/a"}</b></span>
            <span class="chip">Mine: <b class="${scoreClass(job.user_score)}">${job.user_score ?? "n/a"}</b></span>
            <span class="chip">Level: ${escapeHtml(job.level_assessment || "n/a")}</span>
            ${job.downlevel ? '<span class="chip">Downlevel</span>' : ""}
            ${job.filtered ? '<span class="chip">Filtered</span>' : ""}
          </div>
          <p>${escapeHtml(job.gpt_rationale || "No GPT rationale yet.")}</p>
          <button class="warn" onclick="scoreGpt(${job.id})" ${state.gpt_scoring_enabled ? "" : "disabled"}>${state.gpt_scoring_enabled ? "Populate GPT scorecard" : "GPT scoring disabled"}</button>
        </div>

        <div class="panel">
          <h2>My Scorecard</h2>
          <div class="score-grid">${rubric.map(field => `
            <label>${pretty(field)}<input id="user_${field}" type="number" list="score_options" min="0" max="10" step="1" value="${job.user_scorecard?.[field] ?? ""}"></label>
          `).join("")}</div>
          <label>Rationale</label><textarea id="user_rationale">${escapeHtml(job.user_rationale || "")}</textarea>
          <button onclick="saveUserScore(${job.id})">Save my score</button>
        </div>

        <div class="panel">
          <h2>GPT Scorecard</h2>
          <div class="score-grid">${rubric.map(field => `
            <div><span class="small">${pretty(field)}</span><br><b>${job.gpt_scorecard?.[field] ?? "n/a"}</b></div>
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
                <span class="chip">GPT: <b class="${scoreClass(d.gpt_score)}">${d.gpt_score ?? "n/a"}</b></span>
                <span class="chip">Level: ${escapeHtml(d.level_assessment || "n/a")}</span>
                ${d.downlevel ? '<span class="chip">Downlevel</span>' : ""}
              </div>
              <p class="small">${escapeHtml(d.rejection_reason || d.gpt_rationale || "")}</p>
            </div>
          `).join("") || '<div class="small">No discoveries yet.</div>'}</div>
        </div>
      `;
    }

    async function createJob() {
      const payload = {
        company: company.value,
        title: title.value,
        url: url.value,
        location: location.value,
        pipeline: pipeline.value,
        posting_text: posting_text.value,
        notes: notes.value,
      };
      const { job } = await api("/api/jobs", { method: "POST", body: JSON.stringify(payload) });
      selectedId = job.id;
      ["company","title","url","location","posting_text","notes"].forEach(id => document.getElementById(id).value = "");
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
          model: document.getElementById("model").value,
        })
      });
      await load();
    }

    async function saveConfig() {
      const payload = {};
      ["OPENAI_API_KEY","OPENAI_MODEL","JOB_SEARCH_ENABLE_GPT_SCORING","JOB_SEARCH_USE_CAPTURE_CACHE"].forEach(key => {
        const value = document.getElementById(`config_${key}`).value.trim();
        if (value) payload[key] = value;
      });
      await api("/api/config", { method: "POST", body: JSON.stringify(payload) });
      ["OPENAI_API_KEY"].forEach(key => document.getElementById(`config_${key}`).value = "");
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

    function toggleFiltered() {
      includeFiltered = !includeFiltered;
      load();
    }

    function showPage(page) {
      currentPage = page;
      document.getElementById("jobs_page").classList.toggle("hidden", page !== "jobs");
      document.getElementById("queries_page").classList.toggle("hidden", page !== "queries");
      document.getElementById("jobs_nav").classList.toggle("secondary", page !== "jobs");
      document.getElementById("queries_nav").classList.toggle("secondary", page !== "queries");
      if (page === "jobs") requestAnimationFrame(() => {
        syncJobsPageHeight();
        initializeJobSplit();
      });
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
      return String(s ?? "").replace(/[&<>"']/g, c => ({ "&":"&amp;", "<":"&lt;", ">":"&gt;", '"':"&quot;", "'":"&#39;" }[c]));
    }
    function escapeAttr(s) { return escapeHtml(s).replace(/`/g, "&#96;"); }
    configureJobSplitDrag();
    load();
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
