"""Re-scrape workflow independent of HTTP, Flask, and SQLite."""

from __future__ import annotations

from collections.abc import Callable, Mapping
from dataclasses import dataclass
from typing import Any

from job_search.application.contracts import JobRepository


@dataclass(frozen=True)
class RescrapeResult:
    job: Mapping[str, Any] | None
    scraped: Mapping[str, Any] | None


class RescrapeService:
    def __init__(
        self,
        repository: JobRepository,
        scraper: Callable[[str, bool], Mapping[str, Any]],
        refresh_filter: Callable[[int], object],
        clock: Callable[[], int],
        observe: Callable[..., None],
    ) -> None:
        self._repository = repository
        self._scraper = scraper
        self._refresh_filter = refresh_filter
        self._clock = clock
        self._observe = observe

    def rescrape(self, job_id: int, *, force_refresh: bool) -> RescrapeResult:
        job = self._repository.get_job(job_id)
        if job is None:
            return RescrapeResult(None, None)
        url = job.get("url")
        if not url:
            return RescrapeResult(job, None)
        scraped = self._scraper(str(url), force_refresh)
        notes = self._append_note(str(job.get("notes") or ""), "Re-scraped posting URL.")
        self._repository.rescrape_job(job_id, job, scraped, notes, self._clock())
        self._refresh_filter(job_id)
        self._observe(
            "manual_job_rescraped",
            job_id=job_id,
            url=job["url"],
            company=scraped.get("company"),
            title=scraped.get("title"),
            force_refresh=force_refresh,
        )
        return RescrapeResult(self._repository.get_job(job_id), scraped)

    @staticmethod
    def _append_note(existing: str, addition: str) -> str:
        return f"{existing.rstrip()}\n{addition}".strip()
