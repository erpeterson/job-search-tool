"""Company-level interest tracking, independent of individual roles."""

from job_search.domain.clock import now
from job_search.domain.errors import NotFoundError
from job_search.domain.text import normalize_lookup_text


class CompanyService:
    def __init__(self, db):
        self._db = db

    def list(self):
        with self._db.unit_of_work() as uow:
            return uow.companies.list_with_job_counts()

    def get(self, company_id):
        with self._db.unit_of_work() as uow:
            company = uow.companies.get(company_id)
        if not company:
            raise NotFoundError("Company interest not found", "company_not_found")
        return company

    def upsert(self, fields):
        """Create a company interest, or update the one with the same normalized name."""
        fields = {**fields, "normalized_company": normalize_lookup_text(fields["company"])}
        with self._db.unit_of_work() as uow:
            company_id = uow.companies.upsert(fields, now())
            return uow.companies.get(company_id), uow.companies.list_with_job_counts()

    def update(self, company_id, fields):
        """Apply a partial update; omitted fields keep their current values."""
        with self._db.unit_of_work() as uow:
            existing = uow.companies.get(company_id)
            if not existing:
                raise NotFoundError("Company interest not found", "company_update_not_found")
            merged = {
                key: fields[key] if key in fields else existing[key]
                for key in ("company", "status", "interest_score", "rationale", "notes", "next_step", "contacts")
            }
            merged["company"] = merged["company"] or existing["company"]
            merged["normalized_company"] = normalize_lookup_text(merged["company"])
            uow.companies.update(company_id, merged, now())
            return uow.companies.get(company_id), uow.companies.list_with_job_counts()
