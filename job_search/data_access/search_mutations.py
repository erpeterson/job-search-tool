"""Persistence commands for discovered jobs and Codex scoring."""

from __future__ import annotations

import sqlite3
from collections.abc import Mapping
from typing import Any


class SqliteSearchMutations:
    @staticmethod
    def seed_queries(connection: sqlite3.Connection, queries: list[Mapping[str, Any]], timestamp: int) -> None:
        for query in queries:
            existing = connection.execute(
                "SELECT id, keywords, criteria FROM search_queries WHERE board = ? AND pipeline = ? AND seeded = 1 LIMIT 1",
                (query["board"], query["pipeline"]),
            ).fetchone()
            if existing:
                connection.execute(
                    "UPDATE search_queries SET criteria = ?, keywords = ?, location = COALESCE(location, ?) WHERE id = ?",
                    (query["criteria"], query["keywords"], query["location"], existing["id"]),
                )
            else:
                connection.execute(
                    """INSERT INTO search_queries(board, pipeline, keywords, location, enabled, created_at, criteria, seeded)
                    VALUES (?, ?, ?, ?, 1, ?, ?, 1)""",
                    (
                        query["board"],
                        query["pipeline"],
                        query["keywords"],
                        query["location"],
                        timestamp,
                        query["criteria"],
                    ),
                )

    @staticmethod
    def remove_legacy_level_equivalency_seeds(connection: sqlite3.Connection) -> None:
        connection.execute(
            """DELETE FROM level_equivalencies WHERE normalized_company = 'atlassian'
            AND normalized_title_pattern = 'principal engineer' AND notes LIKE '%user-provided equivalency%'"""
        )

    @staticmethod
    def save_application_packet_path(connection: sqlite3.Connection, job_id: int, path: str, timestamp: int) -> None:
        connection.execute(
            "UPDATE jobs SET application_packet_path = ?, updated_at = ? WHERE id = ?", (path, timestamp, job_id)
        )

    @staticmethod
    def create_discovery_job(connection: sqlite3.Connection, values: Mapping[str, Any]) -> int:
        cursor = connection.execute(
            """INSERT INTO jobs(created_at, updated_at, company, title, url, location, pipeline, status,
            posting_text, notes, gpt_score, gpt_rationale, gpt_scorecard_json, filtered, source_board,
            source_job_id, discovered_at, level_assessment, downlevel)
            VALUES (:created_at, :updated_at, :company, :title, :url, :location, :pipeline, 'discovered',
            :posting_text, :notes, :gpt_score, :gpt_rationale, :gpt_scorecard_json, 0, :source_board,
            :source_job_id, :discovered_at, :level_assessment, :downlevel)""",
            values,
        )
        return int(cursor.lastrowid)

    @staticmethod
    def update_query(connection: sqlite3.Connection, query_id: int, values: Mapping[str, str]) -> None:
        connection.execute(
            "UPDATE search_queries SET keywords = ?, location = ?, criteria = ?, refinement_notes = ? WHERE id = ?",
            (values["keywords"], values["location"], values["criteria"], values["refinement_notes"], query_id),
        )

    @staticmethod
    def save_codex_score(connection: sqlite3.Connection, job_id: int, values: Mapping[str, Any]) -> None:
        connection.execute(
            """UPDATE jobs SET gpt_score = :total, gpt_rationale = :rationale, gpt_scorecard_json = :scorecard,
            pipeline = COALESCE(NULLIF(:pipeline, ''), pipeline), level_assessment = :level_assessment,
            downlevel = :downlevel, updated_at = :updated_at WHERE id = :job_id""",
            {**values, "job_id": job_id},
        )
