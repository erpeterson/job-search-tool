"""Use case for reading a Markdown file from an associated packet."""

from __future__ import annotations

from collections.abc import Mapping
from typing import Protocol


class PacketContentReader(Protocol):
    def read(self, packet_path: str, filename: str) -> Mapping[str, object]: ...


class PacketContentService:
    def __init__(self, reader: PacketContentReader) -> None:
        self._reader = reader

    def read(self, packet_path: str, filename: str) -> Mapping[str, object]:
        return self._reader.read(packet_path, filename)
