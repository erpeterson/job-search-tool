"""Scoring thresholds and runtime configuration use cases."""

from job_search.domain.job_filter import apply_job_filters
from job_search.domain.rules import DEFAULT_SETTINGS
from job_search.observability import traced


class SettingsService:
    def __init__(self, db, runtime, default_codex_model=""):
        self._db = db
        self._runtime = runtime
        self._default_codex_model = default_codex_model

    def seed_defaults(self):
        with self._db.unit_of_work() as uow:
            for key, value in {**DEFAULT_SETTINGS, "codex_model": self._default_codex_model}.items():
                uow.settings.set_default(key, value)

    def all(self):
        with self._db.unit_of_work() as uow:
            return uow.settings.all()

    def update_thresholds(self, updates):
        """Persist validated settings and recompute visibility for every job in one pass."""
        with self._db.unit_of_work() as uow:
            for key, value in updates.items():
                uow.settings.set(key, value)
            apply_job_filters(uow, self._runtime.gpt_scoring_enabled())
            return uow.settings.all(), uow.jobs.list(include_filtered=True)

    @traced("runtime_config_update", "domain.settings")
    def update_runtime_config(self, updates):
        """Persist runtime config to ``.env`` and the process; mirror the model into settings."""
        if not updates:
            return None
        self._runtime.apply(updates)
        with self._db.unit_of_work() as uow:
            if "CODEX_MODEL" in updates:
                uow.settings.set("codex_model", updates["CODEX_MODEL"])
            return uow.settings.all()
