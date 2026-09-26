"""Domain constants: pipelines, rubric, statuses, and filtering vocabularies."""

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

PIPELINES = [
    "Executive IC",
    "Office of the CTO",
    "Adjacent industries",
    "Wildcards",
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

PIPELINE_CRITERIA = {
    "Executive IC": {
        "description": (
            "Distinguished Engineer, Chief Architect, Technical Fellow, Principal Architect, Senior Principal "
            "Engineer roles at cloud, infrastructure, enterprise software, and AI platform companies."
        ),
        "keywords": (
            '("Distinguished Engineer" OR "Chief Architect" OR "Technical Fellow" OR "Principal Architect" OR '
            '"Senior Principal Engineer") (cloud OR infrastructure OR platform OR enterprise OR AI)'
        ),
    },
    "Office of the CTO": {
        "description": (
            "Office of CTO, technical strategy, engineering strategy, CTO advisor, strategic initiatives, technical "
            "incubation, emerging technology roles hidden inside executive descriptions."
        ),
        "keywords": (
            '("Office of the CTO" OR "Technical Strategy" OR "Engineering Strategy" OR "CTO Advisor" OR '
            '"Strategic Initiatives" OR "Technical Incubation" OR "Emerging Technology")'
        ),
    },
    "Adjacent industries": {
        "description": (
            "Architectural roles in healthcare, defense, climate, industrial automation, and scientific computing "
            "organizations with complicated technical organizations."
        ),
        "keywords": (
            '("Chief Architect" OR "Principal Architect" OR "Distinguished Engineer" OR "Technical Strategy") '
            '(healthcare OR defense OR climate OR "industrial automation" OR "scientific computing")'
        ),
    },
    "Wildcards": {
        "description": (
            "Intellectually interesting roles in national labs, Disney Imagineering, Apple Vision, NVIDIA research "
            "operations, NASA contractors, AI safety, and robotics platforms."
        ),
        "keywords": (
            '("AI safety" OR robotics OR "research operations" OR "national lab" OR NASA OR "Apple Vision" OR '
            'Imagineering OR NVIDIA) ("Principal Engineer" OR Architect OR "Technical Strategy")'
        ),
    },
}

SALES_ROLE_EXCLUSION_QUERY = (
    '-"Account Executive" -"Sales Executive" -"Sales Director" -"Account Manager" -"Business Development" -sales'
)
SALES_ROLE_EXCLUSION_CRITERIA = "Exclude Account Executive and other sales roles."
SALES_ROLE_TITLE_TERMS = (
    "account executive",
    "sales executive",
    "sales director",
    "sales manager",
    "sales representative",
    "account manager",
    "account director",
    "business development",
)

DEFAULT_SEARCH_QUERIES = [
    {
        "board": board,
        "pipeline": pipeline,
        "keywords": f"{config['keywords']} {SALES_ROLE_EXCLUSION_QUERY}",
        "location": "Remote",
        "criteria": f"{config['description']} {SALES_ROLE_EXCLUSION_CRITERIA}",
    }
    for pipeline, config in PIPELINE_CRITERIA.items()
    for board in SEARCH_BOARDS
]

DEFAULT_SETTINGS = {
    "gpt_threshold": "40",
    "user_threshold": "60",
    "last_search_at": "0",
}

ORACLE_IC6_LEVEL_REFERENCE = (
    "Oracle Software Engineer IC-6 is Architect. "
    "Treat IC6-equivalent as Architect / Principal-plus / Staff-plus scope with broad technical influence, "
    "cross-team architecture, durable technical direction, or organization-level engineering judgment."
)
MIN_ANNUAL_COMPENSATION = 200_000
UNKNOWN_LEVEL_ASSESSMENT = "Unknown - level not assessed"
DOWNLEVEL_HIGH_SCORE_EXCEPTION = 80

SEATTLE_LOCATION_TERMS = (
    "seattle",
    "bellevue",
    "redmond",
    "kirkland",
    "renton",
    "mercer island",
    "tukwila",
    "puget sound",
    "greater seattle",
    "seattle metropolitan",
)

US_LOCATION_TERMS = (
    "united states",
    "usa",
    "u.s.",
    " us ",
    "us-based",
    "anywhere in the us",
    "anywhere in us",
)

NON_US_LOCATION_TERMS = (
    "canada",
    "united kingdom",
    "uk",
    "europe",
    "emea",
    "india",
    "australia",
    "germany",
    "france",
    "netherlands",
    "singapore",
    "mexico",
)
