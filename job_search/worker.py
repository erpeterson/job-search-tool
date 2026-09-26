"""Managed durable-task worker; intentionally separate from the web process."""

from __future__ import annotations

import argparse
import os
import socket
import time
from collections.abc import Callable, Mapping
from typing import Any

from job_search.task_repository import TaskRepository


def process_one(
    repository: TaskRepository,
    worker_id: str,
    processor: Callable[[Mapping[str, Any]], tuple[str, str]],
    *,
    now: Callable[[], int] = lambda: int(time.time()),
    lease_seconds: int = 300,
) -> bool:
    """Claim and process one item, returning false when no durable work exists."""
    claim = repository.claim_next_item(worker_id, now(), lease_seconds)
    if not claim:
        return False
    try:
        status, message = processor(claim)
    except Exception as exc:
        status, message = "error", f"Worker processing failed: {type(exc).__name__}: {exc}"
    repository.complete_claim(claim["task_id"], claim["job_id"], worker_id, now(), status=status, message=message)
    return True


def main() -> int:
    """Run the separate worker executable until interrupted."""
    parser = argparse.ArgumentParser(description="Process durable job-search tasks outside the web process.")
    parser.add_argument("--database", required=True)
    parser.add_argument("--poll-seconds", type=float, default=1.0)
    args = parser.parse_args()
    repository = TaskRepository(args.database)
    repository.initialize(int(time.time()))
    worker_id = f"{socket.gethostname()}:{os.getpid()}"
    from job_search.presentation.legacy import process_background_task_item

    while True:
        processed = process_one(repository, worker_id, process_background_task_item)
        if not processed:
            time.sleep(args.poll_seconds)


if __name__ == "__main__":
    raise SystemExit(main())
