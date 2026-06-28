from __future__ import annotations

import argparse
import json
import shutil
import subprocess
import sys
from pathlib import Path
from typing import Any, Optional

import yaml

REPO_ROOT = Path(__file__).resolve().parents[1]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from plans.aura_base.src.services.ocr_contract import (  # noqa: E402
    OcrDetPostprocessMetadata,
    OcrDetPreprocessMetadata,
    OcrModelFiles,
    OcrModelMetadata,
    OcrPipelineMetadata,
    OcrRecPostprocessMetadata,
    OcrRecPreprocessMetadata,
    OcrTextlineOrientationMetadata,
    write_ocr_metadata,
)


MODEL_DIRS = {
    "det": "PP-OCRv5_server_det",
    "rec": "PP-OCRv5_server_rec",
    "textline_orientation": "PP-LCNet_x1_0_textline_ori",
    "doc_orientation": "PP-LCNet_x1_0_doc_ori",
}


def _load_onnx_module():
    try:
        import onnx
    except ImportError as exc:
        raise RuntimeError("onnx is required for OCR export validation. Install requirements/optional-ocr-export.txt.") from exc
    return onnx


def _load_onnxruntime_module():
    try:
        import onnxruntime as ort
    except ImportError as exc:
        raise RuntimeError(
            "onnxruntime is required for OCR export validation. Install requirements/optional-vision-onnx-cpu.txt "
            "or requirements/optional-vision-onnx-cuda.txt."
        ) from exc
    return ort


def _load_model_config(model_dir: Path) -> dict[str, Any]:
    yaml_path = model_dir / "inference.yml"
    if yaml_path.is_file():
        with yaml_path.open("r", encoding="utf-8") as handle:
            return yaml.safe_load(handle) or {}

    json_path = model_dir / "config.json"
    if json_path.is_file():
        return json.loads(json_path.read_text(encoding="utf-8"))

    raise RuntimeError(f"Missing OCR model config in {model_dir}. Expected inference.yml or config.json.")


def _find_transform(config: dict[str, Any], name: str) -> dict[str, Any]:
    for item in config.get("PreProcess", {}).get("transform_ops", []) or []:
        if isinstance(item, dict) and name in item:
            return item.get(name) or {}
    return {}


def _resolve_model_dir(model_root: Path, key: str) -> Path:
    path = (model_root / MODEL_DIRS[key]).resolve()
    if not path.is_dir():
        raise RuntimeError(f"Missing PaddleOCR model directory for {key}: {path}")
    return path


def _resolve_optional_model_dir(model_root: Path, key: str) -> Path | None:
    path = (model_root / MODEL_DIRS[key]).resolve()
    return path if path.is_dir() else None


def _assert_paddle_inference_model(path: Path, *, required: bool) -> bool:
    model_file = _resolve_paddle_model_file(path)
    params_file = path / "inference.pdiparams"
    if model_file is not None and params_file.is_file():
        return True
    if required:
        raise RuntimeError(
            f"Missing Paddle inference files in {path}. Expected inference.json or inference.pdmodel, plus inference.pdiparams."
        )
    return False


def _convert_paddle_model_to_onnx(source_dir: Path, target_path: Path, *, opset: Optional[int]) -> None:
    target_path.parent.mkdir(parents=True, exist_ok=True)
    model_file = _resolve_paddle_model_file(source_dir)
    if model_file is None:
        raise RuntimeError(f"Missing Paddle model file in {source_dir}. Expected inference.json or inference.pdmodel.")
    command = [
        *_paddle2onnx_command_prefix(),
        "--model_dir",
        str(source_dir),
        "--model_filename",
        model_file.name,
        "--params_filename",
        "inference.pdiparams",
        "--save_file",
        str(target_path),
        "--enable_onnx_checker",
        "True",
    ]
    if opset is not None:
        command.extend(["--opset_version", str(int(opset))])
    completed = subprocess.run(command, capture_output=True, text=True, encoding="utf-8", errors="replace")
    combined_output = f"{completed.stdout}\n{completed.stderr}"
    failed_markers = (
        "Paddle model parsing failed",
        "Paddle model convert failed",
        "Failed to load parameters",
        "Traceback (most recent call last)",
    )
    if completed.returncode != 0 or any(marker in combined_output for marker in failed_markers):
        raise RuntimeError(
            "paddle2onnx export failed for "
            f"{source_dir}.\nSTDOUT:\n{completed.stdout}\nSTDERR:\n{completed.stderr}"
        )
    if not target_path.is_file() or target_path.stat().st_size <= 0:
        raise RuntimeError(f"paddle2onnx did not produce expected file: {target_path}")


def _resolve_paddle_model_file(model_dir: Path) -> Path | None:
    # Paddle 3.x can reject some legacy .pdmodel files with load_combine errors.
    # The bundled PP-OCRv5 inference models include inference.json, which is the
    # preferred input for current paddle2onnx.
    for filename in ("inference.json", "inference.pdmodel"):
        path = model_dir / filename
        if path.is_file():
            return path
    return None


def _paddle2onnx_command_prefix() -> list[str]:
    script_name = "paddle2onnx.exe" if sys.platform.startswith("win") else "paddle2onnx"
    sibling_script = Path(sys.executable).with_name(script_name)
    if sibling_script.is_file():
        return [str(sibling_script)]

    resolved_script = shutil.which("paddle2onnx")
    if resolved_script:
        return [resolved_script]

    # Some paddle2onnx releases provide a module entrypoint, while older wheels only
    # install the console script above. Keep this fallback for forward compatibility.
    return [sys.executable, "-m", "paddle2onnx"]


def _validate_onnx_file(path: Path) -> str:
    onnx = _load_onnx_module()
    model = onnx.load(str(path))
    onnx.checker.check_model(model)

    ort = _load_onnxruntime_module()
    available = list(ort.get_available_providers())
    providers = ["CPUExecutionProvider"] if "CPUExecutionProvider" in available else available[:1]
    if not providers:
        raise RuntimeError("No ONNX Runtime execution provider is available for OCR export validation.")
    session = ort.InferenceSession(str(path), providers=providers)
    active = list(session.get_providers())
    return active[0] if active else providers[0]


def _extract_det_preprocess(config: dict[str, Any]) -> OcrDetPreprocessMetadata:
    resize = _find_transform(config, "DetResizeForTest")
    normalize = _find_transform(config, "NormalizeImage")
    return OcrDetPreprocessMetadata(
        resize_long=int(resize.get("resize_long", 960)),
        mean=tuple(float(item) for item in normalize.get("mean", [0.485, 0.456, 0.406])),
        std=tuple(float(item) for item in normalize.get("std", [0.229, 0.224, 0.225])),
        scale=_parse_scale(normalize.get("scale", 1.0 / 255.0)),
    )


def _extract_det_postprocess(config: dict[str, Any]) -> OcrDetPostprocessMetadata:
    post = config.get("PostProcess", {}) or {}
    return OcrDetPostprocessMetadata(
        thresh=float(post.get("thresh", 0.3)),
        box_thresh=float(post.get("box_thresh", 0.6)),
        max_candidates=int(post.get("max_candidates", 1000)),
        unclip_ratio=float(post.get("unclip_ratio", 1.5)),
    )


def _extract_rec_preprocess(config: dict[str, Any]) -> OcrRecPreprocessMetadata:
    resize = _find_transform(config, "RecResizeImg")
    image_shape = tuple(int(item) for item in resize.get("image_shape", [3, 48, 320]))
    max_width = _extract_max_dynamic_width(config, fallback=int(image_shape[2]))
    return OcrRecPreprocessMetadata(image_shape=image_shape, max_width=max_width)


def _extract_rec_postprocess(config: dict[str, Any]) -> OcrRecPostprocessMetadata:
    post = config.get("PostProcess", {}) or {}
    character_dict = post.get("character_dict")
    if not isinstance(character_dict, list) or not character_dict:
        raise RuntimeError("Unable to derive OCR recognition character_dict from inference.yml.")
    return OcrRecPostprocessMetadata(character_dict=[str(item) for item in character_dict], blank_index=0)


def _extract_textline_orientation(config: dict[str, Any]) -> OcrTextlineOrientationMetadata:
    resize = _find_transform(config, "ResizeImage")
    normalize = _find_transform(config, "NormalizeImage")
    topk = (config.get("PostProcess", {}) or {}).get("Topk", {}) or {}
    labels = [str(item) for item in topk.get("label_list", ["0_degree", "180_degree"])]
    size = resize.get("size", [160, 80])
    return OcrTextlineOrientationMetadata(
        enabled=True,
        image_size=(int(size[0]), int(size[1])),
        labels=labels,
        rotate_label="180_degree",
        rotate_threshold=0.9,
        mean=tuple(float(item) for item in normalize.get("mean", [0.485, 0.456, 0.406])),
        std=tuple(float(item) for item in normalize.get("std", [0.229, 0.224, 0.225])),
        scale=_parse_scale(normalize.get("scale", 1.0 / 255.0)),
    )


def _extract_max_dynamic_width(config: dict[str, Any], *, fallback: int) -> int:
    shapes = (
        config.get("Hpi", {})
        .get("backend_configs", {})
        .get("paddle_infer", {})
        .get("trt_dynamic_shapes", {})
        .get("x", [])
    )
    widths: list[int] = []
    for shape in shapes or []:
        if isinstance(shape, list) and len(shape) >= 4:
            widths.append(int(shape[3]))
    return max(widths or [int(fallback)])


def _parse_scale(value: Any) -> float:
    if isinstance(value, str):
        token = value.strip()
        if "/" in token:
            left, right = token.split("/", 1)
            return float(left.strip().rstrip(".")) / float(right.strip().rstrip("."))
        return float(token)
    return float(value)


def export_paddleocr_onnx(
    *,
    model_root: Path,
    out_dir: Path,
    name: str = "ppocrv5_server",
    opset: Optional[int] = None,
) -> dict[str, Any]:
    model_root = model_root.resolve()
    bundle_dir = (out_dir / name).resolve()
    bundle_dir.mkdir(parents=True, exist_ok=True)

    required = {"det", "rec", "textline_orientation"}
    required_keys = ("det", "rec", "textline_orientation")
    source_dirs: dict[str, Path] = {key: _resolve_model_dir(model_root, key) for key in required_keys}
    optional_doc_dir = _resolve_optional_model_dir(model_root, "doc_orientation")
    if optional_doc_dir is not None:
        source_dirs["doc_orientation"] = optional_doc_dir
    exported_files: dict[str, str] = {}
    validation_providers: dict[str, str] = {}

    output_names = {
        "det": "det.onnx",
        "rec": "rec.onnx",
        "textline_orientation": "textline_orientation.onnx",
        "doc_orientation": "doc_orientation.onnx",
    }

    available_models = {
        key: _assert_paddle_inference_model(source_dir, required=key in required)
        for key, source_dir in source_dirs.items()
    }

    for key, source_dir in source_dirs.items():
        has_model = available_models[key]
        if not has_model:
            continue
        target_path = bundle_dir / output_names[key]
        _convert_paddle_model_to_onnx(source_dir, target_path, opset=opset)
        validation_providers[key] = _validate_onnx_file(target_path)
        exported_files[key] = target_path.name

    det_config = _load_model_config(source_dirs["det"])
    rec_config = _load_model_config(source_dirs["rec"])
    textline_config = _load_model_config(source_dirs["textline_orientation"])

    metadata = OcrModelMetadata(
        schema_version=1,
        task="ocr",
        family="ppocrv5",
        variant="server",
        lang="ch",
        models=OcrModelFiles(
            det=exported_files["det"],
            rec=exported_files["rec"],
            textline_orientation=exported_files["textline_orientation"],
            doc_orientation=exported_files.get("doc_orientation"),
        ),
        det_preprocess=_extract_det_preprocess(det_config),
        det_postprocess=_extract_det_postprocess(det_config),
        rec_preprocess=_extract_rec_preprocess(rec_config),
        rec_postprocess=_extract_rec_postprocess(rec_config),
        textline_orientation=_extract_textline_orientation(textline_config),
        pipeline=OcrPipelineMetadata(
            use_doc_orientation=False,
            use_textline_orientation=True,
            batch_size=8,
        ),
        default_score_threshold=0.0,
    )
    metadata_path = bundle_dir / "ocr.meta.json"
    write_ocr_metadata(metadata_path, metadata)

    return {
        "ok": True,
        "model_dir": str(bundle_dir),
        "metadata": str(metadata_path),
        "models": exported_files,
        "providers": validation_providers,
    }


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Export bundled PaddleOCR inference models into Aura OCR ONNX artifacts.")
    parser.add_argument("--model-root", required=True, type=Path, help="Directory containing PaddleOCR inference submodels.")
    parser.add_argument("--out-dir", required=True, type=Path, help="Directory where the OCR bundle should be written.")
    parser.add_argument("--name", default="ppocrv5_server", help="Output OCR bundle directory name.")
    parser.add_argument("--opset", default=None, type=int, help="Optional ONNX opset version passed to paddle2onnx.")
    return parser


def run_cli(argv: Optional[list[str]] = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    if args.out_dir.exists() and not args.out_dir.is_dir():
        raise RuntimeError(f"--out-dir is not a directory: {args.out_dir}")
    result = export_paddleocr_onnx(
        model_root=args.model_root,
        out_dir=args.out_dir,
        name=args.name,
        opset=args.opset,
    )
    print(result)
    return 0


def main() -> None:
    raise SystemExit(run_cli())


if __name__ == "__main__":
    main()
