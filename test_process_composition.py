"""Managed process builders use their own database paths and fake boundaries."""

import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from job_search import scheduler, worker
from job_search.composition import (
    database_session,
    runtime_configuration,
    scheduler_process_dependencies,
    worker_process_dependencies,
)
from job_search.data_access.schema import initialize_schema
from job_search.task_repository import TaskRepository


class FakeTelemetry:
    def __init__(self):
        self.events = []

    def event(self, name, **fields):
        self.events.append((name, fields))


class FakeObserved:
    def __init__(self):
        self.telemetry = FakeTelemetry()


class FakeScorer:
    def score(self, *_args, **_kwargs):
        return {"total_score": 88, "pipeline": "Executive IC", "scorecard": {}, "downlevel": False}

    def score_discovery(self, *_args, **_kwargs):
        raise AssertionError("Disabled discovery scoring must not invoke the model")


class FakeDraft:
    def generate(self, _job):
        return {"path": "applications/fake", "name": "fake", "markdown_files": ["Resume.md"]}


class FakeGateway:
    def complete(self, *_args, **_kwargs):
        raise AssertionError("Disabled refinement must not invoke the model")


class FakeBoards:
    def __init__(self):
        self.calls = []

    def fetch(self, board, keywords, location, *, force_refresh):
        self.calls.append((board, keywords, location, force_refresh))
        return []


class ProcessCompositionTests(unittest.TestCase):
    def test_worker_cli_records_fake_model_failure_in_temporary_database(self):
        class FailingScorer:
            def score(self, *_args, **_kwargs):
                raise RuntimeError("fake model unavailable")

        with tempfile.TemporaryDirectory() as directory:
            database = Path(directory) / "worker.sqlite3"
            with database_session(database) as connection:
                initialize_schema(connection)
                job_id = connection.execute(
                    "INSERT INTO jobs(created_at, updated_at, company, title, pipeline, status) "
                    "VALUES (1, 1, 'Example', 'Architect', 'Executive IC', 'researching')"
                ).lastrowid
            repository = TaskRepository(database)
            repository.initialize(1)
            repository.create("failure-task", "scorecards", [job_id], 2)
            configuration = runtime_configuration(Path(directory), {})
            observed = FakeObserved()
            real_process_one = worker.process_one

            def process_then_stop(*args, **kwargs):
                self.assertTrue(real_process_one(*args, **kwargs), "The worker should claim the queued item.")
                raise KeyboardInterrupt

            def build(path):
                return worker_process_dependencies(
                    path, configuration=configuration, observed=observed, scorer=FailingScorer(), draft=FakeDraft()
                )

            with (
                patch("sys.argv", ["worker", "--database", str(database)]),
                patch("job_search.worker.worker_process_dependencies", side_effect=build),
                patch("job_search.worker.process_one", side_effect=process_then_stop),
            ):
                with self.assertRaises(KeyboardInterrupt):
                    worker._main()

            task = repository.get("failure-task")
            self.assertEqual(task["status"], "error", "The fake model failure must survive process exit.")
            self.assertEqual(task["items"][0]["message"], "Worker processing failed: RuntimeError")
            self.assertIn("worker_processor_failed", [name for name, _fields in observed.telemetry.events])

    def test_scheduler_cli_respects_existing_lease_in_temporary_database(self):
        with tempfile.TemporaryDirectory() as directory:
            database = Path(directory) / "scheduler.sqlite3"
            with database_session(database) as connection:
                initialize_schema(connection)
                connection.execute(
                    "INSERT INTO search_queries(board, pipeline, keywords, location, enabled, created_at) "
                    "VALUES ('indeed', 'Executive IC', 'architect', 'Remote', 1, 1)"
                )
            configuration = runtime_configuration(Path(directory), {})
            observed = FakeObserved()
            boards = FakeBoards()

            def build(path):
                return scheduler_process_dependencies(
                    path,
                    configuration=configuration,
                    observed=observed,
                    gateway=FakeGateway(),
                    scorer=FakeScorer(),
                    boards=boards,
                )

            with (
                patch("sys.argv", ["scheduler", "--database", str(database)]),
                patch("job_search.scheduler.scheduler_process_dependencies", side_effect=build),
            ):
                self.assertEqual(scheduler._main(), 0)
                self.assertEqual(boards.calls, [("indeed", "architect", "Remote", True)])
                with patch("job_search.scheduler.socket.gethostname", return_value="different-owner"):
                    self.assertEqual(scheduler._main(), 0, "A live lease should make the scheduler exit cleanly.")
                self.assertEqual(len(boards.calls), 1, "The denied scheduler must not run another search.")
            with database_session(database) as connection:
                count = connection.execute("SELECT COUNT(*) FROM search_runs").fetchone()[0]
            self.assertEqual(count, 1, "Only the lease owner should persist a search run.")

    def test_worker_builder_processes_fake_score_and_packet_without_web_bundle(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            database = root / "worker.sqlite3"
            with database_session(database) as connection:
                initialize_schema(connection)
                job_id = connection.execute(
                    "INSERT INTO jobs(created_at, updated_at, company, title, pipeline, status) VALUES (1, 1, 'Example', 'Architect', 'Executive IC', 'researching')"
                ).lastrowid
            config = runtime_configuration(root, {})
            observed = FakeObserved()
            with patch("job_search.composition.presentation_dependencies", side_effect=AssertionError("web bundle")):
                process = worker_process_dependencies(
                    database, configuration=config, observed=observed, scorer=FakeScorer(), draft=FakeDraft()
                )

            self.assertEqual(process.database_path, database)
            self.assertEqual(process.repository.initialize(1), 0)
            self.assertEqual(
                process.processor.process({"operation": "scorecards", "job_id": job_id}),
                ("complete", "Codex score 88"),
            )
            self.assertEqual(
                process.processor.process({"operation": "application_packets", "job_id": job_id}),
                ("complete", "applications/fake"),
            )
            with database_session(database) as connection:
                saved = connection.execute(
                    "SELECT gpt_score, application_packet_path FROM jobs WHERE id = ?", (job_id,)
                ).fetchone()
            self.assertEqual((saved["gpt_score"], saved["application_packet_path"]), (88, "applications/fake"))

    def test_scheduler_builder_uses_supplied_database_and_fake_board(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            database = root / "scheduler.sqlite3"
            with database_session(database) as connection:
                initialize_schema(connection)
                connection.execute(
                    "INSERT INTO search_queries(board, pipeline, keywords, location, enabled, created_at) VALUES ('indeed', 'Executive IC', 'architect', 'Remote', 1, 1)"
                )
            config = runtime_configuration(root, {})
            observed = FakeObserved()
            boards = FakeBoards()
            with patch("job_search.composition.presentation_dependencies", side_effect=AssertionError("web bundle")):
                process = scheduler_process_dependencies(
                    database,
                    configuration=config,
                    observed=observed,
                    gateway=FakeGateway(),
                    scorer=FakeScorer(),
                    boards=boards,
                )

            self.assertEqual(process.database_path, database)
            self.assertTrue(process.lease.acquire("test", 1, 60))
            run = process.search.run(trigger="scheduled", force_refresh=True)
            self.assertEqual(boards.calls, [("indeed", "architect", "Remote", True)])
            self.assertEqual(run["found_count"], 0)
            with database_session(database) as connection:
                trigger = connection.execute("SELECT trigger FROM search_runs ORDER BY id DESC LIMIT 1").fetchone()[0]
            self.assertEqual(trigger, "scheduled")


if __name__ == "__main__":
    unittest.main()
