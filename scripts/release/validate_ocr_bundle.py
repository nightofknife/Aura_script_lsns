from __future__ import annotations

import argparse
import json
from pathlib import Path


def _required_model_entries(metadata: dict) -> list[tuple[str, str]]:
    models = metadata.get("models") or {}
    pipeline = metadata.get("pipeline") or {}
    required = [("det", models.get("det")), ("rec", models.get("rec"))]
    if bool(pipeline.get("use_textline_orientation")):
        required.append(("textline_orientation", models.get("textline_orientation")))
    if bool(pipeline.get("use_doc_orientation")):
        required.append(("doc_orientation", models.get("doc_orientation")))
    return required


def validate_bundle(bundle_dir: Path) -> list[Path]:
    bundle = bundle_dir.resolve()
    metadata_path = bundle / "ocr.meta.json"
    if not metadata_path.is_file():
        raise FileNotFoundError(f"Missing OCR metadata: {metadata_path}")
    metadata = json.loads(metadata_path.read_text(encoding="utf-8-sig"))
    required_paths = [metadata_path]
    for label, relative_value in _required_model_entries(metadata):
        relative = str(relative_value or "").strip()
        if not relative:
            raise ValueError(f"OCR metadata enables {label} but does not define its model path.")
        model_path = (bundle / relative).resolve()
        if bundle not in model_path.parents:
            raise ValueError(f"OCR {label} model escapes the bundle directory: {relative}")
        if model_path.suffix.lower() != ".onnx":
            raise ValueError(f"OCR {label} model must be an .onnx file: {relative}")
        if not model_path.is_file():
            raise FileNotFoundError(f"Missing OCR {label} model: {model_path}")
        required_paths.append(model_path)
    return required_paths


def main() -> int:
    parser = argparse.ArgumentParser(description="Validate a metadata-driven PP-OCR ONNX bundle.")
    parser.add_argument("bundle_dir", type=Path)
    args = parser.parse_args()
    required = validate_bundle(args.bundle_dir)
    print(json.dumps({"ok": True, "required_files": [path.name for path in required]}, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
