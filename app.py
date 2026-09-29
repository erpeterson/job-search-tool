#!/usr/bin/env python3
"""Application composition entry point."""

from pathlib import Path

from job_search.config import StartupConfigurationError
from job_search.presentation.cli import main, startup_failure
from job_search.security import StartupSecurityError

try:
    from job_search.composition import presentation_dependencies
    from job_search.presentation.factory import create_app

    app = create_app(dependencies=presentation_dependencies(Path(__file__).resolve().parent / "job_search.sqlite3"))
except (StartupConfigurationError, StartupSecurityError) as exc:
    startup_failure(exc, controlled=True)
except Exception as exc:
    startup_failure(exc, controlled=False)

if __name__ == "__main__":
    raise SystemExit(main(app))
