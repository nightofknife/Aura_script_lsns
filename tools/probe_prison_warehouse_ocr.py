"""Compare the existing PP-OCRv6 model on hand-labelled warehouse screenshots.

Offline image inference only: no game capture/input, production configuration,
dependency changes, or task integration. Run from the repository root.
"""
from __future__ import annotations

import argparse
from collections import Counter
from hashlib import sha256
import importlib.metadata
import json
import os
from pathlib import Path
import re
import sys
import tempfile
import time

ROOT = Path(__file__).resolve().parents[1]
PLAN = ROOT / "plans/resonance_pc"
SOURCES = PLAN / "development/template_sources/warehouse_transfer"
DEFAULT_SCOPE = ROOT / ".pytest_tmp/prison_warehouse_native_ocr_20261023/ocr"
QUANTITY_ROI = (42, 60, 42, 22)
VARIANTS = ("raw_1x", "raw_2x", "raw_3x", "raw_4x", "gray_3x",
            "inverted_gray_3x", "bar_removed_3x", "stroke_binary_3x")
ADMISSION_SCORE = .80


def digest(path):
    return sha256(path.read_bytes()).hexdigest()


def configure(scope):
    if Path.cwd().resolve() != ROOT:
        raise ValueError("Run from the repository root")
    scope = scope.resolve()
    temporary_root = (ROOT / ".pytest_tmp").resolve()
    if scope == temporary_root or not scope.is_relative_to(temporary_root):
        raise ValueError("--scope must be a dedicated repository .pytest_tmp subdirectory")
    scope.mkdir(parents=True, exist_ok=True)
    for key in ("TEMP", "TMP", "TMPDIR"):
        os.environ[key] = str(scope)
    os.environ["AURA_LOG_ROOT"] = str(scope / "logs")
    os.environ["PYTHONDONTWRITEBYTECODE"] = "1"
    sys.dont_write_bytecode = True
    tempfile.tempdir = str(scope)
    return scope


def read_fixtures(scope):
    import cv2
    import numpy as np
    from PIL import Image

    definitions = json.loads((SOURCES / "quantity_samples.json").read_text(encoding="utf-8"))["samples"]
    frames, provenance = {}, []
    for name in ("warehouse_before", "warehouse_connected", "warehouse_after"):
        path = SOURCES / "screenshots" / f"{name}.png"
        with Image.open(path) as original:
            if original.size != (1282, 752):
                raise ValueError(f"Unexpected screenshot size: {name}")
            frames[name] = np.asarray(original.convert("RGB").crop((1, 31, 1281, 751)))
        provenance.append({"path": path.relative_to(ROOT).as_posix(), "sha256": digest(path),
                           "client_crop_xyxy": [1, 31, 1281, 751]})
    samples = []
    for spec in definitions:
        x, y, width, height = spec["bbox"]
        card = frames[spec["frame"]][y:y+height, x:x+width].copy()
        if card.shape != (84, 84, 3):
            raise ValueError("Quantity sample must be an exact 84x84 card")
        x, y, width, height = QUANTITY_ROI
        samples.append({**spec, "kind": "quantity", "key": f"{spec['frame']}:{spec['id']}",
                        "card": card, "image": card[y:y+height, x:x+width].copy()})

    # Recreate all seven damaged/blank controls at their original 84px resolution;
    # the prior report's 336px presentation PNGs are never downsampled as evidence.
    base = next(item["card"] for item in samples
                if item["frame"] == "warehouse_connected" and item["id"] == "product")
    white_rows = (base.min(axis=2) >= 245).mean(axis=1) >= .75
    starts = []
    for y in range(68, 82):
        if white_rows[y] and (y == 68 or not white_rows[y-1]):
            end = y
            while end < 84 and white_rows[end]:
                end += 1
            if end-y >= 3:
                starts.append(y)
    bar = max(starts) if starts else None
    if bar is None:
        raise ValueError("Cannot recreate the annotated damage controls without a confirmed white stripe")
    negatives = []
    plain = np.full((84, 84, 3), 155, np.uint8)
    plain[78:82] = 255
    negatives.append(("white_bar_only", plain))
    gradient = np.tile(np.linspace(110, 210, 84, dtype=np.uint8)[:, None, None], (1, 84, 3))
    gradient[78:82] = 255
    negatives.append(("white_bar_gradient", gradient))
    for name, left, right in (("twenty_erase_both", 62, 83),
                              ("twenty_drop_leading_digit", 62, 73),
                              ("twenty_drop_trailing_digit", 73, 83),
                              ("twenty_cut_leading_left_half", 64, 68),
                              ("twenty_cut_trailing_right_half", 78, 83)):
        damage = np.zeros((84, 84), np.uint8)
        damage[bar-12:bar+3, left:right] = 255
        negatives.append((name, cv2.inpaint(base, damage, 3, cv2.INPAINT_TELEA)))
    for name, card in negatives:
        Image.fromarray(card).save(scope / f"negative_{name}_84.png")
        x, y, width, height = QUANTITY_ROI
        samples.append({"key": f"synthetic:{name}", "id": name, "kind": "negative",
                        "state": "synthetic_negative", "expected": None, "card": card,
                        "image": card[y:y+height, x:x+width].copy()})

    # Independent manually transcribed capacity labels; not mixed with stack accuracy.
    for frame, expected in (("warehouse_before", "74/1003"),
                            ("warehouse_connected", "74/1003"),
                            ("warehouse_after", "94/1003")):
        x, y, width, height = 414, 632, 85, 28
        samples.append({"key": f"{frame}:train_capacity", "id": "train_capacity",
                        "kind": "capacity", "state": "capacity_auxiliary", "expected": expected,
                        "bbox": [x, y, width, height], "image": frames[frame][y:y+height, x:x+width].copy()})
    return samples, provenance


def prepare(image, variant, card=None):
    import cv2
    import numpy as np

    output = image.copy()
    if variant in {"gray_3x", "inverted_gray_3x"}:
        gray = cv2.cvtColor(output, cv2.COLOR_RGB2GRAY)
        if variant == "inverted_gray_3x":
            gray = 255-gray
        output = cv2.cvtColor(gray, cv2.COLOR_GRAY2RGB)
    elif variant == "bar_removed_3x":
        # A fixed generic stripe removal, not chosen by the expected number.
        source = cv2.cvtColor(card if card is not None else output, cv2.COLOR_RGB2GRAY)
        white_level = max(120, float(np.percentile(source, 98))*.93)
        stripes = (source >= white_level).mean(axis=1) >= .75
        if card is not None:
            for local_y in range(output.shape[0]):
                if stripes[local_y+QUANTITY_ROI[1]]:
                    output[local_y] = np.median(output[:4], axis=(0, 1)).astype(np.uint8)
    elif variant == "stroke_binary_3x":
        # Keep bright, sharp ink; row-median subtraction suppresses the broad stripe.
        source = cv2.cvtColor(card if card is not None else output, cv2.COLOR_RGB2GRAY).astype(np.float32)
        response = source-cv2.GaussianBlur(source, (0, 0), 1.0)
        if card is not None:
            response -= np.median(response[:, 30:62], axis=1)[:, None]
            x, y, width, height = QUANTITY_ROI
            response = response[y:y+height, x:x+width]
        else:
            response -= np.median(response, axis=1)[:, None]
        ink = np.where(response > 12, 0, 255).astype(np.uint8)
        output = cv2.cvtColor(ink, cv2.COLOR_GRAY2RGB)
    scale = int(variant[-2])
    if scale != 1:
        output = cv2.resize(output, (output.shape[1]*scale, output.shape[0]*scale), interpolation=cv2.INTER_CUBIC)
    return output


def parsed_result(raw, expected, kind, score_threshold=ADMISSION_SCORE):
    data = raw[0] if raw else {}
    texts = [str(item) for item in data.get("rec_texts", [])]
    scores = [float(item) for item in data.get("rec_scores", [])]
    text = texts[0] if len(texts) == 1 else None
    score = scores[0] if len(scores) == 1 else None
    pattern = r"[0-9]+/[0-9]+" if kind == "capacity" else r"[1-9][0-9]{0,3}"
    accepted = bool(text is not None and score is not None and score >= score_threshold
                    and re.fullmatch(pattern, text))
    correct = accepted and text == expected if expected is not None else not accepted
    status = "correct" if correct else "wrong_accepted" if accepted else "rejected"
    return {"api_texts": texts, "api_scores": scores, "text": text, "score": score,
            "accepted": accepted, "correct": correct, "status": status,
            "raw_ascii_digits": bool(text is not None and re.fullmatch(r"[0-9]+", text)),
            "raw_zero_or_leading_zero": bool(text and re.fullmatch(r"[0-9]+", text) and text.startswith("0")),
            "raw_high_confidence_mismatch": bool(text is not None and score is not None and score >= ADMISSION_SCORE
                                                   and (expected is None or text != expected)),
            "raw_exact_match": text == expected if expected is not None else not texts}


def summarize(rows):
    import numpy as np

    output = {}
    for key in sorted({(item["mode"], item["variant"]) for item in rows}):
        selection = [item for item in rows if (item["mode"], item["variant"]) == key]
        groups = {}
        for state in ("active", "dim_train_diagnostic", "synthetic_negative", "capacity_auxiliary"):
            subset = [item for item in selection if item["state"] == state]
            times = [item["elapsed_ms"] for item in subset]
            groups[state] = {"count": len(subset),
                             "correct": sum(item["result"]["correct"] for item in subset),
                             "rejected": sum(not item["result"]["accepted"] for item in subset),
                             "wrong_accepted": sum(item["result"]["accepted"] and not item["result"]["correct"] for item in subset),
                             "raw_exact_match": sum(item["result"]["raw_exact_match"] for item in subset),
                             "raw_high_confidence_mismatch": sum(item["result"].get("raw_high_confidence_mismatch", False) for item in subset),
                             "mean_ms": float(np.mean(times)) if times else None,
                             "p95_ms": float(np.percentile(times, 95)) if times else None}
        elapsed = [item["elapsed_ms"] for item in selection]
        output[f"{key[0]}:{key[1]}"] = {"groups": groups, "elapsed_ms": {
            "sum": sum(elapsed), "mean": float(np.mean(elapsed)), "median": float(np.median(elapsed)),
            "p95": float(np.percentile(elapsed, 95))}, "errors": sum(bool(item.get("error")) for item in selection)}
    return output


def write_report(report, scope):
    """Apply the fixed admission rule without re-running any OCR inference."""
    for row in report["results"]:
        old = row["result"]
        row["result"] = parsed_result([{"rec_texts": old["api_texts"], "rec_scores": old["api_scores"]}],
                                      row["expected"], row["kind"])
    report["summary"] = summarize(report["results"])
    policy = report["policy"]
    policy.pop("variants_fixed_before_labels", None)
    policy["variants"] = sorted({row["variant"] for row in report["results"]})
    policy.update(strict_quantity_regex="[1-9][0-9]{0,3}", zero_and_leading_zero_rejected=True,
                  fixed_variants_applied_to_all_samples=True, no_per_sample_selection=True,
                  capacity_excluded_variants=["bar_removed_3x"],
                  timing_definition="Synchronous pipeline.predict wall time, excluding model load, variant preprocessing and all capture; fixed-order single diagnostic pass, not a benchmark.",
                  preprocessing_selection_scope="Global comparison uses these same labelled calibration screenshots; no held-out claim.")
    report["raw_high_confidence_mismatches"] = [
        {"key": row["key"], "kind": row["kind"], "expected": row["expected"], "mode": row["mode"],
         "variant": row["variant"], "text": row["result"]["text"], "score": row["result"]["score"],
         "accepted_by_positive_quantity_policy": row["result"]["accepted"]}
        for row in report["results"] if row["result"]["raw_high_confidence_mismatch"]]
    confusions = {}
    for row in report["results"]:
        if row["kind"] != "quantity":
            continue
        group = f"{row['mode']}:{row['variant']}:{row['state']}"
        mapping = confusions.setdefault(group, {}).setdefault(row["expected"], {})
        output = row["result"]["text"]
        output_key = output if output is not None else "<no-single-result>"
        mapping[output_key] = mapping.get(output_key, 0)+1
    report["full_string_confusions"] = confusions
    baseline = [row for row in report["results"]
                if row["mode"] == "recognition_only" and row["variant"] == "raw_1x"]
    sensitivity = []
    for threshold in (.50, .80, .95, .99):
        grouped = {}
        for state in ("active", "dim_train_diagnostic", "synthetic_negative"):
            selection = [row for row in baseline if row["state"] == state]
            parsed = [parsed_result([{"rec_texts": row["result"]["api_texts"],
                                      "rec_scores": row["result"]["api_scores"]}],
                                    row["expected"], row["kind"], threshold) for row in selection]
            grouped[state] = {"count": len(parsed), "correct": sum(item["correct"] for item in parsed),
                              "wrong_accepted": sum(item["accepted"] and not item["correct"] for item in parsed),
                              "rejected": sum(not item["accepted"] for item in parsed)}
        sensitivity.append({"score_threshold": threshold, "groups": grouped})
    report["score_sensitivity"] = {"mode": "recognition_only", "variant": "raw_1x",
                                   "inference_repeated": False, "rows": sensitivity}
    (scope / "ocr_comparison_report.json").write_text(json.dumps(report, ensure_ascii=False, indent=2)+"\n", encoding="utf-8")
    lines = ["# Existing PP-OCRv6 warehouse quantity experiment", "",
             f"Backend: {report['backend']['model']}, {report['backend']['provider']}. No dependency or production setting changes.",
             "", "Strict ASCII whole-string recognition, one OCR result, score >= 0.80. Zero/leading-zero stack counts are rejected; their raw text and confidence remain visible.",
             "", "| Mode / variant | Active correct / wrong / rejected | Dim correct / wrong / rejected | Negatives rejected / false accept | Raw high-score mismatches | Mean / p95 ms |",
             "|---|---|---|---|---|---|"]
    for key, entry in report["summary"].items():
        groups = entry["groups"]
        active, dim, negative = [groups[name] for name in ("active", "dim_train_diagnostic", "synthetic_negative")]
        high_errors = sum(group["raw_high_confidence_mismatch"] for group in (active, dim, negative))
        lines.append(f"| {key} | {active['correct']} / {active['wrong_accepted']} / {active['rejected']} | {dim['correct']} / {dim['wrong_accepted']} / {dim['rejected']} | {negative['rejected']} / {negative['wrong_accepted']} | {high_errors} | {entry['elapsed_ms']['mean']:.2f} / {entry['elapsed_ms']['p95']:.2f} |")
    if baseline:
        baseline_groups = report["summary"]["recognition_only:raw_1x"]["groups"]
        lines.extend(["", f"Before the 0.80 admission policy, exact raw-string matches are {baseline_groups['active']['raw_exact_match']}/47 active and {baseline_groups['dim_train_diagnostic']['raw_exact_match']}/25 dim; accepted-correct counts are lower because low-score correct text is rejected.",
                      "", "Threshold sensitivity reuses identical saved raw outputs; the selected policy remains 0.80.", "",
                      "| Score threshold | Active correct / wrong / rejected | Dim correct / wrong / rejected | Negatives rejected / false accept |",
                      "|---:|---|---|---|"])
        for row in sensitivity:
            active, dim, negative = [row["groups"][name] for name in ("active", "dim_train_diagnostic", "synthetic_negative")]
            lines.append(f"| {row['score_threshold']:.2f} | {active['correct']} / {active['wrong_accepted']} / {active['rejected']} | {dim['correct']} / {dim['wrong_accepted']} / {dim['rejected']} | {negative['rejected']} / {negative['wrong_accepted']} |")
    lines.extend(["", "Examples below use one fixed baseline (recognition-only, raw ROI 1x), not the best variant per label.", "",
                  "| Sample | Expected | Exact raw text | Score | Admission result |", "|---|---|---|---:|---|"])
    for row in report["results"]:
        if row["mode"] != "recognition_only" or row["variant"] != "raw_1x":
            continue
        if row["kind"] in {"negative", "capacity"} or row["expected"] == "20":
            value = row["result"]
            score = f"{value['score']:.6f}" if value["score"] is not None else "unknown"
            text = json.dumps(value["text"], ensure_ascii=False)
            lines.append(f"| {row['key']} | {row['expected']} | {text} | {score} | {value['status']} |")
    lines.extend(["", "Raw high-confidence errors after strict parsing are also preserved separately in JSON, including rejected zero/nonnumeric output.",
                  "", "All 0..9 raw outputs, full-string confusions and every failed inference remain in JSON.", "",
                  *[f"- {item}" for item in report["limits"]], ""])
    (scope / "ocr_comparison_report.md").write_text("\n".join(lines), encoding="utf-8")


def native_diagnostic(engine, raw_ctc, scope, provider):
    """Synthetic 0..9 diagnostic, kept separate from all real screenshot results."""
    import cv2
    import numpy as np
    from PIL import Image

    directory = PLAN / "templates/workshop/warehouse/native_digits"
    catalog = PLAN / "data/meta/prison_warehouse_native_digits_pc.json"
    samples, sources = [], []
    for digit in "0123456789":
        path = directory / f"digit_{digit}.png"
        with Image.open(path) as opened:
            glyph = opened.convert("RGBA")
        background = Image.new("RGBA", glyph.size, (155, 155, 155, 255))
        background.alpha_composite(glyph)
        image = background.convert("RGB")
        image.save(scope / f"synthetic_native_digit_{digit}.png")
        samples.append((digit, np.asarray(image)))
        sources.append({"digit": digit, "path": path.relative_to(ROOT).as_posix(), "sha256": digest(path)})
    samples.append((None, np.full_like(samples[0][1], 155)))
    rows = []
    for mode in ("recognition_only", "detector_recognizer"):
        for scale in (1, 3, 6):
            for expected, source in samples:
                image = cv2.resize(source, (source.shape[1]*scale, source.shape[0]*scale), interpolation=cv2.INTER_CUBIC) if scale != 1 else source
                raw_ctc.clear()
                started = time.perf_counter()
                raw = engine.predict(image, recognition_only=mode == "recognition_only", use_textline_orientation=False)
                elapsed_ms = (time.perf_counter()-started)*1000
                output = raw[0] if raw else {}
                texts, scores = output.get("rec_texts", []), output.get("rec_scores", [])
                text = str(texts[0]) if len(texts) == 1 else None
                score = float(scores[0]) if len(scores) == 1 else None
                accepted = bool(text is not None and re.fullmatch(r"[0-9]", text) and score is not None and score >= ADMISSION_SCORE)
                rows.append({"expected": expected, "mode": mode, "scale": scale, "text": text, "score": score,
                             "raw_ctc": list(raw_ctc), "api_texts": texts, "api_scores": scores,
                             "accepted": accepted, "correct": accepted and text == expected if expected is not None else not accepted,
                             "elapsed_ms": elapsed_ms})
    report = {"synthetic_only": True, "mixed_into_real_screenshot_accuracy": False,
              "provider": provider, "model": "ppocrv6_small", "background_rgb": [155, 155, 155],
              "native_catalog": {"path": catalog.relative_to(ROOT).as_posix(), "sha256": digest(catalog)},
              "sources": sources, "results": rows,
              "policy": {"strict_digit_regex": "[0-9]", "admission_score": ADMISSION_SCORE,
                         "zero_allowed_for_glyph_diagnostic_only": True,
                         "scales_applied_to_every_glyph": [1, 3, 6]},
              "limits": ["Synthetic native rasterizations on a neutral background are not screenshots.",
                         "Real supplied warehouse quantities still contain no 8/9 glyphs.",
                         "Correct synthetic isolated digits do not establish unknown-length, damaged-glyph or click readiness."]}
    (scope / "synthetic_native_ocr_diagnostic.json").write_text(json.dumps(report, ensure_ascii=False, indent=2)+"\n", encoding="utf-8")
    lines = ["# Synthetic native digit OCR diagnostic", "", "Separate from real screenshot accuracy; existing PP-OCRv6 model, neutral gray background.",
             "", "| Mode | Scale | Correct 0..9 | Blank rejected | 8 text / score | 9 text / score |", "|---|---:|---:|---|---|---|"]
    for mode in ("recognition_only", "detector_recognizer"):
        for scale in (1, 3, 6):
            selected = [row for row in rows if row["mode"] == mode and row["scale"] == scale]
            eight, nine = [next(row for row in selected if row["expected"] == digit) for digit in ("8", "9")]
            blank = next(row for row in selected if row["expected"] is None)
            def cell(row):
                return f"{json.dumps(row['text'])} / {row['score']:.6f}" if row["score"] is not None else "no result"
            lines.append(f"| {mode} | {scale} | {sum(row['correct'] for row in selected if row['expected'] is not None)}/10 | {not blank['accepted']} | {cell(eight)} | {cell(nine)} |")
    lines.extend(["", *[f"- {item}" for item in report["limits"]], ""])
    (scope / "synthetic_native_ocr_diagnostic.md").write_text("\n".join(lines), encoding="utf-8")
    print(json.dumps({"synthetic_only_rows": len(rows), "path": str(scope / "synthetic_native_ocr_diagnostic.json")}))


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--scope", type=Path, default=DEFAULT_SCOPE)
    parser.add_argument("--variants", nargs="+", choices=VARIANTS, default=VARIANTS)
    parser.add_argument("--modes", nargs="+", choices=("recognition_only", "detector_recognizer"),
                        default=("recognition_only", "detector_recognizer"))
    parser.add_argument("--recompute-report", action="store_true",
                        help="Reapply parsing/report rules to saved raw outputs without OCR inference")
    parser.add_argument("--native-diagnostic", action="store_true",
                        help="Separate synthetic native 0..9 inference; does not overwrite real screenshot report")
    args = parser.parse_args()
    try:
        scope = configure(args.scope)
    except ValueError as exc:
        parser.error(str(exc))
    if args.recompute_report:
        report = json.loads((scope / "ocr_comparison_report.json").read_text(encoding="utf-8"))
        write_report(report, scope)
        print(json.dumps({"recomputed_rows": len(report["results"]), "inference_repeated": False}))
        return
    import cv2
    import numpy as np
    sys.path.insert(0, str(ROOT))
    from plans.aura_base.src.services.ocr_service import OcrService
    from plans.aura_base.src.services.ocr_contract import load_ocr_metadata
    from plans.aura_base.src.services.ocr_onnx_pipeline import OnnxOcrPipeline
    from plans.aura_base.src.services._ocr_recognition import OcrRecognitionOptions

    class ProbeConfig:
        # Per-process overrides only; the installed model/dependencies are unchanged.
        @staticmethod
        def get(key, default=None):
            return {"ocr.execution_provider": "cpu", "ocr.default_model": "ppocrv6_small",
                    "ocr.models_root": "models/ocr"}.get(key, default)

    started = time.perf_counter()
    service = OcrService(config=ProbeConfig())
    bundle, metadata_path = service._resolve_bundle_paths()
    metadata = load_ocr_metadata(metadata_path)
    sessions, provider = service._create_pipeline_sessions(metadata, bundle)
    engine = OnnxOcrPipeline(metadata=metadata, sessions=sessions, provider=provider,
                             recognition_options=OcrRecognitionOptions.from_config(ProbeConfig()))
    model_load_ms = (time.perf_counter()-started)*1000
    raw_ctc = []
    decoder = engine._ctc_decode

    def record_decoder(predictions):
        texts, scores = decoder(predictions)
        raw_ctc.extend({"text": str(text), "score": float(score)} for text, score in zip(texts, scores))
        return texts, scores

    engine._ctc_decode = record_decoder
    if args.native_diagnostic:
        native_diagnostic(engine, raw_ctc, scope, provider)
        return
    samples, sources = read_fixtures(scope)
    # One unlabelled inference warms each selected path; model load/warmup is separate.
    warmup = []
    for mode in args.modes:
        stamp = time.perf_counter()
        engine.predict(np.full((48, 96, 3), 128, np.uint8), recognition_only=mode == "recognition_only",
                       use_textline_orientation=False)
        warmup.append({"mode": mode, "elapsed_ms": (time.perf_counter()-stamp)*1000})
    results, errors = [], []
    for mode in args.modes:
        for variant in args.variants:
            group_started = time.perf_counter()
            for sample in samples:
                if sample["kind"] == "capacity" and variant == "bar_removed_3x":
                    continue
                image = prepare(sample["image"], variant, sample.get("card"))
                raw_ctc.clear()
                stamp = time.perf_counter()
                error = None
                try:
                    raw = engine.predict(image, recognition_only=mode == "recognition_only",
                                         use_textline_orientation=False)
                except Exception as exc:
                    error = {"type": type(exc).__name__, "message": str(exc)}
                    errors.append({"sample": sample["key"], "mode": mode, "variant": variant, **error})
                    raw = []
                elapsed_ms = (time.perf_counter()-stamp)*1000
                result = parsed_result(raw, sample["expected"], sample["kind"])
                results.append({key: value for key, value in sample.items() if key not in {"image", "card"}} | {
                    "mode": mode, "variant": variant, "elapsed_ms": elapsed_ms, "raw_ctc": list(raw_ctc),
                    "input_size": [image.shape[1], image.shape[0]], "result": result, "error": error})
            print(json.dumps({"mode": mode, "variant": variant,
                              "elapsed_seconds": time.perf_counter()-group_started}), flush=True)
    summary = summarize(results)
    output_digits = Counter(character for row in results for item in row["raw_ctc"] for character in item["text"] if character in "0123456789")
    expected_digits = Counter(character for sample in samples if sample["kind"] == "quantity"
                              for character in sample["expected"])
    model_files = [metadata_path] + [bundle / value for value in metadata.models.model_dump().values() if value]
    report = {"scope": "offline_supplied_screenshots_only_no_game_or_dependency_changes",
              "sources": sources, "hand_labels": {"path": (SOURCES / "quantity_samples.json").relative_to(ROOT).as_posix(),
                                                       "sha256": digest(SOURCES / "quantity_samples.json")},
              "backend": {"model": bundle.name, "provider": provider, "input_order": "RGB",
                          "onnxruntime_version": importlib.metadata.version("onnxruntime-gpu") if "CUDAExecutionProvider" in __import__("onnxruntime").get_available_providers() else __import__("onnxruntime").__version__,
                          "opencv_version": cv2.__version__, "numpy_version": np.__version__,
                          "models": [{"path": path.relative_to(ROOT).as_posix(), "sha256": digest(path)} for path in model_files],
                          "model_load_ms": model_load_ms, "warmup": warmup,
                          "metadata_score_threshold": metadata.default_score_threshold,
                          "orientation_enabled_for_probe": False},
              "policy": {"quantity_roi_xywh": list(QUANTITY_ROI), "variants": list(args.variants),
                         "strict_quantity_regex": "[1-9][0-9]{0,3}", "strict_capacity_regex": "[0-9]+/[0-9]+",
                         "admission_score": ADMISSION_SCORE, "requires_single_ocr_result": True,
                         "no_character_stripping_or_O_to_zero_correction": True,
                         "raw_ctc_recorded_before_model_score_filter": True,
                         "recognition_only": "Bypasses text detection and orientation; treats full ROI as one line.",
                         "detector_recognizer": "Uses native detector geometric crops and recognizer; orientation classifier disabled explicitly."},
              "digit_coverage": {digit: {"expected_occurrences_in_72_quantity_labels": expected_digits[digit],
                                          "raw_output_occurrences_all_runs": output_digits[digit]} for digit in "0123456789"},
              "summary": summary, "errors": errors, "results": results,
              "limits": ["Same screenshots used to inspect preprocessing; no held-out accuracy or live-runtime claim.",
                         "Real quantity labels cover 0..7 only, and 20 is the sole real multidigit number.",
                         "Synthetic damaged controls are expected unknown, even if a remaining glyph resembles a valid digit.",
                         "No per-sample or truth-driven selection among image variants is performed.",
                         "This fixed 0.80 admission threshold is an experiment policy, not a calibrated safety bound."]}
    write_report(report, scope)
    print(json.dumps({"report": str(scope / "ocr_comparison_report.json"), "rows": len(results), "errors": len(errors),
                      "model_load_ms": model_load_ms}, ensure_ascii=False), flush=True)


if __name__ == "__main__":
    main()
