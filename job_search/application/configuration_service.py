"""Coordinate runtime configuration updates with persisted model settings."""

from __future__ import annotations

from collections.abc import Mapping
from typing import Protocol


class RuntimeConfigurationPort(Protocol):
    def update(self, values: Mapping[str, str]) -> None: ...


class SettingsPort(Protocol):
    def save(self, values: Mapping[str, str]) -> None: ...


class ConfigurationService:
    def __init__(self, configuration: RuntimeConfigurationPort, settings: SettingsPort) -> None:
        self._configuration = configuration
        self._settings = settings

    def update(self, values: Mapping[str, str]) -> None:
        self._configuration.update(values)
        if "CODEX_MODEL" in values:
            self._settings.save({"codex_model": values["CODEX_MODEL"]})
