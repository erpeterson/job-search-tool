"""Application-level bulk progress tests with fake scoring and packet ports."""

import unittest

from job_search.application.bulk_task_service import BulkTaskService
from job_search.application.task_execution_service import TaskExecutionService


class FakeProgress:
    def __init__(self):
        self.tasks = []
        self.items = []

    def update(self, task_id, **updates):
        self.tasks.append((task_id, updates))

    def update_item(self, task_id, job_id, **updates):
        self.items.append((task_id, job_id, updates))


class FakeTelemetry:
    def __init__(self):
        self.events = []

    def event(self, name, **fields):
        self.events.append((name, fields))


class BulkTaskServiceTests(unittest.TestCase):
    def test_score_run_records_success_and_missing_job_skip(self):
        class Operations:
            def job(self, job_id):
                return {"id": job_id} if job_id == 1 else None

            def score(self, _job_id):
                return {"total_score": 90}

            def generate_packet(self, _job_id):
                raise AssertionError("Packet port must not be used for scoring")

        progress = FakeProgress()
        telemetry = FakeTelemetry()
        service = BulkTaskService(progress, TaskExecutionService(Operations()), lambda: 10, telemetry)

        result = service.run("score-task", [1, 2], "scorecards")

        self.assertEqual(result, ("complete", 1, 1, 0))
        self.assertEqual(
            [entry[2]["status"] for entry in progress.items], ["running", "complete", "running", "skipped"]
        )
        self.assertEqual(progress.tasks[-1][1]["status"], "complete")
        self.assertEqual(telemetry.events[-1][0], "background_task_finished")

    def test_packet_run_records_success_skip_and_sanitized_failure(self):
        class Operations:
            def job(self, job_id):
                if job_id == 2:
                    return {"id": job_id, "application_packet_path": "applications/existing"}
                return {"id": job_id}

            def score(self, _job_id):
                raise AssertionError("Scoring port must not be used for packets")

            def generate_packet(self, job_id):
                if job_id == 3:
                    raise RuntimeError("secret prompt content")
                return {"path": "applications/complete"}

        progress = FakeProgress()
        telemetry = FakeTelemetry()
        service = BulkTaskService(progress, TaskExecutionService(Operations()), lambda: 20, telemetry)

        result = service.run("packet-task", [1, 2, 3], "application_packets")

        self.assertEqual(result, ("error", 1, 1, 1))
        self.assertEqual(progress.tasks[-1][1]["status"], "error")
        self.assertEqual(progress.items[-1][2]["status"], "error")
        failure = telemetry.events[0]
        self.assertEqual(failure[1]["error_code"], "BULK_APPLICATION_PACKET_FAILED")
        self.assertEqual(failure[1]["component"], "business.bulk_packets")
        self.assertEqual(failure[1]["operation"], "application_packets")
        self.assertEqual(failure[1]["task_id"], "packet-task")
        self.assertEqual(failure[1]["job_id"], 3)
        self.assertEqual(failure[1]["cause"], "RuntimeError")
        self.assertNotIn("secret prompt content", str(progress.items) + str(telemetry.events))
