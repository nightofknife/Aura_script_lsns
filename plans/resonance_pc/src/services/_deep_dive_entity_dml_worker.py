"""Private binary-stdio worker; never imported to select the parent runtime."""
from __future__ import annotations

import hashlib
import importlib.metadata
from pathlib import Path
import sys
import time
import traceback


def main():
    # Third-party imports may log. Keep the original binary stream exclusively
    # for framed messages, and redirect every Python stdout log to stderr.
    incoming, outgoing = sys.stdin.buffer, sys.stdout.buffer
    sys.stdout = sys.stderr
    if sys.platform == "win32":
        import msvcrt
        import os
        msvcrt.setmode(incoming.fileno(), os.O_BINARY)
        msvcrt.setmode(outgoing.fileno(), os.O_BINARY)
    root = Path(__file__).resolve().parents[4]
    sys.path.insert(0, str(root))
    from ._deep_dive_entity_worker_backend import (
        read_frame, write_frame, _RGB_SHAPE, _RGB_BYTES, _MAX_PREDICTIONS)
    request_id = None
    try:
        import numpy as np
        import cv2
        cv2.setNumThreads(1)
        initial, payload = read_frame(incoming)
        request_id = initial.get("request_id")
        if initial.get("op") != "initialize" or request_id != 0 or payload:
            raise ValueError("The first worker message must initialize without payload.")
        site = Path(initial["runtime_site"]).resolve()
        model = Path(initial["model_path"]).resolve()
        model_hash = hashlib.sha256(model.read_bytes()).hexdigest()
        if model_hash != initial.get("model_sha256"):
            raise ValueError("Model bytes changed before worker initialization.")
        distributions = [d for d in importlib.metadata.distributions(path=[str(site)])
            if d.metadata.get("Name", "").lower().replace("_", "-") == "onnxruntime-directml"]
        if len(distributions) != 1 or distributions[0].version != initial["ort_version"]:
            raise ValueError("The isolated runtime must contain the pinned onnxruntime-directml distribution.")
        if "onnxruntime" in sys.modules:
            raise RuntimeError("Unexpected runtime preload in the isolated worker.")
        # numpy/cv2 were loaded from the ordinary environment before this narrow
        # path insertion, preventing an isolated environment's ABI from leaking.
        sys.path.insert(0, str(site))
        try:
            import onnxruntime as ort
        finally:
            sys.path.remove(str(site))
        if not Path(ort.__file__).resolve().is_relative_to(site) or ort.__version__ != initial["ort_version"]:
            raise ValueError("Actual ONNX Runtime origin/version differs from the isolated runtime contract.")
        if "DmlExecutionProvider" not in ort.get_available_providers():
            raise RuntimeError("The isolated runtime has no DirectML provider.")
        from ._deep_dive_entity_onnx import (
            CLASS_NAMES, prepare_rgb_roi, validate_model)
        device = initial["device_id"]
        threads = initial["intra_threads"]
        if isinstance(device, bool) or not isinstance(device, int) or device < 0 or isinstance(threads, bool) or not isinstance(threads, int) or threads < 1:
            raise ValueError("Invalid worker device/thread configuration.")
        options = ort.SessionOptions()
        options.intra_op_num_threads = threads
        options.inter_op_num_threads = 1
        options.enable_mem_pattern = False
        options.execution_mode = ort.ExecutionMode.ORT_SEQUENTIAL
        session = ort.InferenceSession(str(model), sess_options=options,
            providers=[("DmlExecutionProvider", {"device_id": device}), "CPUExecutionProvider"])
        session.disable_fallback()
        providers = session.get_providers()
        if not providers or providers[0] != "DmlExecutionProvider":
            raise RuntimeError("Worker session did not select DirectML; implicit CPU fallback is forbidden.")
        input_name = validate_model(session)
        write_frame(outgoing, dict(op="initialized", request_id=0,
            ort_version=ort.__version__, ort_path=str(Path(ort.__file__).resolve()),
            runtime_site=str(site), distribution="onnxruntime-directml", provider=providers[0],
            providers=providers, provider_options=session.get_provider_options(),
            device_id=device, intra_threads=threads, model_path=str(model), model_sha256=model_hash,
            classes=list(CLASS_NAMES), input_shape=session.get_inputs()[0].shape,
            output_shape=session.get_outputs()[0].shape))
        last_request = 0
        while True:
            header, payload = read_frame(incoming)
            request_id = header.get("request_id")
            if isinstance(request_id, bool) or not isinstance(request_id, int) or request_id <= last_request:
                raise ValueError("Worker request IDs must strictly advance.")
            last_request = request_id
            if header.get("op") == "close":
                if payload:
                    raise ValueError("Close messages cannot contain RGB data.")
                # Prior requests are serialized and completed before this ACK.
                session = None
                write_frame(outgoing, dict(op="closed", request_id=request_id, drained=True))
                return 0
            if (header.get("op") != "infer" or header.get("model_sha256") != model_hash or
                header.get("rgb_dtype") != "uint8" or header.get("rgb_shape") != list(_RGB_SHAPE) or
                len(payload) != _RGB_BYTES or hashlib.sha256(payload).hexdigest() != header.get("rgb_sha256")):
                raise ValueError("Worker inference payload has invalid model/RGB identity.")
            rgb = np.frombuffer(payload, dtype=np.uint8).reshape(_RGB_SHAPE)
            tensor, letterbox = prepare_rgb_roi(rgb)
            started = time.perf_counter()
            raw = np.asarray(session.run(None, {input_name: tensor})[0])
            inference_sec = time.perf_counter() - started
            if (raw.dtype != np.float32 or raw.ndim != 3 or raw.shape[:2] != (1, 7) or
                not 0 < raw.shape[2] <= _MAX_PREDICTIONS or not np.isfinite(raw).all()):
                raise ValueError("Worker model returned invalid raw FP32 [1,7,N] predictions.")
            data = np.ascontiguousarray(raw, dtype="<f4").tobytes()
            write_frame(outgoing, dict(op="result", request_id=request_id,
                rgb_sha256=header["rgb_sha256"], model_sha256=model_hash,
                provider=providers[0], raw_dtype="<f4", raw_shape=list(raw.shape),
                raw_sha256=hashlib.sha256(data).hexdigest(), letterbox=list(letterbox),
                inference_sec=inference_sec), data)
    except Exception as exc:
        traceback.print_exc(file=sys.stderr)
        try:
            write_frame(outgoing, dict(op="error", request_id=request_id, error=str(exc)[:2000]))
        except Exception:
            pass
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
