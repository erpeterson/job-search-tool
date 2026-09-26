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
