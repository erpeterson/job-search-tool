"""SQLite persistence for saved job-board search queries."""

from __future__ import annotations

import sqlite3
from collections.abc import Callable, Mapping
from typing import Any


class SqliteSearchQueryRepository:
    def __init__(self, connect: Callable[[], sqlite3.Connection]) -> None:
        self._connect = connect

    def create(self, values: Mapping[str, Any]) -> int:
        with self._connect() as connection:
            cursor = connection.execute(
                """INSERT INTO search_queries(
                    board, pipeline, keywords, location, enabled, created_at, criteria, seeded
                ) VALUES (:board, :pipeline, :keywords, :location, :enabled, :created_at, :criteria, 0)""",
                values,
            )
        return int(cursor.lastrowid)

    def update(self, query_id: int, values: Mapping[str, Any]) -> bool:
        with self._connect() as connection:
            updated = connection.execute(
                """UPDATE search_queries
                SET board = COALESCE(:board, board), pipeline = COALESCE(:pipeline, pipeline),
                    keywords = COALESCE(:keywords, keywords), location = COALESCE(:location, location),
                    criteria = COALESCE(:criteria, criteria), enabled = COALESCE(:enabled, enabled)
                WHERE id = :id""",
                {**values, "id": query_id},
            ).rowcount
        return bool(updated)
