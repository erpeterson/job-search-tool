#!/usr/bin/env python3
# ruff: noqa: F401, F403, I001
"""Application composition entry point.

HTTP handlers live in :mod:`job_search.presentation.legacy` while the
remaining migration extracts use cases and adapters behind injectable ports.
This compatibility export preserves the existing CLI and test entry point.
"""

import sys

from job_search.config import StartupConfigurationError
from job_search.security import StartupSecurityError

try:
    from job_search.presentation.legacy import *  # noqa: F403
except (StartupConfigurationError, StartupSecurityError) as exc:
    print(f'ERROR {{"error_code":"STARTUP_CONFIGURATION_FAILED","cause":"{exc}"}}', file=sys.stderr)
    raise SystemExit(2) from None


if __name__ == "__main__":
    main()  # noqa: F405
