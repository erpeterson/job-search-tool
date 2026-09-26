"""Filesystem and Pandoc adapter for application-packet deliverables."""

from __future__ import annotations

import shutil
import subprocess
from collections.abc import Callable, Mapping, Sequence
from pathlib import Path
from typing import Any


class PacketDocumentWriter:
    def __init__(
        self, pandoc_path: Callable[[], str | None] | None = None, run: Callable[..., Any] | None = None
    ) -> None:
        self._pandoc_path = pandoc_path or (lambda: shutil.which("pandoc"))
        self._run = run or subprocess.run

    def write(self, packet_dir: Path, payload: Mapping[str, str]) -> Sequence[str]:
        packet_dir.mkdir(parents=True, exist_ok=False)
        markdown_files = {
            "Job-Brief.md": payload["job_brief_markdown"],
            "Resume.md": payload["resume_markdown"],
            "Cover-Letter.md": payload["cover_letter_markdown"],
        }
        for filename, content in markdown_files.items():
            (packet_dir / filename).write_text(content, encoding="utf-8")
        pandoc = self._pandoc_path()
        if not pandoc:
            raise RuntimeError("Pandoc is required to generate packet DOCX deliverables but was not found on PATH.")
        for filename in markdown_files:
            source = packet_dir / filename
            completed = self._run(
                [
                    pandoc,
                    "--from",
                    "markdown",
                    "--to",
                    "docx",
                    "--output",
                    str(source.with_suffix(".docx")),
                    str(source),
                ],
                text=True,
                capture_output=True,
                check=False,
            )
            if completed.returncode != 0:
                raise RuntimeError(
                    f"Pandoc failed for {filename}: {(completed.stderr or completed.stdout).strip()[:1000]}"
                )
        return list(markdown_files)
