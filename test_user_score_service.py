"""Unit tests for human-score persistence and filter refresh ordering."""

import json
import unittest

from job_search.application.job_scoring_policy import RUBRIC_FIELDS
from job_search.application.user_score_service import UserScoreService


class FakeJobs:
    def __init__(self, *, saved, events=None):
        self.saved = saved
        self.calls = []
        self.events = events

    def save_user_score(self, job_id, total, scorecard_json, rationale, updated_at):
        self.calls.append((job_id, total, scorecard_json, rationale, updated_at))
        if self.events is not None:
            self.events.append("saved")
        return self.saved


class UserScoreServiceTests(unittest.TestCase):
    def test_saves_derived_total_before_refreshing_filter(self):
        events = []
        jobs = FakeJobs(saved=True, events=events)
        service = UserScoreService(jobs, lambda _job_id: events.append("filtered"), now=lambda: 123)
        scorecard = {field: 8 for field in RUBRIC_FIELDS}

        saved = service.save(7, scorecard, None, "Strong fit")

        self.assertTrue(saved, "A persisted score should report success.")
        self.assertEqual(jobs.calls[0][0:2], (7, 80), "The total should be derived from the rubric.")
        self.assertEqual(json.loads(jobs.calls[0][2]), scorecard)
        self.assertEqual(jobs.calls[0][3:], ("Strong fit", 123))
        self.assertEqual(events, ["saved", "filtered"], "Filtering should run after score persistence.")

    def test_missing_job_does_not_refresh_filter(self):
        jobs = FakeJobs(saved=False)
        refreshed = []
        service = UserScoreService(jobs, refreshed.append, now=lambda: 456)

        saved = service.save(99, {field: 0 for field in RUBRIC_FIELDS}, 25, "")

        self.assertFalse(saved, "A missing job must report failure.")
        self.assertEqual(jobs.calls[0][1], 25, "An explicit total must be preserved.")
        self.assertEqual(refreshed, [], "A missing job must not trigger filtering.")


if __name__ == "__main__":
    unittest.main()
