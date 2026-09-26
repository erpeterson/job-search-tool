"""Codex-backed job scoring."""

import json
import textwrap

from job_search.domain.clock import now
from job_search.domain.errors import DependencyUnavailableError, ExternalServiceError, NotFoundError
from job_search.domain.job_filter import apply_job_filters
from job_search.domain.model_output import validate_score_payload
from job_search.domain.rules import RUBRIC_FIELDS
from job_search.domain.text import normalize_pipeline
from job_search.observability import log_event, operation, record_exception


def calibration_examples(rows):
    return [
        {
            "company": row["company"],
            "title": row["title"],
            "pipeline": row["pipeline"],
            "gpt_score": row["gpt_score"],
            "user_score": row["user_score"],
            "user_rationale": row["user_rationale"],
            "posting_excerpt": textwrap.shorten(row["posting_text"] or "", width=800, placeholder="..."),
        }
        for row in rows
    ]


def level_reference(profile):
    return {
        "canonical_source": f"local {profile.target_level.label} target definition",
        "target_level_definition": profile.target_level.reference,
    }


def build_scoring_prompt(job, career_context, examples, profile):
    return {
        "task": f"Score this job for {profile.candidate_name}'s job search.",
        "level_reference": level_reference(profile),
        "instructions": list(profile.scoring_instructions),
        "expected_json_schema": {
            "total_score": "integer 0-100",
            "pipeline": f"one of: {', '.join(profile.pipeline_names)}",
            "scorecard": {field: "integer 0-10" for field in RUBRIC_FIELDS},
            "level_assessment": "short phrase",
            "downlevel": "boolean",
            "rationale": "short paragraph",
            "strengths": ["short bullets"],
            "risks": ["short bullets"],
            "recommended_next_step": "short sentence",
        },
        "career_context": career_context,
        "calibration_examples": examples,
        "job": {
            "company": job.get("company"),
            "title": job.get("title"),
            "url": job.get("url"),
            "location": job.get("location"),
            "pipeline": job.get("pipeline"),
            "posting_text": job.get("posting_text"),
            "notes": job.get("notes"),
        },
    }


def score_total(score):
    """Coerce a model-reported total to an int, defaulting to 0 for missing or malformed values."""
    value = score.get("total_score", 0)
    if isinstance(value, bool) or not isinstance(value, (int, float, str)):
        return 0
    text = str(value).strip()
    return int(float(text)) if text.replace(".", "", 1).lstrip("-").isdigit() else 0


class ScoringService:
    def __init__(self, db, runtime, codex, documents, parse_json, profile):
        self._db = db
        self._profile = profile
        self._runtime = runtime
        self._codex = codex
        self._documents = documents
        self._parse_json = parse_json

    def unavailable_reason(self):
        """Return why Codex scoring cannot run, or None when it can."""
        if not self._runtime.gpt_scoring_enabled():
            return "Codex scoring is disabled."
        if not self._runtime.codex_cli_available():
            return "Codex CLI is unavailable."
        return None

    def ensure_available(self):
        if not self._runtime.gpt_scoring_enabled():
            raise DependencyUnavailableError(
                "Codex scoring is currently disabled. Set JOB_SEARCH_ENABLE_GPT_SCORING=1 to re-enable it.",
                "codex_scoring_disabled",
            )
        if not self._runtime.codex_cli_available():
            raise DependencyUnavailableError(
                "Codex CLI is unavailable. Set CODEX_CLI_PATH or install Codex CLI before scoring.",
                "codex_cli_unavailable",
                detail=f"cli_path={self._runtime.codex_cli_path()!r}",
            )

    def scoring_inputs(self, uow):
        """Read calibration examples and model setting inside an existing unit of work."""
        return (
            calibration_examples(uow.jobs.calibration_examples()),
            self._runtime.codex_model(uow.settings.all().get("codex_model")),
        )

    def score(self, job, examples, model, force_refresh=False):
        """Score a job-like dict with Codex. Does not touch the database."""
        self.ensure_available()
        prompt = build_scoring_prompt(job, self._documents.career_context(), examples, self._profile)
        result = self._codex.call_json(model, prompt, "score_job", force_refresh=force_refresh)
        if not result.output_text:
            raise ExternalServiceError("Codex CLI response did not include text output.", "codex_score_empty")
        try:
            parsed = self._parse_json(result.output_text)
        except json.JSONDecodeError as exc:
            record_exception(
                "codex_score_invalid_json", "domain.scoring", "score", exc, response_excerpt=result.output_text[:500]
            )
            raise ExternalServiceError("Codex CLI response was not valid JSON.", "codex_score_invalid_json") from exc
        return validate_score_payload(parsed)

    def check_can_score(self, job_id):
        """Raise if ``job_id`` cannot be scored now (missing job, scoring disabled, or no Codex CLI)."""
        with self._db.unit_of_work() as uow:
            if not uow.jobs.exists(job_id):
                raise NotFoundError("Job not found", "score_request_job_not_found")
        self.ensure_available()

    def score_tracked_job(self, job_id, force_refresh=False):
        """Score a tracked job on request. Returns ``(updated_job, validated_score)``."""
        self.check_can_score(job_id)
        score = self.populate_score(job_id, force_refresh=force_refresh)
        with self._db.unit_of_work() as uow:
            return uow.jobs.get(job_id), score

    def populate_score(self, job_id, force_refresh=False):
        """Score a tracked job and persist the result, re-applying visibility filters."""
        with operation("codex_score", "domain.scoring", job_id=job_id):
            with self._db.unit_of_work() as uow:
                job = uow.jobs.get(job_id)
                if not job:
                    raise NotFoundError("Job not found", "score_job_not_found")
                examples, model = self.scoring_inputs(uow)
            score = self.score(job, examples, model, force_refresh=force_refresh)
            total = score["total_score"]
            downlevel = score["downlevel"]
            with self._db.unit_of_work() as uow:
                uow.jobs.update_codex_score(
                    job_id,
                    total,
                    score["rationale"],
                    score["scorecard"],
                    normalize_pipeline(score["pipeline"], self._profile.pipeline_names, job.get("pipeline") or ""),
                    score["level_assessment"],
                    downlevel,
                    now(),
                )
                apply_job_filters(uow, self._runtime.gpt_scoring_enabled(), job_id)
            log_event("codex_score_populated", job_id=job_id, total_score=total, downlevel=downlevel)
            return score
