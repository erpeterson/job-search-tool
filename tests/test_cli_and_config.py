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
        assert (config.host, config.port, config.codex_cli_timeout_seconds) == ("127.0.0.1", 5050, 270)
        assert config.workspace_root == tmp_path.resolve()
        assert config.autorun is False, "the scheduler stays disabled until its known bugs are fixed"

    @pytest.mark.parametrize(
        ("environ", "code"),
        [
            ({"JOB_SEARCH_PORT": "http"}, "config_invalid_integer"),
            ({"JOB_SEARCH_PORT": "70000"}, "config_integer_out_of_range"),
            ({"JOB_SEARCH_HOST": " "}, "config_empty_host"),
        ],
    )
    def test_invalid_values_raise_actionable_errors(self, tmp_path, environ, code):
        with pytest.raises(ConfigurationError) as info:
            AppConfig.from_env(environ, app_dir=tmp_path)
        assert info.value.error_code == code

    def test_runtime_settings(self, tmp_path):
        environ = {"CODEX_MODEL": "", "CODEX_CLI_PATH": "a-very-long-path"}
        runtime = RuntimeSettings(environ, "codex", "startup-model")
        assert runtime.codex_model() == "startup-model"
        assert runtime.codex_model(" stored ") == "stored"
        assert runtime.masked()["CODEX_CLI_PATH"]["masked"] == "a-ve...path"
        assert runtime.masked()["CODEX_MODEL"] == {"configured": False, "masked": ""}
        assert runtime.codex_cli_available() is False
        assert RuntimeSettingsPolicy.validate({"CODEX_MODEL": None, "CODEX_CLI_PATH": ""}) == {"CODEX_MODEL": ""}
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
        assert code == cli.EXIT_OK
        assert served == [5050]
        assert "Job Search Console running at http://127.0.0.1:5050" in out.getvalue()

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
        assert code == cli.EXIT_CONFIG_ERROR
        assert "JOB_SEARCH_PORT must be an integer" in capsys.readouterr().err

    def test_unexpected_error_exits_1_and_logs_to_stderr(self, workspace, capsys):
        def boom(app, config):
            raise RuntimeError("serve failed")

        code = cli.main([], environ={}, serve=boom, out=io.StringIO(), app_dir=workspace / "job-search-tool")
        captured = capsys.readouterr()
        assert code == cli.EXIT_FAILURE
        assert "cli_unhandled_exception" in captured.err, "ERROR logs go to stderr"
        assert "cli_unhandled_exception" not in captured.out

    def test_interrupt_exits_130(self, workspace):
        def interrupt(app, config):
            raise KeyboardInterrupt

        code = cli.main([], environ={}, serve=interrupt, out=io.StringIO(), app_dir=workspace / "job-search-tool")
        assert code == cli.EXIT_INTERRUPTED


class TestConsoleLogging:
    def test_info_goes_to_stdout_only_when_verbose(self):
        out, err = io.StringIO(), io.StringIO()
        configure_console_logging(verbose=False, stdout=out, stderr=err)
        log_event("quiet_event")
        log_event("loud_event", level=logging.ERROR)
        assert out.getvalue() == "", "non-error logs are suppressed without --verbose"
        assert "loud_event" in err.getvalue()

        out, err = io.StringIO(), io.StringIO()
        configure_console_logging(verbose=True, stdout=out, stderr=err)
        log_event("chatty_event")
        assert "chatty_event" in out.getvalue() and "chatty_event" not in err.getvalue()


class TestObservability:
    def test_operation_emits_start_success_and_failure(self):
        out = io.StringIO()
        configure_console_logging(verbose=True, stdout=out, stderr=io.StringIO())
        with operation("unit_op", "tests"):
            pass
        with pytest.raises(ValueError), operation("unit_op", "tests"):
            raise ValueError("bad")
        events = [json.loads(line)["event"] for line in out.getvalue().splitlines()]
        assert events[:3] == ["unit_op_started", "unit_op_succeeded", "unit_op_started"]
        assert "blame_metric" in events
        assert METRICS.snapshot()["blame.unit_op_failed"] >= 1

    def test_correlation_scope_tags_events(self):
        out = io.StringIO()
        configure_console_logging(verbose=True, stdout=out, stderr=io.StringIO())
        with correlation_scope("run-7"):
            assert current_correlation_id() == "run-7"
            log_event("tagged")
        assert current_correlation_id() is None
        assert json.loads(out.getvalue().splitlines()[-1])["correlation_id"] == "run-7"


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
