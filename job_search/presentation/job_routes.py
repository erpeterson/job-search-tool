"""Job mutation endpoints backed by injected application services."""

from flask import Blueprint, jsonify, request

from job_search.application.job_scoring_policy import PIPELINES, RUBRIC_FIELDS
from job_search.presentation.read_routes import dependency
from job_search.validation import (
    RequestValidationError,
    boolean,
    choice,
    http_url,
    integer,
    optional_text,
    require_json_object,
)

JOB_STATUSES = {
    "researching",
    "interested",
    "applied",
    "interviewing",
    "offer",
    "rejected",
    "declined",
    "paused",
}


def api_rescrape_job(job_id):
    payload = require_json_object(request.get_json(silent=True))
    force_refresh = boolean(payload.get("force_refresh"), "force_refresh", default=True)
    result = dependency("rescrape_service").rescrape(job_id, force_refresh=force_refresh)
    if result.job is None:
        return jsonify({"error": "Job not found"}), 404
    if result.scraped is None:
        return jsonify({"error": "Job does not have a URL to scrape."}), 400
    return jsonify({"job": dependency("job_service").get_job(job_id), "scraped": result.scraped})


def api_delete_job(job_id):
    payload = require_json_object(request.get_json(silent=True))
    if payload.get("confirm") != "DELETE":
        return jsonify({"error": "Type DELETE to confirm job deletion."}), 400
    reads = dependency("console_query_service")
    job = dependency("job_service").delete_job(job_id)
    if not job:
        return jsonify({"error": "Job not found"}), 404
    return jsonify({"deleted_job_id": job_id, "jobs": reads.jobs(include_filtered=True)})


def api_create_job():
    payload = require_json_object(request.get_json(silent=True) or {})
    url = http_url(payload.get("url"))
    pipeline = choice(payload.get("pipeline"), "pipeline", PIPELINES, required=True)
    status = choice(payload.get("status", "researching"), "status", JOB_STATUSES, required=True)
    result = dependency("manual_job_service").create(
        {
            "company": optional_text(payload.get("company", ""), "company", max_length=300),
            "title": optional_text(payload.get("title", ""), "title", max_length=500),
            "url": url,
            "location": optional_text(payload.get("location", ""), "location", max_length=500),
            "pipeline": pipeline,
            "status": status,
            "posting_text": optional_text(payload.get("posting_text", ""), "posting_text", max_length=100_000),
            "notes": optional_text(payload.get("notes", ""), "notes", max_length=20_000),
        },
        force_refresh=boolean(payload.get("force_refresh"), "force_refresh", default=False),
    )
    if result.job_id is None:
        return jsonify({"error": "This job URL is already tracked.", "job": result.existing_job}), 409
    return jsonify(
        {
            "job": dependency("job_service").get_job(result.job_id),
            "scrape_error": result.scrape_error,
            "score_error": result.score_error,
        }
    ), 201


def api_score_gpt(job_id):
    result = dependency("scoring_service").score(job_id)
    if result.state == "missing":
        return jsonify({"error": "Job not found"}), 404
    if result.state == "unavailable":
        return jsonify({"error": result.unavailable_reason}), 409
    return jsonify({"job": result.job, "raw_score": result.raw_score})


def api_score_user(job_id):
    payload = require_json_object(request.get_json(silent=True) or {})
    raw_scorecard = payload.get("scorecard", {})
    if not isinstance(raw_scorecard, dict):
        raise RequestValidationError("scorecard must be a JSON object.")
    scorecard = {field: integer(raw_scorecard.get(field, 0), field, minimum=0, maximum=10) for field in RUBRIC_FIELDS}
    total = integer(payload["total_score"], "total_score", minimum=0, maximum=100) if "total_score" in payload else None
    rationale = optional_text(payload.get("user_rationale", ""), "user_rationale", max_length=20_000)
    if not dependency("user_score_service").save(job_id, scorecard, total, rationale):
        return jsonify({"error": "Job not found"}), 404
    return jsonify({"job": dependency("console_query_service").job(job_id)})


def api_add_interaction(job_id):
    payload = require_json_object(request.get_json(silent=True) or {})
    values = {
        "occurred_on": optional_text(payload.get("occurred_on", ""), "occurred_on", max_length=40),
        "person_name": optional_text(payload.get("person_name", ""), "person_name", max_length=300),
        "person_role": optional_text(payload.get("person_role", ""), "person_role", max_length=300),
        "channel": optional_text(payload.get("channel", ""), "channel", max_length=100),
        "summary": optional_text(payload.get("summary", ""), "summary", max_length=20_000),
        "notes_to_self": optional_text(payload.get("notes_to_self", ""), "notes_to_self", max_length=20_000),
        "next_step": optional_text(payload.get("next_step", ""), "next_step", max_length=2_000),
    }
    if not dependency("job_service").add_interaction(job_id, values):
        return jsonify({"error": "Job not found"}), 404
    return jsonify({"job": dependency("console_query_service").job(job_id)}), 201


def api_add_note(job_id):
    payload = require_json_object(request.get_json(silent=True) or {})
    note = optional_text(payload.get("note", ""), "note", max_length=20_000)
    if not note:
        raise RequestValidationError("note is required.")
    if not dependency("job_service").add_note(job_id, note):
        return jsonify({"error": "Job not found"}), 404
    return jsonify({"job": dependency("console_query_service").job(job_id)}), 201


def api_update_status(job_id):
    payload = require_json_object(request.get_json(silent=True) or {})
    status = choice(payload.get("status", "researching"), "status", JOB_STATUSES, required=True)
    if not dependency("job_service").update_status(job_id, status):
        return jsonify({"error": "Job not found"}), 404
    return jsonify({"job": dependency("console_query_service").job(job_id)})


def register_job_routes(blueprint: Blueprint) -> None:
    blueprint.add_url_rule("/api/jobs/<int:job_id>/scrape", view_func=api_rescrape_job, methods=["POST"])
    blueprint.add_url_rule("/api/jobs/<int:job_id>", view_func=api_delete_job, methods=["DELETE"])
    blueprint.add_url_rule("/api/jobs", view_func=api_create_job, methods=["POST"])
    blueprint.add_url_rule("/api/jobs/<int:job_id>/score-gpt", view_func=api_score_gpt, methods=["POST"])
    blueprint.add_url_rule("/api/jobs/<int:job_id>/score-user", view_func=api_score_user, methods=["POST"])
    blueprint.add_url_rule("/api/jobs/<int:job_id>/interactions", view_func=api_add_interaction, methods=["POST"])
    blueprint.add_url_rule("/api/jobs/<int:job_id>/notes", view_func=api_add_note, methods=["POST"])
    blueprint.add_url_rule("/api/jobs/<int:job_id>/status", view_func=api_update_status, methods=["POST"])
