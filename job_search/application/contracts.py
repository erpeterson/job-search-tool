"""Ports used by application services.

The application layer depends on these small contracts rather than Flask,
SQLite, Requests, or subprocess APIs. Concrete implementations belong in
``job_search.data_access`` and are wired at startup.
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from typing import Any, Protocol


class JobRepository(Protocol):
    def get_job(self, job_id: int) -> Mapping[str, Any] | None: ...

    def get_job_by_url(self, url: str) -> Mapping[str, Any] | None: ...

    def list_jobs(self, *, include_filtered: bool = False) -> Sequence[Mapping[str, Any]]: ...

    def update_status(self, job_id: int, status: str, updated_at: int) -> bool: ...

    def add_note(self, job_id: int, note: str, created_at: int) -> bool: ...

    def add_interaction(self, job_id: int, values: Mapping[str, Any], created_at: int) -> bool: ...

    def attach_packet(self, job_id: int, path: str, updated_at: int) -> bool: ...

    def delete_job(self, job_id: int) -> bool: ...

    def save_user_score(
        self, job_id: int, total: int, scorecard_json: str, rationale: str, updated_at: int
    ) -> bool: ...

    def rescrape_job(
        self, job_id: int, current: Mapping[str, Any], scraped: Mapping[str, Any], notes: str, updated_at: int
    ) -> bool: ...

    def purge_jobs(self) -> int: ...

    def create_job(self, values: Mapping[str, Any]) -> int | None: ...


class JobBoardGateway(Protocol):
    def fetch(
        self, board: str, keywords: str, location: str, *, force_refresh: bool = False
    ) -> Sequence[Mapping[str, Any]]: ...


class ModelGateway(Protocol):
    def complete_json(self, operation: str, prompt: Mapping[str, Any], *, force_refresh: bool = False) -> str: ...


class Clock(Protocol):
    def now(self) -> int: ...


class Telemetry(Protocol):
    """Framework-independent event sink for application workflows."""

    def event(self, event_type: str, **fields: Any) -> None: ...

    def api_call(
        self,
        service: str,
        method: str,
        url: str,
        response: Any = None,
        error: Exception | None = None,
        elapsed_ms: int | None = None,
    ) -> None: ...


class SearchQueryRepository(Protocol):
    def create(self, values: Mapping[str, Any]) -> int: ...

    def update(self, query_id: int, values: Mapping[str, Any]) -> bool: ...


class SettingsRepository(Protocol):
    def save(self, values: Mapping[str, str]) -> None: ...


class JobFilterRepository(Protocol):
    def settings(self) -> Mapping[str, str]: ...

    def job_for_filtering(self, job_id: int) -> Mapping[str, Any] | None: ...

    def all_job_ids(self) -> Sequence[int]: ...

    def save_filter_decision(self, job_id: int, filtered: bool, updated_at: int) -> None: ...
