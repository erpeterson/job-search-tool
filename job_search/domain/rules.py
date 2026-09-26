"""Domain constants that do not depend on the candidate's search profile."""

RUBRIC_FIELDS = [
    "interesting_technical_problems",
    "organizational_influence",
    "cross_functional_work",
    "opportunity_to_mentor",
    "work_life_balance",
    "low_operational_burden",
    "compensation",
    "mission",
]

JOB_STATUSES = (
    "researching",
    "interested",
    "applied",
    "interviewing",
    "offer",
    "rejected",
    "declined",
    "paused",
    "discovered",
)

COMPANY_STATUSES = ("watching", "target", "active_conversation", "paused", "not_interested")

SEARCH_BOARDS = ("linkedin", "indeed")

DEFAULT_SETTINGS = {
    "gpt_threshold": "40",
    "user_threshold": "60",
    "last_search_at": "0",
}

UNKNOWN_LEVEL_ASSESSMENT = "Unknown - level not assessed"
