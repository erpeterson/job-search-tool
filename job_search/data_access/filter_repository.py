"""SQLite adapter used by the job visibility policy."""

from __future__ import annotations

import sqlite3
from collections.abc import Callable, Mapping, Sequence
from typing import Any


class SqliteJobFilterRepository:
    def __init__(self, connect: Callable[[], sqlite3.Connection]) -> None:
        self._connect = connect

    def settings(self) -> Mapping[str, str]:
        with self._connect() as connection:
            return {row["key"]: row["value"] for row in connection.execute("SELECT key, value FROM settings")}

    def job_for_filtering(self, job_id: int) -> Mapping[str, Any] | None:
        with self._connect() as connection:
            row = connection.execute(
                "SELECT id, company, title, gpt_score, user_score, downlevel FROM jobs WHERE id = ?", (job_id,)
            ).fetchone()
        return dict(row) if row else None

    def all_job_ids(self) -> Sequence[int]:
        with self._connect() as connection:
            return [int(row["id"]) for row in connection.execute("SELECT id FROM jobs")]

    def save_filter_decision(self, job_id: int, filtered: bool, updated_at: int) -> None:
        with self._connect() as connection:
            connection.execute(
                "UPDATE jobs SET filtered = ?, updated_at = ? WHERE id = ?", (int(filtered), updated_at, job_id)
            )
