"""HTML and JSON-LD parsing for external job-posting pages."""

from __future__ import annotations

import json
from collections.abc import Callable, Mapping, Sequence
from typing import Any

from bs4 import BeautifulSoup


class JobPostingParser:
    def __init__(
        self, clean_text: Callable[[Any], str], clean_url: Callable[[str], str], source_id: Callable[[str, str], str]
    ) -> None:
        self._clean_text = clean_text
        self._clean_url = clean_url
        self._source_id = source_id

    def parse(self, url: str, html: str, service: str) -> Mapping[str, str]:
        cleaned_url = self._clean_url(url)
        if not cleaned_url:
            raise ValueError("URL is required.")
        soup = BeautifulSoup(html, "html.parser")
        json_ld = self._json_ld(soup)
        title = (
            self._value(json_ld, "title")
            or self._selector(
                soup,
                [
                    "h1",
                    ".top-card-layout__title",
                    ".jobsearch-JobInfoHeader-title",
                    "[data-testid='jobsearch-JobInfoHeader-title']",
                ],
            )
            or self._meta(soup, ["og:title", "twitter:title"])
            or (self._clean_text(soup.title.get_text(" ")) if soup.title else "")
        )
        company = (
            self._value(json_ld, "hiringOrganization", "name")
            or self._selector(
                soup,
                [
                    ".topcard__org-name-link",
                    ".topcard__flavor",
                    "[data-testid='inlineHeader-companyName']",
                    "[data-company-name]",
                    ".jobsearch-InlineCompanyRating-companyHeader a",
                ],
            )
            or self._meta(soup, ["og:site_name"])
        )
        location = self._location(json_ld) or self._selector(
            soup,
            [
                ".topcard__flavor--bullet",
                ".job-search-card__location",
                "[data-testid='job-location']",
                ".jobsearch-JobInfoHeader-subtitle div",
            ],
        )
        description = (
            self._value(json_ld, "description")
            or self._selector(
                soup,
                [
                    "#job-details",
                    ".show-more-less-html__markup",
                    "#jobDescriptionText",
                    "[data-testid='jobDescriptionText']",
                ],
            )
            or self._clean_text(soup.get_text(" "))[:5000]
        )
        return {
            "company": self._clean_text(company) or "Unknown company",
            "title": self._clean_text(title) or "Unknown title",
            "location": self._clean_text(location),
            "url": cleaned_url,
            "posting_text": self._clean_text(BeautifulSoup(description or "", "html.parser").get_text(" "))[:12000],
            "source_board": service if service in {"linkedin", "indeed"} else "manual",
            "source_job_id": self._source_id(service, cleaned_url),
        }

    def _selector(self, soup: BeautifulSoup, selectors: Sequence[str]) -> str:
        for selector in selectors:
            element = soup.select_one(selector)
            if element:
                value = self._clean_text(element.get("title") or element.get_text(" "))
                if value:
                    return value
        return ""

    def _meta(self, soup: BeautifulSoup, properties: Sequence[str]) -> str:
        for prop in properties:
            element = soup.find("meta", attrs={"property": prop}) or soup.find("meta", attrs={"name": prop})
            if element and element.get("content"):
                return self._clean_text(element["content"])
        return ""

    def _json_ld(self, soup: BeautifulSoup) -> Mapping[str, Any]:
        for script in soup.find_all("script", type="application/ld+json"):
            try:
                payload = json.loads(script.string or script.get_text() or "")
            except json.JSONDecodeError:
                continue
            candidates = payload if isinstance(payload, list) else [payload]
            for candidate in candidates:
                if isinstance(candidate, dict) and "JobPosting" in str(candidate.get("@type", "")):
                    return candidate
        return {}

    @staticmethod
    def _value(value: Mapping[str, Any], *keys: str) -> str:
        current: Any = value
        for key in keys:
            if not isinstance(current, dict):
                return ""
            current = current.get(key)
        return current if isinstance(current, str) else ""

    def _location(self, payload: Mapping[str, Any]) -> str:
        location: Any = payload.get("jobLocation")
        if isinstance(location, list):
            location = location[0] if location else None
        if not isinstance(location, dict):
            return ""
        address = location.get("address")
        if isinstance(address, dict):
            return self._clean_text(
                ", ".join(
                    str(item)
                    for item in (
                        address.get("addressLocality"),
                        address.get("addressRegion"),
                        address.get("addressCountry"),
                    )
                    if item
                )
            )
        return self._value(location, "name")
