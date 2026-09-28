"""Application startup use case independent of web delivery and storage APIs."""

from __future__ import annotations

from collections.abc import Mapping
from typing import Protocol

from job_search.application.contracts import Telemetry


class DatabaseInitializer(Protocol):
    def initialize_database(self, defaults: Mapping[str, str]) -> None: ...


class TaskRecovery(Protocol):
    def initialize(self) -> int: ...


class StartupService:
    def __init__(
        self, database: DatabaseInitializer, tasks: TaskRecovery, telemetry: Telemetry, default_model: str
    ) -> None:
        self._database = database
        self._tasks = tasks
        self._telemetry = telemetry
        self._default_model = default_model

    def initialize(self) -> int:
        self._telemetry.event("startup_started", component="application.startup", operation="initialize")
        self._database.initialize_database(
            {"gpt_threshold": "40", "user_threshold": "60", "codex_model": self._default_model, "last_search_at": "0"}
        )
        recovered = self._tasks.initialize()
        if recovered:
            self._telemetry.event(
                "background_tasks_recovered",
                error_code="BACKGROUND_TASKS_RECOVERED",
                component="application.startup",
                operation="initialize",
                recovered_count=recovered,
            )
        self._telemetry.event(
            "startup_succeeded", component="application.startup", operation="initialize", recovered_count=recovered
        )
        return recovered
