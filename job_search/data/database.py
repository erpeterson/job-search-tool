"""SQLite connection management, schema creation, and migrations."""

import sqlite3
from contextlib import contextmanager

from job_search.data.repositories import UnitOfWork
from job_search.domain.text import normalize_lookup_text

SCHEMA = """
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
    target_level TEXT NOT NULL,
    target_title TEXT NOT NULL,
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

# Columns renamed after release: (table, old name, new name). Earlier releases named the
# target-level columns after the original candidate's employer.
_RENAMED_COLUMNS = (
    ("level_equivalencies", "oracle_level", "target_level"),
    ("level_equivalencies", "oracle_title", "target_title"),
)

# Columns added after the first release; applied to older databases.
_ADDED_COLUMNS = (
    ("jobs", "source_board", "TEXT"),
    ("jobs", "source_job_id", "TEXT"),
    ("jobs", "discovered_at", "INTEGER"),
    ("jobs", "level_assessment", "TEXT"),
    ("jobs", "downlevel", "INTEGER NOT NULL DEFAULT 0"),
    ("jobs", "application_packet_path", "TEXT"),
    ("search_queries", "pipeline", "TEXT"),
    ("search_queries", "criteria", "TEXT"),
    ("search_queries", "refinement_notes", "TEXT"),
    ("search_queries", "seeded", "INTEGER NOT NULL DEFAULT 0"),
    ("discovered_jobs", "query_id", "INTEGER"),
    ("jobs", "normalized_company", "TEXT"),
)

_INDEXES = (
    "CREATE UNIQUE INDEX IF NOT EXISTS idx_jobs_url ON jobs(url) WHERE url IS NOT NULL AND url != ''",
    "CREATE INDEX IF NOT EXISTS idx_level_equivalencies_company ON level_equivalencies(normalized_company)",
    "CREATE INDEX IF NOT EXISTS idx_company_interests_status ON company_interests(status)",
    "CREATE INDEX IF NOT EXISTS idx_jobs_normalized_company ON jobs(normalized_company)",
)

_DATA_MIGRATIONS = (
    # Remove a hardcoded level-equivalency seed from an earlier release.
    """
    DELETE FROM level_equivalencies
    WHERE normalized_company = 'atlassian'
      AND normalized_title_pattern = 'principal engineer'
      AND notes LIKE '%user-provided equivalency%'
    """,
    # Disable legacy ad-hoc queries superseded by seeded pipeline queries.
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
    """,
)


class Database:
    def __init__(self, path, timeout_seconds=30):
        self.path = path
        self.timeout_seconds = timeout_seconds

    def _connect(self):
        conn = sqlite3.connect(self.path, timeout=self.timeout_seconds)
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA foreign_keys = ON")
        return conn

    @contextmanager
    def unit_of_work(self):
        """Yield repositories sharing one transaction; commit on success, always close."""
        conn = self._connect()
        committed = False
        try:
            yield UnitOfWork(conn)
            conn.commit()
            committed = True
        finally:
            if not committed:
                conn.rollback()
            conn.close()

    def create_schema(self):
        self.path.parent.mkdir(parents=True, exist_ok=True)
        with self.unit_of_work() as uow:
            conn = uow.connection
            conn.executescript(SCHEMA)
            for table, old, new in _RENAMED_COLUMNS:
                existing = {row["name"] for row in conn.execute(f"PRAGMA table_info({table})")}
                if old in existing and new not in existing:
                    # Names come from the constant above, never from input; RENAME keeps the data.
                    conn.execute(f"ALTER TABLE {table} RENAME COLUMN {old} TO {new}")
            for table, column, definition in _ADDED_COLUMNS:
                existing = {row["name"] for row in conn.execute(f"PRAGMA table_info({table})")}
                if column not in existing:
                    # Table/column names come from the constant above, never from input.
                    conn.execute(f"ALTER TABLE {table} ADD COLUMN {column} {definition}")
            for statement in _INDEXES:
                conn.execute(statement)
            for statement in _DATA_MIGRATIONS:
                conn.execute(statement)
            # Backfill in Python: the normalization regex has no SQLite equivalent.
            conn.executemany(
                "UPDATE jobs SET normalized_company = ? WHERE id = ?",
                [
                    (normalize_lookup_text(row["company"]), row["id"])
                    for row in conn.execute("SELECT id, company FROM jobs WHERE normalized_company IS NULL")
                ],
            )
