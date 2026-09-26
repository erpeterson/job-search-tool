"""Level-equivalency policy independent of presentation and persistence APIs."""

from __future__ import annotations

import re
from collections.abc import Callable, Mapping
from typing import Any, Protocol


class LevelRepository(Protocol):
    def find(self, normalized_company: str, normalized_title: str) -> Mapping[str, Any] | None: ...

    def save(self, values: Mapping[str, Any]) -> None: ...


def normalize_lookup_text(value: str | None) -> str:
    return re.sub(r"[^a-z0-9]+", " ", (value or "").lower()).strip()


class LevelService:
    def __init__(self, repository: LevelRepository, now: Callable[[], int]) -> None:
        self._repository = repository
        self._now = now

    def lookup(self, company: str | None, title: str | None) -> Mapping[str, Any] | None:
        normalized_company = normalize_lookup_text(company)
        normalized_title = normalize_lookup_text(title)
        if not normalized_company or not normalized_title:
            return None
        cached = self._repository.find(normalized_company, normalized_title)
        if cached:
            return cached
        estimate = self.estimate(title)
        if not estimate:
            return None
        timestamp = self._now()
        source_title = estimate["source_level_title"]
        self._repository.save(
            {
                **estimate,
                "company": (company or "").strip(),
                "normalized_company": normalized_company,
                "title_pattern": source_title,
                "normalized_title_pattern": normalize_lookup_text(source_title),
                "downlevel": int(bool(estimate["downlevel"])),
                "created_at": timestamp,
                "updated_at": timestamp,
            }
        )
        return self._repository.find(normalized_company, normalized_title)

    @staticmethod
    def estimate(title: str | None) -> dict[str, Any] | None:
        normalized_title = normalize_lookup_text(title)
        if not normalized_title:
            return None
        if any(
            re.search(pattern, normalized_title)
            for pattern in (
                r"\b(distinguished engineer|technical fellow|fellow|chief architect)\b",
                r"\b(senior principal engineer|senior principal software engineer|senior principal architect|principal architect)\b",
                r"\b(architect|enterprise architect|platform architect)\b",
            )
        ):
            return _estimate(title, "IC6+", "Architect-equivalent or higher", False)
        if any(
            re.search(pattern, normalized_title)
            for pattern in (
                r"^(new grad|entry level|junior|intern)\b",
                r"^(software engineer|senior software engineer|staff software engineer|staff engineer)(\b|$)",
                r"^engineering manager\b",
            )
        ):
            return _estimate(title, "BELOW_IC6", "Below Architect-equivalent", True)
        return None

    @staticmethod
    def assessment(equivalency: Mapping[str, Any] | None) -> str:
        if not equivalency:
            return ""
        source_level = f" {equivalency['source_level']}" if equivalency.get("source_level") else ""
        return (
            f"{equivalency['company']} {equivalency['source_level_title'] or equivalency['title_pattern']}{source_level} "
            f"maps to Oracle {equivalency['oracle_level']} {equivalency['oracle_title']} per cached level calibration."
        )


def _estimate(title: str | None, oracle_level: str, oracle_title: str, downlevel: bool) -> dict[str, Any]:
    return {
        "source_level": "",
        "source_level_title": (title or "").strip(),
        "oracle_level": oracle_level,
        "oracle_title": oracle_title,
        "downlevel": downlevel,
        "source_url": "",
        "notes": "Estimated locally from title taxonomy because Levels.fyi runtime data is unavailable.",
    }
