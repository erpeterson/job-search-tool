import tempfile
import unittest
from pathlib import Path

from job_search.data_access.scheduler_repository import SchedulerLeaseRepository


class SchedulerLeaseRepositoryTests(unittest.TestCase):
    def test_only_one_scheduler_can_hold_a_live_lease_and_expired_lease_can_be_reclaimed(self):
        with tempfile.TemporaryDirectory() as directory:
            repository = SchedulerLeaseRepository(Path(directory) / "jobs.sqlite3")

            self.assertTrue(repository.acquire("scheduler-a", 10, 30))
            self.assertFalse(repository.acquire("scheduler-b", 20, 30))
            self.assertTrue(repository.acquire("scheduler-b", 41, 30))
