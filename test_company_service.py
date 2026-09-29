"""Company-interest normalization and timestamps belong to the application service."""

import unittest

from job_search.application.company_service import CompanyService


class FakeCompanyRepository:
    def __init__(self):
        self.created = None
        self.updated = None

    def upsert(self, values):
        self.created = dict(values)
        return 7

    def update(self, company_id, values):
        self.updated = (company_id, dict(values))
        return True


class CompanyServiceTests(unittest.TestCase):
    def test_create_normalizes_company_and_owns_timestamps(self):
        repository = FakeCompanyRepository()
        service = CompanyService(repository, now=lambda: 123)

        company_id = service.save({"company": " Example-Co ", "status": "watching"})

        self.assertEqual(company_id, 7)
        self.assertEqual(repository.created["normalized_company"], "example co")
        self.assertEqual(repository.created["created_at"], 123)
        self.assertEqual(repository.created["updated_at"], 123)

    def test_update_refreshes_normalization_and_timestamp(self):
        repository = FakeCompanyRepository()
        service = CompanyService(repository, now=lambda: 456)

        updated = service.update(7, {"company": "New Name", "status": "target"})

        self.assertTrue(updated, "The repository's update result should be preserved.")
        self.assertEqual(repository.updated[0], 7)
        self.assertEqual(repository.updated[1]["normalized_company"], "new name")
        self.assertEqual(repository.updated[1]["updated_at"], 456)


if __name__ == "__main__":
    unittest.main()
