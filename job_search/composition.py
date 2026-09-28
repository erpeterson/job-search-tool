"""Concrete dependency wiring kept outside presentation and application layers."""

import logging
import os
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from bs4 import BeautifulSoup

from job_search.application.background_task_service import BackgroundTaskService
from job_search.application.codex_scoring_workflow import CodexScoringWorkflow
from job_search.application.company_service import CompanyService
from job_search.application.console_query_service import ConsoleQueryService
from job_search.application.discovery_service import DiscoveryService
from job_search.application.filtering_service import FilteringService
from job_search.application.initialization_service import InitializationService
from job_search.application.job_service import JobService
from job_search.application.level_service import LevelService
from job_search.application.packet_attachment_service import PacketAttachmentService
from job_search.application.packet_content_service import PacketContentService
from job_search.application.packet_generation_service import PacketGenerationService
from job_search.application.rescrape_service import RescrapeService
from job_search.application.search_query_service import SearchQueryService
from job_search.application.search_run_service import SearchRunService
from job_search.application.settings_service import SettingsService
from job_search.application.task_execution_service import TaskExecutionService
from job_search.data_access.application_packet_catalog import ApplicationPacketCatalog
from job_search.data_access.board_gateway import CallableBoardGateway
from job_search.data_access.capture_store import CaptureStore  # noqa: F401
from job_search.data_access.codex_cli import CodexCliGateway
from job_search.data_access.company_repository import SqliteCompanyRepository
from job_search.data_access.console_query_repository import SqliteConsoleQueryRepository
from job_search.data_access.document_writer import PacketDocumentWriter
from job_search.data_access.environment_file import update_environment_file  # noqa: F401
from job_search.data_access.filter_repository import SqliteJobFilterRepository
from job_search.data_access.http_gateway import CapturingHttpGateway
from job_search.data_access.initialization_adapter import SqliteInitializationAdapter
from job_search.data_access.job_board_client import JobBoardClient  # noqa: F401
from job_search.data_access.job_board_parser import JobBoardParser
from job_search.data_access.job_posting_parser import JobPostingParser
from job_search.data_access.job_repository import SqliteJobRepository
from job_search.data_access.level_repository import SqliteLevelRepository
from job_search.data_access.model_output_parser import parse_model_json  # noqa: F401
from job_search.data_access.packet_content_reader import FilesystemPacketContentReader
from job_search.data_access.packet_storage import PacketStorage
from job_search.data_access.read_models import SqliteReadModels
from job_search.data_access.schema import initialize_schema
from job_search.data_access.search_mutations import SqliteSearchMutations
from job_search.data_access.search_query_repository import SqliteSearchQueryRepository
from job_search.data_access.search_repository import SqliteSearchRepository
from job_search.data_access.settings_repository import SqliteSettingsRepository
from job_search.data_access.sqlite import open_connection
from job_search.data_access.telemetry import StructuredTelemetry, configure_json_file_logging  # noqa: F401
from job_search.data_access.text_file_reader import read_optional_text  # noqa: F401
from job_search.http_client import SafeHttpClient
from job_search.presentation.dependencies import PresentationDependencies
from job_search.task_repository import TaskRepository


def database_session(database_path: Path):
    """Open a configured database session at the composition boundary."""
    return open_connection(database_path)


@dataclass(frozen=True)
class Infrastructure:
    """Concrete adapters exposed only through the composition boundary.

    Presentation code receives this opaque dependency bundle instead of importing
    storage, parser, HTTP, or subprocess adapters itself.
    """

    html_parser: Any
    board_gateway: Any
    capture_gateway: Any
    codex_gateway: Any
    board_parser: Any
    posting_parser: Any
    packet_writer: Any
    packet_storage: Any
    http_client: Any
    company_repository: Any
    console_query_repository: Any
    filter_repository: Any
    job_repository: Any
    level_repository: Any
    query_repository: Any
    search_repository: Any
    search_mutations: Any
    settings_repository: Any
    read_models: Any
    task_repository: Any
    schema_initializer: Any
    connection_factory: Any


def infrastructure() -> Infrastructure:
    """Return the concrete adapters assembled at the application's edge."""
    return Infrastructure(
        html_parser=BeautifulSoup,
        board_gateway=CallableBoardGateway,
        capture_gateway=CapturingHttpGateway,
        codex_gateway=CodexCliGateway,
        board_parser=JobBoardParser,
        posting_parser=JobPostingParser,
        packet_writer=PacketDocumentWriter,
        packet_storage=PacketStorage,
        http_client=SafeHttpClient,
        company_repository=SqliteCompanyRepository,
        console_query_repository=SqliteConsoleQueryRepository,
        filter_repository=SqliteJobFilterRepository,
        job_repository=SqliteJobRepository,
        level_repository=SqliteLevelRepository,
        query_repository=SqliteSearchQueryRepository,
        search_repository=SqliteSearchRepository,
        search_mutations=SqliteSearchMutations,
        settings_repository=SqliteSettingsRepository,
        read_models=SqliteReadModels,
        task_repository=TaskRepository,
        schema_initializer=initialize_schema,
        connection_factory=open_connection,
    )


def presentation_dependencies(database_path: Path) -> PresentationDependencies:
    """Assemble request-facing services at the composition boundary.

    Route modules consume only this service registry; concrete repository
    construction remains exclusively in this composition module.
    """
    adapters = infrastructure()

    def connect():
        return database_session(database_path)

    def observe(event: str, **fields: Any) -> None:
        # The web adapter supplies structured event logging.  The composition
        # root deliberately keeps non-HTTP processes usable without Flask.
        logging.getLogger("job_search.events").info("%s %s", event, fields)

    packet_catalog = ApplicationPacketCatalog(database_path.parent, database_path.parent / "applications")

    return PresentationDependencies(
        {
            "job_service": JobService(adapters.job_repository(connect)),
            "company_service": CompanyService(adapters.company_repository(connect)),
            "search_query_service": SearchQueryService(adapters.query_repository(connect)),
            "settings_service": SettingsService(adapters.settings_repository(connect)),
            "console_query_service": ConsoleQueryService(
                adapters.console_query_repository(connect, packet_catalog.list)
            ),
            "packet_catalog": packet_catalog,
            "packet_content_service": PacketContentService(
                FilesystemPacketContentReader(database_path.parent, database_path.parent / "applications")
            ),
            "packet_attachment_service": packet_attachment_service(database_path),
            "level_service": level_service(database_path),
            "search_repository": adapters.search_repository(connect),
            "filtering_service": FilteringService(
                adapters.filter_repository(connect),
                lambda: int(time.time()),
                gpt_scoring_enabled=os.environ.get("JOB_SEARCH_ENABLE_GPT_SCORING", "0") == "1",
            ),
            "background_task_service": BackgroundTaskService(
                adapters.task_repository(database_path),
                lambda: int(time.time()),
                observe,
            ),
            "initialization_service": initialization_service(database_path),
        }
    )


def background_task_service(database_path: Path, observe: Any) -> BackgroundTaskService:
    """Compose durable-task commands for a process using ``database_path``."""
    return BackgroundTaskService(TaskRepository(database_path), lambda: int(time.time()), observe)


def initialization_service(database_path: Path) -> InitializationService:
    """Compose startup initialization at the infrastructure boundary."""
    return InitializationService(SqliteInitializationAdapter(database_path, lambda: int(time.time())))


def packet_content_service(database_path: Path) -> PacketContentService:
    """Compose packet filesystem reads outside the HTTP layer."""
    root = database_path.parent
    return PacketContentService(FilesystemPacketContentReader(root, root / "applications"))


def packet_catalog(database_path: Path) -> ApplicationPacketCatalog:
    """Compose filesystem packet discovery outside presentation handlers."""
    root = database_path.parent
    return ApplicationPacketCatalog(root, root / "applications")


def outbound_http_service(
    client: Any, headers: Any, read_capture: Any, write_capture: Any, log_api_call: Any, redact: Any
):
    """Compose the captured outbound HTTP adapter outside presentation."""
    return CapturingHttpGateway(client, headers, read_capture, write_capture, log_api_call, redact)


def packet_attachment_service(database_path: Path) -> PacketAttachmentService:
    """Compose packet association persistence and filesystem validation."""
    root = database_path.parent
    return PacketAttachmentService(
        SqliteJobRepository(lambda: database_session(database_path)),
        PacketStorage(root, root / "applications").packet_relative_path,
        lambda: int(time.time()),
    )


def level_service(database_path: Path, connection: Any = None) -> LevelService:
    """Compose level-equivalency access for application workflows."""
    return LevelService(
        SqliteLevelRepository(lambda: database_session(database_path), connection), lambda: int(time.time())
    )


def search_repository(database_path: Path):
    """Compose the search persistence adapter for application workflows."""
    return SqliteSearchRepository(lambda: database_session(database_path))


def console_query_service(database_path: Path) -> ConsoleQueryService:
    """Compose console reads with their SQLite and packet-catalog adapters."""
    root = database_path.parent
    catalog = ApplicationPacketCatalog(root, root / "applications")
    return ConsoleQueryService(SqliteConsoleQueryRepository(lambda: database_session(database_path), catalog.list))


def packet_document_writer() -> PacketDocumentWriter:
    """Compose the concrete document writer for packet-generation workflows."""
    return PacketDocumentWriter()


def job_service(database_path: Path, observe: Any = None) -> JobService:
    return JobService(SqliteJobRepository(lambda: database_session(database_path)), observe=observe)


def company_service(database_path: Path) -> CompanyService:
    return CompanyService(SqliteCompanyRepository(lambda: database_session(database_path)))


def search_query_service(database_path: Path) -> SearchQueryService:
    return SearchQueryService(SqliteSearchQueryRepository(lambda: database_session(database_path)))


def settings_service(database_path: Path) -> SettingsService:
    return SettingsService(SqliteSettingsRepository(lambda: database_session(database_path)))


def filtering_service(database_path: Path, observe: Any = None) -> FilteringService:
    return FilteringService(
        SqliteJobFilterRepository(lambda: database_session(database_path)),
        lambda: int(time.time()),
        gpt_scoring_enabled=os.environ.get("JOB_SEARCH_ENABLE_GPT_SCORING", "0") == "1",
        observe=observe,
    )


class _TaskExecutionOperations:
    def __init__(self, console: Any, scoring: Any, packets: Any) -> None:
        self._console = console
        self._scoring = scoring
        self._packets = packets

    def job(self, job_id: int):
        return self._console.job(job_id)

    def score(self, job_id: int):
        return self._scoring.populate_by_id(job_id, force_refresh=False)

    def generate_packet(self, job_id: int):
        return self._packets.generate(job_id)


def task_execution_service(console: Any, scoring: Any, packets: Any) -> TaskExecutionService:
    """Compose durable-task operations from application services, not HTTP callbacks."""
    return TaskExecutionService(_TaskExecutionOperations(console, scoring, packets))


class _PacketGenerationOperations:
    def __init__(self, connection: Any, job: Any, generate: Any, save_path: Any, now: Any, log: Any) -> None:
        self.connection = connection
        self.job = job
        self.generate = generate
        self.save_path = save_path
        self.now = now
        self.log = log


def packet_generation_service(
    connection: Any, job: Any, generate: Any, save_path: Any, now: Any, log: Any
) -> PacketGenerationService:
    """Compose packet generation from explicit workflow ports."""
    return PacketGenerationService(_PacketGenerationOperations(connection, job, generate, save_path, now, log))


class _CodexScoringOperations:
    def __init__(
        self, connection: Any, job: Any, score: Any, normalize: Any, save: Any, apply_filter: Any, now: Any, log: Any
    ) -> None:
        self.connection, self.job, self.score, self.normalize_pipeline = connection, job, score, normalize
        self.save, self.apply_filter, self.now, self.log = save, apply_filter, now, log


def codex_scoring_workflow(
    connection: Any, job: Any, score: Any, normalize: Any, save: Any, apply_filter: Any, now: Any, log: Any
) -> CodexScoringWorkflow:
    return CodexScoringWorkflow(
        _CodexScoringOperations(connection, job, score, normalize, save, apply_filter, now, log)
    )


class _DiscoveryOperations:
    def __init__(self, **operations: Any) -> None:
        self.__dict__.update(operations)


def discovery_service(unknown_level_assessment: str, **operations: Any) -> DiscoveryService:
    """Compose discovery workflow ports outside the presentation layer."""
    return DiscoveryService(_DiscoveryOperations(**operations), unknown_level_assessment)


class _SearchRunOperations:
    def __init__(self, **operations: Any) -> None:
        self.__dict__.update(operations)


def search_run_service(**operations: Any) -> SearchRunService:
    """Compose search-run workflow ports outside the presentation layer."""
    return SearchRunService(_SearchRunOperations(**operations))


def rescrape_service(repository: Any, scraper: Any, refresh_filter: Any, clock: Any) -> RescrapeService:
    return RescrapeService(repository, scraper, refresh_filter, clock)


__all__ = [
    "BeautifulSoup",
    "CallableBoardGateway",
    "CapturingHttpGateway",
    "CodexCliGateway",
    "JobBoardParser",
    "PacketDocumentWriter",
    "PacketStorage",
    "SafeHttpClient",
    "SqliteCompanyRepository",
    "SqliteJobFilterRepository",
    "SqliteJobRepository",
    "SqliteLevelRepository",
    "SqliteSearchQueryRepository",
    "SqliteSearchRepository",
    "SqliteSearchMutations",
    "SqliteSettingsRepository",
    "SqliteReadModels",
    "TaskRepository",
    "open_connection",
    "initialize_schema",
    "infrastructure",
    "database_session",
    "presentation_dependencies",
    "background_task_service",
    "initialization_service",
    "packet_content_service",
    "packet_attachment_service",
    "level_service",
    "search_repository",
    "console_query_service",
    "packet_document_writer",
    "job_service",
    "company_service",
    "search_query_service",
    "settings_service",
    "filtering_service",
    "task_execution_service",
    "packet_generation_service",
    "codex_scoring_workflow",
    "discovery_service",
    "search_run_service",
    "rescrape_service",
]
