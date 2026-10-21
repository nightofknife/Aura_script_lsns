"""Room-map geometry and bounded, feedback-driven viewport navigation.

Screenshot-measured anchors take precedence over native prefab extensions.
"""
from __future__ import annotations

import heapq
import json
import math
from functools import lru_cache
from pathlib import Path
from statistics import median


class RoomMapError(RuntimeError):
    def __init__(self, code, detail=None):
        super().__init__(code)
        self.code, self.detail = code, detail or {}


@lru_cache(maxsize=1)
def load_map():
    path = Path(__file__).resolve().parents[2] / "data/meta/reforming_center_map_pc.json"
    model = json.loads(path.read_text(encoding="utf-8"))
    if model.get("reference_client") != [1280, 720] or model.get("anchor_kind") != "template_center":
        raise RoomMapError("room_map_invalid")
    for name, room in model["rooms"].items():
        point = room.get("world_anchor", [])
        if len(point) != 2 or not all(math.isfinite(value) for value in point):
            raise RoomMapError("room_map_invalid", {"room_name": name})
        for neighbor in room.get("neighbors", []):
            if neighbor not in model["rooms"] or name not in model["rooms"][neighbor]["neighbors"]:
                raise RoomMapError("room_map_invalid", {"room_name": name, "neighbor": neighbor})
    return model


def locate_viewport(hits, model, tolerance=8):
    """Consensus of screen - world offsets; never silently merge components."""
    candidates = []
    for hit in hits:
        room = model["rooms"].get(hit["room_name"])
        center = hit.get("center", [])
        score = hit.get("score", 0)
        if (room and len(center) == 2 and math.isfinite(score) and score >= .92
                and all(math.isfinite(value) for value in center)):
            candidates.append({"room_name": hit["room_name"], "component": room["component"],
                               "offset": [center[i]-room["world_anchor"][i] for i in (0, 1)],
                               "native_extension": room.get("geometry_source") == "native_rect_transform_relative"})
    if not candidates:
        raise RoomMapError("room_anchor_not_found")
    observed_candidates = candidates
    # Prefab positions are navigation hints, not additional measured evidence.
    # Keep the existing measured pose whenever any measured label is visible;
    # extensions can only join that pose if their fresh label agrees with it.
    measured = [item for item in candidates if not item["native_extension"]]
    if measured:
        candidates = measured
    clusters = {}
    for item in candidates:
        members = [index for index, other in enumerate(candidates)
                   if other["component"] == item["component"]
                   and max(abs(other["offset"][axis]-item["offset"][axis]) for axis in (0, 1)) <= tolerance]
        clusters[tuple(members)] = members
    maximum = max(len(group) for group in clusters.values())
    winners = [group for group in clusters.values() if len(group) == maximum]
    # Multiple different equally supported locations are ambiguous, not a cue
    # to average offsets from unrelated parts of the map.
    if len(winners) != 1:
        raise RoomMapError("room_map_anchors_inconsistent",
                           {"anchors": [item["room_name"] for item in candidates]})
    group = [candidates[index] for index in winners[0]]
    offset = [float(median(item["offset"][axis] for item in group)) for axis in (0, 1)]
    consistent_extensions = [item for item in observed_candidates
                             if measured and item["native_extension"]
                             and item["component"] == group[0]["component"]
                             and max(abs(item["offset"][axis]-offset[axis]) for axis in (0, 1)) <= tolerance]
    accepted = group+consistent_extensions
    return {"offset": offset, "component": group[0]["component"],
            "anchors": [item["room_name"] for item in accepted], "single_anchor": len(group) == 1,
            "rejected_anchors": [item["room_name"] for item in observed_candidates if item not in accepted]}


def predict_screen(room, pose, model):
    point = model["rooms"][room]["world_anchor"]
    if model["rooms"][room]["component"] != pose["component"]:
        raise RoomMapError("room_map_component_disconnected", {"target": room})
    return [point[axis]+pose["offset"][axis] for axis in (0, 1)]


def choose_waypoint(target, pose, model):
    """Use room links as intermediate navigation anchors; observe before clicking."""
    predict_screen(target, pose, model)  # Refuse a fabricated cross-component link.
    candidates = []
    rooms = model["rooms"]
    for source in pose["anchors"]:
        queue = [(0.0, source, [source])]
        best = {source: 0.0}
        while queue:
            distance, current, route = heapq.heappop(queue)
            if distance > best.get(current, math.inf):
                continue
            if current == target:
                candidates.append((distance, len(route), source, route))
                break
            for neighbor in rooms[current]["neighbors"]:
                hop = math.dist(rooms[current]["world_anchor"], rooms[neighbor]["world_anchor"])
                new_distance = distance+hop
                if new_distance < best.get(neighbor, math.inf):
                    best[neighbor] = new_distance
                    heapq.heappush(queue, (new_distance, neighbor, route+[neighbor]))
    if not candidates:
        raise RoomMapError("room_map_route_not_found", {"target": target})
    _, _, _, route = min(candidates)
    waypoint = route[1] if len(route) > 1 else target
    return {"room_name": waypoint, "screen_point": predict_screen(waypoint, pose, model), "route": route}


def plan_drag(screen_point, single_anchor=False, desired=(520, 300), region=(420, 170, 520, 360)):
    """Move content toward desired point, one axis at a time, within safe ROI."""
    dx, dy = (desired[axis]-screen_point[axis] for axis in (0, 1))
    axis = 0 if abs(dx) >= abs(dy) else 1
    error = dx if axis == 0 else dy
    span = (120 if single_anchor else 420) if axis == 0 else (90 if single_anchor else 260)
    movement = int(round(max(-span, min(span, error))))
    if movement == 0:
        raise RoomMapError("room_map_no_useful_drag")
    center = [region[0]+region[2]//2, region[1]+region[3]//2]
    start, end = list(center), list(center)
    start[axis] -= movement//2
    end[axis] += movement-movement//2
    for point in (start, end):
        if not all(region[a]+20 <= point[a] <= region[a]+region[a+2]-20 for a in (0, 1)):
            raise RoomMapError("room_drag_outside_safe_region")
    return {"start": start, "end": end, "axis": axis, "content_shift": movement,
            "predicted_target_title": list(screen_point), "desired_title": list(desired)}


def drag_progress(previous_pose, pose, drag, stalled_count):
    if pose["component"] != previous_pose["component"]:
        raise RoomMapError("room_map_component_changed")
    axis = drag["axis"]
    moved = pose["offset"][axis]-previous_pose["offset"][axis]
    if abs(moved) <= 3:
        stalled_count += 1
        if stalled_count >= 2:
            raise RoomMapError("room_drag_stalled", {"axis": axis, "observed_shift": moved})
    elif moved*drag["content_shift"] < 0:
        raise RoomMapError("room_drag_wrong_direction", {"observed_shift": moved})
    else:
        stalled_count = 0
    return stalled_count
