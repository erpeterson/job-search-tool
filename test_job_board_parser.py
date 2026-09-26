import unittest

from job_search.data_access.job_board_parser import JobBoardParser


class JobBoardParserTests(unittest.TestCase):
    def test_parses_linkedin_cards_without_http_or_framework_dependencies(self):
        parser = JobBoardParser(
            lambda value: " ".join(value.split()), lambda value: value.split("?")[0], lambda _: "id"
        )

        jobs = parser.linkedin(
            '<li><a class="base-card__full-link" href="https://example.test/jobs/42?token=x">Link</a>'
            '<h3 class="base-search-card__title"> Architect </h3><h4 class="base-search-card__subtitle"> Example </h4></li>',
            "Remote",
        )

        self.assertEqual(jobs[0]["source_job_id"], "id")
        self.assertEqual(jobs[0]["company"], "Example")


if __name__ == "__main__":
    unittest.main()
