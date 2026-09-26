"""Guard the repository-wide quality-gate configuration against regressions."""

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
