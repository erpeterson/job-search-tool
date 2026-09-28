"""Command-line entry point that starts the local web console.

Exit codes: 0 success, 1 unexpected failure, 2 invalid configuration or
arguments, 130 interrupted.
"""

import argparse
import logging
import os
import sys

from job_search.config import DEFAULT_APP_DIR, AppConfig
from job_search.container import build_container
from job_search.data.captures import CaptureStore
from job_search.data.env_file import EnvFile
from job_search.domain.errors import ConfigurationError, ValidationError
from job_search.domain.retention import prune_captures
from job_search.observability import (
    configure_console_logging,
    configure_file_logging,
    install_thread_excepthook,
    log_event,
    process_run_id,
    record_exception,
)
from job_search.web.app import create_app

EXIT_OK = 0
EXIT_FAILURE = 1
EXIT_CONFIG_ERROR = 2
EXIT_INTERRUPTED = 130


def parse_args(argv):
    parser = argparse.ArgumentParser(prog="job-search", description="Run the local Job Search Console.")
    parser.add_argument("-v", "--verbose", action="store_true", help="Write non-error logs to stdout.")
    parser.add_argument("--host", help="Bind address (overrides JOB_SEARCH_HOST).")
    parser.add_argument("--port", help="Port (overrides JOB_SEARCH_PORT).")
    commands = parser.add_subparsers(dest="command", metavar="COMMAND")
    prune = commands.add_parser(
        "prune-captures", help="Archive old request/response captures (dry run unless --yes is given)."
    )
    prune.add_argument("--older-than", type=int, required=True, metavar="DAYS", help="Age threshold in days.")
    prune.add_argument("--yes", action="store_true", help="Move the listed captures into a .tar.gz archive.")
    return parser.parse_args(argv)


def run_prune_captures(args, environ, out, app_dir):
    """List captures older than --older-than days; with --yes, move them into a compressed archive."""
    config = AppConfig.from_env(startup_settings(environ, app_dir / ".env", args), app_dir=app_dir)
    configure_file_logging(config.event_log_path, config.api_log_path, config.log_max_bytes)
    result = prune_captures(CaptureStore(config.capture_dir, lambda: True), args.older_than, confirmed=args.yes)
    for path in result.candidates:
        print(path, file=out)
    if result.confirmed:
        where = f" into {result.archive}" if result.archive else ""
        print(f"Archived {result.archived} of {len(result.candidates)} capture files{where}.", file=out)
    else:
        print(f"Dry run: {len(result.candidates)} capture files would be archived. Re-run with --yes.", file=out)


def startup_settings(environ, env_path, args):
    """Merge settings without mutating the process environment.

    Precedence: CLI arguments, then the environment, then ``.env``.
    """
    settings = {**EnvFile(env_path).read(), **environ}
    if args.host:
        settings["JOB_SEARCH_HOST"] = args.host
    if args.port:
        settings["JOB_SEARCH_PORT"] = args.port
    return settings


def run(args, environ, serve, out, app_dir):
    environ = startup_settings(environ, app_dir / ".env", args)
    config = AppConfig.from_env(environ, app_dir=app_dir)
    install_thread_excepthook()
    configure_file_logging(config.event_log_path, config.api_log_path, config.log_max_bytes)
    container = build_container(config, environ=environ)
    container.bootstrap()
    container.scheduler.start()
    app = create_app(container)
    print(f"Job Search Console running at http://{config.host}:{config.port}", file=out)
    print(f"Database: {config.db_path}", file=out)
    log_event(
        "app_started", host=config.host, port=config.port, pid=os.getpid(), started_process_run_id=process_run_id()
    )
    outcome = "error"
    try:
        serve(app, config)
        outcome = "ok"
    except KeyboardInterrupt:
        outcome = "interrupted"
        raise
    finally:
        log_event("app_stopped", outcome=outcome)


def _serve(app, config):
    app.run(host=config.host, port=config.port, debug=config.debug, use_reloader=False)


def main(argv=None, environ=None, serve=_serve, out=None, app_dir=DEFAULT_APP_DIR):
    args = parse_args(argv)
    configure_console_logging(verbose=args.verbose)
    environ = os.environ if environ is None else environ
    try:
        if args.command == "prune-captures":
            run_prune_captures(args, environ, out or sys.stdout, app_dir)
        else:
            run(args, environ, serve, out or sys.stdout, app_dir)
        return EXIT_OK
    except ValidationError as exc:
        record_exception(exc.error_code, "cli", "arguments", exc, recovery="Exiting with usage error code.")
        print(f"Invalid arguments: {exc.message}", file=sys.stderr)
        return EXIT_CONFIG_ERROR
    except ConfigurationError as exc:
        record_exception(exc.error_code, "cli", "startup", exc, recovery="Exiting with configuration error code.")
        print(f"Configuration error: {exc.message}", file=sys.stderr)
        return EXIT_CONFIG_ERROR
    except KeyboardInterrupt as exc:
        record_exception("cli_interrupted", "cli", "run", exc, level=logging.INFO, recovery="User interrupted.")
        return EXIT_INTERRUPTED
    except Exception as exc:
        record_exception("cli_unhandled_exception", "cli", "run", exc, recovery="Exiting with failure code.")
        return EXIT_FAILURE


if __name__ == "__main__":
    sys.exit(main())
