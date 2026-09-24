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

    def test_initialize_marks_interrupted_work_for_recovery(self):
        self.repository.create("task-2", "packets", [7], 10)
        self.repository.update("task-2", 11, status="running")

        recovered = self.repository.initialize(20)
        task = self.repository.get("task-2")

        self.assertEqual(recovered, 1)
        self.assertEqual(task["status"], "interrupted")
        self.assertIn("retry", task["message"])


if __name__ == "__main__":
    unittest.main()
