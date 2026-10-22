"""Profile the unchanged OCR recognizer using tensors from real fixture calls.

Run from the repository root with the repository Python environment. Normal
pipeline timings and operator traces are collected separately. All artifacts
and process temporary directories stay under the requested repository output.
"""
from __future__ import annotations

import argparse
import asyncio
from collections import defaultdict
from dataclasses import asdict
import hashlib
import json
from pathlib import Path
import platform
import sys
import time

import cv2
import numpy as np
import onnxruntime as ort
from PIL import Image
import psutil

from plans.aura_base.src.services._ocr_timing import OcrTiming
from plans.aura_base.src.services.ocr_service import OcrService
from tools.benchmark_ocr_cpu import SAMPLES
from tools.benchmark_ocr_recognition import _fingerprint
from tools.benchmark_ocr_stages import (
    ROOT, _Config, _repo_path, _run_worker, _save, _session_options, _sha256,
)


SOURCES = (
    "tools/benchmark_ocr_operators.py",
    "plans/aura_base/src/services/ocr_onnx_pipeline.py",
    "plans/aura_base/src/services/ocr_service.py",
    "plans/aura_base/src/services/_ocr_recognition.py",
    "packages/aura_core/services/onnx_runtime_backend.py",
    "packaging/templates/config.yaml",
)


def _digest(array):
    return hashlib.sha256(np.ascontiguousarray(array).tobytes()).hexdigest()


def _cpu_snapshot():
    snapshot = {}
    for process in psutil.process_iter():
        try:
            if process.pid <= 4:
                continue
            name = process.name()
            # This is only a peer Python-workload audit, not a system CPU profile.
            # Querying protected Windows processes is both slow and unreliable.
            if not name.lower().startswith("python"):
                continue
            times = process.cpu_times()
            snapshot[process.pid] = (name, times.user + times.system)
        except (psutil.Error, MemoryError, OSError):
            pass
    return snapshot


def _cpu_delta(before):
    own = psutil.Process().pid
    rows = [{"pid": pid, "name": name, "cpu_ms": (cpu - before[pid][1]) * 1000}
            for pid, (name, cpu) in _cpu_snapshot().items()
            if pid in before and pid != own and cpu > before[pid][1]]
    return sorted(rows, key=lambda r: -r["cpu_ms"])[:10]


def _samples(extra):
    samples = [dict(name=n, path=p, crop_xywh=c, recognition_only=r) for n, p, c, r in SAMPLES]
    if extra:
        samples.extend(json.loads(extra.read_text(encoding="utf-8")))
    if len({s["name"] for s in samples}) != len(samples):
        raise ValueError("Sample names must be unique.")
    return samples


def _worker(args, output):
    service = OcrService(config=_Config())
    asyncio.run(service._preload_engine_async(warmup=True))
    pipe = service._engine
    if pipe is None:
        raise RuntimeError("OCR did not finish startup warmup.")
    options = asdict(pipe.recognition_options)
    if (options["width_mode"], options["batch_mode"]) != ("legacy", "adaptive"):
        raise RuntimeError(f"Unexpected default recognition policy: {options}")
    bundle, metadata_path = service._resolve_bundle_paths()
    artifacts = {"metadata": metadata_path, "rec": bundle / pipe.metadata.models.rec,
                 "det": bundle / pipe.metadata.models.det,
                 "orientation": bundle / pipe.metadata.models.textline_orientation}
    snapshots = {p: _sha256(ROOT / p) for p in SOURCES}
    model_hashes = {n: {"path": str(p.relative_to(ROOT)), "sha256": _sha256(p)}
                    for n, p in artifacts.items()}
    so = service._onnx_backend.create_session_options(ort)
    so.enable_profiling = True
    so.profile_file_prefix = str(output / f"operators-{args.round}")
    profiled = ort.InferenceSession(str(artifacts["rec"]), sess_options=so,
                                   providers=["CPUExecutionProvider"])
    base_settings, prof_settings = _session_options(pipe.sessions.rec), _session_options(profiled)
    if base_settings != prof_settings or base_settings["intra_op_num_threads"] != 8:
        raise RuntimeError(f"Profiling changed session execution settings: {base_settings}, {prof_settings}")
    result = {"round": args.round, "python": sys.version, "ort": ort.__version__,
              "platform": platform.platform(), "numpy": np.__version__, "opencv": cv2.__version__,
              "opencv_threads": cv2.getNumThreads(), "session": base_settings,
              "recognition_options": options, "model_artifacts": model_hashes,
              "source_sha256": snapshots, "samples": [], "profile_run_log": [],
              "scope": "Normal warmed full pipeline measured separately; recognizer profile replays exact "
                       "preprocessed real tensors with identical shape/order. No capture, loading or async queue timing."}
    input_name = profiled.get_inputs()[0].name
    for specification in _samples(args.extra_samples):
        path = _repo_path(Path(specification["path"]))
        with Image.open(path) as image_file:
            image = np.asarray(image_file.convert("RGB"))
        crop = specification.get("crop_xywh")
        if crop:
            x, y, w, h = crop
            image = image[y:y + h, x:x + w].copy()
        if specification.get("scale", 1) != 1:
            scale = specification["scale"]
            image = cv2.resize(image, None, fx=scale, fy=scale, interpolation=cv2.INTER_CUBIC)
        kwargs = {"recognition_only": specification["recognition_only"]}

        def predict(timing=None, fingerprint=True):
            raw = pipe.predict(image, **kwargs, **({"timing": timing} if timing else {}))
            if timing:
                with timing.measure("result_parse"):
                    parsed = service._parse_results(raw)
            else:
                parsed = service._parse_results(raw)
            return _fingerprint(raw, parsed) if fingerprint else (raw, parsed)

        for _ in range(args.warmup):
            reference = predict()
        tensors, reference_hashes = [], []
        original_run = pipe._run_session

        def capture(session, tensor):
            raw = original_run(session, tensor)
            if session is pipe.sessions.rec:
                tensors.append(tensor.copy())
                reference_hashes.append(_digest(raw[0]))
            return raw

        pipe._run_session = capture
        try:
            if predict() != reference:
                raise RuntimeError("Capturing recognizer inputs changed OCR output.")
        finally:
            del pipe._run_session
        if not tensors:
            raise RuntimeError(f"No recognition tensors for {specification['name']}")
        sample = {**specification, "sha256": _sha256(path), "image_shape": list(image.shape),
                  "reference": reference, "input_shapes": [list(t.shape) for t in tensors],
                  "tensor_sha256": [_digest(t) for t in tensors], "output_sha256": reference_hashes,
                  "normal_calls": [], "profile_output_exact_equal": True}
        # Profiling is disabled for all normal measurements, including the full pipeline.
        background_before = _cpu_snapshot()
        for iteration in range(args.iterations):
            timing = OcrTiming()
            begin = time.perf_counter_ns()
            raw, parsed = predict(timing, fingerprint=False)
            elapsed = (time.perf_counter_ns() - begin) / 1e6
            actual = _fingerprint(raw, parsed)
            if actual != reference:
                raise RuntimeError(f"Unstable normal OCR output for {specification['name']}")
            sample["normal_calls"].append({"iteration": iteration,
                                           **timing.snapshot(total_ms=elapsed)})
        sample["normal_background_cpu"] = _cpu_delta(background_before)
        # Warmup calls remain in the trace but are explicitly excluded during aggregation.
        background_before = _cpu_snapshot()
        for phase, count in (("warmup", args.warmup), ("measure", args.profile_iterations)):
            for iteration in range(count):
                for batch, tensor in enumerate(tensors):
                    begin = time.perf_counter_ns()
                    raw = profiled.run(None, {input_name: tensor})[0]
                    elapsed = (time.perf_counter_ns() - begin) / 1e6
                    exact = _digest(raw) == reference_hashes[batch]
                    sample["profile_output_exact_equal"] &= exact
                    if not exact:
                        raise RuntimeError(f"Profiling changed recognition probabilities: {specification['name']}/{batch}")
                    result["profile_run_log"].append({"sample": specification["name"], "phase": phase,
                                                       "iteration": iteration, "batch": batch,
                                                       "shape": list(tensor.shape), "wall_ms": elapsed})
        sample["profile_background_cpu"] = _cpu_delta(background_before)
        result["samples"].append(sample)
        print(f"round {args.round} {specification['name']}: batches={len(tensors)}, "
              f"normal={np.mean([c['total_ms'] for c in sample['normal_calls']]):.2f} ms, exact=True", flush=True)
    result["profile_path"] = str(Path(profiled.end_profiling()).relative_to(ROOT))
    if snapshots != {p: _sha256(ROOT / p) for p in SOURCES}:
        raise RuntimeError("Source or default configuration changed during this experiment.")
    if model_hashes != {n: {"path": str(p.relative_to(ROOT)), "sha256": _sha256(p)}
                        for n, p in artifacts.items()}:
        raise RuntimeError("Model artifacts changed during this experiment.")
    result["process_rss_mib"] = psutil.Process().memory_info().rss / 1024 ** 2
    _save(output / f"run-{args.round}.json", result)


def _group(event):
    name, op = event["name"], event["args"]["op_name"]
    if name.startswith("MatMul.12") or name.startswith("Add.204"):
        if op not in {"MatMul", "FusedMatMul", "Gemm", "Add"}:
            raise RuntimeError(f"Unexpected classification head operator: {name}/{op}")
        return "classification_head"
    if name == "Softmax.2_kernel_time":
        if op != "Softmax":
            raise RuntimeError(f"Unexpected final Softmax operator: {op}")
        return "final_softmax"
    return "backbone_and_sequence"


def _summarize(output, paths):
    runs = [json.loads(p.read_text(encoding="utf-8")) for p in paths]
    for run in runs[1:]:
        for key in ("source_sha256", "model_artifacts", "session", "ort", "opencv_threads", "recognition_options"):
            if run[key] != runs[0][key]:
                raise RuntimeError(f"Experiment environment changed across rounds: {key}")
    totals, nodes, records = defaultdict(list), defaultdict(list), []
    for run in runs:
        trace = json.loads((ROOT / run["profile_path"]).read_text(encoding="utf-8"))
        model_runs = sorted([e for e in trace if e.get("name") == "model_run"], key=lambda e: e["ts"])
        if len(model_runs) != len(run["profile_run_log"]):
            raise RuntimeError("Trace runs and recorded inference calls do not align.")
        kernels = sorted([e for e in trace if e.get("cat") == "Node" and e.get("name", "").endswith("_kernel_time")],
                         key=lambda e: e["ts"])
        cursor = 0
        for interval, label in zip(model_runs, run["profile_run_log"]):
            start, end = interval["ts"], interval["ts"] + interval["dur"]
            selected = []
            while cursor < len(kernels) and kernels[cursor]["ts"] < end:
                e = kernels[cursor]
                if e["ts"] < start or e["ts"] + e["dur"] > end + 1:
                    raise RuntimeError("A kernel is not fully contained in its model run.")
                selected.append(e)
                cursor += 1
            if label["phase"] != "measure":
                continue
            groups = defaultdict(float)
            head, soft = [], []
            for event in selected:
                group = _group(event)
                ms = event["dur"] / 1000
                groups[group] += ms
                if group == "classification_head":
                    head.append(event)
                elif group == "final_softmax":
                    soft.append(event)
                args = event["args"]
                if args.get("provider") != "CPUExecutionProvider":
                    raise RuntimeError(f"Unexpected execution provider: {args}")
                nodes[(label["sample"], event["name"], args["op_name"], group)].append(ms)
            if not head or len(soft) != 1:
                raise RuntimeError("Could not uniquely identify the final classifier/Softmax.")
            for event in [*head, *soft]:
                shapes = [shape for item in event["args"]["output_type_shape"] for shape in item.values()]
                if not any(shape and shape[-1] == 18710 for shape in shapes):
                    raise RuntimeError(f"Head/Softmax output shape mismatch: {event['name']}/{shapes}")
            kernel_ms = sum(groups.values())
            model_ms = interval["dur"] / 1000
            if kernel_ms > model_ms + .1:
                raise RuntimeError("Summed kernels exceed sequential model interval.")
            records.append({"round": run["round"], **label, **groups, "kernel_ms": kernel_ms,
                            "model_run_ms": model_ms, "session_remainder_ms": model_ms - kernel_ms,
                            "head_nodes": [e["name"] for e in head], "softmax_nodes": [e["name"] for e in soft]})
        if cursor != len(kernels):
            raise RuntimeError("Unassigned kernel events remain.")
    for record in records:
        totals[(record["sample"], record["round"], record["iteration"])].append(record)
    rows = []
    for name in [s["name"] for s in runs[0]["samples"]]:
        normal = [c for r in runs for s in r["samples"] if s["name"] == name for c in s["normal_calls"]]
        calls = [{key: sum(c[key] for c in batches) for key in
                  ("classification_head", "final_softmax", "backbone_and_sequence", "kernel_ms", "model_run_ms", "wall_ms")}
                 for (sample, _, _), batches in totals.items() if sample == name]
        row = {"sample": name, "normal_n": len(normal), "profile_n": len(calls),
               "normal_total_mean_ms": float(np.mean([c["total_ms"] for c in normal])),
               "normal_rec_inference_mean_ms": float(np.mean([c["stages_ms"]["rec_inference"] for c in normal])),
               "normal_rec_decode_mean_ms": float(np.mean([c["stages_ms"]["rec_decode"] for c in normal]))}
        for key in calls[0]:
            row["profile_" + key] = float(np.mean([c[key] for c in calls]))
        for group in ("classification_head", "final_softmax", "backbone_and_sequence"):
            row[group + "_kernel_percent"] = row["profile_" + group] / row["profile_kernel_ms"] * 100
        row["round_kernel_percent"] = {}
        for r in runs:
            batches = [c for c in records if c["sample"] == name and c["round"] == r["round"]]
            row["round_kernel_percent"][str(r["round"])] = {
                g: sum(c[g] for c in batches) / sum(c["kernel_ms"] for c in batches) * 100
                for g in ("classification_head", "final_softmax", "backbone_and_sequence")}
        rows.append(row)
    top_nodes = [{"sample": s, "name": n, "op": op, "group": g, "calls": len(values),
                  "total_ms": sum(values), "mean_per_batch_ms": float(np.mean(values))}
                 for (s, n, op, g), values in nodes.items()]
    top_nodes.sort(key=lambda r: (r["sample"], -r["total_ms"]))
    _save(output / "summary.json", {"rows": rows, "top_nodes": top_nodes, "profile_batches": records,
                                    "group_basis": "Final MatMul.12 (including transpose fusion) and Add.204; "
                                                   "final Softmax.2 only; all other executed kernels are backbone/sequence.",
                                    "profile_warning": "Kernel durations include profiling/scheduling overhead. "
                                                       "Do not substitute them for normal inference latency."})
    _save(output / "artifact-sha256.json", {str(p.relative_to(output)): _sha256(p)
                                           for p in [*paths, output / "summary.json",
                                                     *[ROOT / r["profile_path"] for r in runs]]})
    for row in rows:
        print(json.dumps(row, ensure_ascii=False), flush=True)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output", type=Path, default=Path(".pytest_tmp/ocr-operator-profile"))
    parser.add_argument("--rounds", type=int, default=2)
    parser.add_argument("--iterations", type=int, default=20)
    parser.add_argument("--profile-iterations", type=int, default=5)
    parser.add_argument("--warmup", type=int, default=3)
    parser.add_argument("--extra-samples", type=Path)
    parser.add_argument("--worker", action="store_true", help=argparse.SUPPRESS)
    parser.add_argument("--round", type=int, default=0, help=argparse.SUPPRESS)
    parser.add_argument("--summarize-only", action="store_true")
    args = parser.parse_args()
    if min(args.rounds, args.iterations, args.profile_iterations, args.warmup) < 1:
        parser.error("All counts must be positive.")
    output = _repo_path(args.output)
    output.mkdir(parents=True, exist_ok=True)
    if args.extra_samples:
        args.extra_samples = _repo_path(args.extra_samples)
    if args.worker:
        _worker(args, output)
        return
    paths = [output / f"run-{r}.json" for r in range(args.rounds)]
    if not args.summarize_only:
        for r in range(args.rounds):
            command = [sys.executable, "-m", "tools.benchmark_ocr_operators", "--worker", "--round", str(r),
                       "--output", str(output), "--iterations", str(args.iterations),
                       "--profile-iterations", str(args.profile_iterations), "--warmup", str(args.warmup)]
            if args.extra_samples:
                command += ["--extra-samples", str(args.extra_samples)]
            _run_worker(command, output)
    _summarize(output, paths)


if __name__ == "__main__":
    main()
