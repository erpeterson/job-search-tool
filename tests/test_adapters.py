"""Data-access adapters: Codex CLI, HTTP/captures, job boards, file stores, and Markdown rendering."""

import json
import subprocess

import pytest
import requests
from conftest import FakeCodexRunner, FakeHttp, FakeResolver

from job_search.config import RuntimeSettings
from job_search.data.captures import CaptureStore
from job_search.data.codex_client import CodexClient, extract_codex_reported_model, parse_model_json
from job_search.data.env_file import EnvFile
from job_search.data.http_client import REQUEST_HEADERS, HttpClient, HttpResponse
from job_search.data.job_boards import IndeedBoard, JobBoardClient, extract_job_json_ld, location_from_json_ld
from job_search.data.packet_store import PandocConverter
from job_search.domain.errors import (
    CodexCliError,
    DependencyUnavailableError,
    ExternalServiceError,
)
from job_search.web.markdown import markdown_to_html


@pytest.fixture
def captures(tmp_path):
    return CaptureStore(tmp_path / "captures", lambda: True)


@pytest.fixture
def codex(tmp_path, captures, environ):
    runner = FakeCodexRunner()
    runtime = RuntimeSettings(environ, "codex")
    return CodexClient(runtime, captures, tmp_path, timeout_seconds=5, runner=runner), runner


class TestCodexClient:
    def test_extracts_exact_model_from_cli_banner(self):
        stderr = "OpenAI Codex v0.147.0 -------- model: gpt-5.6-terra provider: openai --------"
        assert extract_codex_reported_model(stderr) == "gpt-5.6-terra", (
            "expected extract_codex_reported_model(stderr) to be 'gpt-5.6-terra'"
        )
        assert extract_codex_reported_model(None) == "", "expected extract_codex_reported_model(None) to be ''"

    @pytest.mark.parametrize(
        "text",
        ['{"a": 1}', '```json\n{"a": 1}\n```', 'Here you go: {"a": 1} thanks'],
    )
    def test_parse_model_json_tolerates_wrapping(self, text):
        assert parse_model_json(text) == {"a": 1}, "expected parse_model_json(text) to be {'a': 1}"

    def test_parse_model_json_rejects_empty(self):
        with pytest.raises(json.JSONDecodeError):
            parse_model_json("")

    def test_call_captures_and_replays(self, codex):
        client, runner = codex
        runner.respond({"ok": True}, model="gpt-x")
        first = client.call_json("gpt-x", {"q": 1}, "score_job")
        second = client.call_json("gpt-x", {"q": 1}, "score_job")
        assert (first.output_text, first.effective_model) == ('{"ok": true}', "gpt-x"), (
            f"expected ('{{\"ok\": true}}', 'gpt-x'), got {(first.output_text, first.effective_model)!r}"
        )
        assert second == first, "identical prompts replay from capture"
        assert len(runner.calls) == 1, f"expected 1, got {len(runner.calls)!r}"
        assert runner.calls[0]["command"][:4] == [client._runtime.codex_cli_path(), "exec", "-m", "gpt-x"], (
            f"runner.calls[0]['command'][:4] did not match; got {runner.calls[0]['command'][:4]!r}"
        )

    def test_force_refresh_bypasses_capture(self, codex):
        client, runner = codex
        runner.respond({"n": 1})
        runner.respond({"n": 2})
        client.call_json("", {"q": 1}, "op")
        assert client.call_json("", {"q": 1}, "op", force_refresh=True).output_text == '{"n": 2}', (
            "expected client.call_json(...).output_text to be '{\"n\": 2}'"
        )
        assert "-m" not in runner.calls[0]["command"], "blank model uses the Codex CLI default"

    def test_failed_codex_capture_is_not_replayed(self, codex):
        client, runner = codex
        runner.respond({"partial": True}, returncode=1)
        with pytest.raises(CodexCliError):
            client.call_json("m", {"q": 9}, "op")
        runner.respond({"ok": True})
        result = client.call_json("m", {"q": 9}, "op")
        assert result.output_text == '{"ok": true}', "a non-zero-exit capture must not be replayed as success"
        assert len(runner.calls) == 2, "the second call must run Codex live"

    def test_nonzero_exit_raises(self, codex):
        client, runner = codex
        runner.respond("", returncode=3)
        with pytest.raises(CodexCliError, match="exited with code 3"):
            client.call_json("m", {"q": 2}, "op")

    def test_timeout_and_launch_failure_become_external_errors(self, codex):
        client, runner = codex
        runner.respond(subprocess.TimeoutExpired("codex", 5))
        with pytest.raises(ExternalServiceError, match="timed out"):
            client.call_json("m", {"q": 3}, "op", force_refresh=True)
        runner.respond(FileNotFoundError("codex"))
        with pytest.raises(ExternalServiceError, match="could not be started"):
            client.call_json("m", {"q": 4}, "op", force_refresh=True)


class TestHttpAndCaptures:
    def test_failed_request_is_captured_but_not_replayed(self, captures):
        http = FakeHttp()
        http.route("x.com", error=requests.ConnectionError("down"))
        client = HttpClient(captures, get=http, resolve=FakeResolver())
        with pytest.raises(requests.ConnectionError):
            client.fetch("svc", "https://x.com/a")
        capture = captures.path_for(
            "svc", "http_get", {"method": "GET", "url": "https://x.com/a", "headers": REQUEST_HEADERS}
        )
        failures = list(capture.parent.glob(f"{capture.stem}.failed.*.json"))
        assert len(failures) == 1 and not capture.exists(), f"failures get their own file: {failures}"
        with pytest.raises(requests.ConnectionError):
            client.fetch("svc", "https://x.com/a")
        assert len(http.calls) == 2, "a failed capture must not be replayed; the client retries live"

    def test_non_2xx_response_is_not_replayed(self, captures):
        http = FakeHttp()
        http.route("x.com", status_code=403, text="blocked")
        client = HttpClient(captures, get=http, resolve=FakeResolver())
        client.fetch("svc", "https://x.com/b")
        client.fetch("svc", "https://x.com/b")
        assert len(http.calls) == 2, "a 403 capture must not be replayed as a cached result"

    def test_successful_response_is_replayed(self, captures):
        http = FakeHttp()
        http.route("x.com", text="ok")
        client = HttpClient(captures, get=http, resolve=FakeResolver())
        client.fetch("svc", "https://x.com/c")
        replay = client.fetch("svc", "https://x.com/c")
        assert isinstance(replay, HttpResponse) and replay.text == "ok", "2xx captures replay"
        assert len(http.calls) == 1, "a successful capture avoids a second live call"

    def test_capture_write_failure_does_not_mask_original_error(self, captures, monkeypatch):
        from job_search.observability import METRICS

        def deny(*_args, **_kwargs):
            raise PermissionError("read-only disk")

        monkeypatch.setattr("job_search.data.captures.tempfile.NamedTemporaryFile", deny)
        http = FakeHttp()
        http.route("x.com", error=requests.ConnectionError("down"))
        client = HttpClient(captures, get=http, resolve=FakeResolver())
        before = METRICS.snapshot().get("blame.capture_write_failed", 0)
        with pytest.raises(requests.ConnectionError):
            client.fetch("svc", "https://x.com/d")
        assert METRICS.snapshot()["blame.capture_write_failed"] == before + 1, "write failure must be recorded"

    def test_capture_write_is_atomic(self, captures):
        path = captures.write("svc", "op", {"a": 1}, {"text": "x"})
        assert path.exists(), "capture should be written"
        leftovers = [p.name for p in path.parent.iterdir() if p.suffix == ".tmp"]
        assert leftovers == [], f"no temporary files should remain: {leftovers}"

    def test_corrupt_capture_is_a_cache_miss(self, captures):
        path = captures.path_for("svc", "op", {"a": 1})
        path.parent.mkdir(parents=True)
        path.write_text("{not json", encoding="utf-8")
        assert captures.read("svc", "op", {"a": 1}) is None, "expected captures.read('svc', 'op', {'a': 1}) to be None"

    def test_disabled_cache_never_replays(self, tmp_path):
        store = CaptureStore(tmp_path, lambda: False)
        store.write("svc", "op", {"a": 1}, {"text": "x"})
        assert store.read("svc", "op", {"a": 1}) is None, "expected store.read('svc', 'op', {'a': 1}) to be None"


INDEED_HTML = """
<div data-jk="abc123"><h2><span title="Principal Architect">Principal Architect</span></h2>
<a href="/viewjob?jk=abc123">view</a><span data-testid="company-name">Acme</span>
<div data-testid="text-location">Remote</div></div>
<div class="job_seen_beacon"><h2><span>No link</span></h2></div>
"""


class TestJobBoards:
    def test_indeed_parse(self):
        jobs = IndeedBoard().parse(INDEED_HTML, "Remote")
        expected = {
            "board": "indeed",
            "source_job_id": "abc123",
            "company": "Acme",
            "title": "Principal Architect",
            "location": "Remote",
            "url": "https://www.indeed.com/viewjob?jk=abc123",
            "snippet": "Principal Architect view Acme Remote",
        }
        assert jobs == [expected], f"only the linked card should parse, with these fields; got {jobs!r}"

    def test_search_rejects_unknown_board(self, captures):
        from job_search.data.job_boards import UnsupportedBoardError

        with pytest.raises(UnsupportedBoardError):
            JobBoardClient(HttpClient(captures, get=FakeHttp(), resolve=FakeResolver())).search("monster", "x", "y")

    def test_scrape_returns_empty_fields_when_not_found(self, captures):
        http = FakeHttp()
        http.route("bare.example", "<html><body></body></html>")
        posting = JobBoardClient(HttpClient(captures, get=http, resolve=FakeResolver())).scrape_posting(
            "https://bare.example/1"
        )
        assert (posting["company"], posting["title"]) == ("", ""), f"the adapter must not invent defaults: {posting}"

    def test_scrape_uses_selectors_when_json_ld_invalid(self, captures):
        http = FakeHttp()
        http.route(
            "jobs.example.com",
            '<html><head><script type="application/ld+json">{bad</script>'
            '<meta property="og:site_name" content="ExampleCo"></head>'
            '<body><h1>Staff Architect</h1><div id="jobDescriptionText">Design <b>systems</b>.</div></body></html>',
        )
        posting = JobBoardClient(HttpClient(captures, get=http, resolve=FakeResolver())).scrape_posting(
            "https://jobs.example.com/1"
        )
        assert (posting["title"], posting["company"], posting["posting_text"]) == (
            "Staff Architect",
            "ExampleCo",
            "Design systems .",
        ), "the result did not match the expected value"
        assert posting["source_board"] == "manual", f"expected 'manual', got {posting['source_board']!r}"

    def test_json_ld_graph_and_location_variants(self):
        from bs4 import BeautifulSoup

        soup = BeautifulSoup(
            '<script type="application/ld+json">[{"@graph": [{"@type": ["JobPosting"], "title": "T"}]}]</script>',
            "html.parser",
        )
        assert extract_job_json_ld(soup)["title"] == "T", "expected extract_job_json_ld(soup)['title'] to be 'T'"
        assert location_from_json_ld({"jobLocation": [{"name": "Seattle"}]}) == "Seattle", (
            "expected location_from_json_ld(...) to be 'Seattle'"
        )
        assert location_from_json_ld({"jobLocation": []}) == "", (
            "expected location_from_json_ld({'jobLocation': []}) to be ''"
        )

    def test_fallback_posting_uses_host(self, container):
        posting = container.jobs.fallback_posting("https://www.linkedin.com/jobs/view/1")
        assert (posting["company"], posting["source_board"]) == ("linkedin.com", "linkedin"), (
            f"expected ('linkedin.com', 'linkedin'), got {(posting['company'], posting['source_board'])!r}"
        )


class TestFileStores:
    def test_env_file_update_preserves_comments_and_order(self, tmp_path):
        path = tmp_path / ".env"
        path.write_text("# comment\nA=1\nB=2\nA=3\n", encoding="utf-8")
        EnvFile(path).update({"B": "20", "C": "30"})
        assert path.read_text() == '# comment\nA=3\nB="20"\nC="30"\n', path.read_text()

    @pytest.mark.parametrize("value", ["a # not a comment", 'say "hi"', "  padded  ", "back\\slash", "it's"])
    def test_env_file_values_round_trip(self, tmp_path, value):
        env = EnvFile(tmp_path / ".env")
        env.update({"KEY": value})
        assert env.read() == {"KEY": value}, f"{value!r} must survive save and reload: {env.read()}"

    def test_env_file_read_missing_file(self, tmp_path):
        assert EnvFile(tmp_path / "missing.env").read() == {}, "a missing file reads as empty"

    def test_pandoc_converter_errors(self, tmp_path):
        missing = PandocConverter(which=lambda _name: None)
        with pytest.raises(DependencyUnavailableError):
            missing.to_docx(tmp_path / "a.md", tmp_path / "a.docx")
        failing = PandocConverter(
            which=lambda _name: "/usr/bin/pandoc",
            runner=lambda *a, **k: subprocess.CompletedProcess(a, 1, stdout="", stderr="bad input"),
        )
        with pytest.raises(ExternalServiceError) as info:
            failing.to_docx(tmp_path / "a.md", tmp_path / "a.docx")
        assert "bad input" not in info.value.message, "Pandoc stderr must not reach the client message"
        assert info.value.detail == "bad input", "stderr is kept in detail for the logs"

    @pytest.mark.parametrize(
        ("error", "code"),
        [
            (subprocess.TimeoutExpired("pandoc", 5), "pandoc_timeout"),
            (PermissionError("denied"), "pandoc_launch_failed"),
        ],
    )
    def test_pandoc_timeout_and_launch_failure(self, tmp_path, error, code):
        from job_search.observability import METRICS

        calls = []

        def runner(*args, **kwargs):
            calls.append(kwargs)
            raise error

        converter = PandocConverter(which=lambda _name: "/usr/bin/pandoc", runner=runner, timeout_seconds=5)
        before = METRICS.snapshot().get(f"blame.{code}", 0)
        with pytest.raises(ExternalServiceError) as info:
            converter.to_docx(tmp_path / "a.md", tmp_path / "a.docx")
        assert info.value.error_code == code, f"expected {code}, got {info.value.error_code}"
        assert calls[0]["timeout"] == 5, "Pandoc must run with the configured timeout"
        assert METRICS.snapshot()[f"blame.{code}"] == before + 1, "the failure must be recorded"


class TestMarkdown:
    def test_renders_structure_and_escapes(self):
        html = markdown_to_html(
            "# Title\n- **bold** `code`\n- [ok](https://x.com)\n\n```\n<b>raw</b>\n```\n---\n[js](javascript:alert(1))"
        )
        assert "<h1>Title</h1>" in html, f"expected '<h1>Title</h1>' in {html!r}"
        assert "<li><strong>bold</strong> <code>code</code></li>" in html, (
            f"expected '<li><strong>bold</strong> <code>code</code...' in {html!r}"
        )
        assert '<a href="https://x.com" target="_blank" rel="noopener">ok</a>' in html, (
            f'expected \'<a href="https://x.com" target="_blank" re...\' in {html!r}'
        )
        assert "<pre><code>&lt;b&gt;raw&lt;/b&gt;</code></pre>" in html, (
            f"expected '<pre><code>&lt;b&gt;raw&lt;/b&gt;</code></pre>' in {html!r}"
        )
        assert "<hr>" in html, f"expected '<hr>' in {html!r}"
        assert "javascript:" not in html, "unsafe link schemes are rendered as plain text"

    def test_unterminated_code_and_list_are_closed(self):
        assert markdown_to_html("- a\n```\ncode").endswith("<pre><code>code</code></pre>"), (
            "expected markdown_to_html('- a\\n```\\ncode').endswith('<pre><code>code</code></pre>')"
        )
        assert markdown_to_html("- a").endswith("</ul>"), "expected markdown_to_html('- a').endswith('</ul>')"


def test_job_insert_rejects_unknown_columns(container):
    with container.db.unit_of_work() as uow, pytest.raises(ValueError, match="Unknown jobs columns"):
        uow.jobs.insert({"company": "A", "title": "B", "created_at": 1, "updated_at": 1, "id) VALUES (1); --": 1})


def test_env_file_write_failure_leaves_original_intact(tmp_path, monkeypatch):
    path = tmp_path / ".env"
    path.write_text("A=1\n", encoding="utf-8")

    def fail_replace(*_args):
        raise OSError("disk full")

    monkeypatch.setattr("job_search.data.env_file.os.replace", fail_replace)
    with pytest.raises(OSError):
        EnvFile(path).update({"A": "2"})
    assert path.read_text() == "A=1\n", "a failed write must not truncate or change .env"
    leftovers = [p.name for p in tmp_path.iterdir() if p.suffix == ".tmp"]
    assert not leftovers, f"temporary files must be cleaned up: {leftovers}"


def test_env_file_concurrent_updates_keep_every_key(tmp_path):
    import threading

    env = EnvFile(tmp_path / ".env")
    threads = [threading.Thread(target=env.update, args=({f"K{n}": str(n)},)) for n in range(20)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join()
    assert env.read() == {f"K{n}": str(n) for n in range(20)}, "serialized updates must not lose keys"


class TestCaptureEvidence:
    REQUEST = {"method": "GET", "url": "https://x.com/e", "headers": REQUEST_HEADERS}

    def test_failure_then_success_keeps_both_files(self, captures):
        http = FakeHttp()
        http.route("x.com", status_code=503, text="down")
        client = HttpClient(captures, get=http, resolve=FakeResolver())
        client.fetch("svc", "https://x.com/e")
        http.routes.clear()
        http.route("x.com", text="up")
        client.fetch("svc", "https://x.com/e")
        success = captures.path_for("svc", "http_get", self.REQUEST)
        failures = list(success.parent.glob(f"{success.stem}.failed.*.json"))
        assert success.exists(), "the success capture is written"
        assert len(failures) == 1, f"the earlier failure must survive the later success: {failures}"

    def test_success_then_failure_still_replays_success(self, captures):
        http = FakeHttp()
        http.route("x.com", text="good")
        client = HttpClient(captures, get=http, resolve=FakeResolver())
        client.fetch("svc", "https://x.com/e")
        http.routes.clear()
        http.route("x.com", status_code=500, text="bad")
        client.fetch("svc", "https://x.com/e", force_refresh=True)
        replay = client.fetch("svc", "https://x.com/e")
        assert replay.text == "good", f"a later failure must not overwrite the good capture; got {replay.text!r}"

    def test_repeated_failures_never_overwrite_each_other(self, captures):
        paths = {captures.write("svc", "op", {"a": 1}, {"text": str(n)}, succeeded=False) for n in range(3)}
        assert len(paths) == 3 and all(p.exists() for p in paths), f"each failure keeps its own file: {paths}"
