"""SQLite persistence for search runs, queries, and discovery decisions."""

from __future__ import annotations

import sqlite3
from collections.abc import Callable, Mapping, Sequence
from typing import Any


class SqliteSearchRepository:
    def __init__(self, connect: Callable[[], sqlite3.Connection]) -> None:
        self._connect = connect

    def start_run(self, started_at: int, trigger: str) -> int:
        with self._connect() as connection:
            return int(
                connection.execute(
                    "INSERT INTO search_runs(started_at, trigger, status) VALUES (?, ?, 'running')",
                    (started_at, trigger),
                ).lastrowid
            )

    def enabled_queries(self) -> Sequence[Mapping[str, Any]]:
        with self._connect() as connection:
            return [
                dict(row)
                for row in connection.execute(
                    "SELECT * FROM search_queries WHERE enabled = 1 ORDER BY seeded DESC, pipeline, board, keywords"
                )
            ]

    def mark_query_run(self, query_id: int, ran_at: int) -> None:
        with self._connect() as connection:
            connection.execute("UPDATE search_queries SET last_run_at = ? WHERE id = ?", (ran_at, query_id))

    def finish_run(self, run_id: int, values: Mapping[str, Any]) -> Mapping[str, Any]:
        with self._connect() as connection:
            connection.execute(
                """UPDATE search_runs SET completed_at = :completed_at, status = 'complete', message = :message,
                found_count = :found_count, tracked_count = :tracked_count, rejected_count = :rejected_count WHERE id = :id""",
                {**values, "id": run_id},
            )
            connection.execute(
                "INSERT INTO settings(key, value) VALUES ('last_search_at', ?) ON CONFLICT(key) DO UPDATE SET value = excluded.value",
                (str(values["completed_at"]),),
            )
            row = connection.execute("SELECT * FROM search_runs WHERE id = ?", (run_id,)).fetchone()
        return dict(row)

    def record_discovery(self, values: Mapping[str, Any], connection: sqlite3.Connection | None = None) -> None:
        if connection is not None:
            self._insert_discovery(connection, values)
            return
        with self._connect() as database:
            self._insert_discovery(database, values)

    @staticmethod
    def _insert_discovery(connection: sqlite3.Connection, values: Mapping[str, Any]) -> None:
        connection.execute(
            """INSERT INTO discovered_jobs(
                    run_id, query_id, created_at, board, source_job_id, company, title, location, url, snippet,
                    gpt_score, gpt_rationale, gpt_scorecard_json, level_assessment, downlevel,
                    decision, rejection_reason, tracked_job_id
                ) VALUES (
                    :run_id, :query_id, :created_at, :board, :source_job_id, :company, :title, :location, :url, :snippet,
                    :gpt_score, :gpt_rationale, :gpt_scorecard_json, :level_assessment, :downlevel,
                    :decision, :rejection_reason, :tracked_job_id
                )""",
            values,
        )
