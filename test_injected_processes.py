"""Composed managed processes with temporary persistence and fake external ports."""

import tempfile
import unittest
from pathlib import Path

from job_search.composition import (
    database_session,
    observability,
    runtime_configuration,
    scheduler_process_dependencies,
    startup_service,
    worker_process_dependencies,
)
from job_search.worker import process_one


class InjectedProcessTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        root = Path(temporary.name)
        self.database = root / "jobs.sqlite3"
        self.configuration = runtime_configuration(root, {})
        self.observed = observability(self.configuration)
        startup_service(self.database, self.observed.telemetry, "").initialize()

    def test_composed_worker_persists_fake_codex_score_and_completes_task(self):
        class FakeScorer:
            def score(self, _connection, _job, *, force_refresh):
                self.force_refresh = force_refresh
                return {
                    "total_score": 88,
                    "pipeline": "Executive IC",
                    "scorecard": {},
                    "level_assessment": "IC6-equivalent",
                    "downlevel": False,
                    "rationale": "Test score",
                }

        class UnusedDraft:
            def generate(self, _job):
                raise AssertionError("A score task must not invoke the Pandoc-backed packet path.")

        with database_session(self.database) as connection:
            job_id = connection.execute(
                "INSERT INTO jobs(created_at, updated_at, company, title, pipeline, status) "
                "VALUES (1, 1, 'ExampleCo', 'Architect', 'Executive IC', 'researching')"
            ).lastrowid
        scorer = FakeScorer()
        process = worker_process_dependencies(
            self.database,
            configuration=self.configuration,
            observed=self.observed,
            scorer=scorer,
            draft=UnusedDraft(),
        )
        process.repository.initialize(1)
        process.repository.create("worker-task", "scorecards", [job_id], 2)

        processed = process_one(
            process.repository,
            "worker-test",
            process.processor.process,
            telemetry=process.observability.telemetry,
            now=lambda: 10,
        )

        self.assertIs(process.observability, self.observed, "The worker must retain injected telemetry.")
        self.assertTrue(processed, "The composed worker must claim its durable task.")
        self.assertFalse(scorer.force_refresh, "The worker should use the normal score path.")
        self.assertEqual(process.repository.get("worker-task")["status"], "complete")
        with database_session(self.database) as connection:
            saved = connection.execute("SELECT gpt_score FROM jobs WHERE id = ?", (job_id,)).fetchone()
        self.assertEqual(saved["gpt_score"], 88, "The injected fake Codex score must persist through SQLite.")

    def test_composed_scheduler_uses_lease_and_fake_board_without_codex(self):
        class FakeBoard:
            def __init__(self):
                self.calls = []

            def fetch(self, board, keywords, location, *, force_refresh):
                self.calls.append((board, keywords, location, force_refresh))
                return [
                    {
                        "board": board,
                        "company": "SalesCo",
                        "title": "Sales Director",
                        "location": "Remote",
                        "url": "https://example.test/sales",
                    }
                ]

        class UnusedCodex:
            def complete(self, *_args, **_kwargs):
                raise AssertionError("A rejected discovery must not call Codex.")

        class UnusedScorer:
            def score_discovery(self, *_args, **_kwargs):
                raise AssertionError("A rejected discovery must not be scored.")

        with database_session(self.database) as connection:
            connection.execute("UPDATE search_queries SET enabled = 0")
            connection.execute(
                "INSERT INTO search_queries(board, pipeline, keywords, location, enabled, created_at, criteria) "
                "VALUES ('indeed', 'Executive IC', 'architect', 'Remote', 1, 1, 'test')"
            )
        board = FakeBoard()
        process = scheduler_process_dependencies(
            self.database,
            configuration=self.configuration,
            observed=self.observed,
            gateway=UnusedCodex(),
            scorer=UnusedScorer(),
            boards=board,
        )

        acquired = process.lease.acquire("scheduler-test", 10, 100)
        result = process.search.run(trigger="scheduled", force_refresh=True) if acquired else None

        self.assertIs(process.observability, self.observed, "The scheduler must retain injected telemetry.")
        self.assertTrue(acquired, "The composed scheduler must acquire the durable lease.")
        self.assertEqual(board.calls, [("indeed", "architect", "Remote", True)])
        self.assertEqual(result["found_count"], 1)
        self.assertEqual(result["rejected_count"], 1)
        with database_session(self.database) as connection:
            count = connection.execute("SELECT COUNT(*) FROM discovered_jobs").fetchone()[0]
        self.assertEqual(count, 1, "The injected fake HTTP result must be recorded durably.")


if __name__ == "__main__":
    unittest.main()
