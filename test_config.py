import tempfile
import unittest
from pathlib import Path

from job_search.composition import runtime_configuration
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

    def test_dotenv_defaults_and_injected_values_have_predictable_precedence(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            (root / ".env").write_text("CODEX_MODEL=file-model\nJOB_SEARCH_PORT=6000\n", encoding="utf-8")
            config = runtime_configuration(root, {"CODEX_MODEL": "injected-model"}, which=lambda _: None)

            self.assertEqual(config.model(), "injected-model", "Injected values must override dotenv defaults")
            self.assertEqual(config.settings.port, 6000, "Dotenv should supply omitted settings")
            self.assertEqual(config.paths.database, root.resolve() / "job_search.sqlite3")
            self.assertEqual(config.cli_path(), "codex", "Unavailable discovery must use the command fallback")
            self.assertFalse(config.cli_available(), "A missing CLI must be reported unavailable")

    def test_cli_availability_uses_injected_lookup_and_executable_check(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            executable = root / "codex"
            executable.touch()
            config = runtime_configuration(
                root,
                {"CODEX_CLI_PATH": str(executable)},
                which=lambda _: None,
                executable=lambda path: path == str(executable),
            )
            self.assertTrue(config.cli_available(), "Injected executable check should accept a present CLI")
            config.environment["CODEX_CLI_PATH"] = str(root / "missing")
            self.assertFalse(config.cli_available(), "A missing absolute CLI path must be rejected")

    def test_invalid_dotenv_setting_is_rejected_without_environment_mutation(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            (root / ".env").write_text("JOB_SEARCH_PORT=invalid\n", encoding="utf-8")
            with self.assertRaisesRegex(StartupConfigurationError, "JOB_SEARCH_PORT"):
                runtime_configuration(root, {})

    def test_failed_persistence_does_not_change_runtime_snapshot(self):
        with tempfile.TemporaryDirectory() as directory:
            config = runtime_configuration(Path(directory), {"CODEX_MODEL": "original"})

            def fail_to_persist(_path, _updates):
                raise OSError("simulated disk failure")

            config.persist = fail_to_persist
            with self.assertRaisesRegex(OSError, "simulated disk failure"):
                config.update({"CODEX_MODEL": "replacement"})

            self.assertEqual(config.model(), "original", "Failed persistence must not alter active settings")


if __name__ == "__main__":
    unittest.main()
