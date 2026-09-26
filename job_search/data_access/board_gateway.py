"""Concrete job-board adapter kept outside application services."""

from __future__ import annotations

from collections.abc import Callable, Mapping, Sequence
from typing import Any


class CallableBoardGateway:
    """Adapt existing board functions to the application-layer board port.

    This deliberately keeps legacy HTML parsing at the data-access boundary
    while callers depend only on ``JobBoardGateway``.
    """

    def __init__(
        self,
        linkedin: Callable[[str, str, bool], Sequence[Mapping[str, Any]]],
        indeed: Callable[[str, str, bool], Sequence[Mapping[str, Any]]],
    ) -> None:
        self._handlers = {"linkedin": linkedin, "indeed": indeed}

    def fetch(
        self, board: str, keywords: str, location: str, *, force_refresh: bool = False
    ) -> Sequence[Mapping[str, Any]]:
        try:
            handler = self._handlers[board.lower()]
        except KeyError as exc:
            raise ValueError(f"Unsupported board: {board}") from exc
        return handler(keywords, location, force_refresh)
