"""Captured Codex JSON transport with structured events and no workflow policy."""

from __future__ import annotations

import json
import time
from collections.abc import Callable, Mapping
from typing import Any

from job_search.data_access.capture_store import CaptureStore
from job_search.data_access.codex_cli import CodexCliGateway


class CodexCliError(RuntimeError):
    def __init__(self, operation: str, returncode: int | None = None) -> None:
        self.operation = operation
        self.returncode = returncode
        suffix = f" exited with code {returncode}" if returncode is not None else " failed"
        super().__init__(f"Codex CLI {operation}{suffix}. See logs and captures for details.")


class CodexJsonGateway:
    def __init__(
        self,
        cli: CodexCliGateway,
        captures: CaptureStore,
        observe: Callable[..., None],
        cli_path: Callable[[], str],
        timeout_seconds: int,
    ) -> None:
        self._cli = cli
        self._captures = captures
        self._observe = observe
        self._cli_path = cli_path
        self._timeout_seconds = timeout_seconds

    def complete(
        self,
        model: str,
        prompt: Mapping[str, Any],
        operation: str,
        *,
        force_refresh: bool = False,
        return_metadata: bool = False,
    ) -> str | tuple[str, str]:
        path = self._cli_path()
        request_payload = {"adapter_version": 2, "cli_path": path, "model": model, "prompt": prompt}
        cached = self._captures.read("codex_cli", operation, request_payload, force_refresh=force_refresh)
        if cached:
            output = cached["response"].get("output_text", "")
            return (output, cached["response"].get("effective_model", "")) if return_metadata else output

        started = time.monotonic()
        self._observe(
            "codex_cli_call_started",
            operation=operation,
            model=model,
            cli_path=path,
            timeout_seconds=self._timeout_seconds,
        )
        completed = None
        error = None
        output = ""
        effective_model = ""
        instruction = (
            "You are a JSON-only engine for a local job-search app.\n"
            "Return only one valid JSON object. Do not include markdown fences, prose, or explanations outside JSON.\n\n"
            f"{json.dumps(prompt, indent=2, sort_keys=True, default=str)}\n"
        )
        try:
            completed = self._cli.execute(path, model, instruction, self._timeout_seconds)
            output = completed.output_text
            effective_model = completed.effective_model
            if completed.returncode != 0:
                raise CodexCliError(operation, completed.returncode)
            return (output, effective_model) if return_metadata else output
        except Exception as exc:
            error = exc
            raise
        finally:
            elapsed_ms = int((time.monotonic() - started) * 1000)
            response_payload = {
                "output_text": output,
                "effective_model": effective_model,
                "returncode": completed.returncode if completed is not None else None,
                "stdout_excerpt": " ".join(completed.stdout.split())[:2000]
                if completed is not None and completed.stdout
                else None,
                "stderr_excerpt": " ".join(completed.stderr.split())[:2000]
                if completed is not None and completed.stderr
                else None,
                "error_type": type(error).__name__ if error else None,
                "error_message": str(error) if error else None,
            }
            self._observe(
                "codex_cli_call_completed",
                operation=operation,
                model=model,
                cli_path=path,
                ok=error is None,
                elapsed_ms=elapsed_ms,
                elapsed_seconds=round(elapsed_ms / 1000, 3),
                returncode=completed.returncode if completed is not None else None,
                error_code="CODEX_CLI_CALL_FAILED" if error else None,
                error_type=type(error).__name__ if error else None,
                message=str(error)[:1000] if error else None,
            )
            self._captures.write("codex_cli", operation, request_payload, response_payload, {"elapsed_ms": elapsed_ms})
