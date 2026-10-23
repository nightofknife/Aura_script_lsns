"""Compare fixed quantity OCR regions on the 72 normal supplied warehouse cards.

This performs only the explicitly requested offline OCR calibration. Damaged and
synthetic cards are not loaded or generated. No game input or runtime integration.
"""
from __future__ import annotations

import argparse
import csv
from hashlib import sha256
import json
import math
import os
from pathlib import Path
import re
import sys
import tempfile
import time

ROOT = Path(__file__).resolve().parents[1]
PLAN = ROOT / "plans/resonance_pc"
SOURCE = PLAN / "development/template_sources/warehouse_transfer"
DEFAULT_SCOPE = ROOT / ".pytest_tmp/prison_warehouse_ocr_roi_20261023"
FOCUS = ("warehouse_after:train_0_3", "warehouse_connected:train_2_4",
         "warehouse_after:train_0_4", "warehouse_before:train_2_4", "warehouse_after:train_2_2")


def digest(path):
    return sha256(path.read_bytes()).hexdigest()


def candidates():
    output = [{"id": "baseline_x42_y60_b82", "roi": [42, 60, 42, 22], "kind": "baseline"}]
    for top in (63, 64, 65, 66):
        for bottom in (80, 81, 82):
            output.append({"id": f"x42_y{top}_b{bottom}", "roi": [42, top, 42, bottom-top],
                           "kind": "top_and_bottom"})
    for left in (40, 44):
        for top in (64, 65, 66):
            output.append({"id": f"x{left}_y{top}_b81", "roi": [left, top, 84-left, 81-top],
                           "kind": "horizontal_adjustment"})
    for top in (64, 65):
        output.append({"id": f"x41_y{top}_b81", "roi": [41, top, 43, 81-top],
                       "kind": "horizontal_adjustment"})
    for top in (64, 65, 66):
        output.append({"id": f"native75_y{top}_b81", "roi": [9, top, 75, 81-top],
                       "kind": "native_75px_horizontal_band"})
    for top in (64, 65, 66):
        output.append({"id": f"x35_y{top}_b81", "roi": [35, top, 49, 81-top],
                       "kind": "wider_four_digit_region"})
    for top in (63, 64, 65):
        output.append({"id": f"complete_native_x7_y{top}_b81", "roi": [7, top, 76, 81-top],
                       "kind": "complete_native_text_field"})
    output.append({"id": "complete_native_x7_y66_b81", "roi": [7, 66, 76, 15],
                   "kind": "complete_native_text_field_fixed_fallback"})
    return output


def configure(scope):
    if Path.cwd().resolve() != ROOT:
        raise ValueError("Run this tool from the repository root")
    scope = scope.resolve()
    temporary = (ROOT / ".pytest_tmp").resolve()
    if scope == temporary or not scope.is_relative_to(temporary):
        raise ValueError("--scope must stay in a dedicated repository .pytest_tmp subdirectory")
    scope.mkdir(parents=True, exist_ok=True)
    for key in ("TEMP", "TMP", "TMPDIR"):
        os.environ[key] = str(scope)
    os.environ["AURA_LOG_ROOT"] = str(scope / "logs")
    os.environ["PYTHONDONTWRITEBYTECODE"] = "1"
    tempfile.tempdir = str(scope)
    sys.dont_write_bytecode = True
    return scope


def parse(raw, expected):
    data = raw[0] if raw else {}
    texts = [str(value) for value in data.get("rec_texts", [])]
    scores = [float(value) for value in data.get("rec_scores", [])]
    text = texts[0] if len(texts) == 1 else None
    score = scores[0] if len(scores) == 1 else None
    accepted = bool(text is not None and re.fullmatch(r"[1-9][0-9]*", text)
                    and score is not None and math.isfinite(score) and .8 <= score <= 1)
    return {"texts": texts, "scores": scores, "text": text, "score": score,
            "raw_exact": text == expected, "accepted": accepted,
            "correct": accepted and text == expected,
            "wrong_accepted": accepted and text != expected,
            "rejected": not accepted}


def metrics(rows):
    import numpy as np

    times = [row["elapsed_ms"] for row in rows]
    return {"count": len(rows), "raw_exact": sum(row["result"]["raw_exact"] for row in rows),
            "correct": sum(row["result"]["correct"] for row in rows),
            "rejected": sum(row["result"]["rejected"] for row in rows),
            "wrong_accepted": sum(row["result"]["wrong_accepted"] for row in rows),
            "mean_ms": float(np.mean(times)), "p95_ms": float(np.percentile(times, 95))}


def fixed_fallback(results):
    """Route only on the primary OCR admission result, never on the hand label."""
    primary_id, secondary_id = "complete_native_x7_y65_b81", "complete_native_x7_y66_b81"
    primary = [row for row in results if row["candidate"] == primary_id]
    secondary = {row["key"]: row for row in results if row["candidate"] == secondary_id}
    rows = []
    for first in primary:
        use_fallback = not first["result"]["accepted"]
        chosen = secondary[first["key"]] if use_fallback else first
        rows.append({**chosen, "fallback_used": use_fallback,
                     "primary_result": first["result"],
                     "elapsed_ms": first["elapsed_ms"]+(chosen["elapsed_ms"] if use_fallback else 0)})
    return {"primary_roi": [7, 65, 76, 16], "fallback_roi": [7, 66, 76, 15],
            "route": "Use fallback only if the primary is not a strict positive integer or score < 0.80; no expected value is inspected.",
            "fallback_count": sum(row["fallback_used"] for row in rows),
            "all": metrics(rows), "active": metrics([row for row in rows if row["state"] == "active"]),
            "dim": metrics([row for row in rows if row["state"] == "dim_train_diagnostic"]), "rows": rows}


def verify_canonical_reader(samples, provenance, engine, service, raw_ctc, scope, provider):
    """Call the canonical crop/parser with genuine pipeline OCR responses."""
    from plans.aura_base.src.services.ocr_service import MultiOcrResult
    from plans.resonance_pc.src.actions._prison_warehouse_quantity import (
        DEFAULT_OCR_CATALOG, WarehouseQuantityReader,
    )

    class PipelineOcrAdapter:
        def __init__(self):
            self.calls = []

        def recognize_all(self, source_image, **kwargs):
            raw_ctc.clear()
            before = time.perf_counter()
            raw = engine.predict(source_image, **kwargs)
            parsed = service._parse_results(raw)
            self.calls.append({"input_size": [source_image.shape[1], source_image.shape[0]],
                               "kwargs": kwargs, "elapsed_ms": (time.perf_counter()-before)*1000,
                               "raw_ctc": list(raw_ctc),
                               "api_results": [{"text": item.text, "confidence": item.confidence} for item in parsed]})
            return MultiOcrResult(count=len(parsed), results=parsed)

    adapter = PipelineOcrAdapter()
    reader = WarehouseQuantityReader(adapter)
    rows = []
    for sample in samples:
        adapter.calls = []
        started = time.perf_counter()
        error = None
        try:
            result = reader.read_card(sample["card"])
        except Exception as exc:
            error = {"type": type(exc).__name__, "message": str(exc)}
            result = {"status": "error", "value": None}
        value = result.get("value")
        rows.append({key: entry for key, entry in sample.items() if key != "card"} | {
            "result": result, "ocr_calls": list(adapter.calls), "attempt_count": len(adapter.calls),
            "elapsed_ms": (time.perf_counter()-started)*1000,
            "correct": value is not None and str(value) == sample["expected"],
            "unknown": value is None, "wrong_read": value is not None and str(value) != sample["expected"],
            "error": error})
    groups = {}
    for state in ("active", "dim_train_diagnostic"):
        selected = [row for row in rows if row["state"] == state]
        groups[state] = {"count": len(selected), "correct": sum(row["correct"] for row in selected),
                         "unknown": sum(row["unknown"] for row in selected),
                         "wrong_read": sum(row["wrong_read"] for row in selected),
                         "fallback_used": sum(row["attempt_count"] > 1 for row in selected)}
    helper_path = PLAN / "src/actions/_prison_warehouse_quantity.py"
    report = {"scope": "canonical_reader_on_real_72_supplied_cards_only",
              "mock_ocr_or_expected_text_supplied_to_reader": False,
              "sources": provenance,
              "backend": {"model": "ppocrv6_small", "provider": provider,
                          "adapter": "engine.predict -> existing OcrService._parse_results -> MultiOcrResult"},
              "helper": {"path": helper_path.relative_to(ROOT).as_posix(), "sha256": digest(helper_path)},
              "catalog": {"path": DEFAULT_OCR_CATALOG.relative_to(ROOT).as_posix(), "sha256": digest(DEFAULT_OCR_CATALOG)},
              "summary": groups, "total_ocr_calls": sum(row["attempt_count"] for row in rows),
              "errors": sum(row["error"] is not None for row in rows), "results": rows}
    (scope / "quantity_ocr_reader_report.json").write_text(json.dumps(report, ensure_ascii=False, indent=2)+"\n", encoding="utf-8")
    lines = ["# Canonical quantity OCR reader: offline supplied cards", "",
             "The reader owns cropping, fallback and parsing. The adapter supplies genuine existing PP-OCRv6 predictions; no expected text is passed to OCR or reader.", "",
             "| Group | Correct | Unknown | Wrong | Fallback cards |", "|---|---:|---:|---:|---:|"]
    for state, summary in groups.items():
        lines.append(f"| {state} ({summary['count']}) | {summary['correct']} | {summary['unknown']} | {summary['wrong_read']} | {summary['fallback_used']} |")
    lines.extend(["", f"Total genuine OCR calls: {report['total_ocr_calls']}. Inference/reader errors: {report['errors']}.", ""])
    (scope / "quantity_ocr_reader_report.md").write_text("\n".join(lines), encoding="utf-8")
    print(json.dumps({"canonical_reader_summary": groups, "total_ocr_calls": report["total_ocr_calls"],
                      "errors": report["errors"], "report": str(scope / "quantity_ocr_reader_report.json")}))


def save_previews(samples, selected, baseline, scope):
    from PIL import Image, ImageDraw

    scope.mkdir(parents=True, exist_ok=True)
    contact = Image.new("RGB", (1200, 12*132), (29, 34, 42))
    draw = ImageDraw.Draw(contact)
    indexed = {row["key"]: row for row in selected["rows"]}
    for index, sample in enumerate(samples):
        px, py = index % 6*200, index // 6*132
        row = indexed[sample["key"]]
        result = row["result"]
        x, y, width, height = row["roi"]
        label = sample["key"].replace("warehouse_", "")
        draw.text((px+3, py+2), label, fill="white")
        score = f"{result['score']:.3f}" if result["score"] is not None else "unknown"
        draw.text((px+3, py+16), f"label={sample['expected']} OCR={result['text']} {score}",
                  fill=(150, 240, 170) if result["correct"] else (255, 175, 100))
        image = Image.fromarray(sample["card"])
        card = image.copy()
        ImageDraw.Draw(card).rectangle((x, y, x+width-1, y+height-1), outline=(255, 65, 45), width=1)
        contact.paste(card, (px+5, py+39))
        crop = image.crop((x, y, x+width, y+height))
        crop.thumbnail((96, 70))
        scale = min(96/crop.width, 70/crop.height)
        crop = crop.resize((round(crop.width*scale), round(crop.height*scale)), Image.Resampling.NEAREST)
        contact.paste(crop, (px+99, py+50))
    contact.save(scope / "recommended_roi_all_72_cards.png")

    focus = [sample for sample in samples if sample["key"] in FOCUS or sample["expected"] == "20"]
    focused = Image.new("RGB", (1200, len(focus)*112), (29, 34, 42))
    draw = ImageDraw.Draw(focused)
    by_baseline = {row["key"]: row for row in baseline["rows"]}
    for index, sample in enumerate(focus):
        py = index*112
        image = Image.fromarray(sample["card"])
        focused.paste(image, (8, py+15))
        draw.text((110, py+6), sample["key"]+f" label={sample['expected']}", fill="white")
        for column, candidate in enumerate((baseline, selected)):
            row = (by_baseline if column == 0 else indexed)[sample["key"]]
            bx, by, bw, bh = row["roi"]
            reading = row["result"]
            crop = image.crop((bx, by, bx+bw, by+bh)).resize((bw*3, bh*3), Image.Resampling.NEAREST)
            focused.paste(crop, (110+column*500, py+37))
            score = f"{reading['score']:.6f}" if reading["score"] is not None else "unknown"
            draw.text((122+column*500+bw*3, py+40), f"{row['candidate']}\ntext={reading['text']}\nscore={score}", fill="white")
    focused.save(scope / "recommended_roi_focus_and_three_20s.png")


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--scope", type=Path, default=DEFAULT_SCOPE)
    parser.add_argument("--reuse-report", action="store_true",
                        help="Reuse unchanged saved candidates, infer only newly added fixed ROIs")
    parser.add_argument("--verify-reader", action="store_true",
                        help="Call the canonical quantity reader on the 72 normal cards using genuine OCR inference")
    args = parser.parse_args()
    try:
        scope = configure(args.scope)
    except ValueError as exc:
        parser.error(str(exc))
    import cv2
    import numpy as np
    from PIL import Image
    sys.path.insert(0, str(ROOT))
    from plans.aura_base.src.services.ocr_service import OcrService
    from plans.aura_base.src.services.ocr_contract import load_ocr_metadata
    from plans.aura_base.src.services.ocr_onnx_pipeline import OnnxOcrPipeline

    class ProbeConfig:
        @staticmethod
        def get(key, default=None):
            return {"ocr.execution_provider": "cpu", "ocr.default_model": "ppocrv6_small",
                    "ocr.models_root": "models/ocr"}.get(key, default)

    labels_file = SOURCE / "quantity_samples.json"
    definitions = json.loads(labels_file.read_text(encoding="utf-8"))["samples"]
    frames, provenance = {}, []
    for name in ("warehouse_before", "warehouse_connected", "warehouse_after"):
        path = SOURCE / "screenshots" / f"{name}.png"
        with Image.open(path) as original:
            if original.size != (1282, 752):
                raise ValueError(f"Unexpected supplied image size: {name}")
            frames[name] = np.asarray(original.convert("RGB").crop((1, 31, 1281, 751)))
        provenance.append({"path": path.relative_to(ROOT).as_posix(), "sha256": digest(path)})
    samples = []
    for sample in definitions:
        x, y, width, height = sample["bbox"]
        card = frames[sample["frame"]][y:y+height, x:x+width].copy()
        if card.shape != (84, 84, 3):
            raise ValueError("Normal quantity fixture must be one complete 84x84 RGB card")
        samples.append({**sample, "key": f"{sample['frame']}:{sample['id']}", "card": card})

    before = time.perf_counter()
    service = OcrService(config=ProbeConfig())
    bundle, metadata_path = service._resolve_bundle_paths()
    metadata = load_ocr_metadata(metadata_path)
    sessions, provider = service._create_pipeline_sessions(metadata, bundle)
    engine = OnnxOcrPipeline(metadata=metadata, sessions=sessions, provider=provider)
    model_load_ms = (time.perf_counter()-before)*1000
    engine.predict(np.full((24, 48, 3), 155, np.uint8), recognition_only=True, use_textline_orientation=False)
    raw_ctc = []
    decoder = engine._ctc_decode

    def record(predictions):
        texts, scores = decoder(predictions)
        raw_ctc.extend({"text": str(text), "score": float(score)} for text, score in zip(texts, scores))
        return texts, scores

    engine._ctc_decode = record
    if args.verify_reader:
        verify_canonical_reader(samples, provenance, engine, service, raw_ctc, scope, provider)
        return
    results, summaries, errors = [], [], []
    reused_rows = 0
    if args.reuse_report:
        saved_path = scope / "quantity_ocr_roi_report.json"
        saved = json.loads(saved_path.read_text(encoding="utf-8"))
        if saved["sources"] != provenance or saved["labels"]["sha256"] != digest(labels_file):
            raise ValueError("Saved inference sources differ from current durable fixtures")
        if saved["backend"]["metadata_sha256"] != digest(metadata_path):
            raise ValueError("Saved model metadata differs from the existing model")
        (scope / f"saved_{len(saved['candidate_summaries'])}_roi_before_reuse.json").write_text(json.dumps(saved, ensure_ascii=False, indent=2)+"\n", encoding="utf-8")
        results, summaries, errors = saved["results"], saved["candidate_summaries"], saved["errors"]
        for row in results:
            prior = row["result"]
            row["result"] = parse([{"rec_texts": prior["texts"], "rec_scores": prior["scores"]}], row["expected"])
        for entry in summaries:
            entry_rows = [row for row in results if row["candidate"] == entry["id"]]
            entry["all"] = metrics(entry_rows)
            entry["active"] = metrics([row for row in entry_rows if row["state"] == "active"])
            entry["dim"] = metrics([row for row in entry_rows if row["state"] == "dim_train_diagnostic"])
            entry["focus"] = [{"key": row["key"], "expected": row["expected"], "result": row["result"]}
                              for row in entry_rows if row["key"] in FOCUS or row["expected"] == "20"]
        reused_rows = len(results)
    existing_ids = {entry["id"] for entry in summaries}
    fixed_candidates = candidates()
    for candidate in fixed_candidates:
        if candidate["id"] in existing_ids:
            continue
        rows = []
        x, y, width, height = candidate["roi"]
        if width < 40:
            raise ValueError("A quantity ROI must retain at least 40px for four glyphs and outline")
        for sample in samples:
            image = sample["card"][y:y+height, x:x+width]
            raw_ctc.clear()
            stamp = time.perf_counter()
            error = None
            try:
                raw = engine.predict(image, recognition_only=True, use_textline_orientation=False)
            except Exception as exc:
                error = {"type": type(exc).__name__, "message": str(exc)}
                errors.append({"candidate": candidate["id"], "sample": sample["key"], **error})
                raw = []
            elapsed_ms = (time.perf_counter()-stamp)*1000
            rows.append({key: value for key, value in sample.items() if key != "card"} | {
                "candidate": candidate["id"], "roi": candidate["roi"], "raw_ctc": list(raw_ctc),
                "result": parse(raw, sample["expected"]), "elapsed_ms": elapsed_ms, "error": error})
        entry = {**candidate, "all": metrics(rows),
                 "active": metrics([row for row in rows if row["state"] == "active"]),
                 "dim": metrics([row for row in rows if row["state"] == "dim_train_diagnostic"]),
                 "focus": [{"key": row["key"], "expected": row["expected"], "result": row["result"]}
                           for row in rows if row["key"] in FOCUS or row["expected"] == "20"]}
        results.extend(rows)
        summaries.append(entry)
        print(json.dumps({"candidate": candidate["id"], "active": entry["active"], "dim": entry["dim"]}), flush=True)
    # Choose one ROI from aggregate results only. Never select a crop per card.
    # The full native text field is mandatory for the final recommendation:
    # narrow four-digit crops remain diagnostics and cannot truncate long counts.
    eligible = [entry for entry in summaries if entry["kind"].startswith("complete_native_text_field")]
    ranked = sorted(eligible, key=lambda item: (item["all"]["wrong_accepted"], -item["active"]["correct"],
                                               -item["dim"]["correct"], -item["all"]["raw_exact"],
                                               item["roi"][3], abs(item["roi"][0]-42), item["roi"][2]))
    recommendation = next(entry for entry in summaries if entry["id"] == "complete_native_x7_y65_b81")
    fallback = fixed_fallback(results)
    report = {"scope": "normal_supplied_cards_only", "synthetic_damaged_samples_loaded": 0,
              "labels": {"path": labels_file.relative_to(ROOT).as_posix(), "sha256": digest(labels_file)},
              "sources": provenance,
              "backend": {"model": bundle.name, "provider": provider, "model_load_ms": model_load_ms,
                          "metadata_sha256": digest(metadata_path), "recognition_only": True,
                          "detector_used": False, "scale": 1, "preprocessing": "none", "input_order": "RGB"},
              "policy": {"score_threshold": .8, "quantity_regex": "[1-9][0-9]*",
                         "no_character_stripping": True, "candidate_count": len(fixed_candidates),
                         "minimum_diagnostic_width": 40, "recommended_text_field_width": 76,
                         "no_per_sample_crop_selection": True, "reused_inference_rows": reused_rows,
                         "global_ranking": "Restrict recommendation to complete native x7..83 text field; primary y65 selected globally before the one authorized y66 fallback comparison",
                         "timing": "One fixed-order diagnostic pass of pipeline.predict, excluding crop/model loading; not a performance benchmark."},
              "recommended": recommendation, "aggregate_best_single_roi": ranked[0]["roi"],
              "fixed_fallback": fallback, "candidate_summaries": summaries, "results": results, "errors": errors,
              "limits": ["All ROIs are globally applied to the same 72 hand-labelled normal cards.",
                         "Preprocessing/ROI selection uses these calibration screenshots, with no held-out or live-game claim.",
                         "Only 1..7 and 20 occur in real fixtures. Narrow diagnostic crops cannot establish long-number acceptance.",
                         "Final recommendation retains the complete 76px native text field; real four-to-nine digit accuracy is still not observed.",
                         "No synthetic damaged or white-bar negatives affect this comparison or recommendation."]}
    (scope / "quantity_ocr_roi_report.json").write_text(json.dumps(report, ensure_ascii=False, indent=2)+"\n", encoding="utf-8")
    with (scope / "global_roi_candidates.csv").open("w", encoding="utf-8-sig", newline="") as stream:
        writer = csv.writer(stream)
        writer.writerow(["candidate", "x", "y", "w", "h", "active_raw_exact", "active_admitted_correct", "active_rejected", "active_wrong", "dim_raw_exact", "dim_admitted_correct", "dim_rejected", "dim_wrong"])
        for entry in summaries:
            writer.writerow([entry["id"], *entry["roi"], entry["active"]["raw_exact"], entry["active"]["correct"], entry["active"]["rejected"], entry["active"]["wrong_accepted"], entry["dim"]["raw_exact"], entry["dim"]["correct"], entry["dim"]["rejected"], entry["dim"]["wrong_accepted"]])
    lines = ["# Normal warehouse quantity OCR ROI comparison", "", f"Recommended single global ROI: {recommendation['roi']} ({recommendation['id']}).",
             "", "Existing PP-OCRv6 small, CPU, recognition-only, raw 1x. Damaged/synthetic samples are excluded completely.",
             "", "| ROI x,y,w,h | Active raw exact / admitted / rejected / wrong | Dim raw exact / admitted / rejected / wrong |",
             "|---|---|---|"]
    for entry in summaries:
        active, dim = entry["active"], entry["dim"]
        lines.append(f"| {entry['roi']} | {active['raw_exact']} / {active['correct']} / {active['rejected']} / {active['wrong_accepted']} | {dim['raw_exact']} / {dim['correct']} / {dim['rejected']} / {dim['wrong_accepted']} |")
    lines.extend(["", "Recommended ROI focus samples (full strings, no stripping):", "",
                  "| Sample | Expected | Exact text | Score | Accepted correct |", "|---|---|---|---:|---|"])
    for row in recommendation["focus"]:
        result = row["result"]
        score = f"{result['score']:.6f}" if result["score"] is not None else "unknown"
        lines.append(f"| {row['key']} | {row['expected']} | {json.dumps(result['text'])} | {score} | {result['correct']} |")
    lines.extend(["", "Fixed global fallback, independent of expected labels:", "",
                  "Primary [7,65,76,16]; only non-positive/noninteger text or score < 0.80 triggers [7,66,76,15]. Both untrusted means unknown.",
                  "", f"Fallback used for {fallback['fallback_count']}/72 cards. Active: {fallback['active']['correct']}/47 correct, {fallback['active']['rejected']} rejected, {fallback['active']['wrong_accepted']} wrong. Dim: {fallback['dim']['correct']}/25 correct, {fallback['dim']['rejected']} rejected, {fallback['dim']['wrong_accepted']} wrong.",
                  "", "| Fallback sample | Primary exact text / score | Secondary exact text / score | Final correct |", "|---|---|---|---|"])
    for row in fallback["rows"]:
        if row["fallback_used"]:
            first, second = row["primary_result"], row["result"]
            lines.append(f"| {row['key']} | {json.dumps(first['text'])} / {first['score']} | {json.dumps(second['text'])} / {second['score']} | {second['correct']} |")
    lines.extend(["", *[f"- {item}" for item in report["limits"]], ""])
    (scope / "quantity_ocr_roi_report.md").write_text("\n".join(lines), encoding="utf-8")
    selected = {**recommendation, "rows": [row for row in results if row["candidate"] == recommendation["id"]]}
    baseline = {**summaries[0], "rows": [row for row in results if row["candidate"] == summaries[0]["id"]]}
    save_previews(samples, selected, baseline, scope)
    admitted = {**recommendation, "rows": fallback["rows"]}
    save_previews(samples, admitted, baseline, scope / "fixed_fallback_preview")
    print(json.dumps({"recommended_roi": recommendation["roi"], "active": recommendation["active"],
                      "dim": recommendation["dim"], "candidates": len(summaries), "inferences": len(results),
                      "errors": len(errors), "report": str(scope / "quantity_ocr_roi_report.json")}), flush=True)


if __name__ == "__main__":
    main()
