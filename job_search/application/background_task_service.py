"""Application use cases for durable background-task records."""

from __future__ import annotations

import uuid
from collections.abc import Callable, Mapping, Sequence
from typing import Any, Protocol


class BackgroundTaskRepository(Protocol):
    def initialize(self, timestamp: int) -> int: ...

    def create(self, task_id: str, operation: str, job_ids: Sequence[int], created_at: int) -> Mapping[str, Any]: ...

    def get(self, task_id: str) -> Mapping[str, Any] | None: ...

    def list(self, limit: int) -> Sequence[Mapping[str, Any]]: ...

    def update(self, task_id: str, updated_at: int, **updates: Any) -> Mapping[str, Any] | None: ...

    def update_item(self, task_id: str, job_id: int, updated_at: int, **updates: Any) -> Mapping[str, Any] | None: ...


class BackgroundTaskService:
    """Own durable-task record operations independently of HTTP handlers."""

    def __init__(
        self,
        repository: BackgroundTaskRepository,
        now: Callable[[], int],
        observe: Callable[[str], None] | Callable[..., None],
        new_task_id: Callable[[], str] = lambda: uuid.uuid4().hex,
    ) -> None:
        self._repository = repository
        self._now = now
        self._observe = observe
        self._new_task_id = new_task_id

    def start(self, operation: str, job_ids: Sequence[int]) -> Mapping[str, Any]:
        task_id = self._new_task_id()
        task = self._repository.create(task_id, operation, job_ids, self._now())
        self._observe("background_task_queued", task_id=task_id, operation=operation, job_ids=list(job_ids))
        return task

    def initialize(self) -> int:
        return self._repository.initialize(self._now())

    def get(self, task_id: str) -> Mapping[str, Any] | None:
        return self._repository.get(task_id)

    def list(self, limit: int = 10) -> Sequence[Mapping[str, Any]]:
        return self._repository.list(limit)

    def update(self, task_id: str, **updates: Any) -> Mapping[str, Any] | None:
        return self._repository.update(task_id, self._now(), **updates)

    def update_item(self, task_id: str, job_id: int, **updates: Any) -> Mapping[str, Any] | None:
        return self._repository.update_item(task_id, job_id, self._now(), **updates)
