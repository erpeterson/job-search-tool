"""A guarded outbound HTTP adapter for job-board and posting requests."""

from __future__ import annotations

import ipaddress
import socket
from collections.abc import Callable
from urllib.parse import urljoin, urlparse

import requests

from job_search.validation import RequestValidationError, http_url


class OutboundRequestError(RuntimeError):
    """Raised when a destination violates the outbound-request policy."""


SUPPORTED_BOARD_HOSTS = {
    "linkedin": {"linkedin.com", "www.linkedin.com"},
    "indeed": {"indeed.com", "www.indeed.com"},
}
REDIRECT_STATUS_CODES = {301, 302, 303, 307, 308}


class SafeHttpClient:
    """Resolve, validate, and manually follow HTTP redirects.

    Requests is deliberately called with redirects disabled so each location is
    checked before the next outbound connection.  DNS answers are validated at
    every hop; the resolver is injected for deterministic tests.
    """

    def __init__(
        self,
        *,
        resolver: Callable[..., list[tuple]] = socket.getaddrinfo,
        request: Callable[..., requests.Response] = requests.get,
        max_redirects: int = 5,
    ) -> None:
        self._resolver = resolver
        self._request = request
        self._max_redirects = max_redirects

    def get(self, service: str, url: str, *, headers: dict[str, str], timeout: int) -> requests.Response:
        current_url = url
        for redirect_count in range(self._max_redirects + 1):
            self._validate_destination(service, current_url)
            response = self._request(current_url, headers=headers, timeout=timeout, allow_redirects=False)
            if response.status_code not in REDIRECT_STATUS_CODES:
                return response
            location = response.headers.get("Location")
            if not location:
                return response
            if redirect_count == self._max_redirects:
                raise OutboundRequestError("Outbound request exceeded the redirect limit.")
            current_url = urljoin(current_url, location)
        raise AssertionError("Redirect loop should have returned or raised.")

    def _validate_destination(self, service: str, url: str) -> None:
        try:
            normalized = http_url(url, "Outbound URL")
        except RequestValidationError as exc:
            raise OutboundRequestError(str(exc)) from exc
        hostname = urlparse(normalized).hostname
        assert hostname is not None  # http_url already established this.
        hostname = hostname.lower()
        allowed_hosts = SUPPORTED_BOARD_HOSTS.get(service)
        if allowed_hosts and hostname not in allowed_hosts:
            raise OutboundRequestError(f"{service} requests must target an approved job-board host.")
        try:
            answers = self._resolver(hostname, None, type=socket.SOCK_STREAM)
        except socket.gaierror as exc:
            raise OutboundRequestError("Outbound hostname could not be resolved.") from exc
        addresses = {answer[4][0] for answer in answers if len(answer) >= 5 and answer[4]}
        if not addresses:
            raise OutboundRequestError("Outbound hostname did not resolve to an address.")
        for address in addresses:
            try:
                if not ipaddress.ip_address(address).is_global:
                    raise OutboundRequestError("Outbound hostname resolved to a private or reserved address.")
            except ValueError as exc:
                raise OutboundRequestError("Outbound hostname returned an invalid address.") from exc
