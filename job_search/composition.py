"""Concrete dependency wiring kept outside presentation and application layers."""

import logging
import os
import shutil
import time
from collections.abc import Callable, Mapping, Sequence
from contextvars import ContextVar, Token
from dataclasses import dataclass
from datetime import date
from pathlib import Path
from typing import Any

from bs4 import BeautifulSoup
from dotenv import dotenv_values

from job_search.application.background_task_service import BackgroundTaskService
from job_search.application.bulk_task_service import BulkTaskService
from job_search.application.codex_scoring_workflow import CodexScoringWorkflow
from job_search.application.company_service import CompanyService
from job_search.application.configuration_service import ConfigurationService
from job_search.application.console_query_service import ConsoleQueryService
from job_search.application.contracts import Telemetry
from job_search.application.discovery_policy import MIN_ANNUAL_COMPENSATION, DiscoveryPolicy
from job_search.application.discovery_service import UNKNOWN_LEVEL_ASSESSMENT, DiscoveryService
from job_search.application.discovery_utils import clean_text, clean_url, dedupe_results, source_id
from job_search.application.filtering_service import FilteringService
from job_search.application.initialization_service import InitializationService
from job_search.application.job_score_service import JobScoreService
from job_search.application.job_scoring_policy import (
    ORACLE_IC6_LEVEL_REFERENCE,
    PIPELINES,
    RUBRIC_FIELDS,
    JobScoringPolicy,
    normalize_pipeline,
)
from job_search.application.job_service import JobService
from job_search.application.level_service import LevelService
from job_search.application.manual_job_service import ManualJobService
from job_search.application.packet_attachment_service import PacketAttachmentService
from job_search.application.packet_content_service import PacketContentService
from job_search.application.packet_draft_service import PacketDraftService
from job_search.application.packet_generation_service import PacketGenerationService
from job_search.application.rescrape_service import RescrapeService
from job_search.application.scoring_service import ScoringService
from job_search.application.search_query_service import SearchQueryService
from job_search.application.search_run_service import SearchRunService
from job_search.application.settings_service import SettingsService
from job_search.application.startup_service import StartupService
from job_search.application.task_execution_service import TaskExecutionService
from job_search.application.task_submission_service import TaskSubmissionService
from job_search.application.user_score_service import UserScoreService
from job_search.config import RuntimeConfiguration, RuntimePaths, load_runtime_settings
from job_search.data_access.application_packet_catalog import ApplicationPacketCatalog
from job_search.data_access.board_gateway import CallableBoardGateway
from job_search.data_access.capture_store import CaptureStore
from job_search.data_access.codex_cli import CodexCliGateway
from job_search.data_access.codex_json_gateway import CodexCliError as CodexCliError
from job_search.data_access.codex_json_gateway import CodexJsonGateway
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
from job_search.data_access.scheduler_repository import SchedulerLeaseRepository
from job_search.data_access.schema import initialize_schema
from job_search.data_access.search_mutations import SqliteSearchMutations
from job_search.data_access.search_query_repository import SqliteSearchQueryRepository
from job_search.data_access.search_repository import SqliteSearchRepository
from job_search.data_access.settings_repository import SqliteSettingsRepository
from job_search.data_access.source_documents import SourceDocuments
from job_search.data_access.sqlite import open_connection
from job_search.data_access.telemetry import StructuredTelemetry, configure_json_file_logging
from job_search.data_access.text_file_reader import read_optional_text  # noqa: F401
from job_search.http_client import SafeHttpClient
from job_search.presentation.dependencies import PresentationDependencies
from job_search.redaction import redact_content_metadata, redact_headers, redact_url, redact_value
from job_search.security import load_request_security
from job_search.task_repository import TaskRepository


def database_session(database_path: Path):
    """Open a configured database session at the composition boundary."""
    return open_connection(database_path)


def source_documents(configuration: RuntimeConfiguration) -> SourceDocuments:
    paths = configuration.paths
    return SourceDocuments(paths.guidance, paths.career_manual, paths.master_resume)


def packet_storage(configuration: RuntimeConfiguration) -> PacketStorage:
    paths = configuration.paths
    return PacketStorage(paths.root, paths.applications)


def runtime_configuration(
    root: Path | None = None,
    environment: Mapping[str, str] | None = None,
    *,
    which: Callable[[str], str | None] = shutil.which,
    executable: Callable[[str], bool] | None = None,
) -> RuntimeConfiguration:
    """Load dotenv defaults without changing the process environment."""
    paths = RuntimePaths.from_root(root or Path(__file__).resolve().parent.parent)
    file_values = {key: value for key, value in dotenv_values(paths.environment_file).items() if value is not None}
    values = {**file_values, **dict(os.environ if environment is None else environment)}
    return RuntimeConfiguration(
        paths=paths,
        settings=load_runtime_settings(values),
        security=load_request_security(values),
        environment=values,
        which=which,
        executable=executable or (lambda path: os.access(path, os.X_OK)),
        persist=update_environment_file,
    )


class CorrelationIds:
    """Per-request correlation context without a Flask dependency."""

    def __init__(self) -> None:
        self._value: ContextVar[str | None] = ContextVar("job_search_correlation_id", default=None)

    def get(self) -> str | None:
        return self._value.get()

    def set(self, value: str) -> Token[str | None]:
        return self._value.set(value)

    def reset(self, token: Token[str | None]) -> None:
        self._value.reset(token)


@dataclass(frozen=True)
class Observability:
    telemetry: Telemetry
    captures: CaptureStore
    correlation_ids: CorrelationIds
    api_logger: logging.Logger
    event_logger: logging.Logger


def observability(configuration: RuntimeConfiguration) -> Observability:
    """Build logging, correlation, and redacted captures at the composition edge."""
    api_logger = logging.getLogger("job_search.api")
    event_logger = logging.getLogger("job_search.events")
    for logger in (api_logger, event_logger):
        logger.setLevel(logging.INFO)
        logger.propagate = False
    configure_json_file_logging(
        {api_logger: configuration.paths.api_log, event_logger: configuration.paths.app_log},
        max_bytes=configuration.settings.log_max_bytes,
        backup_count=configuration.settings.log_backup_count,
    )
    correlation_ids = CorrelationIds()
    telemetry = StructuredTelemetry(
        api_logger, event_logger, correlation_ids.get, redact_url, redact_value, redact_content_metadata
    )
    captures = CaptureStore(
        lambda: configuration.paths.captures,
        lambda: configuration.enabled("JOB_SEARCH_USE_CAPTURE_CACHE"),
        lambda: configuration.enabled("JOB_SEARCH_ENABLE_FULL_CAPTURE"),
        redact_value,
        lambda event_type, **fields: telemetry.event(event_type, **fields),
    )
    return Observability(telemetry, captures, correlation_ids, api_logger, event_logger)


def codex_json_gateway(configuration: RuntimeConfiguration, observed: Observability) -> CodexJsonGateway:
    """Compose the model subprocess and redacted replay boundary."""
    return CodexJsonGateway(
        CodexCliGateway(configuration.paths.root),
        observed.captures,
        observed.telemetry.event,
        configuration.cli_path,
        configuration.settings.codex_timeout_seconds,
    )


def presentation_dependencies(database_path: Path) -> PresentationDependencies:
    """Assemble request-facing services at the composition boundary.

    Route modules consume only this service registry; concrete repository
    construction remains exclusively in this composition module.
    """

    def connect():
        return database_session(database_path)

    configuration = runtime_configuration(database_path.parent)
    observed = observability(configuration)
    packets_catalog = packet_catalog(database_path)

    def observe_job(**fields: Any) -> None:
        event_type = fields.pop("event")
        observed.telemetry.event(event_type, **fields)

    gateway = codex_json_gateway(configuration, observed)
    scorer = job_score_service(configuration, gateway)
    scoring_workflow = codex_scoring_workflow(database_path, configuration, scorer, observed.telemetry)
    draft = packet_draft_service(configuration, observed, gateway)
    jobs = JobService(SqliteJobRepository(connect), observe_job, lambda: int(time.time()))
    clients = outbound_clients(observed, clean_text, clean_url, source_id, dedupe_results)

    filtering = observed_filtering_service(database_path, configuration, observed.telemetry)
    settings_workflow = SettingsService(SqliteSettingsRepository(connect), filtering.refresh_all)
    tasks = background_task_service(database_path, observed.telemetry.event)

    def scrape(url: str, force_refresh: bool):
        return clients.boards.scrape(url, force_refresh=force_refresh)

    def manual_scoring_availability() -> str | None:
        if not configuration.enabled("JOB_SEARCH_ENABLE_GPT_SCORING"):
            return "Codex scoring is disabled."
        if not configuration.cli_available():
            return f"Codex CLI is unavailable at {configuration.cli_path()!r}."
        return None

    def report_manual_failure(operation: str, error: Exception, context: Mapping[str, Any]) -> None:
        event = "manual_job_scrape_failed" if operation == "scrape" else "manual_job_auto_score_failed"
        observed.telemetry.event(
            event,
            error_code="MANUAL_JOB_SCRAPE_FAILED" if operation == "scrape" else "MANUAL_JOB_AUTO_SCORE_FAILED",
            component="business.job_ingestion" if operation == "scrape" else "business.job_scoring",
            operation="scrape_job_from_url" if operation == "scrape" else "populate_codex_score",
            error_type=type(error).__name__,
            message=str(error)[:1000],
            **context,
        )

    manual = ManualJobService(
        SqliteJobRepository(connect),
        scrape,
        clients.boards.fallback,
        filtering.refresh_job,
        lambda job_id: scoring_workflow.populate_by_id(job_id, force_refresh=False),
        manual_scoring_availability,
        report_manual_failure,
        lambda job_id, reason: observed.telemetry.event(
            "manual_job_auto_score_skipped", job_id=job_id, reason=f"Automatic Codex scoring skipped: {reason}"
        ),
        lambda: int(time.time()),
    )

    discovery, search = compose_search_workflows(
        database_path, configuration, observed, gateway, scorer, clients.search_gateway, filtering
    )

    def scoring_availability() -> str | None:
        if not configuration.enabled("JOB_SEARCH_ENABLE_GPT_SCORING"):
            return "Codex scoring is currently disabled. Set JOB_SEARCH_ENABLE_GPT_SCORING=1 to re-enable it."
        if not configuration.cli_available():
            return f"Codex CLI is unavailable at {configuration.cli_path()!r}."
        return None

    return PresentationDependencies(
        job_service=jobs,
        company_service=CompanyService(SqliteCompanyRepository(connect), lambda: int(time.time())),
        search_query_service=SearchQueryService(SqliteSearchQueryRepository(connect), lambda: int(time.time())),
        settings_service=settings_workflow,
        configuration_service=ConfigurationService(configuration, settings_workflow),
        console_query_service=ConsoleQueryService(SqliteConsoleQueryRepository(connect, packets_catalog.list)),
        packet_catalog=packets_catalog,
        packet_content_service=packet_content_service(database_path),
        packet_attachment_service=packet_attachment_service(database_path, observed.telemetry.event),
        level_service=level_service(database_path),
        search_repository=search_repository(database_path),
        filtering_service=filtering,
        background_task_service=tasks,
        task_submission_service=TaskSubmissionService(
            tasks,
            lambda: configuration.enabled("JOB_SEARCH_ENABLE_GPT_SCORING"),
            configuration.cli_available,
            configuration.cli_path,
        ),
        initialization_service=initialization_service(database_path),
        startup_service=startup_service(database_path, observed.telemetry, configuration.model()),
        codex_scoring_workflow=scoring_workflow,
        scoring_service=ScoringService(
            jobs.get_job,
            lambda job_id: scoring_workflow.populate_by_id(job_id, force_refresh=False),
            scoring_availability,
        ),
        user_score_service=UserScoreService(jobs, filtering.refresh_job, lambda: int(time.time())),
        packet_generation_service=packet_generation_service(database_path, draft, observed.telemetry),
        manual_job_service=manual,
        rescrape_service=rescrape_service(
            SqliteJobRepository(connect),
            scrape,
            filtering.refresh_job,
            lambda: int(time.time()),
            observed.telemetry.event,
        ),
        discovery_service=discovery,
        search_run_service=search,
        configuration=configuration,
        observability=observed,
        outbound_clients=clients,
        codex_gateway=gateway,
        database_path=database_path,
    )


def background_task_service(database_path: Path, observe: Any) -> BackgroundTaskService:
    """Compose durable-task commands for a process using ``database_path``."""
    return BackgroundTaskService(TaskRepository(database_path), lambda: int(time.time()), observe)


def initialization_service(database_path: Path) -> InitializationService:
    """Compose startup initialization at the infrastructure boundary."""
    return InitializationService(SqliteInitializationAdapter(database_path, lambda: int(time.time())))


def startup_service(database_path: Path, telemetry: Telemetry, default_model: str) -> StartupService:
    """Compose startup schema/default seeding and durable-task recovery."""
    return StartupService(
        initialization_service(database_path),
        background_task_service(database_path, telemetry.event),
        telemetry,
        default_model,
    )


def packet_content_service(database_path: Path) -> PacketContentService:
    """Compose packet filesystem reads outside the HTTP layer."""
    root = database_path.parent
    return PacketContentService(FilesystemPacketContentReader(root, root / "applications"))


def packet_catalog(database_path: Path) -> ApplicationPacketCatalog:
    """Compose filesystem packet discovery outside presentation handlers."""
    root = database_path.parent
    return ApplicationPacketCatalog(root, root / "applications")


def outbound_headers() -> dict[str, str]:
    """Stable, non-secret request headers for permitted job-board requests."""
    return {
        "User-Agent": "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/126 Safari/537.36",
        "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
        "Accept-Language": "en-US,en;q=0.9",
    }


@dataclass(frozen=True)
class OutboundClients:
    http: Any
    gateway: CapturingHttpGateway
    boards: JobBoardClient
    search_gateway: CallableBoardGateway


def outbound_clients(
    observed: Observability,
    clean_text: Callable[[str], str],
    clean_url: Callable[[str], str],
    source_id: Callable[[str, str], str],
    deduplicate: Callable[[Sequence[Mapping[str, str]]], Sequence[Mapping[str, str]]],
    *,
    client: Any = None,
    fetch: Callable[..., Any] | None = None,
) -> OutboundClients:
    """Compose the safe transport, capture gateway, and parsers in one place."""
    http = client if client is not None else SafeHttpClient()
    gateway = CapturingHttpGateway(
        http,
        outbound_headers,
        observed.captures.read,
        observed.captures.write,
        observed.telemetry.api_call,
        redact_headers,
    )
    boards = JobBoardClient(
        fetch or gateway.get,
        JobBoardParser(clean_text, clean_url, lambda href: source_id("linkedin", href)),
        JobPostingParser(clean_text, clean_url, source_id),
        BeautifulSoup,
        clean_text,
        clean_url,
        source_id,
        deduplicate,
    )
    return OutboundClients(http, gateway, boards, CallableBoardGateway(boards.linkedin, boards.indeed))


def packet_attachment_service(database_path: Path, observe: Any) -> PacketAttachmentService:
    """Compose packet association persistence and filesystem validation."""
    root = database_path.parent
    return PacketAttachmentService(
        SqliteJobRepository(lambda: database_session(database_path)),
        PacketStorage(root, root / "applications").packet_relative_path,
        lambda: int(time.time()),
        observe,
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


def packet_draft_service(
    configuration: RuntimeConfiguration, observed: Observability, gateway: CodexJsonGateway
) -> PacketDraftService:
    """Compose source reads, model transport, and atomic packet publication."""
    sources = source_documents(configuration)
    storage = packet_storage(configuration)
    writer = PacketDocumentWriter()
    return PacketDraftService(
        career_manual=sources.career_manual,
        master_resume=sources.master_resume,
        cli_available=configuration.cli_available,
        cli_path=configuration.cli_path,
        model=configuration.model,
        complete=gateway.complete,
        parse=parse_model_json,
        publish=lambda name, payload: storage.publish(name, payload, writer.write),
        relative_path=storage.relative_path,
        today=date.today,
        telemetry=observed.telemetry,
    )


def job_service(database_path: Path, observe: Any = None) -> JobService:
    return JobService(
        SqliteJobRepository(lambda: database_session(database_path)), observe=observe, now=lambda: int(time.time())
    )


def company_service(database_path: Path) -> CompanyService:
    return CompanyService(SqliteCompanyRepository(lambda: database_session(database_path)), lambda: int(time.time()))


def search_query_service(database_path: Path) -> SearchQueryService:
    return SearchQueryService(
        SqliteSearchQueryRepository(lambda: database_session(database_path)), lambda: int(time.time())
    )


def settings_service(database_path: Path) -> SettingsService:
    return SettingsService(
        SqliteSettingsRepository(lambda: database_session(database_path)), filtering_service(database_path).refresh_all
    )


def filtering_service(
    database_path: Path, observe: Any = None, *, scoring_enabled: bool | None = None
) -> FilteringService:
    return FilteringService(
        SqliteJobFilterRepository(lambda: database_session(database_path)),
        lambda: int(time.time()),
        gpt_scoring_enabled=(
            os.environ.get("JOB_SEARCH_ENABLE_GPT_SCORING", "0") == "1" if scoring_enabled is None else scoring_enabled
        ),
        observe=observe,
    )


def observed_filtering_service(
    database_path: Path, configuration: RuntimeConfiguration, telemetry: Telemetry
) -> FilteringService:
    """Preserve filter-decision telemetry in every composed process."""

    def observe(job: Mapping[str, Any], decision: Any) -> None:
        if decision.filtered:
            telemetry.event(
                "job_filtered",
                job_id=job.get("id"),
                company=job["company"],
                title=job["title"],
                reasons=decision.reasons,
                gpt_score=job["gpt_score"],
                user_score=job["user_score"],
                downlevel=bool(job["downlevel"]),
                gpt_scoring_enabled=configuration.enabled("JOB_SEARCH_ENABLE_GPT_SCORING"),
            )

    return filtering_service(
        database_path, observe, scoring_enabled=configuration.enabled("JOB_SEARCH_ENABLE_GPT_SCORING")
    )


def job_score_service(configuration: RuntimeConfiguration, gateway: CodexJsonGateway) -> JobScoreService:
    sources = source_documents(configuration)
    policy = JobScoringPolicy(
        pipelines=PIPELINES, rubric_fields=RUBRIC_FIELDS, level_reference=ORACLE_IC6_LEVEL_REFERENCE
    )
    return JobScoreService(
        policy,
        enabled=lambda: configuration.enabled("JOB_SEARCH_ENABLE_GPT_SCORING"),
        available=configuration.cli_available,
        cli_path=configuration.cli_path,
        model=lambda connection: configuration.model() or SqliteReadModels.settings(connection).get("codex_model", ""),
        career_manual=sources.career_manual,
        guidance=sources.guidance,
        examples=SqliteReadModels.calibration_examples,
        complete=gateway.complete,
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


def bulk_task_service(database_path: Path, processor: TaskExecutionService, telemetry: Telemetry) -> BulkTaskService:
    """Compose durable bulk-task progress with the application item processor."""
    progress = background_task_service(database_path, telemetry.event)
    return BulkTaskService(progress, processor, lambda: int(time.time()), telemetry)


class _PacketGenerationOperations:
    def __init__(self, database_path: Path, draft: PacketDraftService, telemetry: Telemetry) -> None:
        self.connection = lambda: database_session(database_path)
        self.job = SqliteReadModels.job
        self.generate = draft.generate
        self.save_path = SqliteSearchMutations.save_application_packet_path
        self.now = lambda: int(time.time())
        self.log = telemetry.event


def packet_generation_service(
    database_path: Path, draft: PacketDraftService, telemetry: Telemetry
) -> PacketGenerationService:
    """Compose packet association persistence after atomic draft publication."""
    return PacketGenerationService(_PacketGenerationOperations(database_path, draft, telemetry))


class _CodexScoringOperations:
    def __init__(
        self,
        database_path: Path,
        configuration: RuntimeConfiguration,
        scorer: JobScoreService,
        telemetry: Telemetry,
    ) -> None:
        self.connection = lambda: database_session(database_path)
        self.job = SqliteReadModels.job
        self.score = scorer.score
        self.normalize_pipeline = normalize_pipeline
        self.save = SqliteSearchMutations.save_codex_score
        self.now = lambda: int(time.time())
        self.log = telemetry.event

        def refresh(connection: Any, job_id: int) -> None:
            connection.commit()
            filtering_service(
                database_path,
                scoring_enabled=configuration.enabled("JOB_SEARCH_ENABLE_GPT_SCORING"),
            ).refresh_job(job_id)

        self.apply_filter = refresh


def codex_scoring_workflow(
    database_path: Path,
    configuration: RuntimeConfiguration,
    scorer: JobScoreService,
    telemetry: Telemetry,
) -> CodexScoringWorkflow:
    return CodexScoringWorkflow(_CodexScoringOperations(database_path, configuration, scorer, telemetry))


class _DiscoveryOperations:
    def __init__(
        self,
        database_path: Path,
        telemetry: Telemetry,
        scoring_enabled: Callable[[], bool],
        scorer_available: Callable[[], bool],
        scorer_path: Callable[[], str],
        score: Callable[..., Any],
        apply_filter: Callable[..., Any],
        normalize_pipeline: Callable[..., str],
        refine: Callable[..., Any],
        clean_text: Callable[..., str],
    ) -> None:
        self.scoring_enabled = scoring_enabled
        self.scorer_available = scorer_available
        self.scorer_path = scorer_path
        self.log = telemetry.event
        self.score = score
        self.create = SqliteSearchMutations.create_discovery_job
        self.apply_filter = apply_filter
        self.normalize_pipeline = normalize_pipeline
        self.refinement_context = SqliteReadModels.query_refinement_context
        self.refine = refine
        self.parse_model_json = parse_model_json
        self.clean_text = clean_text
        self.update_query = SqliteSearchMutations.update_query
        self.lookup_level = lambda connection, company, title: level_service(database_path, connection).lookup(
            company, title
        )
        self.level_assessment = LevelService.assessment

    @staticmethod
    def now() -> int:
        return int(time.time())


def discovery_service(
    database_path: Path,
    telemetry: Telemetry,
    unknown_level_assessment: str,
    level_reference: str,
    *,
    scoring_enabled: Callable[[], bool],
    scorer_available: Callable[[], bool],
    scorer_path: Callable[[], str],
    score: Callable[..., Any],
    apply_filter: Callable[..., Any],
    normalize_pipeline: Callable[..., str],
    refine: Callable[..., Any],
    clean_text: Callable[..., str],
) -> DiscoveryService:
    """Compose discovery workflow ports outside the presentation layer."""
    return DiscoveryService(
        _DiscoveryOperations(
            database_path,
            telemetry,
            scoring_enabled,
            scorer_available,
            scorer_path,
            score,
            apply_filter,
            normalize_pipeline,
            refine,
            clean_text,
        ),
        unknown_level_assessment,
        level_reference,
    )


class _SearchRunOperations:
    """Concrete query, board, persistence, and run-status ports for search."""

    def __init__(
        self,
        database_path: Path,
        boards: CallableBoardGateway,
        telemetry: Telemetry,
        reject_reason: Callable[..., Any],
        level_assessment: Callable[..., Any],
        already_seen_reason: Callable[..., Any],
        classify: Callable[..., Any],
        refine: Callable[..., Any],
        is_refinement_error: Callable[..., bool],
    ) -> None:
        self._database_path = database_path
        self._boards = boards
        self._telemetry = telemetry
        self._repository = SqliteSearchRepository(lambda: database_session(database_path))
        self.reject_reason = reject_reason
        self.level_assessment = level_assessment
        self.already_seen_reason = already_seen_reason
        self.classify = classify
        self.refine = refine
        self.is_refinement_error = is_refinement_error

    @staticmethod
    def now() -> int:
        return int(time.time())

    def log(self, event: str, **fields: Any) -> None:
        self._telemetry.event(event, **fields)

    def repository(self) -> SqliteSearchRepository:
        return self._repository

    def connection(self):
        return database_session(self._database_path)

    def fetch(self, query: Any, *, force_refresh: bool):
        return self._boards.fetch(query["board"], query["keywords"], query["location"], force_refresh=force_refresh)


def search_run_service(
    database_path: Path,
    boards: CallableBoardGateway,
    telemetry: Telemetry,
    *,
    reject_reason: Callable[..., Any],
    level_assessment: Callable[..., Any],
    already_seen_reason: Callable[..., Any],
    classify: Callable[..., Any],
    refine: Callable[..., Any],
    is_refinement_error: Callable[..., bool],
) -> SearchRunService:
    """Compose explicit search ports outside presentation."""
    return SearchRunService(
        _SearchRunOperations(
            database_path,
            boards,
            telemetry,
            reject_reason,
            level_assessment,
            already_seen_reason,
            classify,
            refine,
            is_refinement_error,
        )
    )


def compose_search_workflows(
    database_path: Path,
    configuration: RuntimeConfiguration,
    observed: Observability,
    gateway: CodexJsonGateway,
    scorer: JobScoreService,
    boards: CallableBoardGateway,
    filtering: FilteringService,
) -> tuple[DiscoveryService, SearchRunService]:
    """Build the same search workflows for web and managed scheduler processes."""

    def apply_discovery_filter(connection: Any, job_id: int) -> None:
        connection.commit()
        filtering.refresh_job(job_id)

    def refine_discovery(connection: Any, prompt: Mapping[str, Any], *, force_refresh: bool) -> str:
        model = configuration.model() or SqliteReadModels.settings(connection).get("codex_model", "")
        return gateway.complete(model, prompt, "refine_search_query", force_refresh=force_refresh)

    discovery = discovery_service(
        database_path,
        observed.telemetry,
        UNKNOWN_LEVEL_ASSESSMENT,
        ORACLE_IC6_LEVEL_REFERENCE,
        scoring_enabled=lambda: configuration.enabled("JOB_SEARCH_ENABLE_GPT_SCORING"),
        scorer_available=configuration.cli_available,
        scorer_path=configuration.cli_path,
        score=scorer.score_discovery,
        apply_filter=apply_discovery_filter,
        normalize_pipeline=normalize_pipeline,
        refine=refine_discovery,
        clean_text=clean_text,
    )

    def already_seen_reason(connection: Any, url: str | None) -> str | None:
        if url and SqliteReadModels.job_exists_url(connection, url):
            return "already tracked in jobs"
        return None

    search = search_run_service(
        database_path,
        boards,
        observed.telemetry,
        reject_reason=DiscoveryPolicy(MIN_ANNUAL_COMPENSATION).rejection_reason,
        level_assessment=discovery.assess_level,
        already_seen_reason=already_seen_reason,
        classify=discovery.classify,
        refine=discovery.refine_query,
        is_refinement_error=lambda error: isinstance(error, CodexCliError),
    )
    return discovery, search


@dataclass(frozen=True)
class WorkerProcessDependencies:
    database_path: Path
    repository: TaskRepository
    processor: TaskExecutionService
    observability: Observability


def worker_process_dependencies(
    database_path: Path,
    *,
    configuration: RuntimeConfiguration | None = None,
    observed: Observability | None = None,
    scorer: JobScoreService | None = None,
    draft: PacketDraftService | None = None,
) -> WorkerProcessDependencies:
    """Build only the durable-task process graph for the supplied database."""
    configuration = configuration or runtime_configuration(database_path.parent)
    observed = observed or observability(configuration)
    gateway = codex_json_gateway(configuration, observed) if scorer is None or draft is None else None
    scorer = scorer or job_score_service(configuration, gateway)
    draft = draft or packet_draft_service(configuration, observed, gateway)
    scoring = codex_scoring_workflow(database_path, configuration, scorer, observed.telemetry)
    packets = packet_generation_service(database_path, draft, observed.telemetry)
    processor = task_execution_service(console_query_service(database_path), scoring, packets)
    return WorkerProcessDependencies(database_path, TaskRepository(database_path), processor, observed)


@dataclass(frozen=True)
class SchedulerProcessDependencies:
    database_path: Path
    lease: SchedulerLeaseRepository
    search: SearchRunService
    observability: Observability


def scheduler_process_dependencies(
    database_path: Path,
    *,
    configuration: RuntimeConfiguration | None = None,
    observed: Observability | None = None,
    gateway: CodexJsonGateway | None = None,
    scorer: JobScoreService | None = None,
    boards: CallableBoardGateway | None = None,
) -> SchedulerProcessDependencies:
    """Build only lease and search services for the supplied database."""
    configuration = configuration or runtime_configuration(database_path.parent)
    observed = observed or observability(configuration)
    gateway = gateway or codex_json_gateway(configuration, observed)
    scorer = scorer or job_score_service(configuration, gateway)
    boards = boards or outbound_clients(observed, clean_text, clean_url, source_id, dedupe_results).search_gateway
    filtering = observed_filtering_service(database_path, configuration, observed.telemetry)
    _discovery, search = compose_search_workflows(
        database_path, configuration, observed, gateway, scorer, boards, filtering
    )
    return SchedulerProcessDependencies(database_path, SchedulerLeaseRepository(database_path), search, observed)


def rescrape_service(repository: Any, scraper: Any, refresh_filter: Any, clock: Any, observe: Any) -> RescrapeService:
    return RescrapeService(repository, scraper, refresh_filter, clock, observe)


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
    "compose_search_workflows",
    "WorkerProcessDependencies",
    "worker_process_dependencies",
    "SchedulerProcessDependencies",
    "scheduler_process_dependencies",
    "rescrape_service",
]
