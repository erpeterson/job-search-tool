"""Settings use cases independent of HTTP and SQLite."""

from __future__ import annotations

from collections.abc import Mapping

from job_search.application.contracts import SettingsRepository


class SettingsService:
    def __init__(self, repository: SettingsRepository) -> None:
        self._repository = repository

    def save(self, values: Mapping[str, str]) -> None:
        self._repository.save(values)
