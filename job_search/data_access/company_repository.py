"""SQLite company-interest commands."""

from __future__ import annotations

import sqlite3
from collections.abc import Callable, Mapping
from typing import Any


class SqliteCompanyRepository:
    def __init__(self, connect: Callable[[], sqlite3.Connection]) -> None:
        self._connect = connect

    def upsert(self, values: Mapping[str, Any]) -> int:
        with self._connect() as connection:
            row = connection.execute(
                """INSERT INTO company_interests(
                    created_at, updated_at, company, normalized_company, status, interest_score, rationale, notes, next_step, contacts
                ) VALUES (:created_at, :updated_at, :company, :normalized_company, :status, :interest_score, :rationale, :notes, :next_step, :contacts)
                ON CONFLICT(normalized_company) DO UPDATE SET company = excluded.company, status = excluded.status,
                interest_score = excluded.interest_score, rationale = excluded.rationale, notes = excluded.notes,
                next_step = excluded.next_step, contacts = excluded.contacts, updated_at = excluded.updated_at RETURNING id""",
                values,
            ).fetchone()
        return int(row["id"])

    def update(self, company_id: int, values: Mapping[str, Any]) -> bool:
        with self._connect() as connection:
            updated = connection.execute(
                """UPDATE company_interests SET company = :company, normalized_company = :normalized_company, status = :status,
                interest_score = :interest_score, rationale = :rationale, notes = :notes, next_step = :next_step,
                contacts = :contacts, updated_at = :updated_at WHERE id = :id""",
                {**values, "id": company_id},
            ).rowcount
        return bool(updated)
