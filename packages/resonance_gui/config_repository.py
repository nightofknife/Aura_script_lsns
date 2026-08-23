"""QSettings-backed preferences for the Resonance GUI."""

from __future__ import annotations

from dataclasses import dataclass
import json
from pathlib import Path
from typing import Any

from PySide6.QtCore import QSettings

from .paths import resolve_application_root


PC_TRADE_CITY_OPTIONS: tuple[tuple[str, str], ...] = (
    ("1", "修格里城"),
    ("2", "铁盟哨站"),
    ("3", "七号自由港"),
    ("4", "澄明数据中心"),
    ("5", "阿妮塔战备工厂"),
    ("6", "阿妮塔能源研究所"),
    ("7", "荒原站"),
    ("8", "曼德矿场"),
    ("9", "淘金乐园"),
    ("10", "阿妮塔发射中心"),
    ("11", "海角城"),
    ("12", "云岫桥基地"),
    ("13", "汇流塔"),
    ("14", "远星大桥"),
    ("15", "岚心城"),
    ("16", "栖羽站"),
    ("17", "塔图站"),
    ("18", "黑月游乐城"),
    ("19", "贡露城"),
    ("20", "维蒂林场"),
    ("21", "武林源"),
)
ALL_PC_TRADE_CITY_IDS = [city_id for city_id, _name in PC_TRADE_CITY_OPTIONS]
DEFAULT_PC_TRADE_CITY_IDS = [
    city_id for city_id in ALL_PC_TRADE_CITY_IDS if city_id != "21"
]
_LEGACY_DEFAULT_PC_TRADE_CITY_ID_SETS = (
    frozenset(("3", "4", "1", "5", "7", "8", "9", "2")),
    frozenset(
        city_id
        for city_id in (str(value) for value in range(1, 21))
        if city_id not in {"14", "17", "19"}
    ),
    frozenset(str(city_id) for city_id in range(1, 21)),
)
_CITY_DEFAULTS_VERSION = 2


@dataclass(frozen=True)
class GuiPreferences:
    timeout_sec: float = 0.0
    history_limit: int = 50
    last_task_id: str = "market_latest"


DEFAULT_TRADE_INPUTS: dict[str, Any] = {
    "start_city_id": "",
    "fatigue_budget": 700,
    "cargo_capacity": 750,
    "book_budget": 0,
    "book_profit_threshold": 15000,
    "negotiation_max_attempts": 5,
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
    "arrival_timeout_seconds": 3600,
    "auto_cape_island_investment": True,
    "auto_rubbish_recycling": True,
}

DEFAULT_PASSENGER_INPUTS: dict[str, Any] = {
    "passenger_city_a_id": "11",
    "passenger_city_b_id": "15",
    "trip_count": 1,
    "trade_during_trip": True,
    "reposition_to_route": True,
    "use_fatigue_medicine": False,
    "allowed_fatigue_medicines": [],
    "fatigue_medicine_max_uses": 4,
    "arrival_timeout_seconds": 1800,
}

PLAYER_DATA_STAGE_ORDER: tuple[str, ...] = (
    "location",
    "profile",
    "inventory",
)
PLAYER_DATA_INVENTORY_CATEGORY_ORDER: tuple[str, ...] = ("items", "materials")
DEFAULT_PLAYER_DATA_INPUTS: dict[str, Any] = {
    "stages": [*PLAYER_DATA_STAGE_ORDER],
    "inventory_categories": ["items"],
}

DEFAULT_BATTLE_INPUTS: dict[str, Any] = {
    "jobs": [],
    "stop_on_failure": True,
}


def create_portable_settings(
    base_path: Path | None = None,
    *,
    legacy_settings: QSettings | None = None,
) -> QSettings:
    root = Path(base_path).resolve() if base_path is not None else resolve_application_root()
    settings_path = root / "gui-settings.ini"
    portable = QSettings(str(settings_path), QSettings.Format.IniFormat)

    if settings_path.exists() or portable.allKeys():
        return portable

    legacy = legacy_settings if legacy_settings is not None else QSettings("Aura", "ResonanceGui")
    legacy_keys = list(legacy.allKeys())
    if not legacy_keys:
        return portable

    for key in legacy_keys:
        portable.setValue(key, legacy.value(key))
    portable.sync()
    if portable.status() == QSettings.Status.NoError:
        legacy.clear()
        legacy.sync()
    return portable


class ResonanceConfigRepository:
    def __init__(
        self,
        settings: QSettings | None = None,
        *,
        base_path: Path | None = None,
        legacy_settings: QSettings | None = None,
    ) -> None:
        self.settings = (
            settings
            if settings is not None
            else create_portable_settings(base_path, legacy_settings=legacy_settings)
        )

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
        self.settings.sync()

    def value(self, key: str, default: Any = None) -> Any:
        return self.settings.value(key, default)

    def set_value(self, key: str, value: Any) -> None:
        self.settings.setValue(key, value)
        self.settings.sync()

    def load_trade_inputs(self) -> dict[str, Any]:
        raw = self.settings.value("trade/inputs_json", "")
        if raw:
            try:
                parsed = json.loads(str(raw))
                if isinstance(parsed, dict):
                    parsed = self._migrate_trade_city_defaults(parsed)
                    return _merge_trade_inputs(parsed)
            except (TypeError, ValueError):
                pass
        self.settings.setValue("trade/city_defaults_version", _CITY_DEFAULTS_VERSION)
        self.settings.sync()
        return _merge_trade_inputs({})

    def save_trade_inputs(self, inputs: dict[str, Any]) -> None:
        normalized = _merge_trade_inputs(inputs)
        self.settings.setValue("trade/inputs_json", json.dumps(normalized, ensure_ascii=False))
        self.settings.setValue("trade/city_defaults_version", _CITY_DEFAULTS_VERSION)
        self.settings.sync()

    def _migrate_trade_city_defaults(self, inputs: dict[str, Any]) -> dict[str, Any]:
        try:
            defaults_version = int(self.settings.value("trade/city_defaults_version", 1))
        except (TypeError, ValueError):
            defaults_version = 1
        if defaults_version >= _CITY_DEFAULTS_VERSION:
            return inputs

        migrated = dict(inputs)
        raw_city_ids = migrated.get("available_city_ids")
        selected = {
            str(city_id)
            for city_id in (raw_city_ids if isinstance(raw_city_ids, list) else [])
        }
        if selected == set(ALL_PC_TRADE_CITY_IDS):
            migrated["available_city_ids"] = list(DEFAULT_PC_TRADE_CITY_IDS)
            self.settings.setValue(
                "trade/inputs_json",
                json.dumps(migrated, ensure_ascii=False),
            )
        self.settings.setValue("trade/city_defaults_version", _CITY_DEFAULTS_VERSION)
        self.settings.sync()
        return migrated

    def load_passenger_inputs(self) -> dict[str, Any]:
        raw = self.settings.value("passenger/inputs_json", "")
        if raw:
            try:
                parsed = json.loads(str(raw))
                if isinstance(parsed, dict):
                    return _merge_passenger_inputs(parsed)
            except (TypeError, ValueError):
                pass
        return _merge_passenger_inputs({})

    def save_passenger_inputs(self, inputs: dict[str, Any]) -> None:
        normalized = _merge_passenger_inputs(inputs)
        self.settings.setValue("passenger/inputs_json", json.dumps(normalized, ensure_ascii=False))
        self.settings.sync()

    def load_player_data_inputs(self) -> dict[str, Any]:
        raw = self.settings.value("player_data/inputs_json", "")
        if raw:
            try:
                parsed = json.loads(str(raw))
                if isinstance(parsed, dict):
                    return _merge_player_data_inputs(parsed)
            except (TypeError, ValueError):
                pass
        return _merge_player_data_inputs({})

    def save_player_data_inputs(self, inputs: dict[str, Any]) -> None:
        normalized = _merge_player_data_inputs(inputs)
        self.settings.setValue(
            "player_data/inputs_json",
            json.dumps(normalized, ensure_ascii=False),
        )
        self.settings.sync()

    def load_battle_inputs(self) -> dict[str, Any]:
        raw = self.settings.value("battle/inputs_json", "")
        if raw:
            try:
                parsed = json.loads(str(raw))
                if isinstance(parsed, dict):
                    return _merge_battle_inputs(parsed)
            except (TypeError, ValueError):
                pass
        return _merge_battle_inputs({})

    def save_battle_inputs(self, inputs: dict[str, Any]) -> None:
        normalized = _merge_battle_inputs(inputs)
        self.settings.setValue("battle/inputs_json", json.dumps(normalized, ensure_ascii=False))
        self.settings.sync()


def _merge_trade_inputs(values: dict[str, Any]) -> dict[str, Any]:
    merged = json.loads(json.dumps(DEFAULT_TRADE_INPUTS, ensure_ascii=False))
    for key in merged:
        if key in values:
            merged[key] = values[key]
    raw_city_ids = merged.get("available_city_ids")
    normalized_city_ids = list(
        dict.fromkeys(
            str(city_id)
            for city_id in (raw_city_ids if isinstance(raw_city_ids, list) else [])
            if str(city_id) in ALL_PC_TRADE_CITY_IDS
        )
    )
    raw_city_id_set = frozenset(
        str(city_id)
        for city_id in (raw_city_ids if isinstance(raw_city_ids, list) else [])
    )
    if (
        len(normalized_city_ids) < 2
        or raw_city_id_set in _LEGACY_DEFAULT_PC_TRADE_CITY_ID_SETS
    ):
        normalized_city_ids = list(DEFAULT_PC_TRADE_CITY_IDS)
    merged["available_city_ids"] = normalized_city_ids
    merged["auto_cape_island_investment"] = bool(merged["auto_cape_island_investment"])
    merged["auto_rubbish_recycling"] = bool(merged["auto_rubbish_recycling"])
    try:
        merged["arrival_timeout_seconds"] = max(int(merged["arrival_timeout_seconds"]), 1)
    except (TypeError, ValueError):
        merged["arrival_timeout_seconds"] = 3600
    return merged


def _merge_passenger_inputs(values: dict[str, Any]) -> dict[str, Any]:
    merged = json.loads(json.dumps(DEFAULT_PASSENGER_INPUTS, ensure_ascii=False))
    for key in merged:
        if key in values:
            merged[key] = values[key]
    try:
        merged["trip_count"] = max(int(merged["trip_count"]), 1)
    except (TypeError, ValueError):
        merged["trip_count"] = 1
    city_a_id = str(merged.get("passenger_city_a_id") or "11").strip()
    city_b_id = str(merged.get("passenger_city_b_id") or "15").strip()
    if city_a_id not in ALL_PC_TRADE_CITY_IDS:
        city_a_id = "11"
    if city_b_id not in ALL_PC_TRADE_CITY_IDS:
        city_b_id = "15"
    if city_a_id == city_b_id:
        city_b_id = "15" if city_a_id != "15" else "11"
    merged["passenger_city_a_id"] = city_a_id
    merged["passenger_city_b_id"] = city_b_id
    merged["reposition_to_route"] = bool(merged["reposition_to_route"])
    merged["trade_during_trip"] = bool(merged["trade_during_trip"])
    merged["use_fatigue_medicine"] = bool(merged["use_fatigue_medicine"])
    raw_medicines = merged.get("allowed_fatigue_medicines")
    merged["allowed_fatigue_medicines"] = [
        str(value).strip()
        for value in (raw_medicines if isinstance(raw_medicines, list) else [])
        if str(value).strip()
    ]
    try:
        merged["fatigue_medicine_max_uses"] = max(int(merged["fatigue_medicine_max_uses"]), 0)
    except (TypeError, ValueError):
        merged["fatigue_medicine_max_uses"] = 4
    try:
        merged["arrival_timeout_seconds"] = max(int(merged["arrival_timeout_seconds"]), 1)
    except (TypeError, ValueError):
        merged["arrival_timeout_seconds"] = 1800
    return merged


def _merge_player_data_inputs(values: dict[str, Any]) -> dict[str, Any]:
    raw_stages = values.get("stages")
    if not isinstance(raw_stages, list):
        return json.loads(json.dumps(DEFAULT_PLAYER_DATA_INPUTS, ensure_ascii=False))

    selected = {str(value).strip() for value in raw_stages}
    if selected.intersection({"clarity", "fatigue"}):
        selected.add("profile")
    if "currencies" in selected:
        selected.add("inventory")
    data_stages = [stage for stage in PLAYER_DATA_STAGE_ORDER if stage in selected]
    if not data_stages:
        return json.loads(json.dumps(DEFAULT_PLAYER_DATA_INPUTS, ensure_ascii=False))
    raw_categories = values.get("inventory_categories")
    selected_categories = (
        {str(value).strip() for value in raw_categories}
        if isinstance(raw_categories, list)
        else {"items"}
    )
    inventory_categories = [
        category
        for category in PLAYER_DATA_INVENTORY_CATEGORY_ORDER
        if category in selected_categories
    ]
    if not inventory_categories:
        inventory_categories = ["items"]
    if "currencies" in selected and "items" not in inventory_categories:
        inventory_categories.insert(0, "items")
    return {
        "stages": data_stages,
        "inventory_categories": inventory_categories,
    }


def _merge_battle_inputs(values: dict[str, Any]) -> dict[str, Any]:
    raw_jobs = values.get("jobs")
    jobs = [
        dict(job)
        for job in (raw_jobs if isinstance(raw_jobs, list) else [])
        if isinstance(job, dict) and str(job.get("route_id") or "").strip()
    ][:50]
    return {
        "jobs": jobs,
        "stop_on_failure": bool(values.get("stop_on_failure", True)),
    }
