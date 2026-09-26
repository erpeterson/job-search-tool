"""Structured logging, correlation IDs, and blame metrics.

Event and API logs are newline-delimited JSON written to rotating files. Every
caught exception is reported through ``record_exception`` which emits both a
``blame_metric`` telemetry event and a unique ``exception`` log line keyed by a
stable error code.
"""

import functools
import gzip
import json
import logging
import shutil
import sys
import threading
import time
import traceback
import uuid
from collections import Counter
from contextlib import contextmanager
from contextvars import ContextVar
from datetime import UTC, datetime
from logging.handlers import RotatingFileHandler
from pathlib import Path

ROOT_LOGGER_NAME = "job_search"
EVENT_LOGGER_NAME = "job_search.events"
API_LOGGER_NAME = "job_search.api"
MAX_CAUSE_CHARS = 1000

_correlation_id = ContextVar("correlation_id", default=None)


class Metrics:
    """Thread-safe in-process counters.

    A single process-wide registry is used deliberately: metrics are
    append-only counters with no behavioral effect, the same model as a
    Prometheus default registry.
    """

    def __init__(self):
        self._lock = threading.Lock()
        self._counts = Counter()

    def increment(self, name, value=1):
        with self._lock:
            self._counts[name] += value

    def snapshot(self):
        with self._lock:
            return dict(self._counts)


METRICS = Metrics()


def new_correlation_id():
    return uuid.uuid4().hex


def current_correlation_id():
    return _correlation_id.get()


def bind_correlation_id(correlation_id):
    """Bind a correlation ID for the current context; pass the result to ``reset_correlation_id``."""
    return _correlation_id.set(correlation_id)


def reset_correlation_id(token):
    _correlation_id.reset(token)


@contextmanager
def correlation_scope(correlation_id=None):
    """Bind a correlation ID to all log events emitted inside the block."""
    token = _correlation_id.set(correlation_id or new_correlation_id())
    try:
        yield _correlation_id.get()
    finally:
        _correlation_id.reset(token)


def _event_payload(event_type, level, fields):
    payload = {
        "ts": datetime.now(UTC).isoformat(),
        "level": logging.getLevelName(level),
        "event": event_type,
        **fields,
    }
    correlation_id = current_correlation_id()
    if correlation_id and "correlation_id" not in payload:
        payload["correlation_id"] = correlation_id
    return payload


def log_event(event_type, level=logging.INFO, **fields):
    payload = _event_payload(event_type, level, fields)
    logging.getLogger(EVENT_LOGGER_NAME).log(level, json.dumps(payload, sort_keys=True, default=str))


def log_api_call(service, method, url, response=None, error=None, elapsed_ms=None):
    response_text = getattr(response, "text", "") if response is not None else ""
    payload = _event_payload(
        "api_call",
        logging.INFO if error is None else logging.WARNING,
        {
            "service": service,
            "method": method,
            "url": url,
            "status_code": getattr(response, "status_code", None) if response is not None else None,
            "ok": response is not None and bool(getattr(response, "ok", False)) and error is None,
            "elapsed_ms": elapsed_ms,
            "error_type": type(error).__name__ if error else None,
            "message": sanitize_cause(error) if error else None,
            "response_excerpt": " ".join(response_text.split())[:2000] if response_text else None,
        },
    )
    logging.getLogger(API_LOGGER_NAME).info(json.dumps(payload, sort_keys=True, default=str))


def sanitize_cause(exc, limit=MAX_CAUSE_CHARS):
    """Return a single-line, length-bounded description of an exception."""
    return " ".join(str(exc).split())[:limit]


def record_exception(error_code, component, operation, exc, level=logging.ERROR, recovery=None, **context):
    """Emit the blame metric and a unique log line for a caught exception.

    ``recovery`` documents why continuing is valid when the exception is handled
    rather than propagated. Tracebacks are written only for ERROR severity.
    """
    metric_name = f"blame.{error_code}"
    METRICS.increment(metric_name)
    log_event("blame_metric", level=logging.INFO, metric=metric_name, value=1, component=component)
    fields = {
        "error_code": error_code,
        "component": component,
        "operation": operation,
        "error_type": type(exc).__name__,
        "cause": sanitize_cause(exc),
        **context,
    }
    detail = getattr(exc, "detail", None)
    if detail:
        fields["detail"] = sanitize_cause(detail, limit=2000)
    if recovery:
        fields["recovery"] = recovery
    if level >= logging.ERROR:
        fields["traceback"] = "".join(traceback.format_exception(type(exc), exc, exc.__traceback__))[-4000:]
    log_event("exception", level=level, **fields)


@contextmanager
def operation(name, component, **fields):
    """Emit start, success, and failure events around a major operation."""
    started = time.monotonic()
    log_event(f"{name}_started", component=component, **fields)
    try:
        yield
    except Exception as exc:
        elapsed_ms = int((time.monotonic() - started) * 1000)
        log_event(
            f"{name}_failed",
            level=logging.WARNING,
            component=component,
            elapsed_ms=elapsed_ms,
            error_type=type(exc).__name__,
            error_code=getattr(exc, "error_code", None),
            **fields,
        )
        record_exception(
            f"{name}_failed",
            component,
            name,
            exc,
            elapsed_ms=int((time.monotonic() - started) * 1000),
            **fields,
        )
        raise
    log_event(f"{name}_succeeded", component=component, elapsed_ms=int((time.monotonic() - started) * 1000), **fields)


def install_thread_excepthook():
    """Route uncaught exceptions in any thread to structured telemetry instead of bare stderr."""

    def hook(args):
        if issubclass(args.exc_type, SystemExit):
            return
        record_exception(
            "thread_unhandled_exception",
            "threading",
            getattr(args.thread, "name", "unknown"),
            args.exc_value or args.exc_type(),
            recovery="The thread ended; see traceback.",
        )

    threading.excepthook = hook
    return hook


def traced(name, component, id_arg=None):
    """Decorate a method so each call emits ``<name>_started/_succeeded/_failed`` events.

    ``id_arg`` names the first positional argument after ``self`` to include in the events.
    """

    def decorate(func):
        @functools.wraps(func)
        def wrapper(self, *args, **kwargs):
            fields = {id_arg: args[0] if args else kwargs.get(id_arg)} if id_arg else {}
            with operation(name, component, **fields):
                return func(self, *args, **kwargs)

        return wrapper

    return decorate


class _BelowLevelFilter(logging.Filter):
    def __init__(self, level):
        super().__init__()
        self.level = level

    def filter(self, record):
        return record.levelno < self.level


class ArchivingRotatingFileHandler(RotatingFileHandler):
    """Rotates at ``maxBytes`` into a timestamped ``.gz`` archive that is never deleted.

    Logs are evidence, so rotated files are compressed and kept rather than discarded;
    removing old archives is a deliberate manual step.
    """

    def __init__(self, filename, max_bytes):
        super().__init__(filename, maxBytes=max_bytes, backupCount=0)

    def doRollover(self):  # noqa: N802 - overrides logging.Handler API
        if self.stream:
            self.stream.close()
            self.stream = None
        source = Path(self.baseFilename)
        if source.exists() and source.stat().st_size:
            stamp = datetime.now(UTC).strftime("%Y%m%dT%H%M%S%fZ")
            archive = source.with_name(f"{source.name}.{stamp}.gz")
            with source.open("rb") as raw, gzip.open(archive, "wb") as compressed:
                shutil.copyfileobj(raw, compressed)
            source.unlink()
        if not self.delay:
            self.stream = self._open()


def configure_file_logging(event_log_path, api_log_path, max_bytes):
    """Attach rotating JSON-lines handlers for the event and API logs."""
    event_log_path.parent.mkdir(parents=True, exist_ok=True)
    api_log_path.parent.mkdir(parents=True, exist_ok=True)
    for name, path in ((EVENT_LOGGER_NAME, event_log_path), (API_LOGGER_NAME, api_log_path)):
        logger = logging.getLogger(name)
        logger.setLevel(logging.INFO)
        if any(getattr(handler, "_job_search_file", False) for handler in logger.handlers):
            continue
        handler = ArchivingRotatingFileHandler(path, max_bytes)
        handler.setFormatter(logging.Formatter("%(message)s"))
        handler._job_search_file = True
        logger.addHandler(handler)


def configure_console_logging(verbose=False, stdout=None, stderr=None):
    """Route ERROR+ to stderr always and lower severities to stdout only when verbose.

    Werkzeug's per-request access log is routed the same way so non-verbose runs
    keep stdout free for command output.
    """
    stdout = stdout or sys.stdout
    stderr = stderr or sys.stderr
    formatter = logging.Formatter("%(message)s")
    for name in (ROOT_LOGGER_NAME, "werkzeug"):
        logger = logging.getLogger(name)
        for handler in [h for h in logger.handlers if getattr(h, "_job_search_console", False)]:
            logger.removeHandler(handler)
        logger.propagate = False
        logger.setLevel(logging.INFO if verbose else logging.ERROR)
        error_handler = logging.StreamHandler(stderr)
        error_handler.setLevel(logging.ERROR)
        error_handler.setFormatter(formatter)
        error_handler._job_search_console = True
        logger.addHandler(error_handler)
        if verbose:
            info_handler = logging.StreamHandler(stdout)
            info_handler.setLevel(logging.INFO)
            info_handler.addFilter(_BelowLevelFilter(logging.ERROR))
            info_handler.setFormatter(formatter)
            info_handler._job_search_console = True
            logger.addHandler(info_handler)
    # File loggers must keep INFO even when the console shows only errors.
    logging.getLogger(EVENT_LOGGER_NAME).setLevel(logging.INFO)
    logging.getLogger(API_LOGGER_NAME).setLevel(logging.INFO)
