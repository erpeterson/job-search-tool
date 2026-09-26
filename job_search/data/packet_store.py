"""Filesystem storage for application packets under ``<workspace>/applications``."""

import shutil
import subprocess
import tempfile
from pathlib import Path

from job_search.domain.errors import ConflictError, DependencyUnavailableError, ExternalServiceError, NotFoundError


class PandocConverter:
    """Converts Markdown files to DOCX with the ``pandoc`` executable."""

    def __init__(self, which=shutil.which, runner=subprocess.run):
        self._which = which
        self._runner = runner

    def to_docx(self, source_path, output_path):
        pandoc_path = self._which("pandoc")
        if not pandoc_path:
            raise DependencyUnavailableError(
                "Pandoc is required to generate packet DOCX deliverables but was not found on PATH.",
                "pandoc_unavailable",
            )
        completed = self._runner(
            [pandoc_path, "--from", "markdown", "--to", "docx", "--output", str(output_path), str(source_path)],
            text=True,
            capture_output=True,
            check=False,
        )
        if completed.returncode != 0:
            raise ExternalServiceError(
                f"Pandoc conversion failed for {source_path.name}; see logs for details.",
                "pandoc_conversion_failed",
                detail=(completed.stderr or completed.stdout or "").strip()[:2000],
            )


class PacketStore:
    def __init__(self, workspace_root, applications_dir, converter):
        self._workspace_root = workspace_root.resolve()
        self._applications_dir = applications_dir
        self._converter = converter

    def relative(self, path):
        return path.resolve().relative_to(self._workspace_root).as_posix()

    @staticmethod
    def markdown_files(packet_dir):
        return [path.name for path in sorted(packet_dir.glob("*.md")) if path.is_file()]

    def list_packet_dirs(self):
        """Return ``(relative_path, name, markdown_files)`` for each packet folder with Markdown."""
        self._applications_dir.mkdir(parents=True, exist_ok=True)
        packets = []
        for path in sorted(self._applications_dir.iterdir()):
            if not path.is_dir():
                continue
            markdown_files = self.markdown_files(path)
            if markdown_files:
                packets.append((self.relative(path), path.name, markdown_files))
        return packets

    def resolve(self, relative_path, error_cls=NotFoundError):
        """Resolve a workspace-relative packet path, rejecting anything outside ``applications/``.

        ``error_cls`` lets callers report user-supplied paths as validation errors
        and stored paths as not-found errors.
        """
        if not relative_path:
            raise error_cls("Application packet path is required.", "packet_path_empty")
        candidate = (self._workspace_root / relative_path).resolve()
        applications_root = self._applications_dir.resolve()
        if candidate != applications_root and applications_root not in candidate.parents:
            raise error_cls("Application packet path must be under applications/.", "packet_path_outside_root")
        if not candidate.is_dir():
            raise error_cls("Application packet folder does not exist.", "packet_folder_missing")
        return candidate

    @staticmethod
    def read_markdown(packet_dir, filename):
        """Read a Markdown file that must live directly inside ``packet_dir``."""
        file_path = (packet_dir / filename).resolve()
        if packet_dir not in file_path.parents or not file_path.is_file():
            raise NotFoundError("Markdown file not found in associated packet.", "packet_markdown_missing")
        return file_path.read_text(encoding="utf-8")

    def publish(self, slug, documents):
        """Write Markdown documents plus DOCX conversions, publishing the folder atomically.

        Returns the final packet directory. Nothing is visible under
        ``applications/`` unless every file was generated successfully.
        """
        packet_dir = self._applications_dir / slug
        if packet_dir.exists():
            raise ConflictError(
                f"Application packet directory already exists: {self.relative(packet_dir)}", "packet_dir_exists"
            )
        self._applications_dir.mkdir(parents=True, exist_ok=True)
        with tempfile.TemporaryDirectory(prefix=".packet-staging-", dir=self._applications_dir) as staging_root:
            staged = Path(staging_root) / slug
            staged.mkdir(parents=True)
            for filename, content in documents.items():
                (staged / filename).write_text(content, encoding="utf-8")
            for filename in documents:
                source = staged / filename
                self._converter.to_docx(source, source.with_suffix(".docx"))
            staged.replace(packet_dir)
        return packet_dir
