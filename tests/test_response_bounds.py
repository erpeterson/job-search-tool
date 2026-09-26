"""List endpoints stay bounded: summaries only, capped rows, and no side effects on GET."""

import shutil

STATE_BUDGET_BYTES = 1_000_000  # documented in README: /api/state stays under 1 MB for 1,000 jobs


def seed_jobs(container, count, posting_chars=50_000):
    posting = "x" * posting_chars
    rows = [
        (
            1,
            n,
            f"Company {n}",
            f"Principal Architect {n}",
            f"https://example.com/jobs/{n}",
            "Wildcards",
            "researching",
            posting,
            "note " * 200,
            "rationale " * 200,
        )
        for n in range(count)
    ]
    with container.db.unit_of_work() as uow:
        uow.connection.executemany(
            "INSERT INTO jobs(created_at, updated_at, company, title, url, pipeline, status, posting_text, notes,"
            " gpt_rationale) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            rows,
        )


def test_state_payload_for_1000_jobs_is_within_budget(client, container):
    seed_jobs(container, 1000)
    response = client.get("/api/state?include_filtered=1")
    body = response.get_json()
    assert response.status_code == 200, f"state should load: {response.status_code}"
    assert len(body["jobs"]) == 1000 and body["jobs_total"] == 1000, "all 1,000 summaries fit under the default limit"
    assert len(response.data) < STATE_BUDGET_BYTES, f"/api/state is {len(response.data)} bytes, over budget"
    assert "posting_text" not in body["jobs"][0], "list rows must not carry full posting text"


def test_job_listing_honors_limit_and_offset(client, container):
    seed_jobs(container, 5, posting_chars=10)
    page = client.get("/api/state?include_filtered=1&limit=2&offset=1").get_json()
    assert [job["company"] for job in page["jobs"]] == ["Company 3", "Company 2"], page["jobs"]
    assert page["jobs_total"] == 5, "the total reflects all jobs, not the page"
    response = client.get("/api/state?limit=999999")
    assert response.status_code == 400, f"limits above the maximum must be rejected, got {response.status_code}"


def test_full_details_come_from_the_job_endpoint(client, container):
    seed_jobs(container, 1, posting_chars=20)
    job_id = client.get("/api/state?include_filtered=1").get_json()["jobs"][0]["id"]
    job = client.get(f"/api/jobs/{job_id}").get_json()["job"]
    assert job["posting_text"] == "x" * 20, "the detail endpoint still returns the full posting"


def test_get_state_has_no_filesystem_side_effects(client, container, workspace):
    applications = workspace / "applications"
    assert applications.is_dir(), "bootstrap creates the applications directory"
    shutil.rmtree(applications)
    response = client.get("/api/state")
    assert response.status_code == 200, f"state should load without the directory: {response.status_code}"
    assert not applications.exists(), "a GET must not create directories"
    assert response.get_json()["application_packets"] == [], "a missing directory means no packets"
