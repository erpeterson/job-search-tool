"""Saved-search execution, discovery classification, and query refinement."""

import json
import logging

from job_search.domain.clock import now
from job_search.domain.discovery_filters import first_rejection
from job_search.domain.errors import AppError, DuplicateUrlError, NotFoundError, public_error_code
from job_search.domain.job_filter import apply_job_filters
from job_search.domain.levels import LevelCalibrationCache, level_assessment_from_equivalency
from job_search.domain.model_output import validate_refinement_payload
from job_search.domain.rules import SEARCH_BOARDS, UNKNOWN_LEVEL_ASSESSMENT
from job_search.domain.scoring import level_reference, score_total
from job_search.domain.text import (
    clean_text,
    normalize_pipeline,
    with_sales_role_exclusion_criteria,
    with_sales_role_exclusion_keywords,
)
from job_search.observability import correlation_scope, log_event, operation, record_exception, traced


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
    def __init__(self, db, runtime, boards, scoring, codex, parse_json, profile):
        self._db = db
        self._profile = profile
        self._runtime = runtime
        self._boards = boards
        self._scoring = scoring
        self._codex = codex
        self._parse_json = parse_json

    # Saved queries ---------------------------------------------------------

    def seed_default_queries(self):
        ts = now()
        with self._db.unit_of_work() as uow:
            exclusion = self._profile.sales_exclusion
            for query in self._profile.default_search_queries(SEARCH_BOARDS):
                existing = uow.search.find_seeded_query(query["board"], query["pipeline"])
                if existing:
                    uow.search.update_seeded_query(
                        existing["id"],
                        with_sales_role_exclusion_keywords(existing["keywords"] or query["keywords"], exclusion),
                        with_sales_role_exclusion_criteria(existing["criteria"] or query["criteria"], exclusion),
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
            # Calibration examples and the model are read once per run, not per result.
            scoring_inputs = self._scoring.scoring_inputs(uow)
        counts = {"found": 0, "tracked": 0, "rejected": 0}
        messages = []
        with (
            correlation_scope(f"search-run-{run_id}"),
            operation("search_run", "domain.search", run_id=run_id, trigger=trigger, force_refresh=force_refresh),
        ):
            try:
                for query in queries:
                    self._run_query(run_id, query, force_refresh, counts, messages, scoring_inputs)
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

    def _run_query(self, run_id, query, force_refresh, counts, messages, scoring_inputs):
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
        for result in results:
            result["pipeline"] = query.get("pipeline") or result.get("pipeline") or ""
            result["criteria"] = query.get("criteria") or ""
        counts["found"] += len(results)
        with self._db.unit_of_work() as uow:
            uow.search.set_query_last_run(query["id"], now())
            candidates = self._screen_results(uow, run_id, query["id"], results, counts)
        for result in candidates:
            outcome = self._track_result(run_id, query["id"], result, force_refresh, messages, scoring_inputs)
            if outcome in counts:  # "skipped" results are not counted
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

    def _screen_results(self, uow, run_id, query_id, results, counts):
        """Filter, level-calibrate, and deduplicate a query's results in one unit of work.

        Rejections are recorded here. Returns the results that still need scoring and tracking.
        URL checks and cached level calibrations each load with one batched query.
        """
        tracked_urls = uow.jobs.existing_urls(result.get("url") for result in results)
        levels = LevelCalibrationCache(
            uow.levels, self._profile.target_level, [result.get("company") for result in results]
        )
        candidates = []
        for result in results:
            rejection = first_rejection(result, self._profile)
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
                uow.discoveries.insert(_discovery_record(run_id, query_id, result, "rejected", rejection_reason=reason))
                counts["rejected"] += 1
                continue
            equivalency = levels.lookup(result.get("company"), result.get("title"))
            if equivalency:
                result["cached_level_assessment"] = level_assessment_from_equivalency(
                    equivalency, self._profile.target_level
                )
                result["cached_downlevel"] = bool(equivalency["downlevel"])
                log_event(
                    "level_equivalency_matched",
                    company=result.get("company"),
                    title=result.get("title"),
                    target_level=equivalency["target_level"],
                    target_title=equivalency["target_title"],
                    downlevel=bool(equivalency["downlevel"]),
                    source_url=equivalency.get("source_url"),
                )
            if result.get("url") in tracked_urls:
                log_event(
                    "discovery_skipped",
                    reason="already tracked in jobs",
                    query_id=query_id,
                    run_id=run_id,
                    **_result_fields(result),
                )
                continue
            candidates.append(result)
        return candidates

    def _track_result(self, run_id, query_id, result, force_refresh, messages, scoring_inputs):
        """Score (outside any transaction) and track one screened result. Returns the count bucket."""
        examples, model = scoring_inputs
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
            pipeline = normalize_pipeline(
                score.get("pipeline"), self._profile.pipeline_names, result.get("pipeline", "")
            )
            notes = "Auto-discovered from job search."
            if downlevel and score_total(score) < self._profile.downlevel_high_score_exception:
                log_event(
                    "discovery_downlevel_tracked",
                    reason="Downlevel relative to the target level; tracked and hidden by default.",
                    gpt_score=score_total(score),
                    level_assessment=level_assessment,
                    downlevel=downlevel,
                    **_result_fields(result),
                )

        try:
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
        except DuplicateUrlError as exc:
            record_exception(
                "search_result_already_tracked",
                "domain.search",
                "track_result",
                exc,
                level=logging.WARNING,
                recovery="The URL was tracked concurrently (manual add or another run); counted as skipped.",
                run_id=run_id,
                query_id=query_id,
                url=result.get("url"),
            )
            return "skipped"
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
            "task": f"Refine a job-board search query for {self._profile.candidate_name}.",
            "instructions": list(self._profile.refinement_instructions),
            "level_reference": level_reference(self._profile),
            "pipeline": query.get("pipeline"),
            "pipeline_criteria": query.get("criteria")
            or getattr(self._profile.pipelines.get(query.get("pipeline")), "description", ""),
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
