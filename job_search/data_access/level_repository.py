"""SQLite persistence for level-equivalency calibrations."""

from __future__ import annotations

import sqlite3
from collections.abc import Callable, Mapping
from typing import Any


class SqliteLevelRepository:
    """Store and match level calibrations without policy decisions."""

    def __init__(self, connect: Callable[[], sqlite3.Connection], connection: sqlite3.Connection | None = None) -> None:
        self._connect = connect
        self._connection = connection

    def find(self, normalized_company: str, normalized_title: str) -> Mapping[str, Any] | None:
        if self._connection is not None:
            return self._find(self._connection, normalized_company, normalized_title)
        with self._connect() as connection:
            return self._find(connection, normalized_company, normalized_title)

    @staticmethod
    def _find(
        connection: sqlite3.Connection, normalized_company: str, normalized_title: str
    ) -> Mapping[str, Any] | None:
        rows = [
            dict(row)
            for row in connection.execute(
                """SELECT * FROM level_equivalencies WHERE normalized_company = ?
                ORDER BY LENGTH(normalized_title_pattern) DESC""",
                (normalized_company,),
            )
        ]
        for row in rows:
            pattern = row["normalized_title_pattern"]
            if pattern and (normalized_title == pattern or normalized_title.startswith(f"{pattern} ")):
                return row
        return None

    def save(self, values: Mapping[str, Any]) -> None:
        if self._connection is not None:
            self._save(self._connection, values)
            return
        with self._connect() as connection:
            self._save(connection, values)

    @staticmethod
    def _save(connection: sqlite3.Connection, values: Mapping[str, Any]) -> None:
        connection.execute(
            """INSERT INTO level_equivalencies(
                    company, normalized_company, title_pattern, normalized_title_pattern,
                    source_level, source_level_title, oracle_level, oracle_title, downlevel,
                    source_url, notes, created_at, updated_at
                ) VALUES (
                    :company, :normalized_company, :title_pattern, :normalized_title_pattern,
                    :source_level, :source_level_title, :oracle_level, :oracle_title, :downlevel,
                    :source_url, :notes, :created_at, :updated_at
                ) ON CONFLICT(normalized_company, normalized_title_pattern) DO UPDATE SET
                    company = excluded.company, title_pattern = excluded.title_pattern,
                    source_level = excluded.source_level, source_level_title = excluded.source_level_title,
                    oracle_level = excluded.oracle_level, oracle_title = excluded.oracle_title,
                    downlevel = excluded.downlevel, source_url = excluded.source_url, notes = excluded.notes,
                    updated_at = excluded.updated_at""",
            values,
        )
