"""Application dispatch for durable background task items."""

from __future__ import annotations

from collections.abc import Mapping
from typing import Any, Protocol


class TaskExecutionOperations(Protocol):
    def job(self, job_id: int) -> Mapping[str, Any] | None: ...

    def score(self, job_id: int) -> Mapping[str, Any]: ...

    def generate_packet(self, job_id: int) -> Mapping[str, Any]: ...


class TaskExecutionService:
    def __init__(self, operations: TaskExecutionOperations) -> None:
        self._operations = operations

    def process(self, claim: Mapping[str, Any]) -> tuple[str, str]:
        operation, job_id = claim["operation"], int(claim["job_id"])
        job = self._operations.job(job_id)
        if not job:
            return "skipped", "Job not found"
        if operation == "scorecards":
            score = self._operations.score(job_id)
            return "complete", f"Codex score {int(score.get('total_score', 0))}"
        if operation == "application_packets":
            if job.get("application_packet_path"):
                return "skipped", "Application packet already associated"
            packet = self._operations.generate_packet(job_id)
            return "complete", packet.get("path") or "Application packet generated"
        return "error", f"Unsupported task operation: {operation}"
