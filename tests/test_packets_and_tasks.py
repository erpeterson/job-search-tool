"""Application packets (generate, attach, read, render) and bulk background tasks."""

import json

import pytest
from conftest import SCORE_RESPONSE, FakePandoc, insert_job, valid_cv, wait_for_task

MODEL = "gpt-test"


def packet_response(cv=None):
    return {"job_brief_markdown": "# Brief\nBrief body.", "cv_markdown": cv or valid_cv()}


def post(client, path, payload=None):
    return client.post(path, data=json.dumps(payload or {}), content_type="application/json")


def generate(client, job_id):
    """Start packet generation and return the finished task (tasks run synchronously in tests)."""
    response = post(client, f"/api/jobs/{job_id}/application-packet/generate")
    assert response.status_code == 202, f"generation should start as a task: {response.get_json()}"
    return wait_for_task(client, response.get_json()["task"])


class TestPacketGeneration:
    def test_generate_writes_markdown_and_docx(self, client, container, codex_runner, pandoc, workspace):
        job_id = insert_job(container, source_job_id="linkedin:1")
        codex_runner.respond(packet_response())

        task = generate(client, job_id)

        assert task["status"] == "complete", f"generation should succeed: {task}"
        packet = task["result"]["packet"]
        packet_dir = workspace / packet["path"]
        files = sorted(p.name for p in packet_dir.iterdir())
        assert files == ["CV.docx", "CV.md", "Job-Brief.md"], f"the CV (only) gets a DOCX twin; got {files}"
        job = client.get(f"/api/jobs/{job_id}").get_json()["job"]
        assert job["application_packet_path"] == packet["path"], "the packet is associated with the job"
        prompt = codex_runner.calls[0]["input"]
        assert "Use evidence only." in prompt, "packet rules from the Career Manual are included"
        assert "--sandbox" in codex_runner.calls[0]["command"], (
            f"expected '--sandbox' in {codex_runner.calls[0]['command']!r}"
        )
        assert not any(p.name.startswith(".packet-staging") for p in (workspace / "applications").iterdir()), (
            "expected not any((p.name.startswith('.packet-staging') for p in (workspace / 'applicat..."
        )

    def test_model_attribution_is_added_by_the_app(self, client, container, codex_runner, workspace):
        job_id = insert_job(container)
        codex_runner.respond(packet_response(valid_cv("\n## AI Generation Attribution\n\nmodel-made-up")), model=MODEL)
        task = generate(client, job_id)
        assert task["status"] == "complete", f"generation should succeed: {task}"
        packet_dir = workspace / task["result"]["packet"]["path"]
        for name in ("CV.md", "Job-Brief.md"):
            text = (packet_dir / name).read_text(encoding="utf-8")
            assert f"using `{MODEL}`" in text, f"{name} should name the invoked model"
            assert text.count("## AI Generation Attribution") == 1, f"{name} keeps one attribution section"
            assert "model-made-up" not in text, "model-written attribution is replaced"
        assert len(codex_runner.calls) == 1, "a valid CV needs no repair call"

    def test_a_weak_cv_is_repaired_once(self, client, container, codex_runner):
        job_id = insert_job(container)
        codex_runner.respond(packet_response("# Eric Peterson\n\nToo short."), model=MODEL)
        codex_runner.respond(packet_response(), model=MODEL)
        task = generate(client, job_id)
        assert task["status"] == "complete", f"the repair should succeed: {task}"
        repair_prompt = codex_runner.calls[1]["input"]
        assert "Structural issues to fix" in repair_prompt, "the repair prompt lists what failed"
        assert "-m" in codex_runner.calls[1]["command"], "repair pins the model reported by the first call"

    def test_fails_when_repaired_cv_still_weak(self, client, container, codex_runner, workspace):
        job_id = insert_job(container)
        codex_runner.respond(packet_response("# Eric Peterson\n\nToo short."))
        codex_runner.respond(packet_response("# Eric Peterson\n\nStill too short."))
        task = generate(client, job_id)
        assert task["error_code"] == "packet_cv_quality_failed", f"generation should fail: {task}"
        assert list((workspace / "applications").glob("*")) == [], "no partial packet is published"

    def test_fails_when_model_unreported(self, client, container, codex_runner):
        job_id = insert_job(container)
        codex_runner.respond(packet_response(), model="")
        task = generate(client, job_id)
        assert task["error_code"] == "packet_model_unreported", f"unexpected outcome: {task}"

    def test_pandoc_failure_publishes_nothing(self, config, environ, http, codex_runner, workspace):
        from conftest import FakeResolver, ImmediateThread, make_client

        from job_search.container import build_container

        container = build_container(
            config,
            environ=environ,
            http_get=http,
            codex_runner=codex_runner,
            pandoc=FakePandoc(fail_on="CV.md"),
            thread_factory=ImmediateThread,
            resolve_host=FakeResolver(),
        )
        container.bootstrap()
        client = make_client(container)
        job_id = insert_job(container)
        codex_runner.respond(packet_response())
        task = generate(client, job_id)
        assert task["status"] == "error", f"generation should fail: {task}"
        assert "Pandoc failed for CV.md" in task["message"], task["message"]
        assert list((workspace / "applications").glob("*")) == [], "no partial packet is published"

    def test_generate_preconditions(self, client, container, environ):
        assert post(client, "/api/jobs/999/application-packet/generate").status_code == 404, (
            "expected post(...).status_code to be 404"
        )
        no_url = insert_job(container, url=None)
        assert post(client, f"/api/jobs/{no_url}/application-packet/generate").status_code == 400, (
            "expected post(...).status_code to be 400"
        )
        attached = insert_job(container, url="https://example.com/2", application_packet_path="applications/x")
        assert post(client, f"/api/jobs/{attached}/application-packet/generate").status_code == 409, (
            "expected post(...).status_code to be 409"
        )
        container.runtime.update({"CODEX_CLI_PATH": "/nonexistent/codex"})
        fresh = insert_job(container, url="https://example.com/3")
        response = post(client, f"/api/jobs/{fresh}/application-packet/generate")
        assert response.status_code == 503, (
            f"missing Codex CLI should be 503, got {response.status_code}: {response.get_data(as_text=True)[:200]}"
        )
        assert "unavailable" in response.get_json()["error"], (
            f"expected 'unavailable' in {response.get_json()['error']!r}"
        )


class TestPacketFiles:
    def make_packet(self, workspace, name="2026-09-acme"):
        packet_dir = workspace / "applications" / name
        packet_dir.mkdir(parents=True)
        (packet_dir / "Resume.md").write_text("# Resume\n- <script>x</script>\n", encoding="utf-8")
        return f"applications/{name}"

    def test_attach_list_read_and_render(self, client, container, workspace):
        job_id = insert_job(container)
        path = self.make_packet(workspace)
        listed = client.get("/api/application-packets").get_json()["application_packets"]
        assert listed[0]["unassociated"] is True, f"expected True, got {listed[0]['unassociated']!r}"

        attached = post(client, f"/api/jobs/{job_id}/application-packet/attach", {"path": path}).get_json()
        assert attached["job"]["application_packet_path"] == path, (
            f"expected path, got {attached['job']['application_packet_path']!r}"
        )
        assert attached["application_packets"][0]["associated_job"]["id"] == job_id, (
            "expected attached['application_packets'][0][...][...] to be job_id"
        )

        content = client.get(f"/api/jobs/{job_id}/application-packet/content?file=Resume.md").get_json()
        assert content["content"].startswith("# Resume"), "expected content['content'].startswith('# Resume')"
        assert content["markdown_files"] == ["Resume.md"], f"expected ['Resume.md'], got {content['markdown_files']!r}"

        rendered = client.get(f"/api/jobs/{job_id}/application-packet/render?file=Resume.md")
        html = rendered.get_data(as_text=True)
        assert rendered.status_code == 200, (
            f"expected HTTP 200, got {rendered.status_code}: {rendered.get_data(as_text=True)[:200]}"
        )
        assert "<h1>Resume</h1>" in html, f"expected '<h1>Resume</h1>' in {html!r}"
        assert "<script>" not in html, "packet Markdown is escaped"

    def test_attach_rejects_paths_outside_applications(self, client, container, workspace):
        job_id = insert_job(container)
        response = post(client, f"/api/jobs/{job_id}/application-packet/attach", {"path": "../../etc"})
        assert response.status_code == 400, (
            f"expected HTTP 400, got {response.status_code}: {response.get_data(as_text=True)[:200]}"
        )
        assert post(client, f"/api/jobs/{job_id}/application-packet/attach", {"path": ""}).status_code == 400, (
            "expected post(...).status_code to be 400"
        )
        assert post(client, "/api/jobs/999/application-packet/attach", {"path": "x"}).status_code == 404, (
            "expected post(...).status_code to be 404"
        )

    def test_read_rejects_traversal_and_missing_files(self, client, container, workspace):
        path = self.make_packet(workspace)
        job_id = insert_job(container, application_packet_path=path)
        assert client.get(f"/api/jobs/{job_id}/application-packet/content?file=../x.md").status_code == 400, (
            "expected client.get(...).status_code to be 400"
        )
        assert client.get(f"/api/jobs/{job_id}/application-packet/render?file=notes.txt").status_code == 400, (
            "expected client.get(...).status_code to be 400"
        )
        assert client.get(f"/api/jobs/{job_id}/application-packet/content?file=Nope.md").status_code == 404, (
            "expected client.get(...).status_code to be 404"
        )
        unattached = insert_job(container, url="https://example.com/9")
        assert client.get(f"/api/jobs/{unattached}/application-packet/content?file=Resume.md").status_code == 404, (
            "expected client.get(...).status_code to be 404"
        )
        assert client.get("/api/jobs/999/application-packet/content?file=Resume.md").status_code == 404, (
            "expected client.get(...).status_code to be 404"
        )


class TestBulkTasks:
    def test_bulk_scoring_reports_per_item_results(self, client, container, codex_runner, enable_scoring):
        good = insert_job(container)
        bad = insert_job(container, url="https://example.com/2")
        codex_runner.respond(SCORE_RESPONSE)
        codex_runner.respond("", returncode=1)

        response = post(client, "/api/jobs/bulk/score-gpt", {"job_ids": [good, str(bad), good, 999]})

        task = response.get_json()["task"]
        assert response.status_code == 202, (
            f"expected HTTP 202, got {response.status_code}: {response.get_data(as_text=True)[:200]}"
        )
        task = client.get(f"/api/codex-tasks/{task['id']}").get_json()["task"]
        assert (task["status"], task["completed"], task["failed"], task["skipped"]) == ("error", 1, 1, 1), (
            "expected the result to be ('error', 1, 1, 1)"
        )
        messages = {item["job_id"]: item["message"] for item in task["items"]}
        assert messages[good] == "Codex score 85", f"expected 'Codex score 85', got {messages[good]!r}"
        assert "exited with code 1" in messages[bad], f"expected 'exited with code 1' in {messages[bad]!r}"
        assert messages[999] == "Job not found", f"expected 'Job not found', got {messages[999]!r}"
        assert client.get("/api/codex-tasks").get_json()["tasks"][0]["id"] == task["id"], (
            "expected client.get(...).get_json(...)[...][...][...] to be task['id']"
        )

    def test_bulk_packets_skip_already_associated(self, client, container, codex_runner):
        attached = insert_job(container, application_packet_path="applications/x")
        fresh = insert_job(container, url="https://example.com/2")
        codex_runner.respond(packet_response())
        task = post(client, "/api/jobs/bulk/application-packets/generate", {"job_ids": [attached, fresh]}).get_json()
        task = client.get(f"/api/codex-tasks/{task['task']['id']}").get_json()["task"]
        assert (task["status"], task["completed"], task["skipped"]) == ("complete", 1, 1), (
            "expected the result to be ('complete', 1, 1)"
        )

    def test_bulk_validation(self, client, enable_scoring):
        assert post(client, "/api/jobs/bulk/score-gpt", {"job_ids": []}).status_code == 400, (
            "expected post(...).status_code to be 400"
        )
        assert post(client, "/api/jobs/bulk/score-gpt", {"job_ids": "1"}).status_code == 400, (
            "expected post(...).status_code to be 400"
        )
        assert post(client, "/api/jobs/bulk/score-gpt", {"job_ids": ["x"]}).status_code == 400, (
            "expected post(...).status_code to be 400"
        )
        assert client.get("/api/codex-tasks/unknown").status_code == 404, (
            "expected client.get(...).status_code to be 404"
        )

    def test_bulk_scoring_requires_enabled_scoring(self, client):
        response = post(client, "/api/jobs/bulk/score-gpt", {"job_ids": [1]})
        assert response.status_code == 503, f"disabled scoring should return 503, got {response.status_code}"

    def test_unexpected_item_error_is_generic(self, container, monkeypatch, enable_scoring):
        job_id = insert_job(container)

        def crash(_job_id):
            raise RuntimeError("internal detail")

        monkeypatch.setattr(container.scoring, "populate_score", crash)
        task = container.bulk.start_scoring([job_id])
        item = container.tasks.get(task["id"])["items"][0]
        assert item["status"] == "error", f"expected 'error', got {item['status']!r}"
        assert "internal detail" not in item["message"], f"expected 'internal detail' not in {item['message']!r}"

    def test_registry_evicts_oldest_finished_tasks(self):
        from conftest import ImmediateThread

        from job_search.domain.tasks import BackgroundTaskRegistry

        registry = BackgroundTaskRegistry(max_retained=2, thread_factory=ImmediateThread)

        def finish(task_id, _job_ids):
            registry.update(task_id, status="complete")

        ids = [registry.start("op", [1], finish)["id"] for _ in range(4)]
        remaining = {task["id"] for task in registry.list()}
        assert ids[-1] in remaining and len(remaining) <= 3, "finished tasks beyond the cap are evicted"
        assert registry.update("missing") is None, "expected registry.update('missing') to be None"
        assert registry.update_item("missing", 1) is None, "expected registry.update_item('missing', 1) to be None"


class TestBackgroundCrashHandling:
    def test_worker_crash_marks_task_error_and_records_blame(self, container, monkeypatch, enable_scoring):
        from job_search.observability import METRICS

        job_id = insert_job(container)
        original_update = container.tasks.update

        def failing_update(task_id, **updates):
            if updates.get("status") == "running":
                raise RuntimeError("registry broken")
            return original_update(task_id, **updates)

        monkeypatch.setattr(container.tasks, "update", failing_update)
        before = METRICS.snapshot().get("blame.background_task_crashed", 0)
        task = container.bulk.start_scoring([job_id])
        final = container.tasks.get(task["id"])
        assert final["status"] == "error", f"a crashed worker must end the task in error: {final}"
        assert "registry broken" not in final["message"], "internal details stay in logs"
        assert METRICS.snapshot()["blame.background_task_crashed"] == before + 1, "crash must be recorded"

    def test_scheduler_loop_survives_tick_crash(self, container, monkeypatch):
        from job_search.domain.scheduler import SearchScheduler
        from job_search.observability import METRICS

        scheduler = SearchScheduler(container.db, None, 60, True, poll_seconds=0)
        calls = []

        def crashing_tick():
            calls.append(1)
            if len(calls) == 2:
                scheduler.stop()
            raise RuntimeError("tick blew up")

        monkeypatch.setattr(scheduler, "tick", crashing_tick)
        before = METRICS.snapshot().get("blame.scheduler_loop_crashed", 0)
        scheduler._loop()
        assert len(calls) == 2, "the loop keeps polling after a crash"
        assert METRICS.snapshot()["blame.scheduler_loop_crashed"] == before + 2, "each crash is recorded"

    def test_thread_excepthook_records_unhandled_exceptions(self, monkeypatch):
        import threading

        from job_search.observability import METRICS, install_thread_excepthook

        monkeypatch.setattr(threading, "excepthook", threading.excepthook)
        install_thread_excepthook()
        before = METRICS.snapshot().get("blame.thread_unhandled_exception", 0)

        def boom():
            raise ValueError("unhandled in thread")

        thread = threading.Thread(target=boom, name="test-thread")
        thread.start()
        thread.join()
        after = METRICS.snapshot().get("blame.thread_unhandled_exception", 0)
        assert after == before + 1, "uncaught thread exceptions must be recorded"


def test_pandoc_failure_response_omits_stderr(config, environ, http, codex_runner):
    import subprocess

    from conftest import FakeResolver, ImmediateThread, make_client

    from job_search.container import build_container
    from job_search.data.packet_store import PandocConverter

    failing = PandocConverter(
        which=lambda _name: "/usr/bin/pandoc",
        runner=lambda *a, **k: subprocess.CompletedProcess(a, 1, stdout="", stderr="SECRET /Users/me/tmp stderr"),
    )
    container = build_container(
        config,
        environ=environ,
        http_get=http,
        codex_runner=codex_runner,
        pandoc=failing,
        thread_factory=ImmediateThread,
        resolve_host=FakeResolver(),
    )
    container.bootstrap()
    job_id = insert_job(container)
    codex_runner.respond(packet_response())
    task = generate(make_client(container), job_id)
    assert task["error_code"] == "pandoc_conversion_failed", f"pandoc failure expected: {task}"
    assert "SECRET" not in json.dumps(task), f"stderr must not be returned to the client: {task}"


def test_codex_unavailable_message_omits_cli_path(client, container, environ):
    container.runtime.update({"CODEX_CLI_PATH": "/secret/location/codex"})
    job_id = insert_job(container)
    body = post(client, f"/api/jobs/{job_id}/application-packet/generate").get_json()
    assert "/secret/location" not in body["error"], f"the CLI path must not be returned: {body}"


class TestTaskConcurrency:
    class DeferredThread:
        """Never runs the worker, so tasks stay queued."""

        def __init__(self, target, args=(), daemon=None, name=None):
            pass

        def start(self):
            pass

    def registry(self):
        from job_search.domain.tasks import BackgroundTaskRegistry

        return BackgroundTaskRegistry(max_running=2, thread_factory=self.DeferredThread)

    def test_third_running_task_is_rejected(self):
        from job_search.domain.errors import CapacityError

        registry = self.registry()
        registry.start("op", [1], lambda *_: None)
        registry.start("op", [2], lambda *_: None)
        with pytest.raises(CapacityError) as info:
            registry.start("op", [3], lambda *_: None)
        assert info.value.error_code == "background_task_capacity_reached", info.value.error_code

    def test_job_already_in_a_task_is_rejected(self):
        from job_search.domain.errors import ConflictError

        registry = self.registry()
        registry.start("op", [1, 2], lambda *_: None)
        with pytest.raises(ConflictError) as info:
            registry.start("op", [2, 3], lambda *_: None)
        assert "2" in info.value.message and info.value.error_code == "background_task_job_busy", info.value.message

    def test_capacity_error_maps_to_429(self, client, container, monkeypatch, enable_scoring):
        from job_search.domain.errors import CapacityError

        def full(*_args, **_kwargs):
            raise CapacityError("2 background tasks are already running.", "background_task_capacity_reached")

        monkeypatch.setattr(container.tasks, "start", full)
        response = post(client, "/api/search/run")
        assert response.status_code == 429, f"capacity errors must return 429: {response.get_json()}"

    def test_manual_add_still_saves_when_task_capacity_is_full(
        self, client, container, http, monkeypatch, enable_scoring
    ):
        from job_search.domain.errors import CapacityError

        http.route("example.com", "<html><h1>Architect</h1></html>")

        def full(*_args, **_kwargs):
            raise CapacityError("2 background tasks are already running.", "background_task_capacity_reached")

        monkeypatch.setattr(container.tasks, "start", full)
        response = post(client, "/api/jobs", {"url": "https://example.com/jobs/cap", "pipeline": "Wildcards"})
        body = response.get_json()
        assert response.status_code == 201, f"the job must still be saved: {body}"
        assert body["score_task"] is None and "already running" in body["score_error"], body


def test_failed_thread_start_releases_the_slot():
    from job_search.domain.errors import TaskStartError
    from job_search.domain.tasks import BackgroundTaskRegistry
    from job_search.observability import METRICS

    attempts = []

    class FlakyThread:
        def __init__(self, target, args=(), daemon=None, name=None):
            self.target, self.args = target, args

        def start(self):
            attempts.append(1)
            if len(attempts) <= 2:
                raise RuntimeError("can't start new thread")
            self.target(*self.args)

    registry = BackgroundTaskRegistry(max_running=2, thread_factory=FlakyThread)
    before = METRICS.snapshot().get("blame.background_task_thread_start_failed", 0)
    for _ in range(2):
        with pytest.raises(TaskStartError):
            registry.start("op", [], lambda *_: None)
    statuses = [task["status"] for task in registry.list()]
    assert statuses == ["error", "error"], f"failed starts must end in error: {statuses}"
    assert METRICS.snapshot()["blame.background_task_thread_start_failed"] == before + 2, "each failure is recorded"
    task = registry.start("op", [], lambda task_id, _ids: registry.update(task_id, status="complete"))
    assert registry.get(task["id"])["status"] == "complete", "a later task can still start"


def test_thread_start_failure_returns_500_with_message(client, container, monkeypatch):
    from job_search.domain.errors import TaskStartError

    def fail(*_args, **_kwargs):
        raise TaskStartError("The background task could not be started; try again shortly.", "x")

    monkeypatch.setattr(container.tasks, "start", fail)
    response = post(client, "/api/search/run")
    assert response.status_code == 500, f"expected 500, got {response.status_code}"
    assert "could not be started" in response.get_json()["error"], response.get_json()


def test_manual_add_returns_201_when_scoring_thread_cannot_start(config, environ, http, profile):
    from conftest import FakeResolver, make_client

    from job_search.container import build_container
    from job_search.observability import METRICS

    class BrokenThread:
        def __init__(self, target, args=(), daemon=None, name=None):
            pass

        def start(self):
            raise RuntimeError("can't start new thread")

    container = build_container(
        config,
        environ=environ,
        http_get=http,
        thread_factory=BrokenThread,
        resolve_host=FakeResolver(),
        profile=profile,
    )
    container.bootstrap()
    container.runtime.update({"JOB_SEARCH_ENABLE_GPT_SCORING": "1"})
    http.route("example.com", "<html><h1>Chief Architect</h1></html>")
    before = METRICS.snapshot().get("blame.manual_job_auto_score_not_started", 0)
    response = post(
        make_client(container), "/api/jobs", {"url": "https://example.com/jobs/t52", "pipeline": "Wildcards"}
    )
    body = response.get_json()
    assert response.status_code == 201, f"the saved job must be returned, not a 500: {body}"
    assert body["job"]["url"] == "https://example.com/jobs/t52", "the response carries the saved job"
    assert body["score_task"] is None and "could not be started" in body["score_error"], body
    assert METRICS.snapshot()["blame.manual_job_auto_score_not_started"] == before + 1, "the failure is recorded"
