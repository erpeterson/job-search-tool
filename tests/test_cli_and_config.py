"""CLI exit codes and logging routing, configuration validation, and observability helpers."""

import io
import json
import logging

import pytest

from job_search import cli
from job_search.config import AppConfig, RuntimeSettings
from job_search.domain.errors import ConfigurationError, ValidationError
from job_search.domain.runtime_policy import RuntimeSettingsPolicy
from job_search.observability import (
    METRICS,
    configure_console_logging,
    correlation_scope,
    current_correlation_id,
    log_event,
    operation,
)


@pytest.fixture(autouse=True)
def reset_console_logging():
    yield
    configure_console_logging(verbose=False)


class TestConfig:
    def test_defaults(self, tmp_path):
        config = AppConfig.from_env({}, app_dir=tmp_path / "app")
        assert (config.host, config.port, config.codex_cli_timeout_seconds) == ("127.0.0.1", 5050, 270), (
            "expected the result to be ('127.0.0.1', 5050, 270)"
        )
        assert config.workspace_root == tmp_path.resolve(), (
            f"expected tmp_path.resolve(), got {config.workspace_root!r}"
        )
        assert config.autorun is False, "the scheduler stays disabled until its known bugs are fixed"
        app, root = (tmp_path / "app").resolve(), tmp_path.resolve()
        expected = {
            "db_path": app / "job_search.sqlite3",
            "log_dir": app / "logs",
            "capture_dir": app / "captures",
            "guidance_path": root / "supporting-documents" / "20260731-job-search-guidance.md",
            "career_manual_path": root / "career-manual" / "Career-Manual.md",
            "master_resume_path": root / "resume" / "Master-Resume.md",
            "http_timeout_seconds": 30,
            "db_timeout_seconds": 30,
            "max_retained_tasks": 50,
        }
        actual = {key: getattr(config, key) for key in expected}
        assert actual == expected, f"unexpected defaults: {actual}"

    def test_configured_paths_resolve_relative_to_their_base(self, tmp_path):
        environ = {
            "JOB_SEARCH_DB_PATH": "data/jobs.db",
            "JOB_SEARCH_LOG_DIR": str(tmp_path / "elsewhere" / "logs"),
            "JOB_SEARCH_CAPTURE_DIR": "cap",
            "JOB_SEARCH_GUIDANCE_PATH": "docs/guide.md",
            "JOB_SEARCH_CAREER_MANUAL_PATH": "docs/manual.md",
            "JOB_SEARCH_MASTER_RESUME_PATH": "docs/resume.md",
            "JOB_SEARCH_HTTP_TIMEOUT_SECONDS": "12",
            "JOB_SEARCH_DB_TIMEOUT_SECONDS": "7",
            "JOB_SEARCH_MAX_RETAINED_TASKS": "5",
        }
        config = AppConfig.from_env(environ, app_dir=tmp_path / "app")
        app, root = (tmp_path / "app").resolve(), tmp_path.resolve()
        assert config.db_path == app / "data" / "jobs.db", "app paths resolve against the app directory"
        assert config.log_dir == (tmp_path / "elsewhere" / "logs").resolve(), "absolute paths are used as given"
        assert config.api_log_path.parent == config.log_dir, "log files follow the log directory"
        assert config.capture_dir == app / "cap", f"expected app / 'cap', got {config.capture_dir!r}"
        assert config.guidance_path == root / "docs" / "guide.md", "document paths resolve against the workspace"
        assert config.career_manual_path == root / "docs" / "manual.md", (
            f"expected root / 'docs' / 'manual.md', got {config.career_manual_path!r}"
        )
        assert config.master_resume_path == root / "docs" / "resume.md", (
            f"expected root / 'docs' / 'resume.md', got {config.master_resume_path!r}"
        )
        assert (config.http_timeout_seconds, config.db_timeout_seconds, config.max_retained_tasks) == (12, 7, 5), (
            "expected the result to be (12, 7, 5)"
        )

    @pytest.mark.parametrize(
        ("environ", "code"),
        [
            ({"JOB_SEARCH_PORT": "http"}, "config_invalid_integer"),
            ({"JOB_SEARCH_PORT": "70000"}, "config_integer_out_of_range"),
            ({"JOB_SEARCH_HOST": " "}, "config_empty_host"),
            ({"JOB_SEARCH_HTTP_TIMEOUT_SECONDS": "0"}, "config_integer_out_of_range"),
            ({"JOB_SEARCH_DB_TIMEOUT_SECONDS": "soon"}, "config_invalid_integer"),
            ({"JOB_SEARCH_MAX_RETAINED_TASKS": "0"}, "config_integer_out_of_range"),
            ({"JOB_SEARCH_DB_PATH": "a\x00b"}, "config_path_invalid"),
            ({"JOB_SEARCH_DB_PATH": "."}, "config_path_not_file"),
            ({"JOB_SEARCH_LOG_DIR": "marker.txt"}, "config_path_not_directory"),
            ({"JOB_SEARCH_CAPTURE_DIR": "marker.txt"}, "config_path_not_directory"),
            ({"JOB_SEARCH_WORKSPACE_ROOT": "marker.txt"}, "config_path_not_directory"),
            ({"JOB_SEARCH_GUIDANCE_PATH": "."}, "config_path_not_file"),
            ({"JOB_SEARCH_CAREER_MANUAL_PATH": "."}, "config_path_not_file"),
            ({"JOB_SEARCH_MASTER_RESUME_PATH": "."}, "config_path_not_file"),
        ],
    )
    def test_invalid_values_raise_actionable_errors(self, tmp_path, environ, code):
        (tmp_path / "marker.txt").write_text("x", encoding="utf-8")
        with pytest.raises(ConfigurationError) as info:
            AppConfig.from_env(environ, app_dir=tmp_path)
        assert info.value.error_code == code, f"{environ}: expected {code}, got {info.value.error_code}"

    def test_runtime_settings(self, tmp_path):
        environ = {"CODEX_MODEL": "", "CODEX_CLI_PATH": "a-very-long-path"}
        runtime = RuntimeSettings(environ, "codex", "startup-model")
        assert runtime.codex_model() == "startup-model", "expected runtime.codex_model() to be 'startup-model'"
        assert runtime.codex_model(" stored ") == "stored", "expected runtime.codex_model(' stored ') to be 'stored'"
        assert runtime.masked()["CODEX_CLI_PATH"]["masked"] == "a-ve...path", (
            "expected runtime.masked()['CODEX_CLI_PATH']['masked'] to be 'a-ve...path'"
        )
        assert runtime.masked()["CODEX_MODEL"] == {"configured": False, "masked": ""}, (
            "expected runtime.masked()['CODEX_MODEL'] to be {'configured': False, 'masked': ''}"
        )
        assert runtime.codex_cli_available() is False, "expected runtime.codex_cli_available() to be False"
        assert RuntimeSettingsPolicy.validate({"CODEX_MODEL": None, "CODEX_CLI_PATH": ""}) == {"CODEX_MODEL": ""}, (
            "expected RuntimeSettingsPolicy.validate(...) to be {'CODEX_MODEL': ''}"
        )
        with pytest.raises(ValidationError):
            RuntimeSettingsPolicy.validate({"CODEX_MODEL": "x" * 2000})
        runtime.update({"CODEX_MODEL": "new", "UNRELATED": "ignored"})
        assert runtime.codex_model() == "new", "updates apply in memory"
        assert environ == {"CODEX_MODEL": "", "CODEX_CLI_PATH": "a-very-long-path"}, "the seed mapping is not mutated"


class TestCli:
    def test_starts_server_and_prints_url(self, workspace):
        served = []
        out = io.StringIO()
        code = cli.main(
            [],
            environ={},
            serve=lambda app, config: served.append(config.port),
            out=out,
            app_dir=workspace / "job-search-tool",
        )
        assert code == cli.EXIT_OK, f"expected cli.EXIT_OK, got {code!r}"
        assert served == [5050], f"expected [5050], got {served!r}"
        assert "Job Search Console running at http://127.0.0.1:5050" in out.getvalue(), (
            "expected 'Job Search Console running at http://127.0...' in out.getvalue()"
        )

    def test_args_and_env_file_override_defaults(self, workspace):
        app_dir = workspace / "job-search-tool"
        (app_dir / ".env").write_text("JOB_SEARCH_PORT=6000\nJOB_SEARCH_HOST=0.0.0.0\n", encoding="utf-8")
        served = []
        cli.main(
            ["--host", "localhost"],
            environ={},
            serve=lambda app, c: served.append((c.host, c.port)),
            out=io.StringIO(),
            app_dir=app_dir,
        )
        assert served == [("localhost", 6000)], "CLI args override .env, which fills unset values"

    def test_configuration_error_exits_2_with_stderr(self, workspace, capsys):
        code = cli.main(
            ["--port", "nope"],
            environ={},
            serve=lambda *a: None,
            out=io.StringIO(),
            app_dir=workspace / "job-search-tool",
        )
        assert code == cli.EXIT_CONFIG_ERROR, f"expected cli.EXIT_CONFIG_ERROR, got {code!r}"
        assert "JOB_SEARCH_PORT must be an integer" in capsys.readouterr().err, (
            "expected 'JOB_SEARCH_PORT must be an integer' in capsys.readouterr().err"
        )

    def test_unexpected_error_exits_1_and_logs_to_stderr(self, workspace, capsys):
        def boom(app, config):
            raise RuntimeError("serve failed")

        code = cli.main([], environ={}, serve=boom, out=io.StringIO(), app_dir=workspace / "job-search-tool")
        captured = capsys.readouterr()
        assert code == cli.EXIT_FAILURE, f"expected cli.EXIT_FAILURE, got {code!r}"
        assert "cli_unhandled_exception" in captured.err, "ERROR logs go to stderr"
        assert "cli_unhandled_exception" not in captured.out, (
            f"expected 'cli_unhandled_exception' not in {captured.out!r}"
        )

    def test_interrupt_exits_130(self, workspace):
        def interrupt(app, config):
            raise KeyboardInterrupt

        code = cli.main([], environ={}, serve=interrupt, out=io.StringIO(), app_dir=workspace / "job-search-tool")
        assert code == cli.EXIT_INTERRUPTED, f"expected cli.EXIT_INTERRUPTED, got {code!r}"


class TestConsoleLogging:
    def test_info_goes_to_stdout_only_when_verbose(self):
        out, err = io.StringIO(), io.StringIO()
        configure_console_logging(verbose=False, stdout=out, stderr=err)
        log_event("quiet_event")
        log_event("loud_event", level=logging.ERROR)
        assert out.getvalue() == "", "non-error logs are suppressed without --verbose"
        assert "loud_event" in err.getvalue(), "expected 'loud_event' in err.getvalue()"

        out, err = io.StringIO(), io.StringIO()
        configure_console_logging(verbose=True, stdout=out, stderr=err)
        log_event("chatty_event")
        assert "chatty_event" in out.getvalue(), "expected 'chatty_event' in out.getvalue()"
        assert "chatty_event" not in err.getvalue(), "expected 'chatty_event' not in err.getvalue()"


class TestObservability:
    def test_operation_emits_start_success_and_failure(self):
        out = io.StringIO()
        configure_console_logging(verbose=True, stdout=out, stderr=io.StringIO())
        with operation("unit_op", "tests"):
            pass
        with pytest.raises(ValueError), operation("unit_op", "tests"):
            raise ValueError("bad")
        events = [json.loads(line)["event"] for line in out.getvalue().splitlines()]
        assert events[:3] == ["unit_op_started", "unit_op_succeeded", "unit_op_started"], (
            f"events[:3] did not match; got {events[:3]!r}"
        )
        assert "blame_metric" in events, f"expected 'blame_metric' in {events!r}"
        assert METRICS.snapshot()["blame.unit_op_failed"] >= 1, (
            f"expected >= 1, got {METRICS.snapshot()['blame.unit_op_failed']!r}"
        )

    def test_correlation_scope_tags_events(self):
        out = io.StringIO()
        configure_console_logging(verbose=True, stdout=out, stderr=io.StringIO())
        with correlation_scope("run-7"):
            assert current_correlation_id() == "run-7", "expected current_correlation_id() to be 'run-7'"
            log_event("tagged")
        assert current_correlation_id() is None, "expected current_correlation_id() to be None"
        assert json.loads(out.getvalue().splitlines()[-1])["correlation_id"] == "run-7", (
            "expected json.loads(out.getvalue().splitlines()[-1])[...] to be 'run-7'"
        )


def test_record_exception_logs_detail_but_message_stays_generic():
    from job_search.domain.errors import ExternalServiceError
    from job_search.observability import record_exception

    out = io.StringIO()
    configure_console_logging(verbose=True, stdout=out, stderr=out)
    exc = ExternalServiceError("Something failed; see logs.", "unit_detail_test", detail="stderr: /private/path")
    record_exception("unit_detail_test", "tests", "detail", exc, level=logging.WARNING)
    line = json.loads(out.getvalue().splitlines()[-1])
    assert line["detail"] == "stderr: /private/path", f"detail must be logged: {line}"
    assert "/private/path" not in exc.message, "the user-facing message stays generic"


def test_cli_does_not_mutate_the_environment(workspace):
    app_dir = workspace / "job-search-tool"
    (app_dir / ".env").write_text("JOB_SEARCH_PORT=6001\n", encoding="utf-8")
    environ = {"CODEX_MODEL": "m"}
    cli.main(["--host", "localhost"], environ=environ, serve=lambda *a: None, out=io.StringIO(), app_dir=app_dir)
    assert environ == {"CODEX_MODEL": "m"}, f".env values and CLI args must not be written into environ: {environ}"


def test_every_log_line_carries_the_process_run_id(tmp_path):
    from job_search.observability import configure_file_logging, log_api_call, process_run_id

    events, api = tmp_path / "events.log", tmp_path / "api.log"
    configure_file_logging(events, api, 1024 * 1024)
    try:
        log_event("unit_event_outside_request")
        log_api_call("svc", "GET", "https://example.com", error=RuntimeError("boom"), elapsed_ms=1)
    finally:
        for name in ("job_search.events", "job_search.api"):
            logger = logging.getLogger(name)
            for handler in [h for h in logger.handlers if getattr(h, "_job_search_file", False)]:
                logger.removeHandler(handler)
                handler.close()
    lines = [json.loads(line) for path in (events, api) for line in path.read_text().splitlines()]
    assert lines, "expected log lines to be written"
    missing = [line["event"] for line in lines if line.get("process_run_id") != process_run_id()]
    assert not missing, f"every line needs the process run id; missing on: {missing}"
