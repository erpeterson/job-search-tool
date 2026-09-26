"""Search-query use cases independent of HTTP and SQLite."""

from __future__ import annotations

from collections.abc import Mapping
from typing import Any

from job_search.application.contracts import SearchQueryRepository


class SearchQueryService:
    def __init__(self, repository: SearchQueryRepository) -> None:
        self._repository = repository

    def create(self, values: Mapping[str, Any]) -> int:
        return self._repository.create(values)

    def update(self, query_id: int, values: Mapping[str, Any]) -> bool:
        return self._repository.update(query_id, values)
