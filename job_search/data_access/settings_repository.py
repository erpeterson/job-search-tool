"""SQLite persistence for application settings."""

from __future__ import annotations

import sqlite3
from collections.abc import Callable, Mapping


class SqliteSettingsRepository:
    def __init__(self, connect: Callable[[], sqlite3.Connection]) -> None:
        self._connect = connect

    def save(self, values: Mapping[str, str]) -> None:
        with self._connect() as connection:
            connection.executemany(
                "INSERT INTO settings(key, value) VALUES (?, ?) ON CONFLICT(key) DO UPDATE SET value = excluded.value",
                values.items(),
            )
