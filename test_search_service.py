import unittest

from job_search.application.search_service import SearchService


class FakeBoards:
    def __init__(self, results):
        self.results = results
        self.calls = []

    def fetch(self, board, keywords, location, *, force_refresh=False):
        self.calls.append((board, keywords, location, force_refresh))
        return self.results


class SearchServiceTests(unittest.TestCase):
    def test_discovers_results_and_applies_domain_visibility_policy(self):
        boards = FakeBoards(
            [
                {"title": "Architect", "gpt_score": 90},
                {"title": "Engineer", "downlevel": True},
            ]
        )
        service = SearchService(boards, gpt_threshold=80, user_threshold=0, gpt_scoring_enabled=True)

        decisions = service.discover("indeed", "architect", "Remote", force_refresh=True)

        self.assertEqual(boards.calls, [("indeed", "architect", "Remote", True)])
        self.assertTrue(decisions[0].tracked)
        self.assertFalse(decisions[1].tracked)
        self.assertIn("downlevel", decisions[1].reason)


if __name__ == "__main__":
    unittest.main()
