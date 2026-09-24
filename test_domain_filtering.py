import unittest

from job_search.domain.filtering import decide_job_filter


class JobFilterPolicyTests(unittest.TestCase):
    def test_downlevel_job_is_filtered_with_a_reason(self):
        decision = decide_job_filter(
            {"downlevel": 1, "gpt_score": 90, "user_score": 90},
            gpt_threshold=40,
            user_threshold=60,
            gpt_scoring_enabled=True,
        )
        self.assertTrue(decision.filtered)
        self.assertIn("downlevel", decision.reasons[0])

    def test_gpt_score_only_filters_when_scoring_is_enabled(self):
        job = {"downlevel": 0, "gpt_score": 10, "user_score": None}
        disabled = decide_job_filter(job, gpt_threshold=40, user_threshold=60, gpt_scoring_enabled=False)
        enabled = decide_job_filter(job, gpt_threshold=40, user_threshold=60, gpt_scoring_enabled=True)
        self.assertFalse(disabled.filtered)
        self.assertTrue(enabled.filtered)


if __name__ == "__main__":
    unittest.main()
