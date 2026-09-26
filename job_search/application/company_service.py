"""Company-interest use cases independent of HTTP and SQLite."""

from __future__ import annotations

from collections.abc import Mapping
from typing import Any, Protocol


class CompanyRepository(Protocol):
    def upsert(self, values: Mapping[str, Any]) -> int: ...

    def update(self, company_id: int, values: Mapping[str, Any]) -> bool: ...


class CompanyService:
    def __init__(self, repository: CompanyRepository) -> None:
        self._repository = repository

    def save(self, values: Mapping[str, Any]) -> int:
        return self._repository.upsert(values)

    def update(self, company_id: int, values: Mapping[str, Any]) -> bool:
        return self._repository.update(company_id, values)
