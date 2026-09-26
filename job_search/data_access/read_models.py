"""SQLite read models for presentation-facing aggregate views."""

from __future__ import annotations

import sqlite3
from collections.abc import Mapping, Sequence
from typing import Any


class SqliteReadModels:
    @staticmethod
    def job_for_filter(connection: sqlite3.Connection, job_id: int) -> Mapping[str, Any] | None:
        row = connection.execute(
            "SELECT company, title, gpt_score, user_score, downlevel FROM jobs WHERE id = ?", (job_id,)
        ).fetchone()
        return dict(row) if row else None

    @staticmethod
    def save_filter(connection: sqlite3.Connection, job_id: int, filtered: bool, timestamp: int) -> None:
        connection.execute(
            "UPDATE jobs SET filtered = ?, updated_at = ? WHERE id = ?", (int(filtered), timestamp, job_id)
        )

    @staticmethod
    def settings(connection: sqlite3.Connection) -> Mapping[str, str]:
        return {row["key"]: row["value"] for row in connection.execute("SELECT key, value FROM settings")}

    @staticmethod
    def initialize_defaults(connection: sqlite3.Connection, defaults: Mapping[str, str]) -> None:
        for key, value in defaults.items():
            connection.execute("INSERT OR IGNORE INTO settings(key, value) VALUES (?, ?)", (key, value))

    @staticmethod
    def disable_legacy_seed_queries(connection: sqlite3.Connection) -> None:
        connection.execute(
            """UPDATE search_queries SET enabled = 0 WHERE seeded = 0 AND pipeline IS NULL AND criteria IS NULL
            AND keywords IN ('Chief Architect', 'Distinguished Engineer', 'Principal Architect', 'Office of the CTO',
            'Engineering Strategy', 'Developer Experience Principal Engineer')"""
        )

    @staticmethod
    def company_interests(connection: sqlite3.Connection) -> Sequence[Mapping[str, Any]]:
        return [
            dict(row)
            for row in connection.execute(
                """SELECT ci.*, COUNT(j.id) AS tracked_job_count, MAX(j.updated_at) AS latest_job_updated_at
                FROM company_interests ci LEFT JOIN jobs j ON lower(j.company) = lower(ci.company)
                GROUP BY ci.id ORDER BY CASE ci.status WHEN 'target' THEN 0 WHEN 'watching' THEN 1
                WHEN 'active_conversation' THEN 2 WHEN 'paused' THEN 3 WHEN 'not_interested' THEN 4 ELSE 5 END,
                COALESCE(ci.interest_score, -1) DESC, ci.updated_at DESC"""
            )
        ]

    @staticmethod
    def company_interest(connection: sqlite3.Connection, company_id: int) -> Mapping[str, Any] | None:
        row = connection.execute("SELECT * FROM company_interests WHERE id = ?", (company_id,)).fetchone()
        if not row:
            return None
        result = dict(row)
        result["jobs"] = [
            dict(job)
            for job in connection.execute(
                """SELECT id, company, title, url, location, pipeline, status, gpt_score, user_score, filtered, downlevel
                FROM jobs WHERE lower(company) = lower(?) ORDER BY updated_at DESC""",
                (result["company"],),
            )
        ]
        return result

    @staticmethod
    def jobs(connection: sqlite3.Connection, include_filtered: bool) -> Sequence[Mapping[str, Any]]:
        query = "SELECT * FROM jobs" + ("" if include_filtered else " WHERE filtered = 0")
        return [dict(row) for row in connection.execute(f"{query} ORDER BY updated_at DESC, created_at DESC")]

    @staticmethod
    def job(connection: sqlite3.Connection, job_id: int) -> Mapping[str, Any] | None:
        row = connection.execute("SELECT * FROM jobs WHERE id = ?", (job_id,)).fetchone()
        if not row:
            return None
        result = dict(row)
        result["interactions"] = [
            dict(item)
            for item in connection.execute(
                "SELECT * FROM interactions WHERE job_id = ? ORDER BY occurred_on DESC, id DESC", (job_id,)
            )
        ]
        result["notes_list"] = [
            dict(item)
            for item in connection.execute(
                "SELECT * FROM notes WHERE job_id = ? ORDER BY created_at DESC, id DESC", (job_id,)
            )
        ]
        return result

    @staticmethod
    def search_queries(connection: sqlite3.Connection) -> Sequence[Mapping[str, Any]]:
        return [
            dict(row)
            for row in connection.execute(
                "SELECT * FROM search_queries ORDER BY enabled DESC, seeded DESC, pipeline, board, keywords, location"
            )
        ]

    @staticmethod
    def search_runs(connection: sqlite3.Connection) -> Sequence[Mapping[str, Any]]:
        return [dict(row) for row in connection.execute("SELECT * FROM search_runs ORDER BY started_at DESC LIMIT 20")]

    @staticmethod
    def discoveries(connection: sqlite3.Connection, limit: int) -> Sequence[Mapping[str, Any]]:
        return [
            dict(row)
            for row in connection.execute(
                "SELECT * FROM discovered_jobs ORDER BY created_at DESC, id DESC LIMIT ?", (limit,)
            )
        ]

    @staticmethod
    def job_exists_url(connection: sqlite3.Connection, url: str) -> bool:
        return connection.execute("SELECT 1 FROM jobs WHERE url = ? LIMIT 1", (url,)).fetchone() is not None

    @staticmethod
    def query_refinement_context(
        connection: sqlite3.Connection, query_id: int
    ) -> tuple[Mapping[str, Any] | None, Sequence[Mapping[str, Any]]]:
        query = connection.execute("SELECT * FROM search_queries WHERE id = ?", (query_id,)).fetchone()
        if not query:
            return None, []
        discoveries = connection.execute(
            """SELECT company, title, location, gpt_score, level_assessment, downlevel, decision,
            rejection_reason, gpt_rationale FROM discovered_jobs WHERE query_id = ?
            ORDER BY created_at DESC LIMIT 20""",
            (query_id,),
        ).fetchall()
        return dict(query), [dict(discovery) for discovery in discoveries]

    @staticmethod
    def calibration_examples(connection: sqlite3.Connection) -> Sequence[Mapping[str, Any]]:
        return [
            dict(row)
            for row in connection.execute(
                """SELECT company, title, pipeline, gpt_score, user_score, user_rationale, posting_text
                FROM jobs WHERE user_score IS NOT NULL ORDER BY updated_at DESC LIMIT 8"""
            )
        ]

    @staticmethod
    def application_packet_jobs(connection: sqlite3.Connection) -> Sequence[Mapping[str, Any]]:
        return [
            dict(row)
            for row in connection.execute(
                """SELECT id, company, title, application_packet_path FROM jobs
                WHERE application_packet_path IS NOT NULL AND application_packet_path != ''"""
            )
        ]
