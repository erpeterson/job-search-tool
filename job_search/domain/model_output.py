"""Schema validation for Codex model output before anything is persisted.

Unusable payloads raise ``ExternalServiceError`` with a specific code. Recoverable
deviations (out-of-range numbers, unknown rubric keys, over-long text) are
normalized and logged as ``codex_output_normalized`` events.
"""

import logging

from job_search.domain.errors import ExternalServiceError
from job_search.domain.rules import RUBRIC_FIELDS
from job_search.observability import log_event

MAX_RATIONALE_CHARS = 4000
MAX_LEVEL_ASSESSMENT_CHARS = 500
MAX_LIST_ITEMS = 10
MAX_LIST_ITEM_CHARS = 500
MAX_KEYWORDS_CHARS = 2000
MAX_LOCATION_CHARS = 200
MAX_CRITERIA_CHARS = 4000


def _note(operation, field, issue):
    log_event("codex_output_normalized", level=logging.WARNING, operation=operation, field=field, issue=issue)


def _number(value, field, code):
    """Return a float for int, float, or numeric-string values; reject anything else."""
    if isinstance(value, bool):
        raise ExternalServiceError(f"Codex returned a non-numeric {field}.", code)
    if isinstance(value, (int, float)):
        return float(value)
    if isinstance(value, str):
        text = value.strip()
        if text.replace(".", "", 1).lstrip("-").isdigit():
            return float(text)
    raise ExternalServiceError(f"Codex returned a non-numeric {field}.", code)


def _clamped_int(value, low, high, field, code, operation):
    number = round(_number(value, field, code))
    if not low <= number <= high:
        _note(operation, field, f"value {number} clamped to {low}-{high}")
    return max(low, min(high, number))


def _text(payload, field, limit, code, operation, default=""):
    value = payload.get(field, default)
    if value is None:
        return default
    if not isinstance(value, str):
        raise ExternalServiceError(f"Codex returned a non-string {field}.", code)
    value = value.strip()
    if len(value) > limit:
        _note(operation, field, f"truncated to {limit} characters")
        value = value[:limit]
    return value


def _string_list(payload, field, operation):
    value = payload.get(field)
    if not isinstance(value, list):
        return []
    items = [item.strip()[:MAX_LIST_ITEM_CHARS] for item in value if isinstance(item, str) and item.strip()]
    if len(items) != len(value) or len(items) > MAX_LIST_ITEMS:
        _note(operation, field, "dropped non-string or excess items")
    return items[:MAX_LIST_ITEMS]


def validate_score_payload(payload):
    """Return a normalized scoring payload with the documented fields only."""
    operation = "score_job"
    if not isinstance(payload, dict):
        raise ExternalServiceError("Codex score response must be a JSON object.", "codex_score_not_object")
    if "total_score" not in payload:
        raise ExternalServiceError("Codex score response is missing total_score.", "codex_score_total_missing")
    total = _clamped_int(payload["total_score"], 0, 100, "total_score", "codex_score_total_invalid", operation)

    raw_scorecard = payload.get("scorecard", {})
    if raw_scorecard is None:
        raw_scorecard = {}
    if not isinstance(raw_scorecard, dict):
        raise ExternalServiceError("Codex scorecard must be a JSON object.", "codex_scorecard_not_object")
    unknown = sorted(set(raw_scorecard) - set(RUBRIC_FIELDS))
    if unknown:
        _note(operation, "scorecard", f"dropped unknown rubric keys {unknown}")
    scorecard = {
        field: _clamped_int(value, 0, 10, f"scorecard.{field}", "codex_scorecard_value_invalid", operation)
        for field, value in raw_scorecard.items()
        if field in RUBRIC_FIELDS
    }

    downlevel = payload.get("downlevel", False)
    if isinstance(downlevel, str) and downlevel.strip().lower() in ("true", "false"):
        downlevel = downlevel.strip().lower() == "true"
    if not isinstance(downlevel, bool):
        raise ExternalServiceError("Codex returned a non-boolean downlevel.", "codex_score_downlevel_invalid")

    pipeline = payload.get("pipeline", "")
    if not isinstance(pipeline, (str, list)):
        _note(operation, "pipeline", "ignored non-string pipeline")
        pipeline = ""

    return {
        "total_score": total,
        "scorecard": scorecard,
        "pipeline": pipeline,
        "level_assessment": _text(
            payload, "level_assessment", MAX_LEVEL_ASSESSMENT_CHARS, "codex_score_text_invalid", operation
        ),
        "downlevel": downlevel,
        "rationale": _text(payload, "rationale", MAX_RATIONALE_CHARS, "codex_score_text_invalid", operation),
        "strengths": _string_list(payload, "strengths", operation),
        "risks": _string_list(payload, "risks", operation),
        "recommended_next_step": _text(
            payload, "recommended_next_step", MAX_LIST_ITEM_CHARS, "codex_score_text_invalid", operation
        ),
    }


def validate_refinement_payload(payload):
    """Return normalized refinement fields; absent fields come back as empty strings."""
    operation = "refine_search_query"
    if not isinstance(payload, dict):
        raise ExternalServiceError("Codex refinement response must be a JSON object.", "refinement_not_object")
    return {
        "keywords": _text(payload, "keywords", MAX_KEYWORDS_CHARS, "refinement_keywords_invalid", operation),
        "location": _text(payload, "location", MAX_LOCATION_CHARS, "refinement_location_invalid", operation),
        "criteria": _text(payload, "criteria", MAX_CRITERIA_CHARS, "refinement_criteria_invalid", operation),
        "refinement_notes": _text(
            payload, "refinement_notes", MAX_CRITERIA_CHARS, "refinement_notes_invalid", operation
        ),
    }
