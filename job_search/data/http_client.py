"""HTTP GET client with capture/replay and API call logging."""

import logging
import time

import requests

from job_search.observability import log_api_call, record_exception

REQUEST_HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like Gecko) "
        "Chrome/126 Safari/537.36"
    ),
    "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
    "Accept-Language": "en-US,en;q=0.9",
}


class CapturedResponse:
    """A replayed response exposing the subset of ``requests.Response`` used by callers."""

    def __init__(self, payload):
        self.status_code = payload.get("status_code")
        self.headers = payload.get("headers") or {}
        self.text = payload.get("text") or ""
        self.ok = self.status_code is not None and 200 <= int(self.status_code) < 400

    def raise_for_status(self):
        if not self.ok:
            raise requests.HTTPError(f"{self.status_code} Error replayed from capture")


class HttpClient:
    def __init__(self, captures, get=requests.get, timeout_seconds=30):
        self._captures = captures
        self._get = get
        self._timeout_seconds = timeout_seconds

    def fetch(self, service, url, force_refresh=False):
        request_payload = {"method": "GET", "url": url, "headers": REQUEST_HEADERS}
        cached = self._captures.read(service, "http_get", request_payload, force_refresh=force_refresh)
        if cached:
            return CapturedResponse(cached["response"])

        started = time.monotonic()
        response = None
        error = None
        try:
            response = self._get(url, headers=REQUEST_HEADERS, timeout=self._timeout_seconds)
            return response
        except requests.RequestException as exc:
            error = exc
            record_exception(
                "http_get_request_failed",
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
                "headers": dict(getattr(response, "headers", {}) or {}),
                "text": getattr(response, "text", None),
                "error_type": type(error).__name__ if error else None,
                "error_message": str(error) if error else None,
            }
            self._captures.write(service, "http_get", request_payload, response_payload, {"elapsed_ms": elapsed_ms})
