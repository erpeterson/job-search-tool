"""A guarded outbound HTTP adapter for job-board and posting requests."""

from __future__ import annotations

import http.client
import ipaddress
import socket
import ssl
from collections.abc import Callable
from dataclasses import dataclass
from typing import Protocol
from urllib.parse import urljoin, urlparse

import requests
from requests.structures import CaseInsensitiveDict

from job_search.validation import RequestValidationError, http_url


class OutboundRequestError(RuntimeError):
    """Raised when a destination violates the outbound-request policy."""


SUPPORTED_BOARD_HOSTS = {
    "linkedin": {"linkedin.com", "www.linkedin.com"},
    "indeed": {"indeed.com", "www.indeed.com"},
}
REDIRECT_STATUS_CODES = {301, 302, 303, 307, 308}


@dataclass(frozen=True)
class ResolvedDestination:
    """A validated network destination pinned for exactly one request hop."""

    url: str
    hostname: str
    address: str


class OutboundTransport(Protocol):
    """Transport contract that must connect to ``destination.address`` only."""

    def get(self, destination: ResolvedDestination, *, headers: dict[str, str], timeout: int) -> requests.Response: ...


class _PinnedHTTPSConnection(http.client.HTTPSConnection):
    """Connect to a numeric address while using the requested hostname for TLS."""

    def __init__(self, address: str, port: int, hostname: str, timeout: int) -> None:
        super().__init__(address, port=port, timeout=timeout, context=ssl.create_default_context())
        self._tls_hostname = hostname

    def connect(self) -> None:
        # ``self.host`` is a validated numeric address, so create_connection
        # cannot trigger a second hostname resolution.
        self.sock = socket.create_connection((self.host, self.port), self.timeout, self.source_address)
        self.sock = self._context.wrap_socket(self.sock, server_hostname=self._tls_hostname)


class PinnedAddressTransport:
    """Minimal HTTP(S) transport which never delegates DNS to a client library."""

    def get(self, destination: ResolvedDestination, *, headers: dict[str, str], timeout: int) -> requests.Response:
        parsed = urlparse(destination.url)
        port = parsed.port or (443 if parsed.scheme == "https" else 80)
        path = parsed.path or "/"
        if parsed.query:
            path = f"{path}?{parsed.query}"
        host_header = destination.hostname
        if parsed.port and parsed.port not in (80, 443):
            host_header = f"{host_header}:{parsed.port}"
        request_headers = {key: value for key, value in headers.items() if key.lower() != "host"}
        request_headers["Host"] = host_header

        if parsed.scheme == "https":
            connection: http.client.HTTPConnection = _PinnedHTTPSConnection(
                destination.address, port, destination.hostname, timeout
            )
        else:
            connection = http.client.HTTPConnection(destination.address, port=port, timeout=timeout)
        try:
            connection.request("GET", path, headers=request_headers)
            raw_response = connection.getresponse()
            response = requests.Response()
            response.status_code = raw_response.status
            response.reason = raw_response.reason
            response.headers = CaseInsensitiveDict(raw_response.getheaders())
            response._content = raw_response.read()
            response.url = destination.url
            response.encoding = requests.utils.get_encoding_from_headers(response.headers)
            return response
        except OSError as exc:
            raise OutboundRequestError("Pinned outbound connection failed.") from exc
        finally:
            connection.close()


class SafeHttpClient:
    """Validate DNS once per hop and connect only to the validated address.

    Redirects are followed manually so each target receives independent URL,
    allowlist, DNS, and IP-address validation. HTTPS uses the original hostname
    for both SNI and certificate verification while TCP is pinned to the
    validated numeric address.
    """

    def __init__(
        self,
        *,
        resolver: Callable[..., list[tuple]] = socket.getaddrinfo,
        transport: OutboundTransport | None = None,
        max_redirects: int = 5,
    ) -> None:
        self._resolver = resolver
        self._transport = transport or PinnedAddressTransport()
        self._max_redirects = max_redirects

    def get(self, service: str, url: str, *, headers: dict[str, str], timeout: int) -> requests.Response:
        current_url = url
        for redirect_count in range(self._max_redirects + 1):
            destination = self._validate_destination(service, current_url)
            response = self._transport.get(destination, headers=headers, timeout=timeout)
            if response.status_code not in REDIRECT_STATUS_CODES:
                return response
            location = response.headers.get("Location")
            if not location:
                return response
            if redirect_count == self._max_redirects:
                raise OutboundRequestError("Outbound request exceeded the redirect limit.")
            current_url = urljoin(current_url, location)
        raise AssertionError("Redirect loop should have returned or raised.")

    def _validate_destination(self, service: str, url: str) -> ResolvedDestination:
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
        addresses = [answer[4][0] for answer in answers if len(answer) >= 5 and answer[4]]
        if not addresses:
            raise OutboundRequestError("Outbound hostname did not resolve to an address.")
        for address in addresses:
            try:
                if ipaddress.ip_address(address).is_global:
                    return ResolvedDestination(normalized, hostname, address)
            except ValueError as exc:
                raise OutboundRequestError("Outbound hostname returned an invalid address.") from exc
        raise OutboundRequestError("Outbound hostname resolved only to private or reserved addresses.")
