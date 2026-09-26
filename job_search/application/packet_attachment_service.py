"""Application-packet attachment workflow with injected storage and repository ports."""

from __future__ import annotations

from collections.abc import Callable, Mapping

from job_search.application.contracts import JobRepository


class PacketAttachmentService:
    def __init__(
        self,
        repository: JobRepository,
        resolve_packet_path: Callable[[str], str],
        clock: Callable[[], int],
    ) -> None:
        self._repository = repository
        self._resolve_packet_path = resolve_packet_path
        self._clock = clock

    def attach(self, job_id: int, path: str) -> Mapping[str, object] | None:
        job = self._repository.get_job(job_id)
        if job is None:
            return None
        relative_path = self._resolve_packet_path(path)
        self._repository.attach_packet(job_id, relative_path, self._clock())
        return {"job": self._repository.get_job(job_id), "path": relative_path}
