"""Search run and query HTTP mapping backed by injected use cases."""

from flask import Blueprint, jsonify, request

from job_search.application.job_scoring_policy import PIPELINES
from job_search.presentation.read_routes import dependency
from job_search.validation import RequestValidationError, boolean, choice, optional_text, require_json_object

SUPPORTED_BOARDS = {"linkedin", "indeed"}


def api_run_search():
    payload = require_json_object(request.get_json(silent=True) or {})
    force_refresh = boolean(payload.get("force_refresh"), "force_refresh", default=False)
    run = dependency("search_run_service").run(trigger="manual", force_refresh=force_refresh)
    state = dependency("console_query_service").state(include_filtered=True)
    return jsonify(
        {"run": run, "jobs": state["jobs"], "search_runs": state["search_runs"], "discoveries": state["discoveries"]}
    )


def api_create_search_query():
    payload = require_json_object(request.get_json(silent=True) or {})
    board = choice(payload.get("board", "linkedin"), "board", SUPPORTED_BOARDS, required=True)
    pipeline = choice(payload.get("pipeline", ""), "pipeline", PIPELINES)
    keywords = optional_text(payload.get("keywords", ""), "keywords", max_length=2_000)
    if not keywords:
        raise RequestValidationError("keywords is required.")
    dependency("search_query_service").create(
        {
            "board": board,
            "pipeline": pipeline,
            "keywords": keywords,
            "location": optional_text(payload.get("location", ""), "location", max_length=500),
            "enabled": boolean(payload.get("enabled"), "enabled", default=True),
            "criteria": optional_text(payload.get("criteria", ""), "criteria", max_length=10_000),
        }
    )
    return jsonify({"search_queries": dependency("console_query_service").queries()}), 201


def api_update_search_query(query_id):
    payload = require_json_object(request.get_json(silent=True) or {})
    board = choice(payload["board"], "board", SUPPORTED_BOARDS, required=True) if "board" in payload else None
    pipeline = choice(payload["pipeline"], "pipeline", PIPELINES) if "pipeline" in payload else None
    keywords = optional_text(payload["keywords"], "keywords", max_length=2_000) if "keywords" in payload else None
    location = optional_text(payload["location"], "location", max_length=500) if "location" in payload else None
    criteria = optional_text(payload["criteria"], "criteria", max_length=10_000) if "criteria" in payload else None
    dependency("search_query_service").update(
        query_id,
        {
            "board": board,
            "pipeline": pipeline,
            "keywords": keywords,
            "location": location,
            "criteria": criteria,
            "enabled": boolean(payload["enabled"], "enabled") if "enabled" in payload else None,
        },
    )
    return jsonify({"search_queries": dependency("console_query_service").queries()})


def register_search_routes(blueprint: Blueprint) -> None:
    blueprint.add_url_rule("/api/search/run", view_func=api_run_search, methods=["POST"])
    blueprint.add_url_rule("/api/search/queries", view_func=api_create_search_query, methods=["POST"])
    blueprint.add_url_rule("/api/search/queries/<int:query_id>", view_func=api_update_search_query, methods=["POST"])
