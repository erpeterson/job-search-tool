"""Codex output schema validation, unit-level and through the scoring and search flows."""

import io
import json

import pytest
from conftest import SCORE_RESPONSE, insert_job, wait_for_task

from job_search.domain.errors import ExternalServiceError
from job_search.domain.model_output import validate_refinement_payload, validate_score_payload
from job_search.observability import METRICS, configure_console_logging


def blame(code):
    return METRICS.snapshot().get(f"blame.{code}", 0)


class TestValidateScorePayload:
    def test_normalizes_ranges_types_and_text(self):
        result = validate_score_payload(
            {
                "total_score": "250",
                "scorecard": {"mission": 14, "compensation": "-3", "made_up": 5},
                "downlevel": "false",
                "rationale": "x" * 5000,
                "strengths": ["good", 3, "  "],
            }
        )
        assert result["total_score"] == 100, "total is clamped to 0-100"
        assert result["scorecard"] == {"mission": 10, "compensation": 0}, "values clamp and unknown keys drop"
        assert result["downlevel"] is False, "string booleans are accepted"
        assert len(result["rationale"]) == 4000, "text is truncated to the documented limit"
        assert result["strengths"] == ["good"], "non-string list items are dropped"

    @pytest.mark.parametrize(
        ("payload", "code"),
        [
            (["not", "a", "dict"], "codex_score_not_object"),
            ({"scorecard": {}}, "codex_score_total_missing"),
            ({"total_score": "high"}, "codex_score_total_invalid"),
            ({"total_score": True}, "codex_score_total_invalid"),
            ({"total_score": 50, "scorecard": "great"}, "codex_scorecard_not_object"),
            ({"total_score": 50, "scorecard": {"mission": [1]}}, "codex_scorecard_value_invalid"),
            ({"total_score": 50, "downlevel": "maybe"}, "codex_score_downlevel_invalid"),
            ({"total_score": 50, "rationale": {"a": 1}}, "codex_score_text_invalid"),
        ],
    )
    def test_unusable_payloads_are_rejected(self, payload, code):
        with pytest.raises(ExternalServiceError) as info:
            validate_score_payload(payload)
        assert info.value.error_code == code, f"expected {code}, got {info.value.error_code}"


class TestValidateRefinementPayload:
    def test_missing_fields_become_empty(self):
        assert validate_refinement_payload({"keywords": " a b "}) == {
            "keywords": "a b",
            "location": "",
            "criteria": "",
            "refinement_notes": "",
        }

    @pytest.mark.parametrize(
        ("payload", "code"),
        [("text", "refinement_not_object"), ({"keywords": ["a"]}, "refinement_keywords_invalid")],
    )
    def test_invalid_refinements_are_rejected(self, payload, code):
        with pytest.raises(ExternalServiceError) as info:
            validate_refinement_payload(payload)
        assert info.value.error_code == code, f"expected {code}, got {info.value.error_code}"


class TestFlows:
    def test_non_dict_scorecard_is_recorded_and_job_still_saved(self, client, http, codex_runner, enable_scoring):
        http.route("example.com", "<html><h1>Chief Architect</h1></html>")
        codex_runner.respond({**SCORE_RESPONSE, "scorecard": "excellent"})
        before = blame("codex_auto_score_task_failed")
        response = client.post(
            "/api/jobs",
            data=json.dumps({"url": "https://example.com/jobs/77", "pipeline": "Wildcards"}),
            content_type="application/json",
        )
        body = response.get_json()
        assert response.status_code == 201, f"job must still be saved: {body}"
        task = wait_for_task(client, body["score_task"])
        assert task["message"] == "Codex scorecard must be a JSON object.", task["message"]
        assert task["error_code"] == "codex_scorecard_not_object", task
        job = client.get(f"/api/jobs/{body['job']['id']}").get_json()["job"]
        assert job["gpt_score"] is None, "nothing from the invalid payload is persisted"
        assert blame("codex_auto_score_task_failed") == before + 1, "the failure must be recorded"

    def test_out_of_range_total_is_clamped_and_logged(self, container, codex_runner, enable_scoring):
        job_id = insert_job(container)
        codex_runner.respond({**SCORE_RESPONSE, "total_score": 250})
        out = io.StringIO()
        configure_console_logging(verbose=True, stdout=out, stderr=io.StringIO())
        try:
            container.scoring.populate_score(job_id)
        finally:
            configure_console_logging(verbose=False)
        assert container.jobs.get(job_id)["gpt_score"] == 100, "total_score is clamped to 100"
        events = [json.loads(line) for line in out.getvalue().splitlines()]
        assert any(e["event"] == "codex_output_normalized" and e["field"] == "total_score" for e in events), (
            "clamping must be logged"
        )

    def test_list_keywords_refinement_is_recorded_and_non_fatal(
        self, client, container, http, codex_runner, enable_scoring
    ):
        with container.db.unit_of_work() as uow:
            uow.connection.execute("UPDATE search_queries SET enabled = 0")
            uow.connection.execute(
                "UPDATE search_queries SET enabled = 1 WHERE board = 'linkedin' AND pipeline = 'Wildcards'"
            )
        before_keywords = next(q for q in container.search.list_queries() if q["enabled"])["keywords"]
        http.route(
            "linkedin.com/jobs-guest",
            '<ul><li><a class="base-card__full-link" href="https://www.linkedin.com/jobs/view/9">x</a>'
            "<h3>Chief Architect</h3><span class='job-search-card__location'>Seattle</span></li></ul>",
        )
        codex_runner.respond(SCORE_RESPONSE)
        codex_runner.respond({"keywords": ["a"]})
        before = blame("search_query_refinement_failed")

        response = client.post("/api/search/run", data="{}", content_type="application/json")

        task = wait_for_task(client, response.get_json()["task"])
        run = task["result"]["run"]
        assert (response.status_code, run["status"]) == (202, "complete"), f"run must complete: {run}"
        assert blame("search_query_refinement_failed") == before + 1, "the invalid refinement must be recorded"
        after_keywords = next(q for q in container.search.list_queries() if q["enabled"])["keywords"]
        assert after_keywords == before_keywords, "the query is unchanged when refinement output is invalid"
