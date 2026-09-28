"""Replayable request/response captures keyed by a hash of the request payload."""

import hashlib
import json
import logging
import os
import tarfile
import tempfile
from datetime import UTC, datetime
from pathlib import Path

from job_search.observability import log_event, record_exception

ARCHIVE_DIRNAME = "archive"


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
        files = [
            path
            for path in self._capture_dir.rglob("*.json")
            if ARCHIVE_DIRNAME not in path.relative_to(self._capture_dir).parts and path.stat().st_mtime < cutoff_epoch
        ]
        return sorted(files, key=lambda path: path.stat().st_mtime)

    def archive_files(self, paths):
        """Move captures into a new ``archive/captures-<UTC>.tar.gz``; returns ``(archive_path, count)``.

        Captures include Codex prompts and responses (model-call records), so pruning archives
        rather than deletes. Originals are removed only after the archive is written and verified.
        """
        root = self._capture_dir.resolve()
        resolved = []
        for path in paths:
            candidate = Path(path).resolve()
            if root not in candidate.parents:
                raise ValueError(f"Refusing to archive a file outside the capture directory: {candidate}")
            resolved.append(candidate)
        if not resolved:
            return None, 0
        archive_dir = root / ARCHIVE_DIRNAME
        archive_dir.mkdir(parents=True, exist_ok=True)
        archive = archive_dir / f"captures-{datetime.now(UTC).strftime('%Y%m%dT%H%M%S%fZ')}.tar.gz"
        names = [path.relative_to(root).as_posix() for path in resolved]
        with tarfile.open(archive, "w:gz") as bundle:
            for path, name in zip(resolved, names, strict=True):
                bundle.add(path, arcname=name)
        with tarfile.open(archive, "r:gz") as bundle:
            archived = set(bundle.getnames())
        if archived != set(names):
            raise OSError(f"Capture archive {archive} is incomplete; originals were kept.")
        removed = 0
        for path in resolved:
            try:
                path.unlink()
            except OSError as exc:
                record_exception(
                    "capture_archive_unlink_failed",
                    "data.captures",
                    "archive_files",
                    exc,
                    level=logging.WARNING,
                    recovery="The file is safely archived; the original copy is left in place.",
                    path=str(path),
                )
                continue
            removed += 1
        log_event("captures_archived", archive=str(archive), archived=len(names), removed=removed)
        return archive, removed

    def failure_path_for(self, service, operation, request_payload):
        """A new, never-overwritten path for a failed outcome: ``<digest>.failed.<UTC timestamp>.json``."""
        success = self.path_for(service, operation, request_payload)
        stamp = datetime.now(UTC).strftime("%Y%m%dT%H%M%S%fZ")
        path = success.with_name(f"{success.stem}.failed.{stamp}.json")
        counter = 1
        while path.exists():
            path = success.with_name(f"{success.stem}.failed.{stamp}-{counter}.json")
            counter += 1
        return path

    def write(self, service, operation, request_payload, response_payload, metadata=None, succeeded=True):
        """Atomically write a capture. Never raises for I/O errors, so it cannot mask a caller's error.

        Successful outcomes replace ``<digest>.json`` (the only file replay reads). Failed outcomes go to
        their own timestamped file so neither the evidence nor the last good response is overwritten.
        """
        if succeeded:
            path = self.path_for(service, operation, request_payload)
        else:
            path = self.failure_path_for(service, operation, request_payload)
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
