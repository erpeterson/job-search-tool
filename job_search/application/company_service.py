"""Company-interest use cases independent of HTTP and SQLite."""

from __future__ import annotations

from collections.abc import Callable, Mapping
from typing import Any, Protocol

from job_search.application.level_service import normalize_lookup_text


class CompanyRepository(Protocol):
    def upsert(self, values: Mapping[str, Any]) -> int: ...

    def update(self, company_id: int, values: Mapping[str, Any]) -> bool: ...


class CompanyService:
    def __init__(self, repository: CompanyRepository, now: Callable[[], int]) -> None:
        self._repository = repository
        self._now = now

    def save(self, values: Mapping[str, Any]) -> int:
        timestamp = self._now()
        return self._repository.upsert(
            {
                **values,
                "normalized_company": normalize_lookup_text(values["company"]),
                "created_at": timestamp,
                "updated_at": timestamp,
            }
        )

    def update(self, company_id: int, values: Mapping[str, Any]) -> bool:
        return self._repository.update(
            company_id,
            {
                **values,
                "normalized_company": normalize_lookup_text(values["company"]),
                "updated_at": self._now(),
            },
        )
