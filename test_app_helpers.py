"""Table-driven unit tests for pure parsing, filtering, and rendering helpers."""

import importlib.util
import tempfile
import unittest
from dataclasses import replace
from pathlib import Path
from unittest.mock import patch

from bs4 import BeautifulSoup

from job_search.application.discovery_policy import DiscoveryPolicy, extract_annual_compensation_values
from job_search.application.packet_draft_service import validate_packet_payload
from job_search.composition import outbound_clients, runtime_configuration
from job_search.config import RuntimePaths
from job_search.data_access import codex_cli, document_writer
from job_search.data_access.http_gateway import CapturedResponse

APP_PATH = Path(__file__).resolve().parent / "job_search" / "presentation" / "legacy.py"
SPEC = importlib.util.spec_from_file_location("helpers_app", APP_PATH)
helpers_app = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(helpers_app)


class AppHelperTests(unittest.TestCase):
    def test_text_url_and_pipeline_normalization(self):
        self.assertEqual(helpers_app.clean_text("  one\n two  "), "one two")
        self.assertEqual(helpers_app.clean_url("https://example.test/job?trk=value"), "https://example.test/job")
        self.assertEqual(helpers_app.normalize_pipeline(["wrong", "Wildcards"]), "Wildcards")
        self.assertEqual(helpers_app.normalize_lookup_text("Senior-Principal Engineer!"), "senior principal engineer")
        self.assertEqual(
            helpers_app.source_id("manual", "https://example.test"),
            helpers_app.source_id("manual", "https://example.test"),
        )

    def test_compensation_helpers_cover_hourly_monthly_and_unknown_text(self):
        values = extract_annual_compensation_values("Pay range $100/hr to $12,000 per month")
        self.assertIn(208_000, values)
        self.assertIn(144_000, values)
        policy = DiscoveryPolicy(200_000)
        self.assertFalse(policy.compensation({"snippet": "$10,000 per month"})[0])
        self.assertTrue(policy.compensation({"snippet": "$250k per year"})[0])

    def test_listing_filters_and_deduplication(self):
        policy = DiscoveryPolicy(200_000)
        self.assertTrue(policy.location({"location": "Remote - US"})[0])
        self.assertFalse(policy.location({"location": "Paris, France"})[0])
        self.assertTrue(policy.compensation({"snippet": "$250,000 per year"})[0])
        self.assertFalse(policy.sales_role({"title": "Sales Director"})[0])
        self.assertEqual(
            policy.rejection_reason({"title": "Sales Director", "location": "Paris, France"})[0],
            "sales_role",
            "Sales-role exclusion should take precedence over location rejection.",
        )
        self.assertEqual(
            policy.rejection_reason({"title": "Architect", "location": "Paris, France"})[0],
            "location",
            "Location rejection should precede compensation checks.",
        )
        self.assertEqual(
            policy.rejection_reason({"title": "Architect", "location": "Seattle, WA", "snippet": "$150,000 per year"})[
                0
            ],
            "compensation",
            "Below-threshold compensation should reject an otherwise eligible role.",
        )
        self.assertIsNone(policy.rejection_reason({"title": "Architect", "location": "Seattle, WA"}))
        values = helpers_app.dedupe_results(
            [{"url": "a"}, {"url": "a"}, {"source_job_id": "b"}, {"source_job_id": "b"}]
        )
        self.assertEqual(len(values), 2)

    def test_html_json_ld_and_markdown_helpers(self):
        html = '<script type="application/ld+json">{"@type":"JobPosting","title":"Architect"}</script>'
        soup = BeautifulSoup(html, "html.parser")
        self.assertEqual(helpers_app.extract_job_json_ld(soup)["title"], "Architect")
        self.assertEqual(
            helpers_app.render_inline_markdown("**bold** and `code`"), "<strong>bold</strong> and <code>code</code>"
        )
        rendered = helpers_app.markdown_to_html(
            "# Heading\n\n- one\n- two\n\n---\n\nParagraph\n\n```\n<safe code>\n```"
        )
        self.assertIn("<h1>Heading</h1>", rendered)
        self.assertIn("<li>one</li>", rendered)
        self.assertIn("<hr>", rendered)
        self.assertIn("&lt;safe code&gt;", rendered)
        self.assertEqual(helpers_app.escape_html("<tag>"), "&lt;tag&gt;")

    def test_packet_payload_validation_rejects_invalid_contracts(self):
        valid = {
            "job_brief_markdown": "Brief",
            "resume_markdown": "Resume",
            "cover_letter_markdown": "Letter",
            "generation_metadata": {"model": "test", "generation_date": "2026-09-24"},
        }
        self.assertEqual(validate_packet_payload(valid)["resume_markdown"], "Resume\n")
        with self.assertRaisesRegex(ValueError, "missing"):
            validate_packet_payload({})
        with self.assertRaisesRegex(ValueError, "object"):
            validate_packet_payload([])

    def test_scrape_and_board_adapters_use_fake_response(self):
        def fake_fetch(service, *_args, **_kwargs):
            pages = {
                "manual_posting": """
                    <title>Principal Engineer - ExampleCo</title>
                    <h1>Principal Engineer</h1><div class='topcard__org-name-link'>ExampleCo</div>
                    <div class='topcard__flavor--bullet'>Seattle, WA</div><div id='job-details'>Architecture work</div>
                    """,
                "linkedin": """
                    <li><a class='base-card__full-link' href='https://linkedin.com/jobs/view/1'>Role</a>
                    <h3 class='base-search-card__title'>Architect</h3><h4 class='base-search-card__subtitle'>ExampleCo</h4>
                    <span class='job-search-card__location'>Remote</span></li>
                    """,
                "indeed": """
                    <div data-jk='abc'><a href='/viewjob?jk=abc'>Role</a><h2><span title='Architect'>Architect</span></h2>
                    <span data-testid='company-name'>ExampleCo</span><div data-testid='text-location'>Remote</div></div>
                    """,
            }
            return CapturedResponse({"status_code": 200, "text": pages[service]})

        boards = outbound_clients(
            helpers_app.OBSERVABILITY,
            helpers_app.clean_text,
            helpers_app.clean_url,
            helpers_app.source_id,
            helpers_app.dedupe_results,
            fetch=fake_fetch,
        ).boards
        scraped = boards.scrape("https://jobs.example.test/123")
        linkedin = boards.linkedin("architect", "Remote")
        indeed = boards.indeed("architect", "Remote")
        self.assertEqual(scraped["title"], "Principal Engineer")
        self.assertEqual(scraped["company"], "ExampleCo")
        self.assertEqual(linkedin[0]["company"], "ExampleCo")
        self.assertEqual(indeed[0]["source_job_id"], "abc")

    def test_document_writer_creates_markdown_and_invokes_fake_pandoc(self):
        with tempfile.TemporaryDirectory() as directory:
            original_which = document_writer.shutil.which
            original_run = document_writer.subprocess.run
            commands = []

            class CompletedProcess:
                returncode = 0
                stdout = ""
                stderr = ""

            document_writer.shutil.which = lambda command: "/fake/pandoc" if command == "pandoc" else None
            document_writer.subprocess.run = lambda command, **_kwargs: (commands.append(command) or CompletedProcess())
            try:
                files = document_writer.PacketDocumentWriter().write(
                    Path(directory) / "packet",
                    {
                        "job_brief_markdown": "Brief",
                        "resume_markdown": "Resume",
                        "cover_letter_markdown": "Letter",
                    },
                )
            finally:
                document_writer.shutil.which = original_which
                document_writer.subprocess.run = original_run

            self.assertEqual(files, ["Job-Brief.md", "Resume.md", "Cover-Letter.md"])
            self.assertEqual(len(commands), 3)
            self.assertTrue((Path(directory) / "packet" / "Resume.md").exists())

    def test_capture_round_trip_uses_temporary_directory(self):
        with tempfile.TemporaryDirectory() as directory:
            with (
                patch.object(
                    helpers_app.RUNTIME_CONFIG,
                    "paths",
                    replace(helpers_app.RUNTIME_CONFIG.paths, captures=Path(directory)),
                ),
                patch.object(helpers_app.RUNTIME_CONFIG, "environment", {"JOB_SEARCH_USE_CAPTURE_CACHE": "1"}),
            ):
                payload = {"request": "value"}
                helpers_app.write_capture("test", "operation", payload, {"status_code": 200})
                replay = helpers_app.read_capture("test", "operation", payload)
            self.assertEqual(replay["response"]["status_code"], 200)

    def test_environment_file_update_preserves_comments_and_replaces_values(self):
        with tempfile.TemporaryDirectory() as directory:
            environment_file = RuntimePaths.from_root(Path(directory)).environment_file
            environment_file.write_text("# local settings\nCODEX_MODEL=old\n\n", encoding="utf-8")
            config = runtime_configuration(Path(directory), {})
            config.update({"CODEX_MODEL": "test-model", "CODEX_CLI_PATH": "codex"})
            contents = environment_file.read_text(encoding="utf-8")

        self.assertIn("# local settings", contents)
        self.assertIn("CODEX_MODEL=test-model", contents)
        self.assertIn("CODEX_CLI_PATH=codex", contents)
        self.assertEqual(config.model(), "test-model", "Updates should refresh the injected snapshot")

    def test_codex_cli_adapter_uses_fake_subprocess_and_extracts_model(self):
        original_run = codex_cli.subprocess.run

        class CompletedProcess:
            returncode = 0
            stdout = '{"total_score": 80}'
            stderr = "model: test-model"

        codex_cli.subprocess.run = lambda *_args, **_kwargs: CompletedProcess()
        try:
            output, model = helpers_app.call_codex_json(
                "test-model",
                {"task": "score_job"},
                "score_job",
                force_refresh=True,
                return_metadata=True,
            )
        finally:
            codex_cli.subprocess.run = original_run

        self.assertEqual(output, '{"total_score": 80}')
        self.assertEqual(model, "test-model")


if __name__ == "__main__":
    unittest.main()
