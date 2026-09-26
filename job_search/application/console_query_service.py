"""Read-side use cases for the console presentation."""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from typing import Any, Protocol


class ConsoleQueryRepository(Protocol):
    def jobs(self, *, include_filtered: bool) -> Sequence[Mapping[str, Any]]: ...

    def job(self, job_id: int) -> Mapping[str, Any] | None: ...

    def company_interests(self) -> Sequence[Mapping[str, Any]]: ...

    def company_interest(self, company_id: int) -> Mapping[str, Any] | None: ...

    def queries(self) -> Sequence[Mapping[str, Any]]: ...

    def runs(self) -> Sequence[Mapping[str, Any]]: ...

    def discoveries(self, limit: int) -> Sequence[Mapping[str, Any]]: ...

    def packets(self) -> Sequence[Mapping[str, Any]]: ...

    def settings(self) -> Mapping[str, str]: ...


class ConsoleQueryService:
    def __init__(self, repository: ConsoleQueryRepository) -> None:
        self._repository = repository

    def state(self, *, include_filtered: bool) -> Mapping[str, Any]:
        return {
            "settings": self._repository.settings(),
            "jobs": self._repository.jobs(include_filtered=include_filtered),
            "company_interests": self._repository.company_interests(),
            "search_queries": self._repository.queries(),
            "search_runs": self._repository.runs(),
            "discoveries": self._repository.discoveries(50),
            "application_packets": self._repository.packets(),
        }

    def job(self, job_id: int) -> Mapping[str, Any] | None:
        return self._repository.job(job_id)

    def packets(self) -> Sequence[Mapping[str, Any]]:
        return self._repository.packets()

    def jobs(self, *, include_filtered: bool) -> Sequence[Mapping[str, Any]]:
        return self._repository.jobs(include_filtered=include_filtered)

    def settings(self) -> Mapping[str, str]:
        return self._repository.settings()

    def queries(self) -> Sequence[Mapping[str, Any]]:
        return self._repository.queries()

    def company(self, company_id: int) -> Mapping[str, Any] | None:
        return self._repository.company_interest(company_id)

    def companies(self) -> Sequence[Mapping[str, Any]]:
        return self._repository.company_interests()
