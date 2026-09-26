"""Saved-search execution, discovery classification, and query refinement."""

import json
import logging

from job_search.domain.clock import now
from job_search.domain.discovery_filters import first_rejection
from job_search.domain.errors import AppError, NotFoundError, public_error_code
from job_search.domain.job_filter import apply_job_filters
from job_search.domain.levels import level_assessment_from_equivalency, lookup_level_equivalency
from job_search.domain.model_output import validate_refinement_payload
from job_search.domain.rules import (
    DEFAULT_SEARCH_QUERIES,
    DOWNLEVEL_HIGH_SCORE_EXCEPTION,
    ORACLE_IC6_LEVEL_REFERENCE,
    PIPELINE_CRITERIA,
    UNKNOWN_LEVEL_ASSESSMENT,
)
from job_search.domain.scoring import score_total
from job_search.domain.text import (
    clean_text,
    normalize_pipeline,
    with_sales_role_exclusion_criteria,
    with_sales_role_exclusion_keywords,
)
from job_search.observability import correlation_scope, log_event, operation, record_exception, traced

REFINEMENT_INSTRUCTIONS = [
    "Return JSON only.",
    "Keep the same job board and pipeline.",
    "Improve the keywords so the next run is more likely to find high-scoring roles for this pipeline.",
    "Prefer query terms that imply Oracle IC6 Architect-equivalent or higher scope.",
    "Avoid terms that produced downlevel or low-score results.",
    "Explicitly exclude Account Executive and other sales roles.",
    "Keep the query concise enough for LinkedIn or Indeed public search boxes.",
    "Do not use Eric's personal LinkedIn or Indeed profile data.",
]


def _result_fields(result):
    return {key: result.get(key) for key in ("board", "company", "title", "location", "url")}


def _discovery_record(run_id, query_id, result, decision, **fields):
    return {
        "run_id": run_id,
        "query_id": query_id,
        "created_at": now(),
        "board": result.get("board"),
        "source_job_id": result.get("source_job_id"),
        "company": result.get("company"),
        "title": result.get("title"),
        "location": result.get("location"),
        "url": result.get("url"),
        "snippet": result.get("snippet"),
        "decision": decision,
        **fields,
    }


def _discovered_job_fields(result, pipeline, notes, level_assessment, downlevel, score=None):
    ts = now()
    fields = {
        "created_at": ts,
        "updated_at": ts,
        "company": result.get("company") or "Unknown company",
        "title": result.get("title") or "Unknown title",
        "url": result.get("url"),
        "location": result.get("location"),
        "pipeline": pipeline,
        "status": "discovered",
        "posting_text": result.get("snippet"),
        "notes": notes,
        "filtered": 0,
        "source_board": result.get("board"),
        "source_job_id": result.get("source_job_id"),
        "discovered_at": ts,
        "level_assessment": level_assessment,
        "downlevel": 1 if downlevel else 0,
    }
    if score is not None:
        fields.update(
            gpt_score=score_total(score),
            gpt_rationale=score.get("rationale", ""),
            gpt_scorecard_json=json.dumps(score.get("scorecard", {})),
        )
    return fields


class SearchService:
    def __init__(self, db, runtime, boards, scoring, codex, parse_json):
        self._db = db
        self._runtime = runtime
        self._boards = boards
        self._scoring = scoring
        self._codex = codex
        self._parse_json = parse_json

    # Saved queries ---------------------------------------------------------

    def seed_default_queries(self):
        ts = now()
        with self._db.unit_of_work() as uow:
            for query in DEFAULT_SEARCH_QUERIES:
                existing = uow.search.find_seeded_query(query["board"], query["pipeline"])
                if existing:
                    uow.search.update_seeded_query(
                        existing["id"],
                        with_sales_role_exclusion_keywords(existing["keywords"] or query["keywords"]),
                        with_sales_role_exclusion_criteria(existing["criteria"] or query["criteria"]),
                        query["location"],
                    )
                else:
                    uow.search.create_query(query, ts, seeded=True)

    def list_queries(self):
        with self._db.unit_of_work() as uow:
            return uow.search.list_queries()

    def list_runs(self):
        with self._db.unit_of_work() as uow:
            return uow.search.list_runs()

    def list_discoveries(self):
        with self._db.unit_of_work() as uow:
            return uow.discoveries.list_recent()

    def create_query(self, fields):
        with self._db.unit_of_work() as uow:
            uow.search.create_query(fields, now())
            return uow.search.list_queries()

    def update_query(self, query_id, fields):
        with self._db.unit_of_work() as uow:
            if not uow.search.get_query(query_id):
                raise NotFoundError("Search query not found", "search_query_not_found")
            uow.search.update_query(query_id, fields)
            return uow.search.list_queries()

    # Runs -------------------------------------------------------------------

    def run(self, trigger="manual", force_refresh=False):
        with self._db.unit_of_work() as uow:
            run_id = uow.search.start_run(trigger, now())
            queries = uow.search.list_enabled_queries()
        counts = {"found": 0, "tracked": 0, "rejected": 0}
        messages = []
        with (
            correlation_scope(f"search-run-{run_id}"),
            operation("search_run", "domain.search", run_id=run_id, trigger=trigger, force_refresh=force_refresh),
        ):
            try:
                for query in queries:
                    self._run_query(run_id, query, force_refresh, counts, messages)
            except Exception as exc:
                record_exception(
                    "search_run_aborted",
                    "domain.search",
                    "run",
                    exc,
                    recovery="Marking the run as error so it is not left running, then re-raising.",
                    run_id=run_id,
                )
                with self._db.unit_of_work() as uow:
                    uow.search.fail_run(
                        run_id,
                        "Search run failed unexpectedly (search_run_aborted); see logs for details.",
                        counts["found"],
                        counts["tracked"],
                        counts["rejected"],
                        now(),
                    )
                raise
            with self._db.unit_of_work() as uow:
                uow.search.complete_run(
                    run_id, "\n".join(messages), counts["found"], counts["tracked"], counts["rejected"], now()
                )
                uow.settings.set("last_search_at", now())
                run = uow.search.get_run(run_id)
            log_event(
                "search_completed",
                trigger=trigger,
                force_refresh=force_refresh,
                run_id=run_id,
                found_count=counts["found"],
                tracked_count=counts["tracked"],
                rejected_count=counts["rejected"],
                message="\n".join(messages),
            )
        return run

    def _run_query(self, run_id, query, force_refresh, counts, messages):
        label = f"{query['board']}:{query['keywords']}"
        try:
            results = self._boards.search(query["board"], query["keywords"], query["location"], force_refresh)
        except Exception as exc:
            record_exception(
                "search_board_fetch_failed",
                "domain.search",
                "run_query",
                exc,
                level=logging.WARNING,
                recovery="Board failures are reported in the run message; remaining queries still run.",
                run_id=run_id,
                query_id=query["id"],
                board=query["board"],
            )
            messages.append(
                f"{label}: board fetch failed ({public_error_code(exc, 'search_board_fetch_failed')}); see logs."
            )
            return
        with self._db.unit_of_work() as uow:
            uow.search.set_query_last_run(query["id"], now())
        for result in results:
            result["pipeline"] = query.get("pipeline") or result.get("pipeline") or ""
            result["criteria"] = query.get("criteria") or ""
            counts["found"] += 1
            outcome = self._process_result(run_id, query["id"], result, force_refresh, messages)
            if outcome in counts:
                counts[outcome] += 1
        try:
            self.refine_query(query["id"], force_refresh=force_refresh)
        except AppError as exc:
            record_exception(
                "search_query_refinement_failed",
                "domain.search",
                "refine_query",
                exc,
                level=logging.WARNING,
                recovery="Refinement is best-effort; the existing query stays in place.",
                run_id=run_id,
                query_id=query["id"],
                board=query.get("board"),
                keywords=query.get("keywords"),
            )
            messages.append(f"{label}: query refinement failed ({exc.error_code}); see logs.")

    def _process_result(self, run_id, query_id, result, force_refresh, messages):
        """Filter, deduplicate, score, and track one discovered result. Returns the count bucket."""
        rejection = first_rejection(result)
        if rejection:
            filter_name, reason = rejection
            log_event(
                "discovery_rejected",
                reason=reason,
                filter=filter_name,
                query_id=query_id,
                run_id=run_id,
                **_result_fields(result),
            )
            with self._db.unit_of_work() as uow:
                uow.discoveries.insert(_discovery_record(run_id, query_id, result, "rejected", rejection_reason=reason))
            return "rejected"

        with self._db.unit_of_work() as uow:
            equivalency = lookup_level_equivalency(uow.levels, result.get("company"), result.get("title"))
            already_tracked = uow.jobs.find_id_by_url(result.get("url")) is not None
            examples, model = self._scoring.scoring_inputs(uow)
        if equivalency:
            result["cached_level_assessment"] = level_assessment_from_equivalency(equivalency)
            result["cached_downlevel"] = bool(equivalency["downlevel"])
            log_event(
                "level_equivalency_matched",
                company=result.get("company"),
                title=result.get("title"),
                oracle_level=equivalency["oracle_level"],
                oracle_title=equivalency["oracle_title"],
                downlevel=bool(equivalency["downlevel"]),
                source_url=equivalency.get("source_url"),
            )
        if already_tracked:
            log_event(
                "discovery_skipped",
                reason="already tracked in jobs",
                query_id=query_id,
                run_id=run_id,
                **_result_fields(result),
            )
            return "skipped"

        score = None
        unavailable = self._scoring.unavailable_reason()
        if not unavailable:
            try:
                score = self._scoring.score(
                    self._discovery_as_job(result), examples, model, force_refresh=force_refresh
                )
            except AppError as exc:
                record_exception(
                    "search_result_scoring_failed",
                    "domain.search",
                    "score_result",
                    exc,
                    level=logging.WARNING,
                    recovery="Tracking the result without a Codex score; the run continues.",
                    run_id=run_id,
                    query_id=query_id,
                    url=result.get("url"),
                )
                unavailable = f"Codex scoring failed ({exc.error_code})."
                messages.append(f"Codex scoring failed for {result.get('url')} ({exc.error_code}); tracked unscored.")
        if unavailable:
            reason = f"{unavailable[:-1]}; discovery tracked without Codex score."
            log_event("discovery_tracked_without_codex", reason=reason, **_result_fields(result))
            pipeline = result.get("pipeline", "")
            level_assessment = result.get("cached_level_assessment", "") or UNKNOWN_LEVEL_ASSESSMENT
            downlevel = bool(result.get("cached_downlevel"))
            notes = f"Auto-discovered from job search. {reason}"
        else:
            reason = ""
            downlevel = bool(score.get("downlevel", False)) or bool(result.get("cached_downlevel"))
            level_assessment = (
                score.get("level_assessment", "")
                or result.get("cached_level_assessment", "")
                or UNKNOWN_LEVEL_ASSESSMENT
            )
            pipeline = normalize_pipeline(score.get("pipeline"), result.get("pipeline", ""))
            notes = "Auto-discovered from job search."
            if downlevel and score_total(score) < DOWNLEVEL_HIGH_SCORE_EXCEPTION:
                log_event(
                    "discovery_downlevel_tracked",
                    reason="Downlevel relative to IC6-equivalent; tracked and hidden by default.",
                    gpt_score=score_total(score),
                    level_assessment=level_assessment,
                    downlevel=downlevel,
                    **_result_fields(result),
                )

        with self._db.unit_of_work() as uow:
            job_id = uow.jobs.insert(
                _discovered_job_fields(result, pipeline, notes, level_assessment, downlevel, score)
            )
            apply_job_filters(uow, self._runtime.gpt_scoring_enabled(), job_id)
            uow.discoveries.insert(
                _discovery_record(
                    run_id,
                    query_id,
                    result,
                    "tracked",
                    gpt_score=score_total(score) if score else None,
                    gpt_rationale=score.get("rationale") if score else None,
                    scorecard=score.get("scorecard", {}) if score else {},
                    level_assessment=level_assessment,
                    downlevel=downlevel,
                    rejection_reason=reason,
                    tracked_job_id=job_id,
                )
            )
        log_event(
            "discovery_decision",
            decision="tracked",
            reason=reason,
            query_id=query_id,
            run_id=run_id,
            tracked_job_id=job_id,
            gpt_score=score_total(score) if score else None,
            level_assessment=level_assessment,
            downlevel=downlevel,
            **_result_fields(result),
        )
        return "tracked"

    @staticmethod
    def _discovery_as_job(result):
        return {
            "company": result.get("company"),
            "title": result.get("title"),
            "url": result.get("url"),
            "location": result.get("location"),
            "pipeline": result.get("pipeline") or "",
            "posting_text": result.get("snippet"),
            "notes": f"Source board: {result.get('board')}. Search criteria: {result.get('criteria', '')}",
        }

    # Refinement ---------------------------------------------------------------

    @traced("query_refinement", "domain.search", id_arg="query_id")
    def refine_query(self, query_id, force_refresh=False):
        unavailable = self._scoring.unavailable_reason()
        if unavailable:
            log_event("query_refinement_skipped", query_id=query_id, reason=unavailable)
            return
        with self._db.unit_of_work() as uow:
            query = uow.search.get_query(query_id)
            recent = uow.discoveries.recent_for_query(query_id) if query else []
            model = self._runtime.codex_model(uow.settings.all().get("codex_model"))
        if not query or not recent:
            return
        prompt = {
            "task": "Refine a job-board search query for Eric Peterson.",
            "instructions": REFINEMENT_INSTRUCTIONS,
            "level_reference": {
                "canonical_source": "local Oracle IC6 target definition",
                "oracle_ic6_definition": ORACLE_IC6_LEVEL_REFERENCE,
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
        output_text = self._codex.call_json(
            model, prompt, "refine_search_query", force_refresh=force_refresh
        ).output_text
        if not output_text:
            return
        try:
            refined = self._parse_json(output_text)
        except json.JSONDecodeError as exc:
            record_exception(
                "query_refinement_invalid_json",
                "domain.search",
                "refine_query",
                exc,
                level=logging.WARNING,
                recovery="Keeping the current query unchanged.",
                query_id=query_id,
                response_excerpt=output_text[:1000],
            )
            return
        refined = validate_refinement_payload(refined)
        keywords = clean_text(refined["keywords"] or query.get("keywords"))
        if not keywords:
            return
        with self._db.unit_of_work() as uow:
            uow.search.apply_refinement(
                query_id,
                keywords,
                clean_text(refined["location"] or query.get("location") or "Remote"),
                clean_text(refined["criteria"] or query.get("criteria") or ""),
                clean_text(refined["refinement_notes"]),
            )
