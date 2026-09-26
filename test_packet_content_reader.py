"""Unit tests for safe application-packet filesystem reads."""

import tempfile
import unittest
from pathlib import Path

from job_search.data_access.packet_content_reader import FilesystemPacketContentReader


class PacketContentReaderTests(unittest.TestCase):
    def test_reads_only_markdown_from_an_associated_packet_directory(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            packet = root / "applications" / "sample"
            packet.mkdir(parents=True)
            (packet / "Resume.md").write_text("# Resume", encoding="utf-8")
            reader = FilesystemPacketContentReader(root, root / "applications")

            result = reader.read("applications/sample", "Resume.md")

            self.assertEqual(result["path"], "applications/sample")
            self.assertEqual(result["content"], "# Resume")
            self.assertEqual(result["markdown_files"], ["Resume.md"])

    def test_rejects_traversal_and_missing_files(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            (root / "applications" / "sample").mkdir(parents=True)
            reader = FilesystemPacketContentReader(root, root / "applications")

            with self.assertRaisesRegex(ValueError, "Select a Markdown"):
                reader.read("applications/sample", "../secret.md")
            with self.assertRaisesRegex(ValueError, "under applications"):
                reader.read("../outside", "Resume.md")
            with self.assertRaisesRegex(FileNotFoundError, "Markdown file not found"):
                reader.read("applications/sample", "Resume.md")
