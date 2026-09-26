"""SQLite lease used to ensure only one scheduler owns a database at a time."""

from __future__ import annotations

import sqlite3
from pathlib import Path


class SchedulerLeaseRepository:
    def __init__(self, database_path: Path) -> None:
        self._database_path = database_path

    def acquire(self, owner: str, timestamp: int, lease_seconds: int) -> bool:
        connection = sqlite3.connect(self._database_path, timeout=10)
        try:
            connection.execute("BEGIN IMMEDIATE")
            connection.execute(
                """CREATE TABLE IF NOT EXISTS scheduler_leases (
                name TEXT PRIMARY KEY, owner TEXT NOT NULL, expires_at INTEGER NOT NULL)"""
            )
            row = connection.execute("SELECT owner, expires_at FROM scheduler_leases WHERE name = 'search'").fetchone()
            if row and row[0] != owner and row[1] > timestamp:
                connection.commit()
                return False
            connection.execute(
                """INSERT INTO scheduler_leases(name, owner, expires_at) VALUES ('search', ?, ?)
                ON CONFLICT(name) DO UPDATE SET owner = excluded.owner, expires_at = excluded.expires_at""",
                (owner, timestamp + lease_seconds),
            )
            connection.commit()
            return True
        finally:
            connection.close()
