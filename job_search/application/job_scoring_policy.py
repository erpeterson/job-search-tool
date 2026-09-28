"""Job-score prompt policy independent of the Codex transport implementation."""

from __future__ import annotations

import json
from collections.abc import Callable, Mapping, Sequence
from typing import Any

RUBRIC_FIELDS = [
    "interesting_technical_problems",
    "organizational_influence",
    "cross_functional_work",
    "opportunity_to_mentor",
    "work_life_balance",
    "low_operational_burden",
    "compensation",
    "mission",
]

PIPELINES = ["Executive IC", "Office of the CTO", "Adjacent industries", "Wildcards"]

ORACLE_IC6_LEVEL_REFERENCE = (
    "Oracle Software Engineer IC-6 is Architect. "
    "Treat IC6-equivalent as Architect / Principal-plus / Staff-plus scope with broad technical influence, "
    "cross-team architecture, durable technical direction, or organization-level engineering judgment."
)


def normalize_pipeline(value: Any, fallback: str = "") -> str:
    """Return a known pipeline from model output or retain the prior value."""
    if isinstance(value, str):
        candidate = value.strip()
        return candidate if candidate in PIPELINES else fallback
    if isinstance(value, (list, tuple, set)):
        for candidate in value:
            if isinstance(candidate, str) and candidate.strip() in PIPELINES:
                return candidate.strip()
    return fallback


class JobScoringPolicy:
    def __init__(self, *, pipelines: Sequence[str], rubric_fields: Sequence[str], level_reference: str) -> None:
        self._pipelines = pipelines
        self._rubric_fields = rubric_fields
        self._level_reference = level_reference

    def score(
        self,
        job: Mapping[str, Any],
        *,
        career_context: Mapping[str, Any],
        calibration_examples: Sequence[Mapping[str, Any]],
        invoke: Callable[[Mapping[str, Any]], str],
    ) -> Mapping[str, Any]:
        prompt = {
            "task": "Score this job for Eric Peterson's job search.",
            "level_reference": {
                "canonical_source": "local Oracle IC6 target definition",
                "oracle_ic6_definition": self._level_reference,
            },
            "instructions": [
                "Return JSON only.",
                "Use a 0-100 total fit score.",
                "Score each rubric item from 0-10.",
                "Reward cross-cutting architecture, organizational scaling, engineering effectiveness, developer experience, AI-enabled development, technical strategy, and technical decision quality.",
                "Penalize line management, heavy operational ownership, firefighting, incremental feature ownership, narrow service ownership, and roles that only value hands-on coding.",
                "Reject or heavily penalize Account Executive, account management, business development, quota-carrying, and other sales roles.",
                "Use calibration examples to adjust future scoring toward Eric's own scores.",
                "Classify IC6-equivalent or higher scope using the local Oracle IC-6 Architect target definition.",
                "Do not invent facts missing from the posting.",
            ],
            "expected_json_schema": {
                "total_score": "integer 0-100",
                "pipeline": f"one of: {', '.join(self._pipelines)}",
                "scorecard": {field: "integer 0-10" for field in self._rubric_fields},
                "level_assessment": "short phrase",
                "downlevel": "boolean",
                "rationale": "short paragraph",
                "strengths": ["short bullets"],
                "risks": ["short bullets"],
                "recommended_next_step": "short sentence",
            },
            "career_context": career_context,
            "calibration_examples": calibration_examples,
            "job": {
                key: job.get(key)
                for key in ("company", "title", "url", "location", "pipeline", "posting_text", "notes")
            },
        }
        output = invoke(prompt)
        if not output:
            raise RuntimeError("Codex CLI response did not include text output.")
        try:
            return json.loads(output)
        except json.JSONDecodeError as exc:
            raise RuntimeError("Codex CLI response was not valid JSON. See logs and captures for details.") from exc
