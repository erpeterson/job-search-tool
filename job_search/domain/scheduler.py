"""Daily search scheduler."""

import threading

from job_search.domain.clock import now
from job_search.observability import log_event, record_exception

POLL_SECONDS = 15 * 60


class SearchScheduler:
    def __init__(self, db, search, interval_seconds, enabled, poll_seconds=POLL_SECONDS):
        self._db = db
        self._search = search
        self._interval_seconds = interval_seconds
        self._enabled = enabled
        self._poll_seconds = poll_seconds
        self._stop = threading.Event()

    def state(self):
        with self._db.unit_of_work() as uow:
            raw = str(uow.settings.all().get("last_search_at", "0") or "0")
        last_search_at = int(raw) if raw.isdigit() else 0
        next_run_at = last_search_at + self._interval_seconds if last_search_at else now()
        return {
            "autorun_enabled": self._enabled,
            "interval_seconds": self._interval_seconds,
            "last_search_at": last_search_at,
            "next_run_at": next_run_at if self._enabled else None,
        }

    def tick(self):
        """Run a search when the interval has elapsed. The first tick only records a baseline."""
        try:
            last_search_at = self.state()["last_search_at"]
            if last_search_at == 0:
                with self._db.unit_of_work() as uow:
                    uow.settings.set("last_search_at", now())
                return
            if now() - last_search_at >= self._interval_seconds:
                self._search.run(trigger="scheduled", force_refresh=True)
        except Exception as exc:
            record_exception(
                "scheduled_search_failed",
                "domain.scheduler",
                "tick",
                exc,
                recovery="Recorded as a failed search run; the scheduler retries on the next poll.",
            )
            with self._db.unit_of_work() as uow:
                uow.search.record_failed_run("scheduled", "Scheduled search failed; see logs for details.", now())

    def _loop(self):
        while not self._stop.is_set():
            self.tick()
            self._stop.wait(self._poll_seconds)

    def start(self):
        if not self._enabled:
            log_event("scheduler_disabled")
            return None
        thread = threading.Thread(target=self._loop, name="job-search-scheduler", daemon=True)
        thread.start()
        log_event("scheduler_started", interval_seconds=self._interval_seconds)
        return thread

    def stop(self):
        self._stop.set()
