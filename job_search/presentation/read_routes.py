"""Console and read-only HTTP routes backed by injected application services."""

from flask import Blueprint, current_app, jsonify, render_template, request

from job_search.application.job_scoring_policy import PIPELINES, RUBRIC_FIELDS

CONFIG_KEYS = (
    "CODEX_CLI_PATH",
    "CODEX_MODEL",
    "JOB_SEARCH_ENABLE_GPT_SCORING",
    "JOB_SEARCH_USE_CAPTURE_CACHE",
)


def dependency(name):
    return getattr(current_app.extensions["job_search.dependencies"], name)


def index():
    return render_template("index.html")


def api_state():
    include_filtered = request.args.get("include_filtered") == "1"
    config = dependency("configuration")
    state = dict(dependency("console_query_service").state(include_filtered=include_filtered))
    state.update(
        {
            "config": config.masked(CONFIG_KEYS),
            "api_log_path": str(config.paths.api_log),
            "event_log_path": str(config.paths.app_log),
            "capture_dir": str(config.paths.captures),
            "gpt_scoring_enabled": config.enabled("JOB_SEARCH_ENABLE_GPT_SCORING"),
            "capture_cache_enabled": config.enabled("JOB_SEARCH_USE_CAPTURE_CACHE"),
            "search_schedule": {
                "interval_seconds": config.settings.search_interval_seconds,
                "managed_by": "job_search.scheduler",
            },
            "codex_tasks": dependency("background_task_service").list(10),
            "pipelines": PIPELINES,
            "rubric_fields": RUBRIC_FIELDS,
        }
    )
    return jsonify(state)


def api_job(job_id):
    job = dependency("console_query_service").job(job_id)
    if not job:
        return jsonify({"error": "Job not found"}), 404
    return jsonify({"job": job})


def api_application_packets():
    return jsonify({"application_packets": dependency("console_query_service").packets()})


def api_codex_tasks():
    return jsonify({"tasks": dependency("background_task_service").list(10)})


def api_codex_task(task_id):
    task = dependency("background_task_service").get(task_id)
    if not task:
        return jsonify({"error": "Task not found"}), 404
    return jsonify({"task": task})


def api_company_interest(company_id):
    company = dependency("console_query_service").company(company_id)
    if not company:
        return jsonify({"error": "Company interest not found"}), 404
    return jsonify({"company": company})


def register_read_routes(blueprint: Blueprint) -> None:
    """Attach read routes to the existing blueprint before app registration."""
    blueprint.add_url_rule("/", view_func=index)
    blueprint.add_url_rule("/api/state", view_func=api_state)
    blueprint.add_url_rule("/api/jobs/<int:job_id>", view_func=api_job)
    blueprint.add_url_rule("/api/application-packets", view_func=api_application_packets)
    blueprint.add_url_rule("/api/codex-tasks", view_func=api_codex_tasks)
    blueprint.add_url_rule("/api/codex-tasks/<task_id>", view_func=api_codex_task)
    blueprint.add_url_rule("/api/companies/<int:company_id>", view_func=api_company_interest)
