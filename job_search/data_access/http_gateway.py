"""HTTP adapter with capture hooks supplied by the composition root."""

from __future__ import annotations

import time
from collections.abc import Callable, Mapping
from typing import Any

import requests

from job_search.http_client import OutboundRequestError


class CapturedResponse:
    def __init__(self, payload: Mapping[str, Any]) -> None:
        self.status_code = payload.get("status_code")
        self.headers = payload.get("headers") or {}
        self.text = payload.get("text") or ""
        self.ok = self.status_code is not None and 200 <= int(self.status_code) < 400

    def raise_for_status(self) -> None:
        if not self.ok:
            raise requests.HTTPError(f"{self.status_code} Error replayed from capture")


class CapturingHttpGateway:
    """Keep HTTP transport/capture behavior out of presentation handlers."""

    def __init__(
        self,
        client: Any,
        headers: Callable[[], Mapping[str, str]],
        read_capture: Callable[..., Mapping[str, Any] | None],
        write_capture: Callable[..., object],
        log_api_call: Callable[..., object],
        observe: Callable[..., None],
        redact_headers: Callable[[Mapping[str, str]], Mapping[str, str]],
    ) -> None:
        self._client = client
        self._headers = headers
        self._read_capture = read_capture
        self._write_capture = write_capture
        self._log_api_call = log_api_call
        self._observe = observe
        self._redact_headers = redact_headers

    def get(self, service: str, url: str, *, force_refresh: bool = False) -> Any:
        headers = self._headers()
        request_payload = {"method": "GET", "url": url, "headers": self._redact_headers(headers)}
        cached = self._read_capture(service, "http_get", request_payload, force_refresh=force_refresh)
        if cached:
            return CapturedResponse(cached["response"])
        started = time.monotonic()
        response = None
        error = None
        try:
            response = self._client.get(service, url, headers=headers, timeout=30)
            return response
        except (requests.RequestException, OutboundRequestError) as exc:
            error = exc
            self._observe(
                "http_outbound_failed",
                error_code="HTTP_OUTBOUND_FAILED",
                component="data_access.http_gateway",
                operation="get",
                service=service,
                cause=type(exc).__name__,
            )
            raise
        finally:
            elapsed_ms = int((time.monotonic() - started) * 1000)
            self._log_api_call(service, "GET", url, response=response, error=error, elapsed_ms=elapsed_ms)
            self._write_capture(
                service,
                "http_get",
                request_payload,
                {
                    "status_code": getattr(response, "status_code", None),
                    "headers": dict(getattr(response, "headers", {}) or {}),
                    "text": getattr(response, "text", None),
                    "error_type": type(error).__name__ if error else None,
                    "error_message": type(error).__name__ if error else None,
                },
                {"elapsed_ms": elapsed_ms},
            )
