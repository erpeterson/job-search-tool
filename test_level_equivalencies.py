import tempfile
import unittest
from dataclasses import replace
from pathlib import Path
from unittest.mock import patch

from job_search.application.manual_job_service import ManualJobService
from job_search.composition import codex_scoring_workflow, database_session, level_service, presentation_dependencies
from job_search.data_access.codex_cli import CodexCliGateway
from job_search.data_access.job_repository import SqliteJobRepository
from job_search.presentation.factory import create_app
from job_search.security import load_request_security


class LevelEquivalencyTests(unittest.TestCase):
    def setUp(self):
        self.tmpdir = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmpdir.cleanup)
        self.database = Path(self.tmpdir.name) / "job_search.sqlite3"
        self.dependencies = presentation_dependencies(self.database, {})
        self.app = create_app(dependencies=self.dependencies)
        self.dependencies.startup_service.initialize()

    def test_startup_does_not_seed_level_equivalencies(self):
        with database_session(self.database) as conn:
            count = conn.execute("SELECT COUNT(*) FROM level_equivalencies").fetchone()[0]

        self.assertEqual(count, 0)

    def test_ambiguous_title_is_unknown_and_does_not_fetch_or_cache(self):
        with database_session(self.database) as conn:
            result = level_service(self.database, conn).lookup("Atlassian", "Principal Engineer")
            count = conn.execute("SELECT COUNT(*) FROM level_equivalencies").fetchone()[0]

        self.assertIsNone(result)
        self.assertEqual(count, 0)

    def test_downlevel_title_estimate_is_cached_and_reused(self):
        with database_session(self.database) as conn:
            service = level_service(self.database, conn)
            first = service.lookup("ExampleCo", "Senior Software Engineer")
            second = service.lookup("ExampleCo", "Senior Software Engineer")

        self.assertEqual(first["oracle_level"], "BELOW_IC6")
        self.assertEqual(first["oracle_title"], "Below Architect-equivalent")
        self.assertEqual(first["downlevel"], 1)
        self.assertEqual(second["oracle_level"], "BELOW_IC6")

    def test_ic6_plus_title_estimate_is_cached(self):
        with database_session(self.database) as conn:
            result = level_service(self.database, conn).lookup("ExampleCo", "Senior Principal Software Engineer")

        self.assertEqual(result["oracle_level"], "IC6+")
        self.assertEqual(result["oracle_title"], "Architect-equivalent or higher")
        self.assertEqual(result["downlevel"], 0)

    def test_scorecard_pipeline_list_is_normalized_before_sqlite_update(self):
        with database_session(self.database) as conn:
            job_id = conn.execute(
                "INSERT INTO jobs(created_at, updated_at, company, title, pipeline, status) VALUES (?, ?, ?, ?, ?, ?)",
                (1, 1, "ExampleCo", "Principal Engineer", "Wildcards", "researching"),
            ).lastrowid

            class FakeScorer:
                def score(self, *_args, **_kwargs):
                    return {
                        "total_score": 85,
                        "scorecard": {},
                        "pipeline": ["Executive IC", "Wildcards"],
                        "level_assessment": "IC6-equivalent",
                        "downlevel": False,
                        "rationale": "Strong fit.",
                    }

            service = codex_scoring_workflow(
                self.database,
                self.dependencies.configuration,
                FakeScorer(),
                self.dependencies.observability.telemetry,
            )
            service.populate(conn, job_id, force_refresh=False)
            pipeline = conn.execute("SELECT pipeline FROM jobs WHERE id = ?", (job_id,)).fetchone()["pipeline"]

        self.assertEqual(pipeline, "Executive IC")

    def test_manually_added_job_is_automatically_scored(self):
        def scraped(url, force_refresh=False):
            return {
                "url": url,
                "company": "ExampleCo",
                "title": "Principal Engineer",
                "location": "Remote",
                "posting_text": "Architecture role.",
                "source_board": "manual",
                "source_job_id": None,
            }

        database = self.database

        class FakeScoringWorkflow:
            def populate_by_id(self, job_id, *, force_refresh):
                with database_session(database) as conn:
                    conn.execute("UPDATE jobs SET gpt_score = ? WHERE id = ?", (88, job_id))
                return {"total_score": 88}

        repository = SqliteJobRepository(lambda: database_session(database))
        manual = ManualJobService(
            repository,
            scraped,
            lambda url: {"url": url},
            self.dependencies.filtering_service.refresh_job,
            lambda job_id: FakeScoringWorkflow().populate_by_id(job_id, force_refresh=False),
            lambda: None,
            lambda *_args: None,
            lambda *_args: None,
            lambda: 100,
        )
        app = create_app(dependencies=replace(self.dependencies, manual_job_service=manual))
        response = app.test_client().post(
            "/api/jobs", json={"url": "https://example.com/jobs/123", "pipeline": "Executive IC"}
        )

        self.assertEqual(response.status_code, 201)
        self.assertIsNone(response.get_json()["score_error"])
        self.assertEqual(response.get_json()["job"]["gpt_score"], 88)

    def test_extracts_exact_model_from_codex_cli_output(self):
        stderr = "OpenAI Codex v0.147.0 -------- model: gpt-5.6-terra provider: openai --------"

        self.assertEqual(CodexCliGateway.reported_model(stderr), "gpt-5.6-terra")

    def test_job_create_rejects_invalid_url_at_api_boundary(self):
        response = self.app.test_client().post(
            "/api/jobs",
            json={"url": "file:///private/source.md", "pipeline": "Executive IC"},
        )

        self.assertEqual(response.status_code, 400)
        self.assertIn("http or https URL", response.get_json()["error"])

    def test_note_requires_existing_job_and_content(self):
        response = self.app.test_client().post("/api/jobs/999/notes", json={"note": ""})

        self.assertEqual(response.status_code, 400)
        self.assertEqual(response.get_json()["error"], "note is required.")

    def test_external_binding_rejects_unauthenticated_and_missing_csrf_mutations(self):
        security = load_request_security(
            {
                "JOB_SEARCH_HOST": "0.0.0.0",
                "JOB_SEARCH_AUTH_TOKEN": "auth",
                "JOB_SEARCH_CSRF_TOKEN": "csrf",
                "JOB_SEARCH_TRUSTED_PROXY": "1",
                "JOB_SEARCH_TLS_TERMINATED": "1",
                "JOB_SEARCH_TRUSTED_PROXY_CIDRS": "127.0.0.1/32",
            }
        )
        secured = create_app(
            dependencies=replace(
                self.dependencies,
                configuration=replace(self.dependencies.configuration, security=security),
            )
        )
        client = secured.test_client()
        unauthenticated = client.post("/api/search/run", json={}, headers={"X-Forwarded-Proto": "https"})
        no_csrf = client.post(
            "/api/search/run",
            json={},
            headers={"X-Forwarded-Proto": "https", "Authorization": "Bearer auth"},
        )
        forged_direct = client.get(
            "/api/state",
            headers={"X-Forwarded-Proto": "https", "Authorization": "Bearer auth"},
            environ_overrides={"REMOTE_ADDR": "8.8.8.8"},
        )
        untrusted_proxy = client.get(
            "/api/state",
            headers={"X-Forwarded-Proto": "https", "Authorization": "Bearer auth"},
            environ_overrides={"REMOTE_ADDR": "10.0.0.2"},
        )
        trusted_proxy = client.get(
            "/api/state",
            headers={"X-Forwarded-Proto": "https", "Authorization": "Bearer auth"},
        )
        self.assertEqual(unauthenticated.status_code, 401)
        self.assertEqual(no_csrf.status_code, 403)
        self.assertEqual(forged_direct.status_code, 403)
        self.assertEqual(untrusted_proxy.status_code, 403)
        self.assertEqual(trusted_proxy.status_code, 200)

    def test_bulk_request_rejects_array_duplicate_and_oversized_job_ids(self):
        client = self.app.test_client()
        for payload, expected in (
            ([], "JSON object"),
            ({"job_ids": [1, 1]}, "duplicates"),
            ({"job_ids": list(range(1, 52))}, "at most 50"),
        ):
            response = client.post("/api/jobs/bulk/score-gpt", json=payload)
            self.assertEqual(response.status_code, 400)
            self.assertIn(expected, response.get_json()["error"])

    def test_validation_response_contains_error_code_and_correlation_id(self):
        events = []
        with patch.object(
            self.dependencies.observability.telemetry,
            "event",
            side_effect=lambda event_type, **fields: events.append((event_type, fields)),
        ):
            response = self.app.test_client().post(
                "/api/jobs", json={"url": "invalid", "pipeline": "Executive IC"}, headers={"X-Request-ID": "run-123"}
            )
            self.assertEqual(response.status_code, 400)
            self.assertEqual(response.get_json()["error_code"], "API_CLIENT_INPUT_INVALID")
            self.assertEqual(events[0][1]["error_code"], "API_CLIENT_INPUT_INVALID")


if __name__ == "__main__":
    unittest.main()
