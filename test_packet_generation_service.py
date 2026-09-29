"""Packet association policy with fake storage and drafting operations."""

import unittest
from contextlib import nullcontext

from job_search.application.packet_generation_service import (
    PacketAlreadyAssociatedError,
    PacketGenerationService,
    PacketJobMissingError,
)


class FakeOperations:
    def __init__(self, job):
        self.current_job = job
        self.saved = []
        self.events = []

    def connection(self):
        return nullcontext(None)

    def job(self, _connection, _job_id):
        return self.current_job

    def generate(self, _job):
        return {"path": "applications/generated"}

    def save_path(self, _connection, job_id, path, timestamp):
        self.saved.append((job_id, path, timestamp))

    def now(self):
        return 123

    def log(self, name, **fields):
        self.events.append((name, fields))


class PacketGenerationServiceTests(unittest.TestCase):
    def test_missing_and_existing_packet_have_distinct_failures(self):
        with self.assertRaises(PacketJobMissingError):
            PacketGenerationService(FakeOperations(None)).generate(7)
        with self.assertRaises(PacketAlreadyAssociatedError):
            PacketGenerationService(FakeOperations({"application_packet_path": "applications/old"})).generate(7)

    def test_success_persists_path_and_emits_operation_event(self):
        operations = FakeOperations({"id": 7, "application_packet_path": None})

        result = PacketGenerationService(operations).generate(7)

        self.assertEqual(result["path"], "applications/generated")
        self.assertEqual(operations.saved, [(7, "applications/generated", 123)])
        self.assertEqual(operations.events[0][0], "application_packet_generated")


if __name__ == "__main__":
    unittest.main()
