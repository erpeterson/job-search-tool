"""Filesystem adapter for preserving and updating dotenv-style configuration."""

from __future__ import annotations

from collections.abc import Mapping
from pathlib import Path


def update_environment_file(path: Path, updates: Mapping[str, object]) -> None:
    """Apply values while retaining comments, blank lines, and key order."""
    existing: dict[str, str] = {}
    order: list[tuple[str | None, str | None]] = []
    if path.exists():
        for line in path.read_text(encoding="utf-8").splitlines():
            stripped = line.strip()
            if not stripped or stripped.startswith("#") or "=" not in line:
                order.append((None, line))
                continue
            key, value = line.split("=", 1)
            existing[key] = value
            order.append((key, None))
    for key, value in updates.items():
        existing[key] = str(value)
        if key not in {item[0] for item in order}:
            order.append((key, None))
    lines: list[str] = []
    seen: set[str] = set()
    for key, original in order:
        if key is None:
            lines.append(original or "")
        elif key not in seen:
            seen.add(key)
            lines.append(f"{key}={existing[key]}")
    path.write_text("\n".join(lines).rstrip() + "\n", encoding="utf-8")
