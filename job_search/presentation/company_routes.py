"""Company-interest request validation and HTTP response mapping."""

from flask import Blueprint, jsonify, request

from job_search.presentation.read_routes import dependency
from job_search.validation import choice, integer, optional_text, require_json_object

COMPANY_STATUSES = {"watching", "target", "active_conversation", "paused", "not_interested"}


def api_create_company_interest():
    payload = require_json_object(request.get_json(silent=True) or {})
    company_name = optional_text(payload.get("company", ""), "company", max_length=300) or "Unknown company"
    interest_score = payload.get("interest_score")
    if interest_score not in (None, ""):
        interest_score = integer(interest_score, "interest_score", minimum=0, maximum=100)
    status = choice(payload.get("status", "watching"), "status", COMPANY_STATUSES, required=True)
    company_id = dependency("company_service").save(
        {
            "company": company_name,
            "status": status,
            "interest_score": interest_score if interest_score != "" else None,
            "rationale": optional_text(payload.get("rationale", ""), "rationale", max_length=20_000),
            "notes": optional_text(payload.get("notes", ""), "notes", max_length=20_000),
            "next_step": optional_text(payload.get("next_step", ""), "next_step", max_length=2_000),
            "contacts": optional_text(payload.get("contacts", ""), "contacts", max_length=10_000),
        }
    )
    reads = dependency("console_query_service")
    return jsonify({"company": reads.company(company_id), "companies": reads.companies()}), 201


def api_update_company_interest(company_id):
    payload = require_json_object(request.get_json(silent=True) or {})
    reads = dependency("console_query_service")
    existing = reads.company(company_id)
    if not existing:
        return jsonify({"error": "Company interest not found"}), 404
    company_name = (
        optional_text(payload.get("company", existing["company"]), "company", max_length=300) or existing["company"]
    )
    interest_score = payload.get("interest_score")
    if interest_score not in (None, ""):
        interest_score = integer(interest_score, "interest_score", minimum=0, maximum=100)
    status = choice(payload.get("status", existing["status"]), "status", COMPANY_STATUSES, required=True)
    dependency("company_service").update(
        company_id,
        {
            "company": company_name,
            "status": status,
            "interest_score": interest_score if interest_score != "" else None,
            "rationale": optional_text(payload.get("rationale", ""), "rationale", max_length=20_000),
            "notes": optional_text(payload.get("notes", ""), "notes", max_length=20_000),
            "next_step": optional_text(payload.get("next_step", ""), "next_step", max_length=2_000),
            "contacts": optional_text(payload.get("contacts", ""), "contacts", max_length=10_000),
        },
    )
    return jsonify({"company": reads.company(company_id), "companies": reads.companies()})


def register_company_routes(blueprint: Blueprint) -> None:
    blueprint.add_url_rule("/api/companies", view_func=api_create_company_interest, methods=["POST"])
    blueprint.add_url_rule("/api/companies/<int:company_id>", view_func=api_update_company_interest, methods=["POST"])
