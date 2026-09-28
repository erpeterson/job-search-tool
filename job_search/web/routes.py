"""HTTP routes. Handlers validate input, delegate to domain services, and shape responses."""

from flask import Blueprint, current_app, jsonify, render_template, request, send_from_directory

from job_search.domain.errors import NotFoundError
from job_search.domain.rules import JOB_STATUSES, RUBRIC_FIELDS
from job_search.observability import METRICS
from job_search.web import validation as v
from job_search.web.markdown import markdown_to_html

bp = Blueprint("job_search", __name__)


def services():
    return current_app.extensions["job_search"]


def _config_payload(container):
    config = container.config
    return {
        "config": container.runtime.masked(),
        "api_log_path": str(config.api_log_path),
        "event_log_path": str(config.event_log_path),
        "capture_dir": str(config.capture_dir),
        "gpt_scoring_enabled": container.runtime.gpt_scoring_enabled(),
        "capture_cache_enabled": container.runtime.capture_cache_enabled(),
    }


@bp.get("/")
def index():
    return send_from_directory(current_app.static_folder, "index.html")


@bp.get("/api/state")
def api_state():
    c = services()
    include_filtered = request.args.get("include_filtered") == "1"
    limit, offset = v.list_window()
    return jsonify(
        {
            **_config_payload(c),
            "settings": c.settings.all(),
            "jobs": c.jobs.list(include_filtered=include_filtered, limit=limit, offset=offset),
            "jobs_total": c.jobs.count(include_filtered=include_filtered),
            "company_interests": c.companies.list(),
            "search_queries": c.search.list_queries(),
            "search_runs": c.search.list_runs(),
            "search_schedule": c.scheduler.state(),
            "discoveries": c.search.list_discoveries(),
            "application_packets": c.packets.list_packets(),
            "codex_tasks": c.tasks.list(),
            "pipelines": c.profile.pipeline_names,
            "rubric_fields": RUBRIC_FIELDS,
            "profile_is_example": c.profile_is_example,
        }
    )


@bp.get("/api/metrics")
def api_metrics():
    return jsonify({"counters": METRICS.snapshot()})


# Jobs -----------------------------------------------------------------------


@bp.get("/api/jobs/<int:job_id>")
def api_job(job_id):
    return jsonify({"job": services().jobs.get(job_id)})


@bp.post("/api/jobs")
def api_create_job():
    payload = v.json_body()
    fields = v.manual_job(payload, services().profile.pipeline_names)
    result = services().jobs.create_manual(fields, force_refresh=v.boolean(payload, "force_refresh"))
    return jsonify(result), 201


@bp.post("/api/jobs/<int:job_id>/scrape")
def api_rescrape_job(job_id):
    payload = v.json_body()
    job, scraped = services().jobs.rescrape(job_id, force_refresh=v.boolean(payload, "force_refresh", default=True))
    return jsonify({"job": job, "scraped": scraped})


@bp.delete("/api/jobs/<int:job_id>")
def api_delete_job(job_id):
    payload = v.json_body()
    v.require_confirmation(payload, "DELETE", "delete_job_not_confirmed")
    c = services()
    c.jobs.delete(job_id)
    return jsonify({"deleted_job_id": job_id, "jobs": c.jobs.list(include_filtered=True)})


@bp.post("/api/jobs/<int:job_id>/score-gpt")
def api_score_gpt(job_id):
    v.json_body()
    return jsonify({"task": services().bulk.start_score(job_id)}), 202


@bp.post("/api/jobs/<int:job_id>/score-user")
def api_score_user(job_id):
    payload = v.json_body()
    scorecard = v.user_scorecard(payload)
    total = v.integer(payload, "total_score", 0, 100, nullable=True) if "total_score" in payload else None
    rationale = v.text(payload, "user_rationale", max_length=v.LONG_TEXT)
    return jsonify({"job": services().jobs.score_user(job_id, scorecard, rationale, total)})


@bp.post("/api/jobs/<int:job_id>/interactions")
def api_add_interaction(job_id):
    return jsonify({"job": services().jobs.add_interaction(job_id, v.interaction(v.json_body()))}), 201


@bp.post("/api/jobs/<int:job_id>/notes")
def api_add_note(job_id):
    note = v.text(v.json_body(), "note", max_length=v.LONG_TEXT, required=True)
    return jsonify({"job": services().jobs.add_note(job_id, note)}), 201


@bp.post("/api/jobs/<int:job_id>/status")
def api_update_status(job_id):
    status = v.choice(v.json_body(), "status", JOB_STATUSES, default="researching")
    return jsonify({"job": services().jobs.update_status(job_id, status)})


@bp.post("/api/admin/purge-jobs")
def api_purge_jobs():
    v.require_confirmation(v.json_body(), "PURGE", "purge_jobs_not_confirmed")
    c = services()
    deleted = c.jobs.purge_all()
    return jsonify(
        {
            "deleted_jobs": deleted,
            "jobs": c.jobs.list(include_filtered=True),
            "discoveries": c.search.list_discoveries(),
        }
    )


# Bulk Codex tasks ---------------------------------------------------------------


@bp.get("/api/codex-tasks")
def api_codex_tasks():
    return jsonify({"tasks": services().tasks.list()})


@bp.get("/api/codex-tasks/<task_id>")
def api_codex_task(task_id):
    task = services().tasks.get(task_id)
    if not task:
        raise NotFoundError("Task not found", "task_not_found")
    return jsonify({"task": task})


@bp.post("/api/jobs/bulk/score-gpt")
def api_bulk_score_gpt():
    return jsonify({"task": services().bulk.start_scoring(v.job_ids(v.json_body()))}), 202


@bp.post("/api/jobs/bulk/application-packets/generate")
def api_bulk_generate_application_packets():
    return jsonify({"task": services().bulk.start_packets(v.job_ids(v.json_body()))}), 202


# Application packets --------------------------------------------------------------


@bp.get("/api/application-packets")
def api_application_packets():
    return jsonify({"application_packets": services().packets.list_packets()})


@bp.post("/api/jobs/<int:job_id>/application-packet/generate")
def api_generate_application_packet(job_id):
    v.json_body()
    return jsonify({"task": services().bulk.start_packet(job_id)}), 202


@bp.post("/api/jobs/<int:job_id>/application-packet/attach")
def api_attach_application_packet(job_id):
    c = services()
    c.packets.attach(job_id, v.text(v.json_body(), "path", max_length=1024))
    return jsonify({"job": c.jobs.get(job_id), "application_packets": c.packets.list_packets()})


@bp.get("/api/jobs/<int:job_id>/application-packet/content")
def api_application_packet_content(job_id):
    document = services().packets.read_document(job_id, v.packet_filename())
    return jsonify(
        {
            "path": document.packet_path,
            "file": document.filename,
            "content": document.content,
            "markdown_files": document.markdown_files,
        }
    )


@bp.get("/api/jobs/<int:job_id>/application-packet/render")
def api_application_packet_render(job_id):
    document = services().packets.read_document(job_id, v.packet_filename())
    job = document.job
    return render_template(
        "packet.html",
        title=f"{document.filename} - {job['company']} - {job['title']}",
        packet_path=document.packet_path,
        filename=document.filename,
        body=markdown_to_html(document.content),
    )


# Companies ------------------------------------------------------------------


@bp.get("/api/companies/<int:company_id>")
def api_company_interest(company_id):
    return jsonify({"company": services().companies.get(company_id)})


@bp.post("/api/companies")
def api_create_company_interest():
    company, companies = services().companies.upsert(v.company_fields(v.json_body()))
    return jsonify({"company": company, "companies": companies}), 201


@bp.post("/api/companies/<int:company_id>")
def api_update_company_interest(company_id):
    company, companies = services().companies.update(company_id, v.company_fields(v.json_body(), partial=True))
    return jsonify({"company": company, "companies": companies})


# Search ---------------------------------------------------------------------------


@bp.post("/api/search/run")
def api_run_search():
    force_refresh = v.boolean(v.json_body(), "force_refresh")
    return jsonify({"task": services().bulk.start_search(force_refresh=force_refresh)}), 202


@bp.post("/api/search/queries")
def api_create_search_query():
    return jsonify(
        {
            "search_queries": services().search.create_query(
                v.search_query(v.json_body(), services().profile.pipeline_names)
            )
        }
    ), 201


@bp.post("/api/search/queries/<int:query_id>")
def api_update_search_query(query_id):
    fields = v.search_query(v.json_body(), services().profile.pipeline_names, partial=True)
    return jsonify({"search_queries": services().search.update_query(query_id, fields)})


# Settings and configuration ---------------------------------------------------------------


@bp.post("/api/settings")
def api_update_settings():
    settings, jobs = services().settings.update_thresholds(v.threshold_settings(v.json_body()))
    return jsonify({"settings": settings, "jobs": jobs})


@bp.post("/api/config")
def api_update_config():
    c = services()
    settings = c.settings.apply_runtime_config(v.json_body())
    payload = _config_payload(c)
    if settings is not None:
        payload["settings"] = settings
    return jsonify(payload)
