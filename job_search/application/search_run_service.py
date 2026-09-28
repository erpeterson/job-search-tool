"""Application orchestration for a durable job-board search run.

The service deliberately depends on a small operations port.  Flask request
state, SQLite connections, board HTTP clients, and Codex invocation remain at
the composition edge.
"""

from __future__ import annotations

from collections.abc import Mapping
from contextlib import AbstractContextManager
from typing import Any, Protocol


class SearchRunOperations(Protocol):
    def now(self) -> int: ...

    def log(self, event: str, **fields: Any) -> None: ...

    def repository(self) -> Any: ...

    def connection(self) -> AbstractContextManager[Any]: ...

    def fetch(self, query: Mapping[str, Any], *, force_refresh: bool) -> list[dict[str, Any]]: ...

    def reject_reason(self, result: Mapping[str, Any]) -> tuple[str, str] | None: ...

    def level_assessment(self, connection: Any, result: dict[str, Any]) -> None: ...

    def already_seen_reason(self, connection: Any, url: str | None) -> str | None: ...

    def classify(
        self, connection: Any, result: Mapping[str, Any], *, force_refresh: bool
    ) -> tuple[str, str, int | None, Mapping[str, Any] | None, Mapping[str, Any], str, bool]: ...

    def refine(self, connection: Any, query_id: int, *, force_refresh: bool) -> None: ...

    def is_refinement_error(self, error: Exception) -> bool: ...


class SearchRunService:
    """Execute search policy without coupling it to delivery or storage APIs."""

    def __init__(self, operations: SearchRunOperations) -> None:
        self._operations = operations

    def run(self, *, trigger: str, force_refresh: bool) -> Mapping[str, Any]:
        ops = self._operations
        started = ops.now()
        ops.log("search_started", trigger=trigger, force_refresh=force_refresh)
        repository = ops.repository()
        run_id = repository.start_run(started, trigger)
        found_count = tracked_count = rejected_count = 0
        seen_urls: set[str] = set()
        messages: list[str] = []

        for query in repository.enabled_queries():
            try:
                results = ops.fetch(query, force_refresh=force_refresh)
            except Exception as exc:
                messages.append(f"{query['board']}:{query['keywords']}: {exc}")
                ops.log(
                    "job_search_query_failed",
                    error_code="JOB_SEARCH_QUERY_FAILED",
                    component="business.search",
                    operation="fetch_jobs_for_query",
                    query_id=query.get("id"),
                    board=query.get("board"),
                    error_type=type(exc).__name__,
                    message=str(exc)[:1000],
                )
                continue

            repository.mark_query_run(query["id"], ops.now())
            for result in results:
                url = result.get("url")
                if url and url in seen_urls:
                    ops.log(
                        "discovery_skipped",
                        reason="duplicate in search run",
                        url=url,
                        query_id=query["id"],
                        run_id=run_id,
                    )
                    continue
                if url:
                    seen_urls.add(url)
                result["pipeline"] = query.get("pipeline") or result.get("pipeline") or ""
                result["criteria"] = query.get("criteria") or ""
                found_count += 1
                with ops.connection() as connection:
                    rejection = ops.reject_reason(result)
                    if rejection:
                        filter_name, reason = rejection
                        rejected_count += 1
                        ops.log(
                            "discovery_rejected",
                            reason=reason,
                            filter=filter_name,
                            board=result.get("board"),
                            company=result.get("company"),
                            title=result.get("title"),
                            location=result.get("location"),
                            url=result.get("url"),
                            query_id=query["id"],
                            run_id=run_id,
                        )
                        self._record_discovery(
                            repository, run_id, query, result, "rejected", reason, None, None, {}, "", False, connection
                        )
                        continue
                    ops.level_assessment(connection, result)
                    seen_reason = ops.already_seen_reason(connection, result.get("url"))
                    if seen_reason:
                        ops.log(
                            "discovery_skipped",
                            reason=seen_reason,
                            url=result.get("url"),
                            query_id=query["id"],
                            run_id=run_id,
                        )
                        continue
                    decision, reason, job_id, score, scorecard, level, downlevel = ops.classify(
                        connection, result, force_refresh=force_refresh
                    )
                    if decision == "tracked":
                        tracked_count += 1
                    else:
                        rejected_count += 1
                    ops.log(
                        "discovery_decision",
                        decision=decision,
                        reason=reason,
                        url=result.get("url"),
                        query_id=query["id"],
                        run_id=run_id,
                        tracked_job_id=job_id,
                        gpt_score=score.get("total_score") if score else None,
                        level_assessment=level,
                        downlevel=downlevel,
                    )
                    self._record_discovery(
                        repository,
                        run_id,
                        query,
                        result,
                        decision,
                        reason,
                        job_id,
                        score,
                        scorecard,
                        level,
                        downlevel,
                        connection,
                    )
            with ops.connection() as connection:
                try:
                    ops.refine(connection, query["id"], force_refresh=force_refresh)
                except Exception as exc:
                    if not ops.is_refinement_error(exc):
                        raise
                    messages.append(f"{query['board']}:{query['keywords']}: {exc}")
                    ops.log(
                        "query_refinement_failed",
                        query_id=query["id"],
                        board=query.get("board"),
                        keywords=query.get("keywords"),
                        error_type=type(exc).__name__,
                        message=str(exc),
                    )

        message = "\n".join(messages)
        run = repository.finish_run(
            run_id,
            {
                "completed_at": ops.now(),
                "message": message,
                "found_count": found_count,
                "tracked_count": tracked_count,
                "rejected_count": rejected_count,
            },
        )
        ops.log(
            "search_completed",
            trigger=trigger,
            force_refresh=force_refresh,
            run_id=run_id,
            found_count=found_count,
            tracked_count=tracked_count,
            rejected_count=rejected_count,
            message=message,
        )
        return run

    def _record_discovery(
        self,
        repository: Any,
        run_id: int,
        query: Mapping[str, Any],
        result: Mapping[str, Any],
        decision: str,
        reason: str,
        job_id: int | None,
        score: Mapping[str, Any] | None,
        scorecard: Mapping[str, Any],
        level: str,
        downlevel: bool,
        connection: Any,
    ) -> None:
        import json

        repository.record_discovery(
            {
                "run_id": run_id,
                "query_id": query["id"],
                "created_at": self._operations.now(),
                "board": result.get("board"),
                "source_job_id": result.get("source_job_id"),
                "company": result.get("company"),
                "title": result.get("title"),
                "location": result.get("location"),
                "url": result.get("url"),
                "snippet": result.get("snippet"),
                "gpt_score": score.get("total_score") if score else None,
                "gpt_rationale": score.get("rationale") if score else None,
                "gpt_scorecard_json": json.dumps(scorecard or {}),
                "level_assessment": level,
                "downlevel": int(downlevel),
                "decision": decision,
                "rejection_reason": reason,
                "tracked_job_id": job_id,
            },
            connection=connection,
        )
