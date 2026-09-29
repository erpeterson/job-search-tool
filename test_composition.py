import json
import logging
import stat
import tempfile
import unittest
from dataclasses import replace
from pathlib import Path
from unittest.mock import patch

import app
from job_search.application.job_scoring_policy import RUBRIC_FIELDS
from job_search.composition import (
    database_session,
    discovery_service,
    observability,
    outbound_clients,
    presentation_dependencies,
    runtime_configuration,
    task_execution_service,
)
from job_search.http_client import OutboundRequestError, SafeHttpClient
from job_search.presentation import legacy
from job_search.presentation.dependencies import PresentationDependencies
from job_search.presentation.factory import create_app


class CompositionTests(unittest.TestCase):
    def test_injected_job_routes_validate_and_persist_mutations(self):
        with tempfile.TemporaryDirectory() as directory:
            database = Path(directory) / "jobs.sqlite3"
            dependencies = presentation_dependencies(database)
            try:
                dependencies.startup_service.initialize()
                with database_session(database) as connection:
                    job_id = connection.execute(
                        "INSERT INTO jobs(created_at, updated_at, company, title, pipeline, status) "
                        "VALUES (1, 1, 'Example', 'Architect', 'Executive IC', 'researching')"
                    ).lastrowid
                client = create_app(dependencies=dependencies, route_blueprint=legacy.routes).test_client()

                invalid_score = client.post(f"/api/jobs/{job_id}/score-user", json={"scorecard": []})
                saved_score = client.post(
                    f"/api/jobs/{job_id}/score-user",
                    json={"scorecard": {field: 8 for field in RUBRIC_FIELDS}},
                )
                status = client.post(f"/api/jobs/{job_id}/status", json={"status": "interested"})
                note = client.post(f"/api/jobs/{job_id}/notes", json={"note": "Follow up"})
                unconfirmed_delete = client.delete(f"/api/jobs/{job_id}", json={"confirm": "no"})

                self.assertEqual(invalid_score.status_code, 400, "Scorecards must be JSON objects.")
                self.assertEqual(saved_score.get_json()["job"]["user_score"], 80)
                self.assertEqual(status.get_json()["job"]["status"], "interested")
                self.assertEqual(note.status_code, 201, "A valid note should be persisted.")
                self.assertEqual(unconfirmed_delete.status_code, 400, "Deletion must require exact confirmation.")
                with database_session(database) as connection:
                    saved = connection.execute("SELECT user_score, status FROM jobs WHERE id = ?", (job_id,)).fetchone()
                self.assertEqual((saved["user_score"], saved["status"]), (80, "interested"))
            finally:
                for logger in (dependencies.observability.api_logger, dependencies.observability.event_logger):
                    for handler in logger.handlers:
                        handler.close()

    def test_injected_company_routes_validate_and_persist_normalized_names(self):
        with tempfile.TemporaryDirectory() as directory:
            database = Path(directory) / "companies.sqlite3"
            dependencies = presentation_dependencies(database)
            try:
                dependencies.startup_service.initialize()
                client = create_app(dependencies=dependencies, route_blueprint=legacy.routes).test_client()

                invalid = client.post("/api/companies", json={"interest_score": 101})
                created = client.post("/api/companies", json={"company": " Example-Co ", "status": "target"})
                company_id = created.get_json()["company"]["id"]
                updated = client.post(f"/api/companies/{company_id}", json={"company": "Updated Co"})

                self.assertEqual(invalid.status_code, 400, "Out-of-range scores must fail at the HTTP boundary.")
                self.assertEqual(created.status_code, 201, "Valid company creation should remain available.")
                self.assertEqual(updated.status_code, 200, "Valid company updates should remain available.")
                with database_session(database) as connection:
                    normalized = connection.execute(
                        "SELECT normalized_company FROM company_interests WHERE id = ?", (company_id,)
                    ).fetchone()[0]
                self.assertEqual(normalized, "updated co", "The application service should normalize persisted names.")
            finally:
                for logger in (dependencies.observability.api_logger, dependencies.observability.event_logger):
                    for handler in logger.handlers:
                        handler.close()

    def test_injected_api_reads_only_its_temporary_database(self):
        with tempfile.TemporaryDirectory() as directory:
            first_path = Path(directory) / "first.sqlite3"
            second_path = Path(directory) / "second.sqlite3"
            first = presentation_dependencies(first_path)
            second = presentation_dependencies(second_path)
            try:
                first.startup_service.initialize()
                second.startup_service.initialize()
                with database_session(first_path) as connection:
                    job_id = connection.execute(
                        "INSERT INTO jobs(created_at, updated_at, company, title, status) VALUES (1, 1, ?, ?, ?)",
                        ("InjectedCo", "Architect", "researching"),
                    ).lastrowid
                    company_id = connection.execute(
                        "INSERT INTO company_interests(created_at, updated_at, company, normalized_company) "
                        "VALUES (1, 1, 'InjectedCo', 'injectedco')"
                    ).lastrowid
                task = first.background_task_service.start("scorecards", [job_id])
                first_app = create_app(dependencies=first, route_blueprint=legacy.routes)
                second_app = create_app(dependencies=second, route_blueprint=legacy.routes)

                client = first_app.test_client()
                first_response = client.get("/api/state")
                second_response = second_app.test_client().get("/api/state")

                self.assertEqual(first_response.status_code, 200, "First injected API should be available.")
                self.assertEqual(second_response.status_code, 200, "Second injected API should be available.")
                self.assertEqual(
                    [job["company"] for job in first_response.get_json()["jobs"]],
                    ["InjectedCo"],
                    "First API must read from its injected database.",
                )
                self.assertEqual(
                    second_response.get_json()["jobs"], [], "Second API must not read the first API's database."
                )
                self.assertEqual(client.get("/").status_code, 200, "The console page should remain registered.")
                self.assertEqual(client.get(f"/api/jobs/{job_id}").get_json()["job"]["company"], "InjectedCo")
                self.assertEqual(client.get("/api/application-packets").get_json()["application_packets"], [])
                self.assertEqual(
                    client.get(f"/api/companies/{company_id}").get_json()["company"]["company"], "InjectedCo"
                )
                self.assertEqual(client.get("/api/codex-tasks").get_json()["tasks"][0]["id"], task["id"])
                self.assertEqual(client.get(f"/api/codex-tasks/{task['id']}").get_json()["task"]["id"], task["id"])
            finally:
                for dependencies in (first, second):
                    for logger in (dependencies.observability.api_logger, dependencies.observability.event_logger):
                        for handler in logger.handlers:
                            handler.close()

    def test_outbound_clients_compose_fake_transport_and_board_parsers(self):
        class FakeResponse:
            status_code = 200
            ok = True
            headers = {}
            text = (
                "<li><a class='base-card__full-link' href='https://www.linkedin.com/jobs/view/1'>Role</a>"
                "<h3 class='base-search-card__title'>Architect</h3>"
                "<h4 class='base-search-card__subtitle'>ExampleCo</h4></li>"
            )

            def raise_for_status(self):
                return None

        class FakeHttp:
            def __init__(self):
                self.calls = []

            def get(self, service, url, **kwargs):
                self.calls.append((service, url, kwargs))
                return FakeResponse()

        with tempfile.TemporaryDirectory() as directory:
            config = runtime_configuration(
                Path(directory), {"JOB_SEARCH_USE_CAPTURE_CACHE": "1", "JOB_SEARCH_ENABLE_FULL_CAPTURE": "1"}
            )
            fake_http = FakeHttp()
            with patch("job_search.composition.logging.getLogger") as logger:
                logger.return_value = logging.Logger("test.outbound")
                observed = observability(config)
            try:
                clients = outbound_clients(
                    observed,
                    lambda value: " ".join((value or "").split()),
                    lambda value: value.split("?")[0],
                    lambda board, value: f"{board}:{value}",
                    lambda values: values,
                    client=fake_http,
                )
                jobs = clients.boards.linkedin("architect", "Remote")
                replayed = clients.boards.linkedin("architect", "Remote")
                self.assertEqual(jobs[0]["company"], "ExampleCo")
                self.assertEqual(replayed, jobs, "The second call should replay the redacted capture")
                self.assertEqual(len(fake_http.calls), 1, "Replay must not call the transport again")
                self.assertEqual(fake_http.calls[0][0], "linkedin")
                secure = outbound_clients(
                    observed,
                    lambda value: value,
                    lambda value: value,
                    lambda board, value: f"{board}:{value}",
                    lambda values: values,
                    client=SafeHttpClient(),
                )
                with self.assertRaisesRegex(OutboundRequestError, "approved job-board host"):
                    secure.gateway.get("indeed", "https://jobs.example.test/1", force_refresh=True)
            finally:
                for handler in observed.api_logger.handlers:
                    handler.close()

    def test_http_request_correlation_reaches_telemetry_and_is_cleared(self):
        with patch.object(legacy.event_logger, "info") as emit:
            response = app.app.test_client().post(
                "/api/jobs", json={"url": "invalid"}, headers={"X-Request-ID": "request-456"}
            )

        self.assertEqual(response.status_code, 400)
        event = json.loads(emit.call_args.args[0])
        self.assertEqual(event["correlation_id"], "request-456")
        self.assertIsNone(legacy.OBSERVABILITY.correlation_ids.get(), "Request context must be cleared")

    def test_observability_composes_redacted_captures_and_request_correlation(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            config = runtime_configuration(root, {"JOB_SEARCH_USE_CAPTURE_CACHE": "1"})
            api_logger = logging.Logger("test.job_search.api")
            event_logger = logging.Logger("test.job_search.events")
            loggers = {"job_search.api": api_logger, "job_search.events": event_logger}
            with patch("job_search.composition.logging.getLogger", side_effect=loggers.__getitem__):
                ports = observability(config)
            try:
                token = ports.correlation_ids.set("request-123")
                ports.telemetry.event("operation_started", operation="test")
                request = {"url": "https://example.test/job?token=secret", "prompt": "private prompt"}
                path = ports.captures.write("board", "get", request, {"status_code": 200})
                replay = ports.captures.read("board", "get", request)
                ports.correlation_ids.reset(token)
                ports.telemetry.event("operation_finished", operation="test")

                records = [json.loads(line) for line in config.paths.app_log.read_text(encoding="utf-8").splitlines()]
                self.assertEqual(records[0]["correlation_id"], "request-123")
                self.assertEqual(records[-1]["correlation_id"], None, "Correlation context must not leak")
                self.assertEqual(replay["request"]["prompt"], "[REDACTED]")
                self.assertEqual(replay["request"]["url"], "https://example.test/job")
                self.assertEqual(stat.S_IMODE(path.stat().st_mode), 0o600, "Captures must be private")
                self.assertEqual(stat.S_IMODE(path.parent.stat().st_mode), 0o700, "Capture directories must be private")
            finally:
                for logger in (api_logger, event_logger):
                    for handler in logger.handlers:
                        handler.close()

    def test_root_composes_the_presentation_application(self):
        self.assertEqual(app.app.name, "job_search.presentation.factory")
        self.assertIsNotNone(app.create_app)

    def test_factory_injects_service_without_constructing_flask_bound_dependencies(self):
        fake_service = object()
        web_app = create_app(
            dependencies=replace(presentation_dependencies(Path("unused.sqlite3")), job_service=fake_service),
            route_blueprint=legacy.routes,
        )

        with web_app.app_context():
            self.assertIs(
                legacy.job_service(), fake_service, "The composition root must honor an injected service fake."
            )

    def test_factory_rejects_missing_dependencies_at_construction(self):
        with self.assertRaisesRegex(ValueError, "Web dependencies"):
            create_app(route_blueprint=legacy.routes)
        with self.assertRaisesRegex(TypeError, "required positional"):
            PresentationDependencies(job_service=object())
        complete = presentation_dependencies(Path("unused.sqlite3"))
        for name in (
            "job_service",
            "configuration",
            "observability",
            "outbound_clients",
            "codex_gateway",
            "database_path",
        ):
            with self.subTest(dependency=name):
                with self.assertRaisesRegex(ValueError, name):
                    replace(complete, **{name: None})

    def test_web_runtime_ports_use_the_supplied_database_root(self):
        with tempfile.TemporaryDirectory() as directory:
            database = Path(directory) / "jobs.sqlite3"
            dependencies = presentation_dependencies(database)
            try:
                self.assertEqual(dependencies.database_path, database)
                self.assertEqual(dependencies.configuration.paths.root, Path(directory).resolve())
                self.assertIsNotNone(dependencies.observability.telemetry)
                self.assertIsNotNone(dependencies.observability.captures)
            finally:
                for logger in (dependencies.observability.api_logger, dependencies.observability.event_logger):
                    for handler in logger.handlers:
                        handler.close()

    def test_startup_tasks_and_job_recovery_share_injected_telemetry(self):
        with tempfile.TemporaryDirectory() as directory:
            dependencies = presentation_dependencies(Path(directory) / "jobs.sqlite3")
            token = dependencies.observability.correlation_ids.set("run-123")
            try:
                with patch.object(dependencies.observability.event_logger, "info") as emit:
                    dependencies.startup_service.initialize()
                    dependencies.background_task_service.start("scorecards", [7])
                    with database_session(dependencies.database_path) as connection:
                        job_id = connection.execute(
                            "INSERT INTO jobs(created_at, updated_at, company, title, status, gpt_scorecard_json) VALUES (1, 1, 'Example', 'Architect', 'researching', 'bad-json')"
                        ).lastrowid
                    dependencies.job_service.get_job(job_id)
                records = [json.loads(call.args[0]) for call in emit.call_args_list]
                event_names = [record["event"] for record in records]
                self.assertIn("startup_started", event_names)
                self.assertIn("background_task_queued", event_names)
                self.assertIn("job_scorecard_parse_recovered", event_names)
                self.assertTrue(all(record["correlation_id"] == "run-123" for record in records))
            finally:
                dependencies.observability.correlation_ids.reset(token)
                for logger in (dependencies.observability.api_logger, dependencies.observability.event_logger):
                    for handler in logger.handlers:
                        handler.close()

    def test_task_processor_is_composed_from_application_services(self):
        class Console:
            def job(self, job_id):
                return {"id": job_id}

        class Scoring:
            def populate_by_id(self, job_id, *, force_refresh):
                self.called = (job_id, force_refresh)
                return {"total_score": 91}

        class Packets:
            def generate(self, job_id):
                return {"path": f"applications/{job_id}"}

        scoring = Scoring()
        service = task_execution_service(Console(), scoring, Packets())

        self.assertEqual(service.process({"operation": "scorecards", "job_id": 3}), ("complete", "Codex score 91"))
        self.assertEqual(scoring.called, (3, False))

    def test_discovery_workflow_receives_explicit_operations(self):
        class Telemetry:
            def event(self, *_args, **_kwargs):
                return None

        service = discovery_service(
            Path("unused.sqlite3"),
            Telemetry(),
            "Unknown",
            "IC6-equivalent",
            scoring_enabled=lambda: False,
            scorer_available=lambda: True,
            scorer_path=lambda: "codex",
            score=lambda *_args, **_kwargs: {},
            apply_filter=lambda *_args, **_kwargs: None,
            normalize_pipeline=lambda value, fallback: value or fallback,
            refine=lambda *_args, **_kwargs: None,
            clean_text=lambda value: value,
        )
        self.assertIsNotNone(service)


if __name__ == "__main__":
    unittest.main()
