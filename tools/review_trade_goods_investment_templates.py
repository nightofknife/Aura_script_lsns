"""Evaluate native-generated investment templates against real still screenshots."""

from __future__ import annotations

import argparse
import json
from pathlib import Path

import cv2
import numpy as np
from PIL import Image, ImageDraw

from build_trade_goods_investment_templates import bright_mask, crop, dark_mask, digit_token, tight_token, yellow_mask


ROOT = Path(__file__).resolve().parents[1]
TEMPLATES = ROOT / "plans/resonance_pc/templates/trade_goods_investment"
FIXTURES = ROOT / "tests/fixtures/trade_goods_investment"


def prepare(image, mode):
    if mode == "bright":
        return bright_mask(image)
    if mode == "bright_blur":
        return cv2.GaussianBlur(bright_mask(image), (3, 3), 0)
    if mode == "yellow":
        return yellow_mask(image)
    if mode == "dark":
        return dark_mask(image)
    if mode == "dark_blur":
        return cv2.GaussianBlur(dark_mask(image), (3, 3), 0)
    if mode == "rgb_blur":
        return cv2.GaussianBlur(image, (3, 3), 0)
    return image


def match(source, template):
    if any(a < b for a, b in zip(source.shape[:2], template.shape[:2])):
        return {"score": 0., "point": [0, 0]}
    response = cv2.matchTemplate(source, template, cv2.TM_CCOEFF_NORMED)
    response = np.nan_to_num(response, nan=-1, posinf=-1, neginf=-1)
    _, score, _, point = cv2.minMaxLoc(response)
    return {"score": float(score), "point": list(point)}


def rank_level(mask, templates):
    observed, foreground = tight_token(mask)
    padded = np.pad(observed, 2)
    ranked = {}
    for meta, pixels in templates:
        result = match(padded, pixels)
        level = meta["level"]
        if level not in ranked or result["score"] > ranked[level]["score"]:
            ranked[level] = {"level": level, "file": meta["file"], **result}
    order = sorted(ranked.values(), key=lambda value: value["score"], reverse=True)
    return order, observed, foreground


def read_digits(mask, templates):
    columns = np.flatnonzero(mask.any(axis=0))
    runs = np.split(columns, np.flatnonzero(np.diff(columns) > 1) + 1)
    if len(runs) not in (4, 5):
        return None, []
    values, details = [], []
    for run in runs[3:]:
        observed = digit_token(mask[:, run[0]:run[-1] + 1])
        candidates = {}
        for meta, template in templates:
            score = match(np.pad(observed, 1), template)["score"]
            candidates[meta["digit"]] = max(candidates.get(meta["digit"], -1), score)
        ranked = sorted(candidates.items(), key=lambda row: row[1], reverse=True)
        digit, score = ranked[0]
        values.append(str(digit))
        details.append({"digit": digit, "score": score, "margin": score - ranked[1][1], "ranked": ranked[:3]})
    return int("".join(values)), details


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output", type=Path, default=ROOT / ".pytest_tmp/trade_goods_investment_templates")
    args = parser.parse_args()
    args.output.mkdir(parents=True, exist_ok=True)
    manifest = json.loads((TEMPLATES / "manifest.json").read_text(encoding="utf-8"))
    samples = json.loads((FIXTURES / "samples.json").read_text(encoding="utf-8"))
    assets = {entry["name"]: (entry, np.array(Image.open(TEMPLATES / entry["file"]))) for entry in manifest["templates"]}
    levels = [(entry, np.array(Image.open(TEMPLATES / entry["file"]))) for entry in manifest["levels"]]
    digits = [(entry, np.array(Image.open(TEMPLATES / entry["file"]))) for entry in manifest["digits"]]
    assertions, measurements, level_rows = [], [], []
    all_frames = {}

    def check(label, expected, actual, **detail):
        assertions.append({"label": label, "expected": expected, "actual": actual,
                           "passed": expected == actual, **detail})

    for sample in samples:
        sid = sample["id"]
        source = np.array(Image.open(FIXTURES / sample["file"]).convert("RGB"))
        all_frames[sid] = source
        annotation = Image.fromarray(source)
        draw = ImageDraw.Draw(annotation)
        investment = sample["expected"]["page"] == "investment"
        for name in ("entry", "page_anchor", "plus", "minus", "confirm_enabled", "success"):
            meta, template = assets[name]
            roi = meta["roi"]
            result = match(prepare(crop(source, roi), meta["preprocess"]), template)
            expected = (sid == "shop" if name == "entry" else sid == "success" if name == "success" else investment)
            detected = result["score"] >= meta["threshold"]
            check(f"{sid}/{name}", expected, detected, score=result["score"], threshold=meta["threshold"])
            measurements.append({"sample": sid, "template": name, **result})
            if detected:
                x, y = roi[0] + result["point"][0], roi[1] + result["point"][1]
                width, height = template.shape[1], template.shape[0]
                draw.rectangle((x, y, x + width, y + height), outline="#39ee9d", width=2)
                draw.text((x, max(0, y - 13)), f"{name} {result['score']:.3f}", fill="#39ee9d")
        if investment:
            for side in ("current", "preview"):
                roi = manifest["level_rois"][side]
                raw = crop(source, roi)
                mask = bright_mask(raw)
                ranked, observed, foreground = rank_level(mask, levels)
                best, second = ranked[:2]
                margin = best["score"] - second["score"]
                digit_value, digit_details = read_digits(mask, digits)
                digit_ok = digit_value == best["level"] and all(d["score"] >= manifest["level_matching"]["digit_threshold"] and d["margin"] >= manifest["level_matching"]["digit_margin"] for d in digit_details)
                accepted = best["score"] >= manifest["level_matching"]["threshold"] and margin >= manifest["level_matching"]["minimum_margin"] and digit_ok
                expected = sample["expected"][side]
                check(f"{sid}/{side}_level", expected, best["level"] if accepted else None,
                      score=best["score"], margin=margin, ranked=ranked[:5], digits=digit_details)
                level_rows.append({"sample": sid, "side": side, "expected": expected, "observed": observed,
                                   "raw": raw, "template": np.array(Image.open(TEMPLATES / best["file"])),
                                   "best": best, "margin": margin, "accepted": accepted})
                draw.rectangle((roi[0], roi[1], roi[0] + roi[2], roi[1] + roi[3]), outline="#56cfff", width=2)
                draw.text((roi[0], roi[1] - 14), f"{side} LV.{best['level']} {best['score']:.3f}", fill="#56cfff")

            locked = set(range(1, 9)) if sid not in ("shell", "double_digit") else set(range(2, 9))
            selected = []
            for index, (x, y) in enumerate(manifest["slot_origins"]):
                # A success banner hides middle-row lock glyphs: those cells are unknown.
                if not (sid == "success" and 3 <= index <= 5):
                    meta, template = assets["lock"]
                    roi = [x + 37, y + 38, 42, 50]
                    result = match(prepare(crop(source, roi), meta["preprocess"]), template)
                    detected = result["score"] >= meta["threshold"]
                    check(f"{sid}/slot_{index}/locked", index in locked, detected, score=result["score"])
                    if detected:
                        px, py = x + 37 + result["point"][0], y + 38 + result["point"][1]
                        draw.rectangle((px, py, px + template.shape[1], py + template.shape[0]), outline="#ff6659", width=2)
                meta, template = assets["selected_corner"]
                result = match(yellow_mask(crop(source, [x - 3, y, 28, 32])), template)
                detected = result["score"] >= meta["threshold"]
                check(f"{sid}/slot_{index}/selected", index == sample["expected"]["selected"], detected, score=result["score"])
                if detected:
                    selected.append(index)
                    draw.rectangle((x, y, x + 115, y + 129), outline="#ffe05a", width=3)
            check(f"{sid}/unique_selection", [sample["expected"]["selected"]], selected)
        annotation.save(args.output / f"{sid}_annotated.png")

    sheet = Image.new("RGB", (940, 75 + 120 * len(level_rows)), "#181c22")
    draw = ImageDraw.Draw(sheet)
    draw.text((15, 12), "REAL SCREENSHOT       NORMALIZED TOKEN       NATIVE GAME FONT       RESULT", fill="white")
    for i, row in enumerate(level_rows):
        y = 52 + i * 120
        draw.text((15, y), f"{row['sample']} / {row['side']}", fill="white")
        sheet.paste(Image.fromarray(row["raw"]).resize((162, 96)), (15, y + 16))
        for x, name in [(220, "observed"), (420, "template")]:
            sheet.paste(Image.fromarray(row[name]).convert("RGB").resize((168, 72)), (x, y + 16))
        draw.text((625, y + 22), f"expected {row['expected']} -> {row['best']['level']} / accepted={row['accepted']}", fill="white")
        draw.text((625, y + 43), f"NCC {row['best']['score']:.4f}; margin {row['margin']:.4f}", fill="#ffe05a")
    sheet.save(args.output / "levels_actual_vs_native.png")

    controls = [("entry", "shop"), ("page_title", "initial"), ("confirm_enabled", "initial"), ("lock", "initial")]
    comparison = Image.new("RGB", (1100, 560), "#21252c")
    draw = ImageDraw.Draw(comparison)
    for index, (name, sid) in enumerate(controls):
        meta, pixels = assets[name]
        raw = crop(all_frames[sid], meta["roi"])
        draw.text((10, index * 140), f"{name}: REAL ROI (left) / NATIVE COMPOSITE (right)", fill="white")
        comparison.paste(Image.fromarray(raw), (10, index * 140 + 25))
        comparison.paste(Image.fromarray(pixels).convert("RGB"), (620, index * 140 + 25))
    comparison.save(args.output / "components_actual_vs_native.png")

    gallery = Image.new("RGB", (1120, 680), "#181c22")
    draw = ImageDraw.Draw(gallery)
    draw.text((16, 14), "NATIVE GAME ASSETS ONLY / LV.0-20 / BEBAS___ / NO OCR / NO SCREENSHOT TEMPLATES", fill="white")
    for lv in range(21):
        entry = next(meta for meta in manifest["levels"] if meta["level"] == lv and meta["render_variant"] == "client")
        pixels = Image.open(TEMPLATES / entry["file"]).convert("RGB").resize((126, 54))
        gallery.paste(pixels, (16 + (lv % 7) * 158, 50 + (lv // 7) * 90))
    for i, name in enumerate(("entry", "page_anchor", "lock", "selected_corner", "plus", "minus", "success")):
        meta, pixels = assets[name]
        x, y = 16 + (i % 4) * 275, 340 + (i // 4) * 130
        draw.text((x, y), name, fill="#aab8c6")
        image = Image.fromarray(pixels).convert("RGB")
        factor = min(2, 250 / image.width, 90 / image.height)
        gallery.paste(image.resize((round(image.width * factor), round(image.height * factor))), (x, y + 22))
    gallery.paste(Image.fromarray(assets["confirm_enabled"][1]), (16, 610))
    draw.text((400, 622), "Confirm = native sprite + native font + native UI scale", fill="#aab8c6")
    gallery.save(args.output / "native_template_sheet.png")

    report = {"source": "native_game_assets_only", "scope": "six real supplied still screenshots",
              "total": len(assertions), "passed": sum(row["passed"] for row in assertions),
              "failed": [row for row in assertions if not row["passed"]], "checks": assertions,
              "measurements": measurements, "live_verified": False,
              "limitations": manifest["limitations"]}
    (args.output / "report.json").write_text(json.dumps(report, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    print(json.dumps({"passed": report["passed"], "total": report["total"],
                      "failed": [{k: row[k] for k in ("label", "expected", "actual", "score") if k in row} for row in report["failed"]]}))
    if report["failed"]:
        raise SystemExit(1)


if __name__ == "__main__":
    main()
