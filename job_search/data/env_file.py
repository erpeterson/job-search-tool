"""The single reader and writer of the ``.env`` file."""

from dotenv import dotenv_values


def _quote(value):
    """Double-quote a value so python-dotenv reads back exactly the same string."""
    escaped = str(value).replace("\\", "\\\\").replace('"', '\\"')
    return f'"{escaped}"'


class EnvFile:
    def __init__(self, path):
        self.path = path

    def read(self):
        """Return the file's key/value pairs, or ``{}`` when it does not exist."""
        if not self.path.exists():
            return {}
        return {key: value for key, value in dotenv_values(self.path).items() if value is not None}

    def update(self, updates):
        """Set keys, preserving comments, order, and untouched lines. Updated values are quoted."""
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
        self.path.write_text("\n".join(lines).rstrip() + "\n", encoding="utf-8")
