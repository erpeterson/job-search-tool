"""Filesystem and SQLite read adapter for application-packet catalog entries."""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from pathlib import Path
from typing import Any

from job_search.data_access.read_models import SqliteReadModels


class ApplicationPacketCatalog:
    """List packets without exposing filesystem traversal to HTTP handlers."""

    def __init__(self, repository_root: Path, applications_root: Path) -> None:
        self._repository_root = repository_root.resolve()
        self._applications_root = applications_root.resolve()

    def list(self, connection: Any) -> Sequence[Mapping[str, Any]]:
        self._applications_root.mkdir(parents=True, exist_ok=True)
        associated_rows = SqliteReadModels.application_packet_jobs(connection)
        associated_by_path = {row["application_packet_path"]: row for row in associated_rows}
        packets = []
        for path in sorted(self._applications_root.iterdir()):
            if not path.is_dir():
                continue
            markdown_files = self._markdown_files(path)
            if not markdown_files:
                continue
            relative = path.resolve().relative_to(self._repository_root).as_posix()
            associated_job = associated_by_path.get(relative)
            packets.append(
                {
                    "path": relative,
                    "name": path.name,
                    "markdown_files": markdown_files,
                    "associated_job": associated_job,
                    "unassociated": associated_job is None,
                }
            )
        return packets

    @staticmethod
    def _markdown_files(packet_dir: Path) -> list[str]:
        return sorted(path.name for path in packet_dir.glob("*.md") if path.is_file())
