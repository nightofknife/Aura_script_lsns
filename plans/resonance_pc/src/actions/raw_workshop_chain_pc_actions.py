"""Pure result aggregation; the four-workshop sequence lives in task YAML."""
from __future__ import annotations

from packages.aura_core.api import action_info


ROOM_ORDER = (
    "植物纤维原料车间", "动物纤维原料车间",
    "缝纫工具原料车间", "人造纤维原料车间",
)


@action_info(name="resonance_pc.raw_workshops_summarize", public=True, read_only=True,
             description="Summarize the four ordered raw-workshop business results and overview handoffs without game input.")
def resonance_pc_raw_workshops_summarize(results: list):
    if not isinstance(results, list) or len(results) != len(ROOM_ORDER):
        raise ValueError("results must contain one slot for each of the four workshops")
    rows = []
    visited = []
    for room, value in zip(ROOM_ORDER, results):
        if value is None:
            rows.append({"room_name": room, "success": False, "status": "skipped",
                         "reason": "previous_workshop_not_ready"})
            continue
        row = dict(value) if isinstance(value, dict) else {}
        problem = None
        if row.get("room_name") != room:
            problem = "workshop_result_mismatch"
        elif row.get("status") not in {"completed", "partial", "blocked", "failed"}:
            problem = "workshop_result_invalid"
        elif row["status"] == "completed" and row.get("success") is not True:
            problem = "workshop_completion_not_confirmed"
        elif row.get("returned_to_overview") is not True or row.get("page_after") != "reforming_center_overview":
            problem = row.get("reason") or "overview_return_not_confirmed"
        if problem:
            row.update(success=False, status="failed", reason=problem)
        row["room_name"] = room
        rows.append(row)
        visited.append(row)
    failed = next((row for row in visited if row["status"] == "failed"), None)
    completed = sum(row["status"] == "completed" for row in visited)
    if failed is not None:
        status, reason, stopped = "failed", failed.get("reason") or "workshop_maintenance_failed", failed["room_name"]
    elif len(visited) != len(ROOM_ORDER):
        status, reason = "failed", "workshop_result_missing"
        stopped = next(row["room_name"] for row in rows if row["status"] == "skipped")
    elif completed == len(ROOM_ORDER):
        status, reason, stopped = "completed", "", None
    else:
        status, reason, stopped = "partial", "some_workshops_incomplete", None
    last = visited[-1] if visited else {}
    returned = last.get("returned_to_overview") is True and last.get("page_after") == "reforming_center_overview"
    return {"success": status == "completed", "status": status, "reason": reason,
            "results": rows, "visited_count": len(visited), "completed_count": completed,
            "skipped_count": len(ROOM_ORDER) - len(visited), "stopped_room": stopped,
            "returned_to_overview": returned, "page_after": last.get("page_after")}
