"""Unit tests for managed-process application entry points."""

import unittest

from job_search.application.managed_processes import process_background_task, run_scheduled_search


class ManagedProcessEntryPointTests(unittest.TestCase):
    def test_dispatches_claim_without_framework_dependency(self):
        class Processor:
            def process(self, claim):
                return "complete", f"processed {claim['job_id']}"

        self.assertEqual(process_background_task(Processor(), {"job_id": 7}), ("complete", "processed 7"))

    def test_scheduled_search_uses_fixed_trigger_and_refresh_flags(self):
        calls = []

        class Runner:
            def run(self, **kwargs):
                calls.append(kwargs)
                return {"status": "complete"}

        self.assertEqual(run_scheduled_search(Runner()), {"status": "complete"})
        self.assertEqual(calls, [{"trigger": "scheduled", "force_refresh": True}])
