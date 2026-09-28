"""Startup orchestration tests with fake ports and temporary persistence."""

import sqlite3
import tempfile
import unittest
from pathlib import Path

from job_search.application.startup_service import StartupService
from job_search.composition import startup_service
from job_search.task_repository import TaskRepository


class RecordingTelemetry:
    def __init__(self):
        self.events = []

    def event(self, name, **fields):
        self.events.append((name, fields))


class StartupServiceTests(unittest.TestCase):
    def test_use_case_seeds_defaults_and_reports_recovery(self):
        class Database:
            defaults = None

            def initialize_database(self, defaults):
                self.defaults = defaults

        class Tasks:
            def initialize(self):
                return 2

        database = Database()
        telemetry = RecordingTelemetry()
        recovered = StartupService(database, Tasks(), telemetry, "test-model").initialize()

        self.assertEqual(recovered, 2)
        self.assertEqual(database.defaults["codex_model"], "test-model")
        self.assertEqual(database.defaults["gpt_threshold"], "40")
        self.assertEqual(
            [name for name, _ in telemetry.events],
            ["startup_started", "background_tasks_recovered", "startup_succeeded"],
        )
        self.assertEqual(telemetry.events[1][1]["error_code"], "BACKGROUND_TASKS_RECOVERED")

    def test_fresh_database_and_running_task_recover_with_temporary_storage(self):
        with tempfile.TemporaryDirectory() as directory:
            database_path = Path(directory) / "nested" / "jobs.sqlite3"
            telemetry = RecordingTelemetry()
            service = startup_service(database_path, telemetry, "test-model")

            self.assertEqual(service.initialize(), 0, "Fresh startup should have no tasks to recover")
            with sqlite3.connect(database_path) as connection:
                query_count = connection.execute("SELECT COUNT(*) FROM search_queries").fetchone()[0]
                model = connection.execute("SELECT value FROM settings WHERE key = 'codex_model'").fetchone()[0]
            self.assertEqual(query_count, 8, "Startup should seed each pipeline on both job boards")
            self.assertEqual(model, "test-model", "Startup defaults must persist")

            tasks = TaskRepository(database_path)
            tasks.create("task-1", "packets", [7], 10)
            tasks.update("task-1", 11, status="running")
            self.assertEqual(service.initialize(), 1, "Restart should requeue a running task")
            self.assertEqual(tasks.get("task-1")["status"], "queued")
            self.assertIn("background_tasks_recovered", [name for name, _ in telemetry.events])
