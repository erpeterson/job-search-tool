"""Score orchestration tests with a fake model port and no CLI process."""

import json
import unittest

from job_search.application.job_score_service import JobScoreService
from job_search.application.job_scoring_policy import (
    ORACLE_IC6_LEVEL_REFERENCE,
    PIPELINES,
    RUBRIC_FIELDS,
    JobScoringPolicy,
)


class JobScoreServiceTests(unittest.TestCase):
    def setUp(self):
        self.calls = []
        self.enabled = True
        self.available = True

        def complete(model, prompt, operation, *, force_refresh):
            self.calls.append((model, prompt, operation, force_refresh))
            return json.dumps({"total_score": 88, "pipeline": "Executive IC", "scorecard": {}})

        self.complete = complete

    def service(self):
        return JobScoreService(
            JobScoringPolicy(
                pipelines=PIPELINES, rubric_fields=RUBRIC_FIELDS, level_reference=ORACLE_IC6_LEVEL_REFERENCE
            ),
            enabled=lambda: self.enabled,
            available=lambda: self.available,
            cli_path=lambda: "codex",
            model=lambda _connection: "test-model",
            career_manual=lambda: "Career rules",
            guidance=lambda: "Guidance",
            examples=lambda _connection: [
                {
                    "company": "ExampleCo",
                    "title": "Architect",
                    "pipeline": "Executive IC",
                    "gpt_score": 70,
                    "user_score": 85,
                    "user_rationale": "Strategic scope",
                    "posting_text": "Architecture responsibilities",
                }
            ],
            complete=self.complete,
        )

    def test_success_builds_prompt_from_injected_sources_and_model_port(self):
        score = self.service().score(None, {"company": "ExampleCo", "title": "Architect"}, force_refresh=True)

        self.assertEqual(score["total_score"], 88)
        model, prompt, operation, force_refresh = self.calls[0]
        self.assertEqual((model, operation, force_refresh), ("test-model", "score_job", True))
        self.assertIn("Career rules", prompt["career_context"])
        self.assertEqual(prompt["calibration_examples"][0]["user_score"], 85)

    def test_unavailable_and_invalid_output_fail_without_leaking_model_text(self):
        self.available = False
        with self.assertRaisesRegex(RuntimeError, "unavailable"):
            self.service().score(None, {"title": "Architect"})
        self.assertEqual(self.calls, [], "Unavailable CLI must not invoke the model port")

        self.available = True
        self.complete = lambda *_args, **_kwargs: "PRIVATE-MODEL-OUTPUT"
        with self.assertRaises(RuntimeError) as raised:
            self.service().score(None, {"title": "Architect"})
        self.assertNotIn("PRIVATE-MODEL-OUTPUT", str(raised.exception))

    def test_discovery_scoring_maps_source_fields_before_invocation(self):
        self.service().score_discovery(
            None,
            {"board": "indeed", "company": "ExampleCo", "title": "Architect", "snippet": "Architecture scope"},
        )

        prompt_job = self.calls[0][1]["job"]
        self.assertEqual(prompt_job["posting_text"], "Architecture scope")
        self.assertIn("indeed", prompt_job["notes"])
