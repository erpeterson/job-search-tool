"""Application packet generation, association, and retrieval."""

import json
from dataclasses import dataclass
from datetime import UTC, datetime

from job_search.domain.clock import now
from job_search.domain.errors import (
    ConflictError,
    DependencyUnavailableError,
    ExternalServiceError,
    NotFoundError,
    ValidationError,
)
from job_search.domain.text import clean_text, slugify
from job_search.observability import log_event, operation, record_exception, traced

PACKET_FIELDS = {
    "job_brief_markdown": "Job-Brief.md",
    "resume_markdown": "Resume.md",
    "cover_letter_markdown": "Cover-Letter.md",
}

PACKET_TASK = (
    "Generate exactly one application packet as JSON. Do not access the network or filesystem; "
    "use only the supplied context."
)
PACKET_WORKFLOW = [
    "First formulate the job brief, including high-signal requirements, tailoring strategy, achievement map, and "
    "likely objections.",
    "Then draft one tailored resume and one cover letter using only source-backed evidence from the supplied master "
    "resume and rules.",
    "Finally append an objection remediation outcome to the job brief. Perform this remediation cycle once only.",
]
PACKET_OUTPUT_CONTRACT = {
    "job_brief_markdown": "Complete Job-Brief.md content. Include source trace naming the supplied Career Manual, "
    "Master Resume, and local tracked job.",
    "resume_markdown": "Complete Resume.md content. One employer-facing, ATS-readable tailored resume.",
    "cover_letter_markdown": "Complete Cover-Letter.md content. Direct, practical, evidence-oriented, and low hype.",
}
PACKET_CONSTRAINTS = [
    "Return only one valid JSON object with exactly the three output_contract keys.",
    "Do not use Markdown fences around the JSON.",
    "Do not create files, propose filenames, or discuss this instruction.",
    "Do not invent accomplishments, metrics, technologies, dates, or domain experience.",
    "Do not generate separate ATS resume artifacts.",
]
MISSING_POSTING_TEXT = "No posting text was captured. Do not invent requirements beyond the role title and metadata."


@dataclass(frozen=True)
class PacketDocument:
    job: dict
    packet_path: str
    filename: str
    content: str
    markdown_files: list


def application_packet_slug(job, today=None):
    today = today or datetime.now()
    company = slugify(job.get("company") or "unknown-company", 60)
    title = slugify(job.get("title") or "unknown-role", 90)
    identifier = slugify(job.get("source_job_id") or str(job.get("id") or "job"), 40)
    return f"{today.strftime('%Y-%m')}-{company}-{title}-{identifier}"


def validate_packet_payload(payload):
    if not isinstance(payload, dict):
        raise ExternalServiceError("Codex packet response must be a JSON object.", "packet_payload_not_object")
    missing = [
        field for field in PACKET_FIELDS if not isinstance(payload.get(field), str) or not payload[field].strip()
    ]
    if missing:
        raise ExternalServiceError(
            f"Codex packet response is missing required Markdown fields: {', '.join(missing)}.",
            "packet_payload_missing_fields",
        )
    return {field: payload[field].strip() + "\n" for field in PACKET_FIELDS}


def has_model_attribution(payload, model):
    return all(model in content for content in payload.values())


def validate_markdown_filename(filename):
    if not filename.endswith(".md") or "/" in filename or "\\" in filename:
        raise ValidationError("Select a Markdown file in the associated packet.", "packet_filename_invalid")


class PacketService:
    def __init__(self, db, runtime, codex, documents, store, parse_json):
        self._db = db
        self._runtime = runtime
        self._codex = codex
        self._documents = documents
        self._store = store
        self._parse_json = parse_json

    def ensure_available(self):
        if not self._runtime.codex_cli_available():
            raise DependencyUnavailableError(
                "Codex CLI is unavailable. Set CODEX_CLI_PATH or install Codex CLI.",
                "packet_codex_cli_unavailable",
                detail=f"cli_path={self._runtime.codex_cli_path()!r}",
            )

    def list_packets(self):
        with self._db.unit_of_work() as uow:
            associated_by_path = {row["application_packet_path"]: row for row in uow.jobs.packet_associations()}
        packets = []
        for relative, name, markdown_files in self._store.list_packet_dirs():
            associated_job = associated_by_path.get(relative)
            packets.append(
                {
                    "path": relative,
                    "name": name,
                    "markdown_files": markdown_files,
                    "associated_job": associated_job,
                    "unassociated": associated_job is None,
                }
            )
        return packets

    def _context(self, job):
        return {
            "packet_creation_date": datetime.now().date().isoformat(),
            "job": {
                "id": job.get("id"),
                "company": job.get("company"),
                "title": job.get("title"),
                "location": job.get("location"),
                "url": job.get("url"),
                "pipeline": job.get("pipeline"),
                "source_board": job.get("source_board"),
                "posting_text": job.get("posting_text") or MISSING_POSTING_TEXT,
            },
            "application_packet_rules": self._documents.application_packet_rules(),
            "master_resume": self._documents.master_resume(),
        }

    def _draft(self, model, prompt):
        result = self._codex.call_json(model, prompt, "generate_application_packet", force_refresh=True)
        try:
            payload = validate_packet_payload(self._parse_json(result.output_text))
        except json.JSONDecodeError as exc:
            record_exception(
                "packet_payload_invalid_json",
                "domain.packets",
                "draft",
                exc,
                response_excerpt=(result.output_text or "")[:500],
            )
            raise ExternalServiceError(
                "Codex packet response was not valid JSON.", "packet_payload_invalid_json"
            ) from exc
        return payload, result.effective_model, result.output_text

    def _generate_documents(self, job):
        """Draft packet Markdown with Codex, retrying once to ensure exact model attribution.

        Returns ``(documents_by_filename, raw_codex_output)``.
        """
        context = self._context(job)
        prompt = {
            "task": PACKET_TASK,
            "workflow": PACKET_WORKFLOW,
            "output_contract": PACKET_OUTPUT_CONTRACT,
            "constraints": PACKET_CONSTRAINTS,
            "context": context,
        }
        payload, model, output_text = self._draft(self._runtime.codex_model(), prompt)
        if not model:
            raise ExternalServiceError(
                "Codex CLI did not report the model used to generate the application packet.", "packet_model_unreported"
            )
        if not has_model_attribution(payload, model):
            context["codex_generation_metadata"] = {
                "generation_date": datetime.now(UTC).date().isoformat(),
                "model": model,
            }
            payload, retry_model, output_text = self._draft(model, prompt)
            if retry_model != model:
                raise ExternalServiceError(
                    "Codex CLI used a different model while regenerating the application packet attribution.",
                    "packet_model_changed",
                )
            if not has_model_attribution(payload, model):
                raise ExternalServiceError(
                    "Codex did not include the exact invoked model in every application-packet attribution.",
                    "packet_model_attribution_missing",
                )
        return {PACKET_FIELDS[field]: content for field, content in payload.items()}, output_text

    def check_can_generate(self, job_id):
        """Raise if a packet cannot be generated for ``job_id``; return the job otherwise."""
        with self._db.unit_of_work() as uow:
            job = uow.jobs.get(job_id)
        if not job:
            raise NotFoundError("Job not found", "packet_job_not_found")
        if job.get("application_packet_path"):
            raise ConflictError("This job already has an associated application packet.", "packet_already_associated")
        if not job.get("url"):
            raise ValidationError("Job does not have a URL for Codex packet generation.", "packet_job_missing_url")
        self.ensure_available()
        return job

    def create_packet(self, job_id, skip_if_associated=False):
        """Generate and associate a packet. Returns the packet dict, or None when skipped."""
        if skip_if_associated:
            with self._db.unit_of_work() as uow:
                existing = uow.jobs.get(job_id)
            if existing and existing.get("application_packet_path"):
                return None
        job = self.check_can_generate(job_id)
        with operation("application_packet_generation", "domain.packets", job_id=job_id, url=job.get("url")):
            documents, output_text = self._generate_documents(job)
            packet_dir = self._store.publish(application_packet_slug(job), documents)
            relative = self._store.relative(packet_dir)
            with self._db.unit_of_work() as uow:
                uow.jobs.set_application_packet_path(job_id, relative, now())
        log_event("application_packet_generated", job_id=job_id, path=relative, generator="codex_cli")
        return {
            "path": relative,
            "name": packet_dir.name,
            "markdown_files": list(documents),
            "codex_output": output_text,
        }

    @traced("application_packet_attach", "domain.packets", id_arg="job_id")
    def attach(self, job_id, packet_path):
        packet_path = clean_text(packet_path)
        with self._db.unit_of_work() as uow:
            if not uow.jobs.exists(job_id):
                raise NotFoundError("Job not found", "packet_attach_job_not_found")
            packet_dir = self._store.resolve(packet_path, error_cls=ValidationError)
            relative = self._store.relative(packet_dir)
            uow.jobs.set_application_packet_path(job_id, relative, now())
        log_event("application_packet_attached", job_id=job_id, path=relative)

    def read_document(self, job_id, filename):
        validate_markdown_filename(filename)
        with self._db.unit_of_work() as uow:
            job = uow.jobs.get(job_id)
        if not job:
            raise NotFoundError("Job not found", "packet_read_job_not_found")
        if not job.get("application_packet_path"):
            raise NotFoundError("Job does not have an associated application packet.", "packet_not_associated")
        packet_dir = self._store.resolve(job["application_packet_path"])
        return PacketDocument(
            job=job,
            packet_path=self._store.relative(packet_dir),
            filename=filename,
            content=self._store.read_markdown(packet_dir, filename),
            markdown_files=self._store.markdown_files(packet_dir),
        )
