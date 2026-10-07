"""The candidate's search profile: pipelines, target level, location, and pay rules.

Loaded from JSON at startup (see ``profile.example.json``) so changing the search
never requires editing code.
"""

import re
from dataclasses import dataclass

from job_search.domain.errors import ConfigurationError
from job_search.observability import record_exception


@dataclass(frozen=True)
class PipelineCriteria:
    description: str
    keywords: str


@dataclass(frozen=True)
class SalesExclusion:
    query: str
    criteria: str
    title_terms: tuple


@dataclass(frozen=True)
class TargetLevel:
    system: str
    label: str
    reference: str
    at_or_above_level: str
    at_or_above_title: str
    below_level: str
    below_title: str
    at_or_above_title_patterns: tuple
    below_title_patterns: tuple


@dataclass(frozen=True)
class LocationPolicy:
    home_metro_label: str
    home_metro_terms: tuple
    us_terms: tuple
    non_us_terms: tuple


@dataclass(frozen=True)
class CvRules:
    """Structure and content rules a generated CV must pass; every field is optional in the profile."""

    default_title: str = "Senior Technical Architect"
    required_sections: tuple = (
        "Professional Profile",
        "Technical and Leadership Expertise",
        "Professional Experience",
        "Education",
    )
    min_words: int = 900
    min_subsections: int = 2
    forbidden_phrases: tuple = ("source material", "source documents", "supplied materials", "college period")
    must_include: tuple = ()
    # (when the CV mentions this, it must also contain that) pairs, lowercase.
    required_with: tuple = ()


@dataclass(frozen=True)
class SearchProfile:
    candidate_name: str
    pipelines: dict
    default_search_location: str
    sales_exclusion: SalesExclusion
    target_level: TargetLevel
    min_annual_compensation: int
    location: LocationPolicy
    downlevel_high_score_exception: int
    scoring_instructions: tuple
    refinement_instructions: tuple
    cv: CvRules = CvRules()

    @property
    def pipeline_names(self):
        return list(self.pipelines)

    def default_search_queries(self, boards):
        return [
            {
                "board": board,
                "pipeline": name,
                "keywords": f"{criteria.keywords} {self.sales_exclusion.query}",
                "location": self.default_search_location,
                "criteria": f"{criteria.description} {self.sales_exclusion.criteria}",
            }
            for name, criteria in self.pipelines.items()
            for board in boards
        ]

    @classmethod
    def from_dict(cls, data, source="profile"):
        """Validate raw profile data, raising ``ConfigurationError`` that names the bad field."""
        reader = _Reader(source)
        root = reader.obj(data, "")
        pipelines_raw = reader.obj(root.get("pipelines"), "pipelines")
        if not pipelines_raw:
            reader.fail("pipelines", "must define at least one pipeline")
        pipelines = {}
        for name, value in pipelines_raw.items():
            entry = reader.obj(value, f"pipelines.{name}")
            pipelines[reader.text(name, "pipelines (name)")] = PipelineCriteria(
                description=reader.text(entry.get("description"), f"pipelines.{name}.description"),
                keywords=reader.text(entry.get("keywords"), f"pipelines.{name}.keywords"),
            )
        sales = reader.obj(root.get("sales_role_exclusion"), "sales_role_exclusion")
        level = reader.obj(root.get("target_level"), "target_level")
        location = reader.obj(root.get("location"), "location")
        return cls(
            candidate_name=reader.text(root.get("candidate_name"), "candidate_name"),
            pipelines=pipelines,
            default_search_location=reader.text(root.get("default_search_location"), "default_search_location"),
            sales_exclusion=SalesExclusion(
                query=reader.text(sales.get("query"), "sales_role_exclusion.query"),
                criteria=reader.text(sales.get("criteria"), "sales_role_exclusion.criteria"),
                title_terms=reader.lower_terms(sales.get("title_terms"), "sales_role_exclusion.title_terms"),
            ),
            target_level=TargetLevel(
                system=reader.text(level.get("system"), "target_level.system"),
                label=reader.text(level.get("label"), "target_level.label"),
                reference=reader.text(level.get("reference"), "target_level.reference"),
                at_or_above_level=reader.text(level.get("at_or_above_level"), "target_level.at_or_above_level"),
                at_or_above_title=reader.text(level.get("at_or_above_title"), "target_level.at_or_above_title"),
                below_level=reader.text(level.get("below_level"), "target_level.below_level"),
                below_title=reader.text(level.get("below_title"), "target_level.below_title"),
                at_or_above_title_patterns=reader.patterns(
                    level.get("at_or_above_title_patterns"), "target_level.at_or_above_title_patterns"
                ),
                below_title_patterns=reader.patterns(
                    level.get("below_title_patterns"), "target_level.below_title_patterns"
                ),
            ),
            min_annual_compensation=reader.integer(
                root.get("min_annual_compensation"), "min_annual_compensation", 0, 100_000_000
            ),
            location=LocationPolicy(
                home_metro_label=reader.text(location.get("home_metro_label"), "location.home_metro_label"),
                home_metro_terms=reader.lower_terms(location.get("home_metro_terms"), "location.home_metro_terms"),
                us_terms=reader.lower_terms(location.get("us_terms"), "location.us_terms", strip=False),
                non_us_terms=reader.lower_terms(location.get("non_us_terms"), "location.non_us_terms"),
            ),
            downlevel_high_score_exception=reader.integer(
                root.get("downlevel_high_score_exception"), "downlevel_high_score_exception", 0, 100
            ),
            scoring_instructions=reader.texts(root.get("scoring_instructions"), "scoring_instructions"),
            refinement_instructions=reader.texts(root.get("refinement_instructions"), "refinement_instructions"),
            cv=_cv_rules(reader, root.get("cv")),
        )


def _cv_rules(reader, raw):
    """Optional ``cv`` section; omitted fields keep the ``CvRules`` defaults."""
    if raw is None:
        return CvRules()
    cv = reader.obj(raw, "cv")
    defaults = CvRules()
    required_with = reader.obj(cv.get("required_with", {}), "cv.required_with")
    return CvRules(
        default_title=reader.text(cv["default_title"], "cv.default_title")
        if "default_title" in cv
        else defaults.default_title,
        required_sections=reader.texts(cv["required_sections"], "cv.required_sections")
        if "required_sections" in cv
        else defaults.required_sections,
        min_words=reader.integer(cv["min_words"], "cv.min_words", 0, 100_000)
        if "min_words" in cv
        else defaults.min_words,
        min_subsections=reader.integer(cv["min_subsections"], "cv.min_subsections", 0, 100)
        if "min_subsections" in cv
        else defaults.min_subsections,
        forbidden_phrases=reader.lower_terms(cv["forbidden_phrases"], "cv.forbidden_phrases")
        if "forbidden_phrases" in cv
        else defaults.forbidden_phrases,
        must_include=reader.texts(cv["must_include"], "cv.must_include")
        if "must_include" in cv
        else defaults.must_include,
        required_with=tuple(
            (reader.text(when, "cv.required_with (key)").lower(), reader.text(need, f"cv.required_with.{when}").lower())
            for when, need in required_with.items()
        ),
    )


class _Reader:
    def __init__(self, source):
        self.source = source

    def fail(self, path, problem):
        raise ConfigurationError(f"Search profile {self.source}: {path or '(root)'} {problem}.", "profile_invalid")

    def obj(self, value, path):
        if not isinstance(value, dict):
            self.fail(path, "must be a JSON object")
        return value

    def text(self, value, path):
        if not isinstance(value, str) or not value.strip():
            self.fail(path, "must be a non-empty string")
        return value

    def texts(self, value, path):
        if not isinstance(value, list) or not value:
            self.fail(path, "must be a non-empty list of strings")
        return tuple(self.text(item, f"{path}[{index}]") for index, item in enumerate(value))

    def lower_terms(self, value, path, strip=True):
        # Terms like " us " rely on surrounding spaces, so stripping is optional.
        return tuple(item.lower().strip() if strip else item.lower() for item in self.texts(value, path))

    def integer(self, value, path, minimum, maximum):
        if isinstance(value, bool) or not isinstance(value, int) or not minimum <= value <= maximum:
            self.fail(path, f"must be an integer between {minimum} and {maximum}")
        return value

    def patterns(self, value, path):
        compiled = []
        for index, pattern in enumerate(self.texts(value, path)):
            try:
                compiled.append(re.compile(pattern))
            except re.error as exc:
                record_exception("profile_pattern_invalid", "domain.profile", "from_dict", exc, path=path)
                self.fail(f"{path}[{index}]", f"is not a valid regular expression ({exc})")
        return tuple(compiled)
