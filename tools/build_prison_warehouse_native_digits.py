"""Reconstruct native warehouse digits and report bounded offline calibration.

The installed dynamic Font has no serialized glyph atlas. This tool renders its
TTF at native size 28, models the UI outline, then downsamples. Screenshots tune
render parameters and measure results; screenshot glyph pixels never enter the
rendering function. No game actions, dependency changes or project tests.
"""

from __future__ import annotations

import argparse
import hashlib
import itertools
import json
import os
from pathlib import Path
import sys

sys.dont_write_bytecode = True

ROOT = Path(__file__).resolve().parents[1]
PLAN = ROOT / "plans/resonance_pc"
OUTPUT = PLAN / "templates/workshop/warehouse/native_digits"
CATALOG = PLAN / "data/meta/prison_warehouse_native_digits_pc.json"
DEFAULT_SCOPE = ROOT / ".pytest_tmp/prison_warehouse_native_ocr_20261023/native"
FONT_ID = -4884400286118422667
PREFAB = "ui/prison/prisonwarehouse/prisonwarehouse.asset"
NUMBER_NODE = "/PrisonWarehouse/Group_ProductWarehouse/Group_ProductWarehouse/ScrollGrid_Warehouse/Viewport/Grid/Group_Item_000/Group_Item/Txt_Num"


def sha256(path):
    return hashlib.sha256(path.read_bytes()).hexdigest()


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--game-data", type=Path, default=Path("D:/software/soli/Resonance/雷索纳斯_Data"))
    parser.add_argument("--unitypy-dir", type=Path, default=ROOT / ".analysis_tmp/prison_warehouse_transfer_20261023/pythonlibs")
    parser.add_argument("--scope", type=Path, default=DEFAULT_SCOPE)
    args = parser.parse_args()
    if Path.cwd().resolve() != ROOT:
        parser.error("Run from the repository root")
    scope = args.scope.resolve()
    if scope == ROOT / ".pytest_tmp" or not scope.is_relative_to(ROOT / ".pytest_tmp"):
        parser.error("Use a dedicated scope directory under this repository's .pytest_tmp")
    scope.mkdir(parents=True, exist_ok=True)
    for key in ("TEMP", "TMP", "TMPDIR"):
        os.environ[key] = str(scope)
    sys.path[:0] = [str(args.unitypy_dir.resolve()), str(PLAN / "src/actions")]
    import cv2
    import numpy as np
    from PIL import Image, ImageDraw, ImageFilter, ImageFont
    from _trade_investment_native import inspect_prefab, load_bundle
    from _prison_warehouse_native_quantity import NativeWarehouseQuantityReader, native_digit_scores, native_response, native_white_bar

    metadata = args.game_data / "il2cpp_data/Metadata/global-metadata.dat"
    bundle_path = args.game_data / "Patch/Asset/ui/font.asset"
    env = load_bundle(bundle_path, metadata)
    font_object = next(obj for obj in env.objects if obj.type.name == "Font" and obj.path_id == FONT_ID)
    font_tree = font_object.read_typetree()
    font_bytes = bytes(font_object.read().m_FontData)
    font_path = scope / f"{FONT_ID}.ttf"
    font_path.write_bytes(font_bytes)
    texture_id = font_tree["m_Texture"]["m_PathID"]
    texture = next(obj.read_typetree() for obj in env.objects if obj.path_id == texture_id)
    prefab_path = args.game_data / "Patch/Asset" / PREFAB
    prefab = load_bundle(prefab_path, metadata)
    nodes = inspect_prefab(prefab)["nodes"]
    number = next(node for node in nodes if node["path"] == NUMBER_NODE)
    text_tree = next(component["tree"] for component in number["components"] if "m_FontData" in component["tree"])
    outline = next(component["tree"] for component in number["components"] if "m_EffectColor" in component["tree"])
    if text_tree["m_FontData"]["m_Font"]["m_PathID"] != FONT_ID:
        raise ValueError("Warehouse quantity binding no longer references the expected native Font")
    source = {
        "font_bundle": "ui/font.asset", "font_bundle_sha256": sha256(bundle_path),
        "font_path_id": FONT_ID, "font_name": font_tree["m_Name"],
        "font_sha256": hashlib.sha256(font_bytes).hexdigest(), "font_data_bytes": len(font_bytes),
        "serialized_character_rect_count": len(font_tree.get("m_CharacterRects", [])),
        "font_texture_path_id": texture_id, "font_texture_size": [texture["m_Width"], texture["m_Height"]],
        "font_texture_complete_image_size": texture["m_CompleteImageSize"],
        "prefab": PREFAB, "prefab_sha256": sha256(prefab_path), "number_node": NUMBER_NODE,
        "native_font_data": text_tree["m_FontData"], "native_text_color": text_tree["m_Color"],
        "native_outline": {key: outline[key] for key in ("m_EffectColor", "m_EffectDistance", "m_UseGraphicAlpha")},
        "scale_chain": [0.7, 2 / 3],
        "pixel_origin": "Native TTF rendered with Pillow/FreeType; not a Unity glyph atlas and not screenshot crops.",
    }
    (scope / "native_source.json").write_text(json.dumps(source, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    font = ImageFont.truetype(str(font_path), text_tree["m_FontData"]["m_FontSize"])
    base = {}
    for digit in "0123456789":
        image = Image.new("L", (80, 80))
        ImageDraw.Draw(image).text((16, 8), digit, font=font, fill=255)
        base[digit] = image

    def render(digit, style):
        fill_image = base[digit]
        if style["synthetic_bold_pixels"]:
            fill_image = fill_image.filter(ImageFilter.MaxFilter(3))
        alpha = np.asarray(fill_image, np.float32) / 255
        shadow = np.zeros_like(alpha)
        opacity = outline["m_EffectColor"]["a"]
        distance_x, distance_y = (round(outline["m_EffectDistance"][axis]) for axis in ("x", "y"))
        for dx, dy in ((-distance_x, -distance_y), (-distance_x, distance_y), (distance_x, -distance_y), (distance_x, distance_y)):
            shifted = np.roll(np.roll(alpha, dx, axis=1), dy, axis=0) * opacity
            shadow = 1 - (1 - shadow) * (1 - shifted)
        x0, y0, x1, y1 = fill_image.getbbox()
        scale = style["scale"]
        tx = 11 + style["phase_x"] - (x1 - x0) * scale
        ty = 13 + style["phase_y"] - (y1 - y0) * scale
        coefficients = (1 / scale, 0, x0 - tx / scale, 0, 1 / scale, y0 - ty / scale)
        resample = Image.Resampling.BILINEAR if style["resample"] == "bilinear" else Image.Resampling.BICUBIC
        fill = np.asarray(fill_image.transform((13, 17), Image.Transform.AFFINE, coefficients, resample=resample), np.float32) / 255
        shadow = np.asarray(Image.fromarray(np.uint8(shadow * 255)).transform((13, 17), Image.Transform.AFFINE, coefficients, resample=resample), np.float32) / 255
        # A neutral reference card models the stripe; no source screenshot pixels.
        card = np.full((84, 84, 3), 175, np.uint8)
        card[78:82] = 255
        background = card[65:82, 70:83, 0].astype(np.float32)
        predicted = 255 * fill + background * (1 - fill) * (1 - shadow)
        card[65:82, 70:83] = np.uint8(np.clip(predicted, 0, 255))[:, :, None]
        return {"response": native_response(card)[65:82, 70:83], "mask": fill + shadow > .05, "fill": fill, "outline": shadow}

    fixture = PLAN / "development/template_sources/warehouse_transfer/quantity_samples.json"
    sample_definitions = json.loads(fixture.read_text(encoding="utf-8"))["samples"]
    frames, captures = {}, []
    for name in ("warehouse_before", "warehouse_connected", "warehouse_after"):
        path = fixture.parent / "screenshots" / f"{name}.png"
        with Image.open(path) as original:
            if original.size != (1282, 752):
                raise ValueError(f"Unexpected source dimensions: {name} {original.size}")
            frames[name] = np.asarray(original.convert("RGB").crop((1, 31, 1281, 751)))
        captures.append({"id": name, "path": path.relative_to(PLAN).as_posix(), "sha256": sha256(path)})
    samples = []
    for spec in sample_definitions:
        x, y, width, height = spec["bbox"]
        card = frames[spec["frame"]][y:y + height, x:x + width].copy()
        samples.append({**spec, "card": card, "response": native_response(card), "bar": native_white_bar(card)})

    def known_length(sample, bank):
        rows = []
        for position, truth in enumerate(reversed(sample["expected"])):
            ranked = native_digit_scores(sample["response"], sample["bar"], position, bank)
            rows.append({"from_right": position, "truth": truth, "ranked": ranked,
                         "correct": ranked[0]["digit"] == truth})
        return rows

    profiles = []
    for scale, bold, px, py, resample in itertools.product((.7 * 2 / 3, .48, .5), (0, 1), (-.33, 0, .33), (-.33, 0, .33), ("bilinear", "bicubic")):
        style = {"scale": scale, "synthetic_bold_pixels": bold, "phase_x": px, "phase_y": py, "resample": resample}
        bank = {digit: render(digit, style) for digit in "0123456789"}
        classifications = [known_length(sample, bank) for sample in samples if sample["bar"] is not None]
        correct = sum(all(row["correct"] for row in rows) for rows in classifications)
        truth_scores = [next(row["score"] for row in glyph["ranked"] if row["digit"] == glyph["truth"]) for rows in classifications for glyph in rows]
        profiles.append({"style": style, "correct_known_length_cards": correct, "evaluated_cards": len(classifications), "mean_truth_score": float(np.mean(truth_scores))})
    profiles.sort(key=lambda row: (row["correct_known_length_cards"], row["mean_truth_score"]), reverse=True)
    (scope / "native_sweep_profiles.json").write_text(json.dumps(profiles, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    selected = profiles[0]["style"]
    bank = {digit: render(digit, selected) for digit in "0123456789"}
    OUTPUT.mkdir(parents=True, exist_ok=True)
    templates = []
    for digit, item in bank.items():
        prefix = OUTPUT / f"digit_{digit}"
        fill, shadow = item["fill"], item["outline"]
        combined = fill + shadow * (1 - fill)
        intensity = np.divide(fill, combined, out=np.zeros_like(fill), where=combined > 0)
        rgba = np.dstack((intensity, intensity, intensity, combined))
        image_path = prefix.with_suffix(".png")
        fill_path, outline_path = OUTPUT / f"digit_{digit}.fill.png", OUTPUT / f"digit_{digit}.outline.png"
        mask_path, response_path = OUTPUT / f"digit_{digit}.mask.png", OUTPUT / f"digit_{digit}.response.npy"
        Image.fromarray(np.uint8(np.clip(rgba * 255, 0, 255)), "RGBA").save(image_path)
        Image.fromarray(np.uint8(fill * 255)).save(fill_path)
        Image.fromarray(np.uint8(shadow * 255)).save(outline_path)
        Image.fromarray(np.uint8(item["mask"]) * 255).save(mask_path)
        np.save(response_path, item["response"].astype(np.float32), allow_pickle=False)
        templates.append({"digit": digit, "image": image_path.relative_to(PLAN).as_posix(), "fill": fill_path.relative_to(PLAN).as_posix(), "outline": outline_path.relative_to(PLAN).as_posix(), "mask": mask_path.relative_to(PLAN).as_posix(), "response": response_path.relative_to(PLAN).as_posix(), "sha256": {"image": sha256(image_path), "fill": sha256(fill_path), "outline": sha256(outline_path), "mask": sha256(mask_path), "response": sha256(response_path)}, "kind": "native_ttf_render_reconstruction", "observed_in_supplied_captures": digit in "01234567"})
    catalog = {
        "schema_version": 1, "experimental": True, "offline_only": True, "auto_click_ready": False,
        "reference_client": [1280, 720], "card_size": [84, 84], "input_order": "RGB",
        "source": source, "selected_style": selected, "templates": templates,
        "sample_fixture": {"path": fixture.relative_to(PLAN).as_posix(), "sha256": sha256(fixture)},
        "reader": {"max_digits": 4, "digit_advance": 9, "minimum_score": .60, "minimum_margin": .10, "absence_score_ceiling": .45},
        "calibration": {"search_profiles": len(profiles), "selection_metric": "Known-length classification followed by mean true-glyph score on all supplied active samples", "screenshot_pixels_used_for_template_rendering": False},
        "limits": ["This is FreeType reconstruction from native TTF, not the runtime Unity atlas or screenshot glyph crops.", "All supplied active samples participated in parameter selection; results are calibration, not held-out accuracy.", "Known-length diagnostics use hand labels to define glyph count and are separate from unknown-length reader outcomes.", "Digit 8/9 templates exist but no supplied capture contains either; recognition coverage for them remains unmeasured.", "Native 8/9 can still resemble a known digit; rejecting an 8/9 top candidate is not a guarantee against their misclassification.", "Bold is requested by the native Text and the TTF face is already Bold; extra synthetic emboldening was only a compared model.", "Only 20 is a supplied multi-digit stack; arbitrary 1-4 digit decoding is not a measured coverage claim.", "No capture, click, task or automatic loading integration exists."],
    }
    CATALOG.write_text(json.dumps(catalog, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    reader = NativeWarehouseQuantityReader(CATALOG)
    results = []
    for sample in samples:
        diagnostic = known_length(sample, bank) if sample["bar"] is not None else []
        value = reader.read_card(sample["card"])
        results.append({**{key: sample[key] for key in ("frame", "id", "bbox", "expected", "state")},
                        "known_length_diagnostic": {"glyphs": diagnostic, "read": "".join(row["ranked"][0]["digit"] for row in reversed(diagnostic)) if diagnostic else None},
                        "unknown_length_result": value, "correct": value["value"] is not None and str(value["value"]) == sample["expected"]})
    base_card = next(sample["card"] for sample in samples if sample["frame"] == "warehouse_connected" and sample["id"] == "product")
    bar = native_white_bar(base_card)
    plain = np.full((84, 84, 3), 155, np.uint8)
    plain[78:82] = 255
    gradient = np.tile(np.linspace(110, 210, 84, dtype=np.uint8)[:, None, None], (1, 84, 3))
    gradient[78:82] = 255
    negatives = [("white_bar_only", plain), ("white_bar_gradient", gradient)]
    for name, left, right in (("twenty_erase_both", 62, 83), ("twenty_drop_leading_digit", 62, 73), ("twenty_drop_trailing_digit", 73, 83), ("twenty_cut_leading_left_half", 64, 68), ("twenty_cut_trailing_right_half", 78, 83)):
        damage = np.zeros((84, 84), np.uint8)
        damage[bar - 12:bar + 3, left:right] = 255
        negatives.append((name, cv2.inpaint(base_card, damage, 3, cv2.INPAINT_TELEA)))
    negative_results = []
    for name, card in negatives:
        value = reader.read_card(card)
        Image.fromarray(card).resize((336, 336)).save(scope / f"negative_{name}.png")
        negative_results.append({"id": name, "expected": "unknown", "result": value, "rejected": value["value"] is None})
    summaries = {}
    for state in ("active", "dim_train_diagnostic"):
        subset = [row for row in results if row["state"] == state]
        summaries[state] = {"count": len(subset), "recognized_correct_unknown_length": sum(row["correct"] for row in subset), "unknown": sum(row["unknown_length_result"]["value"] is None for row in subset), "wrong_accepted": sum(row["unknown_length_result"]["value"] is not None and not row["correct"] for row in subset), "known_length_correct_diagnostic": sum(row["known_length_diagnostic"]["read"] == row["expected"] for row in subset)}
    summaries["synthetic_negatives"] = {"count": len(negative_results), "rejected": sum(row["rejected"] for row in negative_results)}
    report = {"summaries": summaries, "selected_style": selected, "search_profile_count": len(profiles), "results": results, "synthetic_negatives": negative_results, "capture_sources": captures, "limits": catalog["limits"]}
    (scope / "native_quantity_report.json").write_text(json.dumps(report, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    atlas = Image.new("RGB", (1000, 420), (45, 50, 58))
    draw = ImageDraw.Draw(atlas)
    for index, spec in enumerate(templates):
        x, y = index % 5 * 200 + 10, index // 5 * 210 + 10
        draw.text((x, y), f"native TTF {spec['digit']}", fill="white")
        with Image.open(PLAN / spec["image"]) as glyph:
            tile = Image.new("RGBA", glyph.size, (150, 150, 150, 255))
            tile.alpha_composite(glyph)
            atlas.paste(tile.convert("RGB").resize((78, 102)), (x, y + 25))
        with Image.open(PLAN / spec["mask"]) as mask:
            atlas.paste(mask.convert("RGB").resize((78, 102)), (x + 90, y + 25))
        draw.text((x, y + 140), "seen" if spec["observed_in_supplied_captures"] else "NO CAPTURE SAMPLE", fill=(220, 220, 220))
    atlas.save(scope / "native_digits_atlas.png")
    contact = Image.new("RGB", (1200, 13 * 112), (28, 34, 42))
    draw = ImageDraw.Draw(contact)
    for index, (sample, row) in enumerate(zip(samples, results)):
        x, y = index % 6 * 200, index // 6 * 112
        draw.text((x, y), f"{sample['frame'].replace('warehouse_', '')}:{sample['id']}", fill="white")
        draw.text((x, y + 12), f"expected={sample['expected']} native={row['unknown_length_result']['value']}", fill=(155, 230, 175) if row["correct"] else (255, 205, 90))
        contact.paste(Image.fromarray(sample["card"][58:84, 35:84]).resize((196, 78)), (x, y + 27))
    contact.save(scope / "native_samples_contact.png")
    lines = ["# Native TTF reconstruction: offline quantity calibration", "", "Reconstructed native font pixels, not a Unity atlas and not screenshot glyph crops. No game input.", "", "```json", json.dumps(summaries, indent=2), "```", "", "108 render profiles were selected on all supplied active captures. Known-length numbers are diagnostic only; the reader receives no expected length.", "", "| Capture | Sample | Expected | Unknown-length read | Known-length diagnostic | Reasons |", "|---|---|---|---|---|---|"]
    lines.extend(f"| {row['frame']} | {row['id']} | {row['expected']} | {row['unknown_length_result']['value']} | {row['known_length_diagnostic']['read']} | {', '.join(row['unknown_length_result']['reasons'])} |" for row in results)
    lines.extend(["", "| Synthetic negative | Read | Rejected |", "|---|---|---|"])
    lines.extend(f"| {row['id']} | {row['result']['value']} | {row['rejected']} |" for row in negative_results)
    lines.extend(["", "Limits:", "", *[f"- {limit}" for limit in catalog["limits"]], ""])
    (scope / "native_quantity_report.md").write_text("\n".join(lines), encoding="utf-8")
    print(json.dumps({"catalog": str(CATALOG), "report": str(scope / "native_quantity_report.json"), "selected_style": selected, "summaries": summaries}, ensure_ascii=False))


if __name__ == "__main__":
    main()
