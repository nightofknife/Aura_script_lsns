"""Paired orientation on/off timing on real fixtures with shared OCR sessions.

Run from the repository root. Each round is a fresh sequential worker; both
arms share the same loaded, fully warmed pipeline and alternate AB/BA order.
Results describe output agreement, not accuracy against manual ground truth.
"""
from __future__ import annotations

import argparse
import asyncio
import csv
import hashlib
import json
import os
from pathlib import Path
import platform
import random
import sys
import time
from unittest.mock import patch

import cv2
import numpy as np
from PIL import Image
import psutil

from plans.aura_base.src.services._ocr_timing import OcrTiming
from plans.aura_base.src.services.ocr_onnx_pipeline import OnnxOcrPipeline
from plans.aura_base.src.services.ocr_service import OcrService
from tools.benchmark_ocr_cpu import SAMPLES
from tools.benchmark_ocr_recognition import _compare, _fingerprint, _geometry, _repeat_ranges
from tools.benchmark_ocr_stages import (
    ROOT, GROUPS, _Config, _environment, _repo_path, _run_worker,
    _save, _session_options, _sha256,
)


ARMS = {"on": True, "off": False}
SOURCE_PATHS = (
    "tools/benchmark_ocr_orientation.py", "tools/benchmark_ocr_recognition.py",
    "tools/benchmark_ocr_stages.py", "tools/benchmark_ocr_cpu.py",
    "plans/aura_base/src/services/ocr_service.py",
    "plans/aura_base/src/services/ocr_onnx_pipeline.py",
    "plans/aura_base/src/services/_ocr_recognition.py",
    "plans/aura_base/src/services/_ocr_timing.py",
)


def _assert_stage_calls(snapshot: dict, *, recognition_only: bool, orientation: bool):
    calls = snapshot["stage_calls"]
    if calls["rec_inference"] < 1:
        raise RuntimeError("Sample did not execute recognition.")
    if recognition_only:
        if calls["det_inference"] or any(calls[stage] for stage in GROUPS["orientation"]):
            raise RuntimeError("Recognition-only unexpectedly executed detection/orientation.")
    else:
        if calls["det_inference"] < 1:
            raise RuntimeError("Disabling orientation also disabled detection.")
        if orientation:
            if calls["ori_inference"] < 1:
                raise RuntimeError("Enabled orientation did not execute its model.")
        elif any(calls[stage] for stage in GROUPS["orientation"]):
            raise RuntimeError("Disabled orientation still executed orientation stages.")


def _worker(args, output: Path):
    import onnxruntime as ort

    service = OcrService(config=_Config())
    warmup_runs = []
    original_warmup = OnnxOcrPipeline.warmup

    def observe_warmup(pipeline):
        stages = original_warmup(pipeline)
        warmup_runs.append(list(stages))
        return stages

    started = time.perf_counter_ns()
    with patch.object(OnnxOcrPipeline, "warmup", observe_warmup):
        asyncio.run(service._preload_engine_async(warmup=True))
    startup_ms = (time.perf_counter_ns() - started) / 1e6
    pipeline = service._engine
    if pipeline is None or service._warmed_engine is not pipeline or len(warmup_runs) != 1:
        raise RuntimeError("Startup did not warm the one shared OCR pipeline exactly once.")
    warmed = warmup_runs[0]
    if ("det" not in warmed or not any(stage.startswith("rec(") for stage in warmed)
            or not any(stage.startswith("textline_orientation(") for stage in warmed)):
        raise RuntimeError(f"Startup did not warm every enabled model: {warmed}")
    if pipeline.sessions.doc_orientation is not None and "doc_orientation" not in warmed:
        raise RuntimeError("Document orientation session was not warmed.")
    if (pipeline.recognition_options.width_mode, pipeline.recognition_options.batch_mode) != ("legacy", "adaptive"):
        raise RuntimeError("Benchmark requires production default legacy width/adaptive batching.")
    sessions = {name: _session_options(session) for name, session in vars(pipeline.sessions).items()
                if session is not None}
    for name, options in sessions.items():
        if (options["intra_op_num_threads"], options["intra_op_allow_spinning"],
                options["inter_op_allow_spinning"]) != (8, "0", "0"):
            raise RuntimeError(f"Unexpected CPU session settings: {name}: {options}")
    bundle, metadata_path = service._resolve_bundle_paths()
    artifacts = {"metadata": metadata_path, "det": bundle / pipeline.metadata.models.det,
                 "rec": bundle / pipeline.metadata.models.rec}
    for name in ("textline_orientation", "doc_orientation"):
        if getattr(pipeline.sessions, name) is not None:
            artifacts[name] = bundle / getattr(pipeline.metadata.models, name)
    result = {
        "round": args.round, "pid": os.getpid(), "model": service.get_model(),
        "provider": service.get_provider(), "ort": ort.__version__, "python": sys.version,
        "numpy": np.__version__, "opencv": cv2.__version__, "opencv_threads": cv2.getNumThreads(),
        "platform": platform.platform(), "processor": platform.processor(),
        "physical_cores": psutil.cpu_count(logical=False), "logical_cpus": os.cpu_count(),
        "startup_load_and_warmup_ms": startup_ms, "startup_warmup_stages": warmed,
        "startup_warmup_calls": len(warmup_runs), "sessions": sessions,
        "recognition_options": vars(pipeline.recognition_options),
        "sample_warmup": args.warmup, "paired_iterations": args.iterations,
        "timing_scope": "Shared pipeline prediction + service result parsing, with stage timers and recognition "
                        "shape diagnostics; excludes capture, decoding, startup, sample warmup, comparison and serialization.",
        "accuracy_scope": "On/off agreement, no manually checked ground truth; a rotated fixture is a derived diagnostic.",
        "source_sha256": {relative: _sha256(ROOT / relative) for relative in SOURCE_PATHS},
        "model_artifacts": {name: {"path": str(path.relative_to(ROOT)), "sha256": _sha256(path),
                                   "bytes": path.stat().st_size} for name, path in artifacts.items()},
        "samples": [],
    }
    samples = [(name, relative, crop, rec_only, "none") for name, relative, crop, rec_only in SAMPLES]
    if args.include_rotated:
        _, relative, crop, _ = next(sample for sample in SAMPLES if sample[0] == "small_full")
        samples.append(("small_full_rotated_180", relative, crop, False, "rotate_180"))
    random.Random(20261031 + args.round).shuffle(samples)
    process = psutil.Process()
    result["rss_after_startup_mib"] = process.memory_info().rss / 1024 ** 2
    for name, relative, crop, recognition_only, transform in samples:
        path = ROOT / relative
        with Image.open(path) as source:
            image = np.asarray(source.convert("RGB"))
        if crop:
            x, y, width, height = crop
            image = image[y:y + height, x:x + width].copy()
        if transform == "rotate_180":
            image = np.ascontiguousarray(np.rot90(image, 2))

        def predict(arm=None, timing=None):
            kwargs = {"recognition_only": recognition_only}
            if arm is not None:
                kwargs["use_textline_orientation"] = ARMS[arm]
            if timing is not None:
                kwargs["timing"] = timing
            raw = pipeline.predict(image, **kwargs)
            if timing is None:
                return _fingerprint(raw, service._parse_results(raw))
            with timing.measure("result_parse"):
                parsed = service._parse_results(raw)
            return raw, parsed

        references = {}
        for _ in range(args.warmup):
            for arm in ARMS:
                references[arm] = predict(arm)
        if predict() != references["on"]:
            raise RuntimeError(f"Omitted orientation flag differs from explicit True: {name}")
        sample = {
            "name": name, "path": relative, "sha256": _sha256(path), "crop_xywh": crop,
            "transform": transform, "shape": list(image.shape),
            "image_bytes_sha256": hashlib.sha256(image.tobytes()).hexdigest(),
            "recognition_only": recognition_only, "default_equals_explicit_true": True,
            "on_off_comparison": _compare(references["on"], references["off"]),
            "arms": {arm: {"reference": references[arm], "stable_within_worker": True, "calls": []}
                     for arm in ARMS},
        }
        for iteration in range(args.iterations):
            order = ["on", "off"] if (iteration + args.round) % 2 == 0 else ["off", "on"]
            for slot, arm in enumerate(order):
                timing = OcrTiming()
                cpu_started, started = time.process_time_ns(), time.perf_counter_ns()
                raw, parsed = predict(arm, timing)
                elapsed = (time.perf_counter_ns() - started) / 1e6
                cpu_ms = (time.process_time_ns() - cpu_started) / 1e6
                actual = _fingerprint(raw, parsed)
                snapshot = timing.snapshot(total_ms=elapsed)
                _assert_stage_calls(snapshot, recognition_only=recognition_only, orientation=ARMS[arm])
                equal = actual == references[arm]
                arm_result = sample["arms"][arm]
                arm_result["stable_within_worker"] &= equal
                call = {"iteration": iteration, "paired_order": slot, "paired_arm_order": order,
                        "cpu_ms": cpu_ms, **snapshot, "geometry": _geometry(snapshot["recognition_batches"]),
                        "reference_equal": equal}
                if not equal:
                    call["actual"] = actual
                    call["reference_comparison"] = _compare(references[arm], actual)
                arm_result["calls"].append(call)
        for arm, arm_result in sample["arms"].items():
            arm_result["repeat_region_ranges"] = _repeat_ranges(arm_result)
            print(f"  {name}/{arm}: regions={len(arm_result['reference']['parsed'])}, "
                  f"mean={np.mean([call['total_ms'] for call in arm_result['calls']]):.2f} ms", flush=True)
        result["samples"].append(sample)
    if service._engine is not pipeline or service._warmed_engine is not pipeline:
        raise RuntimeError("Benchmark changed the shared engine or warmup state.")
    result["rss_after_run_mib"] = process.memory_info().rss / 1024 ** 2
    _save(output / f"run-{args.round}.json", result)


def _summarize(output: Path, paths: list[Path]):
    runs = [json.loads(path.read_text(encoding="utf-8")) for path in paths]
    if not runs:
        raise ValueError("No worker results found.")
    for run in runs[1:]:
        for key in ("source_sha256", "model_artifacts", "sessions", "ort", "opencv_threads", "recognition_options"):
            if run[key] != runs[0][key]:
                raise RuntimeError(f"Environment changed across workers: {key}")
        if {sample["name"] for sample in run["samples"]} != {sample["name"] for sample in runs[0]["samples"]}:
            raise RuntimeError("Sample set changed across workers.")
    rows, comparisons, call_records = [], [], []
    for first_sample in runs[0]["samples"]:
        name = first_sample["name"]
        samples = [next(sample for sample in run["samples"] if sample["name"] == name) for run in runs]
        comparisons.extend({"round": run["round"], "sample": name, **sample["on_off_comparison"]}
                           for run, sample in zip(runs, samples))
        baseline_mean = float(np.mean([call["total_ms"] for sample in samples for call in sample["arms"]["on"]["calls"]]))
        for arm in ARMS:
            arm_samples = [sample["arms"][arm] for sample in samples]
            calls = [call for sample in arm_samples for call in sample["calls"]]
            mean = float(np.mean([call["total_ms"] for call in calls]))
            compared = []
            for sample in samples:
                candidate = sample["arms"][arm]
                compared.append(_compare(sample["arms"]["on"]["reference"], candidate["reference"]))
                compared.extend(_compare(sample["arms"]["on"]["reference"], call["actual"])
                                for call in candidate["calls"] if "actual" in call)
            row = {"sample": name, "arm": arm, "n": len(calls), "mean_ms": mean,
                   "p50_ms": float(np.median([call["total_ms"] for call in calls])),
                   "p95_ms": float(np.percentile([call["total_ms"] for call in calls], 95)),
                   "total_reduction_percent": (1 - mean / baseline_mean) * 100,
                   "stable_within_workers": all(sample["stable_within_worker"] for sample in arm_samples),
                   "stable_across_rounds": all(sample["reference"] == arm_samples[0]["reference"] for sample in arm_samples),
                   **{key: all(compare[key] for compare in compared) for key in (
                       "exact_equal", "text_equal", "count_equal", "polygons_equal", "centers_equal",
                       "rects_equal", "confidence_equal", "confidence_within_1e_6", "confidence_within_1e_4")},
                   "max_abs_confidence_delta": max(compare["max_abs_confidence_delta"] for compare in compared),
                   "threshold_crossing_count": sum(len(compare["threshold_crossings"]) for compare in compared)}
            for group, stages in GROUPS.items():
                row[group + "_mean_ms"] = float(np.mean([sum(call["stages_ms"][stage] for stage in stages) for call in calls]))
            for run, sample, arm_sample in zip(runs, samples, arm_samples):
                on_mean = float(np.mean([call["total_ms"] for call in sample["arms"]["on"]["calls"]]))
                round_mean = float(np.mean([call["total_ms"] for call in arm_sample["calls"]]))
                row[f"round_{run['round']}_mean_ms"] = round_mean
                row[f"round_{run['round']}_reduction_percent"] = (1 - round_mean / on_mean) * 100
                for call in arm_sample["calls"]:
                    call_records.append({"round": run["round"], "sample": name, "arm": arm,
                                         "iteration": call["iteration"], "paired_order": call["paired_order"],
                                         "total_ms": call["total_ms"], "cpu_ms": call["cpu_ms"],
                                         "reference_equal": call["reference_equal"],
                                         "recognition_shapes": json.dumps([batch["input_shape"] for batch in call["recognition_batches"]]),
                                         **{stage + "_ms": ms for stage, ms in call["stages_ms"].items()}})
            rows.append(row)
    _save(output / "summary.json", {"run_paths": [str(path.relative_to(ROOT)) for path in paths],
                                   "experiment": "paired_shared_pipeline_balanced_AB_BA",
                                   "accuracy_scope": "On/off output agreement, not manually labeled accuracy.",
                                   "rows": rows, "comparisons": comparisons})
    for filename, records in (("summary.csv", rows), ("calls.csv", call_records)):
        fields = list(dict.fromkeys(key for record in records for key in record))
        with (output / filename).open("w", encoding="utf-8-sig", newline="") as handle:
            writer = csv.DictWriter(handle, fieldnames=fields)
            writer.writeheader()
            writer.writerows(records)
    for row in rows:
        print(f"{row['sample']}/{row['arm']}: mean={row['mean_ms']:.2f} ms, "
              f"reduction={row['total_reduction_percent']:.1f}%, text_equal={row['text_equal']}, "
              f"max score delta={row['max_abs_confidence_delta']:.6f}", flush=True)
    _save(output / "artifact-sha256.json", {str(path.relative_to(output)): _sha256(path)
                                          for path in [*paths, output / "summary.json", output / "summary.csv", output / "calls.csv"]})


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output", type=Path, default=Path(".pytest_tmp/ocr-orientation-benchmark"))
    parser.add_argument("--rounds", type=int, default=2)
    parser.add_argument("--iterations", type=int, default=20)
    parser.add_argument("--warmup", type=int, default=3)
    parser.add_argument("--include-rotated", action="store_true", help="Also derive a 180-degree diagnostic from the small real fixture.")
    parser.add_argument("--summarize-only", action="store_true")
    parser.add_argument("--worker", action="store_true", help=argparse.SUPPRESS)
    parser.add_argument("--round", type=int, default=0, help=argparse.SUPPRESS)
    args = parser.parse_args()
    if min(args.rounds, args.iterations, args.warmup) < 1 or args.round < 0:
        parser.error("Rounds, iterations and warmup must be positive; worker round must be nonnegative.")
    output = _repo_path(args.output)
    output.mkdir(parents=True, exist_ok=True)
    if args.worker:
        os.environ.update({key: value for key, value in _environment(output).items()
                           if key in {"TEMP", "TMP", "TMPDIR"}})
        _worker(args, output)
        return
    if args.summarize_only:
        _summarize(output, sorted(output.glob("run-*.json")))
        return
    paths = []
    for round_number in range(args.rounds):
        print(f"Paired round {round_number + 1}/{args.rounds}; shared CPU8/spinning-off/default recognition", flush=True)
        command = [sys.executable, "-m", "tools.benchmark_ocr_orientation", "--output", str(output),
                   "--worker", "--round", str(round_number), "--iterations", str(args.iterations),
                   "--warmup", str(args.warmup)]
        if args.include_rotated:
            command.append("--include-rotated")
        _run_worker(command, output)
        paths.append(output / f"run-{round_number}.json")
    _summarize(output, paths)


if __name__ == "__main__":
    main()
