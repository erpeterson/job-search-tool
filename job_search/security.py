"""Deployment binding, authentication, and CSRF policy."""

from __future__ import annotations

import hmac
import ipaddress
from dataclasses import dataclass
from typing import Mapping


class StartupSecurityError(RuntimeError):
    """Raised when an unsafe network binding is requested."""


@dataclass(frozen=True)
class RequestSecurity:
    externally_exposed: bool
    auth_token: str = ""
    csrf_token: str = ""
    trusted_proxy: bool = False

    @property
    def enabled(self) -> bool:
        return self.externally_exposed


def is_loopback_host(host: str) -> bool:
    candidate = (host or "").strip().lower()
    if candidate == "localhost":
        return True
    try:
        return ipaddress.ip_address(candidate).is_loopback
    except ValueError:
        return False


def load_request_security(environment: Mapping[str, str]) -> RequestSecurity:
    host = environment.get("JOB_SEARCH_HOST", "127.0.0.1")
    if is_loopback_host(host):
        return RequestSecurity(externally_exposed=False)
    auth_token = environment.get("JOB_SEARCH_AUTH_TOKEN", "")
    csrf_token = environment.get("JOB_SEARCH_CSRF_TOKEN", "")
    proxy_enabled = environment.get("JOB_SEARCH_TRUSTED_PROXY", "0") == "1"
    tls_terminated = environment.get("JOB_SEARCH_TLS_TERMINATED", "0") == "1"
    if not (auth_token and csrf_token and proxy_enabled and tls_terminated):
        raise StartupSecurityError(
            "STARTUP_UNSAFE_BINDING: non-loopback binding requires JOB_SEARCH_AUTH_TOKEN, "
            "JOB_SEARCH_CSRF_TOKEN, JOB_SEARCH_TRUSTED_PROXY=1, and JOB_SEARCH_TLS_TERMINATED=1."
        )
    return RequestSecurity(True, auth_token=auth_token, csrf_token=csrf_token, trusted_proxy=True)


def authorized(provided: str | None, expected: str) -> bool:
    return bool(provided) and hmac.compare_digest(provided, f"Bearer {expected}")


def csrf_valid(provided: str | None, expected: str) -> bool:
    return bool(provided) and hmac.compare_digest(provided, expected)
