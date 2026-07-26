"""Presentation model for the Resonance PC battle catalog."""

from __future__ import annotations

from dataclasses import dataclass
import json
import os
from pathlib import Path
import sys
from typing import Any


MAIN_CATEGORY_LABELS = {
    "ct": "协同终端",
    "gp": "全域整备",
}

SUBCATEGORY_LABELS = {
    "tie_an": "铁安局",
    "regional_ops_center": "区域作战中心",
    "action_summary": "行动汇总",
    "structural_exploration": "构析勘探",
}

MISSION_LABELS = {
    "expel": "驱逐",
    "bounty": "悬赏",
    "regional_ops": "区域作战",
}

ACTION_SUMMARY_GROUP_LABELS = {
    "blade_encirclement": "利刃围剿",
    "global_supply": "全境特供",
    "smuggler_crackdown": "私贩追缴",
}

ACTION_SUMMARY_STAGE_LABELS = {
    "special_order": "特殊订单",
    "blade_action": "利刃行动",
    "read_by_lamp": "挑灯看剑",
    "weapon_material_analysis": "武器材质分析",
    "knight_novel": "骑士小说",
    "i_think_i_am": "我思我在",
    "what_i_know": "所知所闻",
    "big_one": "大的！",
    "total_encirclement": "总体围剿",
    "elegant": "雅致",
    "standard": "制式",
    "savior": "救世",
    "cutting_edge": "尖端",
    "chaos": "混沌",
    "magic": "魔力",
    "blind_box": "盲盒",
}

STRUCTURAL_STAGE_LABELS = {
    "disordered_roots": "乱序根须",
    "hetero_branches": "异构厄枝",
    "echo_buoy": "混响浮标",
    "birch_buoy": "桦树浮标",
}

DIFFICULTY_LABELS = {
    1: "I－简单",
    2: "II－困难",
    3: "III－艰巨",
    4: "IV－激战",
    5: "V－苦战",
    6: "VI－鏖战",
}


@dataclass(frozen=True)
class BattleRoute:
    route_id: str
    main_category: str
    subcategory: str
    title: str
    detail: str
    mission_type: str = ""

    @property
    def category_label(self) -> str:
        return MAIN_CATEGORY_LABELS.get(self.main_category, self.main_category)

    @property
    def subcategory_label(self) -> str:
        return SUBCATEGORY_LABELS.get(self.subcategory, self.subcategory)

    @property
    def uses_difficulty(self) -> bool:
        return self.mission_type in {"expel", "regional_ops", "action_summary"}

    @property
    def uses_stage(self) -> bool:
        return self.mission_type == "expel"

    @property
    def uses_threat_level(self) -> bool:
        return self.mission_type == "regional_ops"

    @property
    def uses_combat_options(self) -> bool:
        return self.mission_type in {"expel", "bounty", "regional_ops", "action_summary"}


def _catalog_candidates() -> list[Path]:
    relative = Path("plans") / "resonance_pc" / "data" / "meta" / "battle_catalog.json"
    candidates: list[Path] = []
    base_path = str(os.environ.get("AURA_BASE_PATH") or "").strip()
    if base_path:
        candidates.append(Path(base_path) / relative)
    candidates.append(Path.cwd() / relative)
    candidates.append(Path(__file__).resolve().parents[2] / relative)
    executable = Path(sys.executable).resolve()
    candidates.append(executable.parent.parent / relative)
    return list(dict.fromkeys(path.resolve() for path in candidates))


def battle_catalog_path() -> Path:
    for path in _catalog_candidates():
        if path.is_file():
            return path
    searched = "\n".join(str(path) for path in _catalog_candidates())
    raise FileNotFoundError(f"找不到战斗目录 battle_catalog.json，已搜索：\n{searched}")


def load_battle_routes() -> tuple[BattleRoute, ...]:
    payload = json.loads(battle_catalog_path().read_text(encoding="utf-8"))
    raw_routes = payload.get("routes")
    if not isinstance(raw_routes, list):
        raise ValueError("battle_catalog.routes 必须是列表")
    routes: list[BattleRoute] = []
    for raw in raw_routes:
        if not isinstance(raw, dict):
            continue
        route = _route_from_raw(raw)
        if route is not None:
            routes.append(route)
    return tuple(routes)


def _route_from_raw(raw: dict[str, Any]) -> BattleRoute | None:
    route_id = str(raw.get("route_id") or "").strip()
    main_category = str(raw.get("main_category") or "").strip()
    if not route_id or main_category not in MAIN_CATEGORY_LABELS:
        return None
    if main_category == "ct":
        subcategory = str(raw.get("ct_subcategory") or "").strip()
        mission_type = str(raw.get("mission_type") or "").strip()
        city_name = str(raw.get("city_name") or route_id)
        mission_name = MISSION_LABELS.get(mission_type, mission_type)
        return BattleRoute(
            route_id=route_id,
            main_category=main_category,
            subcategory=subcategory,
            title=f"{city_name} · {mission_name}",
            detail=city_name,
            mission_type=mission_type,
        )

    parts = route_id.split(".")
    subcategory = parts[1]
    if subcategory == "action_summary" and len(parts) == 4:
        group_name = ACTION_SUMMARY_GROUP_LABELS.get(parts[2], parts[2])
        stage_name = ACTION_SUMMARY_STAGE_LABELS.get(parts[3], parts[3])
        return BattleRoute(
            route_id=route_id,
            main_category=main_category,
            subcategory=subcategory,
            title=f"{group_name} · {stage_name}",
            detail=f"{group_name} / {stage_name}",
            mission_type="action_summary",
        )
    if subcategory == "structural_exploration" and len(parts) == 3:
        stage_name = STRUCTURAL_STAGE_LABELS.get(parts[2], parts[2])
        return BattleRoute(
            route_id=route_id,
            main_category=main_category,
            subcategory=subcategory,
            title=stage_name,
            detail=stage_name,
            mission_type="structural_exploration",
        )
    return None


def battle_job_summary(job: dict[str, Any], routes: tuple[BattleRoute, ...]) -> tuple[str, str, str]:
    route_id = str(job.get("route_id") or "")
    route = next((item for item in routes if item.route_id == route_id), None)
    if route is None:
        return route_id or "未知任务", "", "未知"
    params: list[str] = []
    if job.get("stage") is not None:
        params.append(f"第 {job['stage']} 关")
    if job.get("threat_level") is not None:
        params.append(f"威胁 {job['threat_level']}")
    if job.get("difficulty") is not None:
        difficulty = int(job["difficulty"])
        params.append(DIFFICULTY_LABELS.get(difficulty, f"难度 {difficulty}"))
    formation = (
        f"队伍 {job['formation_index']}"
        if job.get("formation_index") is not None
        else "保持当前"
    )
    if job.get("capture_count") is not None:
        params.append(f"抓捕 {job['capture_count']} 次")
    return f"{route.subcategory_label} / {route.title}", " · ".join(params), formation
