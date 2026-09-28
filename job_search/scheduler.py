"""Separate managed scheduler process for durable job-search scheduling."""

from __future__ import annotations

import argparse
import os
import socket
import sys
import time
from pathlib import Path

from job_search.composition import scheduler_process_dependencies


def main() -> int:
    try:
        return _main()
    except Exception as exc:
        print(
            f'ERROR {{"error_code":"SCHEDULER_FATAL_FAILURE","component":"scheduler","operation":"main","cause":"{type(exc).__name__}"}}',
            file=sys.stderr,
        )
        return 1


def _main() -> int:
    parser = argparse.ArgumentParser(description="Run the job-search scheduler outside the web process.")
    parser.add_argument("--database", required=True, type=Path)
    parser.add_argument("--lease-seconds", type=int, default=900)
    args = parser.parse_args()
    owner = f"{socket.gethostname()}:{os.getpid()}"
    process = scheduler_process_dependencies(args.database)
    if not process.lease.acquire(owner, int(time.time()), args.lease_seconds):
        return 0
    process.search.run(trigger="scheduled", force_refresh=True)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
