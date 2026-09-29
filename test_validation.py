import unittest

from job_search.validation import (
    RequestValidationError,
    boolean,
    choice,
    environment_value,
    http_url,
    integer,
    require_json_object,
)


class ValidationTests(unittest.TestCase):
    def test_requires_json_object(self):
        with self.assertRaisesRegex(RequestValidationError, "JSON object"):
            require_json_object([])

    def test_accepts_absolute_http_url(self):
        self.assertEqual(http_url(" https://example.com/jobs/1 "), "https://example.com/jobs/1")

    def test_rejects_non_http_url(self):
        with self.assertRaisesRegex(RequestValidationError, "http or https"):
            http_url("file:///private/resume.md")

    def test_rejects_private_network_url(self):
        with self.assertRaisesRegex(RequestValidationError, "private or reserved"):
            http_url("http://127.0.0.1:5050/admin")

    def test_rejects_malformed_ip_shaped_host(self):
        with self.assertRaisesRegex(RequestValidationError, "invalid IP address"):
            http_url("https://127.0.0.999/job")

    def test_choice_and_integer_boundaries(self):
        self.assertEqual(choice("target", "status", {"target"}, required=True), "target")
        self.assertEqual(integer("100", "score", minimum=0, maximum=100), 100)
        with self.assertRaisesRegex(RequestValidationError, "between 0 and 100"):
            integer(101, "score", minimum=0, maximum=100)

    def test_rejects_environment_file_injection(self):
        with self.assertRaisesRegex(RequestValidationError, "control characters"):
            environment_value("codex\nJOB_SEARCH_ENABLE_GPT_SCORING=1", "CODEX_CLI_PATH")

    def test_boolean_rejects_truthy_strings(self):
        with self.assertRaisesRegex(RequestValidationError, "JSON boolean"):
            boolean("true", "force_refresh")


if __name__ == "__main__":
    unittest.main()
