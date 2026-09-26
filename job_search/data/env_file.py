"""The single reader and writer of the ``.env`` file."""

import os
import tempfile
import threading

from dotenv import dotenv_values

from job_search.observability import record_exception


def _quote(value):
    """Double-quote a value so python-dotenv reads back exactly the same string."""
    escaped = str(value).replace("\\", "\\\\").replace('"', '\\"')
    return f'"{escaped}"'


class EnvFile:
    def __init__(self, path):
        self.path = path
        self._lock = threading.Lock()

    def read(self):
        """Return the file's key/value pairs, or ``{}`` when it does not exist."""
        if not self.path.exists():
            return {}
        return {key: value for key, value in dotenv_values(self.path).items() if value is not None}

    def update(self, updates):
        """Set keys, preserving comments, order, and untouched lines. Updated values are quoted.

        Writes are serialized and atomic: the new content goes to a temporary file that
        replaces ``.env`` only once fully written.
        """
        with self._lock:
            self._write_atomically(self._render(updates))

    def _write_atomically(self, content):
        temp_name = None
        try:
            with tempfile.NamedTemporaryFile(
                "w", encoding="utf-8", dir=self.path.parent, prefix=".env.", suffix=".tmp", delete=False
            ) as handle:
                temp_name = handle.name
                handle.write(content)
            os.replace(temp_name, self.path)
        except OSError as exc:
            record_exception("env_file_write_failed", "data.env_file", "update", exc, path=str(self.path))
            if temp_name and os.path.exists(temp_name):
                os.unlink(temp_name)
            raise

    def _render(self, updates):
        order = []
        if self.path.exists():
            for line in self.path.read_text(encoding="utf-8").splitlines():
                stripped = line.strip()
                if not stripped or stripped.startswith("#") or "=" not in line:
                    order.append((None, line))
                    continue
                order.append((line.split("=", 1)[0].strip(), line))
        # dotenv gives the last duplicate precedence, so untouched duplicates keep their last line.
        last_line = {key: line for key, line in order if key}
        known_keys = set(last_line)
        for key in updates:
            if key not in known_keys:
                order.append((key, None))
                known_keys.add(key)
        lines = []
        seen = set()
        for key, original in order:
            if key is None:
                lines.append(original)
                continue
            if key in seen:
                continue
            seen.add(key)
            lines.append(f"{key}={_quote(updates[key])}" if key in updates else last_line[key])
        return "\n".join(lines).rstrip() + "\n"
