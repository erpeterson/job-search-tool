"""On-demand score workflow independent of transport and storage implementations."""

from __future__ import annotations

from collections.abc import Callable, Mapping
from dataclasses import dataclass
from typing import Any


@dataclass(frozen=True)
class ScoreResult:
    state: str
    job: Mapping[str, Any] | None
    raw_score: Mapping[str, Any] | None
    unavailable_reason: str | None = None


class ScoringService:
    def __init__(
        self,
        get_job: Callable[[int], Mapping[str, Any] | None],
        score: Callable[[int], Mapping[str, Any]],
        availability: Callable[[], str | None],
    ) -> None:
        self._get_job = get_job
        self._score = score
        self._availability = availability

    def score(self, job_id: int) -> ScoreResult:
        job = self._get_job(job_id)
        if job is None:
            return ScoreResult("missing", None, None)
        unavailable_reason = self._availability()
        if unavailable_reason:
            return ScoreResult("unavailable", job, None, unavailable_reason)
        raw_score = self._score(job_id)
        return ScoreResult("scored", self._get_job(job_id), raw_score)
