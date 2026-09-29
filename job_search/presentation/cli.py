"""Web process startup at the presentation boundary."""

import json
import sys


def startup_failure(exc: Exception, *, controlled: bool) -> None:
    """Report composition failures before an injected telemetry graph exists."""
    record = {
        "error_code": "STARTUP_CONFIGURATION_FAILED" if controlled else "STARTUP_UNHANDLED_EXCEPTION",
        "component": "web_startup",
        "operation": "compose_app",
        "cause": type(exc).__name__,
    }
    if controlled:
        record["detail"] = str(exc)
    print("ERROR " + json.dumps(record, sort_keys=True), file=sys.stderr)
    raise SystemExit(2 if controlled else 1) from None


def main(application):
    dependencies = application.extensions["job_search.dependencies"]
    try:
        dependencies.startup_service.initialize()
        configuration = dependencies.configuration
        print(f"Job Search Console running at http://{configuration.settings.host}:{configuration.settings.port}")
        print(f"Database: {dependencies.database_path}")
        application.run(
            host=configuration.settings.host,
            port=configuration.settings.port,
            debug=configuration.settings.debug,
            use_reloader=False,
        )
        return 0
    except Exception as exc:
        dependencies.observability.telemetry.event(
            "web_fatal_failure",
            error_code="WEB_FATAL_FAILURE",
            component="presentation.cli",
            operation="run_web_process",
            database_name=dependencies.database_path.name,
            cause=type(exc).__name__,
        )
        print(
            "ERROR "
            + json.dumps(
                {
                    "error_code": "WEB_FATAL_FAILURE",
                    "component": "presentation.cli",
                    "operation": "main",
                    "cause": type(exc).__name__,
                },
                sort_keys=True,
            ),
            file=sys.stderr,
        )
        return 1
