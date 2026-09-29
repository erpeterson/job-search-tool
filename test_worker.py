import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

from job_search.task_repository import TaskRepository
from job_search.worker import _main, process_one


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

            processed = process_one(
                repository,
                "worker-a",
                lambda _claim: (_ for _ in ()).throw(RuntimeError("Codex unavailable")),
                telemetry=SimpleNamespace(event=lambda *_args, **_kwargs: None),
                now=lambda: 10,
            )
            task = repository.get("task-2")

            self.assertTrue(processed)
            self.assertEqual(task["status"], "error", "A worker failure must be retained in durable task state.")
            self.assertIn("Codex unavailable", task["items"][0]["message"])
