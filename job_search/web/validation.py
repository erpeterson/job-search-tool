"""Request payload parsing and validation for the HTTP API."""

import json
import logging
import re

from flask import request

from job_search.domain.errors import UnsupportedMediaTypeError, ValidationError
from job_search.domain.rules import COMPANY_STATUSES, JOB_STATUSES, PIPELINES, RUBRIC_FIELDS, SEARCH_BOARDS
from job_search.observability import record_exception

SHORT_TEXT = 500
LONG_TEXT = 20_000
POSTING_TEXT = 200_000
_INT_PATTERN = re.compile(r"^-?\d+(\.0+)?$")


def require_json_content_type():
    """Reject non-empty bodies that are not JSON, so HTML forms cannot trigger API actions."""
    has_body = bool(request.content_length or request.get_data(cache=True))
    if has_body and not request.is_json:
        raise UnsupportedMediaTypeError(
            "Request body must be sent as application/json.", "request_body_unsupported_media_type"
        )


def json_body():
    """Parse the JSON object body. Only an empty body is treated as ``{}``."""
    require_json_content_type()
    raw = request.get_data(cache=True)
    if not raw:
        return {}
    try:
        payload = json.loads(raw)
    except (json.JSONDecodeError, UnicodeDecodeError) as exc:
        record_exception(
            "request_body_json_decode_failed",
            "web.validation",
            "json_body",
            exc,
            level=logging.INFO,
            recovery="Rejected with HTTP 400.",
        )
        raise ValidationError("Request body is not valid JSON.", "request_body_malformed_json") from exc
    if not isinstance(payload, dict):
        raise ValidationError("Request body must be a JSON object.", "request_body_not_object")
    return payload


def text(payload, key, default="", max_length=SHORT_TEXT, required=False, strip=True):
    value = payload.get(key, default)
    if value is None:
        value = ""
    if not isinstance(value, str):
        raise ValidationError(f"{key} must be a string.", f"field_{key}_not_string")
    if strip:
        value = value.strip()
    if required and not value:
        raise ValidationError(f"{key.replace('_', ' ').capitalize()} is required.", f"field_{key}_required")
    if len(value) > max_length:
        raise ValidationError(f"{key} must be at most {max_length} characters.", f"field_{key}_too_long")
    return value


def choice(payload, key, options, default=None, allow_blank=False):
    value = payload.get(key, default)
    if allow_blank and value in (None, ""):
        return ""
    if value not in options:
        raise ValidationError(f"{key} must be one of: {', '.join(options)}.", f"field_{key}_invalid_choice")
    return value


def boolean(payload, key, default=False):
    value = payload.get(key, default)
    if isinstance(value, bool):
        return value
    if value in (None, ""):
        return default
    if value in (0, 1, "0", "1", "true", "false"):
        return value in (1, "1", "true")
    raise ValidationError(f"{key} must be a boolean.", f"field_{key}_not_boolean")


def integer(payload, key, minimum, maximum, default=None, nullable=False):
    value = payload.get(key, default)
    if value in (None, ""):
        if nullable:
            return None
        if default is not None:
            return default
        raise ValidationError(f"{key} is required.", f"field_{key}_required")
    if isinstance(value, bool) or not _INT_PATTERN.match(str(value).strip()):
        raise ValidationError(f"{key} must be an integer.", f"field_{key}_not_integer")
    number = int(float(str(value).strip()))
    if not minimum <= number <= maximum:
        raise ValidationError(f"{key} must be between {minimum} and {maximum}.", f"field_{key}_out_of_range")
    return number


def job_ids(payload):
    raw_ids = payload.get("job_ids", [])
    if not isinstance(raw_ids, list):
        raise ValidationError("job_ids must be a list.", "job_ids_not_list")
    if len(raw_ids) > 1000:
        raise ValidationError("Select at most 1000 jobs.", "job_ids_too_many")
    ids = []
    seen = set()
    for raw_id in raw_ids:
        if isinstance(raw_id, bool) or not _INT_PATTERN.match(str(raw_id).strip()):
            raise ValidationError("job_ids must contain only integers.", "job_ids_not_integers")
        job_id = int(float(str(raw_id).strip()))
        if job_id > 0 and job_id not in seen:
            seen.add(job_id)
            ids.append(job_id)
    if not ids:
        raise ValidationError("Select at least one job.", "job_ids_empty")
    return ids


def require_confirmation(payload, word, error_code):
    if payload.get("confirm") != word:
        raise ValidationError(f"Type {word} to confirm.", error_code)


def manual_job(payload):
    url = text(payload, "url", max_length=2048)
    if not url:
        raise ValidationError("URL is required.", "manual_job_url_required")
    if not payload.get("pipeline"):
        raise ValidationError("Pipeline is required.", "manual_job_pipeline_required")
    return {
        "url": url,
        "pipeline": choice(payload, "pipeline", PIPELINES),
        "company": text(payload, "company"),
        "title": text(payload, "title"),
        "location": text(payload, "location"),
        "status": choice(payload, "status", JOB_STATUSES, default="researching"),
        "posting_text": text(payload, "posting_text", max_length=POSTING_TEXT),
        "notes": text(payload, "notes", max_length=LONG_TEXT),
    }


_COMPANY_TEXT_FIELDS = ("rationale", "notes", "next_step", "contacts")


def company_fields(payload, partial=False):
    fields = {}
    if not partial or "company" in payload:
        fields["company"] = text(payload, "company") or "Unknown company"
    if not partial or "status" in payload:
        fields["status"] = choice(payload, "status", COMPANY_STATUSES, default="watching")
    if not partial or "interest_score" in payload:
        fields["interest_score"] = integer(payload, "interest_score", 0, 100, nullable=True)
    for key in _COMPANY_TEXT_FIELDS:
        if not partial or key in payload:
            fields[key] = text(payload, key, max_length=LONG_TEXT)
    return fields


def user_scorecard(payload):
    raw = payload.get("scorecard", {})
    if not isinstance(raw, dict):
        raise ValidationError("scorecard must be an object.", "user_scorecard_not_object")
    scorecard = {}
    for field in RUBRIC_FIELDS:
        value = raw.get(field, 0)
        if value in (None, ""):
            value = 0
        if isinstance(value, bool) or not re.match(r"^-?\d+(\.\d+)?$", str(value).strip()):
            raise ValidationError(f"scorecard.{field} must be a number.", "user_scorecard_not_number")
        scorecard[field] = max(0, min(10, round(float(value))))
    return scorecard


def interaction(payload):
    fields = {key: text(payload, key) for key in ("occurred_on", "person_name", "person_role", "channel", "next_step")}
    fields.update({key: text(payload, key, max_length=LONG_TEXT) for key in ("summary", "notes_to_self")})
    return fields


def search_query(payload, partial=False):
    fields = {}
    if not partial or "board" in payload:
        fields["board"] = choice(payload, "board", SEARCH_BOARDS, default="linkedin")
    if not partial or "pipeline" in payload:
        fields["pipeline"] = choice(payload, "pipeline", PIPELINES, default="", allow_blank=True)
    if not partial or "keywords" in payload:
        fields["keywords"] = text(payload, "keywords", max_length=2000, required=True)
    if not partial or "location" in payload:
        fields["location"] = text(payload, "location")
    if not partial or "criteria" in payload:
        fields["criteria"] = text(payload, "criteria", max_length=LONG_TEXT)
    if not partial or "enabled" in payload:
        fields["enabled"] = boolean(payload, "enabled", default=True)
    return fields


def threshold_settings(payload):
    updates = {}
    for key in ("gpt_threshold", "user_threshold"):
        if key in payload:
            updates[key] = str(integer(payload, key, 0, 100))
    if "codex_model" in payload:
        updates["codex_model"] = text(payload, "codex_model", max_length=200)
    return updates
