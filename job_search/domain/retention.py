"""Retention rules for generated data."""

from dataclasses import dataclass

from job_search.domain.clock import now
from job_search.domain.errors import ValidationError

SECONDS_PER_DAY = 86400


@dataclass(frozen=True)
class PruneResult:
    candidates: list
    archived: int
    archive: object
    confirmed: bool


def prune_captures(store, older_than_days, confirmed):
    """List captures older than ``older_than_days``; with ``confirmed``, move them into a compressed archive.

    Decision (T-44): captures hold model-call and tool-call records, so they are archived like rotated
    logs, never deleted outright.
    """
    if older_than_days < 1:
        raise ValidationError("--older-than must be at least 1 day.", "prune_days_invalid")
    candidates = store.files_older_than(now() - older_than_days * SECONDS_PER_DAY)
    archive, archived = store.archive_files(candidates) if confirmed else (None, 0)
    return PruneResult(candidates=candidates, archived=archived, archive=archive, confirmed=confirmed)
