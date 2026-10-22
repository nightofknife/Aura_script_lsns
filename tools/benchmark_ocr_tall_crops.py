"""Compare 90-degree crop rotation with frozen real detector boxes.

Both arms share loaded/warmed sessions and always enable direction classification.
Detect once per sample outside timing, then alternate on/off with identical boxes.
All other preprocessing, classification, recognition and parsing remain real.
"""
from __future__ import annotations

import argparse
import asyncio
import csv
from dataclasses import asdict
import hashlib
import json
from pathlib import Path
import random
import sys
import time

import cv2
import numpy as np
import onnxruntime as ort
from PIL import Image

from plans.aura_base.src.services._ocr_timing import OcrTiming
from plans.aura_base.src.services.ocr_service import OcrService
from tools.benchmark_ocr_recognition import _compare, _fingerprint
from tools.benchmark_ocr_stages import (
    ROOT, _Config, _repo_path, _run_worker, _save, _session_options, _sha256,
)


ARMS = {"on": True, "off": False}
SOURCES = (
    "tools/benchmark_ocr_tall_crops.py",
    "plans/aura_base/src/services/ocr_onnx_pipeline.py",
    "plans/aura_base/src/services/ocr_service.py",
    "plans/aura_base/src/services/_ocr_recognition.py",
    "plans/aura_base/src/services/_ocr_timing.py",
    "packages/aura_core/services/onnx_runtime_backend.py",
    "packaging/templates/config.yaml",
)


def _tall_size(pipeline, box):
    p = pipeline._order_points_clockwise(box.astype(np.float32))
    width = max(1, int(max(np.linalg.norm(p[0] - p[1]), np.linalg.norm(p[2] - p[3]))))
    height = max(1, int(max(np.linalg.norm(p[0] - p[3]), np.linalg.norm(p[1] - p[2]))))
    return [width, height]


def _worker(args, output):
    config = _Config()
    service = OcrService(config=config)
    asyncio.run(service._preload_engine_async(warmup=True))
    pipeline = service._engine
    bundle, meta = service._resolve_bundle_paths()
    artifact_paths = [meta, bundle / pipeline.metadata.models.det, bundle / pipeline.metadata.models.rec,
                      bundle / pipeline.metadata.models.textline_orientation]
    artifact_hashes = {str(p.relative_to(ROOT)): _sha256(p) for p in artifact_paths}
    source_hashes = {p: _sha256(ROOT / p) for p in SOURCES}
    settings = {name: _session_options(session) for name, session in vars(pipeline.sessions).items()
                if session is not None}
    if not pipeline._use_textline_orientation():
        raise RuntimeError("The classifier must remain enabled.")
    for setting in settings.values():
        if (setting["intra_op_num_threads"], setting["intra_op_allow_spinning"],
                setting["inter_op_allow_spinning"]) != (8, "0", "0"):
            raise RuntimeError("Unexpected session threading configuration.")
    if (pipeline.recognition_options.width_mode, pipeline.recognition_options.batch_mode) != ("legacy", "adaptive"):
        raise RuntimeError("Recognition width and batching must retain their defaults.")
    specs = json.loads(args.samples.read_text(encoding="utf-8"))
    specs += [dict(name="control_small_full", path="tests/fixtures/city_identity/main_visit_roi.png",
                   group="control_small"),
              dict(name="control_large_full", path="tests/fixtures/raw_workshop_ui/idle_blue.png",
                   group="control_large")]
    historical = {}
    if args.legacy_results:
        historical = {row["name"]: row["result"] for row in
                      json.loads(args.legacy_results.read_text(encoding="utf-8"))["rows"]
                      if row["mode"] == "full"}
    random.Random(20261101 + args.round).shuffle(specs)
    result = {"round": args.round, "python": sys.version, "ort": ort.__version__,
              "numpy": np.__version__, "opencv": cv2.__version__, "opencv_threads": cv2.getNumThreads(),
              "sessions": settings, "recognition_options": asdict(pipeline.recognition_options),
              "source_sha256": source_hashes, "model_artifact_sha256": artifact_hashes,
              "samples_sha256": _sha256(args.samples), "iterations": args.iterations,
              "warmup": args.warmup, "samples": [],
              "scope": "Paired shared warmed pipeline, detector replaced by frozen boxes only. "
                       "Total includes image conversion, geometric crop, enabled orientation classifier, "
                       "recognition, result packing/parsing and diagnostic timing. "
                       "Excludes detection, capture, file decoding, warmup, async bridge and fingerprinting."}
    detector = pipeline._detect_text_boxes
    for spec in specs:
        path = _repo_path(Path(spec["path"]))
        with Image.open(path) as image_file:
            image = np.asarray(image_file.convert("RGB"))
        if spec.get("crop_xywh"):
            x, y, w, h = spec["crop_xywh"]
            image = image[y:y + h, x:x + w].copy()
        scale = spec.get("scale", 1)
        if scale != 1:
            image = cv2.resize(image, None, fx=scale, fy=scale, interpolation=cv2.INTER_CUBIC)
        frozen = [box.copy() for box in detector(pipeline._ensure_bgr_image(image))]
        for box in frozen:
            box.flags.writeable = False
        sizes = [_tall_size(pipeline, b) for b in frozen]
        box_bytes = b"".join(b.tobytes() for b in frozen)

        def frozen_detector(_image, **_kwargs):
            return [box.copy() for box in frozen]

        def predict(arm=None, timing=None):
            kwargs = {"use_textline_orientation": True}
            if arm is not None:
                kwargs["rotate_tall_crops"] = ARMS[arm]
            if timing is not None:
                kwargs["timing"] = timing
            raw = pipeline.predict(image, **kwargs)
            if timing is not None:
                with timing.measure("result_parse"):
                    parsed = service._parse_results(raw)
            else:
                parsed = service._parse_results(raw)
            return raw, parsed

        pipeline._detect_text_boxes = frozen_detector
        try:
            references = {}
            for _ in range(args.warmup):
                for arm in ARMS:
                    references[arm] = _fingerprint(*predict(arm))
            if _fingerprint(*predict()) != references["off"]:
                raise RuntimeError("Omitted option does not equal explicit False.")
            historical_equal = (references["on"] == historical[spec["name"]]
                                if spec["name"] in historical else None)
            if historical_equal is False:
                raise RuntimeError(f"Explicit True did not reproduce the previous result: {spec['name']}")
            sample = {**spec, "source_sha256": _sha256(path), "input_shape": list(image.shape),
                      "boxes": [b.tolist() for b in frozen], "box_sha256": hashlib.sha256(box_bytes).hexdigest(),
                      "perspective_sizes": sizes, "tall_box_count": sum(h / w >= 1.5 for w, h in sizes),
                      "default_equals_off": True, "on_equals_historical": historical_equal,
                      "comparison": _compare(references["on"], references["off"]),
                      "arms": {arm: {"reference": fp, "calls": []} for arm, fp in references.items()}}
            for iteration in range(args.iterations):
                order = ["on", "off"] if (iteration + args.round) % 2 == 0 else ["off", "on"]
                for slot, arm in enumerate(order):
                    timing = OcrTiming()
                    start = time.perf_counter_ns()
                    raw, parsed = predict(arm, timing)
                    elapsed = (time.perf_counter_ns() - start) / 1e6
                    actual = _fingerprint(raw, parsed)
                    if actual != references[arm]:
                        raise RuntimeError(f"Unstable output for {spec['name']}/{arm}")
                    report = timing.snapshot(total_ms=elapsed)
                    calls = report["stage_calls"]
                    if calls["det_inference"] != 0 or calls["det_preprocess"] != 0:
                        raise RuntimeError("Detection entered the measured region.")
                    expected_ori_calls = (len(frozen) + pipeline.metadata.pipeline.batch_size - 1) // pipeline.metadata.pipeline.batch_size
                    if calls["ori_inference"] != expected_ori_calls:
                        raise RuntimeError("Orientation was disabled or not run for every frozen box.")
                    sample["arms"][arm]["calls"].append({"iteration": iteration, "order": order,
                                                        "slot": slot, "reference_equal": True, **report})
            if b"".join(b.tobytes() for b in frozen) != box_bytes:
                raise RuntimeError("Frozen detector boxes were mutated.")
            result["samples"].append(sample)
        finally:
            del pipeline._detect_text_boxes
        if len(result["samples"]) % 20 == 0 or spec["name"].startswith("control"):
            print(f"round {args.round}: {len(result['samples'])}/{len(specs)}, {spec['name']}", flush=True)
    if source_hashes != {p: _sha256(ROOT / p) for p in SOURCES}:
        raise RuntimeError("Source/default configuration changed during the experiment.")
    if artifact_hashes != {str(p.relative_to(ROOT)): _sha256(p) for p in artifact_paths}:
        raise RuntimeError("Model changed during the experiment.")
    _save(output / f"run-{args.round}.json", result)


def _summarize(output, paths):
    runs = [json.loads(p.read_text(encoding="utf-8")) for p in paths]
    for run in runs[1:]:
        for key in ("source_sha256", "model_artifact_sha256", "sessions", "samples_sha256", "recognition_options", "ort", "opencv_threads"):
            if run[key] != runs[0][key]:
                raise RuntimeError(f"Changed environment across rounds: {key}")
    rows = []
    for sample in runs[0]["samples"]:
        peers = [next(s for s in r["samples"] if s["name"] == sample["name"]) for r in runs]
        if any(p["box_sha256"] != sample["box_sha256"] or p["input_shape"] != sample["input_shape"]
               or p["source_sha256"] != sample["source_sha256"]
               for p in peers):
            raise RuntimeError("Source image/detector boxes/input shape changed across workers.")
        row = {"sample": sample["name"], "group": sample["group"], "expected": sample.get("expected"),
               "box_count": len(sample["boxes"]), "tall_box_count": sample["tall_box_count"],
               "on_equals_historical": sample["on_equals_historical"],
               "comparison": sample["comparison"], "arms": {}}
        for arm in ARMS:
            reference = sample["arms"][arm]["reference"]
            if any(p["arms"][arm]["reference"] != reference for p in peers):
                raise RuntimeError("Output changed across workers.")
            calls = [c for p in peers for c in p["arms"][arm]["calls"]]
            texts = reference["raw"][0]["rec_texts"]
            row["arms"][arm] = {"n": len(calls), "mean_ms": float(np.mean([c["total_ms"] for c in calls])),
                "p50_ms": float(np.median([c["total_ms"] for c in calls])),
                "p95_ms": float(np.percentile([c["total_ms"] for c in calls], 95)),
                "texts": texts, "confidences": reference["raw"][0]["rec_scores"],
                "exact_label": texts == [sample["expected"]] if "expected" in sample else None,
                "stage_mean_ms": {stage: float(np.mean([c["stages_ms"][stage] for c in calls]))
                                  for stage in calls[0]["stages_ms"]},
                "round_mean_ms": {str(r["round"]): float(np.mean([c["total_ms"] for c in p["arms"][arm]["calls"]]))
                                  for r, p in zip(runs, peers)}}
        row["off_reduction_percent"] = (1 - row["arms"]["off"]["mean_ms"] / row["arms"]["on"]["mean_ms"]) * 100
        rows.append(row)
    groups = []
    for group in sorted({r["group"] for r in rows}):
        selected = [r for r in rows if r["group"] == group]
        record = {"group": group, "n_samples": len(selected),
                  "detected_samples": sum(r["box_count"] > 0 for r in selected),
                  "tall_samples": sum(r["tall_box_count"] > 0 for r in selected)}
        for arm in ARMS:
            record[arm + "_mean_ms"] = float(np.mean([r["arms"][arm]["mean_ms"] for r in selected]))
            record[arm + "_correct"] = (sum(r["arms"][arm]["exact_label"] is True for r in selected)
                                        if any(r["expected"] is not None for r in selected) else None)
        groups.append(record)
    _save(output / "summary.json", {"rows": rows, "groups": groups, "run_paths": [str(p.relative_to(ROOT)) for p in paths],
          "scope": runs[0]["scope"], "accuracy_scope": "Exact labels on existing resources/crops, not live accuracy."})
    with (output / "groups.csv").open("w", encoding="utf-8-sig", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=list(groups[0])); writer.writeheader(); writer.writerows(groups)
    _save(output / "artifact-sha256.json", {str(p.relative_to(output)): _sha256(p)
                                            for p in [*paths, output / "summary.json", output / "groups.csv"]})
    for row in groups:
        print(json.dumps(row, ensure_ascii=False), flush=True)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--samples", type=Path, required=True)
    parser.add_argument("--legacy-results", type=Path)
    parser.add_argument("--output", type=Path, default=Path(".pytest_tmp/ocr-tall-crop/benchmark"))
    parser.add_argument("--rounds", type=int, default=2)
    parser.add_argument("--iterations", type=int, default=20)
    parser.add_argument("--warmup", type=int, default=3)
    parser.add_argument("--worker", action="store_true", help=argparse.SUPPRESS)
    parser.add_argument("--round", type=int, default=0, help=argparse.SUPPRESS)
    parser.add_argument("--summarize-only", action="store_true")
    args = parser.parse_args()
    if min(args.rounds, args.iterations, args.warmup) < 1:
        parser.error("Counts must be positive.")
    args.samples, output = _repo_path(args.samples), _repo_path(args.output)
    if args.legacy_results:
        args.legacy_results = _repo_path(args.legacy_results)
    output.mkdir(parents=True, exist_ok=True)
    if args.worker:
        _worker(args, output)
        return
    paths = [output / f"run-{r}.json" for r in range(args.rounds)]
    if not args.summarize_only:
        for r in range(args.rounds):
            command = [sys.executable, "-m", "tools.benchmark_ocr_tall_crops", "--worker", "--round", str(r),
                       "--output", str(output), "--samples", str(args.samples), "--iterations", str(args.iterations),
                       "--warmup", str(args.warmup)]
            if args.legacy_results:
                command += ["--legacy-results", str(args.legacy_results)]
            _run_worker(command, output)
    _summarize(output, paths)


if __name__ == "__main__":
    main()
