"""Safe filesystem adapter for packet Markdown content."""

from __future__ import annotations

from collections.abc import Mapping
from pathlib import Path


class FilesystemPacketContentReader:
    def __init__(self, repository_root: Path, applications_root: Path) -> None:
        self._repository_root = repository_root.resolve()
        self._applications_root = applications_root.resolve()

    def read(self, packet_path: str, filename: str) -> Mapping[str, object]:
        if not filename.endswith(".md") or "/" in filename or "\\" in filename:
            raise ValueError("Select a Markdown file in the associated packet.")
        packet_dir = (self._repository_root / packet_path).resolve()
        if packet_dir != self._applications_root and self._applications_root not in packet_dir.parents:
            raise ValueError("Application packet path must be under applications/.")
        if not packet_dir.is_dir():
            raise ValueError("Application packet folder does not exist.")
        file_path = (packet_dir / filename).resolve()
        if packet_dir not in file_path.parents or not file_path.is_file():
            raise FileNotFoundError("Markdown file not found in associated packet.")
        return {
            "path": packet_dir.relative_to(self._repository_root).as_posix(),
            "file": filename,
            "content": file_path.read_text(encoding="utf-8"),
            "markdown_files": sorted(path.name for path in packet_dir.glob("*.md") if path.is_file()),
        }
