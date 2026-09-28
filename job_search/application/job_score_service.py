"""Codex score orchestration using injected model and source-material ports."""

from __future__ import annotations

import textwrap
from collections.abc import Callable, Mapping, Sequence
from typing import Any

from job_search.application.job_scoring_policy import JobScoringPolicy


class JobScoreService:
    def __init__(
        self,
        policy: JobScoringPolicy,
        *,
        enabled: Callable[[], bool],
        available: Callable[[], bool],
        cli_path: Callable[[], str],
        model: Callable[[Any], str],
        career_manual: Callable[[], str],
        guidance: Callable[[], str],
        examples: Callable[[Any], Sequence[Mapping[str, Any]]],
        complete: Callable[..., str],
    ) -> None:
        self._policy = policy
        self._enabled = enabled
        self._available = available
        self._cli_path = cli_path
        self._model = model
        self._career_manual = career_manual
        self._guidance = guidance
        self._examples = examples
        self._complete = complete

    def score(self, connection: Any, job: Mapping[str, Any], *, force_refresh: bool = False) -> Mapping[str, Any]:
        if not self._enabled():
            raise RuntimeError(
                "Codex scoring is currently disabled. Set JOB_SEARCH_ENABLE_GPT_SCORING=1 to re-enable it."
            )
        if not self._available():
            raise RuntimeError(
                f"Codex CLI is unavailable at {self._cli_path()!r}. Set CODEX_CLI_PATH or install Codex CLI before scoring."
            )
        return self._policy.score(
            job,
            career_context=self.career_context(),
            calibration_examples=self.calibration_examples(connection),
            invoke=lambda prompt: self._complete(
                self._model(connection), prompt, "score_job", force_refresh=force_refresh
            ),
        )

    def career_context(self) -> str:
        manual = self._career_manual()
        return textwrap.shorten(manual, width=9000, placeholder="\n[manual truncated]\n") + "\n\n" + self._guidance()

    def calibration_examples(self, connection: Any) -> list[dict[str, Any]]:
        return [
            {
                "company": row["company"],
                "title": row["title"],
                "pipeline": row["pipeline"],
                "gpt_score": row["gpt_score"],
                "user_score": row["user_score"],
                "user_rationale": row["user_rationale"],
                "posting_excerpt": textwrap.shorten(row["posting_text"] or "", width=800, placeholder="..."),
            }
            for row in self._examples(connection)
        ]

    def score_discovery(
        self, connection: Any, discovery: Mapping[str, Any], *, force_refresh: bool = False
    ) -> Mapping[str, Any]:
        job = {
            "company": discovery.get("company"),
            "title": discovery.get("title"),
            "url": discovery.get("url"),
            "location": discovery.get("location"),
            "pipeline": discovery.get("pipeline") or "",
            "posting_text": discovery.get("snippet"),
            "notes": f"Source board: {discovery.get('board')}. Search criteria: {discovery.get('criteria', '')}",
        }
        return self.score(connection, job, force_refresh=force_refresh)
