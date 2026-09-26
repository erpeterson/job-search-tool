#!/usr/bin/env python3
# ruff: noqa: F401, F403, I001
"""Application composition entry point."""

import sys
from pathlib import Path

from job_search.config import StartupConfigurationError
from job_search.composition import presentation_dependencies
from job_search.security import StartupSecurityError

try:
    from job_search.presentation.factory import create_app
    from job_search.presentation.legacy import main
except (StartupConfigurationError, StartupSecurityError) as exc:
    print(f'ERROR {{"error_code":"STARTUP_CONFIGURATION_FAILED","cause":"{exc}"}}', file=sys.stderr)
    raise SystemExit(2) from None

app = create_app(dependencies=presentation_dependencies(Path(__file__).resolve().parent / "job_search.sqlite3"))


if __name__ == "__main__":
    main(app)
