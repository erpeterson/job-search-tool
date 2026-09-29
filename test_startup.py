import io
import json
import os
import runpy
import subprocess
import unittest
from contextlib import redirect_stderr
from pathlib import Path
from unittest.mock import patch

ROOT = Path(__file__).resolve().parent


class StartupBoundaryTests(unittest.TestCase):
    def test_unexpected_composition_error_exits_one_without_private_details(self):
        stderr = io.StringIO()
        with patch(
            "job_search.composition.presentation_dependencies", side_effect=RuntimeError("private-startup-token")
        ):
            with redirect_stderr(stderr):
                with self.assertRaises(SystemExit) as exit_result:
                    runpy.run_path(str(ROOT / "app.py"))

        self.assertEqual(exit_result.exception.code, 1)
        self.assertEqual(stderr.getvalue().count("ERROR "), 1)
        record = json.loads(stderr.getvalue().removeprefix("ERROR "))
        self.assertEqual(record["error_code"], "STARTUP_UNHANDLED_EXCEPTION")
        self.assertEqual(record["component"], "web_startup")
        self.assertEqual(record["cause"], "RuntimeError")
        self.assertNotIn("private-startup-token", stderr.getvalue())

    def test_invalid_runtime_configuration_exits_two_with_one_error_record(self):
        result = self._run({"JOB_SEARCH_PORT": "not-a-port"})
        self.assertEqual(result.returncode, 2)
        self.assertEqual(result.stderr.count("ERROR "), 1)
        self.assertIn("STARTUP_CONFIGURATION_FAILED", result.stderr)
        self.assertNotIn("Traceback", result.stderr)

    def test_unsafe_external_binding_exits_two_with_one_error_record(self):
        result = self._run({"JOB_SEARCH_HOST": "0.0.0.0"})
        self.assertEqual(result.returncode, 2)
        self.assertEqual(result.stderr.count("ERROR "), 1)
        self.assertIn("STARTUP_CONFIGURATION_FAILED", result.stderr)
        self.assertNotIn("Traceback", result.stderr)

    @staticmethod
    def _run(updates):
        environment = {**os.environ, **updates}
        return subprocess.run(
            [str(ROOT / ".venv" / "bin" / "python"), "app.py"],
            cwd=ROOT,
            env=environment,
            text=True,
            capture_output=True,
            timeout=10,
            check=False,
        )
