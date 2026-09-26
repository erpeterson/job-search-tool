"""Separate managed scheduler process for durable job-search scheduling."""

from __future__ import annotations

import argparse
import os
import socket
import time
from pathlib import Path

from job_search.data_access.scheduler_repository import SchedulerLeaseRepository


def main() -> int:
    parser = argparse.ArgumentParser(description="Run the job-search scheduler outside the web process.")
    parser.add_argument("--database", required=True, type=Path)
    parser.add_argument("--lease-seconds", type=int, default=900)
    args = parser.parse_args()
    owner = f"{socket.gethostname()}:{os.getpid()}"
    lease = SchedulerLeaseRepository(args.database)
    if not lease.acquire(owner, int(time.time()), args.lease_seconds):
        return 0
    from job_search.presentation.legacy import run_job_search

    run_job_search(trigger="scheduled", force_refresh=True)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
