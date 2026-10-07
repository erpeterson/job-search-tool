"""Pure business rules: text normalization, discovery filters, job visibility, and levels."""

import pytest

from job_search.domain.discovery_filters import (
    compensation_filter_decision,
    extract_annual_compensation_values,
    first_rejection,
    location_filter_decision,
    sales_role_filter_decision,
)
from job_search.domain.errors import ExternalServiceError
from job_search.domain.job_filter import filter_decision, thresholds
from job_search.domain.levels import estimate_level_equivalency, level_assessment_from_equivalency
from job_search.domain.packets import application_packet_slug, validate_packet_payload
from job_search.domain.scoring import score_total
from job_search.domain.text import (
    append_note_text,
    clean_url,
    dedupe_results,
    normalize_lookup_text,
    normalize_pipeline,
    with_sales_role_exclusion_criteria,
    with_sales_role_exclusion_keywords,
)


class TestText:
    def test_clean_url_strips_linkedin_tracking(self):
        assert clean_url(" https://x.com/job?trk=abc ") == "https://x.com/job", "tracking suffix should be removed"

    def test_normalize_lookup_text_collapses_punctuation(self):
        assert normalize_lookup_text("Sr. Principal—Engineer!") == "sr principal engineer", (
            "expected normalize_lookup_text('Sr. Principal—Engineer!') to be 'sr principal engineer'"
        )

    @pytest.mark.parametrize(
        ("existing", "addition", "expected"),
        [("", "new", "new"), ("old", "", "old"), ("old  text", "new", "old text new")],
    )
    def test_append_note_text(self, existing, addition, expected):
        assert append_note_text(existing, addition) == expected, (
            "expected append_note_text(existing, addition) to be expected"
        )

    def test_dedupe_results_keeps_first_by_url_then_source_id(self):
        results = [{"url": "a"}, {"url": "a"}, {"source_job_id": "s"}, {"source_job_id": "s"}, {}]
        assert dedupe_results(results) == [{"url": "a"}, {"source_job_id": "s"}], "duplicates and keyless rows drop"

    @pytest.mark.parametrize(
        ("value", "expected"),
        [
            ("Executive IC", "Executive IC"),
            (" Wildcards ", "Wildcards"),
            ("Nope", "fallback"),
            (["Nope", "Office of the CTO"], "Office of the CTO"),
            (None, "fallback"),
        ],
    )
    def test_normalize_pipeline(self, profile, value, expected):
        assert normalize_pipeline(value, profile.pipeline_names, "fallback") == expected, value

    def test_sales_exclusion_is_appended_once(self, profile):
        exclusion = profile.sales_exclusion
        once = with_sales_role_exclusion_keywords("Chief Architect", exclusion)
        assert '-"Account Executive"' in once, "exclusion should be appended"
        assert with_sales_role_exclusion_keywords(once, exclusion) == once, "exclusion should not be appended twice"
        criteria = with_sales_role_exclusion_criteria("Architects.", exclusion)
        assert with_sales_role_exclusion_criteria(criteria, exclusion) == criteria, (
            "criteria exclusion is appended once"
        )


class TestDiscoveryFilters:
    @pytest.mark.parametrize(
        ("result", "allowed"),
        [
            ({"location": "Seattle, WA"}, True),
            ({"location": "Remote", "snippet": ""}, True),
            ({"location": "Remote - United States"}, True),
            ({"location": "", "snippet": "fully remote role"}, True),
            ({"location": "Remote - Canada"}, False),
            ({"location": "New York, NY"}, False),
        ],
    )
    def test_location(self, profile, result, allowed):
        assert location_filter_decision(result, profile)[0] is allowed, f"unexpected decision for {result}"

    def test_compensation_annualizes_hourly_and_monthly(self):
        assert extract_annual_compensation_values("$100/hour") == [208000], (
            "expected extract_annual_compensation_values('$100/hour') to be [208000]"
        )
        assert extract_annual_compensation_values("$15,000 per month") == [180000], (
            "expected extract_annual_compensation_values(...) to be [180000]"
        )
        assert extract_annual_compensation_values("$150k - $190k") == [150000, 190000], (
            "expected extract_annual_compensation_values(...) to be [150000, 190000]"
        )

    def test_compensation_rejects_explicit_low_pay(self, profile):
        allowed, reason = compensation_filter_decision({"snippet": "Pay: $150,000 - $180,000 per year"}, profile)
        assert not allowed, "a range entirely below $200k should be rejected"
        assert "$180,000" in reason, f"expected '$180,000' in {reason!r}"

    def test_compensation_allows_missing_or_high_pay(self, profile):
        assert compensation_filter_decision({"snippet": "Great benefits"}, profile)[0], "missing pay is allowed"
        assert compensation_filter_decision({"snippet": "$250k - $300k"}, profile)[0], "high pay is allowed"

    def test_sales_titles_are_rejected(self, profile):
        assert not sales_role_filter_decision({"title": "Enterprise Account Executive"}, profile)[0], "AE rejected"
        assert sales_role_filter_decision({"title": "Chief Architect"}, profile)[0], "architect allowed"

    def test_first_rejection_stops_at_first_failing_filter(self, profile):
        result = {"title": "Account Executive", "location": "Paris, France"}
        assert first_rejection(result, profile)[0] == "sales_role", "sales filter runs before location"
        assert first_rejection({"title": "Architect", "location": "Seattle"}, profile) is None, (
            "expected first_rejection(...) to be None"
        )


class TestJobFilter:
    def job(self, **overrides):
        return {"downlevel": 0, "gpt_score": None, "user_score": None, **overrides}

    def test_downlevel_is_always_filtered(self):
        assert filter_decision(self.job(downlevel=1), 40, 60, False)[0], (
            "expected filter_decision(self.job(downlevel=1), 40, 60, False)[0]"
        )

    def test_gpt_threshold_only_applies_when_enabled(self):
        low = self.job(gpt_score=10)
        assert not filter_decision(low, 40, 60, False)[0], "disabled Codex scoring must not filter"
        assert filter_decision(low, 40, 60, True)[0], "expected filter_decision(low, 40, 60, True)[0]"

    def test_user_threshold(self):
        filtered, reasons = filter_decision(self.job(user_score=50), 40, 60, False)
        assert filtered, "expected filtered"
        assert "user_score 50 below threshold 60" in reasons, (
            f"expected 'user_score 50 below threshold 60' in {reasons!r}"
        )

    def test_thresholds_fall_back_to_defaults_for_bad_values(self):
        assert thresholds({"gpt_threshold": "abc", "user_threshold": "70"}) == (40, 70), (
            "expected thresholds(...) to be (40, 70)"
        )


class TestLevels:
    @pytest.mark.parametrize(
        ("title", "level"),
        [
            ("Senior Software Engineer", "BELOW_IC6"),
            ("Staff Engineer", "BELOW_IC6"),
            ("Senior Principal Software Engineer", "IC6+"),
            ("Chief Architect", "IC6+"),
            ("Principal Engineer", None),
            ("", None),
        ],
    )
    def test_estimate(self, profile, title, level):
        estimate = estimate_level_equivalency(title, profile.target_level)
        assert (estimate["target_level"] if estimate else None) == level, f"unexpected level for {title!r}"

    def test_assessment_text(self, profile):
        text = level_assessment_from_equivalency(
            {
                "company": "Co",
                "source_level_title": "Staff Engineer",
                "title_pattern": "",
                "source_level": "L6",
                "target_level": "BELOW_IC6",
                "target_title": "Below",
            },
            profile.target_level,
        )
        assert text == "Co Staff Engineer L6 maps to Oracle BELOW_IC6 Below per cached level calibration.", (
            f"expected 'Co Staff Engineer L6 maps to Oracl...', got {text!r}"
        )
        assert level_assessment_from_equivalency(None, profile.target_level) == "", (
            "expected level_assessment_from_equivalency(...) to be ''"
        )


class TestScoringAndPacketRules:
    @pytest.mark.parametrize(
        ("value", "expected"), [(85, 85), ("72", 72), (88.9, 88), ("n/a", 0), (None, 0), (True, 0)]
    )
    def test_score_total(self, value, expected):
        assert score_total({"total_score": value}) == expected, (
            "expected score_total({'total_score': value}) to be expected"
        )

    def test_packet_payload_requires_all_markdown(self):
        with pytest.raises(ExternalServiceError, match="cv_markdown"):
            validate_packet_payload({"job_brief_markdown": "x", "resume_markdown": "y"})
        with pytest.raises(ExternalServiceError):
            validate_packet_payload(["not", "an", "object"])

    def test_packet_slug_is_filesystem_safe(self):
        from datetime import datetime

        slug = application_packet_slug(
            {"company": "Acme, Inc.", "title": "Chief Architect / AI", "source_job_id": "linkedin:ABC"},
            today=datetime(2026, 9, 1),
        )
        assert slug == "2026-09-acme-inc-chief-architect-ai-linkedin-abc", (
            f"expected '2026-09-acme-inc-chief-architect-a...', got {slug!r}"
        )
