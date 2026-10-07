"""Read-only access to career source documents in the workspace."""

import textwrap


class CareerDocuments:
    def __init__(self, career_manual_path, guidance_path, master_resume_path):
        self._career_manual_path = career_manual_path
        self._guidance_path = guidance_path
        self._master_resume_path = master_resume_path

    @staticmethod
    def _read_optional(path):
        return path.read_text(encoding="utf-8") if path.exists() else ""

    def career_context(self):
        manual = self._read_optional(self._career_manual_path)
        guidance = self._read_optional(self._guidance_path)
        return textwrap.shorten(manual, width=9000, placeholder="\n[manual truncated]\n") + "\n\n" + guidance

    def application_packet_rules(self):
        manual = self._read_optional(self._career_manual_path)
        start = manual.find("# Downstream Artifact Rules")
        if start < 0:
            return ""
        # Interview stories are not packet rules; fall back to the next top-level section.
        end = manual.find("## Interview Stories", start)
        if end < 0:
            end = manual.find("# Open Questions", start)
        return manual[start : end if end >= 0 else None].strip()

    def master_resume(self):
        return self._read_optional(self._master_resume_path)
