"""Availability policy for queuing managed scoring and packet tasks."""

from __future__ import annotations

from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass
from typing import Any, Protocol


class TaskStarter(Protocol):
    def start(self, operation: str, job_ids: Sequence[int]) -> Mapping[str, Any]: ...


@dataclass(frozen=True)
class TaskSubmissionResult:
    task: Mapping[str, Any] | None
    unavailable_reason: str | None


class TaskSubmissionService:
    def __init__(
        self,
        tasks: TaskStarter,
        scoring_enabled: Callable[[], bool],
        cli_available: Callable[[], bool],
        cli_path: Callable[[], str],
    ) -> None:
        self._tasks = tasks
        self._scoring_enabled = scoring_enabled
        self._cli_available = cli_available
        self._cli_path = cli_path

    def submit(self, operation: str, job_ids: Sequence[int]) -> TaskSubmissionResult:
        if operation not in {"scorecards", "application_packets"}:
            raise ValueError(f"Unsupported bulk task operation: {operation}")
        if operation == "scorecards" and not self._scoring_enabled():
            return TaskSubmissionResult(
                None, "Codex scoring is currently disabled. Set JOB_SEARCH_ENABLE_GPT_SCORING=1 to re-enable it."
            )
        if not self._cli_available():
            suffix = " before scoring" if operation == "scorecards" else ""
            return TaskSubmissionResult(
                None,
                f"Codex CLI is unavailable at {self._cli_path()!r}. Set CODEX_CLI_PATH or install Codex CLI{suffix}.",
            )
        return TaskSubmissionResult(self._tasks.start(operation, job_ids), None)
