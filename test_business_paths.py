import importlib.util
import json
import tempfile
import unittest
from dataclasses import replace
from pathlib import Path
from unittest.mock import patch

from job_search.application.discovery_policy import DiscoveryPolicy
from job_search.application.discovery_utils import clean_text, clean_url, dedupe_results, source_id
from job_search.composition import codex_json_gateway, outbound_clients, presentation_dependencies
from job_search.data_access import codex_cli
from job_search.data_access.packet_storage import PacketStorage
from job_search.presentation.factory import create_app

APP_PATH = Path(__file__).resolve().parent / "job_search" / "presentation" / "legacy.py"
SPEC = importlib.util.spec_from_file_location("business_paths_app", APP_PATH)
app_module = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(app_module)


class BusinessPathTests(unittest.TestCase):
    def setUp(self):
        self.tempdir = tempfile.TemporaryDirectory()
        self.original_capture_dir = app_module.CAPTURE_DIR
        self.original_db_path = app_module.DB_PATH
        self.original_applications_dir = app_module.APPLICATIONS_DIR
        self.original_root = app_module.ROOT
        self.original_env_path = app_module.ENV_PATH
        self.original_runtime_paths = app_module.RUNTIME_CONFIG.paths
        self.original_runtime_environment = app_module.RUNTIME_CONFIG.environment
        app_module.RUNTIME_CONFIG.environment = dict(self.original_runtime_environment)
        app_module.CAPTURE_DIR = Path(self.tempdir.name) / "captures"
        app_module.DB_PATH = Path(self.tempdir.name) / "jobs.sqlite3"
        app_module.APPLICATIONS_DIR = Path(self.tempdir.name) / "applications"
        app_module.ROOT = Path(self.tempdir.name)
        app_module.ENV_PATH = Path(self.tempdir.name) / ".env"
        app_module.RUNTIME_CONFIG.paths = replace(
            self.original_runtime_paths,
            root=app_module.ROOT,
            applications=app_module.APPLICATIONS_DIR,
            environment_file=app_module.ENV_PATH,
            captures=app_module.CAPTURE_DIR,
        )
        self.packet_storage = PacketStorage(app_module.ROOT, app_module.APPLICATIONS_DIR)
        app_module.app = create_app(
            dependencies=presentation_dependencies(app_module.DB_PATH), route_blueprint=app_module.routes
        )
        with app_module.app.app_context():
            app_module.startup_service().initialize()

    def tearDown(self):
        app_module.CAPTURE_DIR = self.original_capture_dir
        app_module.DB_PATH = self.original_db_path
        app_module.APPLICATIONS_DIR = self.original_applications_dir
        app_module.ROOT = self.original_root
        app_module.ENV_PATH = self.original_env_path
        app_module.RUNTIME_CONFIG.paths = self.original_runtime_paths
        app_module.RUNTIME_CONFIG.environment = self.original_runtime_environment
        self.tempdir.cleanup()

    def test_location_compensation_and_sales_filters_cover_boundary_cases(self):
        policy = DiscoveryPolicy(200_000)
        self.assertEqual(policy.location({"location": "Seattle, WA"})[0], True)
        self.assertEqual(policy.location({"location": "London, UK"})[0], False)
        self.assertEqual(policy.compensation({"snippet": "$150,000 per year"})[0], False)
        self.assertEqual(policy.compensation({"snippet": "Compensation not listed"})[0], True)
        self.assertEqual(policy.sales_role({"title": "Account Executive"})[0], False)

    def test_packet_path_disallows_traversal_and_accepts_packet_directory(self):
        app_module.APPLICATIONS_DIR.mkdir()
        packet = app_module.APPLICATIONS_DIR / "example-role"
        packet.mkdir()

        self.assertEqual(self.packet_storage.packet_directory("applications/example-role"), packet.resolve())
        with self.assertRaisesRegex(ValueError, "under applications"):
            self.packet_storage.packet_directory("../../outside")

    def test_corrupt_capture_is_recovered_and_emits_telemetry(self):
        with patch.object(app_module.RUNTIME_CONFIG, "environment", {"JOB_SEARCH_USE_CAPTURE_CACHE": "1"}):
            payload = {"url": "https://example.test"}
            path = app_module.capture_path("manual", "http_get", payload)
            path.parent.mkdir(parents=True)
            path.write_text("not-json", encoding="utf-8")
            events = []
            with patch.object(
                app_module.telemetry,
                "event",
                side_effect=lambda event_type, **fields: events.append((event_type, fields)),
            ):
                self.assertIsNone(app_module.read_capture("manual", "http_get", payload))
        self.assertEqual(events[0][1]["error_code"], "CAPTURE_CORRUPTION_RECOVERED")

    def test_config_endpoint_persists_validated_value(self):
        with patch.object(app_module.RUNTIME_CONFIG, "environment", {}):
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

        codex_cli.subprocess.run = lambda *_args, **_kwargs: CompletedProcess()
        app_module.api_logger.info = api_events.append
        app_module.event_logger.info = app_events.append
        environment = {"JOB_SEARCH_USE_CAPTURE_CACHE": "1", "JOB_SEARCH_ENABLE_FULL_CAPTURE": "0"}
        try:
            with patch.object(app_module.RUNTIME_CONFIG, "environment", environment):
                gateway = outbound_clients(
                    app_module.OBSERVABILITY,
                    clean_text,
                    clean_url,
                    source_id,
                    dedupe_results,
                    client=HttpClient(),
                ).gateway
                gateway.get("manual_posting", f"https://example.test/job?token={sentinels['token']}")
                codex_json_gateway(app_module.RUNTIME_CONFIG, app_module.OBSERVABILITY).complete(
                    "test-model", {"prompt": sentinels["prompt"]}, "redaction_test", force_refresh=True
                )
                captures = [path.read_text(encoding="utf-8") for path in app_module.CAPTURE_DIR.rglob("*.json")]
        finally:
            (
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
