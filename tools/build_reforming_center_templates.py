"""Build screenshot-derived Reforming Center templates without OCR or game input.

The annotations below are manual pixel boxes from screenshots supplied by the
user. They are screenshot coordinates (including the desktop window frame).
Only the client rectangle [25, 56, 1305, 776] is used. No game-data-derived
interaction routes or rendered/artificial text is produced.
"""
from __future__ import annotations

import argparse
import hashlib
import json
from pathlib import Path

import cv2
import numpy as np

REPO = Path(__file__).resolve().parents[1]
PLAN = REPO / "plans/resonance_pc"
CLIENT_XYXY = (25, 56, 1305, 776)

# Source IDs are the exact user attachments. Boxes are manually selected and
# exclude room levels, prices, red notification dots, and changing counters.
OVERVIEW = {
    "top": "666e062d-25fb-4132-ad79-f8a67c43c139",
    "right": "2307449c-08d1-4dc3-9031-f421a2af431e",
    "left": "cbef82eb-6cbd-435b-b863-edffbf44f870",
    "yard": "09d2d6ff-6129-486f-852d-bfbd963f91bf",
    "warehouse": "7e12c250-bccf-439e-a309-99d44cf629e1",
    "orders": "cc6ccd36-5fd8-40dc-b557-2c112e9d524f",
}

# Manually read room names whose COMPLETE title fits inside the production
# scan ROI [30,90,1250,490]. The three left-edge titles in the second screenshot
# begin at client x~19, so they must not be expected inside that cropped ROI.
REPLAY_EXPECTED_NAMES = {
    "666e062d-25fb-4132-ad79-f8a67c43c139": ["1号整顿宿舍", "2号整顿宿舍", "1号生产车间", "2号生产车间"],
    "01591961-4b5e-484f-854f-550b8ba5ce86": ["植物纤维原料车间", "动物纤维原料车间", "缝纫工具原料车间", "人造纤维原料车间", "研发中心", "电力中心"],
    "2307449c-08d1-4dc3-9031-f421a2af431e": ["植物纤维原料车间", "动物纤维原料车间", "缝纫工具原料车间", "人造纤维原料车间", "研发中心", "电力中心", "财务中心", "转运中心"],
    "cc6ccd36-5fd8-40dc-b557-2c112e9d524f": ["黑月订单站", "原料采购站"],
    "7e12c250-bccf-439e-a309-99d44cf629e1": ["成品仓库", "原料仓库", "黑月订单站", "原料采购站"],
    "cbef82eb-6cbd-435b-b863-edffbf44f870": ["禁闭室", "医务室", "探视室", "假释室", "安保中心", "囚犯管理中心"],
    "09d2d6ff-6129-486f-852d-bfbd963f91bf": ["放风场地", "餐厅", "禁闭室", "医务室", "探视室", "假释室"],
}
MAP_TITLES = {
    "2号整顿宿舍": ("top", (202, 316, 103, 21)),
    "1号整顿宿舍": ("top", (202, 433, 103, 21)),
    "2号生产车间": ("top", (850, 316, 103, 21)),
    "1号生产车间": ("top", (850, 433, 103, 21)),
    "放风场地": ("yard", (270, 293, 73, 22)),
    "餐厅": ("yard", (697, 293, 39, 22)),
    "禁闭室": ("left", (392, 241, 56, 22)),
    "医务室": ("left", (746, 241, 56, 22)),
    "探视室": ("left", (392, 358, 56, 23)),
    "假释室": ("left", (746, 358, 56, 23)),
    "安保中心": ("left", (392, 478, 74, 23)),
    "囚犯管理中心": ("left", (746, 478, 108, 23)),
    "成品仓库": ("warehouse", (117, 273, 72, 22)),
    "原料仓库": ("warehouse", (117, 445, 72, 22)),
    "植物纤维原料车间": ("right", (360, 217, 139, 22)),
    "动物纤维原料车间": ("right", (713, 217, 139, 22)),
    "缝纫工具原料车间": ("right", (360, 337, 139, 22)),
    "人造纤维原料车间": ("right", (713, 337, 139, 22)),
    "研发中心": ("right", (360, 454, 72, 22)),
    "电力中心": ("right", (713, 454, 72, 22)),
    "财务中心": ("right", (360, 574, 72, 22)),
    "转运中心": ("right", (713, 574, 72, 22)),
    "黑月订单站": ("orders", (534, 273, 90, 22)),
    "原料采购站": ("orders", (534, 445, 90, 22)),
}

# Two independently supplied panning screenshots have a different sub-pixel
# font rasterization. Keep actual screenshot variants rather than weakening
# the shared match threshold or synthesizing text.
MAP_TITLE_VARIANTS = {
    "植物纤维原料车间": [("01591961-4b5e-484f-854f-550b8ba5ce86", (690, 260, 139, 22))],
    "原料采购站": [("7e12c250-bccf-439e-a309-99d44cf629e1", (1131, 445, 90, 22))],
}

SELECTED_TITLES = {
    "植物纤维原料车间": ("b0ec2014-d352-471d-a5ed-6e481656b87e", (133, 314, 283, 39)),
    "动物纤维原料车间": ("7601cdf5-1c39-4754-a3a2-11a5a5e81944", (90, 326, 253, 37)),
    "缝纫工具原料车间": ("a9cf78d6-d85c-4ae2-a169-0866e6e5fcfe", (133, 314, 283, 39)),
    "人造纤维原料车间": ("f680705d-8d5f-43d0-a8c6-b5ad63399055", (133, 314, 283, 39)),
    "研发中心": ("8c0ab801-994c-4868-aa4f-b37577673d7b", (133, 314, 143, 39)),
    "转运中心": ("85faf0fa-6a0b-4579-a52e-86beefe0d43b", (133, 314, 143, 39)),
    "禁闭室": ("ac03e38e-edb9-4a00-9148-69cb49bfc264", (133, 314, 106, 39)),
    "医务室": ("31214f9e-928e-4bbe-904d-6cd9abc7f32c", (133, 314, 106, 39)),
    "假释室": ("a84588ee-d644-4f7d-a65f-88eb412d9ea3", (133, 314, 106, 39)),
    "餐厅": ("12707809-50d9-4bcd-b0b2-dce974ce1cff", (133, 314, 74, 39)),
    "电力中心": ("9c1b5a17-183d-46e9-9fff-1cc6c5044e9c", (133, 314, 143, 39)),
    "放风场地": ("bbad8c0b-b81b-4b91-ab60-1b969a78b0da", (90, 326, 137, 37)),
    "探视室": ("97c8c3af-7891-456e-ae12-8cae07d70b46", (133, 314, 106, 39)),
    "安保中心": ("44b52e42-f96a-4988-8e02-bfd0898dbd07", (133, 314, 143, 39)),
}

BUTTONS = {
    "production": ("b0ec2014-d352-471d-a5ed-6e481656b87e", (746, 358, 126, 36)),
    "detail": ("31214f9e-928e-4bbe-904d-6cd9abc7f32c", (746, 358, 126, 36)),
    "construction": ("12707809-50d9-4bcd-b0b2-dce974ce1cff", (746, 389, 126, 36)),
}

# (source ID, [(label, screen xywh, kind), ...]). White/gray glyphs are
# white_text; black/dark glyphs are color and retain their real background.
DESTINATIONS = {
    "植物纤维原料车间": ("f81060c5-b490-4b80-8a5e-b802456c7176", [
        ("title", (387, 78, 214, 30), "white_text"),
        ("select_prisoner", (902, 712, 77, 25), "white_text")]),
    "动物纤维原料车间": ("f242fc14-93b2-4006-85a1-1a55df25b4f7", [
        ("title", (387, 78, 214, 30), "white_text"),
        ("select_prisoner", (902, 712, 77, 25), "white_text")]),
    "缝纫工具原料车间": ("77cf1072-27f0-40f2-bc3b-a51866c3c61f", [
        ("title", (387, 78, 214, 30), "white_text"),
        ("select_prisoner", (902, 712, 77, 25), "white_text")]),
    "人造纤维原料车间": ("3bca14b5-ed29-4b16-8e2a-adab68280a41", [
        ("title", (387, 78, 214, 30), "white_text"),
        ("select_prisoner", (902, 712, 77, 25), "white_text")]),
    "研发中心": ("1927e684-9826-45b5-a9ee-5d3a2cb97c42", [
        ("title", (550, 157, 92, 28), "white_text"),
        ("research_formula", (929, 150, 73, 25), "color")]),
    "转运中心": ("b98b7f19-48f7-4344-9e2e-120e85e3fef1", [
        ("title", (973, 80, 140, 35), "white_text"),
        ("goods_transport", (909, 687, 92, 27), "color")]),
    "禁闭室": ("a7f53f8d-51c5-470b-993f-c7c5bee183b2", [
        ("title", (392, 65, 90, 31), "white_text"),
        ("view_report", (855, 85, 72, 24), "color")]),
    "医务室": ("d8bb5f12-da83-4ab5-bfff-1944ca583fcf", [
        ("title", (451, 69, 90, 31), "white_text"),
        ("waiting_queue", (1190, 103, 90, 23), "white_text")]),
    "假释室": ("68445bcb-cc49-4610-9dee-1dfd44af8790", [
        ("title", (503, 69, 101, 32), "white_text"),
        ("parole_all", (901, 205, 93, 27), "color")]),
    "dormitory": ("ba7a37e6-07e2-4dcd-857f-c90d276872d3", [
        ("title", (706, 78, 81, 22), "white_text"),
        ("dorm_navigation", (143, 715, 87, 23), "white_text")]),
    "product_workshop": ("249b7a0e-253f-4690-ae3e-4f1d574cd508", [
        ("title", (688, 78, 81, 22), "white_text"),
        ("workshop_edit", (1118, 730, 87, 23), "white_text")]),
    "原料采购站": ("15cb14c9-fa01-4b3f-a5a5-8293e9104b95", [
        ("purchase_list", (289, 602, 170, 33), "white_text"),
        ("one_click_purchase", (322, 710, 96, 24), "white_text")]),
    "warehouse": ("a4ef51a2-2751-44d0-adda-8676a68d6849", [
        ("raw_warehouse", (1149, 267, 89, 23), "white_text"),
        ("product_warehouse", (1149, 600, 89, 23), "white_text")]),
    "黑月订单站": ("61602bd8-7c99-42b2-a7bf-6c829209fbc8", [
        ("title", (886, 212, 203, 45), "white_text"),
        ("transport_orders", (819, 365, 91, 26), "color")]),
    "囚犯管理中心": ("31793e96-0b44-4f99-adf8-194c238fb496", [
        ("roster", (283, 77, 101, 32), "white_text"),
        ("admit", (1140, 729, 122, 24), "white_text")]),
    "财务中心": ("1f89a7d6-4a2c-4510-adb4-882c4dca38f8", [
        ("title", (619, 230, 173, 44), "white_text"),
        ("deposit", (971, 539, 96, 26), "white_text")]),
}


def white_ink(image):
    hsv = cv2.cvtColor(image, cv2.COLOR_BGR2HSV)
    return cv2.inRange(hsv, (0, 0, 170), (179, 80, 255))


def build(source_dir: Path, replay_dir: Path | None = None):
    if replay_dir is not None:
        replay_dir = replay_dir.resolve()
        if not replay_dir.is_relative_to(REPO.resolve()) or replay_dir == REPO.resolve():
            raise ValueError("Replay artifacts must be in a dedicated directory inside this repository")
    output = PLAN / "templates/reforming_center"
    output.mkdir(parents=True, exist_ok=True)
    layout = json.loads((PLAN / "data/meta/reforming_center_rooms_pc.json").read_text(encoding="utf-8"))["rooms"]
    source_cache = {}
    sources = {}

    def asset(identifier, screen_box, name, kind="white_text", search_region=None):
        if identifier not in source_cache:
            path = source_dir / f"codex-clipboard-{identifier}.png"
            binary = path.read_bytes()
            image = cv2.imdecode(np.frombuffer(binary, np.uint8), cv2.IMREAD_COLOR)
            if image is None or image.shape[:2] != (802, 1332):
                raise ValueError(f"Unexpected source dimensions: {path}")
            source_cache[identifier] = image[56:776, 25:1305]
            sources[identifier] = {"filename": path.name, "sha256": hashlib.sha256(binary).hexdigest(),
                                   "dimensions": [1332, 802], "client_crop_xyxy": list(CLIENT_XYXY)}
        sx, sy, w, h = screen_box
        x, y = sx - 25, sy - 56
        crop = source_cache[identifier][y:y+h, x:x+w].copy()
        if crop.shape[:2] != (h, w) or w < 1 or h < 1:
            raise ValueError(f"Invalid manual annotation: {name} {screen_box}")
        image_name = f"{name}.png"
        cv2.imwrite(str(output / image_name), crop)
        item = {"image": f"templates/reforming_center/{image_name}", "kind": kind,
                "source_id": identifier, "source_bbox_xywh": [x, y, w, h],
                "title_center_offset": [w / 2, h / 2]}
        if kind == "white_text":
            mask = white_ink(crop)
            if np.count_nonzero(mask) < 12:
                raise ValueError(f"No stable white text in manual annotation: {name}")
            mask_name = f"{name}.mask.png"
            cv2.imwrite(str(output / mask_name), mask)
            item["mask"] = f"templates/reforming_center/{mask_name}"
        item["search_region"] = search_region or [max(0, x-12), max(0, y-12),
                                                  min(1280-max(0, x-12), w+24),
                                                  min(720-max(0, y-12), h+24)]
        return item

    destinations = {}
    for number, (room, (identifier, annotations)) in enumerate(DESTINATIONS.items()):
        destinations[room] = [asset(identifier, box, f"destination_{number:02d}_{label}", kind)
                              for label, box, kind in annotations]
    rooms = {}
    for number, (room, (source, box)) in enumerate(MAP_TITLES.items()):
        entry_type = "production" if room in list(SELECTED_TITLES)[:4] else (
            "detail" if room in list(SELECTED_TITLES)[4:9] else (
                "selection_only" if room in list(SELECTED_TITLES)[9:] else "direct"))
        item = {"room_key": room, "map_point": layout[room]["map_point"],
                "aliases": layout[room]["aliases"], "click_offset": layout[room]["click_offset"],
                "entry_type": entry_type,
                "map_title": asset(OVERVIEW[source], box, f"map_{number:02d}", search_region=[0, 90, 1280, 630])}
        item["map_title_variants"] = [
            asset(identifier, variant_box, f"map_{number:02d}_variant_{variant:02d}",
                  search_region=[0, 90, 1280, 630])
            for variant, (identifier, variant_box) in enumerate(MAP_TITLE_VARIANTS.get(room, []))
        ]
        if room in SELECTED_TITLES:
            selected_source, selected_box = SELECTED_TITLES[room]
            item["selected_title"] = asset(selected_source, selected_box, f"selected_{number:02d}",
                                            search_region=[0, 180, 800, 350])
        key = "dormitory" if "整顿宿舍" in room else (
            "product_workshop" if "生产车间" in room else (
                "warehouse" if room in {"原料仓库", "成品仓库"} else room))
        item["destination_markers"] = destinations.get(key, [])
        item["shared_destination"] = key if key != room else None
        rooms[room] = item
    buttons = {name: asset(identifier, box, f"button_{name}", "color", [650, 260, 300, 210])
               for name, (identifier, box) in BUTTONS.items()}
    metadata = {"schema_version": 1, "reference_client": [1280, 720],
                "annotation_method": "Manual screenshot visual boxes; no OCR and no generated text",
                "white_ink_hsv": {"lower": [0, 0, 170], "upper": [179, 80, 255]},
                "source_screenshots": sources, "rooms": rooms, "buttons": buttons,
                "unsupported_rooms": [key for key in layout if key not in rooms],
                "scope_note": "3-6 dormitories/workshops have no supplied unlocked screenshots. Shared destination markers do not prove room number independently."}
    meta_path = PLAN / "data/meta/reforming_center_templates_pc.json"
    meta_path.write_text(json.dumps(metadata, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    if replay_dir is not None:
        replay_dir.mkdir(parents=True, exist_ok=True)
        fixtures = []
        for number, (identifier, expected_names) in enumerate(REPLAY_EXPECTED_NAMES.items()):
            image_name = f"overview_{number:02d}.png"
            image_path = replay_dir / image_name
            # PNG compression changes byte size only; the replay pixel array
            # remains identical to the original client crop.
            cv2.imwrite(str(image_path), source_cache[identifier], [cv2.IMWRITE_PNG_COMPRESSION, 9])
            fixtures.append({"id": f"overview_{number:02d}", "image": image_name,
                             "source_id": identifier, "source_filename": sources[identifier]["filename"],
                             "source_sha256": sources[identifier]["sha256"],
                             "client_png_sha256": hashlib.sha256(image_path.read_bytes()).hexdigest(),
                             "client_crop_xyxy": list(CLIENT_XYXY), "expected_names": expected_names,
                             "excluded_edge_names": ["餐厅", "医务室", "假释室"] if number == 1 else []})
        replay_manifest = {"schema_version": 1, "reference_client": [1280, 720],
                           "scan_region": [30, 90, 1250, 490], "expected_method": "Manual visual annotation, not runtime output",
                           "fixtures": fixtures}
        (replay_dir / "manifest.json").write_text(json.dumps(replay_manifest, ensure_ascii=False, indent=2)+"\n", encoding="utf-8")
    print(json.dumps({"rooms": len(rooms), "sources": len(sources), "asset_files": len(list(output.glob("*.png"))),
                      "metadata": str(meta_path), "replay_dir": str(replay_dir) if replay_dir else None}, ensure_ascii=False))


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--source-dir", type=Path, required=True, help="Read-only directory containing user screenshot attachments")
    parser.add_argument("--replay-dir", type=Path, help="Optional repository-local directory for 7 lossless client PNG fixtures")
    args = parser.parse_args()
    build(args.source_dir.resolve(), args.replay_dir)
