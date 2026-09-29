"""Packet drafting tests with fake model, source, and publication ports."""

import unittest
from datetime import date
from pathlib import Path

from job_search.application.packet_draft_service import PacketDraftService


class FakeTelemetry:
    def __init__(self):
        self.events = []

    def event(self, name, **fields):
        self.events.append((name, fields))


class PacketDraftServiceTests(unittest.TestCase):
    def setUp(self):
        self.telemetry = FakeTelemetry()
        self.model_calls = []
        self.published = []
        self.outputs = []

    def make_service(self, publish=None):
        def complete(model, prompt, operation, *, force_refresh, return_metadata):
            self.model_calls.append((model, prompt, operation, force_refresh, return_metadata))
            return self.outputs.pop(0)

        def publish_packet(name, payload):
            self.published.append((name, payload))
            if publish is not None:
                return publish(name, payload)
            return Path("/fake/applications") / name, ["Resume.md"]

        return PacketDraftService(
            career_manual=lambda: "# Intro\n# Downstream Artifact Rules\nUse evidence.\n# Open Questions\nNone",
            master_resume=lambda: "# Master Resume",
            cli_available=lambda: True,
            cli_path=lambda: "codex",
            model=lambda: "requested-model",
            complete=complete,
            parse=lambda output: __import__("json").loads(output),
            publish=publish_packet,
            relative_path=lambda path: f"applications/{path.name}",
            today=lambda: date(2026, 9, 28),
            telemetry=self.telemetry,
        )

    @staticmethod
    def job():
        return {"id": 7, "company": "ExampleCo", "title": "Architect", "url": "https://example.test/role"}

    def test_retries_missing_attribution_then_publishes_validated_packet(self):
        self.outputs = [
            ('{"job_brief_markdown":"Brief","resume_markdown":"Resume","cover_letter_markdown":"Letter"}', "model-a"),
            (
                '{"job_brief_markdown":"Brief model-a","resume_markdown":"Resume model-a",'
                '"cover_letter_markdown":"Letter model-a"}',
                "model-a",
            ),
        ]

        result = self.make_service().generate(self.job())

        self.assertEqual(result["path"], "applications/2026-09-exampleco-architect-7")
        self.assertEqual(result["markdown_files"], ["Resume.md"])
        self.assertEqual(len(self.model_calls), 2, "Missing attribution should trigger one retry")
        self.assertIn("Use evidence.", self.model_calls[0][1]["context"]["application_packet_rules"])
        self.assertIn("codex_generation_metadata", self.model_calls[1][1]["context"])
        self.assertEqual(self.published[0][1]["resume_markdown"], "Resume model-a\n")
        self.assertTrue(self.telemetry.events[-1][1]["ok"])

    def test_attribution_failure_never_publishes_and_emits_failure_event(self):
        self.outputs = [
            ('{"job_brief_markdown":"Brief","resume_markdown":"Resume","cover_letter_markdown":"Letter"}', "model-a"),
            ('{"job_brief_markdown":"Brief","resume_markdown":"Resume","cover_letter_markdown":"Letter"}', "model-a"),
        ]

        with self.assertRaisesRegex(RuntimeError, "attribution"):
            self.make_service().generate(self.job())

        self.assertEqual(self.published, [], "Unattributed output must not reach storage")
        self.assertEqual(self.telemetry.events[-1][1]["error_code"], "APPLICATION_PACKET_GENERATION_FAILED")
        self.assertEqual(self.telemetry.events[-1][1]["component"], "business.packet_draft")
        self.assertEqual(self.telemetry.events[-1][1]["operation"], "generate_application_packet")
        self.assertEqual(self.telemetry.events[-1][1]["job_id"], 7)
        self.assertEqual(self.telemetry.events[-1][1]["cause"], "RuntimeError")

    def test_publish_failure_is_observed_without_partial_success(self):
        self.outputs = [
            (
                '{"job_brief_markdown":"Brief model-a","resume_markdown":"Resume model-a",'
                '"cover_letter_markdown":"Letter model-a"}',
                "model-a",
            )
        ]

        def fail_publish(_name, _payload):
            raise OSError("simulated publish failure")

        with self.assertRaisesRegex(OSError, "simulated publish failure"):
            self.make_service(publish=fail_publish).generate(self.job())

        self.assertEqual(self.telemetry.events[-1][1]["error_type"], "OSError")
        self.assertFalse(self.telemetry.events[-1][1]["ok"])
