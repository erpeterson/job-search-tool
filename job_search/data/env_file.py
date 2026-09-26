"""Persist key/value updates to a ``.env`` file, preserving comments and order."""


class EnvFile:
    def __init__(self, path):
        self.path = path

    def update(self, updates):
        existing = {}
        order = []
        if self.path.exists():
            for line in self.path.read_text(encoding="utf-8").splitlines():
                stripped = line.strip()
                if not stripped or stripped.startswith("#") or "=" not in line:
                    order.append((None, line))
                    continue
                key, value = line.split("=", 1)
                existing[key] = value
                order.append((key, None))
        known_keys = {key for key, _ in order}
        for key, value in updates.items():
            existing[key] = str(value)
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
            lines.append(f"{key}={existing[key]}")
        self.path.write_text("\n".join(lines).rstrip() + "\n", encoding="utf-8")
