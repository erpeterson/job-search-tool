"""In-process background tasks for bulk Codex operations, with pollable progress."""

import logging
import threading
import uuid

from job_search.domain.clock import now
from job_search.domain.errors import AppError
from job_search.domain.scoring import score_total
from job_search.observability import correlation_scope, log_event, record_exception

GENERIC_ITEM_ERROR = "Unexpected error; see logs for details."
JOB_NOT_FOUND_CODES = {"score_job_not_found", "packet_job_not_found"}


def _snapshot(task):
    snapshot = dict(task)
    snapshot["items"] = [dict(item) for item in task.get("items", [])]
    return snapshot


class BackgroundTaskRegistry:
    """Thread-safe task registry. Finished tasks beyond ``max_retained`` are evicted oldest-first."""

    def __init__(self, max_retained=50, thread_factory=threading.Thread):
        self._tasks = {}
        self._lock = threading.Lock()
        self._max_retained = max_retained
        self._thread_factory = thread_factory

    def get(self, task_id):
        with self._lock:
            task = self._tasks.get(task_id)
            return _snapshot(task) if task else None

    def list(self, limit=10):
        with self._lock:
            tasks = sorted(self._tasks.values(), key=lambda task: task["created_at"], reverse=True)
            return [_snapshot(task) for task in tasks[:limit]]

    def update(self, task_id, **updates):
        with self._lock:
            task = self._tasks.get(task_id)
            if not task:
                return None
            task.update(updates)
            task["updated_at"] = now()
            return _snapshot(task)

    def update_item(self, task_id, job_id, **updates):
        with self._lock:
            task = self._tasks.get(task_id)
            if not task:
                return None
            for item in task["items"]:
                if item["job_id"] == job_id:
                    item.update(updates)
                    item["updated_at"] = now()
                    break
            task["updated_at"] = now()
            return _snapshot(task)

    def _evict_finished(self):
        finished = sorted(
            (task for task in self._tasks.values() if task["status"] in ("complete", "error")),
            key=lambda task: task["created_at"],
        )
        for task in finished[: max(0, len(self._tasks) - self._max_retained)]:
            del self._tasks[task["id"]]

    def start(self, operation_name, job_ids, worker):
        task_id = uuid.uuid4().hex
        created_at = now()
        task = {
            "id": task_id,
            "operation": operation_name,
            "status": "queued",
            "created_at": created_at,
            "updated_at": created_at,
            "started_at": None,
            "completed_at": None,
            "total": len(job_ids),
            "completed": 0,
            "failed": 0,
            "skipped": 0,
            "current_job_id": None,
            "message": "",
            "items": [
                {"job_id": job_id, "status": "queued", "message": "", "updated_at": created_at} for job_id in job_ids
            ],
        }
        with self._lock:
            self._evict_finished()
            self._tasks[task_id] = task
        log_event("background_task_started", task_id=task_id, operation=operation_name, job_ids=job_ids)
        thread = self._thread_factory(target=self._guarded, args=(worker, task_id, job_ids), daemon=True)
        thread.start()
        return _snapshot(task)

    def start_call(self, operation_name, call, job_ids=()):
        """Run ``call()`` in the background as a single task and store its return value as ``result``."""

        def worker(task_id, _job_ids):
            self.update(task_id, status="running", started_at=now(), message=f"{operation_name} running")
            try:
                result = call()
            except AppError as exc:
                record_exception(
                    f"{operation_name}_task_failed",
                    "domain.tasks",
                    operation_name,
                    exc,
                    level=logging.WARNING,
                    recovery="Stored the failure on the task for the client to read.",
                    task_id=task_id,
                )
                self._finish(task_id, "error", exc.message, error_code=exc.error_code)
                return
            except Exception as exc:
                record_exception(
                    f"{operation_name}_task_crashed",
                    "domain.tasks",
                    operation_name,
                    exc,
                    recovery="Stored a generic failure on the task.",
                    task_id=task_id,
                )
                self._finish(task_id, "error", GENERIC_ITEM_ERROR, error_code=f"{operation_name}_task_crashed")
                return
            self._finish(task_id, "complete", f"{operation_name} complete", result=result)

        return self.start(operation_name, list(job_ids), worker)

    def _finish(self, task_id, status, message, **fields):
        counts = {"completed": 1} if status == "complete" else {"failed": 1}
        for job_id in [item["job_id"] for item in (self.get(task_id) or {}).get("items", [])]:
            self.update_item(task_id, job_id, status=status, message=message)
        self.update(
            task_id, status=status, message=message, completed_at=now(), current_job_id=None, **counts, **fields
        )

    def _guarded(self, worker, task_id, job_ids):
        """Thread entry point: a crash outside per-item handling still ends the task in ``error``."""
        try:
            worker(task_id, job_ids)
        except Exception as exc:
            record_exception(
                "background_task_crashed",
                "domain.tasks",
                "run_worker",
                exc,
                recovery="Marked the task as error so the UI stops polling a dead task.",
                task_id=task_id,
            )
            self._mark_crashed(task_id)

    def _mark_crashed(self, task_id):
        # Writes the dict directly: the regular update path may be what failed.
        with self._lock:
            task = self._tasks.get(task_id)
            if task:
                task.update(
                    status="error",
                    current_job_id=None,
                    completed_at=now(),
                    updated_at=now(),
                    message="Task stopped unexpectedly (background_task_crashed); see logs for details.",
                )


class BulkOperations:
    """Background work: bulk scoring and packets, plus single long-running operations.

    Single operations validate preconditions synchronously, so the caller still gets
    immediate 4xx errors, then run the slow part as a pollable task.
    """

    def __init__(self, registry, scoring, packets, search=None):
        self._registry = registry
        self._scoring = scoring
        self._packets = packets
        self._search = search

    def start_search(self, force_refresh=False):
        def call():
            return {"run": self._search.run(trigger="manual", force_refresh=force_refresh)}

        return self._registry.start_call("search_run", call)

    def start_packet(self, job_id):
        self._packets.check_can_generate(job_id)
        return self._registry.start_call(
            "application_packet", lambda: {"packet": self._packets.create_packet(job_id)}, [job_id]
        )

    def start_score(self, job_id):
        self._scoring.check_can_score(job_id)
        return self._registry.start_call("codex_score", lambda: self._score_result(job_id), [job_id])

    def start_auto_score(self, job_id):
        """Score a newly added job in the background; the caller has already checked availability."""
        return self._registry.start_call("codex_auto_score", lambda: self._score_result(job_id), [job_id])

    def _score_result(self, job_id):
        return {"raw_score": self._scoring.populate_score(job_id)}

    def start_scoring(self, job_ids):
        self._scoring.ensure_available()
        return self._registry.start("scorecards", job_ids, self._score_worker)

    def start_packets(self, job_ids):
        self._packets.ensure_available()
        return self._registry.start("application_packets", job_ids, self._packet_worker)

    def _score_one(self, job_id):
        score = self._scoring.populate_score(job_id)
        return "complete", f"Codex score {score_total(score)}"

    def _packet_one(self, job_id):
        packet = self._packets.create_packet(job_id, skip_if_associated=True)
        if packet is None:
            return "skipped", "Application packet already associated"
        return "complete", packet.get("path") or "Codex completed"

    def _score_worker(self, task_id, job_ids):
        self._run(
            task_id,
            job_ids,
            "scorecards",
            "Codex scorecard population running",
            "Scoring with Codex",
            "scored",
            self._score_one,
        )

    def _packet_worker(self, task_id, job_ids):
        self._run(
            task_id,
            job_ids,
            "application_packets",
            "Application packet generation running",
            "Generating application packet with Codex",
            "generated",
            self._packet_one,
        )

    def _run(self, task_id, job_ids, operation_name, running_message, item_message, done_verb, handle):
        registry = self._registry
        with correlation_scope(f"task-{task_id}"):
            registry.update(task_id, status="running", started_at=now(), message=running_message)
            counts = {"complete": 0, "error": 0, "skipped": 0}
            for job_id in job_ids:
                registry.update(task_id, current_job_id=job_id)
                registry.update_item(task_id, job_id, status="running", message=item_message)
                status, message = self._run_item(task_id, job_id, operation_name, handle)
                counts[status] += 1
                registry.update_item(task_id, job_id, status=status, message=message)
                registry.update(
                    task_id, completed=counts["complete"], failed=counts["error"], skipped=counts["skipped"]
                )
            status = "complete" if counts["error"] == 0 else "error"
            message = (
                f"Complete: {counts['complete']} {done_verb}, {counts['skipped']} skipped, {counts['error']} failed."
            )
            registry.update(task_id, status=status, completed_at=now(), current_job_id=None, message=message)
            log_event(
                "background_task_finished", task_id=task_id, operation=operation_name, status=status, message=message
            )

    @staticmethod
    def _run_item(task_id, job_id, operation_name, handle):
        """Process one job; failures are recorded per item so the batch continues."""
        try:
            return handle(job_id)
        except AppError as exc:
            if exc.error_code in JOB_NOT_FOUND_CODES:
                record_exception(
                    f"bulk_{operation_name}_item_job_missing",
                    "domain.tasks",
                    operation_name,
                    exc,
                    level=logging.INFO,
                    recovery="The job was deleted after selection; the item is skipped.",
                    task_id=task_id,
                    job_id=job_id,
                )
                return "skipped", "Job not found"
            record_exception(
                f"bulk_{operation_name}_item_failed",
                "domain.tasks",
                operation_name,
                exc,
                recovery="Marked the item as failed; the batch continues.",
                task_id=task_id,
                job_id=job_id,
            )
            return "error", exc.message
        except Exception as exc:
            record_exception(
                f"bulk_{operation_name}_item_crashed",
                "domain.tasks",
                operation_name,
                exc,
                recovery="Marked the item as failed; the batch continues.",
                task_id=task_id,
                job_id=job_id,
            )
            return "error", GENERIC_ITEM_ERROR
