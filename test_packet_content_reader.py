"""Unit tests for safe application-packet filesystem reads."""

import tempfile
import unittest
from pathlib import Path

from job_search.data_access.packet_content_reader import FilesystemPacketContentReader
from job_search.data_access.packet_storage import PacketStorage


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

    def test_packet_storage_publishes_only_complete_staged_packet(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            storage = PacketStorage(root, root / "applications")

            def write(packet_dir, _payload):
                packet_dir.mkdir(parents=True)
                (packet_dir / "Resume.md").write_text("resume", encoding="utf-8")
                return ["Resume.md"]

            destination, files = storage.publish("example", {}, write)
            self.assertEqual(files, ["Resume.md"])
            self.assertTrue((destination / "Resume.md").is_file())
            with self.assertRaises(FileExistsError):
                storage.publish("example", {}, write)

    def test_failed_publish_leaves_no_destination_or_staging_directory(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            storage = PacketStorage(root, root / "applications")

            def fail_after_partial_write(packet_dir, _payload):
                packet_dir.mkdir(parents=True)
                (packet_dir / "Resume.md").write_text("partial", encoding="utf-8")
                raise OSError("simulated writer failure")

            with self.assertRaisesRegex(OSError, "simulated writer failure"):
                storage.publish("example", {}, fail_after_partial_write)

            self.assertFalse((root / "applications" / "example").exists(), "Partial packet must not be published")
            self.assertEqual(list((root / "applications").iterdir()), [], "Staging files must be removed")
