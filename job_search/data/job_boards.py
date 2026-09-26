"""Best-effort public job-board adapters and posting scraper.

LinkedIn and Indeed do not offer open candidate search APIs, so these adapters
parse public HTML and may break when markup changes.
"""

import json
import logging
from urllib.parse import quote_plus, urljoin, urlparse

from bs4 import BeautifulSoup

from job_search.domain.errors import ValidationError
from job_search.domain.text import clean_text, clean_url, dedupe_results, source_id
from job_search.observability import record_exception

_TITLE_SELECTORS = [
    "h1",
    ".top-card-layout__title",
    ".jobsearch-JobInfoHeader-title",
    "[data-testid='jobsearch-JobInfoHeader-title']",
]
_COMPANY_SELECTORS = [
    ".topcard__org-name-link",
    ".topcard__flavor",
    "[data-testid='inlineHeader-companyName']",
    "[data-company-name]",
    ".jobsearch-InlineCompanyRating-companyHeader a",
]
_LOCATION_SELECTORS = [
    ".topcard__flavor--bullet",
    ".job-search-card__location",
    "[data-testid='job-location']",
    ".jobsearch-JobInfoHeader-subtitle div",
]
_DESCRIPTION_SELECTORS = [
    "#job-details",
    ".show-more-less-html__markup",
    "#jobDescriptionText",
    "[data-testid='jobDescriptionText']",
]


def posting_service_from_url(url):
    lower = (url or "").lower()
    if "linkedin." in lower:
        return "linkedin"
    if "indeed." in lower:
        return "indeed"
    return "manual_posting"


def _source_board(service):
    return service if service in ("linkedin", "indeed") else "manual"


def selector_text(soup, selectors):
    for selector in selectors:
        element = soup.select_one(selector)
        if element:
            value = clean_text(element.get("title") or element.get_text(" "))
            if value:
                return value
    return ""


def meta_content(soup, properties):
    for prop in properties:
        element = soup.find("meta", attrs={"property": prop}) or soup.find("meta", attrs={"name": prop})
        if element and element.get("content"):
            return clean_text(element["content"])
    return ""


def nested_value(value, *keys):
    current = value
    for key in keys:
        if not isinstance(current, dict):
            return ""
        current = current.get(key)
    return current if isinstance(current, str) else ""


def extract_job_json_ld(soup):
    for script in soup.find_all("script", type="application/ld+json"):
        text = script.string or script.get_text()
        if not text:
            continue
        try:
            payload = json.loads(text)
        except json.JSONDecodeError as exc:
            record_exception(
                "scrape_json_ld_invalid",
                "data.job_boards",
                "extract_job_json_ld",
                exc,
                level=logging.INFO,
                recovery="Skipping malformed JSON-LD block; HTML selectors provide fallbacks.",
            )
            continue
        candidates = payload if isinstance(payload, list) else [payload]
        for candidate in candidates:
            if not isinstance(candidate, dict):
                continue
            graph = candidate.get("@graph")
            if isinstance(graph, list):
                candidates.extend(graph)
            type_value = candidate.get("@type")
            types = type_value if isinstance(type_value, list) else [type_value]
            if any("JobPosting" in str(item) for item in types):
                return candidate
    return {}


def location_from_json_ld(payload):
    location = payload.get("jobLocation") if isinstance(payload, dict) else None
    if isinstance(location, list):
        location = location[0] if location else None
    if not isinstance(location, dict):
        return ""
    address = location.get("address")
    if isinstance(address, dict):
        parts = [address.get("addressLocality"), address.get("addressRegion"), address.get("addressCountry")]
        return clean_text(", ".join(str(part) for part in parts if part))
    return nested_value(location, "name")


class LinkedInBoard:
    name = "linkedin"

    def search_url(self, keywords, location):
        return (
            "https://www.linkedin.com/jobs-guest/jobs/api/seeMoreJobPostings/search"
            f"?keywords={quote_plus(keywords)}&location={quote_plus(location or 'United States')}&f_TPR=r86400&start=0"
        )

    def parse(self, html, location):
        soup = BeautifulSoup(html, "html.parser")
        jobs = []
        for card in soup.select("li"):
            link = card.select_one("a.base-card__full-link, a")
            title = card.select_one(".base-search-card__title, h3")
            company = card.select_one(".base-search-card__subtitle, h4")
            location_el = card.select_one(".job-search-card__location")
            if not link or not title:
                continue
            href = clean_url(link.get("href", ""))
            jobs.append(
                {
                    "board": self.name,
                    "source_job_id": source_id(self.name, href),
                    "company": clean_text(company.get_text(" ")) if company else "",
                    "title": clean_text(title.get_text(" ")),
                    "location": clean_text(location_el.get_text(" ")) if location_el else location or "",
                    "url": href,
                    "snippet": clean_text(card.get_text(" "))[:1200],
                }
            )
        return jobs


class IndeedBoard:
    name = "indeed"

    def search_url(self, keywords, location):
        return (
            f"https://www.indeed.com/jobs?q={quote_plus(keywords)}"
            f"&l={quote_plus(location or 'United States')}&fromage=1&sort=date"
        )

    def parse(self, html, location):
        soup = BeautifulSoup(html, "html.parser")
        jobs = []
        for card in soup.select("[data-jk], .job_seen_beacon"):
            link = card.select_one("a[href*='/viewjob'], a.jcs-JobTitle")
            title = card.select_one("h2 span[title], h2 span, .jobTitle span")
            company = card.select_one("[data-testid='company-name'], .companyName")
            location_el = card.select_one("[data-testid='text-location'], .companyLocation")
            href = link.get("href", "") if link else ""
            if href.startswith("/"):
                href = urljoin("https://www.indeed.com", href)
            if not href or not title:
                continue
            jobs.append(
                {
                    "board": self.name,
                    "source_job_id": card.get("data-jk") or source_id(self.name, href),
                    "company": clean_text(company.get_text(" ")) if company else "",
                    "title": clean_text(title.get("title") or title.get_text(" ")),
                    "location": clean_text(location_el.get_text(" ")) if location_el else location or "",
                    "url": clean_url(href),
                    "snippet": clean_text(card.get_text(" "))[:1200],
                }
            )
        return jobs


class JobBoardClient:
    """Searches boards and scrapes individual postings via an ``HttpClient``."""

    def __init__(self, http, boards=None):
        self._http = http
        self._boards = {board.name: board for board in (boards or (LinkedInBoard(), IndeedBoard()))}

    def search(self, board_name, keywords, location, force_refresh=False):
        board = self._boards.get((board_name or "").lower())
        if board is None:
            raise ValidationError(f"Unsupported board: {board_name}", "search_board_unsupported")
        response = self._http.fetch(board.name, board.search_url(keywords, location), force_refresh=force_refresh)
        response.raise_for_status()
        return dedupe_results(board.parse(response.text, location))

    def scrape_posting(self, url, force_refresh=False):
        cleaned_url = clean_url(url)
        if not cleaned_url:
            raise ValidationError("URL is required.", "scrape_url_missing")
        service = posting_service_from_url(cleaned_url)
        response = self._http.fetch(service, cleaned_url, force_refresh=force_refresh)
        response.raise_for_status()
        soup = BeautifulSoup(response.text, "html.parser")
        json_ld = extract_job_json_ld(soup)
        page_title = clean_text(soup.title.get_text(" ")) if soup.title else ""
        title = (
            nested_value(json_ld, "title")
            or selector_text(soup, _TITLE_SELECTORS)
            or meta_content(soup, ["og:title", "twitter:title"])
            or page_title
        )
        company = (
            nested_value(json_ld, "hiringOrganization", "name")
            or selector_text(soup, _COMPANY_SELECTORS)
            or meta_content(soup, ["og:site_name"])
        )
        location = location_from_json_ld(json_ld) or selector_text(soup, _LOCATION_SELECTORS)
        description = (
            nested_value(json_ld, "description")
            or selector_text(soup, _DESCRIPTION_SELECTORS)
            or clean_text(soup.get_text(" "))[:5000]
        )
        posting_text = clean_text(BeautifulSoup(description or "", "html.parser").get_text(" "))
        return {
            "company": clean_text(company) or "Unknown company",
            "title": clean_text(title) or "Unknown title",
            "location": clean_text(location),
            "url": cleaned_url,
            "posting_text": posting_text[:12000],
            "source_board": _source_board(service),
            "source_job_id": source_id(service, cleaned_url),
        }

    @staticmethod
    def fallback_posting(url):
        host = urlparse(url).netloc.replace("www.", "")
        service = posting_service_from_url(url)
        return {
            "company": host or "Unknown company",
            "title": f"Job posting from {host}" if host else "Unknown title",
            "location": "",
            "url": clean_url(url),
            "posting_text": "",
            "source_board": _source_board(service),
            "source_job_id": source_id(service, url),
        }
