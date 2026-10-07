"""CV structure rules, model attribution, packet-rule extraction and the profile's optional ``cv`` section."""

import pytest
from conftest import valid_cv

from job_search.data.documents import CareerDocuments
from job_search.domain.cv_quality import add_model_attribution, cv_style_issues
from job_search.domain.errors import ConfigurationError
from job_search.domain.profile import CvRules, SearchProfile


def test_valid_cv_has_no_issues(profile):
    assert cv_style_issues(valid_cv(), profile.cv) == [], "the reference CV should pass the example profile's rules"


@pytest.mark.parametrize(
    ("mutate", "expected"),
    [
        (lambda cv: cv.replace("## Education", "## Schooling"), "missing required section: Education"),
        (lambda cv: cv.replace("Curriculum Vitae", "Resume"), "Curriculum Vitae document-title"),
        (lambda cv: cv.replace("#### ", "### "), "thematic subsections"),
        (lambda cv: cv.replace("LION Inc.", "an early employer"), "must preserve: LION Inc."),
        (lambda cv: cv + "\nBuilt from source material.", "provenance or placeholder phrase: source material"),
        (lambda cv: cv + "\nWorld-Wide Technology Solutions, 1999.", "documented august 2000"),
        (lambda cv: cv[:200], "too short"),
    ],
)
def test_cv_rules_flag_problems(profile, mutate, expected):
    issues = cv_style_issues(mutate(valid_cv()), profile.cv)
    assert any(expected in issue for issue in issues), f"expected an issue containing {expected!r}, got {issues}"


def test_redacted_employers_and_duplicate_bullets_are_flagged(profile):
    master = "## Secret Corp (redact)\n\nDetails"
    cv = valid_cv(
        "\nSecret Corp led the work.\n- Designed the shared platform architecture for many teams worldwide\n"
        "- Designed the shared platform architecture for many teams worldwide today\n"
    )
    issues = cv_style_issues(cv, profile.cv, master)
    assert any("redacted employer: Secret Corp" in issue for issue in issues), issues
    assert any("duplicate bullets" in issue for issue in issues), issues


def test_attribution_replaces_existing_section():
    text = add_model_attribution(
        "# Doc\n\nBody\n\n## AI Generation Attribution\n\nold\n\n## Next\n\nKept", "m1", "2026-10-07"
    )
    assert text.count("## AI Generation Attribution") == 1 and "using `m1`" in text and "old" not in text, text
    assert "## Next" in text, "other sections survive"


def test_packet_rules_stop_before_interview_stories(tmp_path):
    manual = tmp_path / "manual.md"
    manual.write_text(
        "# Intro\nx\n# Downstream Artifact Rules\nRule one.\n## Interview Stories\nStory.\n# Open Questions\nq\n"
    )
    rules = CareerDocuments(manual, tmp_path / "guidance.md", tmp_path / "resume.md").application_packet_rules()
    assert rules == "# Downstream Artifact Rules\nRule one.", f"unexpected rules: {rules!r}"


def test_profile_cv_section_is_optional_and_validated(profile):
    import json
    from pathlib import Path

    raw = json.loads((Path(__file__).resolve().parent.parent / "profile.example.json").read_text())
    del raw["cv"]
    assert SearchProfile.from_dict(raw).cv == CvRules(), "omitting cv keeps the defaults"
    raw["cv"] = {"min_words": "many"}
    with pytest.raises(ConfigurationError, match="cv.min_words"):
        SearchProfile.from_dict(raw)
