"""Filesystem ownership for optional resume and packet source material."""

from dataclasses import dataclass
from pathlib import Path

from job_search.data_access.text_file_reader import read_optional_text


@dataclass(frozen=True)
class SourceDocuments:
    guidance_path: Path
    career_manual_path: Path
    master_resume_path: Path

    def guidance(self) -> str:
        return read_optional_text(self.guidance_path)

    def career_manual(self) -> str:
        return read_optional_text(self.career_manual_path)

    def master_resume(self) -> str:
        return read_optional_text(self.master_resume_path)
