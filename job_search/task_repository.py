"""Durable SQLite storage for asynchronous work records."""

from __future__ import annotations

import sqlite3
from contextlib import contextmanager
from pathlib import Path
from typing import Any


class TaskRepository:
    """Persist task state so status survives web-process restarts."""

    def __init__(self, database_path: Path) -> None:
        self._database_path = database_path

    def _connect(self) -> sqlite3.Connection:
        connection = sqlite3.connect(self._database_path, timeout=10)
        connection.row_factory = sqlite3.Row
        connection.execute("PRAGMA foreign_keys = ON")
        connection.execute("PRAGMA busy_timeout = 10000")
        return connection

    @contextmanager
    def _connection(self):
        connection = self._connect()
        try:
            yield connection
            connection.commit()
        except Exception:
            connection.rollback()
            raise
        finally:
            connection.close()

    def initialize(self, timestamp: int) -> int:
        with self._connection() as connection:
            connection.executescript(
                """
                CREATE TABLE IF NOT EXISTS background_tasks (
                    id TEXT PRIMARY KEY,
                    operation TEXT NOT NULL,
                    status TEXT NOT NULL,
                    created_at INTEGER NOT NULL,
                    updated_at INTEGER NOT NULL,
                    started_at INTEGER,
                    completed_at INTEGER,
                    total INTEGER NOT NULL,
                    completed INTEGER NOT NULL DEFAULT 0,
                    failed INTEGER NOT NULL DEFAULT 0,
                    skipped INTEGER NOT NULL DEFAULT 0,
                    current_job_id INTEGER,
                    message TEXT NOT NULL DEFAULT ''
                );
                CREATE TABLE IF NOT EXISTS background_task_items (
                    task_id TEXT NOT NULL REFERENCES background_tasks(id) ON DELETE CASCADE,
                    job_id INTEGER NOT NULL,
                    status TEXT NOT NULL,
                    message TEXT NOT NULL DEFAULT '',
                    updated_at INTEGER NOT NULL,
                    PRIMARY KEY(task_id, job_id)
                );
                """
            )
            cursor = connection.execute(
                """
                UPDATE background_tasks
                SET status = 'interrupted', completed_at = ?, updated_at = ?,
                    message = 'Worker stopped before task completion; retry the selected jobs.'
                WHERE status IN ('queued', 'running')
                """,
                (timestamp, timestamp),
            )
            return cursor.rowcount

    def create(self, task_id: str, operation: str, job_ids: list[int], timestamp: int) -> dict[str, Any]:
        with self._connection() as connection:
            connection.execute(
                """INSERT INTO background_tasks(id, operation, status, created_at, updated_at, total)
                   VALUES (?, ?, 'queued', ?, ?, ?)""",
                (task_id, operation, timestamp, timestamp, len(job_ids)),
            )
            connection.executemany(
                """INSERT INTO background_task_items(task_id, job_id, status, updated_at)
                   VALUES (?, ?, 'queued', ?)""",
                [(task_id, job_id, timestamp) for job_id in job_ids],
            )
        return self.get(task_id) or {}

    def get(self, task_id: str) -> dict[str, Any] | None:
        with self._connection() as connection:
            task = connection.execute("SELECT * FROM background_tasks WHERE id = ?", (task_id,)).fetchone()
            if task is None:
                return None
            value = dict(task)
            value["items"] = [
                dict(row)
                for row in connection.execute(
                    "SELECT job_id, status, message, updated_at FROM background_task_items WHERE task_id = ? ORDER BY job_id",
                    (task_id,),
                )
            ]
            return value

    def list(self, limit: int) -> list[dict[str, Any]]:
        with self._connection() as connection:
            task_ids = [
                row["id"]
                for row in connection.execute(
                    "SELECT id FROM background_tasks ORDER BY created_at DESC LIMIT ?", (limit,)
                )
            ]
        return [task for task_id in task_ids if (task := self.get(task_id)) is not None]

    def update(self, task_id: str, timestamp: int, **updates: Any) -> dict[str, Any] | None:
        allowed = {
            "status",
            "started_at",
            "completed_at",
            "completed",
            "failed",
            "skipped",
            "current_job_id",
            "message",
        }
        changes = {key: value for key, value in updates.items() if key in allowed}
        if not changes:
            return self.get(task_id)
        assignments = ", ".join([*(f"{key} = ?" for key in changes), "updated_at = ?"])
        with self._connection() as connection:
            connection.execute(
                f"UPDATE background_tasks SET {assignments} WHERE id = ?", (*changes.values(), timestamp, task_id)
            )
        return self.get(task_id)

    def update_item(self, task_id: str, job_id: int, timestamp: int, **updates: Any) -> dict[str, Any] | None:
        allowed = {"status", "message"}
        changes = {key: value for key, value in updates.items() if key in allowed}
        if not changes:
            return self.get(task_id)
        assignments = ", ".join([*(f"{key} = ?" for key in changes), "updated_at = ?"])
        with self._connect() as connection:
            connection.execute(
                f"UPDATE background_task_items SET {assignments} WHERE task_id = ? AND job_id = ?",
                (*changes.values(), timestamp, task_id, job_id),
            )
            connection.execute("UPDATE background_tasks SET updated_at = ? WHERE id = ?", (timestamp, task_id))
        return self.get(task_id)
