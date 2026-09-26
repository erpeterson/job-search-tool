"""Replayable request/response captures keyed by a hash of the request payload."""

import hashlib
import json
import logging
from datetime import UTC, datetime

from job_search.observability import log_event, record_exception


def stable_json(value):
    return json.dumps(value, sort_keys=True, separators=(",", ":"), default=str)


class CaptureStore:
    def __init__(self, capture_dir, cache_enabled):
        """``cache_enabled`` is a zero-argument callable so runtime config changes apply immediately."""
        self._capture_dir = capture_dir
        self._cache_enabled = cache_enabled

    def path_for(self, service, operation, request_payload):
        digest = hashlib.sha256(stable_json(request_payload).encode("utf-8")).hexdigest()
        return self._capture_dir / service / operation / f"{digest}.json"

    def read(self, service, operation, request_payload, force_refresh=False):
        if force_refresh:
            log_event("capture_bypass", service=service, operation=operation, reason="force_refresh")
            return None
        if not self._cache_enabled():
            return None
        path = self.path_for(service, operation, request_payload)
        if not path.exists():
            return None
        try:
            capture = json.loads(path.read_text(encoding="utf-8"))
        except (json.JSONDecodeError, UnicodeDecodeError) as exc:
            record_exception(
                "capture_file_corrupt",
                "data.captures",
                "read",
                exc,
                level=logging.WARNING,
                recovery="Treating as a cache miss; a live request will overwrite the capture.",
                service=service,
                capture_operation=operation,
                path=str(path),
            )
            return None
        log_event("capture_replay", service=service, operation=operation, path=str(path))
        return capture

    def write(self, service, operation, request_payload, response_payload, metadata=None):
        path = self.path_for(service, operation, request_payload)
        path.parent.mkdir(parents=True, exist_ok=True)
        capture = {
            "captured_at": datetime.now(UTC).isoformat(),
            "service": service,
            "operation": operation,
            "request": request_payload,
            "response": response_payload,
            "metadata": metadata or {},
        }
        path.write_text(json.dumps(capture, indent=2, sort_keys=True, default=str) + "\n", encoding="utf-8")
        log_event("capture_write", service=service, operation=operation, path=str(path))
        return path
