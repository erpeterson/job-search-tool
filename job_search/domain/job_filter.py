"""Rules deciding whether a tracked job is hidden from the default job list."""

from job_search.domain.clock import now
from job_search.domain.rules import DEFAULT_SETTINGS
from job_search.observability import log_event


def _threshold(settings, key):
    value = str(settings.get(key, "")).strip()
    return int(value) if value.lstrip("-").isdigit() else int(DEFAULT_SETTINGS[key])


def thresholds(settings):
    return _threshold(settings, "gpt_threshold"), _threshold(settings, "user_threshold")


def filter_decision(job, gpt_threshold, user_threshold, use_gpt_threshold):
    """Return ``(filtered, reasons)`` for a job row."""
    reasons = []
    if job["downlevel"]:
        reasons.append("downlevel relative to Oracle IC6-equivalent target")
    if use_gpt_threshold and job["gpt_score"] is not None and job["gpt_score"] < gpt_threshold:
        reasons.append(f"gpt_score {job['gpt_score']} below threshold {gpt_threshold}")
    if job["user_score"] is not None and job["user_score"] < user_threshold:
        reasons.append(f"user_score {job['user_score']} below threshold {user_threshold}")
    return bool(reasons), reasons


def apply_job_filters(uow, use_gpt_threshold, job_id=None):
    """Recompute ``filtered`` for one job, or for all jobs when ``job_id`` is None."""
    gpt_threshold, user_threshold = thresholds(uow.settings.all())
    updates = []
    for job in uow.jobs.filter_inputs(job_id):
        filtered, reasons = filter_decision(job, gpt_threshold, user_threshold, use_gpt_threshold)
        updates.append((job["id"], filtered))
        if filtered:
            log_event(
                "job_filtered",
                job_id=job["id"],
                company=job["company"],
                title=job["title"],
                reasons=reasons,
                gpt_score=job["gpt_score"],
                user_score=job["user_score"],
                downlevel=bool(job["downlevel"]),
                gpt_scoring_enabled=use_gpt_threshold,
            )
    uow.jobs.set_filtered_many(updates, now())
