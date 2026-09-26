"""HTTP API: jobs, companies, settings, config, and top-level error handling."""

import json

from conftest import SCORE_RESPONSE, insert_job

POSTING_HTML = """
<html><head><title>Fallback title</title>
<script type="application/ld+json">{"@type": "JobPosting", "title": "Chief Architect",
 "hiringOrganization": {"name": "Acme"},
 "jobLocation": {"address": {"addressLocality": "Seattle", "addressRegion": "WA"}},
 "description": "<p>Lead architecture.</p>"}</script>
</head><body></body></html>
"""


def post(client, path, payload=None, method="post"):
    return getattr(client, method)(path, data=json.dumps(payload or {}), content_type="application/json")


class TestState:
    def test_state_lists_seeded_searches_and_masks_config(self, client):
        response = client.get("/api/state?include_filtered=1")
        body = response.get_json()
        assert response.status_code == 200
        assert len(body["search_queries"]) == 8, "four pipelines x two boards should be seeded"
        assert body["settings"]["gpt_threshold"] == "40"
        assert body["config"]["JOB_SEARCH_ENABLE_GPT_SCORING"]["masked"] == "********"
        assert body["search_schedule"]["autorun_enabled"] is False
        assert response.headers["X-Request-ID"], "every response carries a correlation id"

    def test_index_serves_ui(self, client):
        response = client.get("/")
        assert response.status_code == 200
        assert b"<!doctype html>" in response.data.lower()

    def test_supplied_request_id_is_echoed(self, client):
        response = client.get("/api/state", headers={"X-Request-ID": "abc-123"})
        assert response.headers["X-Request-ID"] == "abc-123"


class TestCreateJob:
    def test_manual_job_is_scraped_and_auto_scored(self, client, http, codex_runner, enable_scoring):
        http.route("example.com/jobs/1", POSTING_HTML)
        codex_runner.respond(SCORE_RESPONSE)

        response = post(client, "/api/jobs", {"url": "https://example.com/jobs/1", "pipeline": "Wildcards"})

        body = response.get_json()
        assert response.status_code == 201, body
        assert body["score_error"] is None
        job = body["job"]
        assert (job["company"], job["title"], job["location"]) == ("Acme", "Chief Architect", "Seattle, WA")
        assert job["gpt_score"] == 85
        assert job["pipeline"] == "Executive IC", "list pipeline from the model is normalized to its first valid entry"

    def test_scoring_disabled_reports_skip(self, client, http):
        http.route("example.com", POSTING_HTML)
        body = post(client, "/api/jobs", {"url": "https://example.com/jobs/2", "pipeline": "Wildcards"}).get_json()
        assert body["score_error"] == "Automatic Codex scoring skipped: Codex scoring is disabled."

    def test_scrape_failure_falls_back_and_still_saves(self, client, http):
        http.route("example.com", status_code=403)
        response = post(client, "/api/jobs", {"url": "https://www.example.com/jobs/3", "pipeline": "Wildcards"})
        body = response.get_json()
        assert response.status_code == 201
        assert body["job"]["company"] == "example.com"
        assert "403" in body["scrape_error"]

    def test_codex_failure_is_reported_not_fatal(self, client, http, codex_runner, enable_scoring):
        http.route("example.com", POSTING_HTML)
        codex_runner.respond("not json at all")
        body = post(client, "/api/jobs", {"url": "https://example.com/jobs/4", "pipeline": "Wildcards"}).get_json()
        assert body["score_error"] == "Codex CLI response was not valid JSON."
        assert body["job"]["gpt_score"] is None

    def test_duplicate_url_conflicts(self, client, container, http):
        http.route("example.com", POSTING_HTML)
        insert_job(container, url="https://example.com/jobs/5")
        response = post(client, "/api/jobs", {"url": "https://example.com/jobs/5", "pipeline": "Wildcards"})
        assert response.status_code == 409
        assert response.get_json()["job"]["url"] == "https://example.com/jobs/5"

    def test_validation_errors(self, client):
        assert post(client, "/api/jobs", {"pipeline": "Wildcards"}).get_json()["error"] == "URL is required."
        assert post(client, "/api/jobs", {"url": "https://x.com"}).get_json()["error"] == "Pipeline is required."
        response = post(client, "/api/jobs", {"url": "https://x.com", "pipeline": "Bogus"})
        assert response.status_code == 400
        response = post(client, "/api/jobs", {"url": "http://127.0.0.1:5050/api/state", "pipeline": "Wildcards"})
        assert response.status_code == 400, "local addresses must be rejected to prevent SSRF"

    def test_non_object_body_is_rejected(self, client):
        response = client.post("/api/jobs", data="[1, 2]", content_type="application/json")
        assert response.status_code == 400
        assert response.get_json()["error"] == "Request body must be a JSON object."


class TestJobCrm:
    def test_get_missing_job_is_404(self, client):
        response = client.get("/api/jobs/999")
        assert response.status_code == 404
        assert response.get_json()["error"] == "Job not found"

    def test_notes_interactions_and_status(self, client, container):
        job_id = insert_job(container)
        assert post(client, f"/api/jobs/{job_id}/notes", {"note": "Call recruiter"}).status_code == 201
        interaction = {"occurred_on": "2026-09-01", "person_name": "Pat", "summary": "Intro call"}
        assert post(client, f"/api/jobs/{job_id}/interactions", interaction).status_code == 201
        job = post(client, f"/api/jobs/{job_id}/status", {"status": "applied"}).get_json()["job"]
        assert job["status"] == "applied"
        assert job["notes_list"][0]["note"] == "Call recruiter"
        assert job["interactions"][0]["person_name"] == "Pat"

    def test_crm_writes_on_missing_job_are_404(self, client):
        assert post(client, "/api/jobs/999/notes", {"note": "x"}).status_code == 404
        assert post(client, "/api/jobs/999/interactions", {}).status_code == 404
        assert post(client, "/api/jobs/999/status", {"status": "applied"}).status_code == 404

    def test_invalid_status_and_empty_note_are_400(self, client, container):
        job_id = insert_job(container)
        assert post(client, f"/api/jobs/{job_id}/status", {"status": "hired!"}).status_code == 400
        assert post(client, f"/api/jobs/{job_id}/notes", {"note": "  "}).status_code == 400

    def test_user_score_computes_total_and_filters_low_scores(self, client, container):
        job_id = insert_job(container)
        scorecard = {"mission": 10, "compensation": "5", "work_life_balance": 99}
        job = post(client, f"/api/jobs/{job_id}/score-user", {"scorecard": scorecard}).get_json()["job"]
        assert job["user_scorecard"]["work_life_balance"] == 10, "rubric values clamp to 0-10"
        assert job["user_score"] == 31, "total is the rubric sum scaled to 100"
        assert job["filtered"] == 1, "a user score below 60 hides the job"

    def test_user_score_rejects_non_numeric(self, client, container):
        job_id = insert_job(container)
        response = post(client, f"/api/jobs/{job_id}/score-user", {"scorecard": {"mission": "great"}})
        assert response.status_code == 400

    def test_rescrape_updates_posting(self, client, container, http):
        job_id = insert_job(container, url="https://example.com/jobs/9", notes="Original.")
        http.route("example.com/jobs/9", POSTING_HTML)
        body = post(client, f"/api/jobs/{job_id}/scrape", {}).get_json()
        assert body["job"]["company"] == "Acme"
        assert body["job"]["notes"] == "Original. Re-scraped posting URL."

    def test_delete_requires_confirmation(self, client, container):
        job_id = insert_job(container)
        assert post(client, f"/api/jobs/{job_id}", {"confirm": "nope"}, method="delete").status_code == 400
        response = post(client, f"/api/jobs/{job_id}", {"confirm": "DELETE"}, method="delete")
        assert response.get_json()["deleted_job_id"] == job_id
        assert client.get(f"/api/jobs/{job_id}").status_code == 404

    def test_purge_requires_confirmation(self, client, container):
        insert_job(container)
        assert post(client, "/api/admin/purge-jobs", {}).status_code == 400
        assert post(client, "/api/admin/purge-jobs", {"confirm": "PURGE"}).get_json()["deleted_jobs"] == 1

    def test_score_gpt_requires_enabled_scoring(self, client, container):
        job_id = insert_job(container)
        response = post(client, f"/api/jobs/{job_id}/score-gpt")
        assert response.status_code == 409
        assert "disabled" in response.get_json()["error"]

    def test_score_gpt_persists_score(self, client, container, codex_runner, enable_scoring):
        job_id = insert_job(container)
        codex_runner.respond({**SCORE_RESPONSE, "total_score": 20, "downlevel": True})
        body = post(client, f"/api/jobs/{job_id}/score-gpt").get_json()
        assert body["job"]["gpt_score"] == 20
        assert body["job"]["filtered"] == 1, "downlevel or low Codex score hides the job"


class TestCompanies:
    def test_create_update_and_get(self, client, container):
        insert_job(container, company="Acme")
        created = post(client, "/api/companies", {"company": "Acme", "interest_score": "80", "status": "target"})
        company = created.get_json()["company"]
        assert created.status_code == 201
        assert (company["interest_score"], len(company["jobs"])) == (80, 1)

        updated = post(client, f"/api/companies/{company['id']}", {"notes": "Warm intro"}).get_json()["company"]
        assert updated["notes"] == "Warm intro"
        assert updated["status"] == "target", "omitted fields are preserved on update"
        assert client.get(f"/api/companies/{company['id']}").status_code == 200

    def test_validation_and_not_found(self, client):
        assert post(client, "/api/companies", {"company": "A", "interest_score": 101}).status_code == 400
        assert post(client, "/api/companies", {"company": "A", "status": "bogus"}).status_code == 400
        assert client.get("/api/companies/42").status_code == 404
        assert post(client, "/api/companies/42", {}).status_code == 404


class TestSettingsAndConfig:
    def test_threshold_update_refilters_jobs(self, client, container):
        insert_job(container, user_score=65)
        body = post(client, "/api/settings", {"user_threshold": "70", "codex_model": "m"}).get_json()
        assert body["settings"]["user_threshold"] == "70"
        assert body["jobs"][0]["filtered"] == 1, "raising the threshold hides the 65-scored job"

    def test_threshold_must_be_integer_in_range(self, client):
        assert post(client, "/api/settings", {"gpt_threshold": "abc"}).status_code == 400
        assert post(client, "/api/settings", {"gpt_threshold": 101}).status_code == 400

    def test_config_update_persists_to_env_file(self, client, container, environ):
        body = post(client, "/api/config", {"JOB_SEARCH_ENABLE_GPT_SCORING": "1", "CODEX_MODEL": "gpt-x"}).get_json()
        assert body["gpt_scoring_enabled"] is True
        assert body["settings"]["codex_model"] == "gpt-x"
        assert environ["CODEX_MODEL"] == "gpt-x"
        env_text = container.config.env_path.read_text()
        assert "JOB_SEARCH_ENABLE_GPT_SCORING=1" in env_text and "CODEX_MODEL=gpt-x" in env_text

    def test_config_without_updates_returns_current_config(self, client):
        body = post(client, "/api/config", {}).get_json()
        assert "settings" not in body and "config" in body

    def test_config_rejects_env_injection(self, client, container):
        response = post(client, "/api/config", {"CODEX_CLI_PATH": "codex\nJOB_SEARCH_AUTORUN=1"})
        assert response.status_code == 400
        assert not container.config.env_path.exists(), "nothing is written when validation fails"
        assert post(client, "/api/config", {"JOB_SEARCH_USE_CAPTURE_CACHE": "yes"}).status_code == 400


class TestErrorHandling:
    def test_unexpected_errors_do_not_leak_details(self, client, container, monkeypatch):
        def explode(*_args, **_kwargs):
            raise RuntimeError("secret path /Users/me/private")

        monkeypatch.setattr(container.jobs, "list", explode)
        response = client.get("/api/state")
        body = response.get_json()
        assert response.status_code == 500
        assert "secret" not in body["error"], "internal details must stay in logs"
        assert body["request_id"] in body["error"]

    def test_unknown_api_route_is_json_404(self, client):
        response = client.get("/api/nope")
        assert response.status_code == 404
        assert "error" in response.get_json()

    def test_metrics_count_blame_events(self, client):
        client.get("/api/jobs/999")
        counters = client.get("/api/metrics").get_json()["counters"]
        assert counters.get("blame.job_not_found", 0) >= 1
