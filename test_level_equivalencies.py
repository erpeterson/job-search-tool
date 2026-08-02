import importlib.util
import tempfile
import unittest
from pathlib import Path


APP_PATH = Path(__file__).resolve().parent / "app.py"
SPEC = importlib.util.spec_from_file_location("job_search_app", APP_PATH)
job_search_app = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(job_search_app)


class LevelEquivalencyTests(unittest.TestCase):
    def setUp(self):
        self.tmpdir = tempfile.TemporaryDirectory()
        self.original_db_path = job_search_app.DB_PATH
        self.original_fetch_url = job_search_app.fetch_url
        self.original_log_event = job_search_app.log_event
        job_search_app.DB_PATH = Path(self.tmpdir.name) / "job_search.sqlite3"
        job_search_app.init_db()

    def tearDown(self):
        job_search_app.DB_PATH = self.original_db_path
        job_search_app.fetch_url = self.original_fetch_url
        job_search_app.log_event = self.original_log_event
        self.tmpdir.cleanup()

    def test_init_db_does_not_seed_level_equivalencies(self):
        with job_search_app.connect() as conn:
            count = conn.execute("SELECT COUNT(*) FROM level_equivalencies").fetchone()[0]

        self.assertEqual(count, 0)

    def test_ambiguous_title_is_unknown_and_does_not_fetch_or_cache(self):
        calls = []
        events = []

        def fake_fetch_url(service, url, force_refresh=False):
            calls.append((service, url, force_refresh))
            raise AssertionError("level lookup should not fetch external services")

        def fake_log_event(event_type, **fields):
            events.append((event_type, fields))

        job_search_app.fetch_url = fake_fetch_url
        job_search_app.log_event = fake_log_event

        with job_search_app.connect() as conn:
            result = job_search_app.lookup_level_equivalency(conn, "Atlassian", "Principal Engineer")
            count = conn.execute("SELECT COUNT(*) FROM level_equivalencies").fetchone()[0]

        self.assertIsNone(result)
        self.assertEqual(count, 0)
        self.assertTrue(
            any(
                event_type == "level_equivalency_unknown"
                and fields["company"] == "Atlassian"
                and fields["title"] == "Principal Engineer"
                for event_type, fields in events
            )
        )
        self.assertEqual(calls, [])

    def test_downlevel_title_estimate_is_cached_and_reused(self):
        calls = []

        def fake_fetch_url(service, url, force_refresh=False):
            calls.append((service, url, force_refresh))
            raise AssertionError("level lookup should not fetch external services")

        job_search_app.fetch_url = fake_fetch_url

        with job_search_app.connect() as conn:
            first = job_search_app.lookup_level_equivalency(conn, "ExampleCo", "Senior Software Engineer")
            second = job_search_app.lookup_level_equivalency(conn, "ExampleCo", "Senior Software Engineer")

        self.assertEqual(calls, [])
        self.assertEqual(first["oracle_level"], "BELOW_IC6")
        self.assertEqual(first["oracle_title"], "Below Architect-equivalent")
        self.assertEqual(first["downlevel"], 1)
        self.assertEqual(second["oracle_level"], "BELOW_IC6")

    def test_ic6_plus_title_estimate_is_cached(self):
        with job_search_app.connect() as conn:
            result = job_search_app.lookup_level_equivalency(conn, "ExampleCo", "Senior Principal Software Engineer")

        self.assertEqual(result["oracle_level"], "IC6+")
        self.assertEqual(result["oracle_title"], "Architect-equivalent or higher")
        self.assertEqual(result["downlevel"], 0)


if __name__ == "__main__":
    unittest.main()
