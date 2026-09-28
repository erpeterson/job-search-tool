"""Scheduler entry-point tests with the process boundary replaced by fakes."""

import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock, patch

from job_search.scheduler import _main


class SchedulerEntryPointTests(unittest.TestCase):
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
