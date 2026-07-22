"""Actions for auto battle dispatch/grouping and OCR-driven selectors."""

from __future__ import annotations

import json
import re
import time
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

from packages.aura_core.api import action_info, requires_services
from packages.aura_core.observability.logging.core_logger import logger

_PLAN_ROOT = Path(__file__).resolve().parents[2]
_BATTLE_CATALOG_FILE = _PLAN_ROOT / "data" / "meta" / "battle_catalog.json"

_ACTION_SUMMARY_GROUP_TEXT: Dict[str, str] = {
    "blade_encirclement": "\u5229\u5203\u56f4\u527f",
    "global_supply": "\u5168\u5883\u7279\u4f9b",
    "smuggler_crackdown": "\u79c1\u8d29\u8ffd\u7f34",
}

_ACTION_SUMMARY_STAGE_TEXT: Dict[str, str] = {
    "special_order": "\u7279\u6b8a\u8ba2\u5355",
    "blade_action": "\u5229\u5203\u884c\u52a8",
    "read_by_lamp": "\u6311\u706f\u770b\u5251",
    "weapon_material_analysis": "\u6b66\u5668\u6750\u8d28\u5206\u6790",
    "knight_novel": "\u9a91\u58eb\u5c0f\u8bf4",
    "i_think_i_am": "\u6211\u601d\u6211\u5728",
    "what_i_know": "\u6240\u77e5\u6240\u95fb",
    "big_one": "\u5927\u7684\uff01",
    "total_encirclement": "\u603b\u4f53\u56f4\u527f",
    "elegant": "\u7279\u4f9b\u00b7\u96c5\u81f4",
    "standard": "\u7279\u4f9b\u00b7\u5236\u5f0f",
    "savior": "\u7279\u4f9b\u00b7\u6551\u4e16",
    "cutting_edge": "\u7279\u4f9b\u00b7\u5c16\u7aef",
    "chaos": "\u7279\u4f9b\u00b7\u6df7\u6c8c",
    "magic": "\u7279\u4f9b\u00b7\u9b54\u529b",
    "blind_box": "\u7279\u4f9b\u00b7\u76f2\u76d2",
}

_ACTION_SUMMARY_STAGE_OCR_TEXT: Dict[str, str] = {
    "elegant": "雅致",
    "standard": "制式",
    "savior": "救世",
    "cutting_edge": "尖端",
    "chaos": "混沌",
    "magic": "魔力",
    "blind_box": "盲盒",
}

_ACTION_SUMMARY_STAGE_ORDER: Dict[str, List[str]] = {
    "blade_encirclement": [
        "special_order",
        "blade_action",
        "read_by_lamp",
        "weapon_material_analysis",
        "knight_novel",
        "i_think_i_am",
        "what_i_know",
        "big_one",
        "total_encirclement",
    ],
    "global_supply": [
        "elegant",
        "standard",
        "savior",
        "cutting_edge",
        "chaos",
        "magic",
    ],
    "smuggler_crackdown": [
        "blind_box",
    ],
}

_STRUCTURAL_STAGE_TEXT: Dict[str, str] = {
    "disordered_roots": "\u4e71\u5e8f\u6839\u987b",
    "hetero_branches": "\u5f02\u6784\u5384\u679d",
    "echo_buoy": "\u6df7\u54cd\u6d6e\u6807",
    "birch_buoy": "\u6866\u6811\u6d6e\u6807",
}

_STRUCTURAL_SAMPLE_POINTS: Dict[str, Tuple[int, int]] = {
    "disordered_roots": (300, 375),
    "hetero_branches": (300, 440),
    "echo_buoy": (300, 500),
    "birch_buoy": (300, 560),
}

_BATTLE_FORMATION_POINTS: Dict[int, Tuple[int, int]] = {
    1: (310, 40),
    2: (490, 40),
    3: (660, 40),
    4: (840, 40),
}

_STRUCTURAL_STAGE_REGION: Tuple[int, int, int, int] = (70, 360, 220, 270)
_STRUCTURAL_SELECTED_RGB_MIN: Tuple[int, int, int] = (60, 180, 160)
_STRUCTURAL_SELECTED_RGB_MAX: Tuple[int, int, int] = (130, 245, 225)


class ResonancePcBattleDispatchError(RuntimeError):
    """Structured error for battle dispatch and selectors."""

    def __init__(self, code: str, message: str, detail: Optional[Dict[str, Any]] = None):
        super().__init__(message)
        self.code = str(code)
        self.message = str(message)
        self.detail = detail or {}

    def __str__(self) -> str:
        return f"{self.code}: {self.message}"


def _raise_error(code: str, message: str, detail: Optional[Dict[str, Any]] = None) -> None:
    raise ResonancePcBattleDispatchError(code=code, message=message, detail=detail)


def _load_json(path: Path) -> Dict[str, Any]:
    if not path.is_file():
        _raise_error("file_not_found", f"Required file not found: {path}")
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except Exception as exc:  # noqa: BLE001
        _raise_error("json_invalid", f"Invalid JSON file: {path}", {"cause": str(exc)})
    if not isinstance(payload, dict):
        _raise_error("json_invalid", f"JSON root must be object: {path}")
    return payload


def _load_catalog() -> Dict[str, Any]:
    payload = _load_json(_BATTLE_CATALOG_FILE)
    routes = payload.get("routes")
    if not isinstance(routes, list):
        _raise_error("catalog_invalid", "battle_catalog.routes must be a list")
    route_index: Dict[str, Dict[str, Any]] = {}
    for item in routes:
        if not isinstance(item, dict):
            continue
        route_id = str(item.get("route_id") or "").strip()
        if not route_id:
            continue
        route_index[route_id] = item
    payload["route_index"] = route_index
    return payload


def _normalize_text(text: str) -> str:
    return re.sub(
        r"[\s\u3000\|_\-:：,，。.!！?？·•()（）\[\]【】{}<>《》'\"“”‘’`~]+",
        "",
        str(text or ""),
    ).lower()


def _match_mode_hit(actual: str, target: str, match_mode: str) -> bool:
    if match_mode == "exact":
        return actual == target
    if match_mode == "contains":
        return target in actual
    return target in actual


def _coerce_region(value: Any, fallback: List[int]) -> Tuple[int, int, int, int]:
    base = fallback if isinstance(fallback, list) and len(fallback) == 4 else [0, 0, 1280, 720]
    if not isinstance(value, list) or len(value) != 4:
        value = base
    try:
        x = int(value[0])
        y = int(value[1])
        w = int(value[2])
        h = int(value[3])
    except (TypeError, ValueError):
        x, y, w, h = base
    return (x, y, max(w, 1), max(h, 1))


def _coerce_drag(value: Any, fallback: List[int]) -> Tuple[int, int, int, int]:
    base = fallback if isinstance(fallback, list) and len(fallback) == 4 else [640, 500, 640, 220]
    if not isinstance(value, list) or len(value) != 4:
        value = base
    try:
        sx = int(value[0])
        sy = int(value[1])
        ex = int(value[2])
        ey = int(value[3])
    except (TypeError, ValueError):
        sx, sy, ex, ey = base
    return (sx, sy, ex, ey)


def _sanitize_battle_job_fields(
    raw: Dict[str, Any],
    path: str,
    route_meta: Dict[str, Any],
) -> Tuple[Dict[str, Any], List[str]]:
    unknown_fields = set(raw.keys()) - {
        "route_id",
        "difficulty",
        "stage",
        "threat_level",
        "formation_index",
        "capture_count",
    }
    if unknown_fields:
        _raise_error(
            "invalid_job_field",
            f"{path} has unexpected fields: {sorted(unknown_fields)}",
            {"job": raw},
        )

    route_id = str(raw.get("route_id") or "").strip()
    if not route_id:
        _raise_error("missing_route_id", f"{path}.route_id is required")

    main_category = str(route_meta.get("main_category") or "")
    ct_subcategory = str(route_meta.get("ct_subcategory") or "")
    mission_type = route_meta.get("mission_type")
    allowed_fields = {"route_id"}

    if main_category == "ct" and ct_subcategory == "tie_an":
        if mission_type == "expel":
            allowed_fields.update({"stage", "difficulty", "formation_index", "capture_count"})
        elif mission_type == "bounty":
            allowed_fields.update({"formation_index", "capture_count"})
        else:
            _raise_error("invalid_catalog", f"route '{route_id}' has invalid mission_type in catalog")
    elif main_category == "ct" and ct_subcategory == "regional_ops_center":
        allowed_fields.update({"difficulty", "threat_level", "formation_index", "capture_count"})
    elif main_category == "gp":
        parts = route_id.split(".")
        if len(parts) < 3:
            _raise_error("invalid_catalog", f"route '{route_id}' has invalid gp route format")
        gp_subcategory = parts[1]
        if gp_subcategory == "action_summary":
            allowed_fields.update({"difficulty", "formation_index", "capture_count"})
        elif gp_subcategory == "structural_exploration":
            pass
        else:
            _raise_error("invalid_catalog", f"route '{route_id}' has invalid gp subcategory")

    removed_fields = sorted(key for key in raw.keys() if key not in allowed_fields)
    sanitized = {
        key: raw.get(key)
        for key in ("route_id", "difficulty", "stage", "threat_level", "formation_index", "capture_count")
        if key in allowed_fields
    }
    return sanitized, removed_fields


def _recognize_text_items(
    app: Any,
    ocr: Any,
    region: Tuple[int, int, int, int],
) -> List[Dict[str, Any]]:
    capture = app.capture(rect=region)
    if not capture.success:
        _raise_error("capture_failed", "Failed to capture screen region.", {"region": list(region)})

    result = ocr.recognize_all(capture.image)
    items: List[Dict[str, Any]] = []
    for row in result.results:
        if not row.center_point:
            continue
        cx = int(row.center_point[0]) + region[0]
        cy = int(row.center_point[1]) + region[1]
        abs_rect = None
        if isinstance(getattr(row, "rect", None), (list, tuple)) and len(row.rect) == 4:
            try:
                rx, ry, rw, rh = [int(v) for v in row.rect]
                abs_rect = (rx + region[0], ry + region[1], rw, rh)
            except (TypeError, ValueError):
                abs_rect = None
        txt = str(row.text or "")
        items.append(
            {
                "text": txt,
                "normalized": _normalize_text(txt),
                "center": (cx, cy),
                "rect": abs_rect,
                "confidence": float(row.confidence),
            }
        )
    logger.debug(
        "[BattleOCR] region=%s count=%s items=%s",
        list(region),
        len(items),
        [
            {
                "text": row["text"],
                "normalized": row["normalized"],
                "center": list(row["center"]),
                "rect": list(row["rect"]) if row["rect"] is not None else None,
                "confidence": round(float(row["confidence"]), 4),
            }
            for row in items
        ],
    )
    return items


def _city_from_ocr(items: List[Dict[str, Any]], city_order: List[str], match_mode: str) -> List[Dict[str, Any]]:
    normalized_order = [(_normalize_text(name), name, idx) for idx, name in enumerate(city_order)]
    hits: List[Dict[str, Any]] = []
    for row in items:
        normalized = row["normalized"]
        for city_norm, city_name, city_idx in normalized_order:
            if _match_mode_hit(normalized, city_norm, match_mode):
                hits.append(
                    {
                        "city_name": city_name,
                        "city_index": city_idx,
                        "center": row["center"],
                        "text": row["text"],
                        "confidence": row["confidence"],
                    }
                )
                break
    return hits


def _direction_from_city_hits(target_idx: int, hit_indexes: List[int]) -> str:
    if not hit_indexes:
        return "larger"
    min_idx = min(hit_indexes)
    max_idx = max(hit_indexes)
    if target_idx > max_idx:
        return "larger"
    if target_idx < min_idx:
        return "smaller"
    mid = (min_idx + max_idx) / 2.0
    return "larger" if target_idx >= mid else "smaller"


def _resolve_structural_stage_key(route_id: Optional[str] = None, gp_stage_name: Optional[str] = None) -> str:
    route_raw = str(route_id or "").strip()
    if route_raw.startswith("gp.structural_exploration."):
        parts = route_raw.split(".")
        if len(parts) == 3 and parts[2] in _STRUCTURAL_STAGE_TEXT:
            return parts[2]

    stage_name_norm = _normalize_text(str(gp_stage_name or ""))
    if stage_name_norm:
        for stage_key, display_name in _STRUCTURAL_STAGE_TEXT.items():
            if stage_name_norm == _normalize_text(display_name):
                return stage_key

    _raise_error(
        "invalid_structural_target",
        f"unable to resolve structural target from route_id='{route_id}' gp_stage_name='{gp_stage_name}'",
    )


def _read_structural_target_state(
    app: Any,
    stage_key: str,
    rgb_min: Tuple[int, int, int],
    rgb_max: Tuple[int, int, int],
) -> Dict[str, Any]:
    sample_point = _STRUCTURAL_SAMPLE_POINTS.get(stage_key)
    display_name = _STRUCTURAL_STAGE_TEXT.get(stage_key)
    if not sample_point or not display_name:
        _raise_error("invalid_structural_target", f"unknown structural target '{stage_key}'")

    color = app.get_pixel_color(int(sample_point[0]), int(sample_point[1]))
    if not isinstance(color, (list, tuple)) or len(color) < 3:
        _raise_error("pixel_read_failed", f"failed to read pixel color at {sample_point}")

    r = int(color[0])
    g = int(color[1])
    b = int(color[2])
    selected = rgb_min[0] <= r <= rgb_max[0] and rgb_min[1] <= g <= rgb_max[1] and rgb_min[2] <= b <= rgb_max[2]
    return {
        "stage_key": stage_key,
        "display_name": display_name,
        "sample_point": [int(sample_point[0]), int(sample_point[1])],
        "rgb": [r, g, b],
        "selected": selected,
    }


def _find_structural_target_hit(
    *,
    items: List[Dict[str, Any]],
    stage_key: str,
    match_mode: str,
) -> Optional[Dict[str, Any]]:
    target_text = _STRUCTURAL_STAGE_TEXT.get(stage_key)
    if not target_text:
        return None

    target_norm = _normalize_text(target_text)
    candidates = [row for row in items if _match_mode_hit(row["normalized"], target_norm, match_mode)]
    if not candidates:
        return None
    return max(candidates, key=lambda row: float(row.get("confidence") or 0.0))


def _find_best_text_hit(
    *,
    items: List[Dict[str, Any]],
    targets: List[str],
    match_mode: str = "contains",
) -> Optional[Dict[str, Any]]:
    candidates: List[Dict[str, Any]] = []
    normalized_targets = [(_normalize_text(target), target) for target in targets if str(target or "").strip()]
    for row in items:
        normalized = str(row.get("normalized") or "")
        for target_norm, target_raw in normalized_targets:
            if _match_mode_hit(normalized, target_norm, match_mode):
                enriched = dict(row)
                enriched["matched_target"] = target_raw
                candidates.append(enriched)
                break
    if not candidates:
        return None
    return max(candidates, key=lambda row: float(row.get("confidence") or 0.0))


def _extract_level(text: str) -> Optional[int]:
    m = re.search(r"(\d+)", str(text or ""))
    if not m:
        return None
    try:
        return int(m.group(1))
    except ValueError:
        return None


def _levels_from_ocr(items: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
    parsed: List[Dict[str, Any]] = []
    seen = set()
    for row in items:
        level = _extract_level(row["text"])
        if level is None:
            continue
        key = (level, row["center"])
        if key in seen:
            continue
        seen.add(key)
        parsed.append(
            {
                "level": level,
                "center": row["center"],
                "text": row["text"],
                "rect": row.get("rect"),
                "confidence": row["confidence"],
            }
        )
    return parsed


def _passes_horizontal_edge_margin(
    row: Dict[str, Any],
    *,
    region: Optional[Tuple[int, int, int, int]] = None,
    horizontal_edge_margin: int = 0,
) -> bool:
    region_right = (region[0] + region[2]) if region else None
    rect = row.get("rect")
    if (
        region_right is None
        or not isinstance(rect, (list, tuple))
        or len(rect) != 4
    ):
        return True
    try:
        rect_x = int(rect[0])
        rect_w = int(rect[2])
    except (TypeError, ValueError):
        return False
    rect_right = rect_x + rect_w
    return region_right - rect_right >= int(horizontal_edge_margin)


def _is_level_candidate_positionally_consistent(
    candidate: Dict[str, Any],
    target: int,
    levels: List[Dict[str, Any]],
) -> bool:
    candidate_x = int(candidate["center"][0])
    for row in levels:
        if row is candidate:
            continue
        other_x = int(row["center"][0])
        other_level = int(row["level"])
        if other_x < candidate_x and other_level > int(target):
            return False
        if other_x > candidate_x and other_level < int(target):
            return False
    return True


def _find_exact_level_hit(
    items: List[Dict[str, Any]],
    target: int,
    levels: List[Dict[str, Any]],
    *,
    region: Optional[Tuple[int, int, int, int]] = None,
    horizontal_edge_margin: int = 0,
) -> Optional[Dict[str, Any]]:
    target_text = str(int(target))
    candidates: List[Dict[str, Any]] = []
    for row in items:
        raw_text = str(row.get("text") or "").strip()
        if raw_text == target_text:
            if not _passes_horizontal_edge_margin(
                row,
                region=region,
                horizontal_edge_margin=horizontal_edge_margin,
            ):
                continue
            if not _is_level_candidate_positionally_consistent(row, int(target), levels):
                continue
            candidates.append(row)
    if not candidates:
        return None
    return max(candidates, key=lambda r: float(r.get("confidence") or 0.0))


def _ordered_hits(items: List[Dict[str, Any]], order: List[str], match_mode: str) -> List[Dict[str, Any]]:
    normalized_order = [(_normalize_text(name), name, idx) for idx, name in enumerate(order)]
    hits: List[Dict[str, Any]] = []
    for row in items:
        normalized = row["normalized"]
        for item_norm, item_name, item_idx in normalized_order:
            if _match_mode_hit(normalized, item_norm, match_mode):
                hits.append(
                    {
                        "label_name": item_name,
                        "label_index": item_idx,
                        "center": row["center"],
                        "text": row["text"],
                        "confidence": row["confidence"],
                    }
                )
                break
    return hits


@action_info(
    name="resonance_pc.group_battle_jobs",
    public=True,
    read_only=True,
    description="Group auto battle jobs by top-level category and preserve first-seen category order.",
)
def resonance_pc_group_battle_jobs(jobs: List[Dict[str, Any]]) -> Dict[str, Any]:
    if not isinstance(jobs, list):
        raise ValueError("jobs must be a list.")

    ct_jobs: List[Dict[str, Any]] = []
    gp_jobs: List[Dict[str, Any]] = []
    unknown_jobs: List[Dict[str, Any]] = []
    category_order: List[str] = []

    for raw in jobs:
        if not isinstance(raw, dict):
            unknown_jobs.append({"raw": raw, "reason": "job must be an object"})
            continue

        route_id = str(raw.get("route_id") or "").strip()
        if route_id.startswith("ct."):
            ct_jobs.append(raw)
            if "ct" not in category_order:
                category_order.append("ct")
            continue
        if route_id.startswith("gp."):
            gp_jobs.append(raw)
            if "gp" not in category_order:
                category_order.append("gp")
            continue
        unknown_jobs.append({"job": raw, "reason": "route_id must start with 'ct.' or 'gp.'"})

    return {
        "ct_jobs": ct_jobs,
        "gp_jobs": gp_jobs,
        "unknown_jobs": unknown_jobs,
        "category_order": category_order,
        "has_ct": len(ct_jobs) > 0,
        "has_gp": len(gp_jobs) > 0,
    }


@action_info(
    name="resonance_pc.group_ct_jobs",
    public=True,
    read_only=True,
    description="Group CT jobs into tie_an and regional_ops_center buckets with first-seen order.",
)
def resonance_pc_group_ct_jobs(jobs: List[Dict[str, Any]]) -> Dict[str, Any]:
    if not isinstance(jobs, list):
        raise ValueError("jobs must be a list.")

    tie_an_jobs: List[Dict[str, Any]] = []
    regional_ops_jobs: List[Dict[str, Any]] = []
    unknown_jobs: List[Dict[str, Any]] = []
    category_order: List[str] = []

    for raw in jobs:
        if not isinstance(raw, dict):
            unknown_jobs.append({"raw": raw, "reason": "job must be an object"})
            continue

        route_id = str(raw.get("route_id") or "").strip()
        if route_id.startswith("ct.tie_an."):
            tie_an_jobs.append(raw)
            if "tie_an" not in category_order:
                category_order.append("tie_an")
            continue
        if route_id.startswith("ct.regional_ops_center."):
            regional_ops_jobs.append(raw)
            if "regional_ops_center" not in category_order:
                category_order.append("regional_ops_center")
            continue
        unknown_jobs.append(
            {
                "job": raw,
                "reason": "CT route_id must start with 'ct.tie_an.' or 'ct.regional_ops_center.'",
            }
        )

    return {
        "tie_an_jobs": tie_an_jobs,
        "regional_ops_jobs": regional_ops_jobs,
        "unknown_jobs": unknown_jobs,
        "category_order": category_order,
        "has_tie_an": len(tie_an_jobs) > 0,
        "has_regional_ops_center": len(regional_ops_jobs) > 0,
    }


@action_info(
    name="resonance_pc.group_gp_jobs",
    public=True,
    read_only=True,
    description="Group GP jobs into action_summary and structural_exploration buckets with first-seen order.",
)
def resonance_pc_group_gp_jobs(jobs: List[Dict[str, Any]]) -> Dict[str, Any]:
    if not isinstance(jobs, list):
        raise ValueError("jobs must be a list.")

    action_summary_jobs: List[Dict[str, Any]] = []
    structural_jobs: List[Dict[str, Any]] = []
    unknown_jobs: List[Dict[str, Any]] = []
    category_order: List[str] = []

    for raw in jobs:
        if not isinstance(raw, dict):
            unknown_jobs.append({"raw": raw, "reason": "job must be an object"})
            continue

        route_id = str(raw.get("route_id") or "").strip()
        if route_id.startswith("gp.action_summary."):
            action_summary_jobs.append(raw)
            if "action_summary" not in category_order:
                category_order.append("action_summary")
            continue
        if route_id.startswith("gp.structural_exploration."):
            structural_jobs.append(raw)
            if "structural_exploration" not in category_order:
                category_order.append("structural_exploration")
            continue
        unknown_jobs.append(
            {
                "job": raw,
                "reason": "GP route_id must start with 'gp.action_summary.' or 'gp.structural_exploration.'",
            }
        )

    return {
        "action_summary_jobs": action_summary_jobs,
        "structural_exploration_jobs": structural_jobs,
        "unknown_jobs": unknown_jobs,
        "category_order": category_order,
        "has_action_summary": len(action_summary_jobs) > 0,
        "has_structural_exploration": len(structural_jobs) > 0,
    }


@action_info(
    name="resonance_pc.group_consecutive_jobs_by_route",
    public=True,
    read_only=True,
    description="Group adjacent jobs with the same route_id while preserving order.",
)
def resonance_pc_group_consecutive_jobs_by_route(jobs: List[Dict[str, Any]]) -> Dict[str, Any]:
    if not isinstance(jobs, list):
        raise ValueError("jobs must be a list.")

    groups: List[Dict[str, Any]] = []
    current_group: Optional[Dict[str, Any]] = None

    for raw in jobs:
        if not isinstance(raw, dict):
            continue
        route_id = str(raw.get("route_id") or "").strip()
        if not route_id:
            continue

        if current_group is None or current_group["route_id"] != route_id:
            current_group = {
                "route_id": route_id,
                "jobs": [raw],
                "job_count": 1,
            }
            for key in (
                "main_category",
                "ct_subcategory",
                "gp_subcategory",
                "gp_group_key",
                "gp_group_name",
                "gp_stage_key",
                "gp_stage_name",
                "structural_sample_point",
            ):
                if key in raw:
                    current_group[key] = raw.get(key)
            groups.append(current_group)
            continue

        current_group["jobs"].append(raw)
        current_group["job_count"] = int(current_group["job_count"]) + 1

    return {
        "group_count": len(groups),
        "groups": groups,
    }


@action_info(
    name="resonance_pc.annotate_job_sequence",
    public=True,
    read_only=True,
    description="Attach sequence metadata to each job item while preserving original fields.",
)
def resonance_pc_annotate_job_sequence(jobs: List[Dict[str, Any]]) -> Dict[str, Any]:
    if not isinstance(jobs, list):
        raise ValueError("jobs must be a list.")

    total = len(jobs)
    annotated: List[Dict[str, Any]] = []
    for idx, raw in enumerate(jobs, start=1):
        item = dict(raw) if isinstance(raw, dict) else {"raw": raw}
        item["seq"] = idx
        item["total"] = total
        item["is_first"] = idx == 1
        item["is_last"] = idx == total
        annotated.append(item)
    return {
        "job_count": total,
        "jobs": annotated,
    }


@action_info(
    name="resonance_pc.normalize_battle_jobs",
    public=True,
    read_only=True,
    description="Normalize battle jobs by dropping route-incompatible residual fields.",
)
def resonance_pc_normalize_battle_jobs(jobs: List[Dict[str, Any]]) -> Dict[str, Any]:
    if not isinstance(jobs, list):
        _raise_error("invalid_jobs", "jobs must be a list")

    catalog = _load_catalog()
    route_index = catalog.get("route_index") or {}
    normalized_jobs: List[Dict[str, Any]] = []
    removed_fields_summary: List[Dict[str, Any]] = []

    for idx, raw in enumerate(jobs):
        path = f"jobs[{idx}]"
        if not isinstance(raw, dict):
            _raise_error("invalid_job_item", f"{path} must be an object")

        route_id = str(raw.get("route_id") or "").strip()
        if not route_id:
            _raise_error("missing_route_id", f"{path}.route_id is required")
        route_meta = route_index.get(route_id)
        if not isinstance(route_meta, dict):
            _raise_error("unknown_route_id", f"{path}.route_id '{route_id}' is not in battle catalog")

        normalized, removed_fields = _sanitize_battle_job_fields(raw, path, route_meta)
        if removed_fields:
            logger.info(
                "[BattleJobNormalize] path=%s route_id=%s removed_fields=%s raw=%s normalized=%s",
                path,
                route_id,
                removed_fields,
                raw,
                normalized,
            )
            removed_fields_summary.append(
                {
                    "path": path,
                    "route_id": route_id,
                    "removed_fields": removed_fields,
                }
            )
        normalized_jobs.append(normalized)

    return {
        "ok": True,
        "job_count": len(normalized_jobs),
        "normalized_jobs": normalized_jobs,
        "removed_fields_summary": removed_fields_summary,
    }


@action_info(
    name="resonance_pc.validate_battle_jobs",
    public=True,
    read_only=True,
    description="Validate jobs against battle catalog and normalize required fields.",
)
def resonance_pc_validate_battle_jobs(jobs: List[Dict[str, Any]]) -> Dict[str, Any]:
    if not isinstance(jobs, list):
        _raise_error("invalid_jobs", "jobs must be a list")

    catalog = _load_catalog()
    route_index = catalog.get("route_index") or {}
    normalized_jobs: List[Dict[str, Any]] = []

    for idx, raw in enumerate(jobs):
        path = f"jobs[{idx}]"
        if not isinstance(raw, dict):
            _raise_error("invalid_job_item", f"{path} must be an object")

        route_id = str(raw.get("route_id") or "").strip()
        if not route_id:
            _raise_error("missing_route_id", f"{path}.route_id is required")
        route_meta = route_index.get(route_id)
        if not isinstance(route_meta, dict):
            _raise_error("unknown_route_id", f"{path}.route_id '{route_id}' is not in battle catalog")
        raw, _ = _sanitize_battle_job_fields(raw, path, route_meta)

        difficulty_raw = raw.get("difficulty")
        difficulty: Optional[int]
        if difficulty_raw is None:
            difficulty = None
        else:
            try:
                difficulty = int(difficulty_raw)
            except Exception as exc:  # noqa: BLE001
                _raise_error("invalid_difficulty", f"{path}.difficulty must be an integer", {"cause": str(exc)})
            if difficulty < 1 or difficulty > 6:
                _raise_error("invalid_difficulty", f"{path}.difficulty must be in [1,6]")

        stage_raw = raw.get("stage")
        stage: Optional[int]
        if stage_raw is None:
            stage = None
        else:
            try:
                stage = int(stage_raw)
            except Exception as exc:  # noqa: BLE001
                _raise_error("invalid_stage", f"{path}.stage must be an integer", {"cause": str(exc)})
            if stage < 1 or stage > 3:
                _raise_error("invalid_stage", f"{path}.stage must be in [1,3]")

        threat_raw = raw.get("threat_level")
        threat_level: Optional[int]
        if threat_raw is None:
            threat_level = None
        else:
            try:
                threat_level = int(threat_raw)
            except Exception as exc:  # noqa: BLE001
                _raise_error("invalid_threat_level", f"{path}.threat_level must be an integer", {"cause": str(exc)})
            if threat_level < 1:
                _raise_error("invalid_threat_level", f"{path}.threat_level must be >= 1")

        formation_raw = raw.get("formation_index")
        formation_index: Optional[int]
        if formation_raw is None:
            formation_index = None
        else:
            try:
                formation_index = int(formation_raw)
            except Exception as exc:  # noqa: BLE001
                _raise_error("invalid_formation_index", f"{path}.formation_index must be an integer", {"cause": str(exc)})
            if formation_index < 1 or formation_index > 4:
                _raise_error("invalid_formation_index", f"{path}.formation_index must be in [1,4]")

        capture_raw = raw.get("capture_count")
        capture_count: Optional[int]
        if capture_raw is None:
            capture_count = None
        else:
            try:
                capture_count = int(capture_raw)
            except Exception as exc:  # noqa: BLE001
                _raise_error("invalid_capture_count", f"{path}.capture_count must be an integer", {"cause": str(exc)})
            if capture_count < 1:
                _raise_error("invalid_capture_count", f"{path}.capture_count must be >= 1")

        main_category = str(route_meta.get("main_category") or "")
        ct_subcategory = str(route_meta.get("ct_subcategory") or "")
        mission_type = route_meta.get("mission_type")
        gp_subcategory: Optional[str] = None
        gp_group_key: Optional[str] = None
        gp_group_name: Optional[str] = None
        gp_stage_key: Optional[str] = None
        gp_stage_name: Optional[str] = None
        structural_sample_point: Optional[List[int]] = None

        if main_category == "ct" and ct_subcategory == "tie_an":
            if mission_type == "expel":
                if stage is None or difficulty is None:
                    _raise_error(
                        "invalid_tie_an_expel",
                        f"{path} requires both stage and difficulty for tie_an expel route",
                    )
                if threat_level is not None:
                    _raise_error("invalid_job_field", f"{path}.threat_level is not allowed for tie_an route")
            elif mission_type == "bounty":
                if stage is not None:
                    _raise_error("invalid_job_field", f"{path}.stage is not allowed for tie_an bounty route")
                if difficulty is not None:
                    _raise_error("invalid_job_field", f"{path}.difficulty is not allowed for tie_an bounty route")
                if threat_level is not None:
                    _raise_error("invalid_job_field", f"{path}.threat_level is not allowed for tie_an bounty route")
            else:
                _raise_error("invalid_catalog", f"route '{route_id}' has invalid mission_type in catalog")

        if main_category == "ct" and ct_subcategory == "regional_ops_center":
            if difficulty is None or threat_level is None:
                _raise_error(
                    "invalid_regional_ops",
                    f"{path} requires both difficulty and threat_level for regional_ops route",
                )
            if stage is not None:
                _raise_error("invalid_job_field", f"{path}.stage is not allowed for regional_ops route")

        if main_category == "gp":
            parts = route_id.split(".")
            if len(parts) < 3:
                _raise_error("invalid_catalog", f"route '{route_id}' has invalid gp route format")
            gp_subcategory = parts[1]

            if gp_subcategory == "action_summary":
                if len(parts) != 4:
                    _raise_error("invalid_catalog", f"route '{route_id}' has invalid action_summary route format")
                gp_group_key = parts[2]
                gp_stage_key = parts[3]
                gp_group_name = _ACTION_SUMMARY_GROUP_TEXT.get(gp_group_key)
                gp_stage_name = _ACTION_SUMMARY_STAGE_TEXT.get(gp_stage_key)
                if not gp_group_name or not gp_stage_name:
                    _raise_error("invalid_catalog", f"route '{route_id}' is not mapped in action_summary catalog")
                if difficulty is None:
                    _raise_error(
                        "invalid_gp_action_summary",
                        f"{path} requires difficulty for gp action_summary route",
                    )
                if stage is not None or threat_level is not None:
                    _raise_error(
                        "invalid_job_field",
                        f"{path}.stage/threat_level are not allowed for gp action_summary routes",
                    )
            elif gp_subcategory == "structural_exploration":
                if len(parts) != 3:
                    _raise_error(
                        "invalid_catalog",
                        f"route '{route_id}' has invalid structural_exploration route format",
                    )
                gp_stage_key = parts[2]
                gp_stage_name = _STRUCTURAL_STAGE_TEXT.get(gp_stage_key)
                sample_point = _STRUCTURAL_SAMPLE_POINTS.get(gp_stage_key)
                if not gp_stage_name or not sample_point:
                    _raise_error(
                        "invalid_catalog",
                        f"route '{route_id}' is not mapped in structural_exploration catalog",
                    )
                structural_sample_point = [int(sample_point[0]), int(sample_point[1])]
                if difficulty is not None or stage is not None or threat_level is not None:
                    _raise_error(
                        "invalid_job_field",
                        f"{path}.difficulty/stage/threat_level are not allowed for gp structural_exploration routes",
                    )
            else:
                _raise_error("invalid_catalog", f"route '{route_id}' has invalid gp subcategory")

        normalized = {
            "route_id": route_id,
            "difficulty": difficulty,
            "stage": stage,
            "threat_level": threat_level,
            "formation_index": formation_index,
            "capture_count": capture_count,
            "main_category": main_category,
            "ct_subcategory": ct_subcategory,
            "gp_subcategory": gp_subcategory,
            "gp_group_key": gp_group_key,
            "gp_group_name": gp_group_name,
            "gp_stage_key": gp_stage_key,
            "gp_stage_name": gp_stage_name,
            "structural_sample_point": structural_sample_point,
            "city_name": route_meta.get("city_name"),
            "mission_type": mission_type,
        }
        normalized_jobs.append(normalized)

    return {
        "ok": True,
        "job_count": len(normalized_jobs),
        "normalized_jobs": normalized_jobs,
    }


@action_info(
    name="resonance_pc.resolve_difficulty_text",
    public=True,
    read_only=True,
    description="Resolve difficulty text from numeric difficulty level.",
)
def resonance_pc_resolve_difficulty_text(difficulty: int) -> Dict[str, Any]:
    level = int(difficulty)
    catalog = _load_catalog()
    mapping = catalog.get("difficulty_text_map") or {}
    text = str(mapping.get(str(level)) or "").strip()
    if not text:
        _raise_error("invalid_difficulty", f"difficulty '{level}' has no mapped text")
    return {"difficulty": level, "difficulty_text": text}


@action_info(
    name="resonance_pc.select_ordered_city",
    public=True,
    read_only=False,
    description="Select target city from ordered city list by OCR + directional drag.",
)
@requires_services(
    app="plans/aura_base/app",
    ocr="plans/aura_base/ocr",
)
def resonance_pc_select_ordered_city(
    target_city_name: str,
    city_order: List[str],
    region: Optional[List[int]] = None,
    drag_up: Optional[List[int]] = None,
    drag_down: Optional[List[int]] = None,
    max_attempts: int = 15,
    drag_duration_sec: float = 0.5,
    drag_hold_before_release_sec: float = 0.5,
    after_drag_sec: float = 0.5,
    match_mode: str = "contains",
    app: Any = None,
    ocr: Any = None,
) -> Dict[str, Any]:
    if app is None or ocr is None:
        _raise_error("missing_service", "app/ocr service is required")

    target_name = str(target_city_name or "").strip()
    if not target_name:
        _raise_error("invalid_city", "target_city_name is required")
    if not isinstance(city_order, list) or not city_order:
        _raise_error("invalid_city_order", "city_order must be a non-empty list")

    region_tuple = _coerce_region(region, [0, 0, 1280, 720])
    drag_up_tuple = _coerce_drag(drag_up, [900, 560, 900, 260])
    drag_down_tuple = _coerce_drag(drag_down, [900, 260, 900, 560])
    attempts = max(int(max_attempts), 1)
    after_drag = float(after_drag_sec)
    drag_duration = float(drag_duration_sec)
    drag_hold = max(float(drag_hold_before_release_sec), 0.0)

    normalized_order = [_normalize_text(name) for name in city_order]
    target_norm = _normalize_text(target_name)
    if target_norm not in normalized_order:
        _raise_error(
            "unknown_target_city",
            f"target city '{target_name}' not in city_order",
            {"city_order": city_order},
        )
    target_idx = normalized_order.index(target_norm)

    last_direction = "larger"
    for attempt in range(1, attempts + 1):
        items = _recognize_text_items(app=app, ocr=ocr, region=region_tuple)
        city_hits = _city_from_ocr(items=items, city_order=city_order, match_mode=match_mode)

        matched_target = [row for row in city_hits if _normalize_text(row["city_name"]) == target_norm]
        if matched_target:
            chosen = max(matched_target, key=lambda r: float(r["confidence"]))
            x, y = chosen["center"]
            app.click(x=x, y=y)
            return {
                "found": True,
                "city_name": chosen["city_name"],
                "attempt": attempt,
                "click_x": x,
                "click_y": y,
            }

        hit_indexes = [int(row["city_index"]) for row in city_hits]
        direction = _direction_from_city_hits(target_idx=target_idx, hit_indexes=hit_indexes)
        last_direction = direction

        sx, sy, ex, ey = drag_up_tuple if direction == "larger" else drag_down_tuple
        app.drag(
            start_x=sx,
            start_y=sy,
            end_x=ex,
            end_y=ey,
            duration=drag_duration,
            hold_before_release_sec=drag_hold,
        )
        time.sleep(max(after_drag, 0.0))

    _raise_error(
        "city_select_failed",
        f"Failed to locate city '{target_name}' within {attempts} attempts",
        {"last_direction": last_direction, "region": list(region_tuple)},
    )


@action_info(
    name="resonance_pc.select_threat_level_numeric",
    public=True,
    read_only=False,
    description="Select threat level by OCR numeric scan and directional horizontal drag.",
)
@requires_services(
    app="plans/aura_base/app",
    ocr="plans/aura_base/ocr",
)
def resonance_pc_select_threat_level_numeric(
    threat_level: int,
    region: Optional[List[int]] = None,
    drag_increase: Optional[List[int]] = None,
    drag_decrease: Optional[List[int]] = None,
    max_attempts: int = 20,
    drag_duration_sec: float = 0.5,
    drag_hold_before_release_sec: float = 0.5,
    after_drag_sec: float = 0.5,
    app: Any = None,
    ocr: Any = None,
) -> Dict[str, Any]:
    if app is None or ocr is None:
        _raise_error("missing_service", "app/ocr service is required")

    try:
        target = int(threat_level)
    except Exception as exc:  # noqa: BLE001
        _raise_error("invalid_threat_level", "threat_level must be an integer", {"cause": str(exc)})
    if target < 1:
        _raise_error("invalid_threat_level", "threat_level must be >= 1")

    region_tuple = _coerce_region(region, [0, 0, 1280, 720])
    drag_inc_tuple = _coerce_drag(drag_increase, [980, 420, 420, 420])
    drag_dec_tuple = _coerce_drag(drag_decrease, [420, 420, 980, 420])
    attempts = max(int(max_attempts), 1)
    after_drag = float(after_drag_sec)
    drag_duration = float(drag_duration_sec)
    drag_hold = max(float(drag_hold_before_release_sec), 0.0)

    last_direction = "increase"
    for attempt in range(1, attempts + 1):
        items = _recognize_text_items(app=app, ocr=ocr, region=region_tuple)
        levels = _levels_from_ocr(items)
        target_text = str(target)
        exact_candidates_debug: List[Dict[str, Any]] = []
        for row in items:
            raw_text = str(row.get("text") or "").strip()
            if raw_text != target_text:
                continue
            exact_candidates_debug.append(
                {
                    "text": raw_text,
                    "center": row.get("center"),
                    "rect": row.get("rect"),
                    "confidence": round(float(row.get("confidence") or 0.0), 4),
                    "edge_ok": _passes_horizontal_edge_margin(
                        row,
                        region=region_tuple,
                        horizontal_edge_margin=30,
                    ),
                    "sequence_ok": _is_level_candidate_positionally_consistent(row, target, levels),
                }
            )
        logger.debug(
            "[ThreatLevel] attempt=%s target=%s items=%s levels=%s exact_candidates=%s",
            attempt,
            target,
            [
                {
                    "text": str(row.get("text") or ""),
                    "center": row.get("center"),
                    "rect": row.get("rect"),
                    "confidence": round(float(row.get("confidence") or 0.0), 4),
                }
                for row in items
            ],
            [
                {
                    "level": int(row["level"]),
                    "text": row.get("text"),
                    "center": row.get("center"),
                    "rect": row.get("rect"),
                    "confidence": round(float(row.get("confidence") or 0.0), 4),
                }
                for row in levels
            ],
            exact_candidates_debug,
        )
        exact_text_hit = _find_exact_level_hit(
            items,
            target,
            levels,
            region=region_tuple,
            horizontal_edge_margin=30,
        )
        if exact_text_hit is not None:
            x, y = exact_text_hit["center"]
            app.click(x=x, y=y)
            return {
                "found": True,
                "threat_level": target,
                "attempt": attempt,
                "click_x": x,
                "click_y": y,
            }

        if levels:
            min_level = min(int(row["level"]) for row in levels)
            max_level = max(int(row["level"]) for row in levels)
            if target > max_level:
                direction = "increase"
            elif target < min_level:
                direction = "decrease"
            else:
                avg = (min_level + max_level) / 2.0
                direction = "increase" if target >= avg else "decrease"
        else:
            direction = "decrease" if last_direction == "increase" else "increase"

        last_direction = direction
        logger.debug(
            "[ThreatLevel] attempt=%s target=%s direction=%s drag=%s",
            attempt,
            target,
            direction,
            list(drag_inc_tuple if direction == "increase" else drag_dec_tuple),
        )
        sx, sy, ex, ey = drag_inc_tuple if direction == "increase" else drag_dec_tuple
        app.drag(
            start_x=sx,
            start_y=sy,
            end_x=ex,
            end_y=ey,
            duration=drag_duration,
            hold_before_release_sec=drag_hold,
        )
        time.sleep(max(after_drag, 0.0))

    _raise_error(
        "threat_level_select_failed",
        f"Failed to locate threat level '{target}' within {attempts} attempts",
        {"last_direction": last_direction, "region": list(region_tuple)},
    )


@action_info(
    name="resonance_pc.select_action_summary_stage",
    public=True,
    read_only=False,
    description="Select one action_summary stage, OCR its anchored enter button, and confirm the transition.",
)
@requires_services(
    app="plans/aura_base/app",
    ocr="plans/aura_base/ocr",
)
def resonance_pc_select_action_summary_stage(
    route_id: str,
    region: Optional[List[int]] = None,
    drag_forward: Optional[List[int]] = None,
    drag_backward: Optional[List[int]] = None,
    max_attempts: int = 12,
    drag_duration_sec: float = 0.5,
    drag_hold_before_release_sec: float = 0.5,
    after_drag_sec: float = 0.5,
    enter_button_text: str = "进入挑战",
    button_region_left_offset: int = -128,
    button_region_top_offset: int = 143,
    button_region_width: int = 258,
    button_region_height: int = 95,
    button_min_confidence: float = 0.8,
    button_timeout_sec: float = 2.0,
    button_click_attempts: int = 3,
    after_button_click_sec: float = 0.5,
    transition_text: str = "开始作战",
    transition_region: Optional[List[int]] = None,
    transition_timeout_sec: float = 4.0,
    transition_min_confidence: float = 0.7,
    match_mode: str = "contains",
    app: Any = None,
    ocr: Any = None,
) -> Dict[str, Any]:
    if app is None or ocr is None:
        _raise_error("missing_service", "app/ocr service is required")

    parts = str(route_id or "").strip().split(".")
    if len(parts) != 4 or parts[0] != "gp" or parts[1] != "action_summary":
        _raise_error("invalid_route_id", f"route '{route_id}' is not a gp action_summary route")

    group_key = parts[2]
    stage_key = parts[3]
    stage_order = _ACTION_SUMMARY_STAGE_ORDER.get(group_key)
    stage_name = _ACTION_SUMMARY_STAGE_TEXT.get(stage_key)
    if not stage_order or not stage_name:
        _raise_error("invalid_route_id", f"route '{route_id}' is not mapped in action_summary stage selector")

    order_names = [
        _ACTION_SUMMARY_STAGE_OCR_TEXT.get(key, _ACTION_SUMMARY_STAGE_TEXT[key])
        for key in stage_order
    ]
    region_tuple = _coerce_region(region, [0, 0, 1280, 720])
    drag_forward_tuple = _coerce_drag(drag_forward, [700, 400, 1100, 400])
    drag_backward_tuple = _coerce_drag(drag_backward, [1100, 400, 700, 400])
    attempts = max(int(max_attempts), 1)
    after_drag = float(after_drag_sec)
    drag_duration = float(drag_duration_sec)
    drag_hold = max(float(drag_hold_before_release_sec), 0.0)
    button_target = _normalize_text(enter_button_text)
    if not button_target:
        _raise_error("invalid_enter_button_text", "enter_button_text must not be empty")
    button_width = max(int(button_region_width), 1)
    button_height = max(int(button_region_height), 1)
    button_confidence = min(max(float(button_min_confidence), 0.0), 1.0)
    button_timeout = max(float(button_timeout_sec), 0.0)
    click_attempts = max(int(button_click_attempts), 1)
    click_wait = max(float(after_button_click_sec), 0.0)
    transition_target = _normalize_text(transition_text)
    if not transition_target:
        _raise_error("invalid_transition_text", "transition_text must not be empty")
    transition_region_tuple = _coerce_region(transition_region, [790, 460, 430, 100])
    transition_timeout = max(float(transition_timeout_sec), 0.0)
    transition_confidence = min(max(float(transition_min_confidence), 0.0), 1.0)

    target_idx = stage_order.index(stage_key)
    target_ocr_text = _ACTION_SUMMARY_STAGE_OCR_TEXT.get(stage_key, stage_name)
    target_norm = _normalize_text(target_ocr_text)
    last_direction = "forward"
    last_items: List[Dict[str, Any]] = []
    last_hits: List[Dict[str, Any]] = []

    for attempt in range(1, attempts + 1):
        items = _recognize_text_items(app=app, ocr=ocr, region=region_tuple)
        hits = _ordered_hits(items=items, order=order_names, match_mode=match_mode)
        last_items = items
        last_hits = hits

        matched_target = [row for row in hits if _normalize_text(row["label_name"]) == target_norm]
        logger.info(
            "[BattleOCR][ActionSummaryStage] attempt=%s/%s route_id=%s target=%s "
            "ocr_target=%s target_normalized=%s recognized_texts=%s matched_labels=%s",
            attempt,
            attempts,
            route_id,
            stage_name,
            target_ocr_text,
            target_norm,
            [
                {
                    "text": row["text"],
                    "normalized": row["normalized"],
                    "center": list(row["center"]),
                    "confidence": round(float(row["confidence"]), 4),
                }
                for row in items
            ],
            [
                {
                    "label": row["label_name"],
                    "text": row["text"],
                    "center": list(row["center"]),
                    "confidence": round(float(row["confidence"]), 4),
                }
                for row in hits
            ],
        )
        if matched_target:
            chosen = max(matched_target, key=lambda r: float(r["confidence"]))
            title_x = int(chosen["center"][0])
            title_y = int(chosen["center"][1])
            button_x = max(title_x + int(button_region_left_offset), 0)
            button_y = max(title_y + int(button_region_top_offset), 0)
            button_region = (
                button_x,
                button_y,
                min(button_width, max(1280 - button_x, 1)),
                min(button_height, max(720 - button_y, 1)),
            )
            logger.info(
                "[BattleOCR][ActionSummaryStage] target_found route_id=%s attempt=%s "
                "recognized_text=%s center=%s button_region=%s confidence=%.4f",
                route_id,
                attempt,
                chosen["text"],
                list(chosen["center"]),
                list(button_region),
                float(chosen["confidence"]),
            )

            deadline = time.monotonic() + button_timeout
            button_items: List[Dict[str, Any]] = []
            candidate: Optional[Dict[str, Any]] = None
            button_scan = 0
            while True:
                button_scan += 1
                button_items = _recognize_text_items(app=app, ocr=ocr, region=button_region)
                candidates = [
                    row
                    for row in button_items
                    if _match_mode_hit(row["normalized"], button_target, "contains")
                    and float(row["confidence"]) >= button_confidence
                ]
                candidate = max(candidates, key=lambda r: float(r["confidence"])) if candidates else None
                logger.info(
                    "[BattleOCR][EnterChallenge] route_id=%s scan=%s title_center=%s region=%s "
                    "target=%s recognized_texts=%s candidate=%s",
                    route_id,
                    button_scan,
                    [title_x, title_y],
                    list(button_region),
                    enter_button_text,
                    [
                        {
                            "text": row["text"],
                            "center": list(row["center"]),
                            "confidence": round(float(row["confidence"]), 4),
                        }
                        for row in button_items
                    ],
                    None
                    if candidate is None
                    else {
                        "text": candidate["text"],
                        "center": list(candidate["center"]),
                        "confidence": round(float(candidate["confidence"]), 4),
                    },
                )
                if candidate is not None:
                    break
                if any("开放" in row["normalized"] for row in button_items):
                    _raise_error(
                        "action_summary_stage_unavailable",
                        f"Action-summary stage '{stage_name}' is not currently available",
                        {"route_id": route_id, "button_region": list(button_region), "items": button_items},
                    )
                if time.monotonic() >= deadline:
                    _raise_error(
                        "action_summary_enter_button_not_found",
                        f"Failed to locate '{enter_button_text}' below action-summary stage '{stage_name}'",
                        {"route_id": route_id, "button_region": list(button_region), "items": button_items},
                    )
                time.sleep(min(0.2, max(deadline - time.monotonic(), 0.0)))

            for click_attempt in range(1, click_attempts + 1):
                click_x = int(candidate["center"][0])
                click_y = int(candidate["center"][1])
                delta_x = click_x - title_x
                delta_y = click_y - title_y
                if abs(delta_x) > 128 or not 120 <= delta_y <= 240:
                    _raise_error(
                        "action_summary_enter_button_geometry_invalid",
                        "Recognized enter button is not geometrically associated with the target stage",
                        {
                            "route_id": route_id,
                            "title_center": [title_x, title_y],
                            "button_center": [click_x, click_y],
                            "delta": [delta_x, delta_y],
                        },
                    )
                logger.info(
                    "[BattleClick][EnterChallenge] route_id=%s attempt=%s/%s click=%s confidence=%.4f",
                    route_id,
                    click_attempt,
                    click_attempts,
                    [click_x, click_y],
                    float(candidate["confidence"]),
                )
                app.click(x=click_x, y=click_y)
                time.sleep(click_wait)

                transition_deadline = time.monotonic() + transition_timeout
                transition_items: List[Dict[str, Any]] = []
                transition_scan = 0
                while True:
                    transition_scan += 1
                    transition_items = _recognize_text_items(
                        app=app,
                        ocr=ocr,
                        region=transition_region_tuple,
                    )
                    transition_hits = [
                        row
                        for row in transition_items
                        if _match_mode_hit(row["normalized"], transition_target, "contains")
                        and float(row["confidence"]) >= transition_confidence
                    ]
                    transition_hit = (
                        max(transition_hits, key=lambda r: float(r["confidence"]))
                        if transition_hits
                        else None
                    )
                    logger.info(
                        "[BattleTransition][EnterChallenge] route_id=%s click_attempt=%s scan=%s "
                        "region=%s target=%s recognized_texts=%s candidate=%s",
                        route_id,
                        click_attempt,
                        transition_scan,
                        list(transition_region_tuple),
                        transition_text,
                        [
                            {
                                "text": row["text"],
                                "center": list(row["center"]),
                                "confidence": round(float(row["confidence"]), 4),
                            }
                            for row in transition_items
                        ],
                        None
                        if transition_hit is None
                        else {
                            "text": transition_hit["text"],
                            "center": list(transition_hit["center"]),
                            "confidence": round(float(transition_hit["confidence"]), 4),
                        },
                    )
                    if transition_hit is not None:
                        logger.info(
                            "[BattleTransition][EnterChallenge] route_id=%s click_attempt=%s "
                            "confirmed=true transition_text=%s center=%s confidence=%.4f",
                            route_id,
                            click_attempt,
                            transition_hit["text"],
                            list(transition_hit["center"]),
                            float(transition_hit["confidence"]),
                        )
                        return {
                            "found": True,
                            "stage_name": stage_name,
                            "attempt": attempt,
                            "title_center": [title_x, title_y],
                            "button_region": list(button_region),
                            "button_text": candidate["text"],
                            "button_confidence": float(candidate["confidence"]),
                            "click_x": click_x,
                            "click_y": click_y,
                            "click_attempt": click_attempt,
                            "transition_confirmed": True,
                            "transition_text": transition_hit["text"],
                            "transition_center": list(transition_hit["center"]),
                        }
                    if time.monotonic() >= transition_deadline:
                        break
                    time.sleep(min(0.2, max(transition_deadline - time.monotonic(), 0.0)))

                refreshed_button_items = _recognize_text_items(app=app, ocr=ocr, region=button_region)
                refreshed_candidates = [
                    row
                    for row in refreshed_button_items
                    if _match_mode_hit(row["normalized"], button_target, "contains")
                    and float(row["confidence"]) >= button_confidence
                ]
                if not refreshed_candidates:
                    _raise_error(
                        "action_summary_enter_transition_failed",
                        f"Did not confirm '{transition_text}' and the original enter button is no longer visible",
                        {
                            "route_id": route_id,
                            "transition_region": list(transition_region_tuple),
                            "transition_items": transition_items,
                            "button_region": list(button_region),
                            "button_items": refreshed_button_items,
                        },
                    )
                candidate = max(refreshed_candidates, key=lambda r: float(r["confidence"]))
                logger.info(
                    "[BattleTransition][EnterChallenge] route_id=%s click_attempt=%s confirmed=false "
                    "button_present=true retry_center=%s confidence=%.4f",
                    route_id,
                    click_attempt,
                    list(candidate["center"]),
                    float(candidate["confidence"]),
                )

            _raise_error(
                "action_summary_enter_transition_failed",
                f"Failed to confirm '{transition_text}' after {click_attempts} click attempts",
                {
                    "route_id": route_id,
                    "button_region": list(button_region),
                    "transition_region": list(transition_region_tuple),
                    "last_items": transition_items,
                },
            )

        hit_indexes = [int(row["label_index"]) for row in hits]
        direction = _direction_from_city_hits(target_idx=target_idx, hit_indexes=hit_indexes)
        last_direction = "forward" if direction == "larger" else "backward"

        sx, sy, ex, ey = drag_forward_tuple if direction == "larger" else drag_backward_tuple
        logger.info(
            "[BattleDrag][ActionSummaryStage] attempt=%s/%s route_id=%s direction=%s "
            "start=%s end=%s duration_sec=%.3f hold_before_release_sec=%.3f post_drag_wait_sec=%.3f",
            attempt,
            attempts,
            route_id,
            last_direction,
            [sx, sy],
            [ex, ey],
            drag_duration,
            drag_hold,
            max(after_drag, 0.0),
        )
        app.drag(
            start_x=sx,
            start_y=sy,
            end_x=ex,
            end_y=ey,
            duration=drag_duration,
            hold_before_release_sec=drag_hold,
        )
        time.sleep(max(after_drag, 0.0))

    _raise_error(
        "action_summary_stage_select_failed",
        f"Failed to locate action_summary stage for route '{route_id}' within {attempts} attempts",
        {
            "last_direction": last_direction,
            "region": list(region_tuple),
            "target": stage_name,
            "ocr_target": target_ocr_text,
            "target_normalized": target_norm,
            "last_recognized_items": [
                {
                    "text": row["text"],
                    "normalized": row["normalized"],
                    "center": list(row["center"]),
                    "confidence": round(float(row["confidence"]), 4),
                }
                for row in last_items
            ],
            "last_matched_labels": [
                {
                    "label": row["label_name"],
                    "text": row["text"],
                    "center": list(row["center"]),
                    "confidence": round(float(row["confidence"]), 4),
                }
                for row in last_hits
            ],
        },
    )


@action_info(
    name="resonance_pc.check_pixel_color_range",
    public=True,
    read_only=True,
    description="Check whether one pixel color is inside an inclusive RGB range.",
)
@requires_services(
    app="plans/aura_base/app",
)
def resonance_pc_check_pixel_color_range(
    x: int,
    y: int,
    rgb_min: List[int],
    rgb_max: List[int],
    app: Any = None,
) -> Dict[str, Any]:
    if app is None:
        _raise_error("missing_service", "app service is required")
    if not isinstance(rgb_min, list) or not isinstance(rgb_max, list) or len(rgb_min) != 3 or len(rgb_max) != 3:
        _raise_error("invalid_rgb_range", "rgb_min/rgb_max must both be [r, g, b]")

    color = app.get_pixel_color(int(x), int(y))
    if not isinstance(color, (list, tuple)) or len(color) < 3:
        _raise_error("pixel_read_failed", f"failed to read pixel color at ({x}, {y})")

    r = int(color[0])
    g = int(color[1])
    b = int(color[2])
    selected = (
        int(rgb_min[0]) <= r <= int(rgb_max[0])
        and int(rgb_min[1]) <= g <= int(rgb_max[1])
        and int(rgb_min[2]) <= b <= int(rgb_max[2])
    )
    return {
        "x": int(x),
        "y": int(y),
        "rgb": [r, g, b],
        "selected": selected,
    }


@action_info(
    name="resonance_pc.reconcile_structural_selection",
    public=True,
    read_only=False,
    description="Reconcile all structural targets so only the current job target remains selected.",
)
@requires_services(
    app="plans/aura_base/app",
    ocr="plans/aura_base/ocr",
)
def resonance_pc_reconcile_structural_selection(
    route_id: Optional[str] = None,
    gp_stage_name: Optional[str] = None,
    region: Optional[List[int]] = None,
    rgb_min: Optional[List[int]] = None,
    rgb_max: Optional[List[int]] = None,
    match_mode: str = "contains",
    after_click_sec: float = 0.3,
    app: Any = None,
    ocr: Any = None,
) -> Dict[str, Any]:
    if app is None or ocr is None:
        _raise_error("missing_service", "app/ocr service is required")

    target_stage_key = _resolve_structural_stage_key(route_id=route_id, gp_stage_name=gp_stage_name)
    region_tuple = _coerce_region(region, list(_STRUCTURAL_STAGE_REGION))

    rgb_min_raw = rgb_min if isinstance(rgb_min, list) else list(_STRUCTURAL_SELECTED_RGB_MIN)
    rgb_max_raw = rgb_max if isinstance(rgb_max, list) else list(_STRUCTURAL_SELECTED_RGB_MAX)
    if len(rgb_min_raw) != 3 or len(rgb_max_raw) != 3:
        _raise_error("invalid_rgb_range", "rgb_min/rgb_max must both be [r, g, b]")
    rgb_min_tuple = (int(rgb_min_raw[0]), int(rgb_min_raw[1]), int(rgb_min_raw[2]))
    rgb_max_tuple = (int(rgb_max_raw[0]), int(rgb_max_raw[1]), int(rgb_max_raw[2]))

    operations: List[Dict[str, Any]] = []
    for stage_key in _STRUCTURAL_STAGE_TEXT:
        before_state = _read_structural_target_state(app, stage_key, rgb_min_tuple, rgb_max_tuple)
        is_target = stage_key == target_stage_key
        should_click = (is_target and not before_state["selected"]) or ((not is_target) and before_state["selected"])
        action_name = "keep"
        after_state = before_state

        if should_click:
            items = _recognize_text_items(app=app, ocr=ocr, region=region_tuple)
            hit = _find_structural_target_hit(items=items, stage_key=stage_key, match_mode=match_mode)
            if hit is None:
                _raise_error(
                    "structural_target_not_found",
                    f"failed to locate structural target '{before_state['display_name']}' in settings panel",
                    {"stage_key": stage_key, "region": list(region_tuple)},
                )
            click_x, click_y = hit["center"]
            app.click(x=int(click_x), y=int(click_y))
            time.sleep(max(float(after_click_sec), 0.0))
            after_state = _read_structural_target_state(app, stage_key, rgb_min_tuple, rgb_max_tuple)
            expected_selected = is_target
            if bool(after_state["selected"]) != expected_selected:
                _raise_error(
                    "structural_target_toggle_failed",
                    f"failed to {'select' if is_target else 'deselect'} structural target '{before_state['display_name']}'",
                    {
                        "stage_key": stage_key,
                        "before": before_state,
                        "after": after_state,
                    },
                )
            action_name = "select" if is_target else "deselect"

        operations.append(
            {
                "stage_key": stage_key,
                "display_name": before_state["display_name"],
                "is_target": is_target,
                "action": action_name,
                "before": before_state,
                "after": after_state,
            }
        )

    final_states = {
        stage_key: _read_structural_target_state(app, stage_key, rgb_min_tuple, rgb_max_tuple)
        for stage_key in _STRUCTURAL_STAGE_TEXT
    }
    invalid_states = [
        state
        for stage_key, state in final_states.items()
        if bool(state["selected"]) != (stage_key == target_stage_key)
    ]
    if invalid_states:
        _raise_error(
            "structural_selection_invalid",
            f"structural selection did not converge to target '{_STRUCTURAL_STAGE_TEXT[target_stage_key]}'",
            {
                "target_stage_key": target_stage_key,
                "final_states": final_states,
                "operations": operations,
            },
        )

    logger.info(
        "[StructuralSelection] target=%s operations=%s final_states=%s",
        target_stage_key,
        operations,
        final_states,
    )
    return {
        "ok": True,
        "target_stage_key": target_stage_key,
        "target_stage_name": _STRUCTURAL_STAGE_TEXT[target_stage_key],
        "operations": operations,
        "final_states": final_states,
    }


@action_info(
    name="resonance_pc.prepare_battle_formation",
    public=True,
    read_only=False,
    description="Optionally select one battle formation and wait for the formation screen to stabilize.",
)
@requires_services(
    app="plans/aura_base/app",
)
def resonance_pc_prepare_battle_formation(
    formation_index: Optional[int] = None,
    settle_sec: float = 0.5,
    app: Any = None,
) -> Dict[str, Any]:
    if app is None:
        _raise_error("missing_service", "app service is required")

    try:
        settle = float(settle_sec)
    except (TypeError, ValueError) as exc:
        _raise_error("invalid_settle_sec", "settle_sec must be a number", {"cause": str(exc)})
    if settle < 0.0 or settle > 5.0:
        _raise_error("invalid_settle_sec", "settle_sec must be in [0,5]")

    selected_index: Optional[int]
    if formation_index is None or formation_index == "":
        selected_index = None
    else:
        try:
            selected_index = int(formation_index)
        except (TypeError, ValueError) as exc:
            _raise_error(
                "invalid_formation_index",
                "formation_index must be an integer",
                {"cause": str(exc)},
            )
        if selected_index not in _BATTLE_FORMATION_POINTS:
            _raise_error("invalid_formation_index", "formation_index must be in [1,4]")

    click_point: Optional[List[int]] = None
    if selected_index is not None:
        x, y = _BATTLE_FORMATION_POINTS[selected_index]
        app.click(x=x, y=y)
        click_point = [x, y]

    if settle > 0.0:
        time.sleep(settle)

    result = {
        "ok": True,
        "formation_index": selected_index,
        "selection_changed": selected_index is not None,
        "click_point": click_point,
        "settle_sec": settle,
    }
    logger.info(
        "[BattleFormation] formation_index=%s changed=%s click_point=%s settle_sec=%.3f",
        selected_index,
        result["selection_changed"],
        click_point,
        settle,
    )
    return result


@action_info(
    name="resonance_pc.wait_and_click_any_text",
    public=True,
    read_only=False,
    description="Continuously OCR a region until any target text is found, then click its center.",
)
@requires_services(
    app="plans/aura_base/app",
    ocr="plans/aura_base/ocr",
)
def resonance_pc_wait_and_click_any_text(
    targets: List[str],
    region: Optional[List[int]] = None,
    timeout_sec: float = 10.0,
    interval_sec: float = 0.5,
    match_mode: str = "contains",
    app: Any = None,
    ocr: Any = None,
) -> Dict[str, Any]:
    if app is None or ocr is None:
        _raise_error("missing_service", "app/ocr service is required")
    if not isinstance(targets, list) or len(targets) == 0:
        _raise_error("invalid_targets", "targets must be a non-empty list")

    region_tuple = _coerce_region(region, [0, 0, 1280, 720])
    timeout = max(float(timeout_sec), 0.1)
    interval = max(float(interval_sec), 0.1)

    start_time = time.time()
    poll_index = 0
    while time.time() - start_time < timeout:
        poll_index += 1
        items = _recognize_text_items(app=app, ocr=ocr, region=region_tuple)
        hit = _find_best_text_hit(items=items, targets=targets, match_mode=match_mode)
        logger.debug(
            "[WaitAndClickAnyText] poll=%s elapsed=%.1fs region=%s targets=%s items=%s hit=%s",
            poll_index,
            time.time() - start_time,
            list(region_tuple),
            targets,
            [
                {
                    "text": str(row.get("text") or ""),
                    "center": row.get("center"),
                    "confidence": round(float(row.get("confidence") or 0.0), 4),
                }
                for row in items
            ],
            {
                "text": str(hit.get("text") or ""),
                "center": hit.get("center"),
                "matched_target": str(hit.get("matched_target") or ""),
                "confidence": round(float(hit.get("confidence") or 0.0), 4),
            }
            if hit is not None
            else None,
        )
        if hit is not None:
            x, y = hit["center"]
            app.click(x=int(x), y=int(y))
            return {
                "found": True,
                "matched_target": str(hit.get("matched_target") or ""),
                "text": str(hit.get("text") or ""),
                "click_x": int(x),
                "click_y": int(y),
                "attempt": poll_index,
            }
        time.sleep(interval)

    return {
        "found": False,
        "matched_target": None,
        "region": list(region_tuple),
        "attempts": poll_index,
        "elapsed_sec": round(time.time() - start_time, 3),
    }


@action_info(
    name="resonance_pc.run_battle_resolution",
    public=True,
    read_only=False,
    description="Resolve in-battle next-step/capture branch by OCR polling.",
)
@requires_services(
    app="plans/aura_base/app",
    ocr="plans/aura_base/ocr",
)
def resonance_pc_run_battle_resolution(
    capture_count: Optional[int] = None,
    battle_poll_region: Optional[List[int]] = None,
    confirm_region: Optional[List[int]] = None,
    exit_hint_region: Optional[List[int]] = None,
    confirm_click_point: Optional[List[int]] = None,
    poll_interval_sec: float = 5.0,
    timeout_sec: float = 600.0,
    confirm_timeout_sec: float = 30.0,
    exit_hint_timeout_sec: float = 30.0,
    short_interval_sec: float = 0.5,
    match_mode: str = "contains",
    app: Any = None,
    ocr: Any = None,
) -> Dict[str, Any]:
    if app is None or ocr is None:
        _raise_error("missing_service", "app/ocr service is required")

    arrest_targets = ["逮捕"]
    confirm_targets = ["确认"]
    next_targets = ["下一步"]
    exit_targets = ["触碰空白区域退出"]

    capture_click_count: Optional[int] = None
    if capture_count is not None:
        capture_click_count = int(capture_count)
        if capture_click_count < 1:
            _raise_error("invalid_capture_count", "capture_count must be >= 1")

    battle_region = _coerce_region(battle_poll_region, [1130, 640, 100, 350])
    confirm_region_tuple = _coerce_region(confirm_region, [900, 510, 150, 60])
    exit_region = _coerce_region(exit_hint_region, [440, 600, 460, 100])

    confirm_click = confirm_click_point if isinstance(confirm_click_point, list) and len(confirm_click_point) == 2 else [830, 380]
    confirm_click_x = int(confirm_click[0])
    confirm_click_y = int(confirm_click[1])

    poll_interval = max(float(poll_interval_sec), 0.1)
    timeout = max(float(timeout_sec), 1.0)
    confirm_timeout = max(float(confirm_timeout_sec), 1.0)
    exit_timeout = max(float(exit_hint_timeout_sec), 1.0)
    short_interval = max(float(short_interval_sec), 0.1)
    events: List[Dict[str, Any]] = []

    start_time = time.time()
    poll_index = 0
    while time.time() - start_time < timeout:
        poll_index += 1
        items = _recognize_text_items(app=app, ocr=ocr, region=battle_region)
        if capture_click_count is not None:
            arrest_hit = _find_best_text_hit(items=items, targets=arrest_targets, match_mode=match_mode)
            next_hit = _find_best_text_hit(items=items, targets=next_targets, match_mode=match_mode)
            logger.debug(
                "[BattleResolution] poll=%s mode=capture elapsed=%.1fs region=%s items=%s arrest_hit=%s next_hit=%s",
                poll_index,
                time.time() - start_time,
                list(battle_region),
                [
                    {
                        "text": str(row.get("text") or ""),
                        "center": row.get("center"),
                        "confidence": round(float(row.get("confidence") or 0.0), 4),
                    }
                    for row in items
                ],
                {
                    "text": str(arrest_hit.get("text") or ""),
                    "center": arrest_hit.get("center"),
                    "confidence": round(float(arrest_hit.get("confidence") or 0.0), 4),
                }
                if arrest_hit is not None
                else None,
                {
                    "text": str(next_hit.get("text") or ""),
                    "center": next_hit.get("center"),
                    "confidence": round(float(next_hit.get("confidence") or 0.0), 4),
                }
                if next_hit is not None
                else None,
            )
            if arrest_hit is not None:
                arrest_x, arrest_y = arrest_hit["center"]
                app.click(x=int(arrest_x), y=int(arrest_y))
                events.append({"event": "click_arrest", "center": [int(arrest_x), int(arrest_y)]})

                confirm_deadline = time.time() + confirm_timeout
                confirm_hit: Optional[Dict[str, Any]] = None
                while time.time() < confirm_deadline:
                    confirm_items = _recognize_text_items(app=app, ocr=ocr, region=confirm_region_tuple)
                    confirm_hit = _find_best_text_hit(items=confirm_items, targets=confirm_targets, match_mode=match_mode)
                    if confirm_hit is not None:
                        break
                    time.sleep(short_interval)
                if confirm_hit is None:
                    _raise_error("battle_confirm_not_found", "failed to locate confirm text after arrest")

                extra_capture_clicks = max(capture_click_count - 1, 0)
                for _ in range(extra_capture_clicks):
                    app.click(x=confirm_click_x, y=confirm_click_y)
                    time.sleep(short_interval)
                events.append(
                    {
                        "event": "click_capture_point",
                        "center": [confirm_click_x, confirm_click_y],
                        "count": extra_capture_clicks,
                    }
                )

                confirm_x, confirm_y = confirm_hit["center"]
                app.click(x=int(confirm_x), y=int(confirm_y))
                events.append({"event": "click_confirm", "center": [int(confirm_x), int(confirm_y)]})
                time.sleep(1.0)

                post_confirm_deadline = time.time() + exit_timeout
                while time.time() < post_confirm_deadline:
                    exit_items = _recognize_text_items(app=app, ocr=ocr, region=exit_region)
                    exit_hit = _find_best_text_hit(items=exit_items, targets=exit_targets, match_mode=match_mode)
                    battle_items_after_confirm = _recognize_text_items(app=app, ocr=ocr, region=battle_region)
                    next_hit = _find_best_text_hit(
                        items=battle_items_after_confirm,
                        targets=next_targets,
                        match_mode=match_mode,
                    )
                    logger.debug(
                        "[BattleResolution] post_confirm exit_items=%s battle_items=%s exit_hit=%s next_hit=%s",
                        [
                            {
                                "text": str(row.get("text") or ""),
                                "center": row.get("center"),
                                "confidence": round(float(row.get("confidence") or 0.0), 4),
                            }
                            for row in exit_items
                        ],
                        [
                            {
                                "text": str(row.get("text") or ""),
                                "center": row.get("center"),
                                "confidence": round(float(row.get("confidence") or 0.0), 4),
                            }
                            for row in battle_items_after_confirm
                        ],
                        {
                            "text": str(exit_hit.get("text") or ""),
                            "center": exit_hit.get("center"),
                        }
                        if exit_hit is not None
                        else None,
                        {
                            "text": str(next_hit.get("text") or ""),
                            "center": next_hit.get("center"),
                        }
                        if next_hit is not None
                        else None,
                    )
                    if exit_hit is not None:
                        exit_x, exit_y = exit_hit["center"]
                        app.click(x=int(exit_x), y=int(exit_y))
                        events.append({"event": "click_exit_hint", "center": [int(exit_x), int(exit_y)]})
                        logger.info("[BattleResolution] branch=capture_exit events=%s", events)
                        return {
                            "ok": True,
                            "branch": "capture_exit",
                            "capture_count": capture_click_count,
                            "events": events,
                        }
                    if next_hit is not None:
                        next_x, next_y = next_hit["center"]
                        app.click(x=int(next_x), y=int(next_y))
                        events.append({"event": "click_next_step", "center": [int(next_x), int(next_y)]})
                        logger.info("[BattleResolution] branch=capture_fallback_next events=%s", events)
                        return {
                            "ok": True,
                            "branch": "capture_fallback_next",
                            "capture_count": capture_click_count,
                            "events": events,
                        }
                    time.sleep(short_interval)
                _raise_error(
                    "battle_post_confirm_unresolved",
                    "failed to locate either exit hint or next step after arrest confirm",
                    {
                        "battle_poll_region": list(battle_region),
                        "exit_hint_region": list(exit_region),
                        "capture_count": capture_click_count,
                    },
                )
        else:
            next_hit = _find_best_text_hit(items=items, targets=next_targets, match_mode=match_mode)
            logger.debug(
                "[BattleResolution] poll=%s mode=next_step elapsed=%.1fs region=%s items=%s next_hit=%s",
                poll_index,
                time.time() - start_time,
                list(battle_region),
                [
                    {
                        "text": str(row.get("text") or ""),
                        "center": row.get("center"),
                        "confidence": round(float(row.get("confidence") or 0.0), 4),
                    }
                    for row in items
                ],
                {
                    "text": str(next_hit.get("text") or ""),
                    "center": next_hit.get("center"),
                    "confidence": round(float(next_hit.get("confidence") or 0.0), 4),
                }
                if next_hit is not None
                else None,
            )
            if next_hit is not None:
                next_x, next_y = next_hit["center"]
                app.click(x=int(next_x), y=int(next_y))
                events.append({"event": "click_next_step", "center": [int(next_x), int(next_y)]})
                logger.info("[BattleResolution] branch=next_step events=%s", events)
                return {
                    "ok": True,
                    "branch": "next_step",
                    "capture_count": None,
                    "events": events,
                }

        time.sleep(poll_interval)

    _raise_error(
        "battle_resolution_timeout",
        f"battle resolution timed out after {timeout:.1f}s",
        {
            "capture_count": capture_click_count,
            "battle_poll_region": list(battle_region),
            "events": events,
        },
    )
