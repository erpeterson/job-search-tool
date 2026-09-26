"""Replayable request/response captures keyed by a hash of the request payload."""

import hashlib
import json
import logging
import os
import tempfile
from datetime import UTC, datetime
from pathlib import Path

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

    def read(self, service, operation, request_payload, force_refresh=False, is_success=None):
        """Return a replayable capture, or None.

        ``is_success(response_payload)`` decides whether a capture may be replayed;
        failed outcomes are kept on disk as evidence but trigger a live call.
        """
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
        if is_success is not None and not is_success(capture.get("response") or {}):
            log_event("capture_replay_skipped_failed", service=service, operation=operation, path=str(path))
            return None
        log_event("capture_replay", service=service, operation=operation, path=str(path))
        return capture

    def files_older_than(self, cutoff_epoch):
        """Capture files last modified before ``cutoff_epoch``, oldest first."""
        if not self._capture_dir.exists():
            return []
        files = [path for path in self._capture_dir.rglob("*.json") if path.stat().st_mtime < cutoff_epoch]
        return sorted(files, key=lambda path: path.stat().st_mtime)

    def delete_files(self, paths):
        """Delete the given capture files; returns the number deleted. Paths outside the capture dir are refused."""
        root = self._capture_dir.resolve()
        deleted = 0
        for path in paths:
            resolved = Path(path).resolve()
            if root not in resolved.parents:
                raise ValueError(f"Refusing to delete a file outside the capture directory: {resolved}")
            try:
                resolved.unlink()
            except OSError as exc:
                record_exception(
                    "capture_delete_failed",
                    "data.captures",
                    "delete_files",
                    exc,
                    level=logging.WARNING,
                    recovery="Skipping this file; the rest are still deleted.",
                    path=str(resolved),
                )
                continue
            deleted += 1
        log_event("captures_pruned", deleted=deleted, requested=len(paths))
        return deleted

    def write(self, service, operation, request_payload, response_payload, metadata=None):
        """Atomically write a capture. Never raises for I/O errors, so it cannot mask a caller's error."""
        path = self.path_for(service, operation, request_payload)
        capture = {
            "captured_at": datetime.now(UTC).isoformat(),
            "service": service,
            "operation": operation,
            "request": request_payload,
            "response": response_payload,
            "metadata": metadata or {},
        }
        content = json.dumps(capture, indent=2, sort_keys=True, default=str) + "\n"
        temp_name = None
        try:
            path.parent.mkdir(parents=True, exist_ok=True)
            with tempfile.NamedTemporaryFile(
                "w", encoding="utf-8", dir=path.parent, prefix=f".{path.stem}.", suffix=".tmp", delete=False
            ) as handle:
                temp_name = handle.name
                handle.write(content)
            os.replace(temp_name, path)
        except OSError as exc:
            record_exception(
                "capture_write_failed",
                "data.captures",
                "write",
                exc,
                level=logging.WARNING,
                recovery="Continuing without a capture; the caller's own outcome is unaffected.",
                service=service,
                capture_operation=operation,
                path=str(path),
            )
            if temp_name and os.path.exists(temp_name):
                os.unlink(temp_name)
            return None
        log_event("capture_write", service=service, operation=operation, path=str(path))
        return path
