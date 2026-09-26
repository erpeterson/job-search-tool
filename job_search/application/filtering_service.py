"""Job visibility policy orchestration without web or SQLite dependencies."""

from __future__ import annotations

from collections.abc import Callable, Sequence

from job_search.application.contracts import JobFilterRepository
from job_search.domain.filtering import FilterDecision, decide_job_filter


class FilteringService:
    def __init__(
        self,
        repository: JobFilterRepository,
        clock: Callable[[], int],
        *,
        gpt_scoring_enabled: bool,
    ) -> None:
        self._repository = repository
        self._clock = clock
        self._gpt_scoring_enabled = gpt_scoring_enabled

    def refresh_job(self, job_id: int) -> FilterDecision | None:
        job = self._repository.job_for_filtering(job_id)
        if job is None:
            return None
        configuration = self._repository.settings()
        decision = decide_job_filter(
            job,
            gpt_threshold=int(configuration.get("gpt_threshold", "40")),
            user_threshold=int(configuration.get("user_threshold", "60")),
            gpt_scoring_enabled=self._gpt_scoring_enabled,
        )
        self._repository.save_filter_decision(job_id, decision.filtered, self._clock())
        return decision

    def refresh_all(self) -> Sequence[tuple[int, FilterDecision]]:
        decisions = []
        for job_id in self._repository.all_job_ids():
            decision = self.refresh_job(job_id)
            if decision is not None:
                decisions.append((job_id, decision))
        return decisions
