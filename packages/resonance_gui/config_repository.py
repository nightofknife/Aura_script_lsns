"""QSettings-backed preferences for the Resonance GUI."""

from __future__ import annotations

from dataclasses import dataclass
import json
from typing import Any

from PySide6.QtCore import QSettings


PC_TRADE_CITY_OPTIONS: tuple[tuple[str, str], ...] = (
    ("3", "七号自由港"),
    ("4", "澄明数据中心"),
    ("1", "修格里城"),
    ("5", "阿妮塔战备工厂"),
    ("7", "荒原站"),
    ("8", "曼德矿场"),
    ("9", "淘金乐园"),
    ("2", "铁盟哨站"),
)
DEFAULT_PC_TRADE_CITY_IDS = [city_id for city_id, _name in PC_TRADE_CITY_OPTIONS]


@dataclass(frozen=True)
class GuiPreferences:
    timeout_sec: float = 0.0
    history_limit: int = 50
    last_task_id: str = "market_latest"


DEFAULT_TRADE_INPUTS: dict[str, Any] = {
    "runtime_backend": "pc",
    "start_city_id": "",
    "all_plan": 0,
    "fatigue_budget": 100,
    "cargo_capacity": 650,
    "book_budget": 0,
    "book_profit_threshold": 0,
    "negotiation_budget": 0,
    "bargain_success_rates_bps": [5000],
    "bargain_step_bps": 1000,
    "raise_success_rates_bps": [5000],
    "raise_step_bps": 1000,
    "trade_level": 20,
    "available_city_ids": DEFAULT_PC_TRADE_CITY_IDS,
    "city_prestige": {"default": 20, "overrides": {}},
    "product_unlocks": {"mode": "all", "product_ids": []},
    "active_events": [],
    "use_fatigue_medicine": False,
    "allowed_fatigue_medicines": [],
    "fatigue_medicine_max_uses": 4,
}


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

    def load_trade_inputs(self) -> dict[str, Any]:
        raw = self.settings.value("trade/inputs_json", "")
        if raw:
            try:
                parsed = json.loads(str(raw))
                if isinstance(parsed, dict):
                    return _merge_trade_inputs(parsed)
            except (TypeError, ValueError):
                pass
        return _merge_trade_inputs({})

    def save_trade_inputs(self, inputs: dict[str, Any]) -> None:
        normalized = _merge_trade_inputs(inputs)
        self.settings.setValue("trade/inputs_json", json.dumps(normalized, ensure_ascii=False))


def _merge_trade_inputs(values: dict[str, Any]) -> dict[str, Any]:
    merged = json.loads(json.dumps(DEFAULT_TRADE_INPUTS, ensure_ascii=False))
    for key in merged:
        if key in values:
            merged[key] = values[key]
    return merged
