import unittest

from job_search.errors import ClientInputError, translate_exception


class ErrorTranslationTests(unittest.TestCase):
    def test_typed_input_error_is_safe_and_actionable(self):
        mapped = translate_exception(ClientInputError("field is required"))
        self.assertEqual(mapped.status_code, 400)
        self.assertEqual(mapped.body["error"], "field is required")
        self.assertEqual(mapped.error_code, "API_CLIENT_INPUT_INVALID")

    def test_unexpected_error_does_not_leak_details(self):
        mapped = translate_exception(RuntimeError("database password is secret"))
        self.assertEqual(mapped.status_code, 500)
        self.assertNotIn("secret", mapped.body["error"])
        self.assertEqual(mapped.error_code, "API_UNHANDLED_EXCEPTION")


if __name__ == "__main__":
    unittest.main()
