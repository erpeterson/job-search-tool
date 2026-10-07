"""Search profile loading, validation, and effect on business rules."""

import copy
import io
import json

import pytest
from conftest import PROFILE_EXAMPLE

from job_search.config import PROFILE_EXAMPLE_PATH, AppConfig
from job_search.data.profile_file import load_search_profile
from job_search.domain.discovery_filters import compensation_filter_decision, location_filter_decision
from job_search.domain.errors import ConfigurationError
from job_search.domain.profile import SearchProfile


@pytest.fixture
def raw_profile():
    return json.loads(PROFILE_EXAMPLE.read_text(encoding="utf-8"))


def test_example_profile_loads(profile):
    assert profile.pipeline_names == ["Executive IC", "Office of the CTO", "Adjacent industries", "Wildcards"], (
        f"profile.pipeline_names did not match; got {profile.pipeline_names!r}"
    )
    assert len(profile.default_search_queries(("linkedin", "indeed"))) == 8, "four pipelines x two boards"


@pytest.mark.parametrize(
    ("mutate", "field"),
    [
        (lambda p: p.pop("candidate_name"), "candidate_name"),
        (lambda p: p.update(pipelines={}), "pipelines"),
        (lambda p: p["pipelines"]["Wildcards"].update(keywords=""), "pipelines.Wildcards.keywords"),
        (lambda p: p.update(min_annual_compensation="lots"), "min_annual_compensation"),
        (lambda p: p.update(downlevel_high_score_exception=500), "downlevel_high_score_exception"),
        (lambda p: p["target_level"].update(below_title_patterns=["(unclosed"]), "below_title_patterns[0]"),
        (lambda p: p["location"].update(us_terms=[]), "location.us_terms"),
        (lambda p: p.update(scoring_instructions=[1]), "scoring_instructions[0]"),
    ],
)
def test_invalid_profiles_name_the_field(raw_profile, mutate, field):
    data = copy.deepcopy(raw_profile)
    mutate(data)
    with pytest.raises(ConfigurationError) as info:
        SearchProfile.from_dict(data)
    assert info.value.error_code == "profile_invalid", info.value.error_code
    assert field in info.value.message, f"message should name {field}: {info.value.message}"


def test_loader_reports_missing_and_malformed_files(tmp_path):
    with pytest.raises(ConfigurationError) as info:
        load_search_profile(tmp_path / "nope.json")
    assert info.value.error_code == "profile_file_missing", info.value.error_code
    bad = tmp_path / "bad.json"
    bad.write_text("{not json", encoding="utf-8")
    with pytest.raises(ConfigurationError) as info:
        load_search_profile(bad)
    assert info.value.error_code == "profile_file_unreadable", info.value.error_code


def test_profile_path_resolution(tmp_path):
    app_dir = tmp_path / "app"
    config = AppConfig.from_env({}, app_dir=app_dir)
    assert config.profile_path == PROFILE_EXAMPLE_PATH, "without a workspace profile, the shipped example is used"
    workspace_profile = tmp_path / "job-search-profile.json"
    workspace_profile.write_text("{}", encoding="utf-8")
    config = AppConfig.from_env({}, app_dir=app_dir)
    assert config.profile_path == workspace_profile.resolve(), "a workspace profile takes precedence"
    config = AppConfig.from_env({"JOB_SEARCH_PROFILE_PATH": "custom.json"}, app_dir=app_dir)
    assert config.profile_path == (app_dir / "custom.json").resolve(), "an explicit path is used as configured"


def test_cli_exits_2_for_invalid_profile(workspace, capsys):
    from job_search import cli

    app_dir = workspace / "job-search-tool"
    (workspace / "job-search-profile.json").write_text('{"candidate_name": "X"}', encoding="utf-8")
    code = cli.main([], environ={}, serve=lambda *a: None, out=io.StringIO(), app_dir=app_dir)
    assert code == cli.EXIT_CONFIG_ERROR, f"invalid profile must exit 2, got {code}"
    assert "Search profile" in capsys.readouterr().err, "stderr should explain the profile problem"


def test_custom_profile_changes_rules(raw_profile):
    data = copy.deepcopy(raw_profile)
    data["location"].update(home_metro_label="Austin", home_metro_terms=["austin"])
    data["min_annual_compensation"] = 100000
    profile = SearchProfile.from_dict(data)
    allowed, reason = location_filter_decision({"location": "Austin, TX"}, profile)
    assert allowed and reason == "Austin-based or Austin-area role", reason
    assert not location_filter_decision({"location": "Seattle, WA"}, profile)[0], "the old metro no longer matches"
    assert compensation_filter_decision({"snippet": "$150,000 per year"}, profile)[0], "the lower floor applies"


def _build(config, environ):
    from conftest import FakeResolver

    from job_search.container import build_container

    return build_container(config, environ=environ, resolve_host=FakeResolver())


def test_example_profile_logs_a_warning_and_flags_the_ui(workspace, environ):
    import logging

    from job_search.observability import configure_console_logging

    out = io.StringIO()
    configure_console_logging(verbose=True, stdout=out, stderr=io.StringIO())
    config = AppConfig.from_env(environ, app_dir=workspace / "job-search-tool")
    container = _build(config, environ)
    events = [json.loads(line) for line in out.getvalue().splitlines() if "search_profile_using_example" in line]
    assert container.profile_is_example, "the shipped example profile should be flagged"
    assert events and events[0]["level"] == logging.getLevelName(logging.WARNING), f"expected a warning: {events}"


def test_workspace_profile_is_not_flagged(workspace, environ):
    (workspace / "job-search-profile.json").write_text(PROFILE_EXAMPLE.read_text(), encoding="utf-8")
    config = AppConfig.from_env(environ, app_dir=workspace / "job-search-tool")
    assert _build(config, environ).profile_is_example is False, "a real workspace profile is not flagged"


def test_state_reports_example_profile(client):
    assert client.get("/api/state").get_json()["profile_is_example"] is True, "the UI banner needs the flag"


def test_legacy_level_columns_are_kept_and_exposed_under_domain_names(tmp_path):
    """Released ``oracle_*`` columns stay in place (rollback-safe); the repository maps them to ``target_*``."""
    import sqlite3

    from job_search.data.database import Database

    path = tmp_path / "old.sqlite3"
    conn = sqlite3.connect(path)
    conn.execute(
        "CREATE TABLE level_equivalencies (id INTEGER PRIMARY KEY, company TEXT NOT NULL, normalized_company TEXT"
        " NOT NULL, title_pattern TEXT NOT NULL, normalized_title_pattern TEXT NOT NULL, source_level TEXT,"
        " source_level_title TEXT, oracle_level TEXT NOT NULL, oracle_title TEXT NOT NULL, downlevel INTEGER NOT"
        " NULL, source_url TEXT, notes TEXT, created_at INTEGER NOT NULL, updated_at INTEGER NOT NULL,"
        " UNIQUE(normalized_company, normalized_title_pattern))"
    )
    conn.execute(
        "INSERT INTO level_equivalencies(company, normalized_company, title_pattern, normalized_title_pattern,"
        " oracle_level, oracle_title, downlevel, created_at, updated_at) VALUES ('Co','co','T','t','IC6+','Arch',0,1,1)"
    )
    conn.commit()
    conn.close()
    database = Database(path)
    database.create_schema()
    with database.unit_of_work() as uow:
        stored = {row[1] for row in uow.connection.execute("PRAGMA table_info(level_equivalencies)")}
        rows = uow.levels.for_company("co")
        uow.levels.upsert(
            {**{k: rows[0][k] for k in rows[0] if k not in ("id", "created_at", "updated_at")}, "target_level": "IC7"},
            2,
        )
        updated = uow.levels.for_company("co")[0]
    assert {"oracle_level", "oracle_title"} <= stored, "the stored column names must not change"
    assert not {"target_level", "target_title"} & stored, "no new physical columns, so earlier versions keep working"
    assert (rows[0]["target_level"], rows[0]["target_title"]) == ("IC6+", "Arch")
    assert "oracle_level" not in rows[0], "legacy names do not leak past the repository"
    assert (updated["target_level"], updated["target_title"]) == ("IC7", "Arch"), "upsert writes the stored columns"
