import tempfile
import unittest
from pathlib import Path

from job_search.task_repository import TaskRepository


class TaskRepositoryTests(unittest.TestCase):
    def setUp(self):
        self.tempdir = tempfile.TemporaryDirectory()
        self.repository = TaskRepository(Path(self.tempdir.name) / "tasks.sqlite3")
        self.repository.initialize(1)

    def tearDown(self):
        self.tempdir.cleanup()

    def test_task_and_item_state_persist_across_repository_instances(self):
        self.repository.create("task-1", "scorecards", [3, 5], 10)
        self.repository.update("task-1", 11, status="running", current_job_id=3)
        self.repository.update_item("task-1", 3, 12, status="complete", message="scored")

        restored = TaskRepository(Path(self.tempdir.name) / "tasks.sqlite3").get("task-1")

        self.assertEqual(restored["status"], "running")
        self.assertEqual(restored["items"][0]["status"], "complete")

    def test_initialize_requeues_unfinished_work_for_a_managed_worker(self):
        self.repository.create("task-2", "packets", [7], 10)
        self.repository.update("task-2", 11, status="running")

        recovered = self.repository.initialize(20)
        task = self.repository.get("task-2")

        self.assertEqual(recovered, 1)
        self.assertEqual(task["status"], "queued")
        self.assertIn("managed worker", task["message"])

    def test_only_one_worker_can_claim_an_item_and_expired_lease_is_retryable(self):
        self.repository.create("task-3", "scorecards", [7], 10)

        first = self.repository.claim_next_item("worker-a", 20, 30)
        second = self.repository.claim_next_item("worker-b", 21, 30)
        retry = self.repository.claim_next_item("worker-b", 51, 30)

        self.assertEqual(first["job_id"], 7, "The first worker should receive the queued item.")
        self.assertIsNone(second, "A live lease must prevent duplicate processing by another worker.")
        self.assertEqual(retry["lease_owner"], "worker-b", "An expired lease must become retryable.")

    def test_only_lease_owner_can_complete_claim_and_task_counts_are_finalized(self):
        self.repository.create("task-4", "scorecards", [9], 10)
        self.assertIsNotNone(self.repository.claim_next_item("worker-a", 20, 30))

        denied = self.repository.complete_claim("task-4", 9, "worker-b", 21, status="complete", message="wrong owner")
        completed = self.repository.complete_claim("task-4", 9, "worker-a", 22, status="complete", message="scored")
        task = self.repository.get("task-4")

        self.assertFalse(denied, "A worker that does not own the lease must not complete the item.")
        self.assertTrue(completed, "The lease owner must be able to complete the item.")
        self.assertEqual(task["status"], "complete")
        self.assertEqual(task["completed"], 1)


if __name__ == "__main__":
    unittest.main()
