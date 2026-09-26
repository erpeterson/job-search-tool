"""Major operations emit start, success, and failure events."""

import io
import json

import pytest
from conftest import insert_job

from job_search.domain.errors import ValidationError
from job_search.observability import configure_console_logging


@pytest.fixture
def events():
    out = io.StringIO()
    configure_console_logging(verbose=True, stdout=out, stderr=out)
    yield lambda: [json.loads(line) for line in out.getvalue().splitlines()]
    configure_console_logging(verbose=False)


def names(event_list):
    return [event["event"] for event in event_list]


def test_create_manual_emits_started_and_failed(container, events):
    with pytest.raises(ValidationError):
        container.jobs.create_manual({"url": "http://localhost/x", "pipeline": "Wildcards"})
    emitted = names(events())
    assert "manual_job_create_started" in emitted, f"missing start event: {emitted}"
    assert "manual_job_create_failed" in emitted, f"missing failure event: {emitted}"
    assert any(e.get("error_code") == "manual_job_create_failed" for e in events()), "failure needs a blame record"


def test_purge_all_emits_started_succeeded_and_failed(container, events, monkeypatch):
    insert_job(container)
    container.jobs.purge_all()
    emitted = names(events())
    assert {"jobs_purge_started", "jobs_purge_succeeded"} <= set(emitted), f"missing purge events: {emitted}"

    def broken_unit_of_work():
        raise RuntimeError("db locked")

    monkeypatch.setattr(container.db, "unit_of_work", broken_unit_of_work)
    with pytest.raises(RuntimeError):
        container.jobs.purge_all()
    assert "jobs_purge_failed" in names(events()), "purge failure must emit a failed event"


def test_app_stopped_is_emitted_when_serve_fails(workspace, capsys):
    from job_search import cli

    def boom(app, config):
        raise RuntimeError("port in use")

    code = cli.main(["-v"], environ={}, serve=boom, out=io.StringIO(), app_dir=workspace / "job-search-tool")
    configure_console_logging(verbose=False)
    lines = [json.loads(line) for line in capsys.readouterr().out.splitlines() if line.startswith("{")]
    stopped = [e for e in lines if e["event"] == "app_stopped"]
    assert code == cli.EXIT_FAILURE, f"expected failure exit code, got {code}"
    assert stopped and stopped[-1]["outcome"] == "error", f"app_stopped must report the outcome: {stopped}"
