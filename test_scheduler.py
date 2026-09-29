"""Scheduler entry-point tests with the process boundary replaced by fakes."""

import io
import json
import unittest
from contextlib import redirect_stderr
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock, patch

from job_search.scheduler import _main, main


class SchedulerEntryPointTests(unittest.TestCase):
    def test_main_returns_failure_with_one_sanitized_stderr_record(self):
        output = io.StringIO()
        with patch("job_search.scheduler._main", side_effect=RuntimeError("private-scheduler-token")):
            with redirect_stderr(output):
                result = main()

        self.assertEqual(result, 1)
        self.assertEqual(output.getvalue().count("ERROR "), 1)
        record = json.loads(output.getvalue().removeprefix("ERROR "))
        self.assertEqual(record["error_code"], "SCHEDULER_FATAL_FAILURE")
        self.assertEqual(record["cause"], "RuntimeError")
        self.assertNotIn("private-scheduler-token", output.getvalue())

    def test_composed_scheduler_fatal_error_emits_one_injected_event(self):
        events = []
        lease = Mock()
        lease.acquire.side_effect = RuntimeError("private-lease-token")
        process = SimpleNamespace(
            lease=lease,
            observability=SimpleNamespace(
                telemetry=SimpleNamespace(event=lambda name, **fields: events.append((name, fields)))
            ),
        )
        with (
            patch("sys.argv", ["scheduler", "--database", "scheduler-test.sqlite3"]),
            patch("job_search.scheduler.scheduler_process_dependencies", return_value=process),
        ):
            with self.assertRaises(RuntimeError):
                _main()

        self.assertEqual([name for name, _fields in events], ["scheduler_fatal_failure"])
        self.assertEqual(events[0][1]["error_code"], "SCHEDULER_FATAL_FAILURE")
        self.assertEqual(events[0][1]["cause"], "RuntimeError")
        self.assertNotIn("private-lease-token", str(events))

    def test_cli_runs_injected_search_after_lease_acquisition(self):
        database = Path("scheduler-test.sqlite3")
        lease = Mock()
        lease.acquire.return_value = True
        search = Mock()
        process = SimpleNamespace(lease=lease, search=search)

        with (
            patch("sys.argv", ["scheduler", "--database", str(database)]),
            patch("job_search.scheduler.scheduler_process_dependencies", return_value=process) as compose,
        ):
            result = _main()

        self.assertEqual(result, 0)
        compose.assert_called_once_with(database)
        lease.acquire.assert_called_once()
        search.run.assert_called_once_with(trigger="scheduled", force_refresh=True)

    def test_cli_does_not_search_without_lease(self):
        lease = Mock()
        lease.acquire.return_value = False
        search = Mock()
        process = SimpleNamespace(lease=lease, search=search)

        with (
            patch("sys.argv", ["scheduler", "--database", "scheduler-test.sqlite3"]),
            patch("job_search.scheduler.scheduler_process_dependencies", return_value=process),
        ):
            result = _main()

        self.assertEqual(result, 0)
        search.run.assert_not_called()


if __name__ == "__main__":
    unittest.main()
