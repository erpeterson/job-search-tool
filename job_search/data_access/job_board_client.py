"""External job-board and posting client adapters."""

from __future__ import annotations

from collections.abc import Callable, Mapping, Sequence
from typing import Any
from urllib.parse import quote_plus, urljoin, urlparse


class JobBoardClient:
    """Fetch and parse job boards through injected HTTP and parser adapters."""

    def __init__(
        self,
        fetch: Callable[..., Any],
        board_parser: Any,
        posting_parser: Any,
        html_parser: Callable[..., Any],
        clean_text: Callable[[str], str],
        clean_url: Callable[[str], str],
        source_id: Callable[[str, str], str],
        deduplicate: Callable[[Sequence[Mapping[str, str]]], Sequence[Mapping[str, str]]],
    ) -> None:
        self._fetch, self._board_parser, self._posting_parser, self._html_parser = (
            fetch,
            board_parser,
            posting_parser,
            html_parser,
        )
        self._clean_text, self._clean_url, self._source_id, self._deduplicate = (
            clean_text,
            clean_url,
            source_id,
            deduplicate,
        )

    def linkedin(self, keywords: str, location: str, *, force_refresh: bool = False) -> Sequence[Mapping[str, str]]:
        url = (
            "https://www.linkedin.com/jobs-guest/jobs/api/seeMoreJobPostings/search"
            f"?keywords={quote_plus(keywords)}&location={quote_plus(location or 'United States')}&f_TPR=r86400&start=0"
        )
        response = self._fetch("linkedin", url, force_refresh=force_refresh)
        response.raise_for_status()
        return self._deduplicate(self._board_parser.linkedin(response.text, location or ""))

    def indeed(self, keywords: str, location: str, *, force_refresh: bool = False) -> Sequence[Mapping[str, str]]:
        url = f"https://www.indeed.com/jobs?q={quote_plus(keywords)}&l={quote_plus(location or 'United States')}&fromage=1&sort=date"
        response = self._fetch("indeed", url, force_refresh=force_refresh)
        response.raise_for_status()
        jobs: list[Mapping[str, str]] = []
        for card in self._html_parser(response.text, "html.parser").select("[data-jk], .job_seen_beacon"):
            link = card.select_one("a[href*='/viewjob'], a.jcs-JobTitle")
            title = card.select_one("h2 span[title], h2 span, .jobTitle span")
            company = card.select_one("[data-testid='company-name'], .companyName")
            location_element = card.select_one("[data-testid='text-location'], .companyLocation")
            href = link.get("href", "") if link else ""
            if href.startswith("/"):
                href = urljoin("https://www.indeed.com", href)
            if not href or not title:
                continue
            jobs.append(
                {
                    "board": "indeed",
                    "source_job_id": card.get("data-jk") or self._source_id("indeed", href),
                    "company": self._clean_text(company.get_text(" ")) if company else "",
                    "title": self._clean_text(title.get("title") or title.get_text(" ")),
                    "location": self._clean_text(location_element.get_text(" "))
                    if location_element
                    else location or "",
                    "url": self._clean_url(href),
                    "snippet": self._clean_text(card.get_text(" "))[:1200],
                }
            )
        return self._deduplicate(jobs)

    def scrape(self, url: str, *, force_refresh: bool = False) -> Mapping[str, str]:
        cleaned_url = self._clean_url(url)
        if not cleaned_url:
            raise ValueError("URL is required.")
        service = self.posting_service(cleaned_url)
        response = self._fetch(service, cleaned_url, force_refresh=force_refresh)
        response.raise_for_status()
        return self._posting_parser.parse(cleaned_url, response.text, service)

    def fallback(self, url: str) -> Mapping[str, str]:
        host = urlparse(url).netloc.replace("www.", "")
        service = self.posting_service(url)
        return {
            "company": host or "Unknown company",
            "title": f"Job posting from {host}" if host else "Unknown title",
            "location": "",
            "url": self._clean_url(url),
            "posting_text": "",
            "source_board": service if service in ("linkedin", "indeed") else "manual",
            "source_job_id": self._source_id(service, url),
        }

    @staticmethod
    def posting_service(url: str) -> str:
        lower = (url or "").lower()
        if "linkedin." in lower:
            return "linkedin"
        if "indeed." in lower:
            return "indeed"
        return "manual_posting"
