import unittest

from job_search.config import StartupConfigurationError, load_runtime_settings


class RuntimeSettingsTests(unittest.TestCase):
    def test_defaults_are_safe_and_valid(self):
        settings = load_runtime_settings({})
        self.assertEqual(settings.port, 5050)
        self.assertEqual(settings.codex_timeout_seconds, 270)

    def test_rejects_malformed_and_out_of_range_values(self):
        for environment in (
            {"JOB_SEARCH_PORT": "not-a-port"},
            {"CODEX_CLI_TIMEOUT_SECONDS": "0"},
            {"JOB_SEARCH_LOG_BACKUP_COUNT": "-1"},
        ):
            with self.assertRaisesRegex(StartupConfigurationError, "STARTUP_INVALID_CONFIGURATION"):
                load_runtime_settings(environment)


if __name__ == "__main__":
    unittest.main()
