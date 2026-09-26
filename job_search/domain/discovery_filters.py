"""Pre-tracking discovery filters, evaluated as a chain of responsibility.

Each filter returns ``(allowed, reason)``. The first filter that rejects a
discovered result stops the chain.
"""

import re

from job_search.domain.rules import (
    MIN_ANNUAL_COMPENSATION,
    NON_US_LOCATION_TERMS,
    SALES_ROLE_TITLE_TERMS,
    SEATTLE_LOCATION_TERMS,
    US_LOCATION_TERMS,
)
from job_search.domain.text import clean_text

_MONEY = r"\$?\s*([0-9]{2,3}(?:,[0-9]{3})?(?:\.\d+)?)\s*([kK]?)"
_PERIOD = r"(year|yr|annually|annual|hour|hr|month|mo)"
_RANGE_PATTERN = re.compile(rf"{_MONEY}\s*(?:-|–|—|to)\s*{_MONEY}\s*(?:per\s+|/)?{_PERIOD}?", re.IGNORECASE)
_SINGLE_PATTERN = re.compile(rf"{_MONEY}\s*(?:per\s+|/){_PERIOD}", re.IGNORECASE)


def sales_role_filter_decision(result):
    title = clean_text(result.get("title", "")).lower()
    if any(term in title for term in SALES_ROLE_TITLE_TERMS):
        return False, f"Sales role excluded by title: {result.get('title') or 'unknown'}"
    return True, "Not a sales-role title"


def location_filter_decision(result):
    location = clean_text(result.get("location", ""))
    combined = f" {location} {(result.get('snippet') or '')[:500]} ".lower()
    if any(term in combined for term in SEATTLE_LOCATION_TERMS):
        return True, "Seattle-based or Seattle-area role"
    remote = "remote" in combined
    non_us = any(term in combined for term in NON_US_LOCATION_TERMS)
    us_based = any(term in combined for term in US_LOCATION_TERMS) or location.lower() == "remote"
    if remote and us_based and not non_us:
        return True, "US-based remote role"
    if remote and not non_us and not location:
        return True, "Remote role with no non-US location signal"
    return False, f"Location is not US-based remote or Seattle-based: {location or 'unknown'}"


def normalize_money_value(raw_value, suffix=""):
    value = float(raw_value.replace(",", ""))
    if suffix and suffix.lower() == "k":
        value *= 1000
    return value


def annualize_compensation(value, period):
    period = (period or "year").lower()
    if period in ("hour", "hr"):
        return value * 2080
    if period in ("month", "mo"):
        return value * 12
    return value


def extract_annual_compensation_values(text):
    values = []
    for match in _RANGE_PATTERN.finditer(text):
        period = match.group(5) or "year"
        values.append(annualize_compensation(normalize_money_value(match.group(1), match.group(2)), period))
        values.append(annualize_compensation(normalize_money_value(match.group(3), match.group(4)), period))
    for match in _SINGLE_PATTERN.finditer(text):
        values.append(annualize_compensation(normalize_money_value(match.group(1), match.group(2)), match.group(3)))
    return values


def compensation_filter_decision(result):
    text = clean_text(" ".join(str(result.get(field) or "") for field in ("title", "location", "snippet")))
    values = extract_annual_compensation_values(text)
    if not values:
        return True, "No explicit compensation below threshold found"
    high = max(values)
    if high < MIN_ANNUAL_COMPENSATION:
        return (
            False,
            f"Explicit compensation below ${MIN_ANNUAL_COMPENSATION:,}/year; "
            f"highest parsed annualized value is ${int(high):,}",
        )
    return True, f"Explicit compensation meets threshold; highest parsed annualized value is ${int(high):,}"


DISCOVERY_FILTER_CHAIN = (
    ("sales_role", sales_role_filter_decision),
    ("location", location_filter_decision),
    ("compensation", compensation_filter_decision),
)


def first_rejection(result, chain=DISCOVERY_FILTER_CHAIN):
    """Return ``(filter_name, reason)`` for the first rejecting filter, else ``None``."""
    for name, decide in chain:
        allowed, reason = decide(result)
        if not allowed:
            return name, reason
    return None
