"""Tracked-job CRM use cases: manual add, re-scrape, scoring, notes, and deletion."""

import ipaddress
import logging
from urllib.parse import urlparse

from job_search.domain.clock import now
from job_search.domain.errors import (
    CapacityError,
    DuplicateJobError,
    NotFoundError,
    ValidationError,
    public_error_code,
)
from job_search.domain.job_filter import apply_job_filters
from job_search.domain.rules import RUBRIC_FIELDS
from job_search.domain.text import append_note_text
from job_search.observability import log_event, record_exception, traced

_BLOCKED_HOSTNAMES = {"localhost", "localhost.localdomain", "metadata.google.internal"}


def validate_posting_url(url):
    """Allow only public http(s) URLs so the scraper cannot be pointed at local services.

    Hostnames are not resolved, so this blocks obvious local targets rather than
    DNS-based rebinding; the app is intended to bind to localhost only.
    """
    parsed = urlparse(url)
    if parsed.scheme not in ("http", "https") or not parsed.hostname:
        raise ValidationError("URL must be an absolute http(s) URL.", "job_url_invalid_scheme")
    host = parsed.hostname.lower()
    if host in _BLOCKED_HOSTNAMES or host.endswith(".localhost"):
        raise ValidationError("URL must not point to a local host.", "job_url_local_host")
    if ":" in host:
        raise ValidationError("URL must use a hostname, not an IPv6 address.", "job_url_ipv6_literal")
    if all(char.isdigit() or char == "." for char in host) and not _is_global_ipv4(host):
        raise ValidationError("URL must not point to a private or reserved address.", "job_url_private_address")
    return url


def _is_global_ipv4(host):
    """True only for canonical dotted-quad public addresses; shorthand forms like ``127.1`` are rejected."""
    parts = host.split(".")
    if len(parts) != 4 or not all(part.isdigit() and int(part) <= 255 for part in parts):
        return False
    return ipaddress.IPv4Address(host).is_global


def user_score_total(scorecard, total_score=None):
    if total_score is not None:
        return total_score
    return round(sum(scorecard.values()) * 100 / (len(RUBRIC_FIELDS) * 10))


class JobService:
    def __init__(self, db, runtime, boards, scoring, background):
        self._db = db
        self._runtime = runtime
        self._boards = boards
        self._scoring = scoring
        self._background = background

    def _gpt_enabled(self):
        return self._runtime.gpt_scoring_enabled()

    def get(self, job_id):
        with self._db.unit_of_work() as uow:
            job = uow.jobs.get(job_id)
        if not job:
            raise NotFoundError("Job not found", "job_not_found")
        return job

    def list(self, include_filtered=False):
        with self._db.unit_of_work() as uow:
            return uow.jobs.list(include_filtered=include_filtered)

    @traced("manual_job_create", "domain.jobs")
    def create_manual(self, fields, force_refresh=False):
        """Scrape and track a job from a URL, then start background scoring when Codex is available.

        The scrape is bounded by the HTTP timeout and redirect limit. Returns a dict with
        ``job``, ``scrape_error``, ``score_error`` (why scoring was skipped), and
        ``score_task`` (the pollable scoring task, or None).
        """
        url = validate_posting_url(fields["url"])
        scrape_error = None
        try:
            scraped = self._boards.scrape_posting(url, force_refresh=force_refresh)
        except Exception as exc:
            record_exception(
                "manual_job_scrape_failed",
                "domain.jobs",
                "create_manual",
                exc,
                level=logging.WARNING,
                recovery="Saving the job with URL-derived fallback metadata.",
                url=url,
                pipeline=fields["pipeline"],
            )
            scrape_error = (
                f"Posting could not be scraped ({public_error_code(exc, 'manual_job_scrape_failed')}); "
                "saved with URL-derived details. See logs for the cause."
            )
            scraped = self.fallback_posting(url)
        final_url = scraped.get("url") or url
        ts = now()
        with self._db.unit_of_work() as uow:
            existing_id = uow.jobs.find_id_by_url(final_url)
            if existing_id:
                raise DuplicateJobError(uow.jobs.get(existing_id))
            scrape_note = f" {scrape_error}" if scrape_error else ""
            job_id = uow.jobs.insert(
                {
                    "created_at": ts,
                    "updated_at": ts,
                    "company": fields.get("company") or scraped.get("company") or "Unknown company",
                    "title": fields.get("title") or scraped.get("title") or "Unknown title",
                    "url": final_url,
                    "location": fields.get("location") or scraped.get("location", ""),
                    "pipeline": fields["pipeline"],
                    "status": fields.get("status") or "researching",
                    "posting_text": fields.get("posting_text") or scraped.get("posting_text", ""),
                    "notes": fields.get("notes") or f"Added manually from URL.{scrape_note}",
                    "source_board": scraped.get("source_board"),
                    "source_job_id": scraped.get("source_job_id"),
                    "discovered_at": ts,
                }
            )
            apply_job_filters(uow, self._gpt_enabled(), job_id)
        score_error, score_task = self._start_auto_score(job_id)
        return {
            "job": self.get(job_id),
            "scrape_error": scrape_error,
            "score_error": score_error,
            "score_task": score_task,
        }

    def fallback_posting(self, url):
        """Metadata for a job whose posting could not be scraped, derived from the URL."""
        host = (urlparse(url).hostname or "").removeprefix("www.")
        return {
            **self._boards.describe_url(url),
            "company": host or "Unknown company",
            "title": f"Job posting from {host}" if host else "Unknown title",
            "location": "",
            "posting_text": "",
        }

    def _start_auto_score(self, job_id):
        """Return ``(skip_reason, task)``: scoring runs as a background task when Codex is available."""
        unavailable = self._scoring.unavailable_reason()
        if unavailable:
            log_event("manual_job_auto_score_skipped", job_id=job_id, reason=unavailable)
            return f"Automatic Codex scoring skipped: {unavailable}", None
        try:
            return None, self._background.start_auto_score(job_id)
        except CapacityError as exc:
            record_exception(
                "manual_job_auto_score_deferred",
                "domain.jobs",
                "auto_score",
                exc,
                level=logging.WARNING,
                recovery="The job is saved; it can be scored from the UI once a task slot frees up.",
                job_id=job_id,
            )
            return f"Automatic Codex scoring skipped: {exc.message}", None

    @traced("job_rescrape", "domain.jobs", id_arg="job_id")
    def rescrape(self, job_id, force_refresh=True):
        job = self.get(job_id)
        if not job.get("url"):
            raise ValidationError("Job does not have a URL to scrape.", "rescrape_missing_url")
        scraped = self._boards.scrape_posting(validate_posting_url(job["url"]), force_refresh=force_refresh)
        with self._db.unit_of_work() as uow:
            uow.jobs.update_scraped_posting(
                job_id,
                {
                    "company": scraped.get("company") or job["company"],
                    "title": scraped.get("title") or job["title"],
                    "location": scraped.get("location") or job["location"],
                    "posting_text": scraped.get("posting_text") or job["posting_text"],
                    "source_board": scraped.get("source_board") or job["source_board"],
                    "source_job_id": scraped.get("source_job_id") or job["source_job_id"],
                    "notes": append_note_text(job.get("notes"), "Re-scraped posting URL."),
                },
                now(),
            )
            apply_job_filters(uow, self._gpt_enabled(), job_id)
        log_event(
            "manual_job_rescraped",
            job_id=job_id,
            url=job["url"],
            company=scraped.get("company"),
            title=scraped.get("title"),
            force_refresh=force_refresh,
        )
        return self.get(job_id), scraped

    @traced("job_delete", "domain.jobs", id_arg="job_id")
    def delete(self, job_id):
        with self._db.unit_of_work() as uow:
            job = uow.jobs.get(job_id)
            if not job:
                raise NotFoundError("Job not found", "delete_job_not_found")
            uow.discoveries.detach_job(job_id)
            uow.jobs.delete(job_id)
        log_event("manual_job_deleted", job_id=job_id, company=job["company"], title=job["title"], url=job["url"])

    @traced("jobs_purge", "domain.jobs")
    def purge_all(self):
        with self._db.unit_of_work() as uow:
            uow.discoveries.detach_all_jobs()
            deleted = uow.jobs.delete_all()
        log_event("admin_purge_jobs", deleted_jobs=deleted)
        return deleted

    def _require(self, uow, job_id, error_code):
        if not uow.jobs.exists(job_id):
            raise NotFoundError("Job not found", error_code)

    def score_user(self, job_id, scorecard, rationale, total_score=None):
        with self._db.unit_of_work() as uow:
            self._require(uow, job_id, "user_score_job_not_found")
            uow.jobs.update_user_score(job_id, user_score_total(scorecard, total_score), scorecard, rationale, now())
            apply_job_filters(uow, self._gpt_enabled(), job_id)
        return self.get(job_id)

    def add_interaction(self, job_id, fields):
        with self._db.unit_of_work() as uow:
            self._require(uow, job_id, "interaction_job_not_found")
            ts = now()
            uow.jobs.add_interaction(job_id, fields, ts)
            uow.jobs.touch(job_id, ts)
        return self.get(job_id)

    def add_note(self, job_id, note):
        with self._db.unit_of_work() as uow:
            self._require(uow, job_id, "note_job_not_found")
            ts = now()
            uow.jobs.add_note(job_id, note, ts)
            uow.jobs.touch(job_id, ts)
        return self.get(job_id)

    def update_status(self, job_id, status):
        with self._db.unit_of_work() as uow:
            self._require(uow, job_id, "status_job_not_found")
            uow.jobs.update_status(job_id, status, now())
        return self.get(job_id)
