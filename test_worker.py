import tempfile
import threading
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

from job_search.composition import database_session
from job_search.task_repository import TaskRepository
from job_search.worker import HeartbeatRenewalError, _main, process_one


class ManagedWorkerTests(unittest.TestCase):
    def test_cli_builds_processor_for_requested_database(self):
        database = Path("worker-test.sqlite3")
        repository = SimpleNamespace(initialize=lambda _now: None)
        processor = SimpleNamespace(process=lambda _claim: ("complete", "done"))
        process = SimpleNamespace(
            repository=repository, processor=processor, observability=SimpleNamespace(telemetry=object())
        )
        with (
            patch("sys.argv", ["worker", "--database", str(database)]),
            patch("job_search.worker.worker_process_dependencies", return_value=process) as compose,
            patch("job_search.worker.process_one", side_effect=KeyboardInterrupt) as process_item,
        ):
            with self.assertRaises(KeyboardInterrupt, msg="The test must stop the polling loop deterministically."):
                _main()

        compose.assert_called_once_with(database)
        self.assertIs(process_item.call_args.args[0], repository)
        self.assertIs(process_item.call_args.args[2], processor.process)
        self.assertIs(process_item.call_args.kwargs["telemetry"], process.observability.telemetry)

    def test_process_one_claims_and_finishes_work_without_flask(self):
        with tempfile.TemporaryDirectory() as directory:
            repository = TaskRepository(Path(directory) / "tasks.sqlite3")
            repository.initialize(1)
            repository.create("task-1", "scorecards", [4], 2)

            processed = process_one(
                repository,
                "worker-a",
                lambda claim: ("complete", f"processed {claim['job_id']}"),
                telemetry=SimpleNamespace(event=lambda *_args, **_kwargs: None),
                now=lambda: 10,
            )

            self.assertTrue(processed, "A queued item must be claimed and processed.")
            self.assertEqual(repository.get("task-1")["status"], "complete")

    def test_process_one_records_worker_failure_without_losing_the_task(self):
        with tempfile.TemporaryDirectory() as directory:
            repository = TaskRepository(Path(directory) / "tasks.sqlite3")
            repository.initialize(1)
            repository.create("task-2", "scorecards", [5], 2)
            events = []

            processed = process_one(
                repository,
                "worker-a",
                lambda _claim: (_ for _ in ()).throw(RuntimeError("private-token")),
                telemetry=SimpleNamespace(event=lambda name, **fields: events.append((name, fields))),
                now=lambda: 10,
            )
            task = repository.get("task-2")

            self.assertTrue(processed)
            self.assertEqual(task["status"], "error", "A worker failure must be retained in durable task state.")
            self.assertEqual(task["items"][0]["message"], "Worker processing failed: RuntimeError")
            self.assertEqual(len(events), 1, "One processor failure must emit exactly one event.")
            self.assertEqual(events[0][0], "worker_processor_failed")
            self.assertEqual(events[0][1]["error_code"], "WORKER_PROCESSOR_FAILED")
            self.assertEqual(events[0][1]["task_id"], "task-2")
            self.assertEqual(events[0][1]["job_id"], 5)
            self.assertEqual(events[0][1]["cause"], "RuntimeError")
            self.assertNotIn("private-token", str(events), "Telemetry must not retain exception secrets.")

    def test_heartbeat_exception_marks_owned_claim_failed_and_stops_worker(self):
        with tempfile.TemporaryDirectory() as directory:
            repository = TaskRepository(Path(directory) / "tasks.sqlite3")
            repository.initialize(1)
            repository.create("task-heartbeat", "scorecards", [8], 2)
            renewal_attempted = threading.Event()
            events = []

            class FailingRenewal:
                def claim_next_item(self, *args):
                    return repository.claim_next_item(*args)

                def renew_claim(self, *_args):
                    renewal_attempted.set()
                    raise RuntimeError("private-heartbeat-token")

                def complete_claim(self, *args, **kwargs):
                    return repository.complete_claim(*args, **kwargs)

            def process(_claim):
                self.assertTrue(renewal_attempted.wait(1), "The test must observe the heartbeat attempt.")
                return "complete", "scored"

            with self.assertRaisesRegex(HeartbeatRenewalError, "lease renewal failed"):
                process_one(
                    FailingRenewal(),
                    "worker-a",
                    process,
                    telemetry=SimpleNamespace(event=lambda name, **fields: events.append((name, fields))),
                    now=lambda: 10,
                    lease_seconds=3,
                    wait_for_heartbeat=lambda _interval: False,
                )

            task = repository.get("task-heartbeat")
            self.assertEqual(task["status"], "error", "The failed lease must not commit a success result.")
            self.assertEqual(task["items"][0]["message"], "Worker lease renewal failed.")
            self.assertEqual(len(events), 1, "One renewal failure must emit exactly one event.")
            self.assertEqual(events[0][0], "worker_heartbeat_failed")
            self.assertEqual(events[0][1]["error_code"], "WORKER_HEARTBEAT_FAILED")
            self.assertEqual(events[0][1]["task_id"], "task-heartbeat")
            self.assertEqual(events[0][1]["job_id"], 8)
            self.assertEqual(events[0][1]["cause"], "RuntimeError")
            self.assertNotIn("private-heartbeat-token", str(events))

    def test_lost_heartbeat_lease_never_overwrites_a_new_owner(self):
        with tempfile.TemporaryDirectory() as directory:
            database = Path(directory) / "tasks.sqlite3"
            repository = TaskRepository(database)
            repository.initialize(1)
            repository.create("task-stolen", "scorecards", [9], 2)
            renewal_attempted = threading.Event()
            events = []
            replacements = []

            class StolenLease:
                def claim_next_item(self, *args):
                    return repository.claim_next_item(*args)

                def renew_claim(self, *_args):
                    replacement = repository.claim_next_item("worker-b", 14, 3)
                    renewal_attempted.set()
                    replacements.append(replacement)
                    return False

                def complete_claim(self, *args, **kwargs):
                    return repository.complete_claim(*args, **kwargs)

            def process(_claim):
                self.assertTrue(renewal_attempted.wait(1))
                return "complete", "scored"

            with self.assertRaises(HeartbeatRenewalError):
                process_one(
                    StolenLease(),
                    "worker-a",
                    process,
                    telemetry=SimpleNamespace(event=lambda name, **fields: events.append((name, fields))),
                    now=lambda: 10,
                    lease_seconds=3,
                    wait_for_heartbeat=lambda _interval: False,
                )

            self.assertEqual(replacements[0]["lease_owner"], "worker-b")
            with database_session(database) as connection:
                item = connection.execute(
                    "SELECT status, lease_owner FROM background_task_items WHERE task_id = 'task-stolen'"
                ).fetchone()
            self.assertEqual((item["status"], item["lease_owner"]), ("running", "worker-b"))
            self.assertEqual([name for name, _fields in events], ["worker_heartbeat_failed"])
