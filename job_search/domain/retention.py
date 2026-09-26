"""Retention rules for generated data."""

from dataclasses import dataclass

from job_search.domain.clock import now
from job_search.domain.errors import ValidationError

SECONDS_PER_DAY = 86400


@dataclass(frozen=True)
class PruneResult:
    candidates: list
    deleted: int
    confirmed: bool


def prune_captures(store, older_than_days, confirmed):
    """List captures older than ``older_than_days``; delete them only when ``confirmed``."""
    if older_than_days < 1:
        raise ValidationError("--older-than must be at least 1 day.", "prune_days_invalid")
    candidates = store.files_older_than(now() - older_than_days * SECONDS_PER_DAY)
    deleted = store.delete_files(candidates) if confirmed else 0
    return PruneResult(candidates=candidates, deleted=deleted, confirmed=confirmed)
