"""Deterministic API workflow tests using a temporary SQLite database."""

import importlib.util
import tempfile
import unittest
from pathlib import Path

from job_search.presentation.factory import create_app

APP_PATH = Path(__file__).resolve().parent / "job_search" / "presentation" / "legacy.py"
SPEC = importlib.util.spec_from_file_location("workflow_app", APP_PATH)
workflow_app = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(workflow_app)
workflow_app.app = create_app(route_blueprint=workflow_app.routes)


class ApplicationWorkflowTests(unittest.TestCase):
    def setUp(self):
        self.tempdir = tempfile.TemporaryDirectory()
        self.original_db_path = workflow_app.DB_PATH
        self.original_env_path = workflow_app.ENV_PATH
        self.original_applications_dir = workflow_app.APPLICATIONS_DIR
        self.original_root = workflow_app.ROOT
        self.original_gpt_scoring_enabled = workflow_app.gpt_scoring_enabled
        workflow_app.DB_PATH = Path(self.tempdir.name) / "jobs.sqlite3"
        workflow_app.ENV_PATH = Path(self.tempdir.name) / ".env"
        workflow_app.APPLICATIONS_DIR = Path(self.tempdir.name) / "applications"
        workflow_app.ROOT = Path(self.tempdir.name)
        workflow_app.gpt_scoring_enabled = lambda: False
        workflow_app.init_db()
        self.client = workflow_app.app.test_client()

    def tearDown(self):
        workflow_app.DB_PATH = self.original_db_path
        workflow_app.ENV_PATH = self.original_env_path
        workflow_app.APPLICATIONS_DIR = self.original_applications_dir
        workflow_app.ROOT = self.original_root
        workflow_app.gpt_scoring_enabled = self.original_gpt_scoring_enabled
        self.tempdir.cleanup()

    def create_job(self, *, company="ExampleCo", title="Principal Engineer", url="https://example.com/role"):
        with workflow_app.connect() as connection:
            return connection.execute(
                """INSERT INTO jobs(created_at, updated_at, company, title, url, pipeline, status, source_board)
                   VALUES (?, ?, ?, ?, ?, ?, ?, ?)""",
                (1, 1, company, title, url, "Executive IC", "researching", "manual"),
            ).lastrowid

    def test_company_note_interaction_status_and_user_score_workflow(self):
        job_id = self.create_job()
        company = self.client.post(
            "/api/companies",
            json={"company": "ExampleCo", "status": "target", "interest_score": 90, "next_step": "Research"},
        )
        note = self.client.post(f"/api/jobs/{job_id}/notes", json={"note": "Strong architecture fit."})
        interaction = self.client.post(
            f"/api/jobs/{job_id}/interactions",
            json={"occurred_on": "2026-09-24", "person_name": "Alex", "channel": "email", "summary": "Intro"},
        )
        score = self.client.post(
            f"/api/jobs/{job_id}/score-user",
            json={"scorecard": {field: 8 for field in workflow_app.RUBRIC_FIELDS}, "user_rationale": "Aligned."},
        )
        status = self.client.post(f"/api/jobs/{job_id}/status", json={"status": "interested"})

        self.assertEqual(company.status_code, 201)
        self.assertEqual(note.status_code, 201)
        self.assertEqual(interaction.status_code, 201)
        self.assertEqual(score.status_code, 200)
        self.assertEqual(status.status_code, 200)
        job = status.get_json()["job"]
        self.assertEqual(job["status"], "interested")
        self.assertEqual(job["user_score"], 80)
        self.assertEqual(len(job["notes_list"]), 1)
        self.assertEqual(len(job["interactions"]), 1)

    def test_search_query_and_settings_workflow(self):
        created = self.client.post(
            "/api/search/queries",
            json={"board": "indeed", "pipeline": "Wildcards", "keywords": "robotics", "location": "Remote"},
        )
        query_id = created.get_json()["search_queries"][-1]["id"]
        updated = self.client.post(
            f"/api/search/queries/{query_id}", json={"enabled": False, "criteria": "Interesting"}
        )
        settings = self.client.post("/api/settings", json={"gpt_threshold": 55, "user_threshold": 65})
        state = self.client.get("/api/state?include_filtered=1")

        self.assertEqual(created.status_code, 201)
        self.assertEqual(updated.status_code, 200)
        query = next(item for item in updated.get_json()["search_queries"] if item["id"] == query_id)
        self.assertEqual(query["enabled"], 0)
        self.assertEqual(query["criteria"], "Interesting")
        self.assertEqual(settings.get_json()["settings"]["gpt_threshold"], "55")
        self.assertEqual(state.status_code, 200)
        self.assertIn("jobs", state.get_json())

    def test_packet_attachment_rejects_missing_or_outside_directory(self):
        job_id = self.create_job()
        missing = self.client.post(f"/api/jobs/{job_id}/application-packet/attach", json={})
        traversal = self.client.post(f"/api/jobs/{job_id}/application-packet/attach", json={"path": "../outside"})

        self.assertEqual(missing.status_code, 400)
        self.assertEqual(traversal.status_code, 400)

    def test_admin_purge_requires_confirmation_then_removes_jobs(self):
        self.create_job()
        rejected = self.client.post("/api/admin/purge-jobs", json={"confirm": "no"})
        accepted = self.client.post("/api/admin/purge-jobs", json={"confirm": "PURGE"})

        self.assertEqual(rejected.status_code, 400)
        self.assertEqual(accepted.status_code, 200)
        self.assertEqual(accepted.get_json()["deleted_jobs"], 1)

    def test_search_run_tracks_valid_discovery_and_records_filter_rejections(self):
        originals = (workflow_app.fetch_jobs_for_query, workflow_app.refine_search_query)
        workflow_app.fetch_jobs_for_query = lambda *_args, **_kwargs: [
            {
                "board": "indeed",
                "company": "SalesCo",
                "title": "Sales Director",
                "location": "Remote",
                "url": "https://a",
            },
            {
                "board": "indeed",
                "company": "ParisCo",
                "title": "Architect",
                "location": "Paris, France",
                "url": "https://b",
            },
            {
                "board": "indeed",
                "company": "LowPay",
                "title": "Architect",
                "location": "Remote",
                "url": "https://c",
                "snippet": "$100,000 per year",
            },
            {
                "board": "indeed",
                "company": "GoodCo",
                "title": "Principal Architect",
                "location": "Seattle, WA",
                "url": "https://d",
                "snippet": "$250,000 per year",
            },
        ]
        workflow_app.refine_search_query = lambda *_args, **_kwargs: None
        try:
            with workflow_app.connect() as connection:
                connection.execute("UPDATE search_queries SET enabled = 0")
                connection.execute(
                    """INSERT INTO search_queries(board, pipeline, keywords, location, enabled, created_at, criteria)
                       VALUES ('indeed', 'Executive IC', 'architect', 'Remote', 1, 1, 'test')"""
                )
            run = workflow_app.run_job_search()
        finally:
            workflow_app.fetch_jobs_for_query, workflow_app.refine_search_query = originals

        self.assertEqual(run["found_count"], 4)
        self.assertEqual(run["tracked_count"], 1)
        self.assertEqual(run["rejected_count"], 3)
        with workflow_app.connect() as connection:
            self.assertEqual(connection.execute("SELECT COUNT(*) FROM jobs").fetchone()[0], 1)
            self.assertEqual(connection.execute("SELECT COUNT(*) FROM discovered_jobs").fetchone()[0], 4)

    def test_refinement_and_codex_classification_use_fake_model_boundary(self):
        originals = (
            workflow_app.gpt_scoring_enabled,
            workflow_app.codex_cli_available,
            workflow_app.call_codex_json,
            workflow_app.score_discovery_with_codex,
        )
        workflow_app.gpt_scoring_enabled = lambda: True
        workflow_app.codex_cli_available = lambda: True
        workflow_app.call_codex_json = lambda *_args, **_kwargs: (
            '{"keywords":"chief architect","location":"Remote","criteria":"strategy","refinement_notes":"narrowed"}'
        )
        workflow_app.score_discovery_with_codex = lambda *_args, **_kwargs: {
            "total_score": 85,
            "scorecard": {field: 8 for field in workflow_app.RUBRIC_FIELDS},
            "pipeline": "Executive IC",
            "level_assessment": "IC6-equivalent",
            "downlevel": False,
            "rationale": "Strong fit.",
        }
        try:
            with workflow_app.connect() as connection:
                query_id = connection.execute(
                    """INSERT INTO search_queries(board, pipeline, keywords, location, enabled, created_at, criteria)
                       VALUES ('indeed', 'Executive IC', 'architect', 'Remote', 1, 1, 'old')"""
                ).lastrowid
                connection.execute(
                    """INSERT INTO discovered_jobs(created_at, board, decision, query_id, company, title)
                       VALUES (1, 'indeed', 'tracked', ?, 'ExampleCo', 'Architect')""",
                    (query_id,),
                )
                workflow_app.refine_search_query(connection, query_id)
                refined = connection.execute(
                    "SELECT keywords, refinement_notes FROM search_queries WHERE id = ?", (query_id,)
                ).fetchone()
                decision = workflow_app.classify_discovery(
                    connection,
                    {
                        "board": "indeed",
                        "company": "ExampleCo",
                        "title": "Architect",
                        "url": "https://role",
                        "location": "Remote",
                        "pipeline": "Executive IC",
                    },
                )
            self.assertEqual(refined["keywords"], "chief architect")
            self.assertEqual(refined["refinement_notes"], "narrowed")
            self.assertEqual(decision[0], "tracked")
            self.assertEqual(decision[3]["total_score"], 85)
        finally:
            (
                workflow_app.gpt_scoring_enabled,
                workflow_app.codex_cli_available,
                workflow_app.call_codex_json,
                workflow_app.score_discovery_with_codex,
            ) = originals

    def test_codex_scoring_persists_a_valid_fake_scorecard(self):
        job_id = self.create_job()
        original_values = (
            workflow_app.gpt_scoring_enabled,
            workflow_app.codex_cli_available,
            workflow_app.call_codex_json,
            workflow_app.career_context,
            workflow_app.calibration_examples,
        )
        scorecard = {field: 8 for field in workflow_app.RUBRIC_FIELDS}
        score = {
            "total_score": 88,
            "pipeline": "Executive IC",
            "scorecard": scorecard,
            "level_assessment": "Architect equivalent",
            "downlevel": False,
            "rationale": "Cross-organizational architecture role.",
        }
        workflow_app.gpt_scoring_enabled = lambda: True
        workflow_app.codex_cli_available = lambda: True
        workflow_app.call_codex_json = lambda *_args, **_kwargs: __import__("json").dumps(score)
        workflow_app.career_context = lambda: "Career context"
        workflow_app.calibration_examples = lambda _conn: []
        try:
            with workflow_app.connect() as connection:
                persisted = workflow_app.populate_codex_score(connection, job_id)
                saved = workflow_app.get_job(connection, job_id)
        finally:
            (
                workflow_app.gpt_scoring_enabled,
                workflow_app.codex_cli_available,
                workflow_app.call_codex_json,
                workflow_app.career_context,
                workflow_app.calibration_examples,
            ) = original_values

        self.assertEqual(persisted["total_score"], 88)
        self.assertEqual(saved["gpt_score"], 88)
        self.assertEqual(saved["level_assessment"], "Architect equivalent")

    def test_read_endpoints_return_state_jobs_and_missing_resource_errors(self):
        job_id = self.create_job()

        state = self.client.get("/api/state")
        job = self.client.get(f"/api/jobs/{job_id}")
        packets = self.client.get("/api/application-packets")
        missing_job = self.client.get("/api/jobs/99999")
        missing_company = self.client.get("/api/companies/99999")
        missing_task = self.client.get("/api/codex-tasks/not-a-task")

        self.assertEqual(state.status_code, 200)
        self.assertEqual(job.get_json()["job"]["id"], job_id)
        self.assertEqual(packets.get_json()["application_packets"], [])
        self.assertEqual(missing_job.status_code, 404)
        self.assertEqual(missing_company.status_code, 404)
        self.assertEqual(missing_task.status_code, 404)

    def test_packet_content_render_attach_and_delete_use_safe_paths(self):
        job_id = self.create_job()
        packet_dir = workflow_app.APPLICATIONS_DIR / "example-packet"
        packet_dir.mkdir(parents=True)
        (packet_dir / "Resume.md").write_text("# Resume\n\nSafe content", encoding="utf-8")

        attached = self.client.post(
            f"/api/jobs/{job_id}/application-packet/attach",
            json={"path": workflow_app.repo_relative(packet_dir)},
        )
        content = self.client.get(f"/api/jobs/{job_id}/application-packet/content?file=Resume.md")
        rendered = self.client.get(f"/api/jobs/{job_id}/application-packet/render?file=Resume.md")
        unsafe = self.client.get(f"/api/jobs/{job_id}/application-packet/content?file=../secret.md")
        rejected_delete = self.client.delete(f"/api/jobs/{job_id}", json={"confirm": "no"})
        deleted = self.client.delete(f"/api/jobs/{job_id}", json={"confirm": "DELETE"})

        self.assertEqual(attached.status_code, 200)
        self.assertEqual(content.get_json()["content"], "# Resume\n\nSafe content")
        self.assertIn("Safe content", rendered.get_data(as_text=True))
        self.assertEqual(unsafe.status_code, 400)
        self.assertEqual(rejected_delete.status_code, 400)
        self.assertEqual(deleted.get_json()["deleted_job_id"], job_id)

    def test_rescrape_and_manual_job_creation_handle_fake_success_and_failure(self):
        job_id = self.create_job()
        original_scrape = workflow_app.OUTBOUND_CLIENTS.boards.scrape
        calls = []

        def fake_scrape(url, force_refresh=False):
            calls.append((url, force_refresh))
            if "failure" in url:
                raise RuntimeError("source unavailable")
            return {
                "url": url,
                "company": "RefreshedCo",
                "title": "Principal Architect",
                "location": "Remote",
                "posting_text": "Architecture scope",
                "source_board": "manual",
                "source_job_id": "abc",
            }

        workflow_app.OUTBOUND_CLIENTS.boards.scrape = fake_scrape
        try:
            rescraped = self.client.post(f"/api/jobs/{job_id}/scrape", json={"force_refresh": True})
            created = self.client.post(
                "/api/jobs",
                json={"url": "https://example.com/failure", "pipeline": "Executive IC"},
            )
            duplicate = self.client.post(
                "/api/jobs",
                json={"url": "https://example.com/failure", "pipeline": "Executive IC"},
            )
        finally:
            workflow_app.OUTBOUND_CLIENTS.boards.scrape = original_scrape

        self.assertEqual(rescraped.get_json()["job"]["company"], "RefreshedCo")
        self.assertEqual(created.status_code, 201)
        self.assertIn("source unavailable", created.get_json()["scrape_error"])
        self.assertEqual(duplicate.status_code, 409)
        self.assertIn(("https://example.com/role", True), calls)

    def test_bulk_endpoints_validate_availability_and_start_fake_tasks(self):
        job_id = self.create_job()
        originals = (
            workflow_app.gpt_scoring_enabled,
            workflow_app.codex_cli_available,
            workflow_app.start_background_task,
        )
        workflow_app.gpt_scoring_enabled = lambda: True
        workflow_app.codex_cli_available = lambda: True
        workflow_app.start_background_task = lambda kind, ids: {"kind": kind, "job_ids": ids}
        try:
            score = self.client.post("/api/jobs/bulk/score-gpt", json={"job_ids": [job_id]})
            packets = self.client.post("/api/jobs/bulk/application-packets/generate", json={"job_ids": [job_id]})
        finally:
            (
                workflow_app.gpt_scoring_enabled,
                workflow_app.codex_cli_available,
                workflow_app.start_background_task,
            ) = originals

        self.assertEqual(score.status_code, 202)
        self.assertEqual(score.get_json()["task"]["kind"], "scorecards")
        self.assertEqual(packets.get_json()["task"]["kind"], "application_packets")

    def test_bulk_score_worker_records_complete_skipped_and_failed_items(self):
        job_id = self.create_job()
        originals = (
            workflow_app.update_background_task,
            workflow_app.update_background_task_item,
            workflow_app.process_background_task_item,
        )
        task_updates = []
        item_updates = []
        workflow_app.update_background_task = lambda *args, **kwargs: task_updates.append((args, kwargs))
        workflow_app.update_background_task_item = lambda *args, **kwargs: item_updates.append((args, kwargs))

        def fake_process(claim):
            if claim["job_id"] == job_id:
                return "complete", "Codex score 90"
            return "skipped", "Job not found"

        workflow_app.process_background_task_item = fake_process
        try:
            workflow_app.bulk_score_worker("score-task", [job_id, 99999])
        finally:
            (
                workflow_app.update_background_task,
                workflow_app.update_background_task_item,
                workflow_app.process_background_task_item,
            ) = originals

        self.assertTrue(any(update[1].get("status") == "complete" for update in item_updates))
        self.assertTrue(any(update[1].get("status") == "skipped" for update in item_updates))
        self.assertEqual(task_updates[-1][1]["status"], "complete")

    def test_bulk_packet_worker_records_complete_skip_and_error_items(self):
        complete_id = self.create_job(company="CompleteCo")
        skipped_id = self.create_job(company="SkippedCo", url="https://example.com/skipped")
        failed_id = self.create_job(company="FailedCo", url="https://example.com/failed")
        with workflow_app.connect() as connection:
            connection.execute(
                "UPDATE jobs SET application_packet_path = 'applications/existing' WHERE id = ?", (skipped_id,)
            )
        originals = (
            workflow_app.update_background_task,
            workflow_app.update_background_task_item,
            workflow_app.process_background_task_item,
        )
        task_updates = []
        item_updates = []
        workflow_app.update_background_task = lambda *args, **kwargs: task_updates.append((args, kwargs))
        workflow_app.update_background_task_item = lambda *args, **kwargs: item_updates.append((args, kwargs))

        def fake_process(claim):
            if claim["job_id"] == failed_id:
                raise RuntimeError("draft failed")
            if claim["job_id"] in {skipped_id, 99999}:
                return "skipped", "Not eligible"
            return "complete", "applications/complete"

        workflow_app.process_background_task_item = fake_process
        try:
            workflow_app.bulk_packet_worker("packet-task", [complete_id, skipped_id, failed_id, 99999])
        finally:
            (
                workflow_app.update_background_task,
                workflow_app.update_background_task_item,
                workflow_app.process_background_task_item,
            ) = originals

        statuses = {update[1].get("status") for update in item_updates}
        self.assertTrue({"complete", "skipped", "error"}.issubset(statuses))
        self.assertEqual(task_updates[-1][1]["status"], "error")

    def test_company_update_search_dispatch_and_individual_codex_actions(self):
        job_id = self.create_job()
        company = self.client.post("/api/companies", json={"company": "ExampleCo", "status": "watching"})
        company_id = company.get_json()["company"]["id"]
        updated_company = self.client.post(
            f"/api/companies/{company_id}",
            json={"status": "target", "interest_score": 95, "contacts": "Taylor"},
        )
        originals = (
            workflow_app.run_job_search,
            workflow_app.gpt_scoring_enabled,
            workflow_app.scoring_service,
            workflow_app.create_application_packet,
        )
        workflow_app.run_job_search = lambda **_kwargs: {"status": "complete", "found_count": 0}
        workflow_app.gpt_scoring_enabled = lambda: True

        class FakeScoringService:
            def score(self, current_job_id):
                return type(
                    "ScoreResult",
                    (),
                    {
                        "state": "scored",
                        "job": workflow_app.job_service().get_job(current_job_id),
                        "raw_score": {"total_score": 89},
                    },
                )()

        workflow_app.scoring_service = lambda: FakeScoringService()
        workflow_app.create_application_packet = lambda *_args, **_kwargs: {
            "path": "applications/generated",
            "name": "generated",
            "markdown_files": ["Resume.md"],
        }
        try:
            search = self.client.post("/api/search/run", json={"force_refresh": True})
            score = self.client.post(f"/api/jobs/{job_id}/score-gpt", json={})
            packet = self.client.post(f"/api/jobs/{job_id}/application-packet/generate", json={})
        finally:
            (
                workflow_app.run_job_search,
                workflow_app.gpt_scoring_enabled,
                workflow_app.scoring_service,
                workflow_app.create_application_packet,
            ) = originals

        self.assertEqual(updated_company.get_json()["company"]["status"], "target")
        self.assertEqual(updated_company.get_json()["company"]["interest_score"], 95)
        self.assertEqual(search.get_json()["run"]["status"], "complete")
        self.assertEqual(score.get_json()["raw_score"]["total_score"], 89)
        self.assertEqual(packet.status_code, 201)

    def test_error_routes_cover_disabled_unavailable_and_not_found_responses(self):
        job_id = self.create_job()
        originals = (workflow_app.gpt_scoring_enabled, workflow_app.codex_cli_available)
        workflow_app.gpt_scoring_enabled = lambda: False
        workflow_app.codex_cli_available = lambda: False
        try:
            disabled_score = self.client.post(f"/api/jobs/{job_id}/score-gpt", json={})
            disabled_bulk = self.client.post("/api/jobs/bulk/score-gpt", json={"job_ids": [job_id]})
            unavailable_packet = self.client.post(
                "/api/jobs/bulk/application-packets/generate", json={"job_ids": [job_id]}
            )
            missing_packet = self.client.post("/api/jobs/99999/application-packet/generate", json={})
        finally:
            workflow_app.gpt_scoring_enabled, workflow_app.codex_cli_available = originals

        self.assertEqual(disabled_score.status_code, 409)
        self.assertEqual(disabled_bulk.status_code, 409)
        self.assertEqual(unavailable_packet.status_code, 409)
        self.assertEqual(missing_packet.status_code, 404)

    def test_packet_repository_associates_generated_packet_and_lists_orphans(self):
        job_id = self.create_job()
        generated_dir = workflow_app.APPLICATIONS_DIR / "generated"
        orphan_dir = workflow_app.APPLICATIONS_DIR / "orphan"
        generated_dir.mkdir(parents=True)
        orphan_dir.mkdir()
        (generated_dir / "Resume.md").write_text("Resume", encoding="utf-8")
        (orphan_dir / "Job-Brief.md").write_text("Brief", encoding="utf-8")
        original_generator = workflow_app.generate_application_packet_with_codex
        workflow_app.generate_application_packet_with_codex = lambda _job: {
            "packet_dir": generated_dir,
            "markdown_files": ["Resume.md"],
            "output_text": "{}",
        }
        try:
            with workflow_app.connect() as connection:
                created = workflow_app.create_application_packet(connection, job_id)
                packets = workflow_app.list_application_packets(connection)
        finally:
            workflow_app.generate_application_packet_with_codex = original_generator

        by_name = {packet["name"]: packet for packet in packets}
        self.assertEqual(created["name"], "generated")
        self.assertEqual(by_name["generated"]["associated_job"]["id"], job_id)
        self.assertTrue(by_name["orphan"]["unassociated"])

    def test_calibration_examples_include_saved_user_feedback(self):
        job_id = self.create_job()
        with workflow_app.connect() as connection:
            connection.execute(
                """UPDATE jobs SET gpt_score = 70, user_score = 85, user_rationale = ?, posting_text = ? WHERE id = ?""",
                ("Strategic scope", "A" * 1000, job_id),
            )
            examples = workflow_app.calibration_examples(connection)

        self.assertEqual(examples[0]["user_score"], 85)
        self.assertIn("...", examples[0]["posting_excerpt"])


if __name__ == "__main__":
    unittest.main()
