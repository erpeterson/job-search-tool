"""Filesystem adapter tests for optional source material and startup paths."""

import tempfile
import unittest
from pathlib import Path

from job_search.data_access.initialization_adapter import SqliteInitializationAdapter
from job_search.data_access.source_documents import SourceDocuments


class SourceDocumentTests(unittest.TestCase):
    def test_optional_sources_are_empty_when_absent_and_read_when_present(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            sources = SourceDocuments(root / "guidance.md", root / "manual.md", root / "resume.md")
            self.assertEqual(sources.guidance(), "", "Absent optional guidance should not fail startup")
            self.assertEqual(sources.career_manual(), "", "Absent optional manual should be empty")
            self.assertEqual(sources.master_resume(), "", "Absent optional resume should be empty")

            (root / "manual.md").write_text("# Rules", encoding="utf-8")
            self.assertEqual(sources.career_manual(), "# Rules", "Present files should be read as UTF-8")

    def test_startup_adapter_creates_database_parent_directory(self):
        with tempfile.TemporaryDirectory() as directory:
            database_path = Path(directory) / "nested" / "jobs.sqlite3"
            adapter = SqliteInitializationAdapter(database_path, lambda: 1)

            with adapter.connection() as connection:
                connection.execute("CREATE TABLE test_marker (id INTEGER)")

            self.assertTrue(database_path.is_file(), "The storage adapter should prepare its own directory")
