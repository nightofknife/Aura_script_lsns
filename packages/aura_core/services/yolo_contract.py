from __future__ import annotations

import json
import re
from pathlib import Path
from typing import Literal, Optional

from pydantic import BaseModel, ConfigDict, Field, ValidationError, field_validator

SUPPORTED_FAMILIES = ("yolo8", "yolo11", "yolo26")
FAMILY_PREFIXES = {
    "yolo8": "yolov8",
    "yolo11": "yolo11",
    "yolo26": "yolo26",
}
FAMILY_ALIASES = {
    "yolo8": "yolo8",
    "yolov8": "yolo8",
    "v8": "yolo8",
    "yolo11": "yolo11",
    "yolov11": "yolo11",
    "v11": "yolo11",
    "yolo26": "yolo26",
    "yolov26": "yolo26",
    "v26": "yolo26",
}
MODEL_TOKEN_RE = re.compile(r"^(?P<family>yolo(?:v)?(?:8|11|26)|v(?:8|11|26))(?P<variant>[nslmx])?$", re.I)
VARIANT_RE = re.compile(r"([nslmx])(?:\.[A-Za-z0-9]+)?$", re.I)
METADATA_SUFFIX = ".meta.json"
MODEL_SUFFIX = ".onnx"
EXPORT_OUTPUT_FORMAT = "ultralytics_detect_raw_v1"


class YoloPreprocessMetadata(BaseModel):
    model_config = ConfigDict(extra="forbid")

    letterbox: bool = True
    pad_value: int = 114
    normalize: Literal["divide_255"] = "divide_255"

    @field_validator("pad_value")
    @classmethod
    def _validate_pad_value(cls, value: int) -> int:
        if not 0 <= int(value) <= 255:
            raise ValueError("preprocess.pad_value must be between 0 and 255.")
        return int(value)


class YoloModelMetadata(BaseModel):
    model_config = ConfigDict(extra="forbid")

    schema_version: Literal[1] = 1
    task: Literal["detect"] = "detect"
    family: Literal["yolo8", "yolo11", "yolo26"]
    variant: Optional[str] = None
    input_size: tuple[int, int]
    input_format: Literal["rgb"] = "rgb"
    input_layout: Literal["nchw"] = "nchw"
    preprocess: YoloPreprocessMetadata = Field(default_factory=YoloPreprocessMetadata)
    output_format: Literal["ultralytics_detect_raw_v1"] = EXPORT_OUTPUT_FORMAT
    output_layout: Literal["bcn", "bnc"]
    class_names: list[str]
    default_conf: float = 0.25
    default_iou: float = 0.45

    @field_validator("variant")
    @classmethod
    def _validate_variant(cls, value: Optional[str]) -> Optional[str]:
        if value is None:
            return None
        normalized = str(value).strip().lower()
        if normalized not in {"n", "s", "m", "l", "x"}:
            raise ValueError("variant must be one of: n, s, m, l, x.")
        return normalized

    @field_validator("input_size")
    @classmethod
    def _validate_input_size(cls, value: tuple[int, int]) -> tuple[int, int]:
        if len(value) != 2:
            raise ValueError("input_size must contain width and height.")
        width, height = int(value[0]), int(value[1])
        if width <= 0 or height <= 0:
            raise ValueError("input_size values must be greater than 0.")
        return width, height

    @field_validator("class_names")
    @classmethod
    def _validate_class_names(cls, value: list[str]) -> list[str]:
        normalized = [str(item).strip() for item in value if str(item).strip()]
        if not normalized:
            raise ValueError("class_names must not be empty.")
        return normalized

    @field_validator("default_conf", "default_iou")
    @classmethod
    def _validate_threshold(cls, value: float) -> float:
        numeric = float(value)
        if not 0.0 <= numeric <= 1.0:
            raise ValueError("threshold values must be between 0.0 and 1.0.")
        return numeric

    @property
    def class_count(self) -> int:
        return len(self.class_names)


def normalize_family_token(family_token: str) -> str:
    normalized = str(family_token or "").strip().lower()
    if normalized not in FAMILY_ALIASES:
        raise ValueError(f"Unsupported YOLO family token: {family_token}")
    return FAMILY_ALIASES[normalized]


def infer_variant_from_name(token: str) -> Optional[str]:
    match = VARIANT_RE.search(str(token or "").lower())
    return match.group(1) if match else None


def infer_family_from_name(token: str) -> Optional[str]:
    lowered = str(token or "").strip().lower()
    for family, prefix in FAMILY_PREFIXES.items():
        if lowered.startswith(prefix):
            return family
    return None


def canonical_model_filename(*, family: str, variant: str) -> str:
    normalized_family = normalize_family_token(family)
    normalized_variant = str(variant or "").strip().lower()
    if normalized_variant not in {"n", "s", "m", "l", "x"}:
        raise ValueError("variant must be one of: n, s, m, l, x.")
    return f"{FAMILY_PREFIXES[normalized_family]}{normalized_variant}{MODEL_SUFFIX}"


def metadata_path_for_model(model_path: Path) -> Path:
    return model_path.with_suffix(METADATA_SUFFIX)


def load_model_metadata(path: Path) -> YoloModelMetadata:
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except FileNotFoundError as exc:
        raise ValueError(f"Missing YOLO metadata sidecar: {path}") from exc
    except json.JSONDecodeError as exc:
        raise ValueError(f"Invalid YOLO metadata JSON: {path}: {exc}") from exc

    try:
        return YoloModelMetadata.model_validate(payload)
    except ValidationError as exc:
        raise ValueError(f"Invalid YOLO metadata schema: {path}: {exc}") from exc


def write_model_metadata(path: Path, metadata: YoloModelMetadata) -> None:
    path.write_text(
        json.dumps(metadata.model_dump(mode="json"), ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
    )
