"""Filesystem adapter for safely resolving application-packet directories."""

from __future__ import annotations

import tempfile
from collections.abc import Callable, Mapping, Sequence
from pathlib import Path


class PacketStorage:
    def __init__(self, repository_root: Path, applications_root: Path) -> None:
        self._repository_root = repository_root.resolve()
        self._applications_root = applications_root.resolve()

    def packet_relative_path(self, value: str) -> str:
        candidate = (self._repository_root / value).resolve()
        if candidate != self._applications_root and self._applications_root not in candidate.parents:
            raise ValueError("Application packet path must be under applications/.")
        if not candidate.exists() or not candidate.is_dir():
            raise ValueError("Application packet folder does not exist.")
        return candidate.relative_to(self._repository_root).as_posix()

    def publish(
        self, name: str, payload: Mapping[str, str], writer: Callable[[Path, Mapping[str, str]], Sequence[str]]
    ) -> tuple[Path, Sequence[str]]:
        """Atomically publish a fully generated packet or leave no partial directory."""
        destination = self._applications_root / name
        if destination.exists():
            raise FileExistsError(f"Application packet directory already exists: {destination}")
        self._applications_root.mkdir(parents=True, exist_ok=True)
        with tempfile.TemporaryDirectory(prefix=".packet-staging-", dir=self._applications_root) as staging_root:
            staged = Path(staging_root) / name
            files = writer(staged, payload)
            staged.replace(destination)
        return destination, files
