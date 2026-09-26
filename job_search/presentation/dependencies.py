"""Dependencies supplied to HTTP route registration by the composition root."""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any


@dataclass
class PresentationDependencies:
    """Application services made available to request handlers.

    The presentation package owns only the contract.  The composition root
    provides the concrete service instances or factories.
    """

    services: dict[str, Any] = field(default_factory=dict)
