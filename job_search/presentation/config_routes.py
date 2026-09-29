"""Runtime settings and confirmed administration HTTP routes."""

from flask import Blueprint, jsonify, request

from job_search.presentation.read_routes import CONFIG_KEYS, dependency
from job_search.validation import RequestValidationError, environment_value, integer, optional_text, require_json_object


def _config_response(*, include_settings: bool):
    config = dependency("configuration")
    body = {
        "config": config.masked(CONFIG_KEYS),
        "api_log_path": str(config.paths.api_log),
        "event_log_path": str(config.paths.app_log),
        "capture_dir": str(config.paths.captures),
        "gpt_scoring_enabled": config.enabled("JOB_SEARCH_ENABLE_GPT_SCORING"),
        "capture_cache_enabled": config.enabled("JOB_SEARCH_USE_CAPTURE_CACHE"),
    }
    if include_settings:
        body["settings"] = dependency("console_query_service").settings()
    return jsonify(body)


def api_update_config():
    payload = require_json_object(request.get_json(silent=True) or {})
    updates = {}
    for key in CONFIG_KEYS:
        if key not in payload:
            continue
        value = environment_value(payload.get(key, ""), key, max_length=4_000)
        if key in {"JOB_SEARCH_ENABLE_GPT_SCORING", "JOB_SEARCH_USE_CAPTURE_CACHE"} and value not in {"0", "1"}:
            raise RequestValidationError(f"{key} must be 0 or 1.")
        if value or key == "CODEX_MODEL":
            updates[key] = value
    if not updates:
        return _config_response(include_settings=False)
    dependency("configuration_service").update(updates)
    return _config_response(include_settings=True)


def api_purge_jobs():
    payload = require_json_object(request.get_json(silent=True) or {})
    if payload.get("confirm") != "PURGE":
        return jsonify({"error": "Type PURGE to confirm tracked job deletion."}), 400
    before = dependency("job_service").purge_jobs()
    state = dependency("console_query_service").state(include_filtered=True)
    return jsonify({"deleted_jobs": before, "jobs": state["jobs"], "discoveries": state["discoveries"]})


def api_update_settings():
    payload = require_json_object(request.get_json(silent=True) or {})
    validated = {}
    if "gpt_threshold" in payload:
        validated["gpt_threshold"] = str(integer(payload["gpt_threshold"], "gpt_threshold", minimum=0, maximum=100))
    if "user_threshold" in payload:
        validated["user_threshold"] = str(integer(payload["user_threshold"], "user_threshold", minimum=0, maximum=100))
    if "codex_model" in payload:
        validated["codex_model"] = optional_text(payload["codex_model"], "codex_model", max_length=200)
    dependency("settings_service").save_and_refresh(validated)
    reads = dependency("console_query_service")
    return jsonify({"settings": reads.settings(), "jobs": reads.jobs(include_filtered=True)})


def register_config_routes(blueprint: Blueprint) -> None:
    blueprint.add_url_rule("/api/config", view_func=api_update_config, methods=["POST"])
    blueprint.add_url_rule("/api/admin/purge-jobs", view_func=api_purge_jobs, methods=["POST"])
    blueprint.add_url_rule("/api/settings", view_func=api_update_settings, methods=["POST"])
