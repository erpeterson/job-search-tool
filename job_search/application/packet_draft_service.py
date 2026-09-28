"""Application decisions for drafting and atomically publishing a packet."""

from __future__ import annotations

import re
import time
from collections.abc import Callable, Mapping, Sequence
from datetime import date
from pathlib import Path
from typing import Any

from job_search.application.contracts import Telemetry


def validate_packet_payload(payload: Any) -> dict[str, str]:
    if not isinstance(payload, dict):
        raise ValueError("Codex packet response must be a JSON object.")
    required = ("job_brief_markdown", "resume_markdown", "cover_letter_markdown")
    missing = [field for field in required if not isinstance(payload.get(field), str) or not payload[field].strip()]
    if missing:
        raise ValueError(f"Codex packet response is missing required Markdown fields: {', '.join(missing)}.")
    return {field: payload[field].strip() + "\n" for field in required}


def packet_slug(job: Mapping[str, Any], today: date) -> str:
    def component(value: Any, fallback: str, limit: int) -> str:
        return re.sub(r"[^a-z0-9]+", "-", str(value or fallback).lower()).strip("-")[:limit]

    return (
        f"{today:%Y-%m}-{component(job.get('company'), 'unknown-company', 60)}-"
        f"{component(job.get('title'), 'unknown-role', 90)}-"
        f"{component(job.get('source_job_id') or job.get('id'), 'job', 40)}"
    )


class PacketDraftService:
    def __init__(
        self,
        *,
        career_manual: Callable[[], str],
        master_resume: Callable[[], str],
        cli_available: Callable[[], bool],
        cli_path: Callable[[], str],
        model: Callable[[], str],
        complete: Callable[..., tuple[str, str]],
        parse: Callable[[str], Mapping[str, Any]],
        publish: Callable[[str, Mapping[str, str]], tuple[Path, Sequence[str]]],
        relative_path: Callable[[Path], str],
        today: Callable[[], date],
        telemetry: Telemetry,
    ) -> None:
        self._career_manual = career_manual
        self._master_resume = master_resume
        self._cli_available = cli_available
        self._cli_path = cli_path
        self._model = model
        self._complete = complete
        self._parse = parse
        self._publish = publish
        self._relative_path = relative_path
        self._today = today
        self._telemetry = telemetry

    def generate(self, job: Mapping[str, Any]) -> dict[str, Any]:
        if not job.get("url"):
            raise ValueError("Job does not have a URL for Codex packet generation.")
        if not self._cli_available():
            raise RuntimeError(
                f"Codex CLI is unavailable at {self._cli_path()!r}. Set CODEX_CLI_PATH or install Codex CLI."
            )
        today = self._today()
        context = self._context(job, today)
        prompt = self._prompt(context)
        started = time.monotonic()
        model = self._model()
        error = None
        try:
            output, model = self._complete(
                model, prompt, "generate_application_packet", force_refresh=True, return_metadata=True
            )
            if not model:
                raise RuntimeError("Codex CLI did not report the model used to generate the application packet.")
            payload = validate_packet_payload(self._parse(output))
            if not all(model in content for content in payload.values()):
                context["codex_generation_metadata"] = {"generation_date": today.isoformat(), "model": model}
                output, retry_model = self._complete(
                    model, prompt, "generate_application_packet", force_refresh=True, return_metadata=True
                )
                if retry_model != model:
                    raise RuntimeError("Codex CLI used a different model while regenerating packet attribution.")
                payload = validate_packet_payload(self._parse(output))
                if not all(model in content for content in payload.values()):
                    raise RuntimeError("Codex did not include the invoked model in every packet attribution.")
            packet_dir, files = self._publish(packet_slug(job, today), payload)
            return {
                "path": self._relative_path(packet_dir),
                "name": packet_dir.name,
                "markdown_files": list(files),
                "codex_output": output,
            }
        except Exception as exc:
            error = exc
            raise
        finally:
            self._telemetry.event(
                "codex_cli_application_packet",
                job_id=job.get("id"),
                url=job.get("url"),
                cli_path=self._cli_path(),
                ok=error is None,
                elapsed_ms=int((time.monotonic() - started) * 1000),
                model=model,
                error_code="APPLICATION_PACKET_GENERATION_FAILED" if error else None,
                error_type=type(error).__name__ if error else None,
                message=str(error)[:1000] if error else None,
            )

    def _context(self, job: Mapping[str, Any], today: date) -> dict[str, Any]:
        manual = self._career_manual()
        start = manual.find("# Downstream Artifact Rules")
        end = manual.find("# Open Questions", start)
        rules = manual[start : end if end >= 0 else None].strip() if start >= 0 else ""
        return {
            "packet_creation_date": today.isoformat(),
            "job": {
                key: job.get(key) for key in ("id", "company", "title", "location", "url", "pipeline", "source_board")
            }
            | {
                "posting_text": job.get("posting_text")
                or "No posting text was captured. Do not invent requirements beyond the role title and metadata."
            },
            "application_packet_rules": rules,
            "master_resume": self._master_resume(),
        }

    @staticmethod
    def _prompt(context: dict[str, Any]) -> dict[str, Any]:
        return {
            "task": "Generate exactly one application packet as JSON. Do not access the network or filesystem; use only the supplied context.",
            "workflow": [
                "First formulate the job brief, including high-signal requirements, tailoring strategy, achievement map, and likely objections.",
                "Then draft one tailored resume and one cover letter using only source-backed evidence from the supplied master resume and rules.",
                "Finally append an objection remediation outcome to the job brief. Perform this remediation cycle once only.",
            ],
            "output_contract": {
                "job_brief_markdown": "Complete Job-Brief.md content. Include source trace naming the supplied Career Manual, Master Resume, and local tracked job.",
                "resume_markdown": "Complete Resume.md content. One employer-facing, ATS-readable tailored resume.",
                "cover_letter_markdown": "Complete Cover-Letter.md content. Direct, practical, evidence-oriented, and low hype.",
            },
            "constraints": [
                "Return only one valid JSON object with exactly the three output_contract keys.",
                "Do not use Markdown fences around the JSON.",
                "Do not create files, propose filenames, or discuss this instruction.",
                "Do not invent accomplishments, metrics, technologies, dates, or domain experience.",
                "Do not generate separate ATS resume artifacts.",
            ],
            "context": context,
        }
