"""Unit tests for durable-task application use cases."""

import unittest

from job_search.application.background_task_service import BackgroundTaskService


class _Repository:
    def __init__(self):
        self.calls = []

    def initialize(self, timestamp):
        self.calls.append(("initialize", timestamp))
        return 2

    def create(self, task_id, operation, job_ids, created_at):
        self.calls.append(("create", task_id, operation, list(job_ids), created_at))
        return {"id": task_id, "operation": operation, "job_ids": list(job_ids)}

    def get(self, task_id):
        return {"id": task_id}

    def list(self, limit):
        return [{"limit": limit}]

    def update(self, task_id, updated_at, **updates):
        return {"id": task_id, "updated_at": updated_at, **updates}

    def update_item(self, task_id, job_id, updated_at, **updates):
        return {"id": task_id, "job_id": job_id, "updated_at": updated_at, **updates}


class BackgroundTaskServiceTests(unittest.TestCase):
    def test_start_creates_a_task_and_emits_a_structured_queue_event(self):
        repository = _Repository()
        events = []
        service = BackgroundTaskService(
            repository,
            now=lambda: 123,
            observe=lambda event, **fields: events.append((event, fields)),
            new_task_id=lambda: "task-123",
        )

        task = service.start("scorecards", [4, 9])

        self.assertEqual(task["id"], "task-123")
        self.assertEqual(repository.calls[0], ("create", "task-123", "scorecards", [4, 9], 123))
        self.assertEqual(
            events,
            [("background_task_queued", {"task_id": "task-123", "operation": "scorecards", "job_ids": [4, 9]})],
            "Queueing must retain an observable operation and all requested job IDs.",
        )

    def test_update_operations_share_the_injected_clock(self):
        service = BackgroundTaskService(_Repository(), now=lambda: 456, observe=lambda *_args, **_kwargs: None)

        initialized = service.initialize()
        task = service.update("task-1", status="running")
        item = service.update_item("task-1", 7, status="complete")

        self.assertEqual(initialized, 2)
        self.assertEqual(task, {"id": "task-1", "updated_at": 456, "status": "running"})
        self.assertEqual(item, {"id": "task-1", "job_id": 7, "updated_at": 456, "status": "complete"})
