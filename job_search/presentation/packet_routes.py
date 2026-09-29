"""Packet generation, attachment, content, and rendering HTTP routes."""

from flask import Blueprint, Response, jsonify, request

from job_search.application.packet_generation_service import PacketAlreadyAssociatedError, PacketJobMissingError
from job_search.presentation.packet_rendering import render_packet_page
from job_search.presentation.read_routes import dependency
from job_search.validation import RequestValidationError, optional_text, require_json_object


def _log_failure(event, error_code, operation, job_id, exc):
    dependency("observability").telemetry.event(
        event,
        error_code=error_code,
        component="presentation.packets",
        operation=operation,
        job_id=job_id,
        error_type=type(exc).__name__,
        cause=type(exc).__name__,
    )


def _valid_markdown_filename(filename):
    return filename.endswith(".md") and "/" not in filename and "\\" not in filename


def api_generate_application_packet(job_id):
    try:
        packet = dependency("packet_generation_service").generate(job_id)
    except PacketJobMissingError as exc:
        _log_failure("packet_generation_job_missing", "PACKET_GENERATION_JOB_MISSING", "generate", job_id, exc)
        return jsonify({"error": "Job not found"}), 404
    except PacketAlreadyAssociatedError as exc:
        _log_failure("packet_generation_already_associated", "PACKET_GENERATION_EXISTS", "generate", job_id, exc)
        return jsonify({"error": "This job already has an associated application packet."}), 409
    reads = dependency("console_query_service")
    return jsonify({"packet": packet, "job": reads.job(job_id), "application_packets": reads.packets()}), 201


def api_attach_application_packet(job_id):
    payload = require_json_object(request.get_json(silent=True))
    packet_path = optional_text(payload.get("path"), "path", max_length=2_000)
    if not packet_path:
        raise RequestValidationError("path is required.")
    try:
        result = dependency("packet_attachment_service").attach(job_id, packet_path)
    except ValueError as exc:
        _log_failure("packet_attachment_rejected", "PACKET_ATTACHMENT_REJECTED", "attach", job_id, exc)
        return jsonify({"error": "Invalid or unavailable application packet path."}), 400
    if result is None:
        return jsonify({"error": "Job not found"}), 404
    return jsonify({"job": result["job"], "application_packets": dependency("console_query_service").packets()})


def api_application_packet_content(job_id):
    filename = request.args.get("file", "")
    if not _valid_markdown_filename(filename):
        return jsonify({"error": "Select a Markdown file in the associated packet."}), 400
    job = dependency("console_query_service").job(job_id)
    if not job:
        return jsonify({"error": "Job not found"}), 404
    if not job.get("application_packet_path"):
        return jsonify({"error": "Job does not have an associated application packet."}), 404
    try:
        packet = dependency("packet_content_service").read(job["application_packet_path"], filename)
    except (ValueError, FileNotFoundError) as exc:
        _log_failure("packet_content_path_rejected", "PACKET_CONTENT_PATH_REJECTED", "content", job_id, exc)
        return jsonify({"error": "Markdown file not found."}), 404
    return jsonify(packet)


def api_application_packet_render(job_id):
    filename = request.args.get("file", "")
    if not _valid_markdown_filename(filename):
        return Response("Select a Markdown file in the associated packet.", status=400, mimetype="text/plain")
    job = dependency("console_query_service").job(job_id)
    if not job:
        return Response("Job not found.", status=404, mimetype="text/plain")
    if not job.get("application_packet_path"):
        return Response("Job does not have an associated application packet.", status=404, mimetype="text/plain")
    try:
        packet = dependency("packet_content_service").read(job["application_packet_path"], filename)
    except (ValueError, FileNotFoundError) as exc:
        _log_failure("packet_render_path_rejected", "PACKET_RENDER_PATH_REJECTED", "render", job_id, exc)
        return Response("Markdown file not found.", status=404, mimetype="text/plain")
    return Response(render_packet_page(packet, filename, job), mimetype="text/html")


def register_packet_routes(blueprint: Blueprint) -> None:
    blueprint.add_url_rule(
        "/api/jobs/<int:job_id>/application-packet/generate",
        view_func=api_generate_application_packet,
        methods=["POST"],
    )
    blueprint.add_url_rule(
        "/api/jobs/<int:job_id>/application-packet/attach", view_func=api_attach_application_packet, methods=["POST"]
    )
    blueprint.add_url_rule(
        "/api/jobs/<int:job_id>/application-packet/content", view_func=api_application_packet_content
    )
    blueprint.add_url_rule("/api/jobs/<int:job_id>/application-packet/render", view_func=api_application_packet_render)
