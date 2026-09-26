"""SQLite read adapter for the console query use cases."""

from __future__ import annotations

from collections.abc import Callable, Mapping, Sequence
from typing import Any

from job_search.data_access.read_models import SqliteReadModels


class SqliteConsoleQueryRepository:
    def __init__(self, connect: Callable[[], Any], packets: Callable[[Any], Sequence[Mapping[str, Any]]]) -> None:
        self._connect = connect
        self._packets = packets

    def jobs(self, *, include_filtered: bool) -> Sequence[Mapping[str, Any]]:
        with self._connect() as connection:
            return SqliteReadModels.jobs(connection, include_filtered)

    def job(self, job_id: int) -> Mapping[str, Any] | None:
        with self._connect() as connection:
            return SqliteReadModels.job(connection, job_id)

    def company_interests(self) -> Sequence[Mapping[str, Any]]:
        with self._connect() as connection:
            return SqliteReadModels.company_interests(connection)

    def company_interest(self, company_id: int) -> Mapping[str, Any] | None:
        with self._connect() as connection:
            return SqliteReadModels.company_interest(connection, company_id)

    def queries(self) -> Sequence[Mapping[str, Any]]:
        with self._connect() as connection:
            return SqliteReadModels.search_queries(connection)

    def runs(self) -> Sequence[Mapping[str, Any]]:
        with self._connect() as connection:
            return SqliteReadModels.search_runs(connection)

    def discoveries(self, limit: int) -> Sequence[Mapping[str, Any]]:
        with self._connect() as connection:
            return SqliteReadModels.discoveries(connection, limit)

    def packets(self) -> Sequence[Mapping[str, Any]]:
        with self._connect() as connection:
            return self._packets(connection)

    def settings(self) -> Mapping[str, str]:
        with self._connect() as connection:
            return SqliteReadModels.settings(connection)
