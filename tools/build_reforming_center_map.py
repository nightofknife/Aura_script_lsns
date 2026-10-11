"""Calibrate a translation-only prison map from real overview screenshots.

No OCR, game input, approximate layout coordinates, or synthetic room anchors.
Frame connections use measured shared template anchors. Single-anchor bridges
are explicitly marked weakly cross-checked rather than estimated or hidden.
"""
from __future__ import annotations

import argparse
import hashlib
import itertools
import json
from pathlib import Path
import sys

import cv2
import numpy as np

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from plans.resonance_pc.src.actions import _reforming_center_room_templates as vision

BRIDGES = (
    "2248c2f8-52e3-4e86-b54a-3b6f3001f60e",
    "f0bd1e9c-8fe4-47f4-a558-7e6bbf0bcd51",
    "411fa13f-37bf-440d-9697-86b7e838dc4c",
    "bece2ff0-47f6-4cfa-bcc8-ed4895a79e3f",
    "885922f9-c67b-4698-849e-fd24a19dabb8",
    "cfa19fd5-ea94-47cb-be7e-c7d722749182",
    "63de3cd5-91f2-4c6b-bb59-17ef1d3b5325",
)
# Visually verified full title in the real overview fixture. The existing
# template scores only 0.818262 here; this is NOT counted as a template pass.
# Center uses the same 72x22 template box convention as other map anchors.
MANUAL_ANCHORS = {"overview_05": {"成品仓库": [360, 565, 72, 22]}}


def read_image(path, crop=False):
    image = cv2.imdecode(np.fromfile(path, dtype=np.uint8), cv2.IMREAD_COLOR)
    if image is None:
        raise ValueError(f"Unreadable screenshot: {path}")
    if crop:
        image = image[56:776, 25:1305]
    if image.shape[:2] != (720, 1280):
        raise ValueError(f"Not a 1280x720 client: {path}")
    return image


def gather(attachment_dir):
    catalog = vision.load_catalog()
    folder = ROOT / "tests/fixtures/reforming_center_templates"
    manifest = json.loads((folder / "manifest.json").read_text(encoding="utf-8"))
    inputs = [(row["id"], folder / row["image"], False, row["source_id"])
              for row in manifest["fixtures"]]
    inputs += [(f"bridge_{i:02}", attachment_dir / f"codex-clipboard-{source}.png", True, source)
               for i, source in enumerate(BRIDGES)]
    frames, missing = [], []
    for name, path, crop, source in inputs:
        if not path.exists():
            missing.append(source)
            continue
        image = read_image(path, crop)
        # The bottom-right titles can bridge upper rooms to the lower map.
        # Exclude the fixed bottom-left toolbar instead of counting its labels.
        clean = image.copy()
        clean[596:720, :570] = 0
        hits = vision.room_hits(clean, catalog, region=(0, 90, 1280, 630))
        frames.append({"id": name, "source_id": source,
                       "sha256": hashlib.sha256(path.read_bytes()).hexdigest(),
                       "client_crop_xyxy": [25, 56, 1305, 776] if crop else None,
                       "anchors": {hit["room_name"]: hit["center"] for hit in hits},
                       "scores": {hit["room_name"]: round(hit["score"], 6) for hit in hits}})
        print(name, ":", ", ".join(row for row in frames[-1]["anchors"]))
    return {"frames": frames, "missing_sources": missing}


def add_bridge_anchors(observations):
    """Bounded supplement when a previous cache used stricter runtime ROIs."""
    catalog = vision.load_catalog()
    regions = {"overview_01": {"餐厅": (0, 185, 120, 50), "医务室": (0, 300, 150, 50)},
               "overview_05": {"成品仓库": (330, 540, 220, 56)}}
    for frame in observations["frames"]:
        if frame["id"] not in regions:
            continue
        image = read_image(ROOT / "tests/fixtures/reforming_center_templates" / f"{frame['id']}.png")
        for room, region in regions[frame["id"]].items():
            spec = catalog["rooms"][room]
            hits = [hit for asset in [spec["map_title"], *spec.get("map_title_variants", [])]
                    if (hit := vision.match_asset(image, asset, region)) is not None]
            if hits:
                hit = max(hits, key=lambda item: item["score"])
                frame["anchors"][room] = hit["center"]
                frame["scores"][room] = round(hit["score"], 6)
                measurement = {"room": room, "search_region": list(region),
                    "center": hit["center"], "score": round(hit["score"], 6)}
                records = frame.setdefault("bridge_measurements", [])
                if measurement not in records:
                    records.append(measurement)
                print("bridge", frame["id"], room, hit["center"], hit["score"])
            else:
                diagnostic = vision.match_asset(image, {**spec["map_title"], "threshold": -1}, region)
                print("bridge_missing", frame["id"], room, diagnostic)
    return observations


def add_manual_anchors(observations):
    for frame in observations["frames"]:
        for room, rectangle in MANUAL_ANCHORS.get(frame["id"], {}).items():
            x, y, width, height = rectangle
            center = [x+width//2, y+height//2]
            frame["anchors"][room] = center
            frame["scores"].pop(room, None)
            frame["manual_visual_annotations"] = [{"room": room, "rect": rectangle,
                "center": center, "evidence": "manual_visual_annotation",
                "template_diagnostic_score": 0.818262, "template_threshold": 0.92,
                "note": "Full room title visually confirmed in original overview fixture; not a matcher pass."}]
    return observations


def fit(observations, tolerance=5.0):
    frames = observations["frames"]
    links, conflicts = [], []
    adjacency = {frame["id"]: set() for frame in frames}
    by_id = {frame["id"]: frame for frame in frames}
    for left, right in itertools.combinations(frames, 2):
        shared = sorted(set(left["anchors"]) & set(right["anchors"]))
        if not shared:
            continue
        deltas = np.array([np.subtract(right["anchors"][room], left["anchors"][room])
                           for room in shared], dtype=float)
        delta = np.median(deltas, axis=0)
        residual = np.max(np.abs(deltas-delta), axis=1)
        agreeing = [room for room, error in zip(shared, residual) if error <= tolerance]
        if len(agreeing) != len(shared):
            conflicts.append({"frames": [left["id"], right["id"]], "shared_rooms": shared,
                              "reason": "shared_anchor_translation_disagreement"})
            continue
        links.append({"frames": [left["id"], right["id"]], "shared_rooms": agreeing,
                      "delta": delta.tolist(), "max_error_px": float(max(residual)),
                      "evidence": "multiple_shared_anchors" if len(shared) >= 2 else "single_shared_room_bridge"})
        adjacency[left["id"]].add(right["id"])
        adjacency[right["id"]].add(left["id"])
    components, remaining = [], set(adjacency)
    while remaining:
        pending, group = [min(remaining)], set()
        while pending:
            node = pending.pop()
            if node not in group:
                group.add(node)
                pending.extend(adjacency[node]-group)
        remaining -= group
        components.append(sorted(group))
    components.sort(key=lambda group: (-sum("医务室" in by_id[name]["anchors"] for name in group), -len(group)))
    rooms, summaries = {}, []
    for index, group in enumerate(components):
        room_names = sorted({room for name in group for room in by_id[name]["anchors"]})
        if not room_names:
            continue
        origin = "医务室" if "医务室" in room_names else room_names[0]
        # Fit screen_anchor = world_anchor + frame_offset. Fix one world anchor.
        variables = [("room", room) for room in room_names if room != origin]
        variables += [("frame", name) for name in group]
        lookup = {key: i for i, key in enumerate(variables)}
        equations, values, entries = [], [], []
        for name in group:
            for room, point in by_id[name]["anchors"].items():
                row = np.zeros(len(variables))
                row[lookup[("frame", name)]] = 1
                if room != origin:
                    row[lookup[("room", room)]] = 1
                equations.append(row)
                values.append(point)
                entries.append((name, room))
        matrix, values = np.array(equations), np.array(values)
        solution, _, rank, _ = np.linalg.lstsq(matrix, values, rcond=None)
        errors = np.max(np.abs(matrix @ solution-values), axis=1)
        maximum = float(max(errors))
        if maximum > tolerance or rank != len(variables):
            raise ValueError(f"Component {index} inconsistent: maximum residual={maximum:.3f}, rank={rank}/{len(variables)}")
        for room in room_names:
            if room in rooms:
                raise ValueError(f"Ambiguous disconnected room anchor: {room}")
            point = np.zeros(2) if room == origin else solution[lookup[("room", room)]]
            co_visible = {other for name in group if room in by_id[name]["anchors"]
                          for other in by_id[name]["anchors"] if other != room}
            rooms[room] = {"world_anchor": [round(float(x), 3) for x in point],
                           "component": index, "neighbors": sorted(co_visible),
                           "source_frames": [name for name in group if room in by_id[name]["anchors"]]}
        summaries.append({"id": index, "origin_room": origin, "frames": group,
                          "room_count": len(room_names), "maximum_residual_px": round(maximum, 6),
                          "rms_residual_px": round(float(np.sqrt(np.mean(errors**2))), 6),
                          "frame_offsets": {name: [round(float(v), 3) for v in solution[lookup[("frame", name)]]]
                                            for name in group},
                          "anchor_residuals": [{"frame": name, "room": room, "max_error_px": round(float(error), 6)}
                                               for (name, room), error in zip(entries, errors)]})
    expected = set(vision.load_catalog()["rooms"])
    return {"schema_version": 1, "reference_client": [1280, 720], "anchor_kind": "template_center",
            "coordinate_model": "screen_anchor = world_anchor + viewport_offset; x right, y down",
            "rooms": rooms, "calibration": {"translation_tolerance_px": tolerance,
            "minimum_shared_anchors": 1, "single_anchor_bridge_cross_validation": "weak; measured identity only",
            "connected": len(summaries) == 1,
            "room_count": len(rooms), "missing_rooms": sorted(expected-set(rooms)),
            "components": summaries, "frame_links": links, "source_conflicts": conflicts,
            "missing_sources": observations["missing_sources"],
            "sources": [{key: value for key, value in frame.items() if key != "scores"} for frame in frames]}}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--attachments", type=Path, default=Path("C:/Users/356/AppData/Local/Temp"))
    parser.add_argument("--observations", type=Path, default=ROOT / ".pytest_tmp/reforming_map_20261011/observations.json")
    parser.add_argument("--reuse-observations", action="store_true")
    parser.add_argument("--supplement-bridges", action="store_true")
    parser.add_argument("--output", type=Path, default=ROOT / "plans/resonance_pc/data/meta/reforming_center_map_pc.json")
    args = parser.parse_args()
    if args.reuse_observations:
        observations = json.loads(args.observations.read_text(encoding="utf-8"))
    else:
        observations = gather(args.attachments)
    if args.supplement_bridges:
        observations = add_bridge_anchors(observations)
    observations = add_manual_anchors(observations)
    if not args.reuse_observations or args.supplement_bridges:
        args.observations.parent.mkdir(parents=True, exist_ok=True)
        args.observations.write_text(json.dumps(observations, ensure_ascii=False, indent=2)+"\n", encoding="utf-8")
    result = fit(observations)
    args.output.write_text(json.dumps(result, ensure_ascii=False, indent=2)+"\n", encoding="utf-8")
    print(json.dumps({key: result["calibration"][key] for key in ("connected", "room_count", "missing_rooms", "source_conflicts")}, ensure_ascii=False))


if __name__ == "__main__":
    main()
