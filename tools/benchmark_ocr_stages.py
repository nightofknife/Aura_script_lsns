"""Profile OCR stages on real fixture images in sequential isolated workers.

Run from the repository root with ``python -m tools.benchmark_ocr_stages``.
Capture and image decoding are excluded; each measured call includes pipeline
prediction and the service's public-result parsing.
"""
from __future__ import annotations

import argparse
import asyncio
import csv
from dataclasses import asdict
import hashlib
import json
import os
from pathlib import Path
import platform
import random
import subprocess
import sys
import time

import cv2
import numpy as np
from PIL import Image
import psutil

from plans.aura_base.src.services.ocr_service import OcrService
from tools.benchmark_ocr_cpu import SAMPLES


ROOT = Path(__file__).resolve().parents[1]
GROUPS = {
    "detection": ("det_preprocess", "det_inference"),
    "orientation": ("ori_preprocess", "ori_inference", "ori_postprocess"),
    "recognition": ("rec_preprocess", "rec_inference"),
    "postprocess": ("det_postprocess", "crop", "rec_decode", "result_pack", "result_parse"),
    "other": ("image_prepare",),
}


class _Config:
    def __init__(self) -> None:
        self.values = {
            "ocr.models_root": str(ROOT / "models/ocr"),
            "ocr.default_model": "ppocrv6_small",
            "ocr.execution_provider": "cpu",
            "ocr.session.intra_op_num_threads": 8,
            "ocr.session.allow_spinning": False,
        }

    def get(self, key, default=None):
        return self.values.get(key, default)


def _save(path: Path, value) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(value, ensure_ascii=False, indent=2), encoding="utf-8")


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _json_value(value):
    if isinstance(value, np.ndarray):
        return value.tolist()
    if isinstance(value, np.generic):
        return value.item()
    if isinstance(value, dict):
        return {key: _json_value(item) for key, item in value.items()}
    if isinstance(value, (list, tuple)):
        return [_json_value(item) for item in value]
    return value


def _fingerprint(raw, parsed):
    return {"raw": _json_value(raw), "parsed": _json_value([asdict(item) for item in parsed])}


def _session_options(session):
    options = session.get_session_options()
    return {
        "intra_op_num_threads": options.intra_op_num_threads,
        "inter_op_num_threads": options.inter_op_num_threads,
        "intra_op_allow_spinning": options.get_session_config_entry("session.intra_op.allow_spinning"),
        "inter_op_allow_spinning": options.get_session_config_entry("session.inter_op.allow_spinning"),
        "execution_mode": str(options.execution_mode),
        "graph_optimization_level": str(options.graph_optimization_level),
        "providers": session.get_providers(),
    }


def _repo_path(value: Path) -> Path:
    path = (ROOT / value).resolve()
    path.relative_to(ROOT)
    return path


def _environment(output: Path) -> dict[str, str]:
    temporary = output / "tmp"
    temporary.mkdir(parents=True, exist_ok=True)
    return {**os.environ, "TEMP": str(temporary), "TMP": str(temporary), "TMPDIR": str(temporary)}


def _run_worker(command: list[str], output: Path) -> None:
    with subprocess.Popen(command, cwd=ROOT, env=_environment(output)) as worker:
        try:
            returncode = worker.wait(timeout=600)
        except BaseException:
            worker.terminate()
            try:
                worker.wait(timeout=10)
            except subprocess.TimeoutExpired:
                worker.kill()
                worker.wait()
            raise
    if returncode:
        raise subprocess.CalledProcessError(returncode, command)


def _worker(args) -> None:
    import onnxruntime as ort

    from plans.aura_base.src.services._ocr_timing import OcrTiming

    process = psutil.Process()
    config = _Config()
    service = OcrService(config=config)
    begin = time.perf_counter_ns()
    asyncio.run(service._preload_engine_async(warmup=True))
    startup_ms = (time.perf_counter_ns() - begin) / 1e6
    pipeline = service._engine
    if pipeline is None:
        raise RuntimeError("OCR startup returned without a loaded pipeline.")
    bundle, metadata_path = service._resolve_bundle_paths()
    model_paths = {
        "metadata": metadata_path,
        "detection": bundle / pipeline.metadata.models.det,
        "recognition": bundle / pipeline.metadata.models.rec,
    }
    if pipeline.sessions.textline_orientation is not None:
        model_paths["orientation"] = bundle / pipeline.metadata.models.textline_orientation
    if pipeline.sessions.doc_orientation is not None:
        model_paths["doc_orientation"] = bundle / pipeline.metadata.models.doc_orientation
    sessions = {
        name: _session_options(session)
        for name, session in vars(pipeline.sessions).items() if session is not None
    }
    for name, options in sessions.items():
        if options["intra_op_num_threads"] != 8 or options["intra_op_allow_spinning"] != "0":
            raise RuntimeError(f"Unexpected benchmark CPU configuration for {name}: {options}")
    result = {
        "round": args.round, "pid": os.getpid(), "model": service.get_model(),
        "provider": service.get_provider(), "ort": ort.__version__, "python": sys.version,
        "numpy": np.__version__, "opencv": cv2.__version__, "opencv_threads": cv2.getNumThreads(),
        "physical_cores": psutil.cpu_count(logical=False), "logical_cpus": os.cpu_count(),
        "processor": platform.processor(), "platform": platform.platform(),
        "startup_load_and_warmup_ms": startup_ms,
        "process_threads_after_startup": process.num_threads(),
        "rss_after_startup_mib": process.memory_info().rss / 1024 ** 2,
        "sample_warmup": args.warmup, "paired_iterations": args.iterations,
        "timing_scope": "pipeline prediction + service result parsing; excludes capture, image decoding, "
                        "session loading/warmup, async bridge, fingerprinting and timing report construction",
        "comparison": "Exact equality of all raw fields and parsed dataclass fields on every measured call; "
                      "includes text, confidence, polygons, rectangles and centers",
        "sessions": sessions, "group_definitions": GROUPS,
        "model_artifacts": {
            name: {"path": str(path.relative_to(ROOT)), "sha256": _sha256(path), "bytes": path.stat().st_size}
            for name, path in model_paths.items()
        },
        "source_sha256": {
            relative: _sha256(ROOT / relative) for relative in (
                "plans/aura_base/src/services/ocr_service.py",
                "plans/aura_base/src/services/ocr_onnx_pipeline.py",
                "plans/aura_base/src/services/_ocr_timing.py",
                "tools/benchmark_ocr_stages.py",
            )
        },
        "samples": [],
    }
    sample_order = list(SAMPLES)
    random.Random(20261022 + args.round).shuffle(sample_order)
    for name, relative, crop, recognition_only in sample_order:
        path = ROOT / relative
        with Image.open(path) as source:
            image = np.asarray(source.convert("RGB"))
        if crop:
            x, y, width, height = crop
            image = image[y:y + height, x:x + width].copy()

        def predict(timing=None):
            if timing is None:
                raw = pipeline.predict(image, recognition_only=recognition_only)
                return raw, service._parse_results(raw)
            raw = pipeline.predict(image, recognition_only=recognition_only, timing=timing)
            with timing.measure("result_parse"):
                parsed = service._parse_results(raw)
            return raw, parsed

        for _ in range(args.warmup):
            raw, parsed = predict()
        reference = _fingerprint(raw, parsed)
        if not parsed:
            raise RuntimeError(f"Empty OCR workload: {name}")
        sample = {
            "name": name, "path": relative, "sha256": _sha256(path),
            "image_bytes_sha256": hashlib.sha256(image.tobytes()).hexdigest(),
            "crop_xywh": crop, "shape": list(image.shape), "recognition_only": recognition_only,
            "region_count": len(parsed), "reference": reference, "all_results_equal": True,
            "calls": [],
        }
        for iteration in range(args.iterations):
            # Alternate order to reduce systematic first/second-call bias.
            modes = (False, True) if (iteration + args.round) % 2 == 0 else (True, False)
            for instrumented in modes:
                timing = OcrTiming() if instrumented else None
                cpu_begin = time.process_time_ns()
                begin = time.perf_counter_ns()
                raw, parsed = predict(timing)
                total_ms = (time.perf_counter_ns() - begin) / 1e6
                cpu_ms = (time.process_time_ns() - cpu_begin) / 1e6
                equal = _fingerprint(raw, parsed) == reference
                if not equal:
                    failure = {
                        "sample": name, "iteration": iteration, "instrumented": instrumented,
                        "reference": reference, "actual": _fingerprint(raw, parsed),
                    }
                    _save(args.worker_output.with_suffix(".mismatch.json"), failure)
                    raise RuntimeError(f"OCR output changed: sample={name}, iteration={iteration}, timed={instrumented}")
                call = {
                    "iteration": iteration, "instrumented": instrumented,
                    "order_in_pair": modes.index(instrumented), "total_ms": total_ms,
                    "cpu_ms": cpu_ms, "result_equal": equal,
                }
                if timing is not None:
                    call.update(timing.snapshot(total_ms=total_ms))
                    accounted = sum(call["stages_ms"].values())
                    if accounted > total_ms + 0.01:
                        raise RuntimeError(f"Overlapping OCR stage timers: {accounted} > {total_ms} ms")
                sample["calls"].append(call)
        result["samples"].append(sample)
        timed = [call["total_ms"] for call in sample["calls"] if call["instrumented"]]
        print(f"  {name}: regions={sample['region_count']}, timed P50={np.median(timed):.2f} ms", flush=True)
    result["process_threads_after_run"] = process.num_threads()
    result["rss_after_run_mib"] = process.memory_info().rss / 1024 ** 2
    _save(args.worker_output, result)


def _summarize(output: Path, run_paths: list[Path]) -> None:
    runs = [json.loads(path.read_text(encoding="utf-8")) for path in run_paths]
    if not runs:
        raise ValueError(f"No benchmark workers found under {output}")
    for run in runs[1:]:
        for key in ("model_artifacts", "source_sha256", "sessions", "ort", "opencv_threads"):
            if run[key] != runs[0][key]:
                raise RuntimeError(f"Benchmark environment changed across workers: {key}")
    rows = []
    for name, *_ in SAMPLES:
        samples = [next(sample for sample in run["samples"] if sample["name"] == name) for run in runs]
        reference = samples[0]["reference"]
        if any(sample["reference"] != reference for sample in samples):
            raise RuntimeError(f"OCR output changed across worker processes: {name}")
        timed = [call for sample in samples for call in sample["calls"] if call["instrumented"]]
        baseline = [call for sample in samples for call in sample["calls"] if not call["instrumented"]]
        total_mean = float(np.mean([call["total_ms"] for call in timed]))
        baseline_mean = float(np.mean([call["total_ms"] for call in baseline]))
        other_mean = float(np.mean([call["other_ms"] for call in timed]))
        row = {
            "sample": name, "shape": "x".join(str(size) for size in samples[0]["shape"][:2]),
            "recognition_only": samples[0]["recognition_only"], "region_count": samples[0]["region_count"],
            "rounds": len(runs), "n_per_mode": len(timed),
            "mean_ms": total_mean, "p50_ms": float(np.median([call["total_ms"] for call in timed])),
            "p95_ms": float(np.percentile([call["total_ms"] for call in timed], 95)),
            "baseline_mean_ms": baseline_mean,
            "baseline_p50_ms": float(np.median([call["total_ms"] for call in baseline])),
            "baseline_p95_ms": float(np.percentile([call["total_ms"] for call in baseline], 95)),
            "instrumentation_mean_delta_ms": total_mean - baseline_mean,
            "instrumentation_mean_delta_percent": (total_mean / baseline_mean - 1) * 100,
            "cpu_mean_ms": float(np.mean([call["cpu_ms"] for call in timed])),
            "all_results_equal": all(sample["all_results_equal"] for sample in samples),
        }
        leaf_means = {
            stage: float(np.mean([call["stages_ms"][stage] for call in timed]))
            for stage in timed[0]["stages_ms"]
        }
        for group, stages in GROUPS.items():
            duration = sum(leaf_means[stage] for stage in stages) + (other_mean if group == "other" else 0)
            row[f"{group}_mean_ms"] = duration
            row[f"{group}_share_percent"] = duration / total_mean * 100
        for stage, duration in leaf_means.items():
            row[f"{stage}_mean_ms"] = duration
            row[f"{stage}_share_percent"] = duration / total_mean * 100
        row["unattributed_mean_ms"] = other_mean
        share_sum = sum(row[f"{group}_share_percent"] for group in GROUPS)
        if abs(share_sum - 100) > 0.01:
            raise RuntimeError(f"Stage shares do not account for total: {name}={share_sum}")
        rows.append(row)
    _save(output / "summary.json", {
        "run_paths": [str(path.relative_to(ROOT)) for path in run_paths],
        "group_definitions": GROUPS, "other_includes": "image_prepare + untimed orchestration/timer overhead",
        "share_method": "sum of stage durations / sum of instrumented total durations; non-overlapping groups sum to 100%",
        "rows": rows,
    })
    with (output / "summary.csv").open("w", encoding="utf-8-sig", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=list(rows[0]))
        writer.writeheader()
        writer.writerows(rows)
    call_rows = []
    for run in runs:
        for sample in run["samples"]:
            for call in sample["calls"]:
                call_rows.append({
                    "round": run["round"], "sample": sample["name"],
                    **{key: value for key, value in call.items() if key != "stages_ms"},
                    **{f"{stage}_ms": value for stage, value in call.get("stages_ms", {}).items()},
                })
    fields = list(dict.fromkeys(key for row in call_rows for key in row))
    with (output / "calls.csv").open("w", encoding="utf-8-sig", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=fields)
        writer.writeheader()
        writer.writerows(call_rows)
    for row in rows:
        shares = ", ".join(f"{group}={row[f'{group}_share_percent']:.1f}%" for group in GROUPS)
        print(f"{row['sample']}: P50={row['p50_ms']:.2f} ms, P95={row['p95_ms']:.2f} ms; {shares}", flush=True)
    print(f"Results: {output / 'summary.csv'}", flush=True)


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output", type=Path, default=Path(".pytest_tmp/ocr-stage-benchmark"))
    parser.add_argument("--rounds", type=int, default=2)
    parser.add_argument("--iterations", type=int, default=20)
    parser.add_argument("--warmup", type=int, default=3)
    parser.add_argument("--summarize-only", action="store_true")
    parser.add_argument("--worker-output", type=Path, help=argparse.SUPPRESS)
    parser.add_argument("--round", type=int, default=0, help=argparse.SUPPRESS)
    args = parser.parse_args()
    if min(args.rounds, args.iterations, args.warmup) < 1 or args.round < 0:
        parser.error("Rounds, iterations and warmup must be positive; worker round must be nonnegative.")
    output = _repo_path(args.output)
    output.mkdir(parents=True, exist_ok=True)
    if args.worker_output:
        args.worker_output = _repo_path(args.worker_output)
        os.environ.update({key: value for key, value in _environment(output).items() if key in {"TEMP", "TMP", "TMPDIR"}})
        _worker(args)
        return
    if args.summarize_only:
        paths = [path for path in output.glob("run-*.json") if path.stem.removeprefix("run-").isdigit()]
        _summarize(output, sorted(paths))
        return
    run_paths = []
    for round_number in range(args.rounds):
        destination = output / f"run-{round_number}.json"
        print(f"Round {round_number + 1}/{args.rounds}: CPU threads=8, spinning=off", flush=True)
        command = [
            sys.executable, "-m", "tools.benchmark_ocr_stages", "--worker-output", str(destination),
            "--output", str(output), "--round", str(round_number),
            "--iterations", str(args.iterations), "--warmup", str(args.warmup),
        ]
        _run_worker(command, output)
        run_paths.append(destination)
    _summarize(output, run_paths)


if __name__ == "__main__":
    main()
