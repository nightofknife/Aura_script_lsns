"""Whole-week editor preferences, without resolving away future days."""
from __future__ import annotations

from collections.abc import Mapping
from copy import deepcopy
import json
from typing import Any

from packages.resonance_shared.battle_schedule import (
    WEEKDAYS, WEEKDAY_LABELS, battle_game_day, normalize_battle_day,
)
from .battle_catalog import STRUCTURAL_STAGE_LABELS, battle_catalog_path


def normalize_battle_editor_inputs(values: Mapping[str, Any]) -> dict[str, Any]:
    """Keep a whole week; migrate a formerly repeated daily list once in memory.

    Old ordinary entries repeat on their catalog opening days and old structural
    entries become the final ordered phase on every day. Existing weekly data is
    never filtered by weekday here: an invalid saved choice remains visible for
    correction and the backend remains responsible for execution validation.
    """
    if not isinstance(values, Mapping):
        raise ValueError("作战设置必须是对象。")
    is_week = "weekly_plan" in values or "days" in values or bool(set(values) & set(WEEKDAYS))
    if is_week:
        if "weekly_plan" in values and any(key in values for key in ("jobs", "structural_types")):
            raise ValueError("周计划不能与当天作战输入同时保存。")
        week = values.get("weekly_plan", values)
        if not isinstance(week, Mapping):
            raise ValueError("每周作战计划必须是对象。")
        if "days" in week:
            if set(week) - {"days", "schema_version"}:
                raise ValueError("每周作战计划包含未知字段。")
            version = week.get("schema_version", 1)
            if type(version) is not int or version != 1:
                raise ValueError("不支持的每周作战计划版本。")
            days = week["days"]
        else:
            days = week
        if not isinstance(days, Mapping) or set(days) - set(WEEKDAYS):
            raise ValueError("周计划只接受 mon 到 sun 七个星期键。")
        normalized = {day: normalize_battle_day(days.get(day, {})) for day in WEEKDAYS}
    else:
        legacy = normalize_battle_day(values)
        catalog = json.loads(battle_catalog_path().read_text(encoding="utf-8"))
        index = {row["route_id"]: row for row in catalog["routes"]}
        normalized = {}
        for number, day in enumerate(WEEKDAYS, 1):
            jobs = [deepcopy(job) for job in legacy["jobs"]
                    if not index.get(job["route_id"], {}).get("open_weekdays")
                    or number in index[job["route_id"]]["open_weekdays"]]
            normalized[day] = {"jobs": jobs, "structural_types": list(legacy["structural_types"])}
    return {"weekly_plan": {"schema_version": 1, "days": normalized}}


def battle_editor_day_summary(values: Mapping[str, Any], weekday: str | None = None) -> str:
    """A preview only; the execution service chooses its game day when it starts."""
    selected = weekday or battle_game_day()[1]
    day = normalize_battle_editor_inputs(values)["weekly_plan"]["days"][selected]
    label = WEEKDAY_LABELS[WEEKDAYS.index(selected)]
    if not day["jobs"] and not day["structural_types"]:
        return f"今日（{label}）未安排作战，将跳过并继续后续流程。"
    count = sum(job["run_count"] for job in day["jobs"])
    parts = [f"今日（{label}）普通关卡 {count} 次"]
    types = day["structural_types"]
    if types:
        parts.append("最后构析：" + " → ".join(STRUCTURAL_STAGE_LABELS[key] for key in types))
    return " · ".join(parts)
