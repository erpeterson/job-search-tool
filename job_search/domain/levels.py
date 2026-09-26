"""Level calibration against the search profile's target level.

Levels.fyi runtime data is unavailable (the endpoint returns paywall text), so
unknown company/title pairs are estimated from a conservative local title
taxonomy and cached. Ambiguous titles remain unknown.
"""

from job_search.domain.clock import now
from job_search.domain.text import clean_text, normalize_lookup_text
from job_search.observability import log_event

_ESTIMATE_NOTE = "Estimated locally from title taxonomy because Levels.fyi runtime data is unavailable."


def estimate_level_equivalency(title, target):
    """Classify a title against ``target`` (a ``TargetLevel``); None when ambiguous.

    Results are stored in the ``oracle_level``/``oracle_title`` columns, which hold
    target-level values for whichever level system the profile uses.
    """
    normalized_title = normalize_lookup_text(title)
    if not normalized_title:
        return None
    base = {"source_level": "", "source_level_title": clean_text(title), "source_url": "", "notes": _ESTIMATE_NOTE}
    if any(pattern.search(normalized_title) for pattern in target.at_or_above_title_patterns):
        return {
            **base,
            "oracle_level": target.at_or_above_level,
            "oracle_title": target.at_or_above_title,
            "downlevel": False,
        }
    if any(pattern.search(normalized_title) for pattern in target.below_title_patterns):
        return {**base, "oracle_level": target.below_level, "oracle_title": target.below_title, "downlevel": True}
    return None


def find_cached_level_equivalency(level_repo, company, title):
    normalized_title = normalize_lookup_text(title)
    for row in level_repo.for_company(normalize_lookup_text(company)):
        pattern = row["normalized_title_pattern"]
        if pattern and (normalized_title == pattern or normalized_title.startswith(f"{pattern} ")):
            return row
    return None


def lookup_level_equivalency(level_repo, company, title, target):
    """Return a cached calibration, estimating and caching one when possible."""
    cached = find_cached_level_equivalency(level_repo, company, title)
    if cached:
        return cached
    equivalency = estimate_level_equivalency(title, target)
    if not equivalency:
        log_event(
            "level_equivalency_unknown",
            company=company,
            title=title,
            reason="No cached calibration or reliable local title estimate.",
        )
        return None
    level_repo.upsert(
        {
            **equivalency,
            "company": clean_text(company),
            "normalized_company": normalize_lookup_text(company),
            "title_pattern": equivalency["source_level_title"],
            "normalized_title_pattern": normalize_lookup_text(equivalency["source_level_title"]),
        },
        now(),
    )
    log_event(
        "level_equivalency_cached",
        company=company,
        title=title,
        source_level=equivalency["source_level"],
        source_level_title=equivalency["source_level_title"],
        oracle_level=equivalency["oracle_level"],
        oracle_title=equivalency["oracle_title"],
        downlevel=bool(equivalency["downlevel"]),
        source_url=equivalency["source_url"],
        source="local_title_taxonomy",
    )
    return find_cached_level_equivalency(level_repo, company, title)


def level_assessment_from_equivalency(equivalency, target):
    if not equivalency:
        return ""
    source_level = f" {equivalency['source_level']}" if equivalency.get("source_level") else ""
    return (
        f"{equivalency['company']} {equivalency['source_level_title'] or equivalency['title_pattern']}{source_level} "
        f"maps to {target.system} {equivalency['oracle_level']} {equivalency['oracle_title']} "
        "per cached level calibration."
    )
