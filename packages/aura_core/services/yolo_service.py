from __future__ import annotations

import math
import os
import threading
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Dict, List, Optional, Sequence, Tuple

import cv2
import numpy as np

from packages.aura_core.api import service_info
from packages.aura_core.config.service import ConfigService
from packages.aura_core.context.plan import current_plan_name
from packages.aura_core.observability.logging.core_logger import logger

from .onnx_runtime_backend import OnnxRuntimeBackend
from .yolo_contract import (
    EXPORT_OUTPUT_FORMAT,
    FAMILY_ALIASES,
    MODEL_SUFFIX,
    MODEL_TOKEN_RE,
    SUPPORTED_FAMILIES,
    YoloModelMetadata,
    canonical_model_filename,
    infer_family_from_name,
    infer_variant_from_name,
    load_model_metadata,
    metadata_path_for_model,
    normalize_family_token,
)


@dataclass(frozen=True)
class YoloModelReference:
    requested: str
    source: str
    metadata_source: str
    cache_key: str
    family: Optional[str] = None
    variant: Optional[str] = None
    is_path: bool = False


@dataclass(frozen=True)
class _LetterboxInfo:
    scale: float
    pad_x: float
    pad_y: float


@dataclass
class _LoadedOnnxModel:
    session: Any
    metadata: YoloModelMetadata
    provider: str
    model_ref: YoloModelReference


@service_info(
    alias="yolo",
    public=True,
    description="Core ONNX Runtime YOLO service with model-family support for YOLO 8/11/26 detect models.",
)
class YoloService:
    _SUPPORTED_FAMILIES = SUPPORTED_FAMILIES
    _FAMILY_ALIASES = FAMILY_ALIASES

    def __init__(self, config: ConfigService):
        self._config = config
        self._lock = threading.RLock()
        self._models: Dict[str, _LoadedOnnxModel] = {}
        self._class_names: Dict[str, Dict[int, str]] = {}
        self._active_model_key: Optional[str] = None
        self._onnx_backend = OnnxRuntimeBackend(
            config=config,
            config_prefix="yolo",
            runtime_name="core YOLO service",
            install_hint="requirements/optional-vision-onnx-cpu.txt or requirements/optional-vision-onnx-cuda.txt",
        )

    def supported_generations(self) -> List[str]:
        return list(self._SUPPORTED_FAMILIES)

    def resolve_model_reference(self, model_name: str, *, variant: Optional[str] = None) -> YoloModelReference:
        raw = str(model_name or "").strip()
        if not raw:
            raise ValueError("model_name is required.")

        if self._looks_like_path(raw):
            resolved_path = self._resolve_explicit_path(raw)
            if resolved_path.suffix.lower() != MODEL_SUFFIX:
                raise ValueError(
                    f"Runtime only supports {MODEL_SUFFIX} models. "
                    f"Provide an exported ONNX model and metadata sidecar for '{raw}'."
                )
            return YoloModelReference(
                requested=raw,
                source=str(resolved_path),
                metadata_source=str(metadata_path_for_model(resolved_path)),
                cache_key=resolved_path.stem or resolved_path.name,
                family=infer_family_from_name(resolved_path.stem),
                variant=infer_variant_from_name(resolved_path.stem),
                is_path=True,
            )

        token_match = MODEL_TOKEN_RE.match(raw)
        if token_match:
            family = normalize_family_token(token_match.group("family"))
            resolved_variant = self._normalize_variant(variant or token_match.group("variant") or self._default_variant())
            return self._named_model_reference(raw, family=family, variant=resolved_variant)

        lowered = raw.lower()
        if lowered in self._FAMILY_ALIASES:
            family = normalize_family_token(lowered)
            resolved_variant = self._normalize_variant(variant or self._default_variant())
            return self._named_model_reference(raw, family=family, variant=resolved_variant)

        if Path(raw).suffix and Path(raw).suffix.lower() != MODEL_SUFFIX:
            raise ValueError(
                f"Runtime only supports {MODEL_SUFFIX} models. "
                f"Provide an exported ONNX model and metadata sidecar for '{raw}'."
            )

        filename = raw if raw.lower().endswith(MODEL_SUFFIX) else f"{raw}{MODEL_SUFFIX}"
        resolved_path = self._resolve_named_model_path(filename)
        stem = Path(filename).stem
        return YoloModelReference(
            requested=raw,
            source=str(resolved_path),
            metadata_source=str(metadata_path_for_model(resolved_path)),
            cache_key=resolved_path.stem or stem,
            family=infer_family_from_name(stem),
            variant=infer_variant_from_name(stem),
            is_path=False,
        )

    def preload_model(
        self,
        model_name: str,
        *,
        alias: Optional[str] = None,
        variant: Optional[str] = None,
        force_reload: bool = False,
    ) -> Dict[str, Any]:
        model_ref = self.resolve_model_reference(model_name, variant=variant)
        cache_key = str(alias or model_ref.cache_key)

        with self._lock:
            if cache_key in self._models and not force_reload:
                return self.get_model_info(cache_key)

        model_path = Path(model_ref.source)
        metadata_path = Path(model_ref.metadata_source)
        missing_parts: list[str] = []
        if not model_path.exists():
            missing_parts.append("model")
        if not metadata_path.exists():
            missing_parts.append("metadata")
        if missing_parts:
            message = self._format_missing_model_error(model_ref, missing_parts)
            logger.error("yolo_model_resolution_failed %s", message)
            raise ValueError(message)

        logger.info("yolo_model_resolved model=%s path=%s meta=%s", model_name, model_path, metadata_path)
        metadata = load_model_metadata(metadata_path)
        self._validate_model_metadata(model_ref, metadata)
        session, provider = self._create_session(model_path)
        loaded = _LoadedOnnxModel(session=session, metadata=metadata, provider=provider, model_ref=model_ref)
        class_names = {index: label for index, label in enumerate(metadata.class_names)}

        with self._lock:
            if force_reload:
                self._models.pop(cache_key, None)
                self._class_names.pop(cache_key, None)
            self._models[cache_key] = loaded
            self._class_names[cache_key] = class_names
            if self._active_model_key is None:
                self._active_model_key = cache_key

        info = self.get_model_info(cache_key)
        info["loaded"] = True
        return info

    def set_active_model(self, model_name: str, *, variant: Optional[str] = None) -> Dict[str, Any]:
        model_ref = self.resolve_model_reference(model_name, variant=variant)
        cache_key = model_ref.cache_key
        with self._lock:
            if cache_key not in self._models:
                self.preload_model(model_name, variant=variant)
            self._active_model_key = cache_key
            return self.get_model_info(cache_key) | {"active": True}

    def unload_model(self, model_name: str) -> Dict[str, Any]:
        model_ref = self.resolve_model_reference(model_name)
        cache_key = model_ref.cache_key
        with self._lock:
            removed = self._models.pop(cache_key, None)
            self._class_names.pop(cache_key, None)
            if self._active_model_key == cache_key:
                self._active_model_key = None
        return {"ok": True, "model": cache_key, "unloaded": removed is not None}

    def list_loaded_models(self) -> List[str]:
        with self._lock:
            return sorted(self._models)

    def list_loaded_model_infos(self) -> List[Dict[str, Any]]:
        with self._lock:
            return [self.get_model_info(key) for key in sorted(self._models)]

    def get_active_model(self) -> Optional[str]:
        with self._lock:
            return self._active_model_key

    def get_model_info(self, model_name: str) -> Dict[str, Any]:
        with self._lock:
            loaded = self._models.get(model_name)
            metadata = loaded.metadata if loaded is not None else None
            model_ref = loaded.model_ref if loaded is not None else None
            return {
                "ok": True,
                "model": model_name,
                "active": self._active_model_key == model_name,
                "family": metadata.family if metadata is not None else None,
                "variant": metadata.variant if metadata is not None else None,
                "source": model_ref.source if model_ref is not None else None,
                "metadata_source": model_ref.metadata_source if model_ref is not None else None,
                "is_path": model_ref.is_path if model_ref is not None else False,
                "class_count": len(self._class_names.get(model_name, {})),
                "backend": "onnxruntime" if loaded is not None else None,
                "provider": loaded.provider if loaded is not None else None,
            }

    def get_class_names(self, model_name: Optional[str] = None) -> Dict[int, str]:
        _, cache_key = self._get_loaded_model(model_name)
        with self._lock:
            return dict(self._class_names.get(cache_key, {}))

    def resolve_class_ids(self, labels: Sequence[str], model_name: Optional[str] = None) -> List[int]:
        if not labels:
            return []
        reverse_index = {label.lower(): class_id for class_id, label in self.get_class_names(model_name).items()}
        resolved: List[int] = []
        for label in labels:
            key = str(label).strip().lower()
            if key and key in reverse_index:
                resolved.append(int(reverse_index[key]))
        return resolved

    def detect(
        self,
        source: Any,
        *,
        model_name: Optional[str] = None,
        options: Optional[Dict[str, Any]] = None,
    ) -> Dict[str, Any]:
        loaded, cache_key = self._get_loaded_model(model_name)
        metadata = loaded.metadata
        infer_settings = self._build_infer_settings(options or {}, metadata)

        image_rgb = self._coerce_image_source(source)
        tensor, image_size, letterbox = self._prepare_input_tensor(image_rgb, metadata)
        input_name = loaded.session.get_inputs()[0].name
        outputs = loaded.session.run(None, {input_name: tensor})
        detections = self._decode_outputs(
            outputs=outputs,
            metadata=metadata,
            infer_settings=infer_settings,
            image_size=image_size,
            letterbox=letterbox,
        )
        return {
            "ok": True,
            "model": cache_key,
            "detections": detections,
            "family": metadata.family,
            "image_size": [int(image_size[0]), int(image_size[1])],
            "backend": "onnxruntime",
            "provider": loaded.provider,
        }

    def detect_image(
        self,
        image: Any,
        *,
        model_name: Optional[str] = None,
        options: Optional[Dict[str, Any]] = None,
    ) -> Dict[str, Any]:
        result = self.detect(image, model_name=model_name, options=options)
        for det in result.get("detections", []):
            bbox_xywh = det.get("bbox_xywh")
            if isinstance(bbox_xywh, list) and len(bbox_xywh) == 4:
                det["bbox_global"] = [int(round(value)) for value in bbox_xywh]
        return result

    def detect_on_screen(
        self,
        *,
        app: Any,
        roi: Optional[Tuple[int, int, int, int]] = None,
        model_name: Optional[str] = None,
        options: Optional[Dict[str, Any]] = None,
    ) -> Dict[str, Any]:
        if app is None:
            raise ValueError("app service is required for detect_on_screen.")

        capture = app.capture(rect=roi)
        if not getattr(capture, "success", False) or getattr(capture, "image", None) is None:
            return {
                "ok": False,
                "error": getattr(capture, "error_message", None) or "capture failed",
                "detections": [],
                "backend": "onnxruntime",
                "provider": None,
            }

        result = self.detect(capture.image, model_name=model_name, options=options)
        if not result.get("ok"):
            return result

        offset_x, offset_y = self._resolve_capture_origin(app, capture)
        relative_rect = getattr(capture, "relative_rect", None) or (
            0,
            0,
            int(capture.image.shape[1]),
            int(capture.image.shape[0]),
        )
        offset_x += int(relative_rect[0])
        offset_y += int(relative_rect[1])

        for det in result.get("detections", []):
            bbox_xywh = det.get("bbox_xywh")
            if isinstance(bbox_xywh, list) and len(bbox_xywh) == 4:
                x, y, w, h = [float(value) for value in bbox_xywh]
                det["bbox_global"] = [
                    int(round(x + offset_x)),
                    int(round(y + offset_y)),
                    int(round(w)),
                    int(round(h)),
                ]
        return result

    def _named_model_reference(self, requested: str, *, family: str, variant: str) -> YoloModelReference:
        filename = canonical_model_filename(family=family, variant=variant)
        resolved_path = self._resolve_named_model_path(filename)
        return YoloModelReference(
            requested=requested,
            source=str(resolved_path),
            metadata_source=str(metadata_path_for_model(resolved_path)),
            cache_key=resolved_path.stem,
            family=family,
            variant=variant,
            is_path=False,
        )

    def _get_loaded_model(self, model_name: Optional[str]) -> Tuple[_LoadedOnnxModel, str]:
        with self._lock:
            if model_name:
                cache_key = self.resolve_model_reference(model_name).cache_key
            else:
                cache_key = self._active_model_key or self._get_default_model_cache_key()

            if cache_key not in self._models:
                preload_name = model_name or self._get_default_model_name()
                self.preload_model(preload_name)

            return self._models[cache_key], cache_key

    def _get_default_model_name(self) -> str:
        configured = self._config.get("yolo.default_model", None)
        if configured:
            return str(configured)
        return "yolo11"

    def _get_default_model_cache_key(self) -> str:
        return self.resolve_model_reference(self._get_default_model_name()).cache_key

    def _default_variant(self) -> str:
        return self._normalize_variant(self._config.get("yolo.default_variant", "n"))

    @staticmethod
    def _normalize_variant(value: Any) -> str:
        normalized = str(value or "").strip().lower()
        return normalized if normalized in {"n", "s", "m", "l", "x"} else "n"

    @staticmethod
    def _looks_like_path(token: str) -> bool:
        raw = str(token).strip()
        suffix = Path(raw).suffix.lower()
        return bool("\\" in raw or "/" in raw or raw.startswith(".")) or suffix == MODEL_SUFFIX

    def _resolve_explicit_path(self, raw: str) -> Path:
        candidate = Path(raw)
        if candidate.is_absolute():
            return candidate.resolve()

        base_path = self._repo_root()
        plan_name = current_plan_name.get()
        if plan_name:
            plan_candidate = (base_path / "plans" / plan_name / candidate).resolve()
            if plan_candidate.exists():
                return plan_candidate

        direct_candidate = (base_path / candidate).resolve()
        if direct_candidate.exists():
            return direct_candidate

        return direct_candidate

    def _resolve_named_model_path(self, filename: str) -> Path:
        candidates = self._named_model_candidates(filename)
        for candidate in candidates:
            if candidate.exists():
                return candidate
        return candidates[0]

    def _named_model_candidates(self, filename: str) -> list[Path]:
        configured_root = Path(str(self._config.get("yolo.models_root", "models/yolo"))).expanduser()
        roots: list[Path]
        if configured_root.is_absolute():
            roots = [configured_root]
        else:
            roots = []
            env_root = self._env_base_path()
            if env_root is not None:
                roots.append(env_root / configured_root)
            roots.append(Path(__file__).resolve().parents[3] / configured_root)

        candidates: list[Path] = []
        seen: set[str] = set()
        for root in roots:
            candidate = (root / filename).resolve()
            key = str(candidate).lower()
            if key not in seen:
                candidates.append(candidate)
                seen.add(key)
        return candidates

    def _format_missing_model_error(self, model_ref: YoloModelReference, missing_parts: list[str]) -> str:
        candidate_paths: list[str]
        if model_ref.is_path:
            candidate_paths = [model_ref.source]
        else:
            candidate_paths = [str(path) for path in self._named_model_candidates(Path(model_ref.source).name)]
        return (
            f"Missing YOLO {'/'.join(missing_parts)} for '{model_ref.requested}'. "
            f"model={model_ref.source}; metadata={model_ref.metadata_source}; "
            f"AURA_BASE_PATH={os.environ.get('AURA_BASE_PATH')!r}; "
            f"current_plan_name={current_plan_name.get()!r}; candidates={candidate_paths}"
        )

    def _validate_model_metadata(self, model_ref: YoloModelReference, metadata: YoloModelMetadata) -> None:
        if metadata.output_format != EXPORT_OUTPUT_FORMAT:
            raise ValueError(
                f"Unsupported YOLO output_format '{metadata.output_format}'. "
                "Use a model exported with Aura-compatible YOLO metadata."
            )
        if model_ref.family is not None and metadata.family != model_ref.family:
            raise ValueError(
                f"Model metadata family '{metadata.family}' does not match requested family '{model_ref.family}'."
            )

    def _create_session(self, model_path: Path) -> Tuple[Any, str]:
        return self._onnx_backend.create_session(model_path)

    def _build_infer_settings(self, overrides: Dict[str, Any], metadata: YoloModelMetadata) -> Dict[str, Any]:
        def pick(key: str, default: Any) -> Any:
            return overrides.get(key, self._config.get(f"yolo.{key}", default))

        if overrides.get("device") is not None:
            logger.warning("Ignoring deprecated YOLO option 'device'; use yolo.execution_provider instead.")
        if overrides.get("half") is not None:
            logger.warning("Ignoring deprecated YOLO option 'half'; v1 runtime always uses FP32 ONNX.")

        requested_imgsz = overrides.get("imgsz")
        expected_size = [int(metadata.input_size[0]), int(metadata.input_size[1])]
        if requested_imgsz is not None:
            normalized_imgsz = int(requested_imgsz) if not isinstance(requested_imgsz, (list, tuple)) else None
            if normalized_imgsz is None or normalized_imgsz != expected_size[0] or expected_size[0] != expected_size[1]:
                logger.warning(
                    "Ignoring mismatched YOLO imgsz override %r; runtime uses metadata input_size=%s.",
                    requested_imgsz,
                    expected_size,
                )

        classes = pick("classes", None)
        if classes == []:
            classes = None
        return {
            "conf": float(pick("conf", metadata.default_conf)),
            "iou": float(pick("iou", metadata.default_iou)),
            "max_det": int(pick("max_det", 100)),
            "classes": self._normalize_class_filter(classes),
            "agnostic_nms": bool(pick("agnostic_nms", False)),
        }

    @staticmethod
    def _normalize_class_filter(value: Any) -> Optional[set[int]]:
        if value is None:
            return None
        if not isinstance(value, (list, tuple, set)):
            return {int(value)}
        normalized = {int(item) for item in value}
        return normalized or None

    def _coerce_image_source(self, source: Any) -> np.ndarray:
        if isinstance(source, np.ndarray):
            return self._ensure_rgb_array(source, source_kind="array")
        if isinstance(source, (str, Path)):
            path = Path(source)
            if not path.is_file():
                raise FileNotFoundError(f"Image file not found: {path}")
            image_bgr = cv2.imread(str(path), cv2.IMREAD_COLOR)
            if image_bgr is None:
                raise ValueError(f"Failed to read image file: {path}")
            return self._ensure_rgb_array(image_bgr, source_kind="path")
        raise TypeError("source must be a numpy array or image path.")

    @staticmethod
    def _ensure_rgb_array(image: np.ndarray, *, source_kind: str) -> np.ndarray:
        if image.ndim == 2:
            return cv2.cvtColor(image, cv2.COLOR_GRAY2RGB)
        if image.ndim != 3:
            raise ValueError(f"Unsupported image shape: {image.shape}")
        channels = int(image.shape[2])
        if channels == 3:
            if source_kind == "path":
                return cv2.cvtColor(image, cv2.COLOR_BGR2RGB)
            return image
        if channels == 4:
            if source_kind == "path":
                return cv2.cvtColor(image, cv2.COLOR_BGRA2RGB)
            return cv2.cvtColor(image, cv2.COLOR_RGBA2RGB)
        raise ValueError(f"Unsupported channel count: {channels}")

    def _prepare_input_tensor(
        self,
        image_rgb: np.ndarray,
        metadata: YoloModelMetadata,
    ) -> Tuple[np.ndarray, Tuple[int, int], _LetterboxInfo]:
        original_height, original_width = int(image_rgb.shape[0]), int(image_rgb.shape[1])
        target_width, target_height = int(metadata.input_size[0]), int(metadata.input_size[1])
        scale = min(target_width / original_width, target_height / original_height)

        resized_width = max(1, int(round(original_width * scale)))
        resized_height = max(1, int(round(original_height * scale)))
        resized = cv2.resize(image_rgb, (resized_width, resized_height), interpolation=cv2.INTER_LINEAR)

        pad_width = max(target_width - resized_width, 0)
        pad_height = max(target_height - resized_height, 0)
        pad_left = pad_width / 2.0
        pad_top = pad_height / 2.0
        left = int(math.floor(pad_left))
        right = int(math.ceil(pad_width - left))
        top = int(math.floor(pad_top))
        bottom = int(math.ceil(pad_height - top))

        padded = cv2.copyMakeBorder(
            resized,
            top,
            bottom,
            left,
            right,
            borderType=cv2.BORDER_CONSTANT,
            value=(metadata.preprocess.pad_value, metadata.preprocess.pad_value, metadata.preprocess.pad_value),
        )
        tensor = padded.astype(np.float32) / 255.0
        tensor = np.transpose(tensor, (2, 0, 1))
        tensor = np.expand_dims(tensor, axis=0).astype(np.float32)
        return tensor, (original_width, original_height), _LetterboxInfo(scale=scale, pad_x=pad_left, pad_y=pad_top)

    def _decode_outputs(
        self,
        *,
        outputs: Sequence[Any],
        metadata: YoloModelMetadata,
        infer_settings: Dict[str, Any],
        image_size: Tuple[int, int],
        letterbox: _LetterboxInfo,
    ) -> List[Dict[str, Any]]:
        if not outputs:
            return []
        raw = np.asarray(outputs[0], dtype=np.float32)
        predictions = self._normalize_output_layout(raw, metadata.output_layout)
        detections: List[Dict[str, Any]] = []
        allowed_classes = infer_settings["classes"]
        class_count = metadata.class_count

        for image_index, image_predictions in enumerate(predictions):
            candidates: List[Dict[str, Any]] = []
            for row in image_predictions:
                if row.shape[0] < 4 + class_count:
                    continue
                class_scores = row[4 : 4 + class_count]
                if class_scores.size == 0:
                    continue
                class_id = int(np.argmax(class_scores))
                if allowed_classes is not None and class_id not in allowed_classes:
                    continue
                score = float(class_scores[class_id])
                if score < infer_settings["conf"]:
                    continue
                bbox_xyxy = self._decode_bbox(
                    cx=float(row[0]),
                    cy=float(row[1]),
                    width=float(row[2]),
                    height=float(row[3]),
                    image_size=image_size,
                    letterbox=letterbox,
                )
                bbox_xywh = [
                    float(bbox_xyxy[0]),
                    float(bbox_xyxy[1]),
                    float(max(bbox_xyxy[2] - bbox_xyxy[0], 0.0)),
                    float(max(bbox_xyxy[3] - bbox_xyxy[1], 0.0)),
                ]
                candidates.append(
                    {
                        "image_index": image_index,
                        "class_id": class_id,
                        "label": metadata.class_names[class_id],
                        "score": score,
                        "bbox_xyxy": bbox_xyxy,
                        "bbox_xywh": bbox_xywh,
                    }
                )
            detections.extend(
                self._apply_nms(
                    candidates,
                    iou_threshold=infer_settings["iou"],
                    max_det=infer_settings["max_det"],
                    agnostic=infer_settings["agnostic_nms"],
                )
            )
        return detections

    @staticmethod
    def _normalize_output_layout(raw: np.ndarray, layout: str) -> np.ndarray:
        normalized = np.asarray(raw, dtype=np.float32)
        if normalized.ndim == 2:
            normalized = np.expand_dims(normalized, axis=0)
        if normalized.ndim != 3:
            raise ValueError(f"Unsupported YOLO output rank: {normalized.shape}")
        if layout == "bcn":
            return np.transpose(normalized, (0, 2, 1))
        if layout == "bnc":
            return normalized
        raise ValueError(f"Unsupported YOLO output layout: {layout}")

    @staticmethod
    def _decode_bbox(
        *,
        cx: float,
        cy: float,
        width: float,
        height: float,
        image_size: Tuple[int, int],
        letterbox: _LetterboxInfo,
    ) -> List[float]:
        x1 = cx - width / 2.0
        y1 = cy - height / 2.0
        x2 = cx + width / 2.0
        y2 = cy + height / 2.0

        scale = max(letterbox.scale, 1e-9)
        x1 = (x1 - letterbox.pad_x) / scale
        y1 = (y1 - letterbox.pad_y) / scale
        x2 = (x2 - letterbox.pad_x) / scale
        y2 = (y2 - letterbox.pad_y) / scale

        width_limit, height_limit = image_size
        x1 = float(np.clip(x1, 0.0, max(width_limit - 1, 0)))
        y1 = float(np.clip(y1, 0.0, max(height_limit - 1, 0)))
        x2 = float(np.clip(x2, 0.0, max(width_limit - 1, 0)))
        y2 = float(np.clip(y2, 0.0, max(height_limit - 1, 0)))
        return [x1, y1, x2, y2]

    def _apply_nms(
        self,
        detections: List[Dict[str, Any]],
        *,
        iou_threshold: float,
        max_det: int,
        agnostic: bool,
    ) -> List[Dict[str, Any]]:
        if not detections:
            return []
        if agnostic:
            selected = self._nms_indices(detections, iou_threshold=iou_threshold)
            return [detections[index] for index in selected[: max(int(max_det), 0)]]

        grouped: Dict[int, List[int]] = {}
        for index, det in enumerate(detections):
            grouped.setdefault(int(det["class_id"]), []).append(index)

        kept: List[Dict[str, Any]] = []
        for indices in grouped.values():
            class_detections = [detections[index] for index in indices]
            selected = self._nms_indices(class_detections, iou_threshold=iou_threshold)
            kept.extend(class_detections[index] for index in selected)
        kept.sort(key=lambda item: float(item["score"]), reverse=True)
        return kept[: max(int(max_det), 0)]

    @staticmethod
    def _nms_indices(detections: List[Dict[str, Any]], *, iou_threshold: float) -> List[int]:
        order = sorted(range(len(detections)), key=lambda idx: float(detections[idx]["score"]), reverse=True)
        kept: List[int] = []
        while order:
            current = order.pop(0)
            kept.append(current)
            order = [
                index
                for index in order
                if YoloService._bbox_iou(
                    detections[current]["bbox_xyxy"],
                    detections[index]["bbox_xyxy"],
                )
                <= iou_threshold
            ]
        return kept

    @staticmethod
    def _bbox_iou(box_a: Sequence[float], box_b: Sequence[float]) -> float:
        ax1, ay1, ax2, ay2 = [float(value) for value in box_a]
        bx1, by1, bx2, by2 = [float(value) for value in box_b]
        inter_x1 = max(ax1, bx1)
        inter_y1 = max(ay1, by1)
        inter_x2 = min(ax2, bx2)
        inter_y2 = min(ay2, by2)
        inter_w = max(inter_x2 - inter_x1, 0.0)
        inter_h = max(inter_y2 - inter_y1, 0.0)
        inter_area = inter_w * inter_h
        if inter_area <= 0:
            return 0.0
        area_a = max(ax2 - ax1, 0.0) * max(ay2 - ay1, 0.0)
        area_b = max(bx2 - bx1, 0.0) * max(by2 - by1, 0.0)
        union = max(area_a + area_b - inter_area, 1e-9)
        return inter_area / union

    @staticmethod
    def _repo_root() -> Path:
        env_root = YoloService._env_base_path()
        if env_root is not None:
            return env_root
        return Path(__file__).resolve().parents[3]

    @staticmethod
    def _env_base_path() -> Path | None:
        raw = str(os.environ.get("AURA_BASE_PATH") or "").strip()
        if not raw:
            return None
        root = Path(raw).expanduser()
        try:
            resolved = root.resolve()
        except OSError:
            return None
        if all((resolved / item).exists() for item in ("config.yaml", "plans", "models")):
            return resolved
        return None

    @staticmethod
    def _resolve_capture_origin(app: Any, capture: Any) -> Tuple[int, int]:
        screen = getattr(app, "screen", None)
        if screen is not None and hasattr(screen, "get_client_rect"):
            client_rect = screen.get_client_rect()
            if client_rect:
                return int(client_rect[0]), int(client_rect[1])
        window_rect = getattr(capture, "window_rect", None)
        if isinstance(window_rect, (list, tuple)) and len(window_rect) >= 2:
            return int(window_rect[0]), int(window_rect[1])
        return 0, 0
