import unittest

from job_search.application.manual_job_service import ManualJobService


class FakeJobs:
    def __init__(self, create_result=7):
        self.create_result = create_result
        self.created = []

    def create_job(self, values):
        self.created.append(dict(values))
        return self.create_result

    def get_job_by_url(self, url):
        return {"id": 5, "url": url}


class ManualJobServiceTests(unittest.TestCase):
    def service(self, repository, scraper, availability=lambda: None):
        self.filtered = []
        self.scored = []
        self.failures = []
        self.skipped = []
        return ManualJobService(
            repository,
            scraper,
            lambda url: {"url": url, "company": "Fallback"},
            self.filtered.append,
            self.scored.append,
            availability,
            lambda operation, error, context: self.failures.append((operation, str(error), dict(context))),
            lambda job_id, reason: self.skipped.append((job_id, reason)),
            lambda: 100,
        )

    @staticmethod
    def values():
        return {
            "created_at": 100,
            "updated_at": 100,
            "url": "https://jobs.example.test/1",
            "pipeline": "primary",
            "status": "researching",
            "company": "",
            "title": "",
            "location": "",
            "posting_text": "",
            "notes": "",
        }

    def test_create_merges_scraped_data_and_runs_injected_workflows(self):
        repository = FakeJobs()
        service = self.service(repository, lambda _url, _force: {"company": "Example", "title": "Architect"})

        result = service.create(self.values(), force_refresh=True)

        self.assertEqual(result.job_id, 7)
        self.assertIsNone(result.score_error)
        self.assertEqual(repository.created[0]["company"], "Example")
        self.assertEqual(self.filtered, [7])
        self.assertEqual(self.scored, [7])

    def test_scrape_failure_is_observed_and_uses_fallback(self):
        repository = FakeJobs()

        def fail(_url, _force):
            raise RuntimeError("network unavailable")

        result = self.service(repository, fail, availability=lambda: "Codex scoring is disabled.").create(
            self.values(), force_refresh=False
        )

        self.assertEqual(result.scrape_error, "Job scrape failed (RuntimeError).")
        self.assertIn("Scrape failed", repository.created[0]["notes"])
        self.assertEqual(self.failures[0][0], "scrape")
        self.assertIn("Codex scoring skipped", result.score_error)
        self.assertEqual(self.skipped, [(7, "Codex scoring is disabled.")])

    def test_duplicate_returns_existing_job_without_scoring(self):
        repository = FakeJobs(create_result=None)
        result = self.service(repository, lambda url, _force: {"url": url}).create(self.values(), force_refresh=False)

        self.assertIsNone(result.job_id)
        self.assertEqual(result.existing_job["id"], 5)
        self.assertEqual(self.filtered, [])
        self.assertEqual(self.scored, [])

    def test_score_failure_keeps_job_and_hides_exception_content(self):
        repository = FakeJobs()
        failures = []

        def fail_score(_job_id):
            raise RuntimeError("private-scoring-token")

        service = ManualJobService(
            repository,
            lambda url, _force: {"url": url, "company": "Example"},
            lambda url: {"url": url},
            lambda _job_id: None,
            fail_score,
            lambda: None,
            lambda operation, error, context: failures.append((operation, type(error).__name__, context)),
            lambda *_args: None,
            lambda: 100,
        )

        result = service.create(self.values(), force_refresh=False)

        self.assertEqual(result.job_id, 7, "A failed score must not discard the created job.")
        self.assertEqual(result.score_error, "Automatic scoring failed (RuntimeError).")
        self.assertEqual(failures, [("score", "RuntimeError", {"job_id": 7})])
        self.assertNotIn("private-scoring-token", str(result))


if __name__ == "__main__":
    unittest.main()
