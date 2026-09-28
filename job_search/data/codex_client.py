"""Codex CLI adapter: runs a read-only, JSON-only ``codex exec`` subprocess."""

import json
import re
import subprocess
import tempfile
import time
from dataclasses import dataclass
from pathlib import Path

from job_search.domain.errors import CodexCliError, ExternalServiceError
from job_search.domain.text import clean_text
from job_search.observability import log_event, record_exception

ADAPTER_VERSION = 2
_MODEL_PATTERN = re.compile(r"\bmodel:\s*([^\s]+)", re.IGNORECASE)


@dataclass(frozen=True)
class CodexResult:
    output_text: str
    effective_model: str


def extract_codex_reported_model(output):
    match = _MODEL_PATTERN.search(output or "")
    return match.group(1) if match else ""


def parse_model_json(output_text):
    """Parse a JSON object from model output, tolerating fences and surrounding prose."""
    if not output_text:
        raise json.JSONDecodeError("empty response", "", 0)
    cleaned = output_text.strip()
    if cleaned.startswith("```"):
        cleaned = re.sub(r"^```(?:json)?\s*", "", cleaned, flags=re.IGNORECASE)
        cleaned = re.sub(r"\s*```$", "", cleaned)
    start = cleaned.find("{")
    end = cleaned.rfind("}")
    if start >= 0 and end > start:
        cleaned = cleaned[start : end + 1]
    return json.loads(cleaned)


def _codex_succeeded(response):
    return response.get("returncode") == 0 and not response.get("error_type")


class CodexClient:
    def __init__(self, runtime, captures, workspace_root, timeout_seconds, runner=subprocess.run):
        self._runtime = runtime
        self._captures = captures
        self._workspace_root = workspace_root
        self._timeout_seconds = timeout_seconds
        self._runner = runner

    def call_json(self, model, prompt, operation, force_refresh=False):
        """Run Codex with a JSON prompt and return a ``CodexResult``.

        Responses are captured for replay; a cache hit does not invoke Codex.
        """
        cli_path = self._runtime.codex_cli_path()
        request_payload = {"adapter_version": ADAPTER_VERSION, "cli_path": cli_path, "model": model, "prompt": prompt}
        cached = self._captures.read(
            "codex_cli", operation, request_payload, force_refresh=force_refresh, is_success=_codex_succeeded
        )
        if cached:
            response = cached["response"]
            return CodexResult(response.get("output_text", ""), response.get("effective_model", ""))

        started = time.monotonic()
        log_event(
            "codex_cli_call_started",
            operation=operation,
            model=model,
            cli_path=cli_path,
            timeout_seconds=self._timeout_seconds,
        )
        completed = None
        error = None
        output_text = ""
        effective_model = ""
        instruction = (
            "You are a JSON-only engine for a local job-search app.\n"
            "Return only one valid JSON object. "
            "Do not include markdown fences, prose, or explanations outside JSON.\n\n"
            f"{json.dumps(prompt, indent=2, sort_keys=True, default=str)}\n"
        )
        try:
            with tempfile.TemporaryDirectory(prefix="job-search-codex-") as tmpdir:
                output_path = Path(tmpdir) / "last-message.txt"
                command = [cli_path, "exec", "-C", str(self._workspace_root), "--sandbox", "read-only"]
                if model:
                    command[2:2] = ["-m", model]
                command += ["-o", str(output_path), "-"]
                completed = self._runner(
                    command,
                    input=instruction,
                    text=True,
                    capture_output=True,
                    timeout=self._timeout_seconds,
                    check=False,
                )
                if output_path.exists():
                    output_text = output_path.read_text(encoding="utf-8").strip()
                if not output_text:
                    output_text = (completed.stdout or "").strip()
                effective_model = extract_codex_reported_model(f"{completed.stderr or ''}\n{completed.stdout or ''}")
                if completed.returncode != 0:
                    raise CodexCliError(operation, completed.returncode)
            return CodexResult(output_text, effective_model)
        except subprocess.TimeoutExpired as exc:
            error = exc
            record_exception("codex_cli_timeout", "data.codex_client", operation, exc, cli_path=cli_path, model=model)
            raise ExternalServiceError(
                f"Codex CLI {operation} timed out after {self._timeout_seconds} seconds.", "codex_cli_timeout"
            ) from exc
        except OSError as exc:
            error = exc
            record_exception("codex_cli_launch_failed", "data.codex_client", operation, exc, cli_path=cli_path)
            raise ExternalServiceError(
                "Codex CLI could not be started. Check CODEX_CLI_PATH.",
                "codex_cli_launch_failed",
                detail=f"cli_path={cli_path!r}",
            ) from exc
        except CodexCliError as exc:
            error = exc
            record_exception(
                "codex_cli_nonzero_exit",
                "data.codex_client",
                operation,
                exc,
                cli_path=cli_path,
                model=model,
                returncode=exc.returncode,
            )
            raise
        finally:
            elapsed_ms = int((time.monotonic() - started) * 1000)
            returncode = completed.returncode if completed is not None else None
            log_event(
                "codex_cli_call_completed",
                operation=operation,
                model=model,
                cli_path=cli_path,
                ok=error is None,
                elapsed_ms=elapsed_ms,
                elapsed_seconds=round(elapsed_ms / 1000, 3),
                returncode=returncode,
                error_type=type(error).__name__ if error else None,
                message=str(error)[:1000] if error else None,
            )
            response_payload = {
                "output_text": output_text,
                "effective_model": effective_model,
                "returncode": returncode,
                "stdout_excerpt": clean_text(completed.stdout)[:2000]
                if completed is not None and completed.stdout
                else None,
                "stderr_excerpt": clean_text(completed.stderr)[:2000]
                if completed is not None and completed.stderr
                else None,
                "error_type": type(error).__name__ if error else None,
                "error_message": str(error) if error else None,
            }
            self._captures.write(
                "codex_cli",
                operation,
                request_payload,
                response_payload,
                {"elapsed_ms": elapsed_ms},
                succeeded=_codex_succeeded(response_payload),
            )
