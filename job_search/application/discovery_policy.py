"""Deterministic policy for accepting job-board discoveries."""

from __future__ import annotations

import re
from collections.abc import Mapping
from typing import Any

SEATTLE_LOCATION_TERMS = (
    "seattle",
    "bellevue",
    "redmond",
    "kirkland",
    "renton",
    "mercer island",
    "tukwila",
    "puget sound",
    "greater seattle",
    "seattle metropolitan",
)
US_LOCATION_TERMS = ("united states", "usa", "u.s.", " us ", "us-based", "anywhere in the us", "anywhere in us")
NON_US_LOCATION_TERMS = (
    "canada",
    "united kingdom",
    "uk",
    "europe",
    "emea",
    "india",
    "australia",
    "germany",
    "france",
    "netherlands",
    "singapore",
    "mexico",
)
SALES_ROLE_TITLE_TERMS = (
    "account executive",
    "sales executive",
    "sales director",
    "sales manager",
    "sales representative",
    "account manager",
    "account director",
    "business development",
)


class DiscoveryPolicy:
    def __init__(self, minimum_annual_compensation: int) -> None:
        self._minimum_annual_compensation = minimum_annual_compensation

    def rejection_reason(self, result: Mapping[str, Any]) -> tuple[str, str] | None:
        for name, decision in (
            ("sales_role", self.sales_role),
            ("location", self.location),
            ("compensation", self.compensation),
        ):
            allowed, reason = decision(result)
            if not allowed:
                return name, reason
        return None

    def location(self, result: Mapping[str, Any]) -> tuple[bool, str]:
        location = _clean(result.get("location", ""))
        combined = f" {location} {result.get('snippet', '')[:500]} ".lower()
        if any(term in combined for term in SEATTLE_LOCATION_TERMS):
            return True, "Seattle-based or Seattle-area role"
        remote = "remote" in combined
        non_us = any(term in combined for term in NON_US_LOCATION_TERMS)
        us_based = any(term in combined for term in US_LOCATION_TERMS) or "remote" == location.lower()
        if remote and us_based and not non_us:
            return True, "US-based remote role"
        if remote and not non_us and not location:
            return True, "Remote role with no non-US location signal"
        return False, f"Location is not US-based remote or Seattle-based: {location or 'unknown'}"

    def compensation(self, result: Mapping[str, Any]) -> tuple[bool, str]:
        text = _clean(" ".join(str(result.get(field) or "") for field in ("title", "location", "snippet")))
        values = extract_annual_compensation_values(text)
        if not values:
            return True, "No explicit compensation below threshold found"
        highest = max(values)
        if highest < self._minimum_annual_compensation:
            return (
                False,
                f"Explicit compensation below ${self._minimum_annual_compensation:,}/year; highest parsed annualized value is ${int(highest):,}",
            )
        return True, f"Explicit compensation meets threshold; highest parsed annualized value is ${int(highest):,}"

    @staticmethod
    def sales_role(result: Mapping[str, Any]) -> tuple[bool, str]:
        title = _clean(result.get("title", "")).lower()
        if any(term in title for term in SALES_ROLE_TITLE_TERMS):
            return False, f"Sales role excluded by title: {result.get('title') or 'unknown'}"
        return True, "Not a sales-role title"


def extract_annual_compensation_values(text: str) -> list[float]:
    values = []
    money = r"\$?\s*([0-9]{2,3}(?:,[0-9]{3})?(?:\.\d+)?)\s*([kK]?)"
    ranges = re.compile(
        rf"{money}\s*(?:-|–|—|to)\s*{money}\s*(?:per\s+|/)?(year|yr|annually|annual|hour|hr|month|mo)?", re.IGNORECASE
    )
    singles = re.compile(rf"{money}\s*(?:per\s+|/)(year|yr|annually|annual|hour|hr|month|mo)", re.IGNORECASE)
    for match in ranges.finditer(text):
        period = match.group(5) or "year"
        values.extend(
            (
                _annualize(_money(match.group(1), match.group(2)), period),
                _annualize(_money(match.group(3), match.group(4)), period),
            )
        )
    for match in singles.finditer(text):
        values.append(_annualize(_money(match.group(1), match.group(2)), match.group(3)))
    return values


def _clean(value: object) -> str:
    return " ".join(str(value or "").split())


def _money(raw_value: str, suffix: str) -> float:
    value = float(raw_value.replace(",", ""))
    return value * 1000 if suffix.lower() == "k" else value


def _annualize(value: float, period: str) -> float:
    if (period or "year").lower() in ("hour", "hr"):
        return value * 2080
    if (period or "year").lower() in ("month", "mo"):
        return value * 12
    return value
