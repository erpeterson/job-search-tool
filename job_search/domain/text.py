"""Pure text normalization helpers shared across layers."""

import hashlib
import re

from job_search.domain.rules import (
    PIPELINES,
    SALES_ROLE_EXCLUSION_CRITERIA,
    SALES_ROLE_EXCLUSION_QUERY,
)


def clean_text(value):
    return " ".join((value or "").split())


def clean_url(value):
    return (value or "").split("?trk=")[0].strip()


def normalize_lookup_text(value):
    return re.sub(r"[^a-z0-9]+", " ", (value or "").lower()).strip()


def append_note_text(existing, addition):
    existing = clean_text(existing)
    addition = clean_text(addition)
    if not existing:
        return addition
    if not addition:
        return existing
    return f"{existing} {addition}"


def source_id(board, url):
    digest = hashlib.sha256((url or "").encode("utf-8")).hexdigest()[:16]
    return f"{board}:{digest}"


def dedupe_results(results):
    seen = set()
    deduped = []
    for result in results:
        key = result.get("url") or result.get("source_job_id")
        if not key or key in seen:
            continue
        seen.add(key)
        deduped.append(result)
    return deduped


def normalize_pipeline(value, fallback=""):
    """Return a valid pipeline string from model or request data."""
    if isinstance(value, str):
        candidate = value.strip()
        return candidate if candidate in PIPELINES else fallback
    if isinstance(value, (list, tuple, set)):
        for candidate in value:
            if isinstance(candidate, str) and candidate.strip() in PIPELINES:
                return candidate.strip()
    return fallback


def with_sales_role_exclusion_keywords(keywords):
    keywords = clean_text(keywords or "")
    if "Account Executive" in keywords or SALES_ROLE_EXCLUSION_QUERY in keywords:
        return keywords
    return clean_text(f"{keywords} {SALES_ROLE_EXCLUSION_QUERY}")


def with_sales_role_exclusion_criteria(criteria):
    criteria = clean_text(criteria or "")
    if "Account Executive" in criteria and "sales roles" in criteria.lower():
        return criteria
    return clean_text(f"{criteria} {SALES_ROLE_EXCLUSION_CRITERIA}")


def slugify(value, limit):
    return re.sub(r"[^a-z0-9]+", "-", (value or "").lower()).strip("-")[:limit]
