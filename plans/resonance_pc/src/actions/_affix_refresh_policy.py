"""Strict, UI-independent contracts for refresh-only affix search.

All quantities use displayed units (for example ``"0.5"`` for 0.5%).
The catalog is an offline extraction, never a live game connection. This module
does not identify text approximately, click, consume resources, or infer slots.
"""

from __future__ import annotations

from collections.abc import Mapping
from decimal import Decimal, InvalidOperation, localcontext
import json
from pathlib import Path
import re
from typing import Any


class PolicyError(RuntimeError):
    """A user-readable failure with a stable machine-readable reason."""

    def __init__(self, code: str, message: str):
        self.code = code
        self.message = message
        super().__init__(message)


def _fail(code: str, message: str) -> None:
    raise PolicyError(code, message)


def _object(value: Any, field: str, code: str = "invalid_config") -> Mapping:
    if not isinstance(value, Mapping):
        _fail(code, f"{field} 必须是完整对象")
    return value


def _integer(value: Any, field: str, minimum: int = 0,
             maximum: int | None = None, code: str = "invalid_config") -> int:
    if type(value) is not int or value < minimum or (maximum is not None and value > maximum):
        bounds = f"{minimum}..{maximum}" if maximum is not None else f">={minimum}"
        _fail(code, f"{field} 必须是 {bounds} 的整数")
    return value


def _text(value: Any, field: str, code: str = "invalid_config") -> str:
    if type(value) is not str or not value.strip() or value != value.strip():
        _fail(code, f"{field} 必须是非空且无首尾空白的文本")
    return value


def _decimal(value: Any, field: str, code: str = "invalid_config") -> Decimal:
    if (type(value) is not str or len(value) > 64
            or re.fullmatch(r"[+-]?(?:\d+(?:\.\d*)?|\.\d+)", value, re.ASCII) is None):
        _fail(code, f"{field} 必须是有限十进制数字字符串")
    try:
        number = Decimal(value)
    except (InvalidOperation, ValueError):
        _fail(code, f"{field} 不是有效数字")
    if not number.is_finite() or number.adjusted() > 16 or number.as_tuple().exponent < -16:
        _fail(code, f"{field} 必须是合理精度的有限数值")
    return number


def _canonical(number: Decimal) -> str:
    if number == 0:
        return "0"
    return format(number, "f").rstrip("0").rstrip(".") if number.as_tuple().exponent < 0 else format(number, "f")


def _on_step(value: Decimal, low: Decimal, step: Decimal) -> bool:
    with localcontext() as context:
        context.prec = 80
        return (value - low) % step == 0


def _ids(value: Any, field: str) -> list[int]:
    if type(value) is not list:
        _fail("invalid_catalog", f"{field} 必须是 ID 列表")
    ids = [_integer(item, field, code="invalid_catalog") for item in value]
    if len(ids) != len(set(ids)):
        _fail("invalid_catalog", f"{field} 包含重复 ID")
    return ids


def _catalog_indexes(catalog: Any) -> tuple[dict[int, Mapping], dict[int, Mapping]]:
    catalog = _object(catalog, "catalog", "invalid_catalog")
    if type(catalog.get("schema_version")) is not int or catalog["schema_version"] != 1:
        _fail("invalid_catalog", "不支持的词缀目录版本")
    indexes = []
    for kind in ("affixes", "equipment"):
        rows = catalog.get(kind)
        if type(rows) is not list or not rows:
            _fail("invalid_catalog", f"catalog.{kind} 缺少目录条目")
        index = {}
        for row in rows:
            row = _object(row, kind, "invalid_catalog")
            identity = _integer(row.get("id"), f"{kind}.id", code="invalid_catalog")
            if identity in index:
                _fail("invalid_catalog", f"{kind} 包含重复 ID：{identity}")
            index[identity] = row
            if kind == "affixes":
                _text(row.get("description"), "affix.description", "invalid_catalog")
                if type(row.get("unit")) is not str:
                    _fail("invalid_catalog", "affix.unit 必须是文本")
                low = _decimal(row.get("min_value"), "affix.min_value", "invalid_catalog")
                high = _decimal(row.get("max_value"), "affix.max_value", "invalid_catalog")
                step = _decimal(row.get("step"), "affix.step", "invalid_catalog")
                if low > high or step <= 0 or not _on_step(high, low, step):
                    _fail("invalid_catalog", f"词缀 {identity} 的数值范围或档位无效")
                _ids(row.get("camp_ids"), "affix.camp_ids")
                _ids(row.get("part_ids"), "affix.part_ids")
            else:
                _text(row.get("name"), "equipment.name", "invalid_catalog")
                _integer(row.get("camp_id"), "equipment.camp_id", code="invalid_catalog")
                _integer(row.get("part_id"), "equipment.part_id", code="invalid_catalog")
                _integer(row.get("base_random_slots"), "equipment.base_random_slots", 1, code="invalid_catalog")
                _ids(row.get("affix_ids"), "equipment.affix_ids")
                fixed = row.get("fixed_descriptions")
                if type(fixed) is not list or any(type(item) is not str for item in fixed):
                    _fail("invalid_catalog", "equipment.fixed_descriptions 必须是文本列表")
        indexes.append(index)
    affixes, equipment = indexes
    for row in equipment.values():
        if any(identity not in affixes for identity in row["affix_ids"]):
            _fail("invalid_catalog", f"装备 {row['id']} 的词缀池包含未知 ID")
    return affixes, equipment


def load_catalog(plan_root: Path | str | None = None) -> dict:
    """Load the bundled catalog and reject incomplete or malformed entries."""
    root = Path(plan_root) if plan_root is not None else Path(__file__).resolve().parents[2]
    try:
        with (root / "data/meta/affix_refresh.json").open(encoding="utf-8") as stream:
            catalog = json.load(stream)
    except (OSError, ValueError, TypeError) as exc:
        raise PolicyError("catalog_unavailable", f"无法读取词缀目录：{exc}") from exc
    _catalog_indexes(catalog)
    return catalog


def affix_signature(affix: Mapping) -> tuple[int, str]:
    """Exact effect ID and normalized value; screen text/coordinates are ignored."""
    affix = _object(affix, "affix", "invalid_snapshot")
    identity = _integer(affix.get("id"), "affix.id", code="invalid_snapshot")
    value = _decimal(affix.get("value"), "affix.value", "invalid_snapshot")
    return identity, _canonical(value)


def _observed_affix(row: Mapping, affixes: Mapping) -> None:
    identity, _ = affix_signature(row)
    if identity not in affixes:
        _fail("invalid_snapshot", f"识别到不在词缀目录中的词缀：{identity}")
    _text(row.get("text"), "candidate.text", "invalid_snapshot")
    definition = affixes[identity]
    value = _decimal(row["value"], "affix.value", "invalid_snapshot")
    low = _decimal(definition["min_value"], "min_value", "invalid_catalog")
    high = _decimal(definition["max_value"], "max_value", "invalid_catalog")
    step = _decimal(definition["step"], "step", "invalid_catalog")
    if not low <= value <= high or not _on_step(value, low, step):
        _fail("invalid_snapshot", "识别到的词缀数值不在可获得档位中")


def validate_config(config: Mapping, catalog: Mapping) -> dict:
    """Normalize search settings; legacy replacement settings are never used."""
    config = _object(config, "config")
    affixes, _ = _catalog_indexes(catalog)
    target_id = _integer(config.get("target_affix_id"), "目标词缀 ID")
    if target_id not in affixes:
        _fail("invalid_target", "目标词缀不在目录中")
    comparison = config.get("comparison")
    if type(comparison) is not str or comparison not in ("any", "gte", "eq", "max"):
        _fail("invalid_config", "数值要求必须是任意、至少、等于或满值")
    threshold = config.get("threshold")
    if type(threshold) is not str:
        _fail("invalid_config", "数值要求必须以数字字符串保存")
    limit = _integer(config.get("max_film_cost"), "胶卷消耗上限", 0, 9999)
    target = affixes[target_id]
    if comparison in ("gte", "eq"):
        number = _decimal(threshold, "目标数值")
        low = _decimal(target["min_value"], "min_value", "invalid_catalog")
        high = _decimal(target["max_value"], "max_value", "invalid_catalog")
        step = _decimal(target["step"], "step", "invalid_catalog")
        if not low <= number <= high:
            _fail("unreachable_threshold", "目标数值超出该词缀可获得范围")
        if comparison == "eq" and not _on_step(number, low, step):
            _fail("unreachable_threshold", "等于要求必须选择该词缀可获得的数值档位")
        threshold = _canonical(number)
    elif threshold:
        # Hidden/stale values still must be well formed, even when unused.
        _decimal(threshold, "目标数值")
    # An old saved auto_replace=True must not restore the disabled feature.
    # Discard snapshots and slot selections even for direct action callers.
    return {"target_affix_id": target_id, "comparison": comparison,
            "threshold": threshold, "max_film_cost": limit, "auto_replace": False}


def validate_frame(current: Mapping, catalog: Mapping) -> None:
    """Accept only a readable main window, balance and complete candidate."""
    current = _object(current, "current", "invalid_snapshot")
    if current.get("valid") is not True or current.get("scene") != "affix":
        _fail("invalid_snapshot", "请先打开游戏中的词缀替换窗口")
    if current.get("replace_mode") is not True:
        _fail("mode_unconfirmed", "尚未确认词缀刷新窗口")
    if "candidate" not in current or "film_remaining" not in current:
        _fail("invalid_snapshot", "尚未确认候选词缀或胶卷余额区域")
    if current["film_remaining"] is not None:
        _integer(current["film_remaining"], "胶卷余额", code="invalid_snapshot")
    if current["candidate"] is not None:
        affixes, _ = _catalog_indexes(catalog)
        _observed_affix(current["candidate"], affixes)


def matches(candidate: Mapping | None, config: Mapping, catalog: Mapping) -> bool:
    """Match an exact effect ID and value; never approximate OCR text."""
    config = validate_config(config, catalog)
    if candidate is None:
        return False
    affixes, _ = _catalog_indexes(catalog)
    _observed_affix(candidate, affixes)
    identity, _ = affix_signature(candidate)
    if identity != config["target_affix_id"]:
        return False
    value = _decimal(candidate["value"], "candidate.value", "invalid_snapshot")
    comparison = config["comparison"]
    if comparison == "any":
        return True
    if comparison == "max":
        return value == _decimal(affixes[identity]["max_value"], "max_value", "invalid_catalog")
    threshold = _decimal(config["threshold"], "目标数值")
    return value >= threshold if comparison == "gte" else value == threshold


def preflight(config: Mapping, current: Mapping, catalog: Mapping) -> str:
    """Check a fresh observation before any action that can spend film."""
    validate_config(config, catalog)
    validate_frame(current, catalog)
    return "ready"


def format_affix(affix: Mapping, catalog: Mapping) -> str:
    """Render a catalog template using an exact, valid displayed quantity."""
    affixes, _ = _catalog_indexes(catalog)
    identity, value = affix_signature(affix)
    if identity not in affixes:
        _fail("invalid_snapshot", f"未知词缀 ID：{identity}")
    definition = affixes[identity]
    description = definition["description"]
    return description.replace("%s", value).replace("%%", "%")
