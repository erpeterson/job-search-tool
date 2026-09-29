"""Managed durable-task worker; intentionally separate from the web process."""

from __future__ import annotations

import argparse
import os
import socket
import sys
import threading
import time
from collections.abc import Callable, Mapping
from pathlib import Path
from typing import Any

from job_search.application.contracts import Telemetry
from job_search.composition import worker_process_dependencies
from job_search.task_repository import TaskRepository


class HeartbeatRenewalError(RuntimeError):
    """The worker must stop after it can no longer maintain its task lease."""


def process_one(
    repository: TaskRepository,
    worker_id: str,
    processor: Callable[[Mapping[str, Any]], tuple[str, str]],
    *,
    telemetry: Telemetry,
    now: Callable[[], int] = lambda: int(time.time()),
    lease_seconds: int = 300,
    wait_for_heartbeat: Callable[[float], bool] | None = None,
) -> bool:
    """Claim and process one item, returning false when no durable work exists."""
    claim = repository.claim_next_item(worker_id, now(), lease_seconds)
    if not claim:
        return False
    stop_heartbeat = threading.Event()
    heartbeat_failures: list[Exception] = []

    def heartbeat() -> None:
        interval = max(1, lease_seconds // 3)
        wait = wait_for_heartbeat or stop_heartbeat.wait
        while not wait(interval):
            try:
                if not repository.renew_claim(claim["task_id"], claim["job_id"], worker_id, now(), lease_seconds):
                    raise HeartbeatRenewalError("Task lease is no longer owned by this worker.")
            except Exception as exc:
                heartbeat_failures.append(exc)
                stop_heartbeat.set()
                return

    heartbeat_thread = threading.Thread(target=heartbeat, name=f"lease-{claim['job_id']}", daemon=True)
    heartbeat_thread.start()
    try:
        try:
            status, message = processor(claim)
        except Exception as exc:
            telemetry.event(
                "worker_processor_failed",
                error_code="WORKER_PROCESSOR_FAILED",
                component="worker",
                operation="process_claim",
                task_id=claim["task_id"],
                job_id=claim["job_id"],
                task_operation=claim["operation"],
                worker_id=worker_id,
                cause=type(exc).__name__,
            )
            status, message = "error", f"Worker processing failed: {type(exc).__name__}"
    finally:
        stop_heartbeat.set()
        heartbeat_thread.join(timeout=lease_seconds)
    if heartbeat_thread.is_alive():
        heartbeat_failures.append(HeartbeatRenewalError("Heartbeat thread did not stop after processing."))
    if heartbeat_failures:
        failure = heartbeat_failures[0]
        telemetry.event(
            "worker_heartbeat_failed",
            error_code="WORKER_HEARTBEAT_FAILED",
            component="worker",
            operation="renew_claim",
            task_id=claim["task_id"],
            job_id=claim["job_id"],
            task_operation=claim["operation"],
            worker_id=worker_id,
            cause=type(failure).__name__,
        )
        repository.complete_claim(
            claim["task_id"],
            claim["job_id"],
            worker_id,
            now(),
            status="error",
            message="Worker lease renewal failed.",
        )
        raise HeartbeatRenewalError("Worker stopped after lease renewal failed.") from failure
    if not repository.complete_claim(
        claim["task_id"], claim["job_id"], worker_id, now(), status=status, message=message
    ):
        telemetry.event(
            "worker_claim_lost",
            error_code="WORKER_CLAIM_LOST",
            component="worker",
            operation="complete_claim",
            task_id=claim["task_id"],
            job_id=claim["job_id"],
            worker_id=worker_id,
            cause="LeaseNotOwned",
        )
        raise HeartbeatRenewalError("Worker stopped after losing task ownership.")
    return True


def main() -> int:
    """Run the separate worker executable until interrupted."""
    try:
        return _main()
    except Exception as exc:
        print(
            f'ERROR {{"error_code":"WORKER_FATAL_FAILURE","component":"worker","operation":"main","cause":"{type(exc).__name__}"}}',
            file=sys.stderr,
        )
        return 1


def _main() -> int:
    parser = argparse.ArgumentParser(description="Process durable job-search tasks outside the web process.")
    parser.add_argument("--database", required=True, type=Path)
    parser.add_argument("--poll-seconds", type=float, default=1.0)
    args = parser.parse_args()
    process = worker_process_dependencies(args.database)
    repository = process.repository
    repository.initialize(int(time.time()))
    worker_id = f"{socket.gethostname()}:{os.getpid()}"
    while True:
        processed = process_one(
            repository, worker_id, process.processor.process, telemetry=process.observability.telemetry
        )
        if not processed:
            time.sleep(args.poll_seconds)


if __name__ == "__main__":
    raise SystemExit(main())
