"""Request-level security controls: host and origin checks."""

import json

import pytest

from job_search.config import AppConfig
from job_search.domain.errors import ConfigurationError
from job_search.observability import METRICS


def blame(code):
    return METRICS.snapshot().get(f"blame.{code}", 0)


class TestHostAndOrigin:
    def test_unknown_host_is_forbidden(self, client):
        before = blame("request_host_not_allowed")
        response = client.get("/api/state", headers={"Host": "evil.example"})
        assert response.status_code == 403, f"DNS-rebinding host must be rejected, got {response.status_code}"
        assert response.get_json()["error"] == "Request host is not allowed.", response.get_json()
        assert blame("request_host_not_allowed") == before + 1, "rejection must be recorded with an error code"

    def test_default_hosts_are_allowed(self, client):
        for host in ("127.0.0.1:5050", "localhost:5050"):
            response = client.get("/api/state", headers={"Host": host})
            assert response.status_code == 200, f"{host} should be allowed, got {response.status_code}"

    def test_cross_origin_post_is_forbidden(self, client):
        before = blame("request_origin_not_allowed")
        response = client.post(
            "/api/search/run",
            data=json.dumps({}),
            content_type="application/json",
            headers={"Origin": "https://evil.example"},
        )
        assert response.status_code == 403, f"cross-origin POST must be rejected, got {response.status_code}"
        assert blame("request_origin_not_allowed") == before + 1, "rejection must be recorded with an error code"

    def test_same_origin_post_and_cross_origin_get_are_allowed(self, client):
        response = client.post(
            "/api/settings",
            data=json.dumps({"user_threshold": 60}),
            content_type="application/json",
            headers={"Origin": "http://127.0.0.1:5050"},
        )
        assert response.status_code == 200, f"same-origin POST should succeed: {response.get_json()}"
        response = client.get("/api/state", headers={"Origin": "https://evil.example"})
        assert response.status_code == 200, "reads are not origin-checked; the host check still applies"


class TestAllowedHostsConfig:
    def test_defaults_include_loopback_and_configured_host(self, tmp_path):
        config = AppConfig.from_env({"JOB_SEARCH_PORT": "6000"}, app_dir=tmp_path)
        assert config.allowed_hosts == ("127.0.0.1:6000", "localhost:6000"), config.allowed_hosts

    def test_explicit_list_is_normalized(self, tmp_path):
        config = AppConfig.from_env({"JOB_SEARCH_ALLOWED_HOSTS": " Jobs.Local:80 , 127.0.0.1:5050"}, app_dir=tmp_path)
        assert config.allowed_hosts == ("jobs.local:80", "127.0.0.1:5050"), config.allowed_hosts

    def test_invalid_entries_are_rejected(self, tmp_path):
        with pytest.raises(ConfigurationError) as info:
            AppConfig.from_env({"JOB_SEARCH_ALLOWED_HOSTS": "good:1, bad host"}, app_dir=tmp_path)
        assert info.value.error_code == "config_invalid_allowed_hosts", info.value.error_code


class TestBindSafety:
    @pytest.mark.parametrize("host", ["127.0.0.1", "localhost", "::1", "127.0.0.2"])
    def test_loopback_hosts_are_accepted(self, tmp_path, host):
        assert AppConfig.from_env({"JOB_SEARCH_HOST": host}, app_dir=tmp_path).host == host

    def test_remote_bind_requires_opt_in(self, tmp_path):
        with pytest.raises(ConfigurationError) as info:
            AppConfig.from_env({"JOB_SEARCH_HOST": "0.0.0.0"}, app_dir=tmp_path)
        assert info.value.error_code == "config_remote_bind_not_allowed", info.value.error_code
        config = AppConfig.from_env({"JOB_SEARCH_HOST": "0.0.0.0", "JOB_SEARCH_ALLOW_REMOTE": "1"}, app_dir=tmp_path)
        assert config.host == "0.0.0.0", "the explicit opt-in allows a remote bind"

    def test_debug_on_remote_host_is_always_refused(self, tmp_path):
        environ = {"JOB_SEARCH_HOST": "0.0.0.0", "JOB_SEARCH_ALLOW_REMOTE": "1", "JOB_SEARCH_DEBUG": "1"}
        with pytest.raises(ConfigurationError) as info:
            AppConfig.from_env(environ, app_dir=tmp_path)
        assert info.value.error_code == "config_debug_on_remote_host", info.value.error_code

    def test_cli_exits_2_for_remote_bind_without_opt_in(self, workspace, capsys):
        import io

        from job_search import cli

        code = cli.main(
            ["--host", "0.0.0.0"],
            environ={},
            serve=lambda *a: None,
            out=io.StringIO(),
            app_dir=workspace / "job-search-tool",
        )
        assert code == cli.EXIT_CONFIG_ERROR, f"expected exit code 2, got {code}"
        assert "JOB_SEARCH_ALLOW_REMOTE" in capsys.readouterr().err, "stderr should explain the opt-in"


class TestCodexCliPathUpdates:
    def post_config(self, client, value):
        return client.post("/api/config", data=json.dumps({"CODEX_CLI_PATH": value}), content_type="application/json")

    @pytest.mark.parametrize(
        ("value", "code"),
        [
            ("/bin/sh", "config_codex_path_wrong_name"),
            ("bin/codex", "config_codex_path_not_absolute"),
            ("/nonexistent/codex", "config_codex_path_not_executable"),
        ],
    )
    def test_arbitrary_executables_are_rejected(self, client, container, value, code):
        before = blame(code)
        response = self.post_config(client, value)
        assert response.status_code == 400, f"{value} must be rejected, got {response.status_code}"
        assert blame(code) == before + 1, f"expected error code {code}"
        assert not container.config.env_path.exists(), ".env must not change when validation fails"

    def test_non_executable_codex_file_is_rejected(self, client, workspace):
        fake = workspace / "codex-dir" / "codex"
        fake.parent.mkdir()
        fake.write_text("not executable", encoding="utf-8")
        response = self.post_config(client, str(fake))
        assert response.status_code == 400, f"non-executable file must be rejected: {response.get_json()}"

    def test_bare_codex_must_be_on_path(self, client, monkeypatch):
        monkeypatch.setattr("job_search.config.shutil.which", lambda _name: None)
        response = self.post_config(client, "codex")
        assert response.status_code == 400, f"'codex' not on PATH must be rejected: {response.get_json()}"

    def test_valid_codex_executable_is_saved(self, client, container, workspace):
        path = str(workspace / "bin" / "codex")
        response = self.post_config(client, path)
        assert response.status_code == 200, f"valid codex path should be saved: {response.get_json()}"
        assert f"CODEX_CLI_PATH={path}" in container.config.env_path.read_text(), ".env should record the path"


class TestRequestBodies:
    def test_malformed_json_is_400(self, client):
        before = blame("request_body_malformed_json")
        response = client.post("/api/search/run", data="{not json", content_type="application/json")
        assert response.status_code == 400, f"malformed JSON must be rejected, got {response.status_code}"
        assert response.get_json()["error"] == "Request body is not valid JSON.", response.get_json()
        assert blame("request_body_malformed_json") == before + 1, "rejection must be recorded"

    def test_text_plain_body_is_415_even_when_route_ignores_body(self, client, container):
        from conftest import insert_job

        job_id = insert_job(container)
        response = client.post(f"/api/jobs/{job_id}/score-gpt", data="{}", content_type="text/plain")
        assert response.status_code == 415, f"text/plain form posts must be rejected, got {response.status_code}"
        response = client.post(
            "/api/search/run", data="force_refresh=1", content_type="application/x-www-form-urlencoded"
        )
        assert response.status_code == 415, f"form posts must be rejected, got {response.status_code}"

    def test_empty_body_is_treated_as_empty_object(self, client):
        response = client.post("/api/settings")
        assert response.status_code == 200, f"an empty body is allowed: {response.get_json()}"

    def test_oversized_body_is_413(self, client, container):
        body = json.dumps({"note": "x" * (container.config.max_request_bytes + 1)})
        response = client.post("/api/jobs/1/notes", data=body, content_type="application/json")
        assert response.status_code == 413, f"oversized body must be rejected, got {response.status_code}"

    def test_max_request_bytes_is_configurable(self, tmp_path):
        config = AppConfig.from_env({"JOB_SEARCH_MAX_REQUEST_BYTES": "2048"}, app_dir=tmp_path)
        assert config.max_request_bytes == 2048, config.max_request_bytes
        with pytest.raises(ConfigurationError):
            AppConfig.from_env({"JOB_SEARCH_MAX_REQUEST_BYTES": "10"}, app_dir=tmp_path)


class TestSsrfGuards:
    @pytest.fixture
    def http_client(self, tmp_path):
        from conftest import FakeHttp, FakeResolver

        from job_search.data.captures import CaptureStore
        from job_search.data.http_client import HttpClient

        fake_http, resolver = FakeHttp(), FakeResolver()
        client = HttpClient(
            CaptureStore(tmp_path / "captures", lambda: False), get=fake_http, resolve=resolver, max_response_bytes=64
        )
        return client, fake_http, resolver

    def error_code(self, client, url):
        from job_search.domain.errors import AppError

        with pytest.raises(AppError) as info:
            client.fetch("svc", url)
        return info.value.error_code

    def test_redirect_to_loopback_is_refused(self, http_client):
        client, fake_http, _ = http_client
        fake_http.route("public.example", status_code=302, headers={"Location": "http://127.0.0.1:5050/api/state"})
        assert self.error_code(client, "https://public.example/job") == "http_redirect_blocked"
        assert fake_http.calls == ["https://public.example/job"], "the loopback target must never be requested"

    def test_hostname_resolving_to_private_address_is_refused(self, http_client):
        client, fake_http, resolver = http_client
        resolver.addresses["internal.example"] = ["10.0.0.1"]
        assert self.error_code(client, "http://internal.example/") == "http_host_resolves_private"
        assert fake_http.calls == [], "no request is made to a private address"

    def test_oversized_body_is_rejected(self, http_client):
        client, fake_http, _ = http_client
        fake_http.route("big.example", text="x" * 1000)
        assert self.error_code(client, "https://big.example/") == "http_response_too_large"

    def test_safe_redirect_is_followed(self, http_client):
        client, fake_http, _ = http_client
        fake_http.route("old.example", status_code=301, headers={"Location": "/new"})
        fake_http.route("old.example/new", text="ok")
        fake_http.routes.reverse()  # match the more specific route first
        response = client.fetch("svc", "https://old.example/job")
        assert response.text == "ok", f"a public redirect should be followed, got {response.text!r}"

    def test_redirect_loops_are_capped(self, http_client):
        client, fake_http, _ = http_client
        fake_http.route("loop.example", status_code=302, headers={"Location": "https://loop.example/again"})
        assert self.error_code(client, "https://loop.example/") == "http_too_many_redirects"

    def test_non_http_scheme_and_unresolvable_host(self, http_client):
        client, _, resolver = http_client
        assert self.error_code(client, "file:///etc/passwd") == "http_url_invalid_scheme"

        def fail(host, port):
            raise OSError("nodename nor servname provided")

        client._resolve = fail
        assert self.error_code(client, "https://nowhere.invalid/") == "http_host_unresolvable"


class TestBoardUrlSanitizing:
    def test_linkedin_parser_drops_non_http_links(self):
        from job_search.data.job_boards import LinkedInBoard

        html = (
            '<li><a class="base-card__full-link" href="javascript:alert(1)">x</a><h3>Evil Architect</h3></li>'
            '<li><a class="base-card__full-link" href="https://www.linkedin.com/jobs/view/1">x</a><h3>Architect</h3></li>'
        )
        titles = [job["title"] for job in LinkedInBoard().parse(html, "Remote")]
        assert titles == ["Architect"], f"javascript: links must be dropped, got {titles}"

    def test_indeed_parser_drops_non_http_links(self):
        from job_search.data.job_boards import IndeedBoard

        html = '<div data-jk="1"><h2><span>T</span></h2><a class="jcs-JobTitle" href="javascript:alert(1)">x</a></div>'
        assert IndeedBoard().parse(html, "Remote") == [], "javascript: links must be dropped"

    def test_every_ui_href_goes_through_safe_href(self):
        import re
        from pathlib import Path

        html = (Path(__file__).parent.parent / "job_search" / "web" / "static" / "index.html").read_text()
        hrefs = re.findall(r'href="([^"]*)"', html)
        unsafe = [href for href in hrefs if not href.startswith("${safeHref(")]
        assert hrefs and not unsafe, f"hrefs must use safeHref(): {unsafe}"
        blank_links = re.findall(r'<a [^>]*target="_blank"[^>]*>', html)
        missing_rel = [link for link in blank_links if 'rel="noopener noreferrer"' not in link]
        assert not missing_rel, f"target=_blank links need rel=noopener noreferrer: {missing_rel}"


class TestSecurityHeaders:
    @pytest.mark.parametrize(
        ("path", "status"),
        [("/", 200), ("/api/state", 200), ("/api/jobs/999", 404), ("/api/does-not-exist", 404)],
    )
    def test_headers_on_pages_api_and_errors(self, client, path, status):
        from job_search.web.app import SECURITY_HEADERS

        response = client.get(path)
        assert response.status_code == status, f"{path}: unexpected status {response.status_code}"
        for name, value in SECURITY_HEADERS.items():
            assert response.headers.get(name) == value, f"{path}: missing or wrong {name}: {response.headers.get(name)}"
        assert "frame-ancestors 'none'" in response.headers["Content-Security-Policy"], "framing must be blocked"
