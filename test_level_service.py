import unittest

from job_search.application.level_service import LevelService


class FakeLevels:
    def __init__(self):
        self.entries = {}

    def find(self, company, title):
        for entry in self.entries.values():
            pattern = entry["normalized_title_pattern"]
            if entry["normalized_company"] == company and (title == pattern or title.startswith(f"{pattern} ")):
                return entry
        return None

    def save(self, values):
        self.entries[(values["normalized_company"], values["normalized_title_pattern"])] = dict(values)


class LevelServiceTests(unittest.TestCase):
    def test_estimates_and_reuses_calibration_with_a_plain_repository_fake(self):
        repository = FakeLevels()
        service = LevelService(repository, now=lambda: 123)

        first = service.lookup("Example Co", "Senior Principal Engineer")
        second = service.lookup("Example Co", "Senior Principal Engineer")

        self.assertEqual(first["oracle_level"], "IC6+", "Principal-plus title must map to the IC6+ target.")
        self.assertEqual(second, first, "The cached calibration must be reused without another persistence decision.")
        self.assertEqual(len(repository.entries), 1, "One normalized calibration should be stored.")

    def test_leaves_ambiguous_titles_uncalibrated(self):
        repository = FakeLevels()

        result = LevelService(repository, now=lambda: 123).lookup("Example Co", "Developer")

        self.assertIsNone(result, "An ambiguous title must not be guessed or persisted as a level calibration.")
        self.assertFalse(repository.entries, "Ambiguous titles must not create a cached calibration.")
