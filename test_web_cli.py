"""Web startup reports fatal errors without exposing private exception text."""

import io
import json
import unittest
from contextlib import redirect_stderr, redirect_stdout
from pathlib import Path
from types import SimpleNamespace

from job_search.presentation.cli import main, startup_failure


class WebCliTests(unittest.TestCase):
    def test_runtime_failure_emits_one_event_and_returns_nonzero(self):
        events = []
        dependencies = SimpleNamespace(
            startup_service=SimpleNamespace(initialize=lambda: None),
            configuration=SimpleNamespace(settings=SimpleNamespace(host="127.0.0.1", port=5050, debug=False)),
            database_path=Path("jobs.sqlite3"),
            observability=SimpleNamespace(
                telemetry=SimpleNamespace(event=lambda name, **fields: events.append((name, fields)))
            ),
        )

        class FailingApplication:
            extensions = {"job_search.dependencies": dependencies}

            def run(self, **_kwargs):
                raise RuntimeError("private-web-token")

        stderr = io.StringIO()
        with redirect_stdout(io.StringIO()), redirect_stderr(stderr):
            result = main(FailingApplication())

        self.assertEqual(result, 1)
        self.assertEqual([name for name, _fields in events], ["web_fatal_failure"])
        self.assertEqual(events[0][1]["error_code"], "WEB_FATAL_FAILURE")
        self.assertEqual(events[0][1]["cause"], "RuntimeError")
        self.assertEqual(stderr.getvalue().count("ERROR "), 1)
        self.assertEqual(json.loads(stderr.getvalue().removeprefix("ERROR "))["error_code"], "WEB_FATAL_FAILURE")
        self.assertNotIn("private-web-token", str(events) + stderr.getvalue())

    def test_precomposition_failure_uses_one_stderr_fallback_record(self):
        stderr = io.StringIO()
        with redirect_stderr(stderr):
            with self.assertRaises(SystemExit) as exit_result:
                startup_failure(RuntimeError("private-startup-token"), controlled=False)

        self.assertEqual(exit_result.exception.code, 1)
        self.assertEqual(stderr.getvalue().count("ERROR "), 1)
        record = json.loads(stderr.getvalue().removeprefix("ERROR "))
        self.assertEqual(record["error_code"], "STARTUP_UNHANDLED_EXCEPTION")
        self.assertNotIn("private-startup-token", stderr.getvalue())


if __name__ == "__main__":
    unittest.main()
