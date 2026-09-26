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
