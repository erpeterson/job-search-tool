"""Saved-search runs: filtering, deduplication, tracking, scoring, refinement, and scheduling."""

import json

import requests
from conftest import SCORE_RESPONSE, insert_job, wait_for_task

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
    assert response.status_code == 202, response.get_json()
    task = wait_for_task(client, response.get_json()["task"])
    assert task["status"] == "complete", f"search task failed: {task}"
    state = client.get("/api/state?include_filtered=1").get_json()
    return {"run": task["result"]["run"], "jobs": state["jobs"], "discoveries": state["discoveries"]}


def test_run_filters_rejections_and_tracks_without_codex(client, container, http):
    enable_only(container, "linkedin", "Executive IC")
    http.route("linkedin.com/jobs-guest", LINKEDIN_RESULTS)

    body = run_search(client)

    run = body["run"]
    assert (run["found_count"], run["tracked_count"], run["rejected_count"]) == (5, 2, 3), (
        "expected the result to be (5, 2, 3)"
    )
    decisions = {d["company"]: (d["decision"], d["rejection_reason"] or "") for d in body["discoveries"]}
    assert decisions["SalesCo"][0] == "rejected", f"expected 'rejected', got {decisions['SalesCo'][0]!r}"
    assert "Sales role" in decisions["SalesCo"][1], f"expected 'Sales role' in {decisions['SalesCo'][1]!r}"
    assert "Location" in decisions["EuroCo"][1], f"expected 'Location' in {decisions['EuroCo'][1]!r}"
    assert "compensation" in decisions["CheapCo"][1], f"expected 'compensation' in {decisions['CheapCo'][1]!r}"
    jobs = {job["company"]: job for job in body["jobs"]}
    assert jobs["Acme"]["url"] == "https://www.linkedin.com/jobs/view/1", "tracking suffix stripped"
    assert jobs["BigCo"]["downlevel"] == 1, "cached title taxonomy marks senior SWE as downlevel"
    assert jobs["BigCo"]["filtered"] == 1, "downlevel discoveries are hidden by default"
    acme_notes = client.get(f"/api/jobs/{jobs['Acme']['id']}").get_json()["job"]["notes"]
    assert "Codex scoring is disabled" in acme_notes, f"notes should explain the missing score: {acme_notes}"


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
    assert run["status"] == "complete", f"expected 'complete', got {run['status']!r}"
    assert "search_board_fetch_failed" in run["message"], run["message"]
    assert "connection reset" not in run["message"], "raw exception text must not be persisted"


def test_scored_discovery_and_query_refinement(client, container, http, codex_runner, enable_scoring):
    enable_only(container, "linkedin", "Office of the CTO")
    http.route("linkedin.com/jobs-guest", "<ul>" + linkedin_card("Chief Architect", "Acme", "Seattle", 7) + "</ul>")
    codex_runner.respond(SCORE_RESPONSE)
    codex_runner.respond({"keywords": "Office of the CTO architect", "refinement_notes": "narrowed"})

    body = run_search(client)

    job = body["jobs"][0]
    assert (job["gpt_score"], job["pipeline"], job["level_assessment"]) == (85, "Executive IC", "IC6-equivalent"), (
        "expected the result to be (85, 'Executive IC', 'IC6-equivalent')"
    )
    assert body["discoveries"][0]["gpt_score"] == 85, f"expected 85, got {body['discoveries'][0]['gpt_score']!r}"
    queries = client.get("/api/state").get_json()["search_queries"]
    refined = next(q for q in queries if q["enabled"])
    assert refined["keywords"] == "Office of the CTO architect", (
        f"expected 'Office of the CTO architect', got {refined['keywords']!r}"
    )
    assert refined["refinement_notes"] == "narrowed", f"expected 'narrowed', got {refined['refinement_notes']!r}"


def test_refinement_failure_is_reported_not_fatal(client, container, http, codex_runner, enable_scoring):
    enable_only(container, "linkedin", "Office of the CTO")
    http.route("linkedin.com/jobs-guest", "<ul>" + linkedin_card("Chief Architect", "Acme", "Seattle", 8) + "</ul>")
    codex_runner.respond(SCORE_RESPONSE)
    codex_runner.respond("", returncode=2)

    run = run_search(client)["run"]
    assert run["tracked_count"] == 1, f"expected 1, got {run['tracked_count']!r}"
    assert "query refinement failed (codex_cli_nonzero_exit)" in run["message"], run["message"]


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
    assert created.status_code == 201, (
        f"expected HTTP 201, got {created.status_code}: {created.get_data(as_text=True)[:200]}"
    )
    query = next(q for q in created.get_json()["search_queries"] if q["keywords"] == "Chief Architect")
    toggled = client.post(
        f"/api/search/queries/{query['id']}", data=json.dumps({"enabled": False}), content_type="application/json"
    ).get_json()["search_queries"]
    assert next(q for q in toggled if q["id"] == query["id"])["enabled"] == 0, "expected next(...)[...] to be 0"
    bad = client.post(
        "/api/search/queries", data=json.dumps({"board": "monster", "keywords": "x"}), content_type="application/json"
    )
    assert bad.status_code == 400, f"expected HTTP 400, got {bad.status_code}: {bad.get_data(as_text=True)[:200]}"
    missing = client.post("/api/search/queries/9999", data="{}", content_type="application/json")
    assert missing.status_code == 404, (
        f"expected HTTP 404, got {missing.status_code}: {missing.get_data(as_text=True)[:200]}"
    )


def test_seeding_is_idempotent(container):
    container.bootstrap()
    assert len(container.search.list_queries()) == 8, "re-running bootstrap must not duplicate seeded queries"


def test_level_equivalency_cache_is_populated_without_network(container):
    from job_search.domain.levels import LevelCalibrationCache

    target = container.profile.target_level
    with container.db.unit_of_work() as uow:
        assert uow.levels.count() == 0, "no level equivalencies are seeded"
        cache = LevelCalibrationCache(uow.levels, target, ["Atlassian", "ExampleCo"])
        ambiguous = cache.lookup("Atlassian", "Principal Engineer")
        assert ambiguous is None, f"an ambiguous title should stay unknown; got {ambiguous}"
        first = cache.lookup("ExampleCo", "Senior Software Engineer")
        second = cache.lookup("ExampleCo", "Senior Software Engineer II")
        assert uow.levels.count() == 1, "ambiguous titles are not cached; matching prefixes reuse the cache"
        preloaded = LevelCalibrationCache(uow.levels, target, ["ExampleCo"]).lookup("ExampleCo", "Staff Engineer")
    levels = {first["oracle_level"], second["oracle_level"]}
    assert levels == {"BELOW_IC6"}, f"both titles map below the target level: {levels}"
    assert preloaded["oracle_level"] == "BELOW_IC6", "a new cache instance estimates an uncached title"


class TestScheduler:
    def test_first_tick_records_baseline_only(self, container):
        calls = []
        scheduler = SearchScheduler(container.db, type("S", (), {"run": lambda *a, **k: calls.append(k)})(), 60, True)
        scheduler.tick()
        assert calls == [], "the first tick only records a baseline"
        assert scheduler.state()["last_search_at"] > 0, "expected scheduler.state()['last_search_at'] to be > 0"
        assert scheduler.state()["next_run_at"] is not None, "expected scheduler.state()['next_run_at'] to be not None"

    def test_due_tick_runs_forced_search(self, container):
        calls = []
        with container.db.unit_of_work() as uow:
            uow.settings.set("last_search_at", 1)
        scheduler = SearchScheduler(container.db, type("S", (), {"run": lambda *a, **k: calls.append(k)})(), 60, True)
        scheduler.tick()
        assert calls == [{"trigger": "scheduled", "force_refresh": True}], f"calls did not match; got {calls!r}"

    def test_failed_tick_is_recorded_without_extra_run_rows(self, container):
        from job_search.observability import METRICS

        def fail(**_kwargs):
            raise RuntimeError("board down")

        with container.db.unit_of_work() as uow:
            uow.settings.set("last_search_at", 1)
        scheduler = SearchScheduler(container.db, type("S", (), {"run": staticmethod(fail)})(), 60, True)
        before = METRICS.snapshot().get("blame.scheduled_search_failed", 0)
        scheduler.tick()
        assert METRICS.snapshot()["blame.scheduled_search_failed"] == before + 1, "tick failure must be recorded"
        assert container.search.list_runs() == [], "the scheduler no longer inserts its own error rows"

    def test_disabled_scheduler_does_not_start(self, container):
        assert container.scheduler.start() is None, "expected container.scheduler.start() to be None"
        assert container.scheduler.state()["next_run_at"] is None, (
            "expected container.scheduler.state()['next_run_at'] to be None"
        )


def test_insert_job_helper_marks_existing_urls(container):
    insert_job(container, url="https://www.linkedin.com/jobs/view/1")
    with container.db.unit_of_work() as uow:
        assert uow.jobs.find_id_by_url("https://www.linkedin.com/jobs/view/1") is not None, (
            "expected uow.jobs.find_id_by_url(...) to be not None"
        )
        assert uow.jobs.find_id_by_url("") is None, "expected uow.jobs.find_id_by_url('') to be None"


def test_scoring_failure_for_one_result_does_not_abort_run(client, container, http, codex_runner, enable_scoring):
    from job_search.observability import METRICS

    enable_only(container, "linkedin", "Office of the CTO")
    cards = linkedin_card("Chief Architect", "Acme", "Seattle", 21) + linkedin_card("Fellow", "Beta", "Seattle", 22)
    http.route("linkedin.com/jobs-guest", "<ul>" + cards + "</ul>")
    codex_runner.respond("", returncode=1)  # first result: Codex fails
    codex_runner.respond(SCORE_RESPONSE)  # second result: scored
    codex_runner.respond({"keywords": "kept"})  # refinement
    before = METRICS.snapshot().get("blame.search_result_scoring_failed", 0)

    body = run_search(client)

    run = body["run"]
    assert (run["status"], run["tracked_count"]) == ("complete", 2), f"run should complete and track both: {run}"
    scores = {job["company"]: job["gpt_score"] for job in body["jobs"]}
    assert scores == {"Acme": None, "Beta": 85}, f"failed result is tracked unscored, other scored: {scores}"
    assert "search_result_scoring_failed" not in run["message"], (
        f"expected 'search_result_scoring_failed' not in {run['message']!r}"
    )
    assert "codex_cli_nonzero_exit" in run["message"], f"expected 'codex_cli_nonzero_exit' in {run['message']!r}"
    assert METRICS.snapshot()["blame.search_result_scoring_failed"] == before + 1, "failure must be recorded"


def test_unexpected_run_failure_marks_run_as_error(container, http, monkeypatch):
    import pytest

    enable_only(container, "linkedin", "Executive IC")
    http.route("linkedin.com/jobs-guest", LINKEDIN_RESULTS)

    def explode(*_args, **_kwargs):
        raise RuntimeError("database vanished")

    monkeypatch.setattr(container.search, "_track_result", explode)
    with pytest.raises(RuntimeError):
        container.search.run()
    run = container.search.list_runs()[0]
    assert run["status"] == "error", f"a failed run must not stay running: {run}"
    assert "database vanished" not in run["message"], "internal details stay in logs"
    assert run["completed_at"] is not None, "failed runs get a completion time"


def test_calibration_query_runs_once_per_search_run(client, container, http, codex_runner, enable_scoring, monkeypatch):
    enable_only(container, "linkedin", "Office of the CTO")
    cards = "".join(linkedin_card(f"Chief Architect {n}", f"Co{n}", "Seattle", 300 + n) for n in range(3))
    http.route("linkedin.com/jobs-guest", f"<ul>{cards}</ul>")
    for _ in range(3):
        codex_runner.respond(SCORE_RESPONSE)
    codex_runner.respond({"keywords": "kept"})
    statements = []
    original_connect = container.db._connect

    def counting_connect():
        conn = original_connect()
        conn.set_trace_callback(statements.append)
        return conn

    monkeypatch.setattr(container.db, "_connect", counting_connect)
    run = run_search(client)["run"]

    assert run["tracked_count"] == 3, f"all three results should be tracked: {run}"
    calibration = [s for s in statements if "WHERE user_score IS NOT NULL" in s]
    assert len(calibration) == 1, f"calibration examples must load once per run, not per result: {len(calibration)}"
    url_checks = [s for s in statements if "FROM jobs WHERE url" in s]
    assert len(url_checks) == 1, f"tracked-URL checks must be batched per query: {url_checks}"


def test_company_list_join_uses_normalized_company_index(container):
    from job_search.data.repositories import COMPANY_LIST_SQL

    with container.db.unit_of_work() as uow:
        plan = uow.connection.execute(f"EXPLAIN QUERY PLAN {COMPANY_LIST_SQL}").fetchall()
    details = " ".join(row["detail"] for row in plan)
    assert "idx_jobs_normalized_company" in details, f"the repository's company query must use the index: {details}"


def test_company_matching_ignores_case_and_punctuation(client, container):
    insert_job(container, company="Acme, Inc.", url="https://example.com/a")
    insert_job(container, company="ACME INC", url="https://example.com/b")
    insert_job(container, company="Acme Industries", url="https://example.com/c")
    body = client.post(
        "/api/companies", data=json.dumps({"company": "Acme Inc"}), content_type="application/json"
    ).get_json()
    titles = sorted(job["company"] for job in body["company"]["jobs"])
    assert titles == ["ACME INC", "Acme, Inc."], f"normalized names match; other companies do not: {titles}"
    listed = next(c for c in body["companies"] if c["company"] == "Acme Inc")
    assert listed["tracked_job_count"] == 2, f"the list count uses the same matching: {listed}"


def test_normalized_company_is_backfilled_for_existing_rows(container):
    with container.db.unit_of_work() as uow:
        uow.connection.execute(
            "INSERT INTO jobs(created_at, updated_at, company, title) VALUES (1, 1, 'Acme, Inc.', 'Architect')"
        )
    container.db.create_schema()
    with container.db.unit_of_work() as uow:
        value = uow.connection.execute("SELECT normalized_company FROM jobs").fetchone()[0]
    assert value == "acme inc", f"rows written before the migration must be backfilled: {value!r}"


def test_second_search_run_is_rejected_while_one_is_running(container, config, environ, http, codex_runner, profile):
    import json as _json

    from conftest import FakeResolver, make_client

    from job_search.container import build_container

    class DeferredThread:
        def __init__(self, target, args=(), daemon=None, name=None):
            pass

        def start(self):
            pass

    deferred = build_container(
        config,
        environ=environ,
        http_get=http,
        codex_runner=codex_runner,
        thread_factory=DeferredThread,
        resolve_host=FakeResolver(),
        profile=profile,
    )
    client = make_client(deferred)
    first = client.post("/api/search/run", data="{}", content_type="application/json")
    assert first.status_code == 202, f"the first run should start: {first.get_json()}"
    second = client.post("/api/search/run", data=_json.dumps({}), content_type="application/json")
    body = second.get_json()
    assert second.status_code == 409, f"a concurrent run must be rejected, got {second.status_code}: {body}"
    assert body["task"]["id"] == first.get_json()["task"]["id"], "the 409 body points at the running task"
    assert "search_run" in body["error"], body["error"]


def test_url_tracked_between_screening_and_tracking_is_skipped(client, container, http, monkeypatch):
    from job_search.observability import METRICS

    enable_only(container, "linkedin", "Executive IC")
    http.route("linkedin.com/jobs-guest", "<ul>" + linkedin_card("Chief Architect", "Acme", "Seattle", 901) + "</ul>")
    original = container.search._track_result

    def race(run_id, query_id, result, *args):
        insert_job(container, url=result["url"])  # a manual add lands after screening
        return original(run_id, query_id, result, *args)

    monkeypatch.setattr(container.search, "_track_result", race)
    before = METRICS.snapshot().get("blame.search_result_already_tracked", 0)
    run = run_search(client)["run"]
    assert run["status"] == "complete", f"the run must complete despite the duplicate: {run}"
    assert run["tracked_count"] == 0, f"the raced result is skipped, not tracked: {run}"
    assert METRICS.snapshot()["blame.search_result_already_tracked"] == before + 1, "the skip is recorded"
