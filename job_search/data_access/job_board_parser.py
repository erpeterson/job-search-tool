"""HTML parsing adapter for external job-board response bodies."""

from __future__ import annotations

from collections.abc import Callable, Mapping, Sequence

from bs4 import BeautifulSoup


class JobBoardParser:
    def __init__(
        self, clean_text: Callable[[str], str], clean_url: Callable[[str], str], source_id: Callable[[str], str]
    ) -> None:
        self._clean_text = clean_text
        self._clean_url = clean_url
        self._source_id = source_id

    def linkedin(self, html: str, location: str) -> Sequence[Mapping[str, str]]:
        jobs = []
        for card in BeautifulSoup(html, "html.parser").select("li"):
            link = card.select_one("a.base-card__full-link, a")
            title = card.select_one(".base-search-card__title, h3")
            company = card.select_one(".base-search-card__subtitle, h4")
            location_element = card.select_one(".job-search-card__location")
            if not link or not title:
                continue
            url = self._clean_url(link.get("href", ""))
            jobs.append(
                {
                    "board": "linkedin",
                    "source_job_id": self._source_id(url),
                    "company": self._clean_text(company.get_text(" ")) if company else "",
                    "title": self._clean_text(title.get_text(" ")),
                    "location": self._clean_text(location_element.get_text(" ")) if location_element else location,
                    "url": url,
                    "snippet": self._clean_text(card.get_text(" "))[:1200],
                }
            )
        return jobs
