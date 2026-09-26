"""SQLite schema creation and additive migrations."""

import sqlite3

SCHEMA = """
CREATE TABLE IF NOT EXISTS settings (key TEXT PRIMARY KEY, value TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS jobs (
 id INTEGER PRIMARY KEY AUTOINCREMENT, created_at INTEGER NOT NULL, updated_at INTEGER NOT NULL,
 company TEXT NOT NULL, title TEXT NOT NULL, url TEXT, location TEXT, pipeline TEXT,
 status TEXT NOT NULL DEFAULT 'researching', posting_text TEXT, notes TEXT, gpt_score INTEGER,
 gpt_rationale TEXT, gpt_scorecard_json TEXT, user_score INTEGER, user_scorecard_json TEXT,
 user_rationale TEXT, filtered INTEGER NOT NULL DEFAULT 0, source_board TEXT, source_job_id TEXT,
 discovered_at INTEGER, level_assessment TEXT, downlevel INTEGER NOT NULL DEFAULT 0, application_packet_path TEXT
);
CREATE TABLE IF NOT EXISTS interactions (id INTEGER PRIMARY KEY AUTOINCREMENT, job_id INTEGER NOT NULL REFERENCES jobs(id) ON DELETE CASCADE, occurred_on TEXT NOT NULL, person_name TEXT, person_role TEXT, channel TEXT, summary TEXT, notes_to_self TEXT, next_step TEXT, created_at INTEGER NOT NULL);
CREATE TABLE IF NOT EXISTS notes (id INTEGER PRIMARY KEY AUTOINCREMENT, job_id INTEGER NOT NULL REFERENCES jobs(id) ON DELETE CASCADE, created_at INTEGER NOT NULL, note TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS search_queries (id INTEGER PRIMARY KEY AUTOINCREMENT, board TEXT NOT NULL, pipeline TEXT, keywords TEXT NOT NULL, location TEXT, enabled INTEGER NOT NULL DEFAULT 1, created_at INTEGER NOT NULL, last_run_at INTEGER, criteria TEXT, refinement_notes TEXT, seeded INTEGER NOT NULL DEFAULT 0);
CREATE TABLE IF NOT EXISTS search_runs (id INTEGER PRIMARY KEY AUTOINCREMENT, started_at INTEGER NOT NULL, completed_at INTEGER, trigger TEXT NOT NULL, status TEXT NOT NULL, message TEXT, found_count INTEGER NOT NULL DEFAULT 0, tracked_count INTEGER NOT NULL DEFAULT 0, rejected_count INTEGER NOT NULL DEFAULT 0);
CREATE TABLE IF NOT EXISTS discovered_jobs (id INTEGER PRIMARY KEY AUTOINCREMENT, run_id INTEGER REFERENCES search_runs(id) ON DELETE SET NULL, query_id INTEGER REFERENCES search_queries(id) ON DELETE SET NULL, created_at INTEGER NOT NULL, board TEXT NOT NULL, source_job_id TEXT, company TEXT, title TEXT, location TEXT, url TEXT, snippet TEXT, gpt_score INTEGER, gpt_rationale TEXT, gpt_scorecard_json TEXT, level_assessment TEXT, downlevel INTEGER NOT NULL DEFAULT 0, decision TEXT NOT NULL, rejection_reason TEXT, tracked_job_id INTEGER REFERENCES jobs(id) ON DELETE SET NULL);
CREATE TABLE IF NOT EXISTS level_equivalencies (id INTEGER PRIMARY KEY AUTOINCREMENT, company TEXT NOT NULL, normalized_company TEXT NOT NULL, title_pattern TEXT NOT NULL, normalized_title_pattern TEXT NOT NULL, source_level TEXT, source_level_title TEXT, oracle_level TEXT NOT NULL, oracle_title TEXT NOT NULL, downlevel INTEGER NOT NULL, source_url TEXT, notes TEXT, created_at INTEGER NOT NULL, updated_at INTEGER NOT NULL, UNIQUE(normalized_company, normalized_title_pattern));
CREATE TABLE IF NOT EXISTS company_interests (id INTEGER PRIMARY KEY AUTOINCREMENT, created_at INTEGER NOT NULL, updated_at INTEGER NOT NULL, company TEXT NOT NULL, normalized_company TEXT NOT NULL UNIQUE, status TEXT NOT NULL DEFAULT 'watching', interest_score INTEGER, rationale TEXT, notes TEXT, next_step TEXT, contacts TEXT);
"""


def initialize_schema(connection: sqlite3.Connection) -> None:
    connection.executescript(SCHEMA)
    for table, column, definition in (
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
    ):
        ensure_column(connection, table, column, definition)
    connection.execute(
        "CREATE UNIQUE INDEX IF NOT EXISTS idx_jobs_url ON jobs(url) WHERE url IS NOT NULL AND url != ''"
    )
    connection.execute(
        "CREATE INDEX IF NOT EXISTS idx_level_equivalencies_company ON level_equivalencies(normalized_company)"
    )
    connection.execute("CREATE INDEX IF NOT EXISTS idx_company_interests_status ON company_interests(status)")


def ensure_column(connection: sqlite3.Connection, table: str, column: str, definition: str) -> None:
    columns = {row["name"] for row in connection.execute(f"PRAGMA table_info({table})")}
    if column not in columns:
        connection.execute(f"ALTER TABLE {table} ADD COLUMN {column} {definition}")
