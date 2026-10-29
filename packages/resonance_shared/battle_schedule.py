"""Pure preparation of a day's battle inputs, without account or game access."""

from __future__ import annotations

from collections.abc import Mapping
from copy import deepcopy
from datetime import datetime, timedelta, timezone
from typing import Any


WEEKDAYS = ("mon", "tue", "wed", "thu", "fri", "sat", "sun")
WEEKDAY_LABELS = ("周一", "周二", "周三", "周四", "周五", "周六", "周日")
STRUCTURAL_TYPES = ("disordered_roots", "hetero_branches", "echo_buoy", "birch_buoy")
STRUCTURAL_PREFIX = "gp.structural_exploration."
GAME_TIMEZONE = timezone(timedelta(hours=8), "Asia/Shanghai")
GAME_REFRESH_HOUR = 5
MAX_JOBS = 50
MAX_RUN_COUNT = 9999


def battle_game_day(now: datetime | None = None) -> tuple[str, str]:
    """Return the date and weekday of the CN server's 05:00 game day.

    A supplied naive datetime is interpreted in server time. Production callers
    omit it and resolve once when the actual backend task starts.
    """
    current = datetime.now(GAME_TIMEZONE) if now is None else now
    if current.tzinfo is None:
        current = current.replace(tzinfo=GAME_TIMEZONE)
    day = (current.astimezone(GAME_TIMEZONE) - timedelta(hours=GAME_REFRESH_HOUR)).date()
    return day.isoformat(), WEEKDAYS[day.weekday()]


def normalize_structural_types(values: Any) -> list[str]:
    if values is None:
        return []
    if not isinstance(values, list):
        raise ValueError("构析勘探类型必须是有序列表。")
    result = []
    for value in values:
        if not isinstance(value, str) or value not in STRUCTURAL_TYPES:
            raise ValueError(f"未知构析勘探类型：{value!r}")
        if value not in result:
            result.append(value)
    return result


def _catalog_index(catalog: Mapping[str, Any] | None) -> dict[str, dict]:
    if catalog is None:
        return {}
    if not isinstance(catalog, Mapping):
        raise ValueError("作战目录必须是对象。")
    if isinstance(catalog.get("route_index"), Mapping):
        return dict(catalog["route_index"])
    rows = catalog.get("routes")
    if not isinstance(rows, list):
        raise ValueError("作战目录缺少 routes。")
    return {str(row["route_id"]): dict(row) for row in rows
            if isinstance(row, Mapping) and isinstance(row.get("route_id"), str)}


def normalize_battle_day(
    values: Mapping[str, Any], *, weekday: str | None = None,
    catalog: Mapping[str, Any] | None = None,
) -> dict[str, Any]:
    """Keep counts compact; adapt legacy structural jobs into ordered types."""
    if not isinstance(values, Mapping):
        raise ValueError("当天作战计划必须是对象。")
    unknown = set(values) - {"jobs", "structural_types", "stop_on_failure"}
    if unknown:
        raise ValueError(f"未知当天作战字段：{sorted(unknown)}")
    raw_jobs = values.get("jobs", [])
    if not isinstance(raw_jobs, list) or len(raw_jobs) > MAX_JOBS + len(STRUCTURAL_TYPES):
        raise ValueError("每天最多配置 50 项普通关卡和 4 类构析勘探。")
    types = normalize_structural_types(values.get("structural_types"))
    index = _catalog_index(catalog)
    number = WEEKDAYS.index(weekday) + 1 if weekday is not None else None
    jobs = []
    for position, value in enumerate(raw_jobs):
        if not isinstance(value, Mapping):
            raise ValueError(f"第 {position + 1} 项作战任务必须是对象。")
        job = deepcopy(dict(value))
        route = job.get("route_id")
        if not isinstance(route, str) or not route.strip():
            raise ValueError(f"第 {position + 1} 项作战任务缺少关卡。")
        route = route.strip()
        if route.startswith(STRUCTURAL_PREFIX):
            structural = normalize_structural_types([route[len(STRUCTURAL_PREFIX):]])
            if structural[0] not in types:
                types.append(structural[0])
            continue
        count = job.get("run_count", 1)
        if type(count) is not int or not 1 <= count <= MAX_RUN_COUNT:
            raise ValueError(f"第 {position + 1} 项执行次数必须是 1～{MAX_RUN_COUNT} 的整数。")
        if index:
            if route not in index:
                raise ValueError(f"未知作战关卡：{route}")
            open_days = index[route].get("open_weekdays", [])
            if open_days and number is not None and number not in open_days:
                raise ValueError(f"{WEEKDAY_LABELS[number - 1]}未开放关卡 {route}。")
        job.update(route_id=route, run_count=count)
        jobs.append(job)
    if len(jobs) > MAX_JOBS:
        raise ValueError("每天最多配置 50 项普通作战关卡。")
    return {"jobs": jobs, "structural_types": types}


def resolve_battle_plan(
    jobs: list | None = None, structural_types: list | None = None,
    weekly_plan: Mapping[str, Any] | None = None, now: datetime | None = None,
    catalog: Mapping[str, Any] | None = None,
) -> dict[str, Any]:
    """Resolve and freeze a weekly plan, or adapt a direct/legacy daily list.

    stop_on_failure is intentionally absent from the executable model. A defeat
    or insufficient stamina always ends the battle step, after verified cleanup.
    """
    date, weekday = battle_game_day(now)
    if weekly_plan is not None:
        if jobs is not None or structural_types is not None:
            raise ValueError("周计划不能与当天 jobs/structural_types 同时提交。")
        if not isinstance(weekly_plan, Mapping):
            raise ValueError("每周作战计划必须是对象。")
        if "days" in weekly_plan:
            if set(weekly_plan) - {"days", "schema_version"}:
                raise ValueError("每周作战计划包含未知字段。")
            version = weekly_plan.get("schema_version", 1)
            if type(version) is not int or version != 1:
                raise ValueError("不支持的每周作战计划版本。")
            days = weekly_plan["days"]
        else:
            days = weekly_plan
        if not isinstance(days, Mapping) or set(days) - set(WEEKDAYS):
            raise ValueError("周计划只接受 mon 到 sun 七个星期键。")
        # Validate the whole saved week before sending any input, including days
        # other than today; do not silently discard invalid saved preferences.
        normalized = {
            key: normalize_battle_day(days.get(key, {}), weekday=key, catalog=catalog)
            for key in WEEKDAYS
        }
        plan = normalized[weekday]
    else:
        plan = normalize_battle_day(
            {"jobs": [] if jobs is None else jobs, "structural_types": structural_types},
            weekday=weekday, catalog=catalog,
        )
    return {"effective_date": date, "weekday": weekday, **deepcopy(plan)}
