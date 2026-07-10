"""QSettings-backed preferences for the Resonance GUI."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from PySide6.QtCore import QSettings


@dataclass(frozen=True)
class GuiPreferences:
    timeout_sec: float = 0.0
    history_limit: int = 50
    last_task_id: str = "market_latest"


class ResonanceConfigRepository:
    def __init__(self, settings: QSettings | None = None) -> None:
        self.settings = settings or QSettings("Aura", "ResonanceGui")

    def load_preferences(self) -> GuiPreferences:
        return GuiPreferences(
            timeout_sec=float(self.settings.value("runner/timeout_sec", 0.0)),
            history_limit=int(self.settings.value("history/limit", 50)),
            last_task_id=str(self.settings.value("workbench/last_task_id", "market_latest") or "market_latest"),
        )

    def save_preferences(self, preferences: GuiPreferences) -> None:
        self.settings.setValue("runner/timeout_sec", float(preferences.timeout_sec))
        self.settings.setValue("history/limit", int(preferences.history_limit))
        self.settings.setValue("workbench/last_task_id", preferences.last_task_id)

    def value(self, key: str, default: Any = None) -> Any:
        return self.settings.value(key, default)

    def set_value(self, key: str, value: Any) -> None:
        self.settings.setValue(key, value)
