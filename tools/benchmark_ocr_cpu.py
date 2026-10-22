"""Compare OCR CPU thread counts and spinning with isolated, sequential workers."""
from __future__ import annotations

import argparse
import csv
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

from packages.aura_core.services.onnx_runtime_backend import OnnxRuntimeBackend
from plans.aura_base.src.services.ocr_contract import load_ocr_metadata
from plans.aura_base.src.services.ocr_service import OcrService
from plans.aura_base.src.services.ocr_onnx_pipeline import OnnxOcrPipeline


ROOT = Path(__file__).resolve().parents[1]
SAMPLES = (
    ("rec_digits", "tests/fixtures/profile_status_digits/clarity_258_286.png", None, True),
    ("rec_chinese", "tests/fixtures/raw_workshop_ui/idle_blue.png", (354, 16, 280, 44), True),
    ("small_full", "tests/fixtures/city_identity/main_visit_roi.png", None, False),
    ("large_full", "tests/fixtures/raw_workshop_ui/idle_blue.png", None, False),
)


class _Config:
    def __init__(self, threads):
        self.values = {"ocr.execution_provider": "cpu", "ocr.session.intra_op_num_threads": threads}

    def get(self, key, default=None):
        return self.values.get(key, default)


class _BenchmarkBackend(OnnxRuntimeBackend):
    """Override only the experiment's session options, leaving production unchanged."""

    def __init__(self, config, spinning):
        super().__init__(config=config, config_prefix="ocr", runtime_name="OCR CPU benchmark", install_hint="")
        self.spinning = spinning

    def create_session_options(self, ort):
        options = super().create_session_options(ort)
        options.log_severity_level = 3
        options.execution_mode = ort.ExecutionMode.ORT_SEQUENTIAL
        for pool in ("intra_op", "inter_op"):
            options.add_session_config_entry(f"session.{pool}.allow_spinning", str(int(self.spinning)))
        return options


def _save(path, value):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(value, ensure_ascii=False, indent=2), encoding="utf-8")


def _worker(args):
    import onnxruntime as ort

    config = _Config(args.threads)
    service = OcrService(config=config)
    service._onnx_backend = _BenchmarkBackend(config, args.spinning == "on")
    bundle = ROOT / "models/ocr/ppocrv6_small"
    metadata = load_ocr_metadata(bundle / "ocr.meta.json")
    started = time.perf_counter()
    sessions, provider = service._create_pipeline_sessions(metadata, bundle)
    load_ms = (time.perf_counter() - started) * 1000
    pipeline = OnnxOcrPipeline(metadata=metadata, sessions=sessions, provider=provider)
    service._engine_provider = provider
    process = psutil.Process()
    result = {
        "threads": args.threads, "spinning": args.spinning, "round": args.round,
        "pid": os.getpid(), "provider": provider, "ort": ort.__version__,
        "opencv_threads": cv2.getNumThreads(), "physical_cores": psutil.cpu_count(logical=False),
        "logical_cpus": os.cpu_count(), "platform": platform.platform(),
        "session_load_ms": load_ms, "process_threads_after_load": process.num_threads(),
        "rss_after_load_mib": process.memory_info().rss / 1024 ** 2,
        "warmup": args.warmup, "continuous_iterations": args.iterations,
        "spaced_iterations": args.spaced_iterations, "gap_seconds": args.gap,
        "timing_scope": "pipeline + result parsing; excludes capture, file I/O, session loading, async bridge",
        "samples": [],
    }
    sample_order = list(SAMPLES)
    random.Random(args.round).shuffle(sample_order)
    for name, relative, crop, recognition_only in sample_order:
        path = ROOT / relative
        image = np.asarray(Image.open(path).convert("RGB"))
        if crop:
            x, y, width, height = crop
            image = image[y:y + height, x:x + width].copy()

        def predict():
            return service._parse_results(service._run_ocr_sync(
                pipeline, image, recognition_only=recognition_only,
            ))

        for _ in range(args.warmup):
            recognized = predict()
        fingerprint = [item.text for item in recognized]
        if not fingerprint:
            raise RuntimeError(f"Empty OCR workload: {name}")
        sample = {
            "name": name, "path": relative, "sha256": hashlib.sha256(path.read_bytes()).hexdigest(),
            "crop_xywh": crop, "shape": list(image.shape), "recognition_only": recognition_only,
            "texts": fingerprint, "region_count": len(recognized), "stable_repeats": True, "modes": {},
        }
        for mode, count in (("continuous", args.iterations), ("spaced", args.spaced_iterations)):
            time.sleep(0.25)  # Let the previous mode's short spin window expire.
            latency_ms = []
            idle_cpu_ns = 0
            cpu_start = time.process_time_ns()
            wall_start = time.perf_counter_ns()
            for _ in range(count):
                begin = time.perf_counter_ns()
                recognized = predict()
                latency_ms.append((time.perf_counter_ns() - begin) / 1e6)
                sample["stable_repeats"] &= [item.text for item in recognized] == fingerprint
                if mode == "spaced":
                    before_idle = time.process_time_ns()
                    time.sleep(args.gap)
                    idle_cpu_ns += time.process_time_ns() - before_idle
            cpu_ms = (time.process_time_ns() - cpu_start) / 1e6
            wall_ms = (time.perf_counter_ns() - wall_start) / 1e6
            sample["modes"][mode] = {
                "latency_ms": latency_ms, "cpu_ms_per_call": cpu_ms / count,
                "idle_cpu_ms_per_gap": idle_cpu_ns / count / 1e6,
                "average_process_cpu_cores": cpu_ms / wall_ms,
            }
        result["samples"].append(sample)
        print(f"  {name}: regions={len(fingerprint)}, median="
              f"{np.median(sample['modes']['continuous']['latency_ms']):.2f} ms", flush=True)
    result["process_threads_after_run"] = process.num_threads()
    _save(args.worker_output, result)


def _summarize(output):
    runs = [json.loads(path.read_text(encoding="utf-8")) for path in sorted(output.glob("run-*.json"))]
    baseline = next((run for run in runs if run["threads"] == 0 and run["spinning"] == "on"), None)
    baseline_texts = {sample["name"]: sample["texts"] for sample in baseline["samples"]} if baseline else {}
    rows = []
    for threads in (0, 1, 2, 4, 8):
        for spinning in ("on", "off"):
            matching = [run for run in runs if run["threads"] == threads and run["spinning"] == spinning]
            if not matching:
                continue
            for name, *_ in SAMPLES:
                samples = [next(sample for sample in run["samples"] if sample["name"] == name) for run in matching]
                reference = samples[0]["texts"]
                for mode in ("continuous", "spaced"):
                    measurements = [sample["modes"][mode] for sample in samples]
                    timings = [value for measurement in measurements for value in measurement["latency_ms"]]
                    rows.append({
                        "threads": threads, "spinning": spinning, "sample": name, "mode": mode,
                        "rounds": len(matching), "n": len(timings),
                        "median_ms": float(np.median(timings)), "p95_ms": float(np.percentile(timings, 95)),
                        "cpu_ms_per_call": float(np.mean([m["cpu_ms_per_call"] for m in measurements])),
                        "idle_cpu_ms_per_gap": float(np.mean([m["idle_cpu_ms_per_gap"] for m in measurements])),
                        "average_process_cpu_cores": float(np.mean([m["average_process_cpu_cores"] for m in measurements])),
                        "region_count": samples[0]["region_count"],
                        "stable_repeats": all(s["stable_repeats"] for s in samples),
                        "stable_across_rounds": all(s["texts"] == reference for s in samples),
                        "same_text_as_baseline": (
                            all(s["texts"] == baseline_texts[name] for s in samples)
                            if name in baseline_texts else None
                        ),
                    })
    _save(output / "summary.json", {"runs": runs, "rows": rows})
    if rows:
        with (output / "summary.csv").open("w", encoding="utf-8-sig", newline="") as handle:
            writer = csv.DictWriter(handle, fieldnames=list(rows[0]))
            writer.writeheader()
            writer.writerows(rows)
    print(f"Results: {output / 'summary.csv'}", flush=True)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output", type=Path, default=Path(".pytest_tmp/ocr-cpu-benchmark"))
    parser.add_argument("--rounds", type=int, default=2)
    parser.add_argument("--iterations", type=int, default=20)
    parser.add_argument("--spaced-iterations", type=int, default=8)
    parser.add_argument("--gap", type=float, default=0.15)
    parser.add_argument("--warmup", type=int, default=3)
    parser.add_argument("--summarize-only", action="store_true")
    parser.add_argument("--worker-output", type=Path)
    parser.add_argument("--threads", type=int, default=2)
    parser.add_argument("--spinning", choices=("on", "off"), default="off")
    parser.add_argument("--round", type=int, default=0)
    args = parser.parse_args()
    if min(args.rounds, args.iterations, args.spaced_iterations, args.warmup) < 1 or args.gap < 0:
        parser.error("Rounds, iterations and warmup must be positive; gap must be nonnegative.")
    output = (ROOT / args.output).resolve()
    output.relative_to(ROOT)
    output.mkdir(parents=True, exist_ok=True)
    if args.worker_output:
        args.worker_output = (ROOT / args.worker_output).resolve()
        args.worker_output.relative_to(ROOT)
        _worker(args)
        return
    if args.summarize_only:
        _summarize(output)
        return
    profiles = [(threads, spin) for threads in (0, 1, 2, 4, 8) for spin in ("on", "off")]
    for round_number in range(args.rounds):
        order = list(profiles)
        random.Random(20261022 + round_number).shuffle(order)
        for threads, spin in order:
            destination = output / f"run-{round_number}-{threads}-{spin}.json"
            print(f"Round {round_number + 1}/{args.rounds}: threads={threads or 'auto'}, spinning={spin}", flush=True)
            command = [
                sys.executable, "-m", "tools.benchmark_ocr_cpu", "--worker-output", str(destination),
                "--threads", str(threads), "--spinning", spin, "--round", str(round_number),
                "--iterations", str(args.iterations), "--spaced-iterations", str(args.spaced_iterations),
                "--gap", str(args.gap), "--warmup", str(args.warmup),
            ]
            with subprocess.Popen(command, cwd=ROOT) as worker:
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
            _summarize(output)


if __name__ == "__main__":
    main()
