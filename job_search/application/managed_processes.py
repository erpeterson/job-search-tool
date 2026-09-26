"""Framework-independent entry points for managed worker and scheduler processes."""

from __future__ import annotations

from collections.abc import Mapping
from typing import Any, Protocol


class BackgroundTaskProcessor(Protocol):
    def process(self, claim: Mapping[str, Any]) -> tuple[str, str]: ...


class SearchRunner(Protocol):
    def run(self, *, trigger: str, force_refresh: bool) -> Mapping[str, Any]: ...


def process_background_task(processor: BackgroundTaskProcessor, claim: Mapping[str, Any]) -> tuple[str, str]:
    """Dispatch a claimed durable item through an application service."""
    return processor.process(claim)


def run_scheduled_search(runner: SearchRunner) -> Mapping[str, Any]:
    """Run the scheduled search with its fixed managed-process semantics."""
    return runner.run(trigger="scheduled", force_refresh=True)
