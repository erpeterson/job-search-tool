"""Job read use cases independent of Flask and SQLite."""

from __future__ import annotations

import json
from collections.abc import Callable, Mapping, Sequence
from typing import Any

from job_search.application.contracts import JobRepository


class JobService:
    def __init__(
        self,
        repository: JobRepository,
        observe: Callable[..., None] | None,
        now: Callable[[], int],
    ) -> None:
        self._repository = repository
        self._observe = observe or (lambda **_fields: None)
        self._now = now

    def list_jobs(self, *, include_filtered: bool = False) -> Sequence[Mapping[str, Any]]:
        return [self._present(job) for job in self._repository.list_jobs(include_filtered=include_filtered)]

    def get_job(self, job_id: int) -> Mapping[str, Any] | None:
        job = self._repository.get_job(job_id)
        return self._present(job) if job else None

    def get_job_by_url(self, url: str) -> Mapping[str, Any] | None:
        job = self._repository.get_job_by_url(url)
        return self._present(job) if job else None

    def update_status(self, job_id: int, status: str) -> bool:
        return self._repository.update_status(job_id, status, self._now())

    def add_note(self, job_id: int, note: str) -> bool:
        return self._repository.add_note(job_id, note, self._now())

    def add_interaction(self, job_id: int, values: Mapping[str, Any]) -> bool:
        return self._repository.add_interaction(job_id, values, self._now())

    def attach_packet(self, job_id: int, path: str, updated_at: int) -> bool:
        return self._repository.attach_packet(job_id, path, updated_at)

    def delete_job(self, job_id: int) -> Mapping[str, Any] | None:
        job = self._repository.get_job(job_id)
        if not job or not self._repository.delete_job(job_id):
            return None
        self._observe(
            event="manual_job_deleted",
            job_id=job_id,
            company=job["company"],
            title=job["title"],
            url=job["url"],
        )
        return job

    def save_user_score(self, job_id: int, total: int, scorecard_json: str, rationale: str, updated_at: int) -> bool:
        return self._repository.save_user_score(job_id, total, scorecard_json, rationale, updated_at)

    def rescrape_job(
        self, job_id: int, current: Mapping[str, Any], scraped: Mapping[str, Any], notes: str, updated_at: int
    ) -> bool:
        return self._repository.rescrape_job(job_id, current, scraped, notes, updated_at)

    def purge_jobs(self) -> int:
        deleted = self._repository.purge_jobs()
        self._observe(event="admin_purge_jobs", deleted_jobs=deleted)
        return deleted

    def create_job(self, values: Mapping[str, Any]) -> int | None:
        return self._repository.create_job(values)

    def _present(self, job: Mapping[str, Any]) -> dict[str, Any]:
        result = dict(job)
        for field in ("gpt_scorecard_json", "user_scorecard_json"):
            raw = result.pop(field, "")
            try:
                result[field.removesuffix("_json")] = json.loads(raw) if raw else {}
            except json.JSONDecodeError as exc:
                self._observe(
                    event="job_scorecard_parse_recovered",
                    error_code="JOB_SCORECARD_PARSE_RECOVERED",
                    component="application.job_service",
                    operation="present_scorecard",
                    record_id=result.get("id"),
                    field=field,
                    cause=type(exc).__name__,
                )
                result[field.removesuffix("_json")] = {}
        return result
