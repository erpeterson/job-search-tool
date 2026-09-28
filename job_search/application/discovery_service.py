"""Framework-independent discovery classification and query-refinement workflows."""

from __future__ import annotations

import json
from collections.abc import Mapping
from typing import Any, Protocol

UNKNOWN_LEVEL_ASSESSMENT = "Unknown - level not assessed"

PIPELINE_CRITERIA = {
    "Executive IC": {
        "description": "Distinguished Engineer, Chief Architect, Technical Fellow, Principal Architect, Senior Principal Engineer roles at cloud, infrastructure, enterprise software, and AI platform companies.",
        "keywords": '("Distinguished Engineer" OR "Chief Architect" OR "Technical Fellow" OR "Principal Architect" OR "Senior Principal Engineer") (cloud OR infrastructure OR platform OR enterprise OR AI)',
    },
    "Office of the CTO": {
        "description": "Office of CTO, technical strategy, engineering strategy, CTO advisor, strategic initiatives, technical incubation, emerging technology roles hidden inside executive descriptions.",
        "keywords": '("Office of the CTO" OR "Technical Strategy" OR "Engineering Strategy" OR "CTO Advisor" OR "Strategic Initiatives" OR "Technical Incubation" OR "Emerging Technology")',
    },
    "Adjacent industries": {
        "description": "Architectural roles in healthcare, defense, climate, industrial automation, and scientific computing organizations with complicated technical organizations.",
        "keywords": '("Chief Architect" OR "Principal Architect" OR "Distinguished Engineer" OR "Technical Strategy") (healthcare OR defense OR climate OR "industrial automation" OR "scientific computing")',
    },
    "Wildcards": {
        "description": "Intellectually interesting roles in national labs, Disney Imagineering, Apple Vision, NVIDIA research operations, NASA contractors, AI safety, and robotics platforms.",
        "keywords": '("AI safety" OR robotics OR "research operations" OR "national lab" OR NASA OR "Apple Vision" OR Imagineering OR NVIDIA) ("Principal Engineer" OR Architect OR "Technical Strategy")',
    },
}


class DiscoveryOperations(Protocol):
    def scoring_enabled(self) -> bool: ...

    def scorer_available(self) -> bool: ...

    def scorer_path(self) -> str: ...

    def log(self, event: str, **fields: Any) -> None: ...

    def score(self, connection: Any, result: Mapping[str, Any], *, force_refresh: bool) -> Mapping[str, Any]: ...

    def create(self, connection: Any, values: Mapping[str, Any]) -> int: ...

    def apply_filter(self, connection: Any, job_id: int) -> None: ...

    def now(self) -> int: ...

    def normalize_pipeline(self, value: Any, fallback: str) -> str: ...

    def refinement_context(
        self, connection: Any, query_id: int
    ) -> tuple[Mapping[str, Any] | None, list[Mapping[str, Any]]]: ...

    def refine(self, connection: Any, prompt: Mapping[str, Any], *, force_refresh: bool) -> str | None: ...

    def parse_model_json(self, output: str) -> Mapping[str, Any]: ...

    def clean_text(self, value: Any) -> str: ...

    def update_query(self, connection: Any, query_id: int, values: Mapping[str, str]) -> None: ...

    def lookup_level(self, connection: Any, company: str | None, title: str | None) -> Mapping[str, Any] | None: ...

    def level_assessment(self, equivalency: Mapping[str, Any]) -> str: ...


class DiscoveryService:
    def __init__(self, operations: DiscoveryOperations, unknown_level: str, level_reference: str) -> None:
        self._ops = operations
        self._unknown_level = unknown_level
        self._level_reference = level_reference

    def assess_level(self, connection: Any, result: dict[str, Any]) -> None:
        equivalency = self._ops.lookup_level(connection, result.get("company"), result.get("title"))
        if not equivalency:
            self._ops.log(
                "level_equivalency_unknown",
                company=result.get("company"),
                title=result.get("title"),
                reason="No cached calibration or reliable local title estimate.",
            )
            return
        result["cached_level_assessment"] = self._ops.level_assessment(equivalency)
        result["cached_downlevel"] = bool(equivalency["downlevel"])
        self._ops.log(
            "level_equivalency_matched",
            company=result.get("company"),
            title=result.get("title"),
            oracle_level=equivalency["oracle_level"],
            oracle_title=equivalency["oracle_title"],
            downlevel=bool(equivalency["downlevel"]),
            source_url=equivalency.get("source_url"),
        )

    def refine_query(self, connection: Any, query_id: int, *, force_refresh: bool) -> None:
        ops = self._ops
        if not ops.scoring_enabled():
            ops.log("query_refinement_skipped", query_id=query_id, reason="Codex scoring disabled")
            return
        if not ops.scorer_available():
            ops.log(
                "query_refinement_skipped",
                query_id=query_id,
                reason="Codex CLI unavailable",
                codex_cli_path=ops.scorer_path(),
            )
            return
        query, recent = ops.refinement_context(connection, query_id)
        if not query or not recent:
            return
        output = ops.refine(connection, self._refinement_prompt(query, recent), force_refresh=force_refresh)
        if not output:
            return
        try:
            refined = ops.parse_model_json(output)
        except json.JSONDecodeError as exc:
            ops.log(
                "query_refinement_invalid_json",
                error_code="QUERY_REFINEMENT_INVALID_JSON",
                component="business.search_refinement",
                operation="parse_model_json",
                error_type=type(exc).__name__,
            )
            return
        keywords = ops.clean_text(refined.get("keywords") or query.get("keywords"))
        if not keywords:
            return
        ops.update_query(
            connection,
            query_id,
            {
                "keywords": keywords,
                "location": ops.clean_text(refined.get("location") or query.get("location") or "Remote"),
                "criteria": ops.clean_text(refined.get("criteria") or query.get("criteria") or ""),
                "refinement_notes": ops.clean_text(refined.get("refinement_notes") or ""),
            },
        )

    def _refinement_prompt(self, query: Mapping[str, Any], recent: list[Mapping[str, Any]]) -> Mapping[str, Any]:
        return {
            "task": "Refine a job-board search query for Eric Peterson.",
            "instructions": [
                "Return JSON only.",
                "Keep the same job board and pipeline.",
                "Improve the keywords for high-scoring roles at IC6-equivalent or higher scope.",
                "Avoid downlevel, low-score, Account Executive, and other sales results.",
            ],
            "level_reference": {
                "canonical_source": "local Oracle IC6 target definition",
                "oracle_ic6_definition": self._level_reference,
            },
            "pipeline": query.get("pipeline"),
            "pipeline_criteria": query.get("criteria")
            or PIPELINE_CRITERIA.get(query.get("pipeline"), {}).get("description", ""),
            "current_keywords": query.get("keywords"),
            "location": query.get("location"),
            "recent_results": recent,
            "expected_json_schema": {
                "keywords": "updated search query string",
                "location": "updated location string or current location",
                "criteria": "updated short criteria description",
                "refinement_notes": "what changed and why",
            },
        }

    def classify(self, connection: Any, result: Mapping[str, Any], *, force_refresh: bool):
        ops = self._ops
        if not ops.scoring_enabled() or not ops.scorer_available():
            reason = (
                "Codex scoring is disabled; discovery tracked without Codex score."
                if not ops.scoring_enabled()
                else "Codex CLI is unavailable; discovery tracked without Codex score."
            )
            event = "discovery_codex_disabled" if not ops.scoring_enabled() else "discovery_codex_cli_unavailable"
            ops.log(
                event,
                company=result.get("company"),
                title=result.get("title"),
                url=result.get("url"),
                reason=reason,
                codex_cli_path=ops.scorer_path() if event.endswith("unavailable") else None,
            )
            job_id = self._create_unscored(connection, result, reason)
            return (
                "tracked",
                reason,
                job_id,
                None,
                {},
                result.get("cached_level_assessment", ""),
                bool(result.get("cached_downlevel")),
            )
        score = ops.score(connection, result, force_refresh=force_refresh)
        scorecard = score.get("scorecard", {})
        total = int(score.get("total_score", 0))
        downlevel = bool(score.get("downlevel", False) or result.get("cached_downlevel"))
        level = score.get("level_assessment", "") or result.get("cached_level_assessment", "") or self._unknown_level
        if downlevel and total < 80:
            ops.log(
                "discovery_downlevel_tracked",
                reason="Downlevel relative to IC6-equivalent; tracked and hidden by default.",
                company=result.get("company"),
                title=result.get("title"),
                url=result.get("url"),
                gpt_score=total,
                level_assessment=level,
                downlevel=downlevel,
            )
        timestamp = ops.now()
        job_id = ops.create(
            connection,
            {
                "created_at": timestamp,
                "updated_at": timestamp,
                "company": result.get("company") or "Unknown company",
                "title": result.get("title") or "Unknown title",
                "url": result.get("url"),
                "location": result.get("location"),
                "pipeline": ops.normalize_pipeline(score.get("pipeline"), result.get("pipeline", "")),
                "posting_text": result.get("snippet"),
                "notes": "Auto-discovered from job search.",
                "gpt_score": total,
                "gpt_rationale": score.get("rationale", ""),
                "gpt_scorecard_json": json.dumps(scorecard),
                "source_board": result.get("board"),
                "source_job_id": result.get("source_job_id"),
                "discovered_at": timestamp,
                "level_assessment": level,
                "downlevel": int(downlevel),
            },
        )
        ops.apply_filter(connection, job_id)
        return "tracked", "", job_id, score, scorecard, level, downlevel

    def _create_unscored(self, connection: Any, result: Mapping[str, Any], reason: str) -> int:
        timestamp = self._ops.now()
        job_id = self._ops.create(
            connection,
            {
                "created_at": timestamp,
                "updated_at": timestamp,
                "company": result.get("company") or "Unknown company",
                "title": result.get("title") or "Unknown title",
                "url": result.get("url"),
                "location": result.get("location"),
                "pipeline": result.get("pipeline", ""),
                "posting_text": result.get("snippet"),
                "notes": f"Auto-discovered from job search. {reason}",
                "gpt_score": None,
                "gpt_rationale": None,
                "gpt_scorecard_json": None,
                "source_board": result.get("board"),
                "source_job_id": result.get("source_job_id"),
                "discovered_at": timestamp,
                "level_assessment": result.get("cached_level_assessment", "") or self._unknown_level,
                "downlevel": int(bool(result.get("cached_downlevel"))),
            },
        )
        self._ops.apply_filter(connection, job_id)
        return job_id
