"""Application workflow for persisting a completed Codex scorecard."""

from __future__ import annotations

import json
from collections.abc import Mapping
from contextlib import AbstractContextManager
from typing import Any, Protocol


class CodexScoringOperations(Protocol):
    def connection(self) -> AbstractContextManager[Any]: ...

    def job(self, connection: Any, job_id: int) -> Mapping[str, Any] | None: ...

    def score(self, connection: Any, job: Mapping[str, Any], *, force_refresh: bool) -> Mapping[str, Any]: ...

    def normalize_pipeline(self, value: Any, fallback: str) -> str: ...

    def save(self, connection: Any, job_id: int, values: Mapping[str, Any]) -> None: ...

    def apply_filter(self, connection: Any, job_id: int) -> None: ...

    def now(self) -> int: ...

    def log(self, event: str, **fields: Any) -> None: ...


class CodexScoringWorkflow:
    """Score one job and persist its normalized result through ports."""

    def __init__(self, operations: CodexScoringOperations) -> None:
        self._operations = operations

    def populate(self, connection: Any, job_id: int, *, force_refresh: bool) -> Mapping[str, Any]:
        ops = self._operations
        job = ops.job(connection, job_id)
        if not job:
            raise ValueError("Job not found")
        score = ops.score(connection, job, force_refresh=force_refresh)
        total = int(score.get("total_score", 0))
        downlevel = bool(score.get("downlevel", False))
        ops.save(
            connection,
            job_id,
            {
                "total": total,
                "rationale": score.get("rationale", ""),
                "scorecard": json.dumps(score.get("scorecard", {})),
                "pipeline": ops.normalize_pipeline(score.get("pipeline"), job.get("pipeline", "")),
                "level_assessment": score.get("level_assessment", ""),
                "downlevel": int(downlevel),
                "updated_at": ops.now(),
            },
        )
        ops.apply_filter(connection, job_id)
        ops.log("codex_score_populated", job_id=job_id, total_score=total, downlevel=downlevel)
        return score

    def populate_by_id(self, job_id: int, *, force_refresh: bool) -> Mapping[str, Any]:
        with self._operations.connection() as connection:
            return self.populate(connection, job_id, force_refresh=force_refresh)
