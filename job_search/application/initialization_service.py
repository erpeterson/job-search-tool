"""Application startup initialization workflow."""

from __future__ import annotations

from collections.abc import Mapping
from contextlib import AbstractContextManager
from typing import Any, Protocol


class InitializationOperations(Protocol):
    def connection(self) -> AbstractContextManager[Any]: ...

    def initialize_schema(self, connection: Any) -> None: ...

    def initialize_defaults(self, connection: Any, defaults: Mapping[str, str]) -> None: ...

    def seed_queries(self, connection: Any) -> None: ...

    def remove_legacy_seeds(self, connection: Any) -> None: ...

    def disable_legacy_queries(self, connection: Any) -> None: ...


class InitializationService:
    def __init__(self, operations: InitializationOperations) -> None:
        self._operations = operations

    def initialize_database(self, defaults: Mapping[str, str]) -> None:
        with self._operations.connection() as connection:
            self._operations.initialize_schema(connection)
            self._operations.initialize_defaults(connection, defaults)
            self._operations.seed_queries(connection)
            self._operations.remove_legacy_seeds(connection)
            self._operations.disable_legacy_queries(connection)
