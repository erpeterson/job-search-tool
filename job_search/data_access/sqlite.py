"""SQLite connection factory kept outside the application-service layer."""

from __future__ import annotations

import sqlite3
from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path


@contextmanager
def connection(database_path: Path) -> Iterator[sqlite3.Connection]:
    """Open a configured SQLite connection with consistent safety settings."""
    database = sqlite3.connect(database_path, timeout=10)
    database.row_factory = sqlite3.Row
    database.execute("PRAGMA foreign_keys = ON")
    database.execute("PRAGMA busy_timeout = 10000")
    try:
        yield database
        database.commit()
    finally:
        database.close()


class ManagedConnection(sqlite3.Connection):
    """A connection that closes when a ``with`` transaction finishes."""

    def __exit__(self, exc_type, exc_value, traceback):
        try:
            return super().__exit__(exc_type, exc_value, traceback)
        finally:
            self.close()


def open_connection(database_path: Path) -> sqlite3.Connection:
    """Open a managed SQLite handle for legacy composition and repositories."""
    database = sqlite3.connect(database_path, timeout=10, factory=ManagedConnection)
    database.row_factory = sqlite3.Row
    database.execute("PRAGMA foreign_keys = ON")
    database.execute("PRAGMA busy_timeout = 10000")
    return database
