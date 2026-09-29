import unittest

from job_search.application.rescrape_service import RescrapeService


class FakeJobs:
    def __init__(self, job):
        self.job = job
        self.updated = []

    def get_job(self, _job_id):
        return self.job

    def rescrape_job(self, job_id, current, scraped, notes, updated_at):
        self.updated.append((job_id, current, scraped, notes, updated_at))


class RescrapeServiceTests(unittest.TestCase):
    def test_rescrapes_existing_job_and_refreshes_visibility(self):
        repository = FakeJobs({"id": 4, "url": "https://jobs.example.test/4", "notes": "Original"})
        refreshed = []
        events = []
        service = RescrapeService(
            repository,
            lambda url, force: {"url": url, "title": "Architect", "force": force},
            refreshed.append,
            lambda: 100,
            lambda name, **fields: events.append((name, fields)),
        )

        result = service.rescrape(4, force_refresh=True)

        self.assertEqual(result.scraped["title"], "Architect")
        self.assertEqual(refreshed, [4])
        self.assertEqual(repository.updated[0][3], "Original\nRe-scraped posting URL.")
        self.assertEqual(repository.updated[0][4], 100)
        self.assertEqual(events[0][0], "manual_job_rescraped")
        self.assertEqual(events[0][1]["job_id"], 4)

    def test_returns_distinct_results_for_missing_job_and_missing_url(self):
        missing = RescrapeService(
            FakeJobs(None), lambda *_args: {}, lambda _id: None, lambda: 1, lambda *_args, **_kwargs: None
        )
        no_url = RescrapeService(
            FakeJobs({"id": 1, "url": ""}),
            lambda *_args: {},
            lambda _id: None,
            lambda: 1,
            lambda *_args, **_kwargs: None,
        )

        self.assertEqual(missing.rescrape(1, force_refresh=False).job, None)
        self.assertEqual(no_url.rescrape(1, force_refresh=False).scraped, None)


if __name__ == "__main__":
    unittest.main()
