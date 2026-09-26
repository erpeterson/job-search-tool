import unittest

from job_search.application.filtering_service import FilteringService


class FakeFilterRepository:
    def __init__(self):
        self.jobs = {
            1: {"company": "Example", "title": "Architect", "gpt_score": 90, "user_score": None, "downlevel": 0},
            2: {"company": "Example", "title": "Engineer", "gpt_score": 90, "user_score": None, "downlevel": 1},
        }
        self.saved = []

    def settings(self):
        return {"gpt_threshold": "80", "user_threshold": "60"}

    def job_for_filtering(self, job_id):
        return self.jobs.get(job_id)

    def all_job_ids(self):
        return list(self.jobs)

    def save_filter_decision(self, job_id, filtered, updated_at):
        self.saved.append((job_id, filtered, updated_at))


class FilteringServiceTests(unittest.TestCase):
    def test_refreshes_all_jobs_using_a_plain_repository_fake(self):
        repository = FakeFilterRepository()
        service = FilteringService(repository, lambda: 123, gpt_scoring_enabled=True)

        decisions = service.refresh_all()

        self.assertEqual([(job_id, item.filtered) for job_id, item in decisions], [(1, False), (2, True)])
        self.assertEqual(repository.saved, [(1, False, 123), (2, True, 123)])


if __name__ == "__main__":
    unittest.main()
