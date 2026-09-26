import unittest

from job_search.application.job_scoring_policy import JobScoringPolicy


class JobScoringPolicyTests(unittest.TestCase):
    def test_builds_portable_prompt_and_returns_model_json(self):
        prompts = []
        policy = JobScoringPolicy(
            pipelines=("Executive IC",), rubric_fields=("scope",), level_reference="Oracle IC6 is Architect."
        )

        result = policy.score(
            {"company": "Example", "title": "Architect", "url": "https://example.test", "pipeline": "Executive IC"},
            career_context={"focus": "architecture"},
            calibration_examples=[],
            invoke=lambda prompt: prompts.append(prompt) or '{"total_score": 91, "scorecard": {"scope": 9}}',
        )

        self.assertEqual(91, result["total_score"], "The parsed score should preserve the model result.")
        self.assertEqual("Architect", prompts[0]["job"]["title"], "The policy must forward job facts to its port.")

    def test_rejects_empty_or_invalid_model_output(self):
        policy = JobScoringPolicy(pipelines=(), rubric_fields=(), level_reference="target")
        common = {"career_context": {}, "calibration_examples": []}
        with self.assertRaisesRegex(RuntimeError, "did not include text"):
            policy.score({}, invoke=lambda _prompt: "", **common)
        with self.assertRaisesRegex(RuntimeError, "not valid JSON"):
            policy.score({}, invoke=lambda _prompt: "not-json", **common)
