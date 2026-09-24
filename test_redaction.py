import unittest

from job_search.redaction import REDACTED, redact_headers, redact_url, redact_value


class RedactionTests(unittest.TestCase):
    def test_removes_url_credentials_and_query(self):
        self.assertEqual(redact_url("https://user:secret@example.com/path?token=abc"), "https://example.com/path")

    def test_removes_sensitive_headers_and_nested_payloads(self):
        value = redact_value({"prompt": "resume text", "url": "https://example.com/?token=secret", "note": "Bearer abc"})
        self.assertEqual(value["prompt"], REDACTED)
        self.assertEqual(value["url"], "https://example.com/")
        self.assertNotIn("abc", value["note"])
        self.assertEqual(redact_headers({"Authorization": "Bearer abc"})["Authorization"], REDACTED)


if __name__ == "__main__":
    unittest.main()
