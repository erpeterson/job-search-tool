"""Configuration update coordination without filesystem or Flask dependencies."""

import unittest

from job_search.application.configuration_service import ConfigurationService


class FakeConfiguration:
    def __init__(self):
        self.updates = []

    def update(self, values):
        self.updates.append(dict(values))


class FakeSettings:
    def __init__(self):
        self.saved = []

    def save(self, values):
        self.saved.append(dict(values))


class ConfigurationServiceTests(unittest.TestCase):
    def test_model_update_is_mirrored_to_persisted_settings(self):
        configuration = FakeConfiguration()
        settings = FakeSettings()
        service = ConfigurationService(configuration, settings)

        service.update({"CODEX_MODEL": "test-model", "JOB_SEARCH_USE_CAPTURE_CACHE": "0"})

        self.assertEqual(configuration.updates[0]["CODEX_MODEL"], "test-model")
        self.assertEqual(settings.saved, [{"codex_model": "test-model"}])

    def test_other_configuration_updates_do_not_change_model_setting(self):
        configuration = FakeConfiguration()
        settings = FakeSettings()

        ConfigurationService(configuration, settings).update({"JOB_SEARCH_USE_CAPTURE_CACHE": "1"})

        self.assertEqual(settings.saved, [], "Unrelated configuration updates must not rewrite the model setting.")


if __name__ == "__main__":
    unittest.main()
