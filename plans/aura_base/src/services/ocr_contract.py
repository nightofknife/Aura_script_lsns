from __future__ import annotations

import json
from pathlib import Path
from typing import Literal, Optional

from pydantic import BaseModel, ConfigDict, Field, ValidationError, field_validator


METADATA_FILENAME = "ocr.meta.json"
SUPPORTED_FAMILY = "ppocrv5"
SUPPORTED_VARIANT = "server"


class OcrModelFiles(BaseModel):
    model_config = ConfigDict(extra="forbid")

    det: str
    rec: str
    textline_orientation: str
    doc_orientation: Optional[str] = None


class OcrDetPreprocessMetadata(BaseModel):
    model_config = ConfigDict(extra="forbid")

    resize_long: int = 960
    mean: tuple[float, float, float] = (0.485, 0.456, 0.406)
    std: tuple[float, float, float] = (0.229, 0.224, 0.225)
    scale: float = 1.0 / 255.0

    @field_validator("resize_long")
    @classmethod
    def _validate_resize_long(cls, value: int) -> int:
        numeric = int(value)
        if numeric <= 0:
            raise ValueError("det_preprocess.resize_long must be greater than 0.")
        return numeric


class OcrDetPostprocessMetadata(BaseModel):
    model_config = ConfigDict(extra="forbid")

    thresh: float = 0.3
    box_thresh: float = 0.6
    max_candidates: int = 1000
    unclip_ratio: float = 1.5

    @field_validator("thresh", "box_thresh")
    @classmethod
    def _validate_threshold(cls, value: float) -> float:
        numeric = float(value)
        if not 0.0 <= numeric <= 1.0:
            raise ValueError("det_postprocess thresholds must be between 0.0 and 1.0.")
        return numeric


class OcrRecPreprocessMetadata(BaseModel):
    model_config = ConfigDict(extra="forbid")

    image_shape: tuple[int, int, int] = (3, 48, 320)
    max_width: int = 3200

    @field_validator("image_shape")
    @classmethod
    def _validate_image_shape(cls, value: tuple[int, int, int]) -> tuple[int, int, int]:
        if len(value) != 3:
            raise ValueError("rec_preprocess.image_shape must be [channels, height, width].")
        channels, height, width = [int(item) for item in value]
        if channels != 3 or height <= 0 or width <= 0:
            raise ValueError("rec_preprocess.image_shape must be [3, positive_height, positive_width].")
        return channels, height, width


class OcrRecPostprocessMetadata(BaseModel):
    model_config = ConfigDict(extra="forbid")

    character_dict: list[str]
    blank_index: int = 0

    @field_validator("character_dict")
    @classmethod
    def _validate_character_dict(cls, value: list[str]) -> list[str]:
        normalized = [str(item) for item in value]
        if not normalized:
            raise ValueError("rec_postprocess.character_dict must not be empty.")
        return normalized


class OcrTextlineOrientationMetadata(BaseModel):
    model_config = ConfigDict(extra="forbid")

    enabled: bool = True
    image_size: tuple[int, int] = (160, 80)
    labels: list[str] = Field(default_factory=lambda: ["0_degree", "180_degree"])
    rotate_label: str = "180_degree"
    rotate_threshold: float = 0.9
    mean: tuple[float, float, float] = (0.485, 0.456, 0.406)
    std: tuple[float, float, float] = (0.229, 0.224, 0.225)
    scale: float = 1.0 / 255.0

    @field_validator("image_size")
    @classmethod
    def _validate_image_size(cls, value: tuple[int, int]) -> tuple[int, int]:
        if len(value) != 2:
            raise ValueError("textline_orientation.image_size must be [width, height].")
        width, height = int(value[0]), int(value[1])
        if width <= 0 or height <= 0:
            raise ValueError("textline_orientation.image_size values must be positive.")
        return width, height


class OcrPipelineMetadata(BaseModel):
    model_config = ConfigDict(extra="forbid")

    use_doc_orientation: bool = False
    use_textline_orientation: bool = True
    batch_size: int = 8

    @field_validator("batch_size")
    @classmethod
    def _validate_batch_size(cls, value: int) -> int:
        numeric = int(value)
        if numeric <= 0:
            raise ValueError("pipeline.batch_size must be greater than 0.")
        return numeric


class OcrModelMetadata(BaseModel):
    model_config = ConfigDict(extra="forbid")

    schema_version: Literal[1] = 1
    task: Literal["ocr"] = "ocr"
    family: Literal["ppocrv5"] = SUPPORTED_FAMILY
    variant: Literal["server"] = SUPPORTED_VARIANT
    lang: str = "ch"
    models: OcrModelFiles
    det_preprocess: OcrDetPreprocessMetadata = Field(default_factory=OcrDetPreprocessMetadata)
    det_postprocess: OcrDetPostprocessMetadata = Field(default_factory=OcrDetPostprocessMetadata)
    rec_preprocess: OcrRecPreprocessMetadata = Field(default_factory=OcrRecPreprocessMetadata)
    rec_postprocess: OcrRecPostprocessMetadata
    textline_orientation: OcrTextlineOrientationMetadata = Field(default_factory=OcrTextlineOrientationMetadata)
    pipeline: OcrPipelineMetadata = Field(default_factory=OcrPipelineMetadata)
    default_score_threshold: float = 0.0

    @field_validator("default_score_threshold")
    @classmethod
    def _validate_default_score_threshold(cls, value: float) -> float:
        numeric = float(value)
        if not 0.0 <= numeric <= 1.0:
            raise ValueError("default_score_threshold must be between 0.0 and 1.0.")
        return numeric


def metadata_path_for_bundle(bundle_dir: Path) -> Path:
    return bundle_dir / METADATA_FILENAME


def load_ocr_metadata(path: Path) -> OcrModelMetadata:
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except FileNotFoundError as exc:
        raise ValueError(f"Missing OCR metadata sidecar: {path}. Run tools/export_paddleocr_onnx.py first.") from exc
    except json.JSONDecodeError as exc:
        raise ValueError(f"Invalid OCR metadata JSON: {path}: {exc}") from exc

    try:
        return OcrModelMetadata.model_validate(payload)
    except ValidationError as exc:
        raise ValueError(f"Invalid OCR metadata schema: {path}: {exc}") from exc


def write_ocr_metadata(path: Path, metadata: OcrModelMetadata) -> None:
    path.write_text(
        json.dumps(metadata.model_dump(mode="json"), ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
    )
