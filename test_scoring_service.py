import unittest

from job_search.application.scoring_service import ScoringService


class ScoringServiceTests(unittest.TestCase):
    def test_returns_missing_and_unavailable_without_invoking_score_port(self):
        calls = []
        missing = ScoringService(lambda _id: None, lambda _id: calls.append("score"), lambda: None)
        unavailable = ScoringService(lambda _id: {"id": 1}, lambda _id: calls.append("score"), lambda: "disabled")

        self.assertEqual(missing.score(1).state, "missing")
        self.assertEqual(unavailable.score(1).unavailable_reason, "disabled")
        self.assertEqual(calls, [])

    def test_scores_with_plain_callable_ports(self):
        service = ScoringService(lambda _id: {"id": 1}, lambda _id: {"total_score": 88}, lambda: None)

        result = service.score(1)

        self.assertEqual(result.state, "scored")
        self.assertEqual(result.raw_score, {"total_score": 88})


if __name__ == "__main__":
    unittest.main()
