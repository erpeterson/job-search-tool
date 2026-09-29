import unittest

from job_search.application.job_service import JobService


class FakeJobs:
    def list_jobs(self, *, include_filtered=False):
        return [{"id": 1, "gpt_scorecard_json": '{"scope": 8}', "user_scorecard_json": ""}]

    def get_job(self, job_id):
        return {"id": job_id, "gpt_scorecard_json": "not-json", "user_scorecard_json": "{}"}


class JobServiceTests(unittest.TestCase):
    def test_delete_emits_event_only_after_repository_success(self):
        class FakeDeleteJobs:
            def __init__(self, deleted):
                self.deleted = deleted

            def get_job(self, _job_id):
                return {"company": "Example", "title": "Architect", "url": "https://example.test/1"}

            def delete_job(self, _job_id):
                return self.deleted

        events = []
        successful = JobService(FakeDeleteJobs(True), observe=lambda **event: events.append(event), now=lambda: 1)
        failed = JobService(FakeDeleteJobs(False), observe=lambda **event: events.append(event), now=lambda: 1)

        self.assertIsNotNone(successful.delete_job(7), "A deleted job should return its details.")
        self.assertIsNone(failed.delete_job(8), "A failed deletion should not report success.")
        self.assertEqual(len(events), 1, "Only a completed deletion should emit the event.")
        self.assertEqual(events[0]["event"], "manual_job_deleted")
        self.assertEqual(events[0]["job_id"], 7)

    def test_presents_repository_records_without_storage_dependency(self):
        events = []
        service = JobService(FakeJobs(), observe=lambda **event: events.append(event), now=lambda: 123)

        jobs = service.list_jobs()
        job = service.get_job(9)

        self.assertEqual(jobs[0]["gpt_scorecard"], {"scope": 8})
        self.assertEqual(jobs[0]["user_scorecard"], {})
        self.assertEqual(job["gpt_scorecard"], {})
        self.assertEqual(events[0]["error_code"], "JOB_SCORECARD_PARSE_RECOVERED")
        self.assertEqual(events[0]["record_id"], 9)


if __name__ == "__main__":
    unittest.main()
