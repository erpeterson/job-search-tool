"""Guard the repository-wide quality-gate configuration against regressions."""

import os
import subprocess
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parent


class QualityGateConfigurationTests(unittest.TestCase):
    def test_coverage_includes_the_root_composition_module(self):
        configuration = (ROOT / "pyproject.toml").read_text(encoding="utf-8")

        self.assertIn('source = ["."]', configuration, "Coverage must measure all first-party production modules.")
        self.assertNotIn(
            '"app.py"', configuration.partition("omit = ")[2], "The root composition module must not be omitted."
        )
        self.assertIn("fail_under = 80", configuration, "The branch-coverage floor must remain enforced at 80%.")

    def test_ci_runs_the_same_documented_quality_command(self):
        workflow = (ROOT / ".github" / "workflows" / "quality.yml").read_text(encoding="utf-8")

        self.assertIn("pull_request:", workflow, "Quality checks must run for pull requests.")
        self.assertIn("PYTHON_BIN=python ./quality.sh", workflow, "CI must invoke the documented quality gate.")

    def test_quality_gate_accepts_python_command_on_path(self):
        with tempfile.TemporaryDirectory() as directory:
            fake_python = Path(directory) / "fake-python"
            fake_python.write_text("#!/bin/sh\nprintf 'fake-python-called\\n'\n", encoding="utf-8")
            fake_python.chmod(0o755)
            environment = {
                **os.environ,
                "PATH": f"{directory}{os.pathsep}{os.environ['PATH']}",
                "PYTHON_BIN": fake_python.name,
            }

            result = subprocess.run(
                ["bash", str(ROOT / "quality.sh")],
                cwd=directory,
                env=environment,
                text=True,
                capture_output=True,
                check=False,
            )

        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(result.stdout.splitlines(), ["fake-python-called"] * 5, "Run every quality-gate command")

    def test_quality_gate_rejects_missing_python_command(self):
        with tempfile.TemporaryDirectory() as directory:
            environment = {**os.environ, "PYTHON_BIN": "missing-quality-test-python"}
            result = subprocess.run(
                ["bash", str(ROOT / "quality.sh")],
                cwd=directory,
                env=environment,
                text=True,
                capture_output=True,
                check=False,
            )

        self.assertEqual(result.returncode, 2, "Missing Python must fail before running the gate")
        self.assertIn("Python executable not found: missing-quality-test-python", result.stderr)
