"""Concrete dependency wiring kept outside presentation and application layers."""

from dataclasses import dataclass
from pathlib import Path
from typing import Any

from bs4 import BeautifulSoup

from job_search.data_access.board_gateway import CallableBoardGateway
from job_search.data_access.codex_cli import CodexCliGateway
from job_search.data_access.company_repository import SqliteCompanyRepository
from job_search.data_access.console_query_repository import SqliteConsoleQueryRepository
from job_search.data_access.document_writer import PacketDocumentWriter
from job_search.data_access.filter_repository import SqliteJobFilterRepository
from job_search.data_access.http_gateway import CapturingHttpGateway
from job_search.data_access.job_board_parser import JobBoardParser
from job_search.data_access.job_posting_parser import JobPostingParser
from job_search.data_access.job_repository import SqliteJobRepository
from job_search.data_access.level_repository import SqliteLevelRepository
from job_search.data_access.packet_storage import PacketStorage
from job_search.data_access.read_models import SqliteReadModels
from job_search.data_access.schema import initialize_schema
from job_search.data_access.search_mutations import SqliteSearchMutations
from job_search.data_access.search_query_repository import SqliteSearchQueryRepository
from job_search.data_access.search_repository import SqliteSearchRepository
from job_search.data_access.settings_repository import SqliteSettingsRepository
from job_search.data_access.sqlite import open_connection
from job_search.http_client import SafeHttpClient
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
]
