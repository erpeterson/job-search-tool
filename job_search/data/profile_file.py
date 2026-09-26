"""Load the search profile JSON file."""

import json

from job_search.domain.errors import ConfigurationError
from job_search.domain.profile import SearchProfile
from job_search.observability import log_event, record_exception


def load_search_profile(path):
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except FileNotFoundError as exc:
        record_exception("profile_file_missing", "data.profile_file", "load", exc, path=str(path))
        raise ConfigurationError(
            f"Search profile not found at {path}. Copy profile.example.json there or set JOB_SEARCH_PROFILE_PATH.",
            "profile_file_missing",
        ) from exc
    except (OSError, UnicodeDecodeError, json.JSONDecodeError) as exc:
        record_exception("profile_file_unreadable", "data.profile_file", "load", exc, path=str(path))
        raise ConfigurationError(
            f"Search profile at {path} could not be read as JSON: {exc}.", "profile_file_unreadable"
        ) from exc
    profile = SearchProfile.from_dict(data, source=str(path))
    log_event("search_profile_loaded", path=str(path), pipelines=profile.pipeline_names)
    return profile
