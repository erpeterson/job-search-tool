"""SQLite adapter for application startup initialization."""

from __future__ import annotations

from collections.abc import Callable, Mapping
from pathlib import Path
from typing import Any

from job_search.application.discovery_service import PIPELINE_CRITERIA
from job_search.data_access.read_models import SqliteReadModels
from job_search.data_access.schema import initialize_schema
from job_search.data_access.search_mutations import SqliteSearchMutations
from job_search.data_access.sqlite import open_connection

SALES_ROLE_EXCLUSION_QUERY = (
    '-"Account Executive" -"Sales Executive" -"Sales Director" -"Account Manager" -"Business Development" -sales'
)
SALES_ROLE_EXCLUSION_CRITERIA = "Exclude Account Executive and other sales roles."


def _default_queries() -> list[dict[str, Any]]:
    return [
        {
            "board": board,
            "pipeline": pipeline,
            "keywords": f"{config['keywords']} {SALES_ROLE_EXCLUSION_QUERY}",
            "location": "Remote",
            "criteria": f"{config['description']} {SALES_ROLE_EXCLUSION_CRITERIA}",
            "seeded": 1,
        }
        for pipeline, config in PIPELINE_CRITERIA.items()
        for board in ("linkedin", "indeed")
    ]


class SqliteInitializationAdapter:
    """Concrete startup operations; presentation never opens this connection."""

    def __init__(self, database_path: Path, now: Callable[[], int]) -> None:
        self._database_path = database_path
        self._now = now

    def connection(self):
        self._database_path.parent.mkdir(parents=True, exist_ok=True)
        return open_connection(self._database_path)

    @staticmethod
    def initialize_schema(connection: Any) -> None:
        initialize_schema(connection)

    @staticmethod
    def initialize_defaults(connection: Any, defaults: Mapping[str, str]) -> None:
        SqliteReadModels.initialize_defaults(connection, defaults)

    def seed_queries(self, connection: Any) -> None:
        SqliteSearchMutations.seed_queries(connection, _default_queries(), self._now())

    @staticmethod
    def remove_legacy_seeds(connection: Any) -> None:
        SqliteSearchMutations.remove_legacy_level_equivalency_seeds(connection)

    @staticmethod
    def disable_legacy_queries(connection: Any) -> None:
        SqliteReadModels.disable_legacy_seed_queries(connection)
