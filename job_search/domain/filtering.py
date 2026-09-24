"""Job visibility policy independent of Flask and SQLite."""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from typing import Any


@dataclass(frozen=True)
class FilterDecision:
    filtered: bool
    reasons: tuple[str, ...]


def decide_job_filter(
    job: Mapping[str, Any] | None,
    *,
    gpt_threshold: int,
    user_threshold: int,
    gpt_scoring_enabled: bool,
) -> FilterDecision:
    """Apply the product's visibility rules to a job record."""
    if job is None:
        return FilterDecision(False, ())
    reasons: list[str] = []
    if job.get("downlevel"):
        reasons.append("downlevel relative to Oracle IC6-equivalent target")
    gpt_score = job.get("gpt_score")
    if gpt_scoring_enabled and gpt_score is not None and gpt_score < gpt_threshold:
        reasons.append(f"gpt_score {gpt_score} below threshold {gpt_threshold}")
    user_score = job.get("user_score")
    if user_score is not None and user_score < user_threshold:
        reasons.append(f"user_score {user_score} below threshold {user_threshold}")
    return FilterDecision(bool(reasons), tuple(reasons))
