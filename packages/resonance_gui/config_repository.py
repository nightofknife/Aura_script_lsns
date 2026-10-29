"""QSettings-backed preferences for the Resonance GUI."""

from __future__ import annotations

from dataclasses import dataclass
import json
from pathlib import Path
from typing import Any, Mapping

from PySide6.QtCore import QSettings

from .paths import resolve_application_root
from .logic import validate_trade_goods_investment_level, validate_pickup_click_interval, validate_trailer_options, validate_city_tax_rates_bps, validate_allow_stale_market
from .black_moon_catalog import normalize_black_moon_settings
from .affix_catalog import normalize_affix_inputs
from .battle_plan import normalize_battle_editor_inputs
from packages.resonance_shared.freight_parameters import (
    DEFAULT_RECOVERY_THRESHOLDS,
)


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
PORTABLE_SETTINGS_FILENAME = "gui-settings-layout-v2.ini"
TRADE_INPUTS_KEY = "trade/two_mode_inputs_json"
TRADE_PREVIEW_INPUTS_KEY = "trade_preview/two_mode_inputs_json"


@dataclass(frozen=True)
class GuiPreferences:
    timeout_sec: float = 0.0
    history_limit: int = 50


DEFAULT_TRADE_INPUTS: dict[str, Any] = {
    "start_city_id": "",
    "fatigue_budget": 700,
    "cargo_capacity": 750,
    "book_budget": 0,
    "trade_mode": "free",
    "book_policy": "profit",
    "negotiation_policy": "auto",
    "books_enabled": False,
    "books_unlimited": False,
    "finite_book_budget": 0,
    "fixed_route_city_ids": [],
    "reposition_to_route": False,
    "target_profit": None,
    "target_profit_enabled": False,
    "book_profit_threshold": 500000,
    "book_waste_limit": 0,
    "negotiation_max_attempts": 5,
    "bargain_success_rates_bps": [5000],
    "bargain_step_bps": 1000,
    "raise_success_rates_bps": [5000],
    "raise_step_bps": 1000,
    "available_city_ids": DEFAULT_PC_TRADE_CITY_IDS,
    "required_end_city_ids": None,
    "city_prestige": {"default": 20, "overrides": {}},
    "city_tax_rates_bps": {},
    "product_unlocks": {"mode": "all", "product_ids": []},
    "auto_sparkling_water": False,
    "auto_bento": False,
    "prison_product_goods": [],
    "bento_priority": ["work_meals", "love_bentos"],
    **DEFAULT_RECOVERY_THRESHOLDS,
    "auto_pickup": False,
    "allow_stale_market": False,
    "auto_pickup_interval_sec": 0.5,
    "auto_trailer": False,
    "trailer_daily_first_n": 1,
    "trailer_payment": "gold",
    "use_fatigue_medicine": False,
    "allowed_fatigue_medicines": [],
    "fatigue_medicine_max_uses": 4,
    "arrival_timeout_seconds": 3600,
    "auto_cape_island_investment": True,
    "auto_trade_goods_investment": False,
    "trade_goods_investment_mode": 10,
    "auto_rubbish_recycling": True,
    "auto_black_moon_local_purchase": False,
    "black_moon_purchase_table": {},
    "black_moon_purchase_record_scope": "default",
}

# Planning inputs declared by preview_trade_plan_pc.yaml.
TRADE_PREVIEW_INPUT_KEYS: tuple[str, ...] = (
    "start_city_id",
    "fatigue_budget",
    "cargo_capacity",
    "book_budget",
    "trade_mode",
    "book_policy",
    "negotiation_policy",
    "books_enabled",
    "books_unlimited",
    "finite_book_budget",
    "fixed_route_city_ids",
    "reposition_to_route",
    "target_profit",
    "target_profit_enabled",
    "book_profit_threshold",
    "book_waste_limit",
    "bargain_success_rates_bps",
    "bargain_step_bps",
    "raise_success_rates_bps",
    "raise_step_bps",
    "available_city_ids",
    "required_end_city_ids",
    "city_prestige",
    "city_tax_rates_bps",
    "product_unlocks",
)

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
    "characters",
    "prison_products",
)
PLAYER_DATA_INVENTORY_CATEGORY_ORDER: tuple[str, ...] = (
    "items",
    "materials",
    "equipment",
)
PLAYER_DATA_PROFILE_SECTION_ORDER: tuple[str, ...] = (
    "cargo", "clarity", "fatigue", "sparkling_water", "bento_count",
)
DEFAULT_PROFILE_SECTIONS: tuple[str, ...] = ("cargo", "clarity", "fatigue")
PLAYER_DATA_INPUTS_SCHEMA_VERSION = 4
DEFAULT_PLAYER_DATA_INPUTS: dict[str, Any] = {
    "stages": [stage for stage in PLAYER_DATA_STAGE_ORDER if stage != "prison_products"],
    "inventory_categories": ["items"],
    "profile_sections": [*DEFAULT_PROFILE_SECTIONS],
}

DEFAULT_BATTLE_INPUTS: dict[str, Any] = {
    "jobs": [],
    "stop_on_failure": True,
}


def create_portable_settings(
    base_path: Path | None = None,
) -> QSettings:
    """Store current GUI preferences in the portable layout-v2 namespace."""
    root = Path(base_path).resolve() if base_path is not None else resolve_application_root()
    settings_path = root / PORTABLE_SETTINGS_FILENAME
    return QSettings(str(settings_path), QSettings.Format.IniFormat)


class ResonanceConfigRepository:
    def __init__(
        self,
        settings: QSettings | None = None,
        *,
        base_path: Path | None = None,
    ) -> None:
        self.settings = (
            settings
            if settings is not None
            else create_portable_settings(base_path)
        )

    def load_preferences(self) -> GuiPreferences:
        return GuiPreferences(
            timeout_sec=float(self.settings.value("runner/timeout_sec", 0.0)),
            history_limit=int(self.settings.value("history/limit", 50)),
        )

    def save_preferences(self, preferences: GuiPreferences) -> None:
        self.settings.setValue("runner/timeout_sec", float(preferences.timeout_sec))
        self.settings.setValue("history/limit", int(preferences.history_limit))
        self.settings.sync()

    def value(self, key: str, default: Any = None) -> Any:
        return self.settings.value(key, default)

    def set_value(self, key: str, value: Any) -> None:
        self.settings.setValue(key, value)
        self.settings.sync()

    def sync_checked(self) -> None:
        """Flush settings and report persistence errors to explicit save callers."""
        self.settings.sync()
        status = self.settings.status()
        if status != QSettings.Status.NoError:
            reason = (
                "配置文件或目录不可写"
                if status == QSettings.Status.AccessError
                else "配置文件格式错误"
            )
            raise OSError(f"{reason}：{self.settings.fileName()}")

    def load_hotkeys(self) -> dict[str, str]:
        try:
            values = json.loads(str(self.value("execution/hotkeys_json", "{}") or "{}"))
        except (TypeError, ValueError):
            values = {}
        if not isinstance(values, dict):
            values = {}
        return {key: values.get(key) if isinstance(values.get(key), str) else ""
                for key in ("start", "stop")}

    def save_hotkeys(self, start: str, stop: str) -> None:
        """Store the pair together; retain the previous pair on a failed flush."""
        key = "execution/hotkeys_json"
        previous = self.settings.value(key)
        self.settings.setValue(key, json.dumps({"start": start, "stop": stop}, ensure_ascii=False))
        try:
            self.sync_checked()
        except OSError:
            if previous is None:
                self.settings.remove(key)
            else:
                self.settings.setValue(key, previous)
            self.settings.sync()
            raise

    def load_trade_inputs(self) -> dict[str, Any]:
        raw = self.settings.value(TRADE_INPUTS_KEY, "")
        if raw:
            try:
                parsed = json.loads(str(raw))
            except (TypeError, ValueError):
                parsed = None
            if isinstance(parsed, dict):
                validate_trade_goods_investment_level(parsed.get("trade_goods_investment_mode", 10))
                validate_pickup_click_interval(parsed.get("auto_pickup_interval_sec", 0.5))
                # Invalid purchase settings must not silently reset all freight fields.
                normalize_black_moon_settings(parsed)
                validate_city_tax_rates_bps(parsed.get("city_tax_rates_bps", {}))
                validate_allow_stale_market(parsed.get("allow_stale_market", False))
                try:
                    return _merge_trade_inputs(parsed)
                except (TypeError, ValueError):
                    pass
        return _merge_trade_inputs({})

    def save_trade_inputs(self, inputs: dict[str, Any]) -> None:
        normalized = _merge_trade_inputs(inputs)
        keys = (TRADE_INPUTS_KEY, "trade/city_defaults_version")
        previous = {key: self.settings.value(key) for key in keys}
        self.settings.setValue(TRADE_INPUTS_KEY, json.dumps(normalized, ensure_ascii=False))
        self.settings.setValue("trade/city_defaults_version", _CITY_DEFAULTS_VERSION)
        try:
            self.sync_checked()
        except OSError:
            for key, value in previous.items():
                if value is None:
                    self.settings.remove(key)
                else:
                    self.settings.setValue(key, value)
            self.settings.sync()
            raise

    def save_black_moon_settings(self, values: dict[str, Any]) -> None:
        """Commit this tool only; do not collect or overwrite unsaved mode fields."""
        normalized = normalize_black_moon_settings(values, require_catalog=True)
        current = self.load_trade_inputs()
        current.update(normalized)
        self.save_trade_inputs(current)

    def _save_freight_inputs_json(self, key: str, values: dict[str, Any]) -> None:
        previous = self.settings.value(key)
        self.settings.setValue(key, json.dumps(values, ensure_ascii=False))
        try:
            self.sync_checked()
        except OSError:
            if previous is None:
                self.settings.remove(key)
            else:
                self.settings.setValue(key, previous)
            self.settings.sync()
            raise

    def load_trade_preview_inputs(self) -> dict[str, Any]:
        """Load independent planning inputs, seeding normal trade inputs once."""
        key = TRADE_PREVIEW_INPUTS_KEY
        if not self.settings.contains(key):
            normalized = _merge_trade_preview_inputs(self.load_trade_inputs())
            self.save_trade_preview_inputs(normalized)
            return normalized
        raw = self.settings.value(key, "")
        try:
            parsed = json.loads(str(raw))
        except (TypeError, ValueError):
            parsed = None
        if isinstance(parsed, dict):
            validate_city_tax_rates_bps(parsed.get("city_tax_rates_bps", {}))
            try:
                return _merge_trade_preview_inputs(parsed)
            except (TypeError, ValueError):
                pass
        normalized = _merge_trade_preview_inputs({})
        self.save_trade_preview_inputs(normalized)
        return normalized

    def save_trade_preview_inputs(self, inputs: dict[str, Any]) -> None:
        """Persist only preview planning inputs without changing trade settings."""
        normalized = _merge_trade_preview_inputs(inputs)
        self._save_freight_inputs_json(TRADE_PREVIEW_INPUTS_KEY, normalized)

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
        try:
            schema_version = int(
                self.settings.value("player_data/inputs_schema_version", 0) or 0
            )
        except (TypeError, ValueError):
            schema_version = 0
        parsed: dict[str, Any] = {}
        if raw:
            try:
                candidate = json.loads(str(raw))
                if isinstance(candidate, dict):
                    parsed = candidate
            except (TypeError, ValueError):
                pass
        if schema_version < PLAYER_DATA_INPUTS_SCHEMA_VERSION:
            if parsed:
                if schema_version < 2:
                    migrated_stages = list(parsed.get("stages") or [])
                    if "characters" not in migrated_stages:
                        migrated_stages.append("characters")
                    parsed["stages"] = migrated_stages
                normalized = _merge_player_data_inputs(parsed)
            else:
                normalized = _merge_player_data_inputs({})
            self.settings.setValue(
                "player_data/inputs_json",
                json.dumps(normalized, ensure_ascii=False),
            )
            self.settings.setValue(
                "player_data/inputs_schema_version",
                PLAYER_DATA_INPUTS_SCHEMA_VERSION,
            )
            self.settings.sync()
            return normalized
        return _merge_player_data_inputs(parsed)

    def save_player_data_inputs(self, inputs: dict[str, Any]) -> None:
        normalized = _merge_player_data_inputs(inputs)
        self.settings.setValue(
            "player_data/inputs_json",
            json.dumps(normalized, ensure_ascii=False),
        )
        self.settings.setValue(
            "player_data/inputs_schema_version",
            PLAYER_DATA_INPUTS_SCHEMA_VERSION,
        )
        self.settings.sync()

    def load_affix_refresh_inputs(self) -> dict[str, Any]:
        defaults = {"target_affix_id": None, "comparison": "gte", "threshold": "1", "max_film_cost": 20,
                    "auto_replace": False}
        try:
            raw = json.loads(str(self.settings.value("affix_refresh/inputs_json", "")))
            if isinstance(raw, dict) and raw.get("schema_version") == 1:
                return normalize_affix_inputs(raw)
        except (ValueError, TypeError):
            pass
        return defaults

    def save_affix_refresh_inputs(self, inputs: Mapping[str, Any]) -> None:
        normalized = normalize_affix_inputs(inputs)
        normalized["schema_version"] = 1
        self.settings.setValue("affix_refresh/inputs_json", json.dumps(normalized, ensure_ascii=False))
        self.settings.sync()

    def load_eternal_scuffle_inputs(self) -> dict[str, int]:
        values: dict[str, int] = {}
        for key, maximum in (("coins_per_run", 5), ("run_count", 9999)):
            try:
                value = int(self.settings.value(f"eternal_scuffle/{key}", 1))
            except (ValueError, TypeError):
                value = 1
            values[key] = max(1, min(maximum, value))
        return values

    def save_eternal_scuffle_inputs(self, inputs: dict[str, Any]) -> None:
        for key, maximum in (("coins_per_run", 5), ("run_count", 9999)):
            value = inputs.get(key, 1)
            if type(value) is not int or not 1 <= value <= maximum:
                raise ValueError(f"{key} 必须是 1～{maximum} 的整数")
        for key in ("coins_per_run", "run_count"):
            self.settings.setValue(f"eternal_scuffle/{key}", inputs.get(key, 1))
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
    merged["city_tax_rates_bps"] = validate_city_tax_rates_bps(merged["city_tax_rates_bps"])
    merged["allow_stale_market"] = validate_allow_stale_market(merged["allow_stale_market"])
    if merged["trade_mode"] not in ("free", "fixed"):
        raise ValueError("货运模式必须为 free 或 fixed。")
    merged["target_profit_enabled"] = bool(values.get("target_profit_enabled", False))
    for key, minimum, maximum in (("fatigue_budget", 0, None), ("cargo_capacity", 1, None),
                                  ("book_waste_limit", 0, None),
                                  ("negotiation_max_attempts", 1, 6),
                                  ("fatigue_medicine_max_uses", 0, None),
                                  ("arrival_timeout_seconds", 1, None)):
        value = merged[key]
        if type(value) is not int or value < minimum or (maximum is not None and value > maximum):
            raise ValueError(f"{key} 必须为范围内的整数，不能使用布尔或小数。")
    raw_city_ids = merged.get("available_city_ids")
    normalized_city_ids = list(
        dict.fromkeys(
            str(city_id)
            for city_id in (raw_city_ids if isinstance(raw_city_ids, list) else [])
            if str(city_id) in ALL_PC_TRADE_CITY_IDS
        )
    )
    # Incomplete selections are drafts, not a request to restore defaults.
    # Execution validation belongs to the task boundary.
    merged["available_city_ids"] = normalized_city_ids
    raw_end_city_ids = merged.get("required_end_city_ids")
    normalized_end_city_ids = list(
        dict.fromkeys(
            str(city_id)
            for city_id in (raw_end_city_ids if isinstance(raw_end_city_ids, list) else [])
            if str(city_id) in ALL_PC_TRADE_CITY_IDS
        )
    )
    merged["required_end_city_ids"] = None if raw_end_city_ids is None else normalized_end_city_ids
    budget = merged["book_budget"]
    if budget is not None and (type(budget) is not int or budget < 0):
        raise ValueError("进货书必须为非负整数或无限。")
    merged["books_enabled"] = bool(values.get("books_enabled", budget is None or budget > 0))
    merged["books_unlimited"] = bool(values.get("books_unlimited", budget is None))
    finite = values.get("finite_book_budget", budget if budget is not None else 0)
    if type(finite) is not int or finite < 0:
        raise ValueError("有限进货书数量必须为非负整数。")
    merged["finite_book_budget"] = finite
    merged["book_budget"] = (None if merged["books_unlimited"] else finite) if merged["books_enabled"] else 0
    merged["auto_cape_island_investment"] = bool(merged["auto_cape_island_investment"])
    merged["auto_trade_goods_investment"] = bool(merged["auto_trade_goods_investment"])
    merged["trade_goods_investment_mode"] = validate_trade_goods_investment_level(merged["trade_goods_investment_mode"])
    merged["auto_rubbish_recycling"] = bool(merged["auto_rubbish_recycling"])
    merged["auto_sparkling_water"] = bool(merged["auto_sparkling_water"])
    merged["auto_bento"] = bool(merged["auto_bento"])
    merged["prison_product_goods"] = normalize_prison_product_goods(merged["prison_product_goods"])
    priority = merged["bento_priority"]
    if (
        not isinstance(priority, list)
        or any(key not in ("work_meals", "love_bentos") for key in priority)
        or len(priority) != len(set(priority))
    ):
        raise ValueError("便当类型顺序至少选择一种，只允许工作餐和爱心便当，且不能重复。")
    merged["bento_priority"] = list(priority)
    for key in ("sparkling_water_start_threshold", "sparkling_water_stop_threshold",
                "bento_start_threshold", "bento_stop_threshold"):
        value = merged[key]
        if value is not None and (type(value) is not int or not 0 <= value <= 2147483647):
            raise ValueError(f"{key} 必须为非负整数或未设置，不能使用布尔或小数。")
    merged["auto_pickup"] = bool(merged["auto_pickup"])
    merged["auto_pickup_interval_sec"] = validate_pickup_click_interval(merged["auto_pickup_interval_sec"])
    merged.update(normalize_black_moon_settings(merged))
    merged.update(validate_trailer_options(merged))
    return merged


def _merge_trade_preview_inputs(values: dict[str, Any]) -> dict[str, Any]:
    planning_values = {
        key: values[key] for key in TRADE_PREVIEW_INPUT_KEYS if key in values
    }
    merged = _merge_trade_inputs(planning_values)
    start_city_id = str(merged.get("start_city_id") or "").strip()
    merged["start_city_id"] = (
        start_city_id if start_city_id in ALL_PC_TRADE_CITY_IDS else ""
    )
    return {key: merged[key] for key in TRADE_PREVIEW_INPUT_KEYS}


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


def normalize_prison_product_goods(value: Any) -> list[str]:
    if value is None:
        return []
    if not isinstance(value, list) or any(not isinstance(item, str) or not item.strip() for item in value):
        raise ValueError("监狱成品选择必须是商品名称或 ID 列表。")
    return list(dict.fromkeys(item.strip() for item in value))


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
    raw_profile_sections = values.get("profile_sections")
    requested_profile_sections = (
        raw_profile_sections if isinstance(raw_profile_sections, list) else DEFAULT_PROFILE_SECTIONS
    )
    if "bento_count" not in requested_profile_sections and any(
        section in requested_profile_sections for section in ("work_meals", "love_bentos")
    ):
        requested_profile_sections = [*requested_profile_sections, "bento_count"]
    result = {
        "stages": data_stages,
        "inventory_categories": inventory_categories,
        "profile_sections": [section for section in PLAYER_DATA_PROFILE_SECTION_ORDER if section in requested_profile_sections],
    }
    if "prison_products" in data_stages or "prison_product_goods" in values:
        result["prison_product_goods"] = normalize_prison_product_goods(values.get("prison_product_goods"))
    return result


def _merge_battle_inputs(values: dict[str, Any]) -> dict[str, Any]:
    return normalize_battle_editor_inputs(values)
