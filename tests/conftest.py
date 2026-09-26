"""Shared fixtures: an isolated workspace, fake external effects, and a wired container."""

import json
import subprocess
import sys
from pathlib import Path

import pytest
from flask.testing import FlaskClient

from job_search.config import AppConfig
from job_search.container import build_container
from job_search.data.profile_file import load_search_profile
from job_search.observability import configure_console_logging
from job_search.web.app import create_app

CAREER_MANUAL = """# Career Manual
Background.
# Downstream Artifact Rules
Use evidence only.
# Open Questions
None.
"""


class FakeCodexRunner:
    """Stands in for ``subprocess.run`` of the Codex CLI.

    Queue responses with ``respond(output, model=..., returncode=...)``; each call
    consumes one. Output may be a dict (serialized to JSON) or a string.
    """

    def __init__(self):
        self.responses = []
        self.calls = []

    def respond(self, output, model="gpt-test", returncode=0):
        self.responses.append((output, model, returncode))

    def __call__(self, command, input=None, **kwargs):  # noqa: A002 - mirrors subprocess.run
        self.calls.append({"command": command, "input": input, **kwargs})
        if not self.responses:
            raise AssertionError(f"Unexpected Codex call: {command}")
        output, model, returncode = self.responses.pop(0)
        if isinstance(output, BaseException):
            raise output
        text = output if isinstance(output, str) else json.dumps(output)
        output_path = command[command.index("-o") + 1]
        with open(output_path, "w", encoding="utf-8") as handle:
            handle.write(text)
        stderr = f"OpenAI Codex -------- model: {model} provider: openai" if model else ""
        return subprocess.CompletedProcess(command, returncode, stdout="", stderr=stderr)


class FakeResponse:
    def __init__(self, status_code=200, text="", headers=None):
        self.status_code = status_code
        self.text = text
        self.headers = {"Content-Type": "text/html", **(headers or {})}
        self.ok = 200 <= status_code < 400
        self.encoding = "utf-8"
        self.closed = False

    def iter_content(self, chunk_size=1):
        data = self.text.encode("utf-8")
        for start in range(0, len(data), chunk_size):
            yield data[start : start + chunk_size]

    def close(self):
        self.closed = True

    def raise_for_status(self):
        if not self.ok:
            import requests

            raise requests.HTTPError(f"{self.status_code} Error")


class FakeHttp:
    """Stands in for ``requests.get``; routes by URL substring."""

    def __init__(self):
        self.routes = []
        self.calls = []

    def route(self, url_part, text="", status_code=200, error=None, headers=None):
        self.routes.append((url_part, text, status_code, error, headers))

    def __call__(self, url, headers=None, timeout=None, allow_redirects=True, stream=False):
        assert allow_redirects is False, "redirects must be followed manually so each hop is validated"
        self.calls.append(url)
        for url_part, text, status_code, error, response_headers in self.routes:
            if url_part in url:
                if error is not None:
                    raise error
                return FakeResponse(status_code, text, response_headers)
        raise AssertionError(f"Unexpected HTTP GET: {url}")


@pytest.fixture(autouse=True)
def reset_console_logging():
    """Point console logging back at the real streams so no handler outlives pytest's capture."""
    yield
    configure_console_logging(verbose=False, stdout=sys.__stdout__, stderr=sys.__stderr__)


def wait_for_task(client, task):
    """Return the final task state. Tasks run synchronously in tests (ImmediateThread)."""
    final = client.get(f"/api/codex-tasks/{task['id']}").get_json()["task"]
    assert final["status"] not in ("queued", "running"), f"task should have finished: {final}"
    return final


PUBLIC_TEST_IP = "93.184.216.34"


class FakeResolver:
    """Stands in for DNS: hosts resolve to a public address unless mapped otherwise."""

    def __init__(self):
        self.addresses = {"localhost": ["127.0.0.1"]}

    def __call__(self, host, port):
        if host in self.addresses:
            return self.addresses[host]
        if all(char.isdigit() or char == "." for char in host):
            return [host]
        return [PUBLIC_TEST_IP]


class FakePandoc:
    def __init__(self, fail_on=None):
        self.fail_on = fail_on
        self.converted = []

    def to_docx(self, source_path, output_path):
        if self.fail_on and source_path.name == self.fail_on:
            from job_search.domain.errors import ExternalServiceError

            raise ExternalServiceError(f"Pandoc failed for {source_path.name}: boom", "pandoc_conversion_failed")
        output_path.write_bytes(b"docx")
        self.converted.append(source_path.name)


class ImmediateThread:
    """Runs the target synchronously on ``start()`` so background tasks are deterministic."""

    def __init__(self, target, args=(), daemon=None, name=None):
        self._target = target
        self._args = args

    def start(self):
        self._target(*self._args)


@pytest.fixture
def workspace(tmp_path):
    app_dir = tmp_path / "job-search-tool"
    app_dir.mkdir()
    (tmp_path / "career-manual").mkdir()
    (tmp_path / "career-manual" / "Career-Manual.md").write_text(CAREER_MANUAL, encoding="utf-8")
    (tmp_path / "resume").mkdir()
    (tmp_path / "resume" / "Master-Resume.md").write_text("# Resume\nArchitect.\n", encoding="utf-8")
    (tmp_path / "supporting-documents").mkdir()
    (tmp_path / "supporting-documents" / "20260731-job-search-guidance.md").write_text("Guidance.", encoding="utf-8")
    codex = tmp_path / "bin" / "codex"
    codex.parent.mkdir()
    codex.write_text("#!/bin/sh\n", encoding="utf-8")
    codex.chmod(0o755)
    return tmp_path


@pytest.fixture
def environ(workspace):
    return {
        "CODEX_CLI_PATH": str(workspace / "bin" / "codex"),
        "JOB_SEARCH_ENABLE_GPT_SCORING": "0",
        "JOB_SEARCH_USE_CAPTURE_CACHE": "1",
    }


@pytest.fixture
def codex_runner():
    return FakeCodexRunner()


@pytest.fixture
def http():
    return FakeHttp()


@pytest.fixture
def resolver():
    return FakeResolver()


@pytest.fixture
def pandoc():
    return FakePandoc()


PROFILE_EXAMPLE = Path(__file__).resolve().parent.parent / "profile.example.json"


@pytest.fixture(scope="session")
def profile():
    """The shipped example profile, loaded through the real loader and validator."""
    return load_search_profile(PROFILE_EXAMPLE)


@pytest.fixture
def config(workspace, environ):
    return AppConfig.from_env(environ, app_dir=workspace / "job-search-tool")


@pytest.fixture
def container(config, environ, http, codex_runner, pandoc, resolver, profile):
    built = build_container(
        config,
        environ=environ,
        http_get=http,
        codex_runner=codex_runner,
        pandoc=pandoc,
        thread_factory=ImmediateThread,
        resolve_host=resolver,
        profile=profile,
    )
    built.bootstrap()
    return built


class LocalClient(FlaskClient):
    """Test client that sends the Host header a browser uses for the default local address."""

    def open(self, *args, **kwargs):
        kwargs.setdefault("base_url", "http://127.0.0.1:5050")
        return super().open(*args, **kwargs)


def make_client(container):
    app = create_app(container)
    app.testing = True
    app.test_client_class = LocalClient
    return app.test_client()


@pytest.fixture
def client(container):
    return make_client(container)


@pytest.fixture
def enable_scoring(container):
    container.runtime.update({"JOB_SEARCH_ENABLE_GPT_SCORING": "1"})


def insert_job(container, **overrides):
    """Insert a tracked job directly and return its id."""
    fields = {
        "created_at": 1,
        "updated_at": 1,
        "company": "ExampleCo",
        "title": "Principal Engineer",
        "url": "https://example.com/jobs/1",
        "pipeline": "Executive IC",
        "status": "researching",
        "posting_text": "Architecture role.",
    }
    fields.update(overrides)
    with container.db.unit_of_work() as uow:
        return uow.jobs.insert(fields)


SCORE_RESPONSE = {
    "total_score": 85,
    "scorecard": {"mission": 8},
    "pipeline": ["Executive IC", "Wildcards"],
    "level_assessment": "IC6-equivalent",
    "downlevel": False,
    "rationale": "Strong fit.",
}
