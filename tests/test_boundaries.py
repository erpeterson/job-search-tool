"""Boundary and failure paths not covered elsewhere (T-37)."""

import json
import runpy
import threading

import pytest
from conftest import SCORE_RESPONSE, insert_job, wait_for_task

from job_search.domain.errors import ValidationError
from job_search.domain.jobs import validate_posting_url
from job_search.domain.scheduler import SearchScheduler
from job_search.web import validation as v


def post(client, path, payload=None):
    return client.post(path, data=json.dumps(payload or {}), content_type="application/json")


def test_module_entry_point_exits_with_main_status(monkeypatch):
    monkeypatch.setattr("job_search.cli.main", lambda: 7)
    with pytest.raises(SystemExit) as info:
        runpy.run_module("job_search", run_name="__main__")
    assert info.value.code == 7, f"python -m job_search must exit with main()'s code, got {info.value.code}"


class TestSchedulerThread:
    def test_start_runs_ticks_until_stopped(self, container, monkeypatch):
        scheduler = SearchScheduler(container.db, None, 60, True, poll_seconds=0.01)
        ticked = threading.Event()
        monkeypatch.setattr(scheduler, "tick", ticked.set)
        thread = scheduler.start()
        assert thread is not None, "an enabled scheduler starts a thread"
        assert ticked.wait(2), "the scheduler thread should tick"
        scheduler.stop()
        thread.join(2)
        assert not thread.is_alive(), "stop() must end the scheduler thread"


class TestValidationHelpers:
    @pytest.mark.parametrize(
        ("payload", "code"),
        [({"note": 5}, "field_note_not_string"), ({"note": "x" * 11}, "field_note_too_long")],
    )
    def test_text_rejects_non_strings_and_long_values(self, payload, code):
        with pytest.raises(ValidationError) as info:
            v.text(payload, "note", max_length=10)
        assert info.value.error_code == code, f"expected {code}, got {info.value.error_code}"

    def test_text_none_becomes_empty(self):
        assert v.text({"note": None}, "note") == "", "None is treated as an empty string"

    @pytest.mark.parametrize(
        ("value", "expected"),
        [(True, True), ("1", True), ("true", True), (0, False), ("false", False), ("", False), (None, False)],
    )
    def test_boolean_coercion(self, value, expected):
        assert v.boolean({"flag": value}, "flag") is expected, f"{value!r} should coerce to {expected}"

    def test_boolean_rejects_other_values(self):
        with pytest.raises(ValidationError) as info:
            v.boolean({"flag": "yes"}, "flag")
        assert info.value.error_code == "field_flag_not_boolean", info.value.error_code

    def test_integer_nullable_default_and_required(self):
        assert v.integer({}, "n", 0, 10, nullable=True) is None, "nullable fields may be absent"
        assert v.integer({"n": ""}, "n", 0, 10, default=4) == 4, "blank values fall back to the default"
        with pytest.raises(ValidationError) as info:
            v.integer({}, "n", 0, 10)
        assert info.value.error_code == "field_n_required", info.value.error_code
        with pytest.raises(ValidationError) as info:
            v.integer({"n": True}, "n", 0, 10)
        assert info.value.error_code == "field_n_not_integer", "booleans are not integers"

    def test_job_ids_limits(self):
        with pytest.raises(ValidationError) as info:
            v.job_ids({"job_ids": list(range(1, 1002))})
        assert info.value.error_code == "job_ids_too_many", info.value.error_code

    def test_user_scorecard_must_be_an_object(self, client, container):
        job_id = insert_job(container)
        response = post(client, f"/api/jobs/{job_id}/score-user", {"scorecard": [1, 2]})
        assert response.status_code == 400, f"a list scorecard must be rejected, got {response.status_code}"


@pytest.mark.parametrize(
    ("url", "code"),
    [
        ("http://localhost:5050/", "job_url_local_host"),
        ("http://jobs.localhost/", "job_url_local_host"),
        ("http://[::1]/", "job_url_ipv6_literal"),
        ("http://10.1.2.3/", "job_url_private_address"),
        ("http://127.1/", "job_url_private_address"),
        ("http://192.168.0.1/x", "job_url_private_address"),
        ("javascript:alert(1)", "job_url_invalid_scheme"),
    ],
)
def test_posting_url_rejections(url, code):
    with pytest.raises(ValidationError) as info:
        validate_posting_url(url)
    assert info.value.error_code == code, f"{url}: expected {code}, got {info.value.error_code}"


def test_public_posting_urls_are_allowed():
    for url in ("https://www.linkedin.com/jobs/view/1", "http://8.8.8.8/job"):
        assert validate_posting_url(url) == url, f"{url} should be allowed"


def test_invalid_json_packet_response_fails_the_task(client, container, codex_runner):
    job_id = insert_job(container)
    codex_runner.respond("this is not json")
    response = post(client, f"/api/jobs/{job_id}/application-packet/generate")
    task = wait_for_task(client, response.get_json()["task"])
    assert task["error_code"] == "packet_payload_invalid_json", f"expected invalid-JSON failure: {task}"


def test_packet_repair_with_a_different_model_fails(client, container, codex_runner):
    job_id = insert_job(container)
    weak = {"job_brief_markdown": "# Brief", "cv_markdown": "# Eric Peterson\n\nToo short."}
    codex_runner.respond(weak, model="model-a")
    codex_runner.respond(weak, model="model-b")
    task = wait_for_task(client, post(client, f"/api/jobs/{job_id}/application-packet/generate").get_json()["task"])
    assert task["error_code"] == "packet_model_changed", f"a model switch during repair must fail: {task}"


def test_rescrape_and_delete_failure_paths(client, container):
    no_url = insert_job(container, url=None)
    response = post(client, f"/api/jobs/{no_url}/scrape")
    assert response.status_code == 400, f"rescraping a job without a URL must fail, got {response.status_code}"
    response = client.delete("/api/jobs/999", data=json.dumps({"confirm": "DELETE"}), content_type="application/json")
    assert response.status_code == 404, f"deleting a missing job must be 404, got {response.status_code}"


def test_non_api_http_errors_are_not_json(client):
    response = client.get("/no-such-page")
    assert response.status_code == 404, f"unknown pages are 404, got {response.status_code}"
    assert not response.is_json, "non-API errors keep Werkzeug's HTML error page"


def test_scoring_payload_through_api_is_validated(client, container, codex_runner, enable_scoring):
    job_id = insert_job(container)
    codex_runner.respond({**SCORE_RESPONSE, "total_score": -5})
    task = wait_for_task(client, post(client, f"/api/jobs/{job_id}/score-gpt").get_json()["task"])
    assert task["result"]["raw_score"]["total_score"] == 0, f"negative totals clamp to 0: {task}"
