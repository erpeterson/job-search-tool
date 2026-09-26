"""SQLite read models for jobs, isolated from Flask and workflow decisions."""

from __future__ import annotations

import sqlite3
from collections.abc import Callable, Mapping, Sequence
from typing import Any


class SqliteJobRepository:
    """Read job records through an injected connection factory."""

    def __init__(self, connect: Callable[[], sqlite3.Connection]) -> None:
        self._connect = connect

    def list_jobs(self, *, include_filtered: bool = False) -> Sequence[Mapping[str, Any]]:
        query = "SELECT * FROM jobs"
        if not include_filtered:
            query += " WHERE filtered = 0"
        query += " ORDER BY updated_at DESC, created_at DESC"
        with self._connect() as connection:
            return [dict(row) for row in connection.execute(query)]

    def get_job(self, job_id: int) -> Mapping[str, Any] | None:
        with self._connect() as connection:
            row = connection.execute("SELECT * FROM jobs WHERE id = ?", (job_id,)).fetchone()
        return dict(row) if row else None

    def get_job_by_url(self, url: str) -> Mapping[str, Any] | None:
        with self._connect() as connection:
            row = connection.execute("SELECT * FROM jobs WHERE url = ? LIMIT 1", (url,)).fetchone()
        return dict(row) if row else None

    def update_status(self, job_id: int, status: str, updated_at: int) -> bool:
        with self._connect() as connection:
            updated = connection.execute(
                "UPDATE jobs SET status = ?, updated_at = ? WHERE id = ?", (status, updated_at, job_id)
            ).rowcount
        return bool(updated)

    def add_note(self, job_id: int, note: str, created_at: int) -> bool:
        with self._connect() as connection:
            if not connection.execute("SELECT 1 FROM jobs WHERE id = ?", (job_id,)).fetchone():
                return False
            connection.execute(
                "INSERT INTO notes(job_id, created_at, note) VALUES (?, ?, ?)", (job_id, created_at, note)
            )
            connection.execute("UPDATE jobs SET updated_at = ? WHERE id = ?", (created_at, job_id))
        return True

    def add_interaction(self, job_id: int, values: Mapping[str, Any], created_at: int) -> bool:
        with self._connect() as connection:
            if not connection.execute("SELECT 1 FROM jobs WHERE id = ?", (job_id,)).fetchone():
                return False
            connection.execute(
                """INSERT INTO interactions(
                    job_id, occurred_on, person_name, person_role, channel, summary, notes_to_self, next_step, created_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                (
                    job_id,
                    values["occurred_on"],
                    values["person_name"],
                    values["person_role"],
                    values["channel"],
                    values["summary"],
                    values["notes_to_self"],
                    values["next_step"],
                    created_at,
                ),
            )
            connection.execute("UPDATE jobs SET updated_at = ? WHERE id = ?", (created_at, job_id))
        return True

    def attach_packet(self, job_id: int, path: str, updated_at: int) -> bool:
        with self._connect() as connection:
            updated = connection.execute(
                "UPDATE jobs SET application_packet_path = ?, updated_at = ? WHERE id = ?", (path, updated_at, job_id)
            ).rowcount
        return bool(updated)

    def delete_job(self, job_id: int) -> bool:
        with self._connect() as connection:
            if not connection.execute("SELECT 1 FROM jobs WHERE id = ?", (job_id,)).fetchone():
                return False
            connection.execute("UPDATE discovered_jobs SET tracked_job_id = NULL WHERE tracked_job_id = ?", (job_id,))
            connection.execute("DELETE FROM jobs WHERE id = ?", (job_id,))
        return True

    def save_user_score(self, job_id: int, total: int, scorecard_json: str, rationale: str, updated_at: int) -> bool:
        with self._connect() as connection:
            updated = connection.execute(
                """UPDATE jobs SET user_score = ?, user_scorecard_json = ?, user_rationale = ?, updated_at = ?
                WHERE id = ?""",
                (total, scorecard_json, rationale, updated_at, job_id),
            ).rowcount
        return bool(updated)

    def rescrape_job(
        self, job_id: int, current: Mapping[str, Any], scraped: Mapping[str, Any], notes: str, updated_at: int
    ) -> bool:
        with self._connect() as connection:
            updated = connection.execute(
                """UPDATE jobs SET company = ?, title = ?, location = ?, posting_text = ?, source_board = ?,
                source_job_id = ?, discovered_at = COALESCE(discovered_at, ?), notes = ?, updated_at = ? WHERE id = ?""",
                (
                    scraped.get("company") or current["company"],
                    scraped.get("title") or current["title"],
                    scraped.get("location") or current["location"],
                    scraped.get("posting_text") or current["posting_text"],
                    scraped.get("source_board") or current["source_board"],
                    scraped.get("source_job_id") or current["source_job_id"],
                    updated_at,
                    notes,
                    updated_at,
                    job_id,
                ),
            ).rowcount
        return bool(updated)

    def purge_jobs(self) -> int:
        with self._connect() as connection:
            count = connection.execute("SELECT COUNT(*) AS count FROM jobs").fetchone()["count"]
            connection.execute("UPDATE discovered_jobs SET tracked_job_id = NULL WHERE tracked_job_id IS NOT NULL")
            connection.execute("DELETE FROM jobs")
        return int(count)

    def create_job(self, values: Mapping[str, Any]) -> int | None:
        with self._connect() as connection:
            existing = connection.execute("SELECT id FROM jobs WHERE url = ? LIMIT 1", (values["url"],)).fetchone()
            if existing:
                return None
            return int(
                connection.execute(
                    """INSERT INTO jobs(
                        created_at, updated_at, company, title, url, location, pipeline, status, posting_text, notes,
                        source_board, source_job_id, discovered_at
                    ) VALUES (
                        :created_at, :updated_at, :company, :title, :url, :location, :pipeline, :status, :posting_text,
                        :notes, :source_board, :source_job_id, :discovered_at
                    ) RETURNING id""",
                    values,
                ).fetchone()["id"]
            )
