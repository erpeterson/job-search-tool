"""Search-query use cases independent of HTTP and SQLite."""

from __future__ import annotations

from collections.abc import Callable, Mapping
from typing import Any

from job_search.application.contracts import SearchQueryRepository


class SearchQueryService:
    def __init__(self, repository: SearchQueryRepository, now: Callable[[], int]) -> None:
        self._repository = repository
        self._now = now

    def create(self, values: Mapping[str, Any]) -> int:
        return self._repository.create(
            {**values, "enabled": int(bool(values.get("enabled", True))), "created_at": self._now()}
        )

    def update(self, query_id: int, values: Mapping[str, Any]) -> bool:
        record = dict(values)
        if record.get("enabled") is not None:
            record["enabled"] = int(bool(record["enabled"]))
        return self._repository.update(query_id, record)
