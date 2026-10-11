"""Crop user-supplied game pixels and make review sheets; no OCR/game input.

Native control crops are integrated into the offline-validated maintenance
runtime. Missing live-state coverage is documented separately. Preview labels
are annotations only; no generated glyph enters a template.
"""
from __future__ import annotations

import argparse
import hashlib
import json
from pathlib import Path

from PIL import Image, ImageDraw, ImageFont

ROOT = Path(__file__).resolve().parents[1]
PLAN = ROOT / "plans/resonance_pc"
CLIENT = (25, 56, 1305, 776)
SOURCES = {
    "idle_blue": "bb20fa22-f8b8-4181-b8d0-033e8871ed2f",
    "deposit_dialog": "9fba6cd4-f24e-4d00-bbce-fcd6683bfaf1",
    "deposit_full": "318ff7c3-ca38-4bd0-ab9c-8296fbe6f98f",
    "idle_gray": "3a15c8fd-73a2-42d3-8b29-50457e8022d2",
    "selection_empty": "830fa130-4193-455c-9183-2dc00847b230",
    "selection_checked": "76ed5e59-122b-47b1-92cd-81a78f2d5aa0",
    "staff_full": "c36cafc2-1d85-4d13-97cd-824db0eb0b26",
    "running": "623785cd-7d53-4a50-8acb-8086f4964f5d",
}

# Every box is manually selected in 1280x720 client coordinates: x,y,w,h.
# Variable values/portraits are excluded from reusable control templates.
ANNOTATIONS = [
    ("A01", "deposit_button", "预存按钮", "idle_blue", (330, 420, 140, 40), "control", "保留黄色背景、图标和文字"),
    ("A02", "deposit_max", "金额层：最多", "deposit_dialog", (865, 350, 60, 60), "control", "保留边框和文字"),
    ("A03", "deposit_confirm", "金额层：确定", "deposit_dialog", (905, 510, 110, 50), "control", "保留蓝底、确认图标和文字"),
    ("A04", "deposit_cancel", "金额层：取消", "deposit_dialog", (270, 510, 110, 50), "control", "金额层辅助标识／取消入口"),
    ("A05", "deposit_currency", "金额层：铁盟币", "deposit_dialog", (605, 305, 80, 30), "marker", "不包含可变金额"),
    ("A06", "deposit_full_toast", "预存已达上限提示", "deposit_full", (530, 345, 225, 32), "marker", "只取黑条固定文字附近"),
    ("A07", "start_enabled", "开始生产：蓝色可用", "idle_blue", (205, 650, 125, 40), "control_state", "保留蓝底、齿轮和文字"),
    ("A08", "start_disabled", "开始生产：灰色禁用", "idle_gray", (205, 650, 125, 40), "control_state", "与蓝色状态同一裁切范围"),
    ("A09", "running_stop", "生产中：终止（只观察）", "running", (115, 650, 100, 40), "marker", "保留红底；不是可执行点击"),
    ("A10", "running_speed", "生产中：加速（只观察）", "running", (330, 650, 100, 40), "marker", "与终止组合确认生产中"),
    ("A11", "slider_track_reference", "滑块初始状态参考", "deposit_dialog", (495, 375, 290, 30), "reference_only", "非最大值模板，不包含上方金额"),
    ("A12", "deposit_min_marker", "金额层：最少", "deposit_dialog", (360, 350, 60, 60), "marker", "辅助确认是预存金额层"),
    ("B01", "work_tab_selected", "工作名单：已选页签", "idle_gray", (660, 280, 76, 34), "tab_state", "排除右侧人数"),
    ("B02", "reserve_tab_unselected", "替补名单：未选页签", "idle_gray", (1005, 280, 73, 34), "tab_state", "尚无替补选中状态素材"),
    ("B03", "choose_prisoners", "车间：选择囚犯", "idle_gray", (870, 650, 100, 40), "control", "不包含左侧人数框"),
    ("B04", "select_all", "选择页：一键选中", "selection_empty", (875, 650, 120, 40), "control_state", "只可点击一次"),
    ("B05", "cancel_all", "选择页：一键取消", "selection_checked", (875, 650, 120, 40), "marker", "选中后的状态，不执行取消"),
    ("B06", "staff_confirm", "选择页：确认", "selection_empty", (1110, 650, 100, 40), "control", "保留白底和黑色图标文字"),
    ("B07", "prisoner_selected_ribbon", "囚犯卡：选中标记", "selection_checked", (45, 226, 155, 26), "marker", "只取黄条，不取人物头像"),
    ("B08", "selection_footer", "选择页：车间囚犯数量", "selection_empty", (30, 650, 145, 35), "marker", "排除右侧可变人数"),
    ("B09", "selection_back", "选择页：返回箭头", "selection_empty", (70, 25, 50, 30), "control", "辅助页面标识"),
    ("B10", "work_count_0_7_sample", "工作人数样本：0/7", "idle_gray", (735, 282, 35, 28), "counter_sample", "仅人数观察区，不当通用页标识"),
    ("B11", "work_count_7_7_sample", "工作人数样本：7/7", "staff_full", (735, 282, 35, 28), "counter_sample", "与0/7使用同一个观察区"),
    ("B12", "reserve_count_0_2_sample", "替补人数样本：0/2", "staff_full", (1080, 282, 40, 28), "counter_sample", "不把容量2固定到所有房间"),
    ("B13", "selection_count_0_7_sample", "选择人数样本：0/7", "selection_empty", (175, 650, 45, 35), "counter_sample", "位于选择页底栏"),
    ("B14", "selection_count_7_7_sample", "选择人数样本：7/7", "selection_checked", (175, 650, 45, 35), "counter_sample", "保留选中人数的黄色状态"),
]
CLICK_POINTS = {
    "deposit": [400, 440], "deposit_max": [895, 380],
    "deposit_confirm": [960, 535], "work_tab": [730, 300],
    "reserve_tab": [1060, 300], "choose_prisoners": [885, 670],
    "select_all": [935, 670], "staff_confirm": [1150, 670],
    "start": [265, 675],
}
MISSING = [
    "替补页签已选中／工作页签未选中的真实画面",
    "点击最多之后，滑块位于最大位置的真实画面",
    "生产中切换到替补名单并出现选择囚犯入口的真实画面",
]


def font(size):
    return ImageFont.truetype("C:/Windows/Fonts/msyh.ttc", size)


def review_sheet(records, title, path):
    columns, cw, ch, gap = 3, 380, 170, 14
    rows = (len(records)+columns-1)//columns
    canvas = Image.new("RGB", (columns*cw+(columns+1)*gap, 125+rows*(ch+gap)), "#101923")
    draw = ImageDraw.Draw(canvas)
    draw.text((20, 14), title, font=font(25), fill="#f3f6fa")
    draw.text((20, 55), "实机原像素裁切 · 以下预览放大显示 · 标签不是模板内容", font=font(18), fill="#b7c6d6")
    draw.text((20, 82), "候选素材，未跑匹配测试；人数样本和滑块参考不作为通用按钮模板", font=font(16), fill="#f4c35e")
    for index, record in enumerate(records):
        x = gap+(index % columns)*(cw+gap)
        y = 112+(index // columns)*(ch+gap)
        draw.rounded_rectangle((x, y, x+cw, y+ch), radius=8, fill="#203040", outline="#3d5269")
        draw.text((x+12, y+8), f"{record['id']}  {record['label']}", font=font(18), fill="white")
        crop = Image.open(PLAN / record["image"]).convert("RGB")
        scale = min(3, (cw-32)/crop.width, 75/crop.height)
        enlarged = crop.resize((round(crop.width*scale), round(crop.height*scale)), Image.Resampling.NEAREST)
        canvas.paste(enlarged, (x+(cw-enlarged.width)//2, y+42+(75-enlarged.height)//2))
        w, h = crop.size
        draw.text((x+12, y+119), f"{w}×{h} px · {record['role']}", font=font(14), fill="#9cc4e6")
        draw.text((x+12, y+143), record["note"], font=font(14), fill="#c9d4df")
    canvas.save(path)


def build(source_dir, preview_dir):
    preview_dir = preview_dir.resolve()
    if not preview_dir.is_relative_to(ROOT) or preview_dir == ROOT:
        raise ValueError("Preview directory must be a dedicated repo-local directory")
    output = PLAN / "templates/reforming_center/raw_workshop"
    output.mkdir(parents=True, exist_ok=True)
    preview_dir.mkdir(parents=True, exist_ok=True)
    frames, provenance = {}, {}
    for name, identifier in SOURCES.items():
        source = source_dir / f"codex-clipboard-{identifier}.png"
        image = Image.open(source).convert("RGB")
        if image.size != (1332, 802):
            raise ValueError(f"Unexpected source dimensions: {source}: {image.size}")
        frames[name] = image.crop(CLIENT)
        provenance[name] = {"source_id": identifier, "filename": source.name,
                            "sha256": hashlib.sha256(source.read_bytes()).hexdigest(),
                            "dimensions": list(image.size), "client_crop_xyxy": list(CLIENT)}
    records = []
    for identifier, name, label, source, box, role, note in ANNOTATIONS:
        x, y, w, h = box
        if not (0 <= x < x+w <= 1280 and 0 <= y < y+h <= 720):
            raise ValueError(f"Invalid crop: {name}: {box}")
        crop = frames[source].crop((x, y, x+w, y+h))
        destination = output / f"{name}.png"
        crop.save(destination)
        records.append({"id": identifier, "name": name, "label": label,
                        "image": destination.relative_to(PLAN).as_posix(),
                        "source": source, "source_id": SOURCES[source],
                        "client_bbox_xywh": list(box),
                        "screen_bbox_xywh": [x+25, y+56, w, h],
                        "role": role, "note": note,
                        "sha256": hashlib.sha256(destination.read_bytes()).hexdigest()})
    metadata = {"schema_version": 1, "status": "offline_validated_pending_live",
                "reference_client": [1280, 720], "sources": provenance,
                "method": "Manual pixel boxes cropped from user screenshots; no OCR; no generated template glyphs",
                "color_contract": "PNG preserves native RGB; OpenCV loading yields BGR; runtime captures must convert RGB to BGR once",
                "matching_tested": False, "click_points": CLICK_POINTS,
                "missing_source_states": MISSING, "assets": records}
    catalog = PLAN / "data/meta/raw_workshop_templates_pc.json"
    catalog.write_text(json.dumps(metadata, ensure_ascii=False, indent=2)+"\n", encoding="utf-8")
    sheets = []
    for prefix, title in [("A", "原料车间候选模板 A：预存与生产"), ("B", "原料车间候选模板 B：名单与人数")]:
        path = preview_dir / f"templates_{prefix.lower()}.png"
        review_sheet([item for item in records if item["id"].startswith(prefix)], title, path)
        sheets.append(str(path))
    # Source annotations preserve the real 1280x720 frame and exact crop boxes.
    for source, frame in frames.items():
        annotated = frame.copy()
        draw = ImageDraw.Draw(annotated)
        for item in records:
            if item["source"] != source:
                continue
            x, y, w, h = item["client_bbox_xywh"]
            draw.rectangle((x, y, x+w-1, y+h-1), outline="#ff4d83", width=2)
            draw.rectangle((x, max(0, y-22), x+42, y), fill="#101923")
            draw.text((x+2, max(0, y-22)), item["id"], font=font(14), fill="#ffffff")
        annotated.save(preview_dir / f"source_{source}.png")
    return {"asset_count": len(records), "catalog": str(catalog), "preview_sheets": sheets,
            "source_annotation_dir": str(preview_dir), "matching_tested": False,
            "missing_source_states": MISSING}


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--source-dir", type=Path, required=True)
    parser.add_argument("--preview-dir", type=Path, required=True)
    args = parser.parse_args()
    print(json.dumps(build(args.source_dir, args.preview_dir), ensure_ascii=False, indent=2))
