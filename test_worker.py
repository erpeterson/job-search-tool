import tempfile
import unittest
from pathlib import Path

from job_search.task_repository import TaskRepository
from job_search.worker import process_one


class ManagedWorkerTests(unittest.TestCase):
    def test_process_one_claims_and_finishes_work_without_flask(self):
        with tempfile.TemporaryDirectory() as directory:
            repository = TaskRepository(Path(directory) / "tasks.sqlite3")
            repository.initialize(1)
            repository.create("task-1", "scorecards", [4], 2)

            processed = process_one(
                repository,
                "worker-a",
                lambda claim: ("complete", f"processed {claim['job_id']}"),
                now=lambda: 10,
            )

            self.assertTrue(processed, "A queued item must be claimed and processed.")
            self.assertEqual(repository.get("task-1")["status"], "complete")

    def test_process_one_records_worker_failure_without_losing_the_task(self):
        with tempfile.TemporaryDirectory() as directory:
            repository = TaskRepository(Path(directory) / "tasks.sqlite3")
            repository.initialize(1)
            repository.create("task-2", "scorecards", [5], 2)

            processed = process_one(
                repository,
                "worker-a",
                lambda _claim: (_ for _ in ()).throw(RuntimeError("Codex unavailable")),
                now=lambda: 10,
            )
            task = repository.get("task-2")

            self.assertTrue(processed)
            self.assertEqual(task["status"], "error", "A worker failure must be retained in durable task state.")
            self.assertIn("Codex unavailable", task["items"][0]["message"])
