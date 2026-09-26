import os
import subprocess
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parent


class StartupBoundaryTests(unittest.TestCase):
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
