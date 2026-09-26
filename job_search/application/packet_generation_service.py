"""Application workflow for creating and associating job application packets."""

from __future__ import annotations

from collections.abc import Mapping
from contextlib import AbstractContextManager
from typing import Any, Protocol


class PacketGenerationOperations(Protocol):
    def connection(self) -> AbstractContextManager[Any]: ...

    def job(self, connection: Any, job_id: int) -> Mapping[str, Any] | None: ...

    def generate(self, job: Mapping[str, Any]) -> Mapping[str, Any]: ...

    def save_path(self, connection: Any, job_id: int, path: str, timestamp: int) -> None: ...

    def now(self) -> int: ...

    def log(self, event: str, **fields: Any) -> None: ...


class PacketGenerationService:
    def __init__(self, operations: PacketGenerationOperations) -> None:
        self._operations = operations

    def generate(self, job_id: int) -> Mapping[str, Any]:
        with self._operations.connection() as connection:
            job = self._operations.job(connection, job_id)
            if not job:
                raise ValueError("Job not found.")
            if job.get("application_packet_path"):
                raise FileExistsError("This job already has an associated application packet.")
            result = self._operations.generate(job)
            self._operations.save_path(connection, job_id, result["path"], self._operations.now())
        self._operations.log("application_packet_generated", job_id=job_id, path=result["path"], generator="codex_cli")
        return result
