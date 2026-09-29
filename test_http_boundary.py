"""Representative HTTP boundary behavior with isolated, injected services."""

import tempfile
import unittest
from dataclasses import replace
from pathlib import Path
from unittest.mock import patch

from job_search.composition import presentation_dependencies
from job_search.presentation.factory import create_app


class HttpBoundaryTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.database = Path(temporary.name) / "jobs.sqlite3"
        self.dependencies = presentation_dependencies(self.database, {})
        self.dependencies.startup_service.initialize()

    def test_success_reads_from_injected_service(self):
        class FakeReads:
            def job(self, job_id):
                return {"id": job_id, "company": "InjectedCo"}

        client = create_app(dependencies=replace(self.dependencies, console_query_service=FakeReads())).test_client()

        response = client.get("/api/jobs/7")

        self.assertEqual(response.status_code, 200, "A valid read should succeed.")
        self.assertEqual(response.get_json()["job"], {"id": 7, "company": "InjectedCo"})

    def test_validation_error_has_public_code_and_observed_failure(self):
        events = []
        with patch.object(
            self.dependencies.observability.telemetry,
            "event",
            side_effect=lambda name, **fields: events.append((name, fields)),
        ):
            response = (
                create_app(dependencies=self.dependencies)
                .test_client()
                .post("/api/jobs", json={"url": "file:///private/job", "pipeline": "Executive IC"})
            )

        self.assertEqual(response.status_code, 400, "Invalid URLs must fail at the HTTP boundary.")
        self.assertEqual(response.get_json()["error_code"], "API_CLIENT_INPUT_INVALID")
        self.assertEqual(events[-1][0], "api_request_validation_failed")
        self.assertEqual(events[-1][1]["error_code"], "API_CLIENT_INPUT_INVALID")

    def test_external_binding_requires_authentication_and_csrf(self):
        environment = {
            "JOB_SEARCH_HOST": "0.0.0.0",
            "JOB_SEARCH_AUTH_TOKEN": "auth",
            "JOB_SEARCH_CSRF_TOKEN": "csrf",
            "JOB_SEARCH_TRUSTED_PROXY": "1",
            "JOB_SEARCH_TLS_TERMINATED": "1",
            "JOB_SEARCH_TRUSTED_PROXY_CIDRS": "127.0.0.1/32",
        }
        secured = presentation_dependencies(self.database, environment)
        client = create_app(dependencies=secured).test_client()
        headers = {"X-Forwarded-Proto": "https"}

        unauthenticated = client.get("/api/state", headers=headers)
        missing_csrf = client.post("/api/jobs", json={}, headers={**headers, "Authorization": "Bearer auth"})

        self.assertEqual(unauthenticated.status_code, 401, "External reads require authentication.")
        self.assertEqual(missing_csrf.status_code, 403, "External mutations require CSRF validation.")

    def test_unexpected_error_is_logged_and_does_not_leak_details(self):
        class FailingReads:
            def job(self, _job_id):
                raise RuntimeError("private-database-secret")

        client = create_app(dependencies=replace(self.dependencies, console_query_service=FailingReads())).test_client()
        events = []
        with patch.object(
            self.dependencies.observability.telemetry,
            "event",
            side_effect=lambda name, **fields: events.append((name, fields)),
        ):
            response = client.get("/api/jobs/7", headers={"X-Request-ID": "boundary-123"})

        self.assertEqual(response.status_code, 500, "Unhandled exceptions must map to HTTP 500.")
        self.assertEqual(response.get_json()["error_code"], "API_UNHANDLED_EXCEPTION")
        self.assertNotIn("private-database-secret", response.get_data(as_text=True))
        self.assertEqual(events[-1][0], "api_unhandled_exception")
        self.assertEqual(events[-1][1]["error_code"], "API_UNHANDLED_EXCEPTION")
        self.assertEqual(events[-1][1]["operation"], "job_search.api_job")
        self.assertEqual(events[-1][1]["cause"], "RuntimeError")
        self.assertNotIn("private-database-secret", str(events))


if __name__ == "__main__":
    unittest.main()
