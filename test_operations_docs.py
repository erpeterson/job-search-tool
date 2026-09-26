import unittest
from pathlib import Path


class OperationsDocumentationTests(unittest.TestCase):
    def test_documentation_describes_only_the_managed_lease_worker_model(self):
        readme = (Path(__file__).resolve().parent / "README.md").read_text(encoding="utf-8").lower()
        self.assertIn("python -m job_search.worker --database", readme)
        self.assertIn("python -m job_search.scheduler --database", readme)
        self.assertIn("lease", readme)
        self.assertNotIn("single-worker local-development dispatcher", readme)
        self.assertNotIn("become `interrupted`", readme)
