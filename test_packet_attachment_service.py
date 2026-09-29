import tempfile
import unittest
from pathlib import Path

from job_search.application.packet_attachment_service import PacketAttachmentService
from job_search.data_access.packet_storage import PacketStorage


class FakeJobs:
    def __init__(self):
        self.job = {"id": 3}
        self.attached = []

    def get_job(self, _job_id):
        return self.job

    def attach_packet(self, job_id, path, updated_at):
        self.attached.append((job_id, path, updated_at))
        self.job = {**self.job, "application_packet_path": path}
        return True


class PacketAttachmentServiceTests(unittest.TestCase):
    def test_attaches_only_existing_packet_folder_with_fake_repository(self):
        with tempfile.TemporaryDirectory() as temporary_directory:
            root = Path(temporary_directory)
            packet = root / "applications" / "example"
            packet.mkdir(parents=True)
            jobs = FakeJobs()
            events = []
            service = PacketAttachmentService(
                jobs,
                PacketStorage(root, root / "applications").packet_relative_path,
                lambda: 50,
                lambda name, **fields: events.append((name, fields)),
            )

            result = service.attach(3, "applications/example")

        self.assertEqual(result["path"], "applications/example")
        self.assertEqual(jobs.attached, [(3, "applications/example", 50)])
        self.assertEqual(events[0][0], "application_packet_attached")

    def test_rejects_path_outside_application_directory(self):
        with tempfile.TemporaryDirectory() as temporary_directory:
            root = Path(temporary_directory)
            (root / "applications").mkdir()
            storage = PacketStorage(root, root / "applications")

            with self.assertRaisesRegex(ValueError, "under applications"):
                storage.packet_relative_path("../outside")


if __name__ == "__main__":
    unittest.main()
