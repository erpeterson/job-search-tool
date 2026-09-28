"""Captured Codex transport tests using a fake CLI boundary."""

import tempfile
import unittest
from pathlib import Path

from job_search.data_access.capture_store import CaptureStore
from job_search.data_access.codex_cli import CodexCliResult
from job_search.data_access.codex_json_gateway import CodexCliError, CodexJsonGateway


class FakeCli:
    def __init__(self, returncode=0):
        self.calls = []
        self.returncode = returncode

    def execute(self, path, model, instruction, timeout):
        self.calls.append((path, model, instruction, timeout))
        return CodexCliResult('{"score": 88}', "test-model", self.returncode, "", "")


class CodexJsonGatewayTests(unittest.TestCase):
    def test_success_replays_capture_without_invoking_fake_cli_twice(self):
        with tempfile.TemporaryDirectory() as directory:
            events = []
            captures = CaptureStore(
                Path(directory),
                lambda: True,
                lambda: True,
                lambda value, **_: value,
                lambda name, **fields: events.append((name, fields)),
            )
            cli = FakeCli()
            gateway = CodexJsonGateway(
                cli, captures, lambda name, **fields: events.append((name, fields)), lambda: "codex", 30
            )

            first = gateway.complete("test-model", {"task": "score"}, "score_job", return_metadata=True)
            second = gateway.complete("test-model", {"task": "score"}, "score_job", return_metadata=True)

            self.assertEqual(first, ('{"score": 88}', "test-model"))
            self.assertEqual(second, first)
            self.assertEqual(len(cli.calls), 1, "Capture replay must bypass the CLI")
            self.assertIn("codex_cli_call_completed", [name for name, _ in events])

    def test_nonzero_exit_emits_failure_code_and_raises_controlled_error(self):
        with tempfile.TemporaryDirectory() as directory:
            events = []
            captures = CaptureStore(
                Path(directory), lambda: False, lambda: False, lambda value, **_: value, lambda *_args, **_kwargs: None
            )
            gateway = CodexJsonGateway(
                FakeCli(returncode=7),
                captures,
                lambda name, **fields: events.append((name, fields)),
                lambda: "codex",
                30,
            )

            with self.assertRaisesRegex(CodexCliError, "exited with code 7"):
                gateway.complete("test-model", {"task": "score"}, "score_job")

            self.assertEqual(events[-1][1]["error_code"], "CODEX_CLI_CALL_FAILED")
