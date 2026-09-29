import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from job_search.application.discovery_policy import DiscoveryPolicy
from job_search.application.discovery_utils import clean_text, clean_url, dedupe_results, source_id
from job_search.composition import (
    codex_json_gateway,
    observability,
    outbound_clients,
    presentation_dependencies,
    runtime_configuration,
)
from job_search.data_access import codex_cli
from job_search.data_access.packet_storage import PacketStorage
from job_search.presentation.factory import create_app


class BusinessPathTests(unittest.TestCase):
    def setUp(self):
        self.tempdir = tempfile.TemporaryDirectory()
        self.addCleanup(self.tempdir.cleanup)
        self.root = Path(self.tempdir.name)
        self.database = self.root / "jobs.sqlite3"
        self.applications = self.root / "applications"
        self.packet_storage = PacketStorage(self.root, self.applications)
        self.dependencies = presentation_dependencies(self.database, {})
        self.app = create_app(dependencies=self.dependencies)
        self.dependencies.startup_service.initialize()

    def test_location_compensation_and_sales_filters_cover_boundary_cases(self):
        policy = DiscoveryPolicy(200_000)
        self.assertEqual(policy.location({"location": "Seattle, WA"})[0], True)
        self.assertEqual(policy.location({"location": "London, UK"})[0], False)
        self.assertEqual(policy.compensation({"snippet": "$150,000 per year"})[0], False)
        self.assertEqual(policy.compensation({"snippet": "Compensation not listed"})[0], True)
        self.assertEqual(policy.sales_role({"title": "Account Executive"})[0], False)

    def test_packet_path_disallows_traversal_and_accepts_packet_directory(self):
        self.applications.mkdir()
        packet = self.applications / "example-role"
        packet.mkdir()

        self.assertEqual(self.packet_storage.packet_directory("applications/example-role"), packet.resolve())
        with self.assertRaisesRegex(ValueError, "under applications"):
            self.packet_storage.packet_directory("../../outside")

    def test_corrupt_capture_is_recovered_and_emits_telemetry(self):
        observed = observability(runtime_configuration(self.root, {"JOB_SEARCH_USE_CAPTURE_CACHE": "1"}))
        payload = {"url": "https://example.test"}
        path = observed.captures.path("manual", "http_get", payload)
        path.parent.mkdir(parents=True)
        path.write_text("not-json", encoding="utf-8")
        events = []
        with patch.object(
            observed.telemetry,
            "event",
            side_effect=lambda event_type, **fields: events.append((event_type, fields)),
        ):
            self.assertIsNone(observed.captures.read("manual", "http_get", payload))
        self.assertEqual(events[0][1]["error_code"], "CAPTURE_CORRUPTION_RECOVERED")

    def test_config_endpoint_persists_validated_value(self):
        response = self.app.test_client().post("/api/config", json={"CODEX_MODEL": "test-model"})

        self.assertEqual(response.status_code, 200)
        self.assertIn("CODEX_MODEL=test-model", (self.root / ".env").read_text(encoding="utf-8"))

    def test_normal_telemetry_and_captures_exclude_http_and_codex_content(self):
        sentinels = {
            "posting": "POSTING-SENTINEL",
            "prompt": "PROMPT-SENTINEL",
            "model": "MODEL-OUTPUT-SENTINEL",
            "stderr": "STDERR-SENTINEL",
            "token": "URL-TOKEN-SENTINEL",
            "cookie": "COOKIE-SENTINEL",
        }
        api_events = []
        app_events = []

        class HttpResponse:
            status_code = 200
            ok = True
            headers = {"Set-Cookie": sentinels["cookie"]}
            text = sentinels["posting"]

        class HttpClient:
            @staticmethod
            def get(*_args, **_kwargs):
                return HttpResponse()

        class CompletedProcess:
            returncode = 0
            stdout = sentinels["model"]
            stderr = f"model: test-model {sentinels['stderr']}"

        environment = {"JOB_SEARCH_USE_CAPTURE_CACHE": "1", "JOB_SEARCH_ENABLE_FULL_CAPTURE": "0"}
        configuration = runtime_configuration(self.root, environment)
        observed = observability(configuration)
        with (
            patch.object(codex_cli.subprocess, "run", return_value=CompletedProcess()),
            patch.object(observed.api_logger, "info", side_effect=api_events.append),
            patch.object(observed.event_logger, "info", side_effect=app_events.append),
        ):
            gateway = outbound_clients(
                observed,
                clean_text,
                clean_url,
                source_id,
                dedupe_results,
                client=HttpClient(),
            ).gateway
            gateway.get("manual_posting", f"https://example.test/job?token={sentinels['token']}")
            codex_json_gateway(configuration, observed).complete(
                "test-model", {"prompt": sentinels["prompt"]}, "redaction_test", force_refresh=True
            )
            captures = [path.read_text(encoding="utf-8") for path in configuration.paths.captures.rglob("*.json")]

        persisted = "\n".join([*api_events, *app_events, *captures])
        for sentinel in sentinels.values():
            self.assertNotIn(sentinel, persisted)
        self.assertIn("response_content", json.loads(api_events[0]))


if __name__ == "__main__":
    unittest.main()
