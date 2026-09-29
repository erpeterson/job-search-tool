"""Dependencies supplied to HTTP route registration by the composition root."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any


@dataclass(frozen=True, slots=True)
class PresentationDependencies:
    """Required web services supplied by the composition root.

    These named fields are the contract between route registration and the
    composition root.
    """

    job_service: Any
    company_service: Any
    search_query_service: Any
    settings_service: Any
    configuration_service: Any
    console_query_service: Any
    packet_catalog: Any
    packet_content_service: Any
    packet_attachment_service: Any
    level_service: Any
    search_repository: Any
    filtering_service: Any
    background_task_service: Any
    task_submission_service: Any
    initialization_service: Any
    startup_service: Any
    codex_scoring_workflow: Any
    scoring_service: Any
    user_score_service: Any
    packet_generation_service: Any
    manual_job_service: Any
    rescrape_service: Any
    discovery_service: Any
    search_run_service: Any
    configuration: Any
    observability: Any
    outbound_clients: Any
    codex_gateway: Any
    database_path: Any

    def __post_init__(self) -> None:
        missing = [name for name in self.__dataclass_fields__ if getattr(self, name) is None]
        if missing:
            raise ValueError(f"Missing required web dependencies: {', '.join(missing)}")
