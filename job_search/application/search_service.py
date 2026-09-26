"""Framework-independent search policy orchestration."""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from typing import Any

from job_search.application.contracts import JobBoardGateway
from job_search.domain.filtering import decide_job_filter


@dataclass(frozen=True)
class SearchDecision:
    result: Mapping[str, Any]
    tracked: bool
    reason: str


class SearchService:
    """Apply deterministic job policy to externally supplied board results."""

    def __init__(
        self,
        boards: JobBoardGateway,
        *,
        gpt_threshold: int = 0,
        user_threshold: int = 0,
        gpt_scoring_enabled: bool = False,
    ) -> None:
        self._boards = boards
        self._gpt_threshold = gpt_threshold
        self._user_threshold = user_threshold
        self._gpt_scoring_enabled = gpt_scoring_enabled

    def discover(
        self, board: str, keywords: str, location: str, *, force_refresh: bool = False
    ) -> Sequence[SearchDecision]:
        results = self._boards.fetch(board, keywords, location, force_refresh=force_refresh)
        decisions = []
        for result in results:
            filter_result = decide_job_filter(
                result,
                gpt_threshold=self._gpt_threshold,
                user_threshold=self._user_threshold,
                gpt_scoring_enabled=self._gpt_scoring_enabled,
            )
            decisions.append(
                SearchDecision(
                    result=result, tracked=not filter_result.filtered, reason="; ".join(filter_result.reasons)
                )
            )
        return decisions
