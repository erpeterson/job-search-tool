import unittest

from job_search.application.job_service import JobService


class FakeJobs:
    def list_jobs(self, *, include_filtered=False):
        return [{"id": 1, "gpt_scorecard_json": '{"scope": 8}', "user_scorecard_json": ""}]

    def get_job(self, job_id):
        return {"id": job_id, "gpt_scorecard_json": "not-json", "user_scorecard_json": "{}"}


class JobServiceTests(unittest.TestCase):
    def test_presents_repository_records_without_storage_dependency(self):
        events = []
        service = JobService(FakeJobs(), observe=lambda **event: events.append(event))

        jobs = service.list_jobs()
        job = service.get_job(9)

        self.assertEqual(jobs[0]["gpt_scorecard"], {"scope": 8})
        self.assertEqual(jobs[0]["user_scorecard"], {})
        self.assertEqual(job["gpt_scorecard"], {})
        self.assertEqual(events[0]["error_code"], "JOB_SCORECARD_PARSE_RECOVERED")
        self.assertEqual(events[0]["record_id"], 9)


if __name__ == "__main__":
    unittest.main()
