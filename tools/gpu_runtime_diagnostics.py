from __future__ import annotations

import argparse
import asyncio
import importlib.metadata
import os
import subprocess
import sys
from pathlib import Path
from typing import Any, Sequence

import numpy as np

REPO_ROOT = Path(__file__).resolve().parents[1]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from tools._shared import OverlayConfig, add_common_output_flag, maybe_print, normalize_payload  # noqa: E402


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Inspect local GPU runtime health for Aura ONNX OCR and YOLO services.")
    parser.add_argument(
        "--onnx-model",
        help="Optional ONNX model path for probing the Aura YoloService with a blank image.",
    )
    parser.add_argument(
        "--probe-ocr",
        action="store_true",
        help="Initialize Aura ONNX OcrService and run one blank-image recognition pass.",
    )
    add_common_output_flag(parser)
    return parser


def collect_payload(args: argparse.Namespace) -> dict[str, Any]:
    payload: dict[str, Any] = {
        "python": {
            "executable": sys.executable,
            "version": sys.version.split()[0],
        },
        "environment": {
            "cuda_path_vars": {
                key: value
                for key, value in os.environ.items()
                if key == "CUDA_PATH" or key.startswith("CUDA_PATH_V")
            },
        },
        "nvidia_smi": _run_nvidia_smi(),
        "torch": _probe_torch(),
        "paddle": _probe_paddle(),
        "onnxruntime": _probe_onnxruntime(),
    }

    if args.onnx_model:
        payload["yolo_service"] = _probe_yolo_service(Path(args.onnx_model).expanduser().resolve())
    if args.probe_ocr:
        payload["ocr_service"] = _probe_ocr_service()
    return normalize_payload(payload)


def render_text(payload: dict[str, Any]) -> str:
    lines: list[str] = []

    python_payload = payload.get("python", {})
    lines.append(f"Python: {python_payload.get('version')} ({python_payload.get('executable')})")

    env_payload = payload.get("environment", {}).get("cuda_path_vars", {})
    lines.append("CUDA env:")
    if env_payload:
        for key, value in env_payload.items():
            lines.append(f"- {key}={value}")
    else:
        lines.append("- <none>")

    nvidia_smi = payload.get("nvidia_smi", {})
    lines.append("nvidia-smi:")
    if nvidia_smi.get("ok"):
        lines.append(f"- summary: {nvidia_smi.get('first_line')}")
    else:
        lines.append(f"- error: {nvidia_smi.get('error')}")

    torch_payload = payload.get("torch", {})
    lines.append("Torch:")
    if torch_payload.get("installed"):
        lines.append(
            f"- version={torch_payload.get('version')} cuda_available={torch_payload.get('cuda_available')} "
            f"cuda_version={torch_payload.get('cuda_version')} device_count={torch_payload.get('device_count')}"
        )
    else:
        lines.append(f"- not installed ({torch_payload.get('error')})")

    paddle_payload = payload.get("paddle", {})
    lines.append("Paddle export toolchain:")
    if paddle_payload.get("installed"):
        lines.append(
            f"- version={paddle_payload.get('version')} compiled_with_cuda={paddle_payload.get('compiled_with_cuda')} "
            f"device={paddle_payload.get('device')} cuda_version={paddle_payload.get('cuda_version')}"
        )
    else:
        lines.append(f"- not installed ({paddle_payload.get('error')})")

    ort_payload = payload.get("onnxruntime", {})
    lines.append("ONNX Runtime:")
    if ort_payload.get("installed"):
        lines.append(
            f"- version={ort_payload.get('version')} providers={ort_payload.get('available_providers')} "
            f"preload_dlls={ort_payload.get('has_preload_dlls')}"
        )
        distributions = ort_payload.get("distributions") or {}
        if distributions:
            lines.append(f"- distributions={distributions}")
        if ort_payload.get("provider_error"):
            lines.append(f"- provider_error={ort_payload.get('provider_error')}")
        warnings = ort_payload.get("warnings") or []
        for warning in warnings:
            lines.append(f"- warning: {warning}")
    else:
        lines.append(f"- not installed ({ort_payload.get('error')})")

    yolo_payload = payload.get("yolo_service")
    if isinstance(yolo_payload, dict):
        lines.append("YOLO probe:")
        if yolo_payload.get("ok"):
            lines.append(
                f"- provider={yolo_payload.get('provider')} backend={yolo_payload.get('backend')} "
                f"detections={yolo_payload.get('detection_count')}"
            )
        else:
            lines.append(f"- error: {yolo_payload.get('error')}")

    ocr_payload = payload.get("ocr_service")
    if isinstance(ocr_payload, dict):
        lines.append("OCR probe:")
        if ocr_payload.get("ok"):
            lines.append(
                f"- backend={ocr_payload.get('backend')} provider={ocr_payload.get('provider')} "
                f"model={ocr_payload.get('model')} device={ocr_payload.get('preload_device')} "
                f"result_count={ocr_payload.get('result_count')}"
            )
        else:
            lines.append(f"- error: {ocr_payload.get('error')}")

    return "\n".join(lines)


def _run_nvidia_smi() -> dict[str, Any]:
    try:
        completed = subprocess.run(
            ["nvidia-smi"],
            capture_output=True,
            text=True,
            timeout=20,
            check=False,
            encoding="utf-8",
            errors="replace",
        )
    except Exception as exc:  # noqa: BLE001
        return {"ok": False, "error": str(exc)}

    output = (completed.stdout or "").strip().splitlines()
    if completed.returncode != 0:
        return {
            "ok": False,
            "error": (completed.stderr or completed.stdout or f"exit={completed.returncode}").strip(),
        }
    return {
        "ok": True,
        "first_line": output[0] if output else "",
        "line_count": len(output),
    }


def _probe_torch() -> dict[str, Any]:
    try:
        import torch
    except Exception as exc:  # noqa: BLE001
        return {"installed": False, "error": str(exc)}
    return {
        "installed": True,
        "version": torch.__version__,
        "cuda_available": bool(torch.cuda.is_available()),
        "cuda_version": torch.version.cuda,
        "device_count": int(torch.cuda.device_count()),
        "device_names": [torch.cuda.get_device_name(index) for index in range(torch.cuda.device_count())],
    }


def _probe_paddle() -> dict[str, Any]:
    try:
        import paddle
    except Exception as exc:  # noqa: BLE001
        return {"installed": False, "error": str(exc)}

    payload: dict[str, Any] = {
        "installed": True,
        "version": paddle.__version__,
        "compiled_with_cuda": bool(paddle.is_compiled_with_cuda()),
    }
    try:
        payload["device"] = paddle.device.get_device()
    except Exception as exc:  # noqa: BLE001
        payload["device_error"] = str(exc)
    try:
        payload["cuda_version"] = getattr(paddle.version, "cuda", lambda: None)()
    except Exception as exc:  # noqa: BLE001
        payload["cuda_version_error"] = str(exc)
    return payload


def _probe_onnxruntime() -> dict[str, Any]:
    dist_versions: dict[str, str] = {}
    warnings: list[str] = []
    for dist_name in ("onnxruntime", "onnxruntime-gpu"):
        try:
            dist_versions[dist_name] = importlib.metadata.version(dist_name)
        except importlib.metadata.PackageNotFoundError:
            continue

    try:
        import onnxruntime as ort
    except Exception as exc:  # noqa: BLE001
        return {
            "installed": False,
            "error": str(exc),
            "distributions": dist_versions,
            "warnings": warnings,
        }

    providers = []
    provider_error = None
    try:
        providers = list(ort.get_available_providers())
    except Exception as exc:  # noqa: BLE001
        provider_error = str(exc)

    version = getattr(ort, "__version__", None)
    if not version:
        version = dist_versions.get("onnxruntime-gpu") or dist_versions.get("onnxruntime")

    if "onnxruntime" in dist_versions and "onnxruntime-gpu" in dist_versions:
        warnings.append(
            "Detected both onnxruntime and onnxruntime-gpu. Install exactly one ORT runtime package to avoid CPU-only fallbacks."
        )
    if "onnxruntime-gpu" in dist_versions and "CUDAExecutionProvider" not in providers:
        warnings.append(
            "onnxruntime-gpu is installed but CUDAExecutionProvider is unavailable. Reinstall the CUDA package and verify CUDA/cuDNN DLL visibility."
        )

    return {
        "installed": True,
        "version": version,
        "available_providers": providers,
        "provider_error": provider_error,
        "has_preload_dlls": bool(hasattr(ort, "preload_dlls")),
        "has_inference_session": bool(hasattr(ort, "InferenceSession")),
        "distributions": dist_versions,
        "warnings": warnings,
    }


def _probe_yolo_service(model_path: Path) -> dict[str, Any]:
    try:
        from packages.aura_core.services.yolo_service import YoloService

        class _Cfg:
            def __init__(self, values: dict[str, Any]):
                self._values = values

            def get(self, key: str, default: Any = None) -> Any:
                return self._values.get(key, default)

        service = YoloService(
            _Cfg(
                {
                    "yolo.execution_provider": "auto",
                    "yolo.models_root": str(model_path.parent),
                    "yolo.default_model": model_path.name,
                }
            )
        )
        info = service.preload_model(str(model_path))
        result = service.detect_image(np.zeros((640, 640, 3), dtype=np.uint8), model_name=str(model_path))
        return {
            "ok": True,
            "provider": info.get("provider"),
            "backend": result.get("backend"),
            "detection_count": len(result.get("detections", [])),
        }
    except Exception as exc:  # noqa: BLE001
        return {"ok": False, "error": str(exc)}


def _probe_ocr_service() -> dict[str, Any]:
    async def _run_probe() -> dict[str, Any]:
        from plans.aura_base.src.services.ocr_service import OcrService

        service = OcrService()
        preload_device = await service._preload_engine_async(False)
        result = await service._recognize_all_async(np.zeros((64, 64, 3), dtype=np.uint8))
        return {
            "ok": True,
            "backend": service.get_backend(),
            "provider": service.get_provider(),
            "model": service.get_model(),
            "preload_device": preload_device,
            "result_count": result.count,
        }

    try:
        return asyncio.run(_run_probe())
    except Exception as exc:  # noqa: BLE001
        return {"ok": False, "error": str(exc)}


def run_cli(argv: Sequence[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    payload = collect_payload(args)
    maybe_print(payload, as_json=args.json, text_renderer=render_text)
    return 0


def main() -> None:
    raise SystemExit(run_cli())


if __name__ == "__main__":
    main()
