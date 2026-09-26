"""Subprocess adapter for the local Codex CLI."""

from __future__ import annotations

import re
import subprocess
import tempfile
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path


@dataclass(frozen=True)
class CodexCliResult:
    output_text: str
    effective_model: str
    returncode: int
    stdout: str
    stderr: str


class CodexCliGateway:
    """Invoke Codex without coupling application workflows to subprocess APIs."""

    def __init__(self, root: Path, run: Callable[..., object] | None = None) -> None:
        self._root = root
        self._run = run or subprocess.run

    def execute(self, cli_path: str, model: str, instruction: str, timeout_seconds: int) -> CodexCliResult:
        with tempfile.TemporaryDirectory(prefix="job-search-codex-") as directory:
            output_path = Path(directory) / "last-message.txt"
            command = [cli_path, "exec", "-C", str(self._root), "--sandbox", "read-only", "-o", str(output_path), "-"]
            if model:
                command[2:2] = ["-m", model]
            completed = self._run(
                command, input=instruction, text=True, capture_output=True, timeout=timeout_seconds, check=False
            )
            stdout = completed.stdout or ""
            stderr = completed.stderr or ""
            output = output_path.read_text(encoding="utf-8").strip() if output_path.exists() else stdout.strip()
            return CodexCliResult(
                output, self.reported_model(f"{stderr}\n{stdout}"), completed.returncode, stdout, stderr
            )

    @staticmethod
    def reported_model(output: str) -> str:
        match = re.search(r"\bmodel:\s*([^\s]+)", output or "", flags=re.IGNORECASE)
        return match.group(1) if match else ""
