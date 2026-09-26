import importlib.util
import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from job_search.data_access import codex_cli
from job_search.presentation.factory import create_app

APP_PATH = Path(__file__).resolve().parent / "job_search" / "presentation" / "legacy.py"
SPEC = importlib.util.spec_from_file_location("business_paths_app", APP_PATH)
app_module = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(app_module)
app_module.app = create_app(route_blueprint=app_module.routes)


class BusinessPathTests(unittest.TestCase):
    def setUp(self):
        self.tempdir = tempfile.TemporaryDirectory()
        self.original_capture_dir = app_module.CAPTURE_DIR
        self.original_applications_dir = app_module.APPLICATIONS_DIR
        self.original_root = app_module.ROOT
        self.original_env_path = app_module.ENV_PATH
        app_module.CAPTURE_DIR = Path(self.tempdir.name) / "captures"
        app_module.APPLICATIONS_DIR = Path(self.tempdir.name) / "applications"
        app_module.ROOT = Path(self.tempdir.name)
        app_module.ENV_PATH = Path(self.tempdir.name) / ".env"

    def tearDown(self):
        app_module.CAPTURE_DIR = self.original_capture_dir
        app_module.APPLICATIONS_DIR = self.original_applications_dir
        app_module.ROOT = self.original_root
        app_module.ENV_PATH = self.original_env_path
        self.tempdir.cleanup()

    def test_location_compensation_and_sales_filters_cover_boundary_cases(self):
        self.assertEqual(app_module.location_filter_decision({"location": "Seattle, WA"})[0], True)
        self.assertEqual(app_module.location_filter_decision({"location": "London, UK"})[0], False)
        self.assertEqual(app_module.compensation_filter_decision({"snippet": "$150,000 per year"})[0], False)
        self.assertEqual(app_module.compensation_filter_decision({"snippet": "Compensation not listed"})[0], True)
        self.assertEqual(app_module.sales_role_filter_decision({"title": "Account Executive"})[0], False)

    def test_packet_path_disallows_traversal_and_accepts_packet_directory(self):
        app_module.APPLICATIONS_DIR.mkdir()
        packet = app_module.APPLICATIONS_DIR / "example-role"
        packet.mkdir()

        self.assertEqual(app_module.application_packet_abs_path("applications/example-role"), packet.resolve())
        with self.assertRaisesRegex(ValueError, "under applications"):
            app_module.application_packet_abs_path("../../outside")

    def test_score_coercion_recovery_emits_a_stable_telemetry_code(self):
        events = []
        original_logger = app_module.event_logger.info
        app_module.event_logger.info = events.append
        try:
            self.assertEqual(app_module.clamp_score("not-a-number"), 0)
        finally:
            app_module.event_logger.info = original_logger

        self.assertIn("SCORE_VALUE_COERCION_RECOVERED", "\n".join(events))

    def test_corrupt_capture_is_recovered_and_emits_telemetry(self):
        with patch.object(app_module.os, "environ", {"JOB_SEARCH_USE_CAPTURE_CACHE": "1"}):
            payload = {"url": "https://example.test"}
            path = app_module.capture_path("manual", "http_get", payload)
            path.parent.mkdir(parents=True)
            path.write_text("not-json", encoding="utf-8")
            events = []
            original_log_event = app_module.log_event
            app_module.log_event = lambda event_type, **fields: events.append((event_type, fields))
            try:
                self.assertIsNone(app_module.read_capture("manual", "http_get", payload))
            finally:
                app_module.log_event = original_log_event
        self.assertEqual(events[0][1]["error_code"], "CAPTURE_CORRUPTION_RECOVERED")

    def test_config_endpoint_persists_validated_value(self):
        with patch.object(app_module.os, "environ", {}):
            response = app_module.app.test_client().post("/api/config", json={"CODEX_MODEL": "test-model"})

        self.assertEqual(response.status_code, 200)
        self.assertIn("CODEX_MODEL=test-model", app_module.ENV_PATH.read_text(encoding="utf-8"))

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
        originals = (
            app_module.OUTBOUND_HTTP_CLIENT,
            codex_cli.subprocess.run,
            app_module.api_logger.info,
            app_module.event_logger.info,
        )

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

        app_module.OUTBOUND_HTTP_CLIENT = HttpClient()
        codex_cli.subprocess.run = lambda *_args, **_kwargs: CompletedProcess()
        app_module.api_logger.info = api_events.append
        app_module.event_logger.info = app_events.append
        environment = {"JOB_SEARCH_USE_CAPTURE_CACHE": "1", "JOB_SEARCH_ENABLE_FULL_CAPTURE": "0"}
        try:
            with patch.object(app_module.os, "environ", environment):
                app_module.fetch_url("manual_posting", f"https://example.test/job?token={sentinels['token']}")
                app_module.call_codex_json(
                    "test-model", {"prompt": sentinels["prompt"]}, "redaction_test", force_refresh=True
                )
                captures = [path.read_text(encoding="utf-8") for path in app_module.CAPTURE_DIR.rglob("*.json")]
        finally:
            (
                app_module.OUTBOUND_HTTP_CLIENT,
                codex_cli.subprocess.run,
                app_module.api_logger.info,
                app_module.event_logger.info,
            ) = originals

        persisted = "\n".join([*api_events, *app_events, *captures])
        for sentinel in sentinels.values():
            self.assertNotIn(sentinel, persisted)
        self.assertIn("response_content", json.loads(api_events[0]))


if __name__ == "__main__":
    unittest.main()
