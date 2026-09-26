#!/usr/bin/env python3
# ruff: noqa: F401, F403, I001
"""Application composition entry point.

HTTP handlers live in :mod:`job_search.presentation.legacy` while the
remaining migration extracts use cases and adapters behind injectable ports.
This compatibility export preserves the existing CLI and test entry point.
"""

from job_search.presentation.legacy import *  # noqa: F403


if __name__ == "__main__":
    main()  # noqa: F405
