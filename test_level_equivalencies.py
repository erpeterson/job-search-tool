import importlib.util
import tempfile
import unittest
from pathlib import Path

from job_search.presentation.factory import create_app
from job_search.security import load_request_security

APP_PATH = Path(__file__).resolve().parent / "job_search" / "presentation" / "legacy.py"
SPEC = importlib.util.spec_from_file_location("job_search_app", APP_PATH)
job_search_app = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(job_search_app)
job_search_app.app = create_app(route_blueprint=job_search_app.routes)


class LevelEquivalencyTests(unittest.TestCase):
    def setUp(self):
        self.tmpdir = tempfile.TemporaryDirectory()
        self.original_db_path = job_search_app.DB_PATH
        self.original_fetch_url = job_search_app.fetch_url
        self.original_log_event = job_search_app.log_event
        self.original_scrape_job_from_url = job_search_app.scrape_job_from_url
        self.original_gpt_scoring_enabled = job_search_app.gpt_scoring_enabled
        self.original_codex_cli_available = job_search_app.codex_cli_available
        self.original_codex_model = job_search_app.codex_model
        self.original_populate_codex_score = job_search_app.populate_codex_score
        job_search_app.DB_PATH = Path(self.tmpdir.name) / "job_search.sqlite3"
        job_search_app.init_db()

    def tearDown(self):
        job_search_app.DB_PATH = self.original_db_path
        job_search_app.fetch_url = self.original_fetch_url
        job_search_app.log_event = self.original_log_event
        job_search_app.scrape_job_from_url = self.original_scrape_job_from_url
        job_search_app.gpt_scoring_enabled = self.original_gpt_scoring_enabled
        job_search_app.codex_cli_available = self.original_codex_cli_available
        job_search_app.codex_model = self.original_codex_model
        job_search_app.populate_codex_score = self.original_populate_codex_score
        self.tmpdir.cleanup()

    def test_init_db_does_not_seed_level_equivalencies(self):
        with job_search_app.connect() as conn:
            count = conn.execute("SELECT COUNT(*) FROM level_equivalencies").fetchone()[0]

        self.assertEqual(count, 0)

    def test_ambiguous_title_is_unknown_and_does_not_fetch_or_cache(self):
        calls = []
        events = []

        def fake_fetch_url(service, url, force_refresh=False):
            calls.append((service, url, force_refresh))
            raise AssertionError("level lookup should not fetch external services")

        def fake_log_event(event_type, **fields):
            events.append((event_type, fields))

        job_search_app.fetch_url = fake_fetch_url
        job_search_app.log_event = fake_log_event

        with job_search_app.connect() as conn:
            result = job_search_app.lookup_level_equivalency(conn, "Atlassian", "Principal Engineer")
            count = conn.execute("SELECT COUNT(*) FROM level_equivalencies").fetchone()[0]

        self.assertIsNone(result)
        self.assertEqual(count, 0)
        self.assertTrue(
            any(
                event_type == "level_equivalency_unknown"
                and fields["company"] == "Atlassian"
                and fields["title"] == "Principal Engineer"
                for event_type, fields in events
            )
        )
        self.assertEqual(calls, [])

    def test_downlevel_title_estimate_is_cached_and_reused(self):
        calls = []

        def fake_fetch_url(service, url, force_refresh=False):
            calls.append((service, url, force_refresh))
            raise AssertionError("level lookup should not fetch external services")

        job_search_app.fetch_url = fake_fetch_url

        with job_search_app.connect() as conn:
            first = job_search_app.lookup_level_equivalency(conn, "ExampleCo", "Senior Software Engineer")
            second = job_search_app.lookup_level_equivalency(conn, "ExampleCo", "Senior Software Engineer")

        self.assertEqual(calls, [])
        self.assertEqual(first["oracle_level"], "BELOW_IC6")
        self.assertEqual(first["oracle_title"], "Below Architect-equivalent")
        self.assertEqual(first["downlevel"], 1)
        self.assertEqual(second["oracle_level"], "BELOW_IC6")

    def test_ic6_plus_title_estimate_is_cached(self):
        with job_search_app.connect() as conn:
            result = job_search_app.lookup_level_equivalency(conn, "ExampleCo", "Senior Principal Software Engineer")

        self.assertEqual(result["oracle_level"], "IC6+")
        self.assertEqual(result["oracle_title"], "Architect-equivalent or higher")
        self.assertEqual(result["downlevel"], 0)

    def test_scorecard_pipeline_list_is_normalized_before_sqlite_update(self):
        original_score = job_search_app.score_with_codex_cli
        try:
            with job_search_app.connect() as conn:
                job_id = conn.execute(
                    "INSERT INTO jobs(created_at, updated_at, company, title, pipeline, status) VALUES (?, ?, ?, ?, ?, ?)",
                    (1, 1, "ExampleCo", "Principal Engineer", "Wildcards", "researching"),
                ).lastrowid
                job_search_app.score_with_codex_cli = lambda *_args, **_kwargs: {
                    "total_score": 85,
                    "scorecard": {},
                    "pipeline": ["Executive IC", "Wildcards"],
                    "level_assessment": "IC6-equivalent",
                    "downlevel": False,
                    "rationale": "Strong fit.",
                }

                job_search_app.populate_codex_score(conn, job_id)
                pipeline = conn.execute("SELECT pipeline FROM jobs WHERE id = ?", (job_id,)).fetchone()["pipeline"]

            self.assertEqual(pipeline, "Executive IC")
        finally:
            job_search_app.score_with_codex_cli = original_score

    def test_manually_added_job_is_automatically_scored(self):
        job_search_app.scrape_job_from_url = lambda url, force_refresh=False: {
            "url": url,
            "company": "ExampleCo",
            "title": "Principal Engineer",
            "location": "Remote",
            "posting_text": "Architecture role.",
            "source_board": "manual",
            "source_job_id": None,
        }
        job_search_app.gpt_scoring_enabled = lambda: True
        job_search_app.codex_cli_available = lambda: True

        class FakeScoringWorkflow:
            def populate_by_id(self, job_id, *, force_refresh):
                with job_search_app.connect() as conn:
                    conn.execute("UPDATE jobs SET gpt_score = ? WHERE id = ?", (88, job_id))
                return {"total_score": 88}

        original_workflow = job_search_app.codex_scoring_workflow
        job_search_app.codex_scoring_workflow = lambda: FakeScoringWorkflow()
        client = job_search_app.app.test_client()
        try:
            response = client.post(
                "/api/jobs", json={"url": "https://example.com/jobs/123", "pipeline": "Executive IC"}
            )
        finally:
            job_search_app.codex_scoring_workflow = original_workflow

        self.assertEqual(response.status_code, 201)
        self.assertIsNone(response.get_json()["score_error"])
        self.assertEqual(response.get_json()["job"]["gpt_score"], 88)

    def test_extracts_exact_model_from_codex_cli_output(self):
        stderr = "OpenAI Codex v0.147.0 -------- model: gpt-5.6-terra provider: openai --------"

        self.assertEqual(job_search_app.extract_codex_reported_model(stderr), "gpt-5.6-terra")

    def test_job_create_rejects_invalid_url_at_api_boundary(self):
        response = job_search_app.app.test_client().post(
            "/api/jobs",
            json={"url": "file:///private/source.md", "pipeline": "Executive IC"},
        )

        self.assertEqual(response.status_code, 400)
        self.assertIn("http or https URL", response.get_json()["error"])

    def test_note_requires_existing_job_and_content(self):
        response = job_search_app.app.test_client().post("/api/jobs/999/notes", json={"note": ""})

        self.assertEqual(response.status_code, 400)
        self.assertEqual(response.get_json()["error"], "note is required.")

    def test_external_binding_rejects_unauthenticated_and_missing_csrf_mutations(self):
        original_security = job_search_app.REQUEST_SECURITY
        try:
            job_search_app.REQUEST_SECURITY = load_request_security(
                {
                    "JOB_SEARCH_HOST": "0.0.0.0",
                    "JOB_SEARCH_AUTH_TOKEN": "auth",
                    "JOB_SEARCH_CSRF_TOKEN": "csrf",
                    "JOB_SEARCH_TRUSTED_PROXY": "1",
                    "JOB_SEARCH_TLS_TERMINATED": "1",
                    "JOB_SEARCH_TRUSTED_PROXY_CIDRS": "127.0.0.1/32",
                }
            )
            client = job_search_app.app.test_client()
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
        finally:
            job_search_app.REQUEST_SECURITY = original_security

    def test_bulk_request_rejects_array_duplicate_and_oversized_job_ids(self):
        client = job_search_app.app.test_client()
        job_search_app.gpt_scoring_enabled = lambda: True
        job_search_app.codex_cli_available = lambda: True
        try:
            for payload, expected in (
                ([], "JSON object"),
                ({"job_ids": [1, 1]}, "duplicates"),
                ({"job_ids": list(range(1, 52))}, "at most 50"),
            ):
                response = client.post("/api/jobs/bulk/score-gpt", json=payload)
                self.assertEqual(response.status_code, 400)
                self.assertIn(expected, response.get_json()["error"])
        finally:
            job_search_app.gpt_scoring_enabled = self.original_gpt_scoring_enabled
            job_search_app.codex_cli_available = self.original_codex_cli_available

    def test_validation_response_contains_error_code_and_correlation_id(self):
        events = []
        job_search_app.log_event = lambda event_type, **fields: events.append((event_type, fields))
        try:
            response = job_search_app.app.test_client().post(
                "/api/jobs", json={"url": "invalid", "pipeline": "Executive IC"}, headers={"X-Request-ID": "run-123"}
            )
            self.assertEqual(response.status_code, 400)
            self.assertEqual(response.get_json()["error_code"], "API_CLIENT_INPUT_INVALID")
            self.assertEqual(events[0][1]["error_code"], "API_CLIENT_INPUT_INVALID")
        finally:
            job_search_app.log_event = self.original_log_event


if __name__ == "__main__":
    unittest.main()
