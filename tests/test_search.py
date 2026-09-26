"""Saved-search runs: filtering, deduplication, tracking, scoring, refinement, and scheduling."""

import json

import requests
from conftest import SCORE_RESPONSE, insert_job

from job_search.domain.scheduler import SearchScheduler


def linkedin_card(title, company, location, job_id, snippet=""):
    return f"""
    <li><a class="base-card__full-link" href="https://www.linkedin.com/jobs/view/{job_id}?trk=x">link</a>
      <h3 class="base-search-card__title">{title}</h3>
      <h4 class="base-search-card__subtitle">{company}</h4>
      <span class="job-search-card__location">{location}</span>{snippet}</li>
    """


LINKEDIN_RESULTS = (
    "<ul>"
    + "".join(
        [
            linkedin_card("Chief Architect", "Acme", "Seattle, WA", 1),
            linkedin_card("Account Executive", "SalesCo", "Remote", 2),
            linkedin_card("Principal Architect", "EuroCo", "Berlin, Germany", 3),
            linkedin_card("Distinguished Engineer", "CheapCo", "Remote", 4, "Pay $120,000 - $150,000 per year"),
            linkedin_card("Senior Software Engineer", "BigCo", "Remote - United States", 5),
        ]
    )
    + "</ul>"
)


def enable_only(container, board, pipeline):
    with container.db.unit_of_work() as uow:
        uow.connection.execute("UPDATE search_queries SET enabled = 0")
        uow.connection.execute(
            "UPDATE search_queries SET enabled = 1 WHERE board = ? AND pipeline = ?", (board, pipeline)
        )


def run_search(client, force_refresh=False):
    response = client.post(
        "/api/search/run", data=json.dumps({"force_refresh": force_refresh}), content_type="application/json"
    )
    assert response.status_code == 200, response.get_json()
    return response.get_json()


def test_run_filters_rejections_and_tracks_without_codex(client, container, http):
    enable_only(container, "linkedin", "Executive IC")
    http.route("linkedin.com/jobs-guest", LINKEDIN_RESULTS)

    body = run_search(client)

    run = body["run"]
    assert (run["found_count"], run["tracked_count"], run["rejected_count"]) == (5, 2, 3)
    decisions = {d["company"]: (d["decision"], d["rejection_reason"] or "") for d in body["discoveries"]}
    assert decisions["SalesCo"][0] == "rejected" and "Sales role" in decisions["SalesCo"][1]
    assert "Location" in decisions["EuroCo"][1]
    assert "compensation" in decisions["CheapCo"][1]
    jobs = {job["company"]: job for job in body["jobs"]}
    assert jobs["Acme"]["url"] == "https://www.linkedin.com/jobs/view/1", "tracking suffix stripped"
    assert jobs["BigCo"]["downlevel"] == 1, "cached title taxonomy marks senior SWE as downlevel"
    assert jobs["BigCo"]["filtered"] == 1, "downlevel discoveries are hidden by default"
    assert "Codex scoring is disabled" in jobs["Acme"]["notes"]


def test_rerun_skips_already_tracked_and_replays_capture(client, container, http):
    enable_only(container, "linkedin", "Executive IC")
    http.route("linkedin.com/jobs-guest", LINKEDIN_RESULTS)
    run_search(client)
    second = run_search(client)["run"]
    assert second["tracked_count"] == 0, "previously tracked URLs are skipped"
    assert len(http.calls) == 1, "the second run replays the captured response instead of refetching"
    run_search(client, force_refresh=True)
    assert len(http.calls) == 2, "force refresh bypasses the capture cache"


def test_board_failure_is_reported_and_run_completes(client, container, http):
    enable_only(container, "indeed", "Wildcards")
    http.route("indeed.com", error=requests.ConnectionError("connection reset"))
    run = run_search(client)["run"]
    assert run["status"] == "complete"
    assert "connection reset" in run["message"]


def test_scored_discovery_and_query_refinement(client, container, http, codex_runner, enable_scoring):
    enable_only(container, "linkedin", "Office of the CTO")
    http.route("linkedin.com/jobs-guest", "<ul>" + linkedin_card("Chief Architect", "Acme", "Seattle", 7) + "</ul>")
    codex_runner.respond(SCORE_RESPONSE)
    codex_runner.respond({"keywords": "Office of the CTO architect", "refinement_notes": "narrowed"})

    body = run_search(client)

    job = body["jobs"][0]
    assert (job["gpt_score"], job["pipeline"], job["level_assessment"]) == (85, "Executive IC", "IC6-equivalent")
    assert body["discoveries"][0]["gpt_score"] == 85
    queries = client.get("/api/state").get_json()["search_queries"]
    refined = next(q for q in queries if q["enabled"])
    assert refined["keywords"] == "Office of the CTO architect"
    assert refined["refinement_notes"] == "narrowed"


def test_refinement_failure_is_reported_not_fatal(client, container, http, codex_runner, enable_scoring):
    enable_only(container, "linkedin", "Office of the CTO")
    http.route("linkedin.com/jobs-guest", "<ul>" + linkedin_card("Chief Architect", "Acme", "Seattle", 8) + "</ul>")
    codex_runner.respond(SCORE_RESPONSE)
    codex_runner.respond("", returncode=2)

    run = run_search(client)["run"]
    assert run["tracked_count"] == 1
    assert "exited with code 2" in run["message"]


def test_refinement_ignores_invalid_json(container, http, codex_runner, enable_scoring):
    enable_only(container, "linkedin", "Wildcards")
    query_id = next(q["id"] for q in container.search.list_queries() if q["enabled"])
    with container.db.unit_of_work() as uow:
        uow.discoveries.insert(
            {"run_id": None, "query_id": query_id, "created_at": 1, "board": "linkedin", "decision": "tracked"}
        )
    before = container.search.list_queries()
    codex_runner.respond("nonsense")
    container.search.refine_query(query_id)
    assert container.search.list_queries() == before, "invalid refinement output leaves the query unchanged"


def test_search_query_crud(client):
    created = client.post(
        "/api/search/queries",
        data=json.dumps({"board": "indeed", "keywords": "Chief Architect", "pipeline": "Wildcards"}),
        content_type="application/json",
    )
    assert created.status_code == 201
    query = next(q for q in created.get_json()["search_queries"] if q["keywords"] == "Chief Architect")
    toggled = client.post(
        f"/api/search/queries/{query['id']}", data=json.dumps({"enabled": False}), content_type="application/json"
    ).get_json()["search_queries"]
    assert next(q for q in toggled if q["id"] == query["id"])["enabled"] == 0
    bad = client.post(
        "/api/search/queries", data=json.dumps({"board": "monster", "keywords": "x"}), content_type="application/json"
    )
    assert bad.status_code == 400
    missing = client.post("/api/search/queries/9999", data="{}", content_type="application/json")
    assert missing.status_code == 404


def test_seeding_is_idempotent(container):
    container.bootstrap()
    assert len(container.search.list_queries()) == 8, "re-running bootstrap must not duplicate seeded queries"


def test_level_equivalency_cache_is_populated_without_network(container):
    from job_search.domain.levels import lookup_level_equivalency

    with container.db.unit_of_work() as uow:
        assert uow.levels.count() == 0, "no level equivalencies are seeded"
        assert lookup_level_equivalency(uow.levels, "Atlassian", "Principal Engineer") is None
        first = lookup_level_equivalency(uow.levels, "ExampleCo", "Senior Software Engineer")
        second = lookup_level_equivalency(uow.levels, "ExampleCo", "Senior Software Engineer II")
        assert uow.levels.count() == 1, "ambiguous titles are not cached; matching prefixes reuse the cache"
    assert first["oracle_level"] == second["oracle_level"] == "BELOW_IC6"


class TestScheduler:
    def test_first_tick_records_baseline_only(self, container):
        calls = []
        scheduler = SearchScheduler(container.db, type("S", (), {"run": lambda *a, **k: calls.append(k)})(), 60, True)
        scheduler.tick()
        assert calls == [], "the first tick only records a baseline"
        assert scheduler.state()["last_search_at"] > 0
        assert scheduler.state()["next_run_at"] is not None

    def test_due_tick_runs_forced_search(self, container):
        calls = []
        with container.db.unit_of_work() as uow:
            uow.settings.set("last_search_at", 1)
        scheduler = SearchScheduler(container.db, type("S", (), {"run": lambda *a, **k: calls.append(k)})(), 60, True)
        scheduler.tick()
        assert calls == [{"trigger": "scheduled", "force_refresh": True}]

    def test_failed_tick_records_error_run(self, container):
        def fail(**_kwargs):
            raise RuntimeError("board down")

        with container.db.unit_of_work() as uow:
            uow.settings.set("last_search_at", 1)
        scheduler = SearchScheduler(container.db, type("S", (), {"run": staticmethod(fail)})(), 60, True)
        scheduler.tick()
        runs = container.search.list_runs()
        assert runs[0]["status"] == "error"
        assert "board down" not in runs[0]["message"], "details stay in logs"

    def test_disabled_scheduler_does_not_start(self, container):
        assert container.scheduler.start() is None
        assert container.scheduler.state()["next_run_at"] is None


def test_insert_job_helper_marks_existing_urls(container):
    insert_job(container, url="https://www.linkedin.com/jobs/view/1")
    with container.db.unit_of_work() as uow:
        assert uow.jobs.find_id_by_url("https://www.linkedin.com/jobs/view/1") is not None
        assert uow.jobs.find_id_by_url("") is None
