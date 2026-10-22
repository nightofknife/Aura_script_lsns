"""Check unchanged OCR on labeled active numeric templates and real UI crops.

The input JSON is a list of samples with name, path, expected and group. Optional
crop_xywh and scale describe the current caller's input preparation. This tool
does not normalize recognized text, change confidence thresholds or change OCR
configuration. It compares full OCR, full OCR without orientation, and
recognition-only, with a repeated output check for each input and mode.
"""
from __future__ import annotations

import argparse
import asyncio
from collections import defaultdict
import json
from pathlib import Path
import sys

import cv2
import numpy as np
from PIL import Image

from plans.aura_base.src.services.ocr_service import OcrService
from tools.benchmark_ocr_recognition import _fingerprint
from tools.benchmark_ocr_stages import ROOT, _Config, _repo_path, _run_worker, _save, _sha256


def _worker(args):
    samples = json.loads(args.samples.read_text(encoding="utf-8"))
    service = OcrService(config=_Config())
    asyncio.run(service._preload_engine_async(warmup=True))
    pipe = service._engine
    bundle, meta_path = service._resolve_bundle_paths()
    sources = [ROOT / p for p in (
        "plans/aura_base/src/services/ocr_service.py",
        "plans/aura_base/src/services/ocr_onnx_pipeline.py",
        "plans/aura_base/src/services/_ocr_recognition.py",
        "packaging/templates/config.yaml",
    )]
    sources += [meta_path, bundle / pipe.metadata.models.rec, bundle / pipe.metadata.models.det,
                bundle / pipe.metadata.models.textline_orientation]
    hashes = {str(p.relative_to(ROOT)): _sha256(p) for p in sources}
    rows = []
    for spec in samples:
        path = _repo_path(Path(spec["path"]))
        with Image.open(path) as image_file:
            source_mode = image_file.mode
            image = np.asarray(image_file.convert("RGB"))
        crop = spec.get("crop_xywh")
        if crop:
            x, y, w, h = crop
            image = image[y:y + h, x:x + w].copy()
        native_shape = list(image.shape)
        scale = spec.get("scale", 1)
        if scale != 1:
            image = cv2.resize(image, None, fx=scale, fy=scale, interpolation=cv2.INTER_CUBIC)
        for mode, rec_only, orientation in (
            ("full", False, True), ("full_no_orientation", False, False), ("recognition_only", True, True),
        ):
            recognition = []
            orientation_predictions = []
            original_recognize = pipe._recognize_crops
            original_run = pipe._run_session

            def capture_orientation(session, tensor):
                result = original_run(session, tensor)
                if session is pipe.sessions.textline_orientation:
                    for probabilities in np.asarray(result[0]):
                        winner = int(np.argmax(probabilities))
                        label = pipe._orientation_label(winner)
                        score = float(probabilities[winner])
                        orientation_predictions.append({"label": label, "confidence": score,
                            "rotated": label == pipe.metadata.textline_orientation.rotate_label
                                       and score >= pipe.metadata.textline_orientation.rotate_threshold})
                return result

            def capture(crops, *, timing=None):
                texts, scores = original_recognize(crops, timing=timing)
                recognition.extend({"text": text, "confidence": float(score)}
                                   for text, score in zip(texts, scores))
                return texts, scores

            pipe._recognize_crops = capture
            pipe._run_session = capture_orientation
            kwargs = dict(recognition_only=rec_only, use_textline_orientation=orientation)
            try:
                raw = pipe.predict(image, **kwargs)
            finally:
                del pipe._recognize_crops
                del pipe._run_session
            actual = _fingerprint(raw, service._parse_results(raw))
            repeated = pipe.predict(image, **kwargs)
            if actual != _fingerprint(repeated, service._parse_results(repeated)):
                raise RuntimeError(f"Unstable output: {spec['name']}/{mode}")
            texts = actual["raw"][0]["rec_texts"]
            rows.append({**spec, "mode": mode, "native_shape": native_shape,
                         "input_shape": list(image.shape), "source_image_mode": source_mode,
                         "source_sha256": _sha256(path), "texts": texts,
                         "confidences": actual["raw"][0]["rec_scores"],
                         "single_region_exact": texts == [spec["expected"]],
                         "joined_exact": "".join(texts) == spec["expected"],
                         "empty": not texts, "stable_repeat": True,
                         "orientation_predictions": orientation_predictions,
                         "recognition_before_result_filter": recognition, "result": actual})
    grouped = defaultdict(list)
    for row in rows:
        grouped[(row["group"], row["mode"])].append(row)
    summary = [{"group": g, "mode": m, "n": len(items),
                "single_region_exact": sum(r["single_region_exact"] for r in items),
                "joined_exact": sum(r["joined_exact"] for r in items),
                "empty": sum(r["empty"] for r in items),
                "wrong_nonempty": sum(not r["empty"] and not r["joined_exact"] for r in items)}
               for (g, m), items in grouped.items()]
    if hashes != {str(p.relative_to(ROOT)): _sha256(p) for p in sources}:
        raise RuntimeError("OCR source/model/defaults changed while checking numeric samples.")
    _save(args.output / "numeric-results.json", {"samples_sha256": _sha256(args.samples),
          "preserved_source_model_sha256": hashes, "rows": rows, "summary": summary,
          "scope": "Resource and offline real-crop agreement against explicit labels; no live screenshot claim; "
                   "each mode called twice with exact output stability; zero text cleanup or whitelist."})
    for row in summary:
        print(json.dumps(row, ensure_ascii=False), flush=True)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--samples", type=Path, required=True)
    parser.add_argument("--output", type=Path, default=Path(".pytest_tmp/ocr-operator-profile/numeric"))
    parser.add_argument("--worker", action="store_true", help=argparse.SUPPRESS)
    args = parser.parse_args()
    args.samples, args.output = _repo_path(args.samples), _repo_path(args.output)
    args.output.mkdir(parents=True, exist_ok=True)
    if args.worker:
        _worker(args)
    else:
        _run_worker([sys.executable, "-m", "tools.probe_ocr_numeric_samples", "--worker",
                     "--samples", str(args.samples), "--output", str(args.output)], args.output)


if __name__ == "__main__":
    main()
