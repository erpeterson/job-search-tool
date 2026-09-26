#!/usr/bin/env python3
# ruff: noqa: F401, F403, I001
"""Application composition entry point.

HTTP handlers live in :mod:`job_search.presentation.legacy` while the
remaining migration extracts use cases and adapters behind injectable ports.
This compatibility export preserves the existing CLI and test entry point.
"""

import sys

try:
    from job_search.presentation.legacy import *  # noqa: F403
except Exception as exc:  # Startup must not expose a traceback for invalid untrusted configuration.
    if exc.__class__.__name__ not in {"StartupConfigurationError", "SecurityConfigurationError"}:
        raise
    print(f"ERROR STARTUP_CONFIGURATION_FAILED: {exc}", file=sys.stderr)
    raise SystemExit(2) from None


if __name__ == "__main__":
    main()  # noqa: F405
