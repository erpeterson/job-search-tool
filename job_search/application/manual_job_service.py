"""Manual job-ingestion workflow with injectable scraping and scoring ports."""

from __future__ import annotations

from collections.abc import Callable, Mapping
from dataclasses import dataclass
from typing import Any

from job_search.application.contracts import JobRepository


@dataclass(frozen=True)
class ManualJobResult:
    job_id: int | None
    existing_job: Mapping[str, Any] | None
    scrape_error: str | None
    score_error: str | None


class ManualJobService:
    def __init__(
        self,
        repository: JobRepository,
        scraper: Callable[[str, bool], Mapping[str, Any]],
        fallback: Callable[[str], Mapping[str, Any]],
        refresh_filter: Callable[[int], object],
        score: Callable[[int], object],
        scoring_availability: Callable[[], str | None],
        report_failure: Callable[[str, Exception, Mapping[str, Any]], None],
    ) -> None:
        self._repository = repository
        self._scraper = scraper
        self._fallback = fallback
        self._refresh_filter = refresh_filter
        self._score = score
        self._scoring_availability = scoring_availability
        self._report_failure = report_failure

    def create(self, values: Mapping[str, Any], *, force_refresh: bool) -> ManualJobResult:
        url = str(values["url"])
        scrape_error = None
        try:
            scraped = self._scraper(url, force_refresh)
        except Exception as exc:
            scrape_error = str(exc)
            scraped = self._fallback(url)
            self._report_failure("scrape", exc, {"url": url, "pipeline": values["pipeline"]})

        record = self._merge(values, scraped, scrape_error)
        job_id = self._repository.create_job(record)
        if job_id is None:
            return ManualJobResult(
                job_id=None,
                existing_job=self._repository.get_job_by_url(str(record["url"])),
                scrape_error=scrape_error,
                score_error=None,
            )

        self._refresh_filter(job_id)
        unavailable_reason = self._scoring_availability()
        if unavailable_reason:
            return ManualJobResult(job_id, None, scrape_error, f"Automatic Codex scoring skipped: {unavailable_reason}")
        try:
            self._score(job_id)
        except Exception as exc:
            self._report_failure("score", exc, {"job_id": job_id})
            return ManualJobResult(job_id, None, scrape_error, str(exc))
        return ManualJobResult(job_id, None, scrape_error, None)

    @staticmethod
    def _merge(values: Mapping[str, Any], scraped: Mapping[str, Any], scrape_error: str | None) -> dict[str, Any]:
        result = dict(values)
        result["company"] = result.get("company") or scraped.get("company") or "Unknown company"
        result["title"] = result.get("title") or scraped.get("title") or "Unknown title"
        result["url"] = scraped.get("url") or result["url"]
        result["location"] = result.get("location") or scraped.get("location", "")
        result["posting_text"] = result.get("posting_text") or scraped.get("posting_text", "")
        result["notes"] = result.get("notes") or (
            "Added manually from URL." + (f" Scrape failed: {scrape_error[:500]}" if scrape_error else "")
        )
        result["source_board"] = scraped.get("source_board")
        result["source_job_id"] = scraped.get("source_job_id")
        result["discovered_at"] = result["created_at"] if scraped else None
        return result
