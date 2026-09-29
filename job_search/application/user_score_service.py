"""Persist a human scorecard and refresh the derived filtering decision."""

from __future__ import annotations

import json
from collections.abc import Callable, Mapping
from typing import Protocol

from job_search.application.job_scoring_policy import RUBRIC_FIELDS


class UserScoreJobs(Protocol):
    def save_user_score(
        self, job_id: int, total: int, scorecard_json: str, rationale: str, updated_at: int
    ) -> bool: ...


class UserScoreService:
    def __init__(
        self,
        jobs: UserScoreJobs,
        refresh_filter: Callable[[int], object],
        now: Callable[[], int],
    ) -> None:
        self._jobs = jobs
        self._refresh_filter = refresh_filter
        self._now = now

    def save(self, job_id: int, scorecard: Mapping[str, int], total: int | None, rationale: str) -> bool:
        actual_total = (
            round(sum(scorecard[field] for field in RUBRIC_FIELDS) * 100 / (len(RUBRIC_FIELDS) * 10))
            if total is None
            else total
        )
        saved = self._jobs.save_user_score(job_id, actual_total, json.dumps(dict(scorecard)), rationale, self._now())
        if saved:
            self._refresh_filter(job_id)
        return saved
