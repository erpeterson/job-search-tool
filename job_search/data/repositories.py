"""SQLite repositories. Each repository operates on the connection of one unit of work."""

import json
import logging

from job_search.domain.text import normalize_lookup_text
from job_search.observability import record_exception

JOB_COLUMNS = frozenset(
    {
        "created_at",
        "updated_at",
        "company",
        "title",
        "url",
        "location",
        "pipeline",
        "status",
        "posting_text",
        "notes",
        "gpt_score",
        "gpt_rationale",
        "gpt_scorecard_json",
        "user_score",
        "user_scorecard_json",
        "user_rationale",
        "filtered",
        "source_board",
        "source_job_id",
        "discovered_at",
        "level_assessment",
        "downlevel",
        "application_packet_path",
        "normalized_company",
    }
)


def _chunks(values, size=500):
    """Split values so IN (...) lists stay under SQLite's bound-parameter limit."""
    for start in range(0, len(values), size):
        yield values[start : start + size]


def _row_to_dict(row):
    return dict(row) if row else None


def _parse_json_field(value, fallback, table, column, row_id):
    if not value:
        return fallback
    try:
        return json.loads(value)
    except json.JSONDecodeError as exc:
        record_exception(
            "db_json_field_corrupt",
            "data.repositories",
            "parse_json_field",
            exc,
            level=logging.WARNING,
            recovery="Returning an empty value so the row remains viewable.",
            table=table,
            column=column,
            row_id=row_id,
        )
        return fallback


def _job_from_row(row):
    job = _row_to_dict(row)
    job["gpt_scorecard"] = _parse_json_field(job.pop("gpt_scorecard_json"), {}, "jobs", "gpt_scorecard_json", job["id"])
    job["user_scorecard"] = _parse_json_field(
        job.pop("user_scorecard_json"), {}, "jobs", "user_scorecard_json", job["id"]
    )
    return job


class JobRepository:
    def __init__(self, conn):
        self._conn = conn

    def get(self, job_id):
        row = self._conn.execute("SELECT * FROM jobs WHERE id = ?", (job_id,)).fetchone()
        if not row:
            return None
        job = _job_from_row(row)
        job["interactions"] = [
            _row_to_dict(r)
            for r in self._conn.execute(
                "SELECT * FROM interactions WHERE job_id = ? ORDER BY occurred_on DESC, id DESC", (job_id,)
            )
        ]
        job["notes_list"] = [
            _row_to_dict(r)
            for r in self._conn.execute(
                "SELECT * FROM notes WHERE job_id = ? ORDER BY created_at DESC, id DESC", (job_id,)
            )
        ]
        return job

    def exists(self, job_id):
        return self._conn.execute("SELECT 1 FROM jobs WHERE id = ?", (job_id,)).fetchone() is not None

    def list(self, include_filtered=False):
        query = "SELECT * FROM jobs"
        if not include_filtered:
            query += " WHERE filtered = 0"
        query += " ORDER BY updated_at DESC, created_at DESC"
        return [_job_from_row(row) for row in self._conn.execute(query)]

    def existing_urls(self, urls):
        """Return the subset of ``urls`` already tracked, using one query per 500 URLs."""
        found = set()
        for chunk in _chunks(sorted({url for url in urls if url})):
            placeholders = ", ".join("?" for _ in chunk)
            found.update(
                row["url"]
                # Only "?" placeholders are interpolated; values are bound parameters.
                for row in self._conn.execute(f"SELECT url FROM jobs WHERE url IN ({placeholders})", chunk)  # noqa: S608 - placeholders only
            )
        return found

    def find_id_by_url(self, url):
        if not url:
            return None
        row = self._conn.execute("SELECT id FROM jobs WHERE url = ? LIMIT 1", (url,)).fetchone()
        return row["id"] if row else None

    def insert(self, fields):
        if "company" in fields:
            fields = {**fields, "normalized_company": normalize_lookup_text(fields["company"])}
        columns = list(fields)
        unknown = set(columns) - JOB_COLUMNS
        if unknown:
            raise ValueError(f"Unknown jobs columns: {sorted(unknown)}")
        placeholders = ", ".join("?" for _ in columns)
        cur = self._conn.execute(
            # Column names are checked against JOB_COLUMNS above; values are bound parameters.
            f"INSERT INTO jobs({', '.join(columns)}) VALUES ({placeholders})",  # noqa: S608 - allowlisted columns
            [fields[column] for column in columns],
        )
        return cur.lastrowid

    def update_scraped_posting(self, job_id, fields, ts):
        self._conn.execute(
            """
            UPDATE jobs
            SET company = ?, normalized_company = ?, title = ?, location = ?, posting_text = ?,
                source_board = ?, source_job_id = ?, discovered_at = COALESCE(discovered_at, ?),
                notes = ?, updated_at = ?
            WHERE id = ?
            """,
            (
                fields["company"],
                normalize_lookup_text(fields["company"]),
                fields["title"],
                fields["location"],
                fields["posting_text"],
                fields["source_board"],
                fields["source_job_id"],
                ts,
                fields["notes"],
                ts,
                job_id,
            ),
        )

    def update_codex_score(self, job_id, total, rationale, scorecard, pipeline, level_assessment, downlevel, ts):
        self._conn.execute(
            """
            UPDATE jobs
            SET gpt_score = ?, gpt_rationale = ?, gpt_scorecard_json = ?,
                pipeline = COALESCE(NULLIF(?, ''), pipeline),
                level_assessment = ?, downlevel = ?, updated_at = ?
            WHERE id = ?
            """,
            (total, rationale, json.dumps(scorecard), pipeline, level_assessment, 1 if downlevel else 0, ts, job_id),
        )

    def update_user_score(self, job_id, total, scorecard, rationale, ts):
        self._conn.execute(
            "UPDATE jobs SET user_score = ?, user_scorecard_json = ?, user_rationale = ?, updated_at = ? WHERE id = ?",
            (total, json.dumps(scorecard), rationale, ts, job_id),
        )

    def update_status(self, job_id, status, ts):
        self._conn.execute("UPDATE jobs SET status = ?, updated_at = ? WHERE id = ?", (status, ts, job_id))

    def set_application_packet_path(self, job_id, path, ts):
        self._conn.execute(
            "UPDATE jobs SET application_packet_path = ?, updated_at = ? WHERE id = ?", (path, ts, job_id)
        )

    def touch(self, job_id, ts):
        self._conn.execute("UPDATE jobs SET updated_at = ? WHERE id = ?", (ts, job_id))

    def filter_inputs(self, job_id=None):
        query = "SELECT id, company, title, gpt_score, user_score, downlevel FROM jobs"
        if job_id is None:
            return [_row_to_dict(row) for row in self._conn.execute(query)]
        return [_row_to_dict(row) for row in self._conn.execute(f"{query} WHERE id = ?", (job_id,))]

    def set_filtered_many(self, updates, ts):
        """``updates`` is an iterable of ``(job_id, filtered)``."""
        self._conn.executemany(
            "UPDATE jobs SET filtered = ?, updated_at = ? WHERE id = ?",
            [(1 if filtered else 0, ts, job_id) for job_id, filtered in updates],
        )

    def delete(self, job_id):
        self._conn.execute("DELETE FROM jobs WHERE id = ?", (job_id,))

    def delete_all(self):
        count = self._conn.execute("SELECT COUNT(*) AS count FROM jobs").fetchone()["count"]
        self._conn.execute("DELETE FROM jobs")
        return count

    def packet_associations(self):
        return [
            _row_to_dict(row)
            for row in self._conn.execute(
                """
                SELECT id, company, title, application_packet_path
                FROM jobs
                WHERE application_packet_path IS NOT NULL
                  AND application_packet_path != ''
                """
            )
        ]

    def calibration_examples(self, limit=8):
        return [
            _row_to_dict(row)
            for row in self._conn.execute(
                """
                SELECT company, title, pipeline, gpt_score, user_score, user_rationale, posting_text
                FROM jobs
                WHERE user_score IS NOT NULL
                ORDER BY updated_at DESC
                LIMIT ?
                """,
                (limit,),
            )
        ]

    def add_interaction(self, job_id, fields, ts):
        self._conn.execute(
            """
            INSERT INTO interactions(
                job_id, occurred_on, person_name, person_role, channel, summary, notes_to_self, next_step, created_at
            )
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                job_id,
                fields["occurred_on"],
                fields["person_name"],
                fields["person_role"],
                fields["channel"],
                fields["summary"],
                fields["notes_to_self"],
                fields["next_step"],
                ts,
            ),
        )

    def add_note(self, job_id, note, ts):
        self._conn.execute("INSERT INTO notes(job_id, created_at, note) VALUES (?, ?, ?)", (job_id, ts, note))


class CompanyRepository:
    def __init__(self, conn):
        self._conn = conn

    def list_with_job_counts(self):
        return [
            _row_to_dict(row)
            for row in self._conn.execute(
                """
                SELECT ci.*,
                       COUNT(j.id) AS tracked_job_count,
                       MAX(j.updated_at) AS latest_job_updated_at
                FROM company_interests ci
                LEFT JOIN jobs j ON j.normalized_company = ci.normalized_company
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
            )
        ]

    def get(self, company_id):
        row = self._conn.execute("SELECT * FROM company_interests WHERE id = ?", (company_id,)).fetchone()
        if not row:
            return None
        company = _row_to_dict(row)
        company["jobs"] = [
            _row_to_dict(job)
            for job in self._conn.execute(
                """
                SELECT id, company, title, url, location, pipeline, status, gpt_score, user_score, filtered, downlevel
                FROM jobs WHERE normalized_company = ? ORDER BY updated_at DESC
                """,
                (company["normalized_company"],),
            )
        ]
        return company

    def upsert(self, fields, ts):
        row = self._conn.execute(
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
                fields["company"],
                fields["normalized_company"],
                fields["status"],
                fields["interest_score"],
                fields["rationale"],
                fields["notes"],
                fields["next_step"],
                fields["contacts"],
            ),
        ).fetchone()
        return row["id"]

    def update(self, company_id, fields, ts):
        self._conn.execute(
            """
            UPDATE company_interests
            SET company = ?, normalized_company = ?, status = ?, interest_score = ?,
                rationale = ?, notes = ?, next_step = ?, contacts = ?, updated_at = ?
            WHERE id = ?
            """,
            (
                fields["company"],
                fields["normalized_company"],
                fields["status"],
                fields["interest_score"],
                fields["rationale"],
                fields["notes"],
                fields["next_step"],
                fields["contacts"],
                ts,
                company_id,
            ),
        )


class SearchRepository:
    def __init__(self, conn):
        self._conn = conn

    def list_queries(self):
        return [
            _row_to_dict(row)
            for row in self._conn.execute(
                "SELECT * FROM search_queries ORDER BY enabled DESC, seeded DESC, pipeline, board, keywords, location"
            )
        ]

    def list_enabled_queries(self):
        return [
            _row_to_dict(row)
            for row in self._conn.execute(
                "SELECT * FROM search_queries WHERE enabled = 1 ORDER BY seeded DESC, pipeline, board, keywords"
            )
        ]

    def get_query(self, query_id):
        return _row_to_dict(self._conn.execute("SELECT * FROM search_queries WHERE id = ?", (query_id,)).fetchone())

    def find_seeded_query(self, board, pipeline):
        return _row_to_dict(
            self._conn.execute(
                """
                SELECT id, keywords, criteria FROM search_queries
                WHERE board = ? AND pipeline = ? AND seeded = 1
                LIMIT 1
                """,
                (board, pipeline),
            ).fetchone()
        )

    def create_query(self, fields, ts, seeded=False):
        cur = self._conn.execute(
            """
            INSERT INTO search_queries(board, pipeline, keywords, location, enabled, created_at, criteria, seeded)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                fields["board"],
                fields["pipeline"],
                fields["keywords"],
                fields["location"],
                1 if fields.get("enabled", True) else 0,
                ts,
                fields["criteria"],
                1 if seeded else 0,
            ),
        )
        return cur.lastrowid

    def update_seeded_query(self, query_id, keywords, criteria, default_location):
        self._conn.execute(
            "UPDATE search_queries SET criteria = ?, keywords = ?, location = COALESCE(location, ?) WHERE id = ?",
            (criteria, keywords, default_location, query_id),
        )

    def update_query(self, query_id, fields):
        """Update only the provided fields; ``None`` values leave columns unchanged."""
        self._conn.execute(
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
                fields.get("board"),
                fields.get("pipeline"),
                fields.get("keywords"),
                fields.get("location"),
                fields.get("criteria"),
                None if fields.get("enabled") is None else (1 if fields["enabled"] else 0),
                query_id,
            ),
        )

    def set_query_last_run(self, query_id, ts):
        self._conn.execute("UPDATE search_queries SET last_run_at = ? WHERE id = ?", (ts, query_id))

    def apply_refinement(self, query_id, keywords, location, criteria, notes):
        self._conn.execute(
            "UPDATE search_queries SET keywords = ?, location = ?, criteria = ?, refinement_notes = ? WHERE id = ?",
            (keywords, location, criteria, notes, query_id),
        )

    def start_run(self, trigger, ts):
        cur = self._conn.execute(
            "INSERT INTO search_runs(started_at, trigger, status) VALUES (?, ?, 'running')", (ts, trigger)
        )
        return cur.lastrowid

    def complete_run(self, run_id, message, found_count, tracked_count, rejected_count, ts):
        self._conn.execute(
            """
            UPDATE search_runs
            SET completed_at = ?, status = 'complete', message = ?, found_count = ?, tracked_count = ?,
                rejected_count = ?
            WHERE id = ?
            """,
            (ts, message, found_count, tracked_count, rejected_count, run_id),
        )

    def fail_run(self, run_id, message, found_count, tracked_count, rejected_count, ts):
        self._conn.execute(
            """
            UPDATE search_runs
            SET completed_at = ?, status = 'error', message = ?, found_count = ?, tracked_count = ?,
                rejected_count = ?
            WHERE id = ?
            """,
            (ts, message, found_count, tracked_count, rejected_count, run_id),
        )

    def get_run(self, run_id):
        return _row_to_dict(self._conn.execute("SELECT * FROM search_runs WHERE id = ?", (run_id,)).fetchone())

    def list_runs(self, limit=20):
        return [
            _row_to_dict(row)
            for row in self._conn.execute("SELECT * FROM search_runs ORDER BY started_at DESC LIMIT ?", (limit,))
        ]


class DiscoveryRepository:
    def __init__(self, conn):
        self._conn = conn

    def insert(self, record):
        self._conn.execute(
            """
            INSERT INTO discovered_jobs(
                run_id, query_id, created_at, board, source_job_id, company, title, location, url, snippet,
                gpt_score, gpt_rationale, gpt_scorecard_json, level_assessment, downlevel,
                decision, rejection_reason, tracked_job_id
            )
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                record["run_id"],
                record["query_id"],
                record["created_at"],
                record["board"],
                record.get("source_job_id"),
                record.get("company"),
                record.get("title"),
                record.get("location"),
                record.get("url"),
                record.get("snippet"),
                record.get("gpt_score"),
                record.get("gpt_rationale"),
                json.dumps(record.get("scorecard") or {}),
                record.get("level_assessment") or "",
                1 if record.get("downlevel") else 0,
                record["decision"],
                record.get("rejection_reason"),
                record.get("tracked_job_id"),
            ),
        )

    def list_recent(self, limit=50):
        discoveries = []
        for row in self._conn.execute(
            "SELECT * FROM discovered_jobs ORDER BY created_at DESC, id DESC LIMIT ?", (limit,)
        ).fetchall():
            item = _row_to_dict(row)
            item["gpt_scorecard"] = _parse_json_field(
                item.pop("gpt_scorecard_json"), {}, "discovered_jobs", "gpt_scorecard_json", item["id"]
            )
            discoveries.append(item)
        return discoveries

    def recent_for_query(self, query_id, limit=20):
        return [
            _row_to_dict(row)
            for row in self._conn.execute(
                """
                SELECT company, title, location, gpt_score, level_assessment, downlevel, decision, rejection_reason,
                       gpt_rationale
                FROM discovered_jobs
                WHERE query_id = ?
                ORDER BY created_at DESC
                LIMIT ?
                """,
                (query_id, limit),
            )
        ]

    def detach_job(self, job_id):
        self._conn.execute("UPDATE discovered_jobs SET tracked_job_id = NULL WHERE tracked_job_id = ?", (job_id,))

    def detach_all_jobs(self):
        self._conn.execute("UPDATE discovered_jobs SET tracked_job_id = NULL WHERE tracked_job_id IS NOT NULL")


class SettingsRepository:
    def __init__(self, conn):
        self._conn = conn

    def all(self):
        return {row["key"]: row["value"] for row in self._conn.execute("SELECT key, value FROM settings")}

    def set(self, key, value):
        self._conn.execute(
            "INSERT INTO settings(key, value) VALUES (?, ?) ON CONFLICT(key) DO UPDATE SET value = excluded.value",
            (key, str(value)),
        )

    def set_default(self, key, value):
        self._conn.execute("INSERT OR IGNORE INTO settings(key, value) VALUES (?, ?)", (key, str(value)))


class LevelEquivalencyRepository:
    def __init__(self, conn):
        self._conn = conn

    def for_company(self, normalized_company):
        return [
            _row_to_dict(row)
            for row in self._conn.execute(
                """
                SELECT * FROM level_equivalencies
                WHERE normalized_company = ?
                ORDER BY LENGTH(normalized_title_pattern) DESC
                """,
                (normalized_company,),
            )
        ]

    def for_companies(self, normalized_companies):
        """Return ``{normalized_company: rows}`` for many companies in one query, rows longest pattern first."""
        companies = sorted(normalized_companies)
        grouped = {company: [] for company in companies}
        for chunk in _chunks(companies):
            placeholders = ", ".join("?" for _ in chunk)
            for row in self._conn.execute(
                # Only "?" placeholders are interpolated; values are bound parameters.
                f"SELECT * FROM level_equivalencies WHERE normalized_company IN ({placeholders}) "  # noqa: S608 - placeholders only
                "ORDER BY normalized_company, LENGTH(normalized_title_pattern) DESC",
                chunk,
            ):
                grouped[row["normalized_company"]].append(_row_to_dict(row))
        return grouped

    def upsert(self, fields, ts):
        self._conn.execute(
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
                fields["company"],
                fields["normalized_company"],
                fields["title_pattern"],
                fields["normalized_title_pattern"],
                fields["source_level"],
                fields["source_level_title"],
                fields["oracle_level"],
                fields["oracle_title"],
                1 if fields["downlevel"] else 0,
                fields["source_url"],
                fields["notes"],
                ts,
                ts,
            ),
        )

    def count(self):
        return self._conn.execute("SELECT COUNT(*) FROM level_equivalencies").fetchone()[0]


class UnitOfWork:
    """Repositories bound to a single connection and transaction."""

    def __init__(self, conn):
        self.connection = conn
        self.jobs = JobRepository(conn)
        self.companies = CompanyRepository(conn)
        self.search = SearchRepository(conn)
        self.discoveries = DiscoveryRepository(conn)
        self.settings = SettingsRepository(conn)
        self.levels = LevelEquivalencyRepository(conn)
