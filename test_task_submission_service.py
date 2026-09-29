"""Unit tests for durable-task availability and queueing policy."""

import unittest

from job_search.application.task_submission_service import TaskSubmissionService


class FakeTasks:
    def __init__(self):
        self.started = []

    def start(self, operation, job_ids):
        self.started.append((operation, list(job_ids)))
        return {"operation": operation, "job_ids": list(job_ids)}


class TaskSubmissionServiceTests(unittest.TestCase):
    def test_disabled_scoring_does_not_enqueue(self):
        tasks = FakeTasks()
        service = TaskSubmissionService(tasks, lambda: False, lambda: True, lambda: "codex")

        result = service.submit("scorecards", [7])

        self.assertIsNone(result.task)
        self.assertIn("disabled", result.unavailable_reason)
        self.assertEqual(tasks.started, [], "Disabled scoring must not create a durable task.")

    def test_unavailable_cli_blocks_packet_queue(self):
        tasks = FakeTasks()
        service = TaskSubmissionService(tasks, lambda: True, lambda: False, lambda: "missing-codex")

        result = service.submit("application_packets", [7])

        self.assertIsNone(result.task)
        self.assertIn("missing-codex", result.unavailable_reason)
        self.assertEqual(tasks.started, [], "Missing CLI must not create a durable task.")

    def test_available_task_is_enqueued_with_validated_job_ids(self):
        tasks = FakeTasks()
        service = TaskSubmissionService(tasks, lambda: True, lambda: True, lambda: "codex")

        result = service.submit("scorecards", [7, 9])

        self.assertEqual(result.task["operation"], "scorecards")
        self.assertEqual(tasks.started, [("scorecards", [7, 9])])


if __name__ == "__main__":
    unittest.main()
