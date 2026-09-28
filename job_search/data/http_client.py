"""HTTP GET client with SSRF guards, capture/replay, and API call logging."""

import ipaddress
import logging
import socket
import time
from urllib.parse import urljoin, urlparse

import requests

from job_search.domain.errors import AppError, ExternalServiceError, ValidationError
from job_search.observability import log_api_call, record_exception

REQUEST_HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like Gecko) "
        "Chrome/126 Safari/537.36"
    ),
    "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
    "Accept-Language": "en-US,en;q=0.9",
}
MAX_REDIRECTS = 5
# Credentials and session cookies are never written to captures.
SENSITIVE_HEADERS = frozenset({"set-cookie", "cookie", "authorization", "proxy-authorization"})
REDIRECT_STATUSES = frozenset({301, 302, 303, 307, 308})
DEFAULT_MAX_RESPONSE_BYTES = 5 * 1024 * 1024
_CHUNK_BYTES = 64 * 1024


class UnsafeUrlError(ValidationError):
    """A URL targets a non-public address or uses a disallowed scheme."""


class HttpResponse:
    """A fully read, size-bounded response exposing the subset of ``requests.Response`` callers use."""

    def __init__(self, status_code, headers, text, replayed=False):
        self.status_code = status_code
        self.headers = headers or {}
        self.text = text or ""
        self.ok = status_code is not None and 200 <= int(status_code) < 400
        self._replayed = replayed

    @classmethod
    def from_capture(cls, payload):
        return cls(payload.get("status_code"), payload.get("headers"), payload.get("text"), replayed=True)

    def raise_for_status(self):
        if not self.ok:
            suffix = " replayed from capture" if self._replayed else ""
            raise requests.HTTPError(f"{self.status_code} Error{suffix}")


def _resolve_all(host, port):
    return [info[4][0] for info in socket.getaddrinfo(host, port, type=socket.SOCK_STREAM)]


def _http_succeeded(response):
    status = response.get("status_code")
    return isinstance(status, int) and 200 <= status < 300 and not response.get("error_type")


class HttpClient:
    def __init__(
        self,
        captures,
        get=requests.get,
        timeout_seconds=30,
        resolve=_resolve_all,
        max_response_bytes=DEFAULT_MAX_RESPONSE_BYTES,
    ):
        self._captures = captures
        self._get = get
        self._timeout_seconds = timeout_seconds
        self._resolve = resolve
        self._max_response_bytes = max_response_bytes

    def check_url(self, url):
        """Require http(s) and a host whose every resolved address is public.

        The check runs before each request and redirect hop. It narrows but does not
        eliminate DNS-rebinding races between resolution and connection.
        """
        parsed = urlparse(url)
        if parsed.scheme not in ("http", "https") or not parsed.hostname:
            raise UnsafeUrlError("URL must be an absolute http(s) URL.", "http_url_invalid_scheme")
        port = parsed.port or (443 if parsed.scheme == "https" else 80)
        try:
            addresses = self._resolve(parsed.hostname, port)
        except (OSError, UnicodeError) as exc:
            record_exception(
                "http_host_resolution_failed",
                "data.http_client",
                "check_url",
                exc,
                level=logging.WARNING,
                host=parsed.hostname,
            )
            raise ExternalServiceError(
                f"Could not resolve host {parsed.hostname!r}.", "http_host_unresolvable"
            ) from exc
        if not addresses or not all(ipaddress.ip_address(address.split("%")[0]).is_global for address in addresses):
            raise UnsafeUrlError(
                "URL resolves to a private, loopback, or reserved address.", "http_host_resolves_private"
            )

    @property
    def deadline_seconds(self):
        """Upper bound on one fetch, including redirects and slow bodies."""
        return self._timeout_seconds * (MAX_REDIRECTS + 1)

    def _check_deadline(self, deadline, response=None):
        if time.monotonic() > deadline:
            if response is not None:
                response.close()
            raise ExternalServiceError(
                f"Request exceeded the {self.deadline_seconds}-second fetch deadline.", "http_fetch_deadline_exceeded"
            )

    def _read_limited(self, response, deadline):
        chunks = []
        size = 0
        for chunk in response.iter_content(chunk_size=_CHUNK_BYTES):
            self._check_deadline(deadline, response)
            size += len(chunk)
            if size > self._max_response_bytes:
                response.close()
                raise ExternalServiceError(
                    f"Response exceeded {self._max_response_bytes} bytes.", "http_response_too_large"
                )
            chunks.append(chunk)
        body = b"".join(chunks)
        return body.decode(response.encoding or "utf-8", errors="replace")

    def _get_following_redirects(self, url):
        deadline = time.monotonic() + self.deadline_seconds
        self.check_url(url)
        current = url
        for _ in range(MAX_REDIRECTS + 1):
            self._check_deadline(deadline)
            response = self._get(
                current, headers=REQUEST_HEADERS, timeout=self._timeout_seconds, allow_redirects=False, stream=True
            )
            location = response.headers.get("Location")
            if response.status_code in REDIRECT_STATUSES and location:
                response.close()
                current = urljoin(current, location)
                try:
                    self.check_url(current)
                except UnsafeUrlError as exc:
                    raise UnsafeUrlError(
                        "Redirect target resolves to a private, loopback, or reserved address.",
                        "http_redirect_blocked",
                    ) from exc
                continue
            return HttpResponse(response.status_code, dict(response.headers), self._read_limited(response, deadline))
        raise ExternalServiceError(f"More than {MAX_REDIRECTS} redirects.", "http_too_many_redirects")

    def fetch(self, service, url, force_refresh=False):
        request_payload = {"method": "GET", "url": url, "headers": REQUEST_HEADERS}
        cached = self._captures.read(
            service, "http_get", request_payload, force_refresh=force_refresh, is_success=_http_succeeded
        )
        if cached:
            return HttpResponse.from_capture(cached["response"])

        started = time.monotonic()
        response = None
        error = None
        try:
            response = self._get_following_redirects(url)
            return response
        except (requests.RequestException, AppError) as exc:
            error = exc
            record_exception(
                getattr(exc, "error_code", "http_get_request_failed"),
                "data.http_client",
                "fetch",
                exc,
                level=logging.WARNING,
                service=service,
                url=url,
            )
            raise
        finally:
            elapsed_ms = int((time.monotonic() - started) * 1000)
            log_api_call(service, "GET", url, response=response, error=error, elapsed_ms=elapsed_ms)
            response_payload = {
                "status_code": getattr(response, "status_code", None),
                "headers": {
                    name: value
                    for name, value in dict(getattr(response, "headers", {}) or {}).items()
                    if name.lower() not in SENSITIVE_HEADERS
                },
                "text": getattr(response, "text", None),
                "error_type": type(error).__name__ if error else None,
                "error_message": str(error) if error else None,
            }
            self._captures.write(
                service,
                "http_get",
                request_payload,
                response_payload,
                {"elapsed_ms": elapsed_ms},
                succeeded=_http_succeeded(response_payload),
            )
