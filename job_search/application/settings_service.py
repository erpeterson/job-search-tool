"""Settings use cases independent of HTTP and SQLite."""

from __future__ import annotations

from collections.abc import Callable, Mapping

from job_search.application.contracts import SettingsRepository


class SettingsService:
    def __init__(self, repository: SettingsRepository, refresh_filter: Callable[[], object]) -> None:
        self._repository = repository
        self._refresh_filter = refresh_filter

    def save(self, values: Mapping[str, str]) -> None:
        self._repository.save(values)

    def save_and_refresh(self, values: Mapping[str, str]) -> None:
        self._repository.save(values)
        self._refresh_filter()
