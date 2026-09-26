"""Composition root: constructs data adapters and domain services from configuration."""

from dataclasses import dataclass

from job_search.config import AppConfig, RuntimeSettings
from job_search.data.captures import CaptureStore
from job_search.data.codex_client import CodexClient, parse_model_json
from job_search.data.database import Database
from job_search.data.documents import CareerDocuments
from job_search.data.env_file import EnvFile
from job_search.data.http_client import HttpClient
from job_search.data.job_boards import JobBoardClient
from job_search.data.packet_store import PacketStore, PandocConverter
from job_search.domain.companies import CompanyService
from job_search.domain.jobs import JobService
from job_search.domain.packets import PacketService
from job_search.domain.scheduler import SearchScheduler
from job_search.domain.scoring import ScoringService
from job_search.domain.search import SearchService
from job_search.domain.settings import SettingsService
from job_search.domain.tasks import BackgroundTaskRegistry, BulkOperations


@dataclass
class Container:
    config: AppConfig
    runtime: RuntimeSettings
    db: Database
    jobs: JobService
    companies: CompanyService
    scoring: ScoringService
    packets: PacketService
    search: SearchService
    settings: SettingsService
    tasks: BackgroundTaskRegistry
    bulk: BulkOperations
    scheduler: SearchScheduler

    def bootstrap(self):
        """Create or migrate the schema and seed default data."""
        self.db.create_schema()
        self.settings.seed_defaults()
        self.search.seed_default_queries()


def build_container(
    config, environ=None, http_get=None, codex_runner=None, pandoc=None, thread_factory=None, resolve_host=None
):
    """Build the service graph.

    ``environ`` backs runtime settings (defaults to ``os.environ``). The other
    optional arguments replace external effects in tests.
    """
    runtime = RuntimeSettings(
        EnvFile(config.env_path), config.default_codex_cli_path, config.default_codex_model, environ=environ
    )
    db = Database(config.db_path)
    captures = CaptureStore(config.capture_dir, runtime.capture_cache_enabled)
    http_kwargs = {"max_response_bytes": config.http_max_response_bytes}
    if http_get:
        http_kwargs["get"] = http_get
    if resolve_host:
        http_kwargs["resolve"] = resolve_host
    http = HttpClient(captures, **http_kwargs)
    boards = JobBoardClient(http)
    codex_kwargs = {"runner": codex_runner} if codex_runner else {}
    codex = CodexClient(runtime, captures, config.workspace_root, config.codex_cli_timeout_seconds, **codex_kwargs)
    documents = CareerDocuments(config.career_manual_path, config.guidance_path, config.master_resume_path)
    store = PacketStore(
        config.workspace_root,
        config.applications_dir,
        pandoc or PandocConverter(timeout_seconds=config.pandoc_timeout_seconds),
    )
    scoring = ScoringService(db, runtime, codex, documents, parse_model_json)
    packets = PacketService(db, runtime, codex, documents, store, parse_model_json)
    search = SearchService(db, runtime, boards, scoring, codex, parse_model_json)
    tasks = BackgroundTaskRegistry(**({"thread_factory": thread_factory} if thread_factory else {}))
    return Container(
        config=config,
        runtime=runtime,
        db=db,
        jobs=JobService(db, runtime, boards, scoring),
        companies=CompanyService(db),
        scoring=scoring,
        packets=packets,
        search=search,
        settings=SettingsService(db, runtime, config.default_codex_model),
        tasks=tasks,
        bulk=BulkOperations(tasks, scoring, packets),
        scheduler=SearchScheduler(db, search, config.search_interval_seconds, config.autorun),
    )
