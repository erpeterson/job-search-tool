#!/usr/bin/env python3
"""Entry point kept for run.sh compatibility; see job_search/cli.py."""

import sys

from job_search.cli import main

if __name__ == "__main__":
    sys.exit(main())
