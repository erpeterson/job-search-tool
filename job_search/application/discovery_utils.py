"""Pure normalization and deduplication helpers for discovered jobs."""

from __future__ import annotations

import hashlib
from collections.abc import Mapping, Sequence
from typing import Any


def clean_text(value: Any) -> str:
    return " ".join((value or "").split())


def clean_url(value: str) -> str:
    return (value or "").split("?trk=")[0].strip()


def source_id(board: str, url: str) -> str:
    digest = hashlib.sha256((url or "").encode("utf-8")).hexdigest()[:16]
    return f"{board}:{digest}"


def dedupe_results(results: Sequence[Mapping[str, Any]]) -> list[Mapping[str, Any]]:
    seen: set[str] = set()
    deduped = []
    for result in results:
        key = result.get("url") or result.get("source_job_id")
        if not key or key in seen:
            continue
        seen.add(key)
        deduped.append(result)
    return deduped
