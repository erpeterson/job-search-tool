"""Safe UTF-8 text-file reads for composition-supplied source material."""

from pathlib import Path


def read_optional_text(path: Path) -> str:
    """Return UTF-8 content or an empty string when an optional source is absent."""
    return path.read_text(encoding="utf-8") if path.is_file() else ""
