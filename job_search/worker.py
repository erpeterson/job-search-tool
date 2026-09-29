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


def process_one(
    repository: TaskRepository,
    worker_id: str,
    processor: Callable[[Mapping[str, Any]], tuple[str, str]],
    *,
    telemetry: Telemetry,
    now: Callable[[], int] = lambda: int(time.time()),
    lease_seconds: int = 300,
) -> bool:
    """Claim and process one item, returning false when no durable work exists."""
    claim = repository.claim_next_item(worker_id, now(), lease_seconds)
    if not claim:
        return False
    stop_heartbeat = threading.Event()

    def heartbeat() -> None:
        interval = max(1, lease_seconds // 3)
        while not stop_heartbeat.wait(interval):
            repository.renew_claim(claim["task_id"], claim["job_id"], worker_id, now(), lease_seconds)

    heartbeat_thread = threading.Thread(target=heartbeat, name=f"lease-{claim['job_id']}", daemon=True)
    heartbeat_thread.start()
    try:
        try:
            status, message = processor(claim)
        except Exception as exc:
            status, message = "error", f"Worker processing failed: {type(exc).__name__}: {exc}"
    finally:
        stop_heartbeat.set()
        heartbeat_thread.join(timeout=lease_seconds)
    repository.complete_claim(claim["task_id"], claim["job_id"], worker_id, now(), status=status, message=message)
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
