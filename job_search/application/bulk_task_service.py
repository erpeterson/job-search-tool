"""Bulk task progress and outcome decisions independent of HTTP delivery."""

from __future__ import annotations

from collections.abc import Callable, Mapping, Sequence
from typing import Any, Protocol

from job_search.application.contracts import Telemetry


class TaskProgress(Protocol):
    def update(self, task_id: str, **updates: Any) -> Any: ...

    def update_item(self, task_id: str, job_id: int, **updates: Any) -> Any: ...


class TaskProcessor(Protocol):
    def process(self, claim: Mapping[str, Any]) -> tuple[str, str]: ...


_OPERATIONS = {
    "scorecards": ("Scoring with Codex", "scored", "BULK_CODEX_SCORE_FAILED", "business.bulk_scoring"),
    "application_packets": (
        "Generating application packet with Codex",
        "generated",
        "BULK_APPLICATION_PACKET_FAILED",
        "business.bulk_packets",
    ),
}


class BulkTaskService:
    def __init__(
        self, progress: TaskProgress, processor: TaskProcessor, now: Callable[[], int], telemetry: Telemetry
    ) -> None:
        self._progress = progress
        self._processor = processor
        self._now = now
        self._telemetry = telemetry

    def run(self, task_id: str, job_ids: Sequence[int], operation: str) -> tuple[str, int, int, int]:
        try:
            running_message, success_verb, error_code, component = _OPERATIONS[operation]
        except KeyError as exc:
            raise ValueError(f"Unsupported bulk task operation: {operation}") from exc
        self._progress.update(task_id, status="running", started_at=self._now(), message=running_message)
        completed = failed = skipped = 0
        for job_id in job_ids:
            self._progress.update(task_id, current_job_id=job_id)
            self._progress.update_item(task_id, job_id, status="running", message=running_message)
            try:
                status, message = self._processor.process({"operation": operation, "job_id": job_id})
                if status == "complete":
                    completed += 1
                elif status == "skipped":
                    skipped += 1
                else:
                    failed += 1
                self._progress.update_item(task_id, job_id, status=status, message=message)
            except Exception as exc:
                failed += 1
                safe_message = f"{type(exc).__name__} during {operation}"
                self._progress.update_item(task_id, job_id, status="error", message=safe_message)
                self._telemetry.event(
                    error_code.lower(),
                    error_code=error_code,
                    component=component,
                    operation=operation,
                    task_id=task_id,
                    job_id=job_id,
                    error_type=type(exc).__name__,
                    cause=type(exc).__name__,
                    message=safe_message,
                )
            finally:
                self._progress.update(task_id, completed=completed, failed=failed, skipped=skipped)
        status = "complete" if failed == 0 else "error"
        message = f"Complete: {completed} {success_verb}, {skipped} skipped, {failed} failed."
        self._progress.update(task_id, status=status, completed_at=self._now(), current_job_id=None, message=message)
        self._telemetry.event(
            "background_task_finished", task_id=task_id, operation=operation, status=status, message=message
        )
        return status, completed, skipped, failed
