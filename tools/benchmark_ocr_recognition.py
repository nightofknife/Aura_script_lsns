"""Isolate recognition width and batch-padding changes on real OCR fixtures.

Run from the repository root. By default each policy and round uses a fresh
process. ``--paired`` shares one session bundle between four policy pipelines
per round and interleaves the policies within each measured iteration. Workers
always run sequentially, with deterministic randomized policy/sample order.
Output equality describes agreement with the legacy pipeline, not accuracy
against human labels. No synthetic text is used.
"""
from __future__ import annotations

import argparse
import asyncio
import csv
from dataclasses import asdict
import hashlib
import json
import math
import os
from pathlib import Path
import platform
import random
import sys
import time

import cv2
import numpy as np
from PIL import Image
import psutil

from plans.aura_base.src.services._ocr_timing import OcrTiming
from plans.aura_base.src.services._ocr_recognition import OcrRecognitionOptions
from plans.aura_base.src.services.ocr_onnx_pipeline import OnnxOcrPipeline
from plans.aura_base.src.services.ocr_service import OcrService
from tools.benchmark_ocr_cpu import SAMPLES
from tools.benchmark_ocr_stages import (
    ROOT, GROUPS, _environment, _json_value, _repo_path, _run_worker,
    _save, _session_options, _sha256,
)


POLICIES = {
    "baseline": ("legacy", "fixed"),
    "width_only": ("adaptive", "fixed"),
    "batch_only": ("legacy", "adaptive"),
    "combined": ("adaptive", "adaptive"),
}
THRESHOLDS = (0.5, 0.8, 0.9)


class _Config:
    def __init__(self, policy: str):
        width, batching = POLICIES[policy]
        self.values = {
            "ocr.models_root": str(ROOT / "models/ocr"),
            "ocr.default_model": "ppocrv6_small",
            "ocr.execution_provider": "cpu",
            "ocr.session.intra_op_num_threads": 8,
            "ocr.session.allow_spinning": False,
            "ocr.recognition.width_mode": width,
            "ocr.recognition.batch_mode": batching,
            "ocr.recognition.min_width": 160,
            "ocr.recognition.batch_size": 8,
            "ocr.recognition.split_min_saving": 0.25,
        }

    def get(self, key, default=None):
        return self.values.get(key, default)


def _fingerprint(raw, parsed):
    return {"raw": _json_value(raw), "parsed": _json_value([asdict(item) for item in parsed])}


def _regions(fingerprint: dict) -> list[dict]:
    data = fingerprint["raw"][0]
    result = []
    for index, (text, score, polygon) in enumerate(zip(
        data["rec_texts"], data["rec_scores"], data["rec_polys"],
    )):
        parsed = fingerprint["parsed"][index]
        result.append({"text": text, "confidence": score, "polygon": polygon,
                       "center": parsed["center_point"], "rect": parsed["rect"]})
    return result


def _compare(reference: dict, actual: dict) -> dict:
    before, after = _regions(reference), _regions(actual)
    before_by_polygon = {json.dumps(region["polygon"]): region for region in before}
    after_by_polygon = {json.dumps(region["polygon"]): region for region in after}
    differences, crossings = [], []
    confidence_deltas = []
    for polygon in sorted(before_by_polygon.keys() | after_by_polygon.keys()):
        old, new = before_by_polygon.get(polygon), after_by_polygon.get(polygon)
        if old is not None and new is not None:
            delta = new["confidence"] - old["confidence"]
            confidence_deltas.append(delta)
            for threshold in THRESHOLDS:
                if (old["confidence"] >= threshold) != (new["confidence"] >= threshold):
                    crossings.append({"polygon": old["polygon"], "threshold": threshold,
                                      "before": old["confidence"], "after": new["confidence"],
                                      "before_text": old["text"], "after_text": new["text"]})
        if old != new:
            differences.append({"before": old, "after": new})
    return {
        "exact_equal": reference == actual,
        "count_equal": len(before) == len(after),
        "text_equal": [r["text"] for r in before] == [r["text"] for r in after],
        "polygons_equal": [r["polygon"] for r in before] == [r["polygon"] for r in after],
        "centers_equal": [r["center"] for r in before] == [r["center"] for r in after],
        "rects_equal": [r["rect"] for r in before] == [r["rect"] for r in after],
        "confidence_equal": [r["confidence"] for r in before] == [r["confidence"] for r in after],
        "max_abs_confidence_delta": max(map(abs, confidence_deltas), default=0.0),
        "mean_abs_confidence_delta": float(np.mean(np.abs(confidence_deltas))) if confidence_deltas else 0.0,
        "confidence_within_1e_6": len(before) == len(after) and len(confidence_deltas) == len(before)
                                  and all(abs(delta) <= 1e-6 for delta in confidence_deltas),
        "confidence_within_1e_4": len(before) == len(after) and len(confidence_deltas) == len(before)
                                  and all(abs(delta) <= 1e-4 for delta in confidence_deltas),
        "threshold_crossings": crossings, "region_differences": differences,
        "baseline_count": len(before), "actual_count": len(after),
    }


def _geometry(batches):
    """Derive padding outside the timed call from actual profiling records."""
    allocated = sum(batch["input_shape"][0] * batch["input_shape"][3] for batch in batches)
    content = sum(sum(batch["content_widths"]) for batch in batches)
    return {
        "recognition_batches": batches,
        "recognition_batch_count": len(batches),
        "recognition_input_elements": sum(math.prod(batch["input_shape"]) for batch in batches),
        "recognition_padding_percent": (allocated - content) / allocated * 100 if allocated else 0.0,
    }


def _repeat_ranges(sample):
    observed_regions = {}
    for call in sample["calls"]:
        for region in _regions(call.get("actual", sample["reference"])):
            key = json.dumps(region["polygon"])
            observed_regions.setdefault(key, []).append(region)
    return [
        {"polygon": regions[0]["polygon"], "observations": len(regions),
         "texts": sorted(set(region["text"] for region in regions)),
         "confidence_min": min(region["confidence"] for region in regions),
         "confidence_max": max(region["confidence"] for region in regions)}
        for regions in observed_regions.values()
    ]


def _worker(args):
    import onnxruntime as ort

    config = _Config(args.policy)
    process = psutil.Process()
    service = OcrService(config=config)
    started = time.perf_counter_ns()
    asyncio.run(service._preload_engine_async(warmup=True))
    startup_ms = (time.perf_counter_ns() - started) / 1e6
    pipeline = service._engine
    if pipeline is None:
        raise RuntimeError("Startup did not load OCR sessions.")
    sessions = {name: _session_options(session) for name, session in vars(pipeline.sessions).items()
                if session is not None}
    for name, options in sessions.items():
        if (options["intra_op_num_threads"], options["intra_op_allow_spinning"],
                options["inter_op_allow_spinning"]) != (8, "0", "0"):
            raise RuntimeError(f"Unexpected CPU session settings: {name}: {options}")
    bundle, metadata_path = service._resolve_bundle_paths()
    paths = {"metadata": metadata_path, "detection": bundle / pipeline.metadata.models.det,
             "recognition": bundle / pipeline.metadata.models.rec}
    for name, attribute in (("orientation", "textline_orientation"), ("doc_orientation", "doc_orientation")):
        if getattr(pipeline.sessions, attribute) is not None:
            paths[name] = bundle / getattr(pipeline.metadata.models, attribute)
    result = {
        "round": args.round, "policy": args.policy, "config": config.values, "pid": os.getpid(),
        "model": service.get_model(), "provider": service.get_provider(), "ort": ort.__version__,
        "python": sys.version, "numpy": np.__version__, "opencv": cv2.__version__,
        "opencv_threads": cv2.getNumThreads(), "processor": platform.processor(),
        "platform": platform.platform(), "logical_cpus": os.cpu_count(),
        "physical_cores": psutil.cpu_count(logical=False), "sessions": sessions,
        "startup_load_and_warmup_ms": startup_ms, "sample_warmup": args.warmup,
        "iterations": args.iterations, "rss_after_startup_mib": process.memory_info().rss / 1024 ** 2,
        "timing_scope": "pipeline + service parsing, stage timers and batch shape diagnostics; excludes screenshots, "
                        "decoding, loading/warmup, geometry arithmetic, output comparison and serialization",
        "accuracy_scope": "Agreement with baseline only; fixtures have no independently checked ground truth here.",
        "model_artifacts": {name: {"path": str(path.relative_to(ROOT)), "sha256": _sha256(path),
                                   "bytes": path.stat().st_size} for name, path in paths.items()},
        "source_sha256": {relative: _sha256(ROOT / relative) for relative in (
            "plans/aura_base/src/services/ocr_service.py", "plans/aura_base/src/services/ocr_onnx_pipeline.py",
            "plans/aura_base/src/services/_ocr_timing.py", "tools/benchmark_ocr_recognition.py",
            "plans/aura_base/src/services/_ocr_recognition.py",
        )}, "samples": [],
    }
    samples = list(SAMPLES)
    random.Random(20261022 + args.round).shuffle(samples)
    for name, relative, crop, recognition_only in samples:
        path = ROOT / relative
        with Image.open(path) as source:
            image = np.asarray(source.convert("RGB"))
        if crop:
            x, y, width, height = crop
            image = image[y:y + height, x:x + width].copy()

        def predict(timing=None):
            raw = pipeline.predict(image, recognition_only=recognition_only, timing=timing)
            if timing is None:
                return raw, service._parse_results(raw)
            with timing.measure("result_parse"):
                parsed = service._parse_results(raw)
            return raw, parsed

        for _ in range(args.warmup):
            raw, parsed = predict()
        reference = _fingerprint(raw, parsed)
        if not parsed:
            raise RuntimeError(f"Empty real OCR sample: {name}")
        sample = {"name": name, "path": relative, "sha256": _sha256(path), "crop_xywh": crop,
                  "image_bytes_sha256": hashlib.sha256(image.tobytes()).hexdigest(), "shape": list(image.shape),
                  "recognition_only": recognition_only, "reference": reference,
                  "stable_within_worker": True, "calls": []}
        for iteration in range(args.iterations):
            timing = OcrTiming()
            cpu_started = time.process_time_ns()
            started = time.perf_counter_ns()
            raw, parsed = predict(timing)
            elapsed = (time.perf_counter_ns() - started) / 1e6
            cpu_ms = (time.process_time_ns() - cpu_started) / 1e6
            actual = _fingerprint(raw, parsed)
            equal = actual == reference
            sample["stable_within_worker"] &= equal
            snapshot = timing.snapshot(total_ms=elapsed)
            call = {"iteration": iteration, **snapshot, "cpu_ms": cpu_ms,
                    "geometry": _geometry(snapshot["recognition_batches"]), "reference_equal": equal}
            if not equal:
                call["actual"] = actual
                call["reference_comparison"] = _compare(reference, actual)
            sample["calls"].append(call)
        sample["repeat_region_ranges"] = _repeat_ranges(sample)
        result["samples"].append(sample)
        mean = np.mean([call["total_ms"] for call in sample["calls"]])
        print(f"  {args.policy}/{name}: {len(parsed)} regions, mean={mean:.2f} ms", flush=True)
    result["rss_after_run_mib"] = process.memory_info().rss / 1024 ** 2
    _save(args.worker_output, result)


def _paired_worker(args, output):
    """Use exactly one session bundle, with balanced interleaved policy order."""
    import onnxruntime as ort

    process = psutil.Process()
    service = OcrService(config=_Config("baseline"))
    started = time.perf_counter_ns()
    asyncio.run(service._preload_engine_async(warmup=True))
    shared_startup_ms = (time.perf_counter_ns() - started) / 1e6
    baseline_pipeline = service._engine
    if baseline_pipeline is None:
        raise RuntimeError("Startup did not load OCR sessions.")
    shared_sessions = baseline_pipeline.sessions
    session_settings = {name: _session_options(session) for name, session in vars(shared_sessions).items()
                        if session is not None}
    for name, options in session_settings.items():
        if (options["intra_op_num_threads"], options["intra_op_allow_spinning"],
                options["inter_op_allow_spinning"]) != (8, "0", "0"):
            raise RuntimeError(f"Unexpected CPU session settings: {name}: {options}")
    bundle, metadata_path = service._resolve_bundle_paths()
    metadata = baseline_pipeline.metadata
    paths = {"metadata": metadata_path, "detection": bundle / metadata.models.det,
             "recognition": bundle / metadata.models.rec}
    for name, attribute in (("orientation", "textline_orientation"), ("doc_orientation", "doc_orientation")):
        if getattr(shared_sessions, attribute) is not None:
            paths[name] = bundle / getattr(metadata.models, attribute)
    artifacts = {name: {"path": str(path.relative_to(ROOT)), "sha256": _sha256(path),
                        "bytes": path.stat().st_size} for name, path in paths.items()}
    source_sha256 = {relative: _sha256(ROOT / relative) for relative in (
        "plans/aura_base/src/services/ocr_service.py", "plans/aura_base/src/services/ocr_onnx_pipeline.py",
        "plans/aura_base/src/services/_ocr_timing.py", "tools/benchmark_ocr_recognition.py",
        "plans/aura_base/src/services/_ocr_recognition.py",
    )}
    pipelines, results = {}, {}
    for policy in POLICIES:
        config = _Config(policy)
        pipeline = OnnxOcrPipeline(
            metadata=metadata, sessions=shared_sessions, provider=baseline_pipeline.provider,
            recognition_options=OcrRecognitionOptions.from_config(config),
        )
        if pipeline.sessions is not shared_sessions:
            raise RuntimeError("Paired experiment must reuse the original session bundle.")
        started = time.perf_counter_ns()
        completed = pipeline.warmup()
        policy_warmup_ms = (time.perf_counter_ns() - started) / 1e6
        pipelines[policy] = pipeline
        results[policy] = {
            "round": args.round, "policy": policy, "config": config.values, "pid": os.getpid(),
            "experiment": "paired_shared_sessions", "shared_session_bundle": True,
            "shared_session_object_ids": {name: id(session) for name, session in vars(shared_sessions).items()
                                          if session is not None},
            "policy_order": "Randomized base order per block of four iterations; cyclic rotation within block. "
                            "Each arm occupies every slot once per complete block.",
            "model": service.get_model(), "provider": service.get_provider(), "ort": ort.__version__,
            "python": sys.version, "numpy": np.__version__, "opencv": cv2.__version__,
            "opencv_threads": cv2.getNumThreads(), "processor": platform.processor(),
            "platform": platform.platform(), "logical_cpus": os.cpu_count(),
            "physical_cores": psutil.cpu_count(logical=False), "sessions": session_settings,
            "startup_load_and_warmup_ms": shared_startup_ms,
            "policy_warmup_ms": policy_warmup_ms, "policy_warmup_stages": list(completed),
            "sample_warmup": args.warmup, "iterations": args.iterations,
            "rss_after_startup_mib": process.memory_info().rss / 1024 ** 2,
            "timing_scope": "pipeline + service parsing, stage timers and batch shape diagnostics; excludes "
                            "capture, image decoding, loading/warmup, geometry arithmetic, output comparison "
                            "and serialization. All policies share one session bundle and interleave sequentially.",
            "accuracy_scope": "Agreement with baseline only; fixtures have no independently checked ground truth here.",
            "model_artifacts": artifacts, "source_sha256": source_sha256, "samples": [],
        }
    samples = list(SAMPLES)
    random.Random(20261022 + args.round).shuffle(samples)
    rng = random.Random(20261027 + args.round)
    for name, relative, crop, recognition_only in samples:
        path = ROOT / relative
        with Image.open(path) as source:
            image = np.asarray(source.convert("RGB"))
        if crop:
            x, y, width, height = crop
            image = image[y:y + height, x:x + width].copy()

        def predict(policy, timing=None):
            raw = pipelines[policy].predict(image, recognition_only=recognition_only, timing=timing)
            if timing is None:
                return raw, service._parse_results(raw)
            with timing.measure("result_parse"):
                parsed = service._parse_results(raw)
            return raw, parsed

        references = {}
        for _ in range(args.warmup):
            order = list(POLICIES)
            rng.shuffle(order)
            for policy in order:
                raw, parsed = predict(policy)
                if not parsed:
                    raise RuntimeError(f"Empty real OCR sample: {name}/{policy}")
                references[policy] = _fingerprint(raw, parsed)
        sample_results = {
            policy: {"name": name, "path": relative, "sha256": _sha256(path), "crop_xywh": crop,
                     "image_bytes_sha256": hashlib.sha256(image.tobytes()).hexdigest(), "shape": list(image.shape),
                     "recognition_only": recognition_only, "reference": references[policy],
                     "stable_within_worker": True, "calls": []}
            for policy in POLICIES
        }
        for iteration in range(args.iterations):
            if iteration % len(POLICIES) == 0:
                base_order = list(POLICIES)
                rng.shuffle(base_order)
            rotation = iteration % len(POLICIES)
            order = base_order[rotation:] + base_order[:rotation]
            for slot, policy in enumerate(order):
                timing = OcrTiming()
                cpu_started = time.process_time_ns()
                started = time.perf_counter_ns()
                raw, parsed = predict(policy, timing)
                elapsed = (time.perf_counter_ns() - started) / 1e6
                cpu_ms = (time.process_time_ns() - cpu_started) / 1e6
                actual = _fingerprint(raw, parsed)
                equal = actual == references[policy]
                sample_results[policy]["stable_within_worker"] &= equal
                snapshot = timing.snapshot(total_ms=elapsed)
                call = {"iteration": iteration, "paired_order": slot, "paired_policy_order": order,
                        **snapshot, "cpu_ms": cpu_ms, "geometry": _geometry(snapshot["recognition_batches"]),
                        "reference_equal": equal}
                if not equal:
                    call["actual"] = actual
                    call["reference_comparison"] = _compare(references[policy], actual)
                sample_results[policy]["calls"].append(call)
        for policy, sample in sample_results.items():
            sample["repeat_region_ranges"] = _repeat_ranges(sample)
            results[policy]["samples"].append(sample)
            mean = np.mean([call["total_ms"] for call in sample["calls"]])
            print(f"  paired {policy}/{name}: {len(sample['reference']['parsed'])} regions, "
                  f"mean={mean:.2f} ms", flush=True)
    for policy, result in results.items():
        result["rss_after_run_mib"] = process.memory_info().rss / 1024 ** 2
        _save(output / f"run-{args.round}-{policy}.json", result)


def _summarize(output, run_paths):
    runs = [json.loads(path.read_text(encoding="utf-8")) for path in run_paths]
    if not runs:
        raise ValueError("No ablation workers found.")
    experiments = {run.get("experiment", "isolated_workers") for run in runs}
    if len(experiments) != 1:
        raise ValueError("Do not combine isolated and paired experiments in one summary.")
    for run in runs[1:]:
        for key in ("source_sha256", "model_artifacts", "sessions", "ort", "opencv_threads"):
            if run[key] != runs[0][key]:
                raise RuntimeError(f"Benchmark environment changed: {key}")
    by_round = {(run["round"], run["policy"]): run for run in runs}
    comparisons, rows, calls_csv = [], [], []
    for name, *_ in SAMPLES:
        baseline_samples = [next(sample for sample in run["samples"] if sample["name"] == name)
                            for run in runs if run["policy"] == "baseline"]
        if not baseline_samples:
            raise ValueError("A baseline worker is required.")
        baseline_calls = [call for sample in baseline_samples for call in sample["calls"]]
        baseline_mean = float(np.mean([call["total_ms"] for call in baseline_calls]))
        baseline_rec_mean = float(np.mean([call["stages_ms"]["rec_inference"] for call in baseline_calls]))
        for policy in POLICIES:
            arm_runs = [run for run in runs if run["policy"] == policy]
            samples = [next(sample for sample in run["samples"] if sample["name"] == name) for run in arm_runs]
            calls = [call for sample in samples for call in sample["calls"]]
            if not calls:
                continue
            arm_comparisons = []
            round_reductions = []
            for run, sample in zip(arm_runs, samples):
                baseline_run = by_round[(run["round"], "baseline")]
                baseline = next(item for item in baseline_run["samples"] if item["name"] == name)
                compare = _compare(baseline["reference"], sample["reference"])
                entry = {"round": run["round"], "sample": name, "policy": policy, **compare}
                comparisons.append(entry)
                actuals = [sample["reference"]] + [call["actual"] for call in sample["calls"] if "actual" in call]
                arm_comparisons.extend(_compare(baseline["reference"], actual) for actual in actuals)
                baseline_round_mean = float(np.mean([call["total_ms"] for call in baseline["calls"]]))
                arm_round_mean = float(np.mean([call["total_ms"] for call in sample["calls"]]))
                round_reductions.append((run["round"], (1 - arm_round_mean / baseline_round_mean) * 100))
                for call in sample["calls"]:
                    calls_csv.append({"round": run["round"], "policy": policy, "sample": name,
                                      "iteration": call["iteration"], "total_ms": call["total_ms"],
                                      "paired_order": call.get("paired_order"),
                                      "cpu_ms": call["cpu_ms"], "reference_equal": call["reference_equal"],
                                      "recognition_batch_count": call["geometry"]["recognition_batch_count"],
                                      "recognition_input_elements": call["geometry"]["recognition_input_elements"],
                                      "recognition_padding_percent": call["geometry"]["recognition_padding_percent"],
                                      "recognition_shapes": json.dumps([batch["input_shape"] for batch in
                                                                       call["geometry"]["recognition_batches"]]),
                                      **{stage + "_ms": duration for stage, duration in call["stages_ms"].items()}})
            mean = float(np.mean([call["total_ms"] for call in calls]))
            rec_mean = float(np.mean([call["stages_ms"]["rec_inference"] for call in calls]))
            row = {"sample": name, "policy": policy, "n": len(calls), "rounds": len(samples),
                   "mean_ms": mean, "p50_ms": float(np.median([call["total_ms"] for call in calls])),
                   "p95_ms": float(np.percentile([call["total_ms"] for call in calls], 95)),
                   "total_reduction_percent": (1 - mean / baseline_mean) * 100,
                   "rec_inference_mean_ms": rec_mean,
                   "rec_inference_reduction_percent": (1 - rec_mean / baseline_rec_mean) * 100,
                   "batch_count_mean": float(np.mean([call["geometry"]["recognition_batch_count"] for call in calls])),
                   "input_elements_mean": float(np.mean([call["geometry"]["recognition_input_elements"] for call in calls])),
                   "padding_percent_mean": float(np.mean([call["geometry"]["recognition_padding_percent"] for call in calls])),
                   "stable_within_workers": all(sample["stable_within_worker"] for sample in samples),
                   "stable_across_rounds": all(sample["reference"] == samples[0]["reference"] for sample in samples),
                   **{key: all(compare[key] for compare in arm_comparisons) for key in
                      ("text_equal", "count_equal", "polygons_equal", "centers_equal", "rects_equal", "confidence_equal",
                       "confidence_within_1e_6", "confidence_within_1e_4")},
                   "max_abs_confidence_delta": max(compare["max_abs_confidence_delta"] for compare in arm_comparisons),
                   "threshold_crossing_count": sum(len(compare["threshold_crossings"]) for compare in arm_comparisons)}
            for group, stages in GROUPS.items():
                row[group + "_mean_ms"] = float(np.mean([sum(call["stages_ms"][stage] for stage in stages)
                                                           for call in calls]))
            for sample, run in zip(samples, arm_runs):
                row[f"round_{run['round']}_mean_ms"] = float(np.mean([call["total_ms"] for call in sample["calls"]]))
            for round_number, reduction in round_reductions:
                row[f"round_{round_number}_total_reduction_percent"] = reduction
            rows.append(row)
    _save(output / "summary.json", {"run_paths": [str(path.relative_to(ROOT)) for path in run_paths],
                                   "experiment": next(iter(experiments)),
                                   "policies": POLICIES, "thresholds": THRESHOLDS,
                                   "accuracy_scope": "Legacy output agreement; not labeled accuracy.",
                                   "rows": rows, "comparisons": comparisons})
    for filename, records in (("summary.csv", rows), ("calls.csv", calls_csv)):
        fields = list(dict.fromkeys(key for record in records for key in record))
        with (output / filename).open("w", encoding="utf-8-sig", newline="") as handle:
            writer = csv.DictWriter(handle, fieldnames=fields)
            writer.writeheader()
            writer.writerows(records)
    for row in rows:
        print(f"{row['sample']}/{row['policy']}: mean={row['mean_ms']:.2f} ms, "
              f"reduction={row['total_reduction_percent']:.1f}%, text_equal={row['text_equal']}, "
              f"max score delta={row['max_abs_confidence_delta']:.6f}", flush=True)
    print(f"Results: {output / 'summary.csv'}", flush=True)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output", type=Path, default=Path(".pytest_tmp/ocr-recognition-ablation"))
    parser.add_argument("--rounds", type=int, default=2)
    parser.add_argument("--iterations", type=int, default=20)
    parser.add_argument("--warmup", type=int, default=3)
    parser.add_argument("--summarize-only", action="store_true")
    parser.add_argument("--paired", action="store_true", help="Interleave all four policies over shared sessions.")
    parser.add_argument("--paired-worker", action="store_true", help=argparse.SUPPRESS)
    parser.add_argument("--worker-output", type=Path, help=argparse.SUPPRESS)
    parser.add_argument("--round", type=int, default=0, help=argparse.SUPPRESS)
    parser.add_argument("--policy", choices=POLICIES, default="baseline", help=argparse.SUPPRESS)
    args = parser.parse_args()
    if min(args.rounds, args.iterations, args.warmup) < 1 or args.round < 0:
        parser.error("Rounds, iterations and warmup must be positive; worker round must be nonnegative.")
    output = _repo_path(args.output)
    output.mkdir(parents=True, exist_ok=True)
    if args.paired_worker:
        os.environ.update({key: value for key, value in _environment(output).items()
                           if key in {"TEMP", "TMP", "TMPDIR"}})
        _paired_worker(args, output)
        return
    if args.worker_output:
        args.worker_output = _repo_path(args.worker_output)
        os.environ.update({key: value for key, value in _environment(output).items()
                           if key in {"TEMP", "TMP", "TMPDIR"}})
        _worker(args)
        return
    if args.summarize_only:
        _summarize(output, sorted(output.glob("run-*-*.json")))
        return
    run_paths = []
    for round_number in range(args.rounds):
        if args.paired:
            print(f"Paired round {round_number + 1}/{args.rounds}; shared CPU8/spinning-off sessions", flush=True)
            _run_worker([sys.executable, "-m", "tools.benchmark_ocr_recognition", "--output", str(output),
                         "--paired-worker", "--round", str(round_number),
                         "--iterations", str(args.iterations), "--warmup", str(args.warmup)], output)
            run_paths.extend(output / f"run-{round_number}-{policy}.json" for policy in POLICIES)
            continue
        policies = list(POLICIES)
        random.Random(20261023 + round_number).shuffle(policies)
        for policy in policies:
            destination = output / f"run-{round_number}-{policy}.json"
            print(f"Round {round_number + 1}/{args.rounds}, {policy}; CPU8/spinning off", flush=True)
            _run_worker([sys.executable, "-m", "tools.benchmark_ocr_recognition", "--output", str(output),
                         "--worker-output", str(destination), "--round", str(round_number),
                         "--policy", policy, "--iterations", str(args.iterations),
                         "--warmup", str(args.warmup)], output)
            run_paths.append(destination)
    _summarize(output, run_paths)


if __name__ == "__main__":
    main()
