"""Pre-tracking discovery filters, evaluated as a chain of responsibility.

Each filter returns ``(allowed, reason)``. The first filter that rejects a
discovered result stops the chain.
"""

import re

from job_search.domain.text import clean_text

_MONEY = r"\$?\s*([0-9]{2,3}(?:,[0-9]{3})?(?:\.\d+)?)\s*([kK]?)"
_PERIOD = r"(year|yr|annually|annual|hour|hr|month|mo)"
_RANGE_PATTERN = re.compile(rf"{_MONEY}\s*(?:-|–|—|to)\s*{_MONEY}\s*(?:per\s+|/)?{_PERIOD}?", re.IGNORECASE)
_SINGLE_PATTERN = re.compile(rf"{_MONEY}\s*(?:per\s+|/){_PERIOD}", re.IGNORECASE)


def sales_role_filter_decision(result, profile):
    title = clean_text(result.get("title", "")).lower()
    if any(term in title for term in profile.sales_exclusion.title_terms):
        return False, f"Sales role excluded by title: {result.get('title') or 'unknown'}"
    return True, "Not a sales-role title"


def location_filter_decision(result, profile):
    policy = profile.location
    metro = policy.home_metro_label
    location = clean_text(result.get("location", ""))
    combined = f" {location} {(result.get('snippet') or '')[:500]} ".lower()
    if any(term in combined for term in policy.home_metro_terms):
        return True, f"{metro}-based or {metro}-area role"
    remote = "remote" in combined
    non_us = any(term in combined for term in policy.non_us_terms)
    us_based = any(term in combined for term in policy.us_terms) or location.lower() == "remote"
    if remote and us_based and not non_us:
        return True, "US-based remote role"
    if remote and not non_us and not location:
        return True, "Remote role with no non-US location signal"
    return False, f"Location is not US-based remote or {metro}-based: {location or 'unknown'}"


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


def compensation_filter_decision(result, profile):
    minimum = profile.min_annual_compensation
    text = clean_text(" ".join(str(result.get(field) or "") for field in ("title", "location", "snippet")))
    values = extract_annual_compensation_values(text)
    if not values:
        return True, "No explicit compensation below threshold found"
    high = max(values)
    if high < minimum:
        return (
            False,
            f"Explicit compensation below ${minimum:,}/year; highest parsed annualized value is ${int(high):,}",
        )
    return True, f"Explicit compensation meets threshold; highest parsed annualized value is ${int(high):,}"


DISCOVERY_FILTER_CHAIN = (
    ("sales_role", sales_role_filter_decision),
    ("location", location_filter_decision),
    ("compensation", compensation_filter_decision),
)


def first_rejection(result, profile, chain=DISCOVERY_FILTER_CHAIN):
    """Return ``(filter_name, reason)`` for the first rejecting filter, else ``None``."""
    for name, decide in chain:
        allowed, reason = decide(result, profile)
        if not allowed:
            return name, reason
    return None
