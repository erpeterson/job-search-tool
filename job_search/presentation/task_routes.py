"""HTTP validation and response mapping for durable bulk-task submission."""

from flask import Blueprint, jsonify, request

from job_search.presentation.read_routes import dependency
from job_search.validation import RequestValidationError, integer, require_json_object


def clean_job_ids(payload):
    raw_ids = payload.get("job_ids", [])
    if not isinstance(raw_ids, list):
        raise RequestValidationError("job_ids must be a list.")
    if not raw_ids:
        raise RequestValidationError("Select at least one job.")
    if len(raw_ids) > 50:
        raise RequestValidationError("job_ids must contain at most 50 jobs.")
    job_ids = []
    seen = set()
    for raw_id in raw_ids:
        job_id = integer(raw_id, "job_ids item", minimum=1, maximum=2_147_483_647)
        if job_id in seen:
            raise RequestValidationError("job_ids must not contain duplicates.")
        seen.add(job_id)
        job_ids.append(job_id)
    return job_ids


def _submit(operation):
    payload = require_json_object(request.get_json(silent=True))
    job_ids = clean_job_ids(payload)
    result = dependency("task_submission_service").submit(operation, job_ids)
    if result.unavailable_reason:
        return jsonify({"error": result.unavailable_reason}), 409
    return jsonify({"task": result.task}), 202


def api_bulk_score_gpt():
    return _submit("scorecards")


def api_bulk_generate_application_packets():
    return _submit("application_packets")


def register_task_routes(blueprint: Blueprint) -> None:
    blueprint.add_url_rule("/api/jobs/bulk/score-gpt", view_func=api_bulk_score_gpt, methods=["POST"])
    blueprint.add_url_rule(
        "/api/jobs/bulk/application-packets/generate", view_func=api_bulk_generate_application_packets, methods=["POST"]
    )
