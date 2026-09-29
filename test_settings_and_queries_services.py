import unittest

from job_search.application.search_query_service import SearchQueryService
from job_search.application.settings_service import SettingsService


class FakeQueries:
    def __init__(self):
        self.created = []
        self.updated = []

    def create(self, values):
        self.created.append(dict(values))
        return 42

    def update(self, query_id, values):
        self.updated.append((query_id, dict(values)))
        return True


class FakeSettings:
    def __init__(self):
        self.saved = []

    def save(self, values):
        self.saved.append(dict(values))


class SettingsAndQueriesServiceTests(unittest.TestCase):
    def test_search_query_service_uses_a_plain_repository_fake(self):
        repository = FakeQueries()
        service = SearchQueryService(repository)

        created_id = service.create({"board": "indeed", "keywords": "architect"})
        updated = service.update(42, {"enabled": 0})

        self.assertEqual(created_id, 42)
        self.assertTrue(updated)
        self.assertEqual(repository.created, [{"board": "indeed", "keywords": "architect"}])
        self.assertEqual(repository.updated, [(42, {"enabled": 0})])

    def test_settings_service_uses_a_plain_repository_fake(self):
        repository = FakeSettings()
        refreshed = []
        service = SettingsService(repository, lambda: refreshed.append(True))

        service.save({"gpt_threshold": "80"})
        service.save_and_refresh({"user_threshold": "70"})

        self.assertEqual(repository.saved, [{"gpt_threshold": "80"}, {"user_threshold": "70"}])
        self.assertEqual(refreshed, [True], "Filtering should refresh only for the settings workflow.")


if __name__ == "__main__":
    unittest.main()
