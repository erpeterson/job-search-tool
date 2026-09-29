"""Deterministic API workflow tests using a temporary SQLite database."""

import tempfile
import unittest
from dataclasses import replace
from pathlib import Path

from job_search.application.discovery_policy import MIN_ANNUAL_COMPENSATION, DiscoveryPolicy
from job_search.application.discovery_service import UNKNOWN_LEVEL_ASSESSMENT
from job_search.application.discovery_utils import clean_text
from job_search.application.job_scoring_policy import ORACLE_IC6_LEVEL_REFERENCE, RUBRIC_FIELDS, normalize_pipeline
from job_search.application.manual_job_service import ManualJobService
from job_search.application.rescrape_service import RescrapeService
from job_search.application.task_submission_service import TaskSubmissionService
from job_search.composition import (
    codex_scoring_workflow,
    database_session,
    discovery_service,
    job_score_service,
    packet_generation_service,
    presentation_dependencies,
    search_run_service,
)
from job_search.data_access.job_repository import SqliteJobRepository
from job_search.data_access.packet_storage import PacketStorage
from job_search.data_access.read_models import SqliteReadModels
from job_search.presentation.factory import create_app


class ApplicationWorkflowTests(unittest.TestCase):
    def setUp(self):
        self.tempdir = tempfile.TemporaryDirectory()
        self.addCleanup(self.tempdir.cleanup)
        self.root = Path(self.tempdir.name)
        self.database = self.root / "jobs.sqlite3"
        self.applications = self.root / "applications"
        self.packet_storage = PacketStorage(self.root, self.applications)
        self.dependencies = presentation_dependencies(self.database, {"JOB_SEARCH_ENABLE_GPT_SCORING": "0"})
        self.app = create_app(dependencies=self.dependencies)
        self.dependencies.startup_service.initialize()
        self.client = self.app.test_client()

    def client_with(self, **overrides):
        return create_app(dependencies=replace(self.dependencies, **overrides)).test_client()

    def create_job(self, *, company="ExampleCo", title="Principal Engineer", url="https://example.com/role"):
        with database_session(self.database) as connection:
            return connection.execute(
                """INSERT INTO jobs(created_at, updated_at, company, title, url, pipeline, status, source_board)
                   VALUES (?, ?, ?, ?, ?, ?, ?, ?)""",
                (1, 1, company, title, url, "Executive IC", "researching", "manual"),
            ).lastrowid

    def make_discovery(self, *, scoring_enabled=False, score=None, refine=None, telemetry=None):
        return discovery_service(
            self.database,
            telemetry or self.dependencies.observability.telemetry,
            UNKNOWN_LEVEL_ASSESSMENT,
            ORACLE_IC6_LEVEL_REFERENCE,
            scoring_enabled=lambda: scoring_enabled,
            scorer_available=lambda: True,
            scorer_path=lambda: "fake-codex",
            score=score or (lambda *_args, **_kwargs: {}),
            apply_filter=lambda *_args: None,
            normalize_pipeline=normalize_pipeline,
            refine=refine or (lambda *_args, **_kwargs: None),
            clean_text=clean_text,
        )

    def make_search(self, fetch, *, telemetry=None):
        class FakeBoards:
            def fetch(self, board, keywords, location, *, force_refresh=False):
                return fetch(board, keywords, location, force_refresh=force_refresh)

        discovery = self.make_discovery(telemetry=telemetry)
        return search_run_service(
            self.database,
            FakeBoards(),
            telemetry or self.dependencies.observability.telemetry,
            reject_reason=DiscoveryPolicy(MIN_ANNUAL_COMPENSATION).rejection_reason,
            level_assessment=discovery.assess_level,
            already_seen_reason=lambda connection, url: (
                "already tracked in jobs" if url and SqliteReadModels.job_exists_url(connection, url) else None
            ),
            classify=discovery.classify,
            refine=lambda *_args, **_kwargs: None,
            is_refinement_error=lambda _error: False,
        )

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
            json={"scorecard": {field: 8 for field in RUBRIC_FIELDS}, "user_rationale": "Aligned."},
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
        results = [
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
        with database_session(self.database) as connection:
            connection.execute("UPDATE search_queries SET enabled = 0")
            connection.execute(
                """INSERT INTO search_queries(board, pipeline, keywords, location, enabled, created_at, criteria)
                   VALUES ('indeed', 'Executive IC', 'architect', 'Remote', 1, 1, 'test')"""
            )
        run = self.make_search(lambda *_args, **_kwargs: results).run(trigger="manual", force_refresh=False)

        self.assertEqual(run["found_count"], 4)
        self.assertEqual(run["tracked_count"], 1)
        self.assertEqual(run["rejected_count"], 3)
        with database_session(self.database) as connection:
            self.assertEqual(connection.execute("SELECT COUNT(*) FROM jobs").fetchone()[0], 1)
            self.assertEqual(connection.execute("SELECT COUNT(*) FROM discovered_jobs").fetchone()[0], 4)

    def test_search_run_deduplicates_results_and_records_failed_board_call(self):
        events = []

        class FakeTelemetry:
            def event(self, name, **fields):
                events.append((name, fields))

        def fake_fetch(board, _keywords, _location, *, force_refresh=False):
            if board == "linkedin":
                raise RuntimeError("board unavailable")
            rejected = {
                "board": "indeed",
                "company": "SalesCo",
                "title": "Sales Director",
                "location": "Remote",
                "url": "https://example.test/duplicate",
            }
            return [dict(rejected), dict(rejected)]

        with database_session(self.database) as connection:
            connection.execute("UPDATE search_queries SET enabled = 0")
            for board in ("indeed", "linkedin"):
                connection.execute(
                    """INSERT INTO search_queries(board, pipeline, keywords, location, enabled, created_at, criteria)
                       VALUES (?, 'Executive IC', 'architect', 'Remote', 1, 1, 'test')""",
                    (board,),
                )
        run = self.make_search(fake_fetch, telemetry=FakeTelemetry()).run(trigger="manual", force_refresh=False)

        self.assertEqual(run["found_count"], 1, "A repeated URL should be counted only once")
        self.assertEqual(run["rejected_count"], 1)
        self.assertIn("board unavailable", run["message"])
        self.assertIn("job_search_query_failed", [name for name, _ in events])
        self.assertIn("discovery_skipped", [name for name, _ in events])
        with database_session(self.database) as connection:
            count = connection.execute("SELECT COUNT(*) FROM discovered_jobs").fetchone()[0]
        self.assertEqual(count, 1, "The duplicate should not persist a second discovery")

    def test_search_route_uses_the_injected_runner(self):
        calls = []

        class FakeSearch:
            def run(self, *, trigger, force_refresh):
                calls.append((trigger, force_refresh))
                return {"status": "complete", "found_count": 0}

        response = self.client_with(search_run_service=FakeSearch()).post(
            "/api/search/run", json={"force_refresh": True}
        )

        self.assertEqual(response.status_code, 200)
        self.assertEqual(calls, [("manual", True)], "The route should forward validated inputs to the injected runner")
        self.assertEqual(response.get_json()["run"]["status"], "complete")

    def test_refinement_and_codex_classification_use_fake_model_boundary(self):
        score = {
            "total_score": 85,
            "scorecard": {field: 8 for field in RUBRIC_FIELDS},
            "pipeline": "Executive IC",
            "level_assessment": "IC6-equivalent",
            "downlevel": False,
            "rationale": "Strong fit.",
        }
        refined_output = (
            '{"keywords":"chief architect","location":"Remote","criteria":"strategy","refinement_notes":"narrowed"}'
        )
        service = self.make_discovery(
            scoring_enabled=True,
            score=lambda *_args, **_kwargs: score,
            refine=lambda *_args, **_kwargs: refined_output,
        )
        with database_session(self.database) as connection:
            query_id = connection.execute(
                """INSERT INTO search_queries(board, pipeline, keywords, location, enabled, created_at, criteria)
                   VALUES ('indeed', 'Executive IC', 'architect', 'Remote', 1, 1, 'old')"""
            ).lastrowid
            connection.execute(
                """INSERT INTO discovered_jobs(created_at, board, decision, query_id, company, title)
                   VALUES (1, 'indeed', 'tracked', ?, 'ExampleCo', 'Architect')""",
                (query_id,),
            )
            service.refine_query(connection, query_id, force_refresh=False)
            refined = connection.execute(
                "SELECT keywords, refinement_notes FROM search_queries WHERE id = ?", (query_id,)
            ).fetchone()
            decision = service.classify(
                connection,
                {
                    "board": "indeed",
                    "company": "ExampleCo",
                    "title": "Architect",
                    "url": "https://role",
                    "location": "Remote",
                    "pipeline": "Executive IC",
                },
                force_refresh=False,
            )
        self.assertEqual(refined["keywords"], "chief architect")
        self.assertEqual(refined["refinement_notes"], "narrowed")
        self.assertEqual(decision[0], "tracked")
        self.assertEqual(decision[3]["total_score"], 85)
        with database_session(self.database) as connection:
            saved = connection.execute("SELECT company, gpt_score FROM jobs WHERE id = ?", (decision[2],)).fetchone()
        self.assertEqual((saved["company"], saved["gpt_score"]), ("ExampleCo", 85))

    def test_invalid_refinement_model_output_is_observed_without_query_update(self):
        events = []

        class FakeTelemetry:
            def event(self, name, **fields):
                events.append((name, fields))

        service = self.make_discovery(
            scoring_enabled=True,
            refine=lambda *_args, **_kwargs: "not-json",
            telemetry=FakeTelemetry(),
        )
        with database_session(self.database) as connection:
            query_id = connection.execute(
                """INSERT INTO search_queries(board, pipeline, keywords, location, enabled, created_at, criteria)
                   VALUES ('indeed', 'Executive IC', 'architect', 'Remote', 1, 1, 'old')"""
            ).lastrowid
            connection.execute(
                """INSERT INTO discovered_jobs(created_at, board, decision, query_id, company, title)
                   VALUES (1, 'indeed', 'tracked', ?, 'ExampleCo', 'Architect')""",
                (query_id,),
            )
            service.refine_query(connection, query_id, force_refresh=False)
            keywords = connection.execute("SELECT keywords FROM search_queries WHERE id = ?", (query_id,)).fetchone()[0]

        self.assertEqual(keywords, "architect", "Invalid model output must not replace the query")
        self.assertEqual(events[0][1]["error_code"], "QUERY_REFINEMENT_INVALID_JSON")

    def test_codex_scoring_persists_a_valid_fake_scorecard(self):
        job_id = self.create_job()
        scorecard = {field: 8 for field in RUBRIC_FIELDS}
        score = {
            "total_score": 88,
            "pipeline": "Executive IC",
            "scorecard": scorecard,
            "level_assessment": "Architect equivalent",
            "downlevel": False,
            "rationale": "Cross-organizational architecture role.",
        }

        class FakeScorer:
            def score(self, *_args, **_kwargs):
                return score

        service = codex_scoring_workflow(
            self.database, self.dependencies.configuration, FakeScorer(), self.dependencies.observability.telemetry
        )
        with database_session(self.database) as connection:
            persisted = service.populate(connection, job_id, force_refresh=False)
            saved = SqliteReadModels.job(connection, job_id)

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
        packet_dir = self.applications / "example-packet"
        packet_dir.mkdir(parents=True)
        (packet_dir / "Resume.md").write_text("# Resume\n\nSafe content", encoding="utf-8")

        attached = self.client.post(
            f"/api/jobs/{job_id}/application-packet/attach",
            json={"path": self.packet_storage.relative_path(packet_dir)},
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

        repository = SqliteJobRepository(lambda: database_session(self.database))
        filtering = self.dependencies.filtering_service
        manual = ManualJobService(
            repository,
            fake_scrape,
            lambda url: {"url": url, "company": "Fallback"},
            filtering.refresh_job,
            lambda _job_id: None,
            lambda: "Codex scoring is disabled.",
            lambda *_args: None,
            lambda *_args: None,
            lambda: 100,
        )
        client = self.client_with(
            manual_job_service=manual,
            rescrape_service=RescrapeService(
                repository, fake_scrape, filtering.refresh_job, lambda: 1, lambda *_args, **_kwargs: None
            ),
        )
        rescraped = client.post(f"/api/jobs/{job_id}/scrape", json={"force_refresh": True})
        created = client.post(
            "/api/jobs",
            json={"url": "https://example.com/failure", "pipeline": "Executive IC"},
        )
        duplicate = client.post(
            "/api/jobs",
            json={"url": "https://example.com/failure", "pipeline": "Executive IC"},
        )

        self.assertEqual(rescraped.get_json()["job"]["company"], "RefreshedCo")
        self.assertEqual(created.status_code, 201)
        self.assertIn("source unavailable", created.get_json()["scrape_error"])
        self.assertEqual(duplicate.status_code, 409)
        self.assertIn(("https://example.com/role", True), calls)

    def test_bulk_endpoints_validate_availability_and_start_fake_tasks(self):
        job_id = self.create_job()

        class FakeTasks:
            def start(self, kind, ids):
                return {"kind": kind, "job_ids": ids}

        available_client = self.client_with(
            task_submission_service=TaskSubmissionService(FakeTasks(), lambda: True, lambda: True, lambda: "fake"),
        )
        score = available_client.post("/api/jobs/bulk/score-gpt", json={"job_ids": [job_id]})
        packets = available_client.post("/api/jobs/bulk/application-packets/generate", json={"job_ids": [job_id]})
        unavailable_client = self.client_with(
            task_submission_service=TaskSubmissionService(
                FakeTasks(), lambda: False, lambda: False, lambda: "missing-codex"
            ),
        )
        disabled_score = unavailable_client.post("/api/jobs/bulk/score-gpt", json={"job_ids": [job_id]})
        missing_cli = unavailable_client.post("/api/jobs/bulk/application-packets/generate", json={"job_ids": [job_id]})

        self.assertEqual(score.status_code, 202)
        self.assertEqual(score.get_json()["task"]["kind"], "scorecards")
        self.assertEqual(packets.get_json()["task"]["kind"], "application_packets")
        self.assertEqual(disabled_score.status_code, 409, "Disabled scoring must not enqueue a task.")
        self.assertEqual(missing_cli.status_code, 409, "Missing CLI must not enqueue a packet task.")

    def test_company_update_search_dispatch_and_individual_codex_actions(self):
        job_id = self.create_job()
        company = self.client.post("/api/companies", json={"company": "ExampleCo", "status": "watching"})
        company_id = company.get_json()["company"]["id"]
        updated_company = self.client.post(
            f"/api/companies/{company_id}",
            json={"status": "target", "interest_score": 95, "contacts": "Taylor"},
        )
        jobs = self.dependencies.job_service

        class FakeSearchRunService:
            def run(self, *, trigger, force_refresh):
                self.trigger = trigger
                self.force_refresh = force_refresh
                return {"status": "complete", "found_count": 0}

        class FakeScoringService:
            def score(self, current_job_id):
                return type(
                    "ScoreResult",
                    (),
                    {
                        "state": "scored",
                        "job": jobs.get_job(current_job_id),
                        "raw_score": {"total_score": 89},
                    },
                )()

        class FakePacketService:
            def generate(self, _job_id):
                return {"path": "applications/generated", "name": "generated", "markdown_files": ["Resume.md"]}

        fake_search = FakeSearchRunService()
        client = self.client_with(
            scoring_service=FakeScoringService(),
            search_run_service=fake_search,
            packet_generation_service=FakePacketService(),
        )
        search = client.post("/api/search/run", json={"force_refresh": True})
        score = client.post(f"/api/jobs/{job_id}/score-gpt", json={})
        packet = client.post(f"/api/jobs/{job_id}/application-packet/generate", json={})

        self.assertEqual(updated_company.get_json()["company"]["status"], "target")
        self.assertEqual(updated_company.get_json()["company"]["interest_score"], 95)
        self.assertEqual(search.get_json()["run"]["status"], "complete")
        self.assertEqual((fake_search.trigger, fake_search.force_refresh), ("manual", True))
        self.assertEqual(score.get_json()["raw_score"]["total_score"], 89)
        self.assertEqual(packet.status_code, 201)

    def test_error_routes_cover_disabled_unavailable_and_not_found_responses(self):
        job_id = self.create_job()

        class NoTaskExpected:
            def start(self, *_args):
                raise AssertionError("Unavailable operations must not queue tasks")

        client = self.client_with(
            task_submission_service=TaskSubmissionService(
                NoTaskExpected(), lambda: False, lambda: False, lambda: "missing-codex"
            ),
        )
        disabled_score = client.post(f"/api/jobs/{job_id}/score-gpt", json={})
        disabled_bulk = client.post("/api/jobs/bulk/score-gpt", json={"job_ids": [job_id]})
        unavailable_packet = client.post("/api/jobs/bulk/application-packets/generate", json={"job_ids": [job_id]})
        missing_packet = client.post("/api/jobs/99999/application-packet/generate", json={})

        self.assertEqual(disabled_score.status_code, 409)
        self.assertEqual(disabled_bulk.status_code, 409)
        self.assertEqual(unavailable_packet.status_code, 409)
        self.assertEqual(missing_packet.status_code, 404)

    def test_packet_repository_associates_generated_packet_and_lists_orphans(self):
        job_id = self.create_job()
        generated_dir = self.applications / "generated"
        orphan_dir = self.applications / "orphan"
        generated_dir.mkdir(parents=True)
        orphan_dir.mkdir()
        (generated_dir / "Resume.md").write_text("Resume", encoding="utf-8")
        (orphan_dir / "Job-Brief.md").write_text("Brief", encoding="utf-8")

        class FakeDraft:
            def generate(self, _job):
                return {
                    "path": "applications/generated",
                    "name": "generated",
                    "markdown_files": ["Resume.md"],
                    "codex_output": "{}",
                }

        client = self.client_with(
            packet_generation_service=packet_generation_service(
                self.database, FakeDraft(), self.dependencies.observability.telemetry
            ),
        )
        response = client.post(f"/api/jobs/{job_id}/application-packet/generate", json={})
        duplicate = client.post(f"/api/jobs/{job_id}/application-packet/generate", json={})

        self.assertEqual(response.status_code, 201)
        self.assertEqual(duplicate.status_code, 409, "An associated packet must not be generated again.")
        created = response.get_json()["packet"]
        packets = response.get_json()["application_packets"]
        by_name = {packet["name"]: packet for packet in packets}
        self.assertEqual(created["name"], "generated")
        self.assertEqual(by_name["generated"]["associated_job"]["id"], job_id)
        self.assertTrue(by_name["orphan"]["unassociated"])

    def test_calibration_examples_include_saved_user_feedback(self):
        job_id = self.create_job()
        with database_session(self.database) as connection:
            connection.execute(
                """UPDATE jobs SET gpt_score = 70, user_score = 85, user_rationale = ?, posting_text = ? WHERE id = ?""",
                ("Strategic scope", "A" * 1000, job_id),
            )

            class UnusedGateway:
                def complete(self, *_args, **_kwargs):
                    raise AssertionError("Calibration examples must not invoke Codex")

            scorer = job_score_service(self.dependencies.configuration, UnusedGateway())
            examples = scorer.calibration_examples(connection)

        self.assertEqual(examples[0]["user_score"], 85)
        self.assertIn("...", examples[0]["posting_excerpt"])


if __name__ == "__main__":
    unittest.main()
