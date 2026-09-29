import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import Mock

from job_search.data_access.capture_store import CaptureStore
from job_search.data_access.environment_file import update_environment_file
from job_search.data_access.job_board_client import JobBoardClient
from job_search.data_access.model_output_parser import parse_model_json


class ModelOutputParserTests(unittest.TestCase):
    def test_parses_fenced_json_and_json_embedded_in_explanation(self):
        telemetry = Mock()
        self.assertEqual(parse_model_json('```json\n{"score": 8}\n```', telemetry), {"score": 8})
        self.assertEqual(parse_model_json('Result follows: {"score": 8} Thanks.', telemetry), {"score": 8})

    def test_rejects_empty_or_non_json_model_output(self):
        telemetry = Mock()
        with self.assertRaisesRegex(json.JSONDecodeError, "empty response"):
            parse_model_json("", telemetry)
        with self.assertRaises(json.JSONDecodeError):
            parse_model_json("No structured result", telemetry)


class EnvironmentFileTests(unittest.TestCase):
    def test_preserves_comments_and_replaces_or_appends_values(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / ".env"
            path.write_text("# local settings\nCODEX_MODEL=old\n\n", encoding="utf-8")
            update_environment_file(path, {"CODEX_MODEL": "test-model", "CODEX_CLI_PATH": "codex"})
            contents = path.read_text(encoding="utf-8")
        self.assertIn("# local settings", contents)
        self.assertIn("CODEX_MODEL=test-model", contents)
        self.assertIn("CODEX_CLI_PATH=codex", contents)


class CaptureStoreTests(unittest.TestCase):
    def test_replays_redacted_capture_and_emits_events(self):
        events = []
        with tempfile.TemporaryDirectory() as directory:
            store = CaptureStore(
                Path(directory),
                lambda: True,
                lambda: False,
                lambda value, **_kwargs: value,
                lambda event, **_fields: events.append(event),
            )
            store.write("board", "get", {"url": "https://example.test"}, {"status_code": 200}, {"elapsed_ms": 4})
            replay = store.read("board", "get", {"url": "https://example.test"})
        self.assertEqual(replay["response"]["status_code"], 200)
        self.assertIn("capture_write", events)
        self.assertIn("capture_replay", events)

    def test_recovers_from_corrupt_capture_and_skips_forced_refresh(self):
        events = []
        with tempfile.TemporaryDirectory() as directory:
            store = CaptureStore(
                Path(directory),
                lambda: True,
                lambda: False,
                lambda value, **_kwargs: value,
                lambda event, **_fields: events.append(event),
            )
            path = store.path("board", "get", {"url": "https://example.test"})
            path.parent.mkdir(parents=True)
            path.write_text("not-json", encoding="utf-8")
            self.assertIsNone(store.read("board", "get", {"url": "https://example.test"}))
            self.assertIsNone(store.read("board", "get", {"url": "https://example.test"}, force_refresh=True))
        self.assertIn("capture_corruption_recovered", events)
        self.assertIn("capture_bypass", events)


class JobBoardClientTests(unittest.TestCase):
    def test_fetches_linkedin_and_rejects_empty_posting_url(self):
        calls = []

        class Response:
            text = "listing-html"

            @staticmethod
            def raise_for_status():
                return None

        class BoardParser:
            @staticmethod
            def linkedin(html, location):
                return [{"url": html, "location": location}]

        client = JobBoardClient(
            lambda *args, **kwargs: (calls.append((args, kwargs)) or Response()),
            BoardParser(),
            object(),
            object(),
            lambda value: " ".join((value or "").split()),
            lambda value: (value or "").strip(),
            lambda board, value: f"{board}:{value}",
            lambda values: values,
        )
        self.assertEqual(client.linkedin("principal engineer", "Remote")[0]["location"], "Remote")
        self.assertIn("keywords=principal+engineer", calls[0][0][1])
        with self.assertRaisesRegex(ValueError, "URL is required"):
            client.scrape("   ")
