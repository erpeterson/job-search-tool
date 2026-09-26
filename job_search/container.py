"""Composition root: constructs data adapters and domain services from configuration."""

import os
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
from job_search.data.profile_file import load_search_profile
from job_search.domain.companies import CompanyService
from job_search.domain.jobs import JobService
from job_search.domain.packets import PacketService
from job_search.domain.profile import SearchProfile
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
    profile: SearchProfile
    tasks: BackgroundTaskRegistry
    bulk: BulkOperations
    scheduler: SearchScheduler

    def bootstrap(self):
        """Create or migrate the schema and seed default data."""
        self.db.create_schema()
        self.settings.seed_defaults()
        self.search.seed_default_queries()


def build_container(
    config,
    environ=None,
    http_get=None,
    codex_runner=None,
    pandoc=None,
    thread_factory=None,
    resolve_host=None,
    profile=None,
):
    """Build the service graph.

    ``environ`` seeds runtime settings once (defaults to ``os.environ``, read only). The other
    optional arguments replace external effects in tests.
    """
    runtime = RuntimeSettings(
        os.environ if environ is None else environ, config.default_codex_cli_path, config.default_codex_model
    )
    profile = profile or load_search_profile(config.profile_path)
    db = Database(config.db_path, timeout_seconds=config.db_timeout_seconds)
    captures = CaptureStore(config.capture_dir, runtime.capture_cache_enabled)
    http_kwargs = {
        "max_response_bytes": config.http_max_response_bytes,
        "timeout_seconds": config.http_timeout_seconds,
    }
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
    scoring = ScoringService(db, runtime, codex, documents, parse_model_json, profile)
    packets = PacketService(db, runtime, codex, documents, store, parse_model_json)
    search = SearchService(db, runtime, boards, scoring, codex, parse_model_json, profile)
    task_kwargs = {"max_retained": config.max_retained_tasks, "max_running": config.max_running_tasks}
    if thread_factory:
        task_kwargs["thread_factory"] = thread_factory
    tasks = BackgroundTaskRegistry(**task_kwargs)
    bulk = BulkOperations(tasks, scoring, packets, search)
    return Container(
        config=config,
        runtime=runtime,
        db=db,
        jobs=JobService(db, runtime, boards, scoring, bulk),
        companies=CompanyService(db),
        scoring=scoring,
        profile=profile,
        packets=packets,
        search=search,
        settings=SettingsService(db, runtime, EnvFile(config.env_path), config.default_codex_model),
        tasks=tasks,
        bulk=bulk,
        scheduler=SearchScheduler(db, search, config.search_interval_seconds, config.autorun),
    )
