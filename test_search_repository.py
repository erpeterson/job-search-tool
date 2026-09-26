import tempfile
import unittest
from pathlib import Path

from job_search.data_access.schema import initialize_schema
from job_search.data_access.search_repository import SqliteSearchRepository
from job_search.data_access.sqlite import connection


class SearchRepositoryTests(unittest.TestCase):
    def test_persists_run_lifecycle_without_framework_dependency(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "jobs.sqlite3"
            with connection(path) as database:
                initialize_schema(database)
                database.execute(
                    "INSERT INTO search_queries(board, keywords, created_at) VALUES ('indeed', 'architect', 1)"
                )
            repository = SqliteSearchRepository(lambda: connection(path))
            run_id = repository.start_run(10, "manual")
            repository.mark_query_run(1, 11)
            run = repository.finish_run(
                run_id,
                {"completed_at": 12, "message": "", "found_count": 1, "tracked_count": 1, "rejected_count": 0},
            )
            repository.record_discovery(
                {
                    "run_id": run_id,
                    "query_id": 1,
                    "created_at": 12,
                    "board": "indeed",
                    "source_job_id": "1",
                    "company": "Example",
                    "title": "Architect",
                    "location": "Remote",
                    "url": "https://example.test/1",
                    "snippet": "",
                    "gpt_score": None,
                    "gpt_rationale": None,
                    "gpt_scorecard_json": "{}",
                    "level_assessment": "",
                    "downlevel": 0,
                    "decision": "rejected",
                    "rejection_reason": "test",
                    "tracked_job_id": None,
                }
            )
            with connection(path) as database:
                discoveries = database.execute("SELECT decision FROM discovered_jobs").fetchall()

        self.assertEqual(run["status"], "complete")
        self.assertEqual(run["tracked_count"], 1)
        self.assertEqual(discoveries[0]["decision"], "rejected")


if __name__ == "__main__":
    unittest.main()
