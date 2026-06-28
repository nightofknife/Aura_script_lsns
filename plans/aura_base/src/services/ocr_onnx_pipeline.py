from __future__ import annotations

import math
from dataclasses import dataclass
from typing import Any, Iterable, Sequence

import cv2
import numpy as np

from .ocr_contract import OcrModelMetadata


@dataclass(frozen=True)
class OcrOnnxSessions:
    det: Any
    rec: Any
    textline_orientation: Any | None = None
    doc_orientation: Any | None = None


class OnnxOcrPipeline:
    """PP-OCRv5 ONNX Runtime inference pipeline."""

    def __init__(self, *, metadata: OcrModelMetadata, sessions: OcrOnnxSessions, provider: str):
        self.metadata = metadata
        self.sessions = sessions
        self.provider = provider

    def predict(self, image: np.ndarray) -> list[dict[str, Any]]:
        image_bgr = self._ensure_bgr_image(image)
        boxes = self._detect_text_boxes(image_bgr)
        if not boxes:
            return [{"rec_texts": [], "rec_scores": [], "rec_polys": []}]

        crops = [self._get_rotate_crop_image(image_bgr, box) for box in boxes]
        if self._use_textline_orientation():
            crops = self._apply_textline_orientation(crops)
        texts, scores = self._recognize_crops(crops)

        results: list[tuple[np.ndarray, str, float]] = []
        threshold = float(self.metadata.default_score_threshold)
        for box, text, score in zip(boxes, texts, scores):
            if not text:
                continue
            if float(score) < threshold:
                continue
            results.append((box, text, float(score)))

        results.sort(key=lambda item: self._sort_key(item[0]))
        return [
            {
                "rec_texts": [item[1] for item in results],
                "rec_scores": [item[2] for item in results],
                "rec_polys": [item[0].astype(np.float32) for item in results],
                "backend": "onnxruntime",
                "provider": self.provider,
            }
        ]

    def _detect_text_boxes(self, image_bgr: np.ndarray) -> list[np.ndarray]:
        tensor, shape_info = self._preprocess_det(image_bgr)
        outputs = self._run_session(self.sessions.det, tensor)
        pred = np.asarray(outputs[0], dtype=np.float32)
        while pred.ndim > 2:
            pred = pred[0]
        return self._postprocess_det(pred, shape_info)

    def _preprocess_det(self, image_bgr: np.ndarray) -> tuple[np.ndarray, dict[str, float]]:
        src_h, src_w = image_bgr.shape[:2]
        resize_long = int(self.metadata.det_preprocess.resize_long)
        scale = resize_long / max(float(src_h), float(src_w))
        resized_h = max(32, int(round(src_h * scale / 32.0)) * 32)
        resized_w = max(32, int(round(src_w * scale / 32.0)) * 32)
        resized = cv2.resize(image_bgr, (resized_w, resized_h), interpolation=cv2.INTER_LINEAR)

        image = resized.astype(np.float32) * float(self.metadata.det_preprocess.scale)
        mean = np.asarray(self.metadata.det_preprocess.mean, dtype=np.float32).reshape(1, 1, 3)
        std = np.asarray(self.metadata.det_preprocess.std, dtype=np.float32).reshape(1, 1, 3)
        image = (image - mean) / std
        tensor = np.transpose(image, (2, 0, 1))[None, ...].astype(np.float32)
        return tensor, {
            "src_h": float(src_h),
            "src_w": float(src_w),
            "resize_h": float(resized_h),
            "resize_w": float(resized_w),
            "ratio_h": float(resized_h) / max(float(src_h), 1.0),
            "ratio_w": float(resized_w) / max(float(src_w), 1.0),
        }

    def _postprocess_det(self, pred: np.ndarray, shape_info: dict[str, float]) -> list[np.ndarray]:
        pred = np.asarray(pred, dtype=np.float32)
        if pred.ndim != 2:
            raise ValueError(f"Unsupported OCR detection output shape: {pred.shape}")

        post = self.metadata.det_postprocess
        bitmap = (pred > float(post.thresh)).astype(np.uint8) * 255
        contours, _ = cv2.findContours(bitmap, cv2.RETR_LIST, cv2.CHAIN_APPROX_SIMPLE)

        boxes: list[np.ndarray] = []
        for contour in contours[: int(post.max_candidates)]:
            if contour.shape[0] < 3:
                continue
            points = contour.reshape(-1, 2).astype(np.float32)
            score = self._box_score(pred, points)
            if score < float(post.box_thresh):
                continue
            box = self._min_area_box(points)
            if self._short_side(box) < 3:
                continue
            expanded = self._expand_polygon(box, float(post.unclip_ratio))
            box = self._min_area_box(expanded)
            box[:, 0] = np.clip(box[:, 0] / max(shape_info["ratio_w"], 1e-9), 0, shape_info["src_w"] - 1)
            box[:, 1] = np.clip(box[:, 1] / max(shape_info["ratio_h"], 1e-9), 0, shape_info["src_h"] - 1)
            if self._short_side(box) < 3:
                continue
            boxes.append(self._order_points_clockwise(box).astype(np.float32))

        boxes.sort(key=self._sort_key)
        return boxes

    def _apply_textline_orientation(self, crops: list[np.ndarray]) -> list[np.ndarray]:
        session = self.sessions.textline_orientation
        if session is None or not crops:
            return crops

        output: list[np.ndarray] = []
        batch_size = int(self.metadata.pipeline.batch_size)
        for chunk in self._chunks(crops, batch_size):
            tensor = np.stack([self._preprocess_textline_orientation(crop) for crop in chunk], axis=0)
            preds = np.asarray(self._run_session(session, tensor)[0], dtype=np.float32)
            for crop, pred in zip(chunk, preds):
                label_index = int(np.argmax(pred))
                score = float(pred[label_index])
                label = self._orientation_label(label_index)
                if label == self.metadata.textline_orientation.rotate_label and score >= float(
                    self.metadata.textline_orientation.rotate_threshold
                ):
                    output.append(cv2.rotate(crop, cv2.ROTATE_180))
                else:
                    output.append(crop)
        return output

    def _recognize_crops(self, crops: list[np.ndarray]) -> tuple[list[str], list[float]]:
        if not crops:
            return [], []

        texts: list[str] = []
        scores: list[float] = []
        batch_size = int(self.metadata.pipeline.batch_size)
        for chunk in self._chunks(crops, batch_size):
            target_width = self._resolve_recognition_width(chunk)
            tensor = np.stack([self._preprocess_rec_image(crop, target_width) for crop in chunk], axis=0)
            preds = np.asarray(self._run_session(self.sessions.rec, tensor)[0], dtype=np.float32)
            chunk_texts, chunk_scores = self._ctc_decode(preds)
            texts.extend(chunk_texts)
            scores.extend(chunk_scores)
        return texts, scores

    def _preprocess_textline_orientation(self, image_bgr: np.ndarray) -> np.ndarray:
        width, height = [int(item) for item in self.metadata.textline_orientation.image_size]
        resized = cv2.resize(image_bgr, (width, height), interpolation=cv2.INTER_LINEAR)
        image = resized.astype(np.float32) * float(self.metadata.textline_orientation.scale)
        mean = np.asarray(self.metadata.textline_orientation.mean, dtype=np.float32).reshape(1, 1, 3)
        std = np.asarray(self.metadata.textline_orientation.std, dtype=np.float32).reshape(1, 1, 3)
        image = (image - mean) / std
        return np.transpose(image, (2, 0, 1)).astype(np.float32)

    def _preprocess_rec_image(self, image_bgr: np.ndarray, target_width: int) -> np.ndarray:
        _channels, target_height, base_width = [int(item) for item in self.metadata.rec_preprocess.image_shape]
        target_width = max(int(target_width), int(base_width))
        h, w = image_bgr.shape[:2]
        ratio = w / max(float(h), 1.0)
        resized_width = min(target_width, max(1, int(math.ceil(target_height * ratio))))
        resized = cv2.resize(image_bgr, (resized_width, target_height), interpolation=cv2.INTER_LINEAR)
        image = resized.astype(np.float32) / 255.0
        image = (image - 0.5) / 0.5
        image = np.transpose(image, (2, 0, 1))
        padded = np.zeros((3, target_height, target_width), dtype=np.float32)
        padded[:, :, :resized_width] = image
        return padded

    def _resolve_recognition_width(self, crops: Sequence[np.ndarray]) -> int:
        _channels, target_height, base_width = [int(item) for item in self.metadata.rec_preprocess.image_shape]
        session_width = self._session_input_width(self.sessions.rec)
        if session_width is not None:
            return int(session_width)
        max_ratio = max((crop.shape[1] / max(float(crop.shape[0]), 1.0) for crop in crops), default=base_width / target_height)
        dynamic_width = int(math.ceil(max_ratio * target_height / 32.0) * 32)
        return min(max(dynamic_width, base_width), int(self.metadata.rec_preprocess.max_width))

    def _ctc_decode(self, preds: np.ndarray) -> tuple[list[str], list[float]]:
        if preds.ndim != 3:
            raise ValueError(f"Unsupported OCR recognition output shape: {preds.shape}")
        characters = self.metadata.rec_postprocess.character_dict
        blank_index = int(self.metadata.rec_postprocess.blank_index)
        texts: list[str] = []
        scores: list[float] = []

        for batch_pred in preds:
            indices = np.argmax(batch_pred, axis=1)
            probs = np.max(batch_pred, axis=1)
            chars: list[str] = []
            char_scores: list[float] = []
            previous = None
            for class_id, prob in zip(indices.tolist(), probs.tolist()):
                class_id = int(class_id)
                if class_id == blank_index or class_id == previous:
                    previous = class_id
                    continue
                char_index = class_id - 1 if blank_index == 0 else class_id
                if 0 <= char_index < len(characters):
                    chars.append(characters[char_index])
                    char_scores.append(float(prob))
                previous = class_id
            texts.append("".join(chars))
            scores.append(float(np.mean(char_scores)) if char_scores else 0.0)
        return texts, scores

    @staticmethod
    def _run_session(session: Any, tensor: np.ndarray) -> list[Any]:
        inputs = session.get_inputs()
        if not inputs:
            raise RuntimeError("ONNX session has no inputs.")
        input_name = inputs[0].name
        return list(session.run(None, {input_name: tensor.astype(np.float32)}))

    @staticmethod
    def _ensure_bgr_image(image: np.ndarray) -> np.ndarray:
        if image.ndim == 2:
            return cv2.cvtColor(image, cv2.COLOR_GRAY2BGR)
        if image.ndim != 3:
            raise ValueError(f"Unsupported OCR image shape: {image.shape}")
        if image.shape[2] == 3:
            return cv2.cvtColor(image, cv2.COLOR_RGB2BGR)
        if image.shape[2] == 4:
            return cv2.cvtColor(image, cv2.COLOR_RGBA2BGR)
        raise ValueError(f"Unsupported OCR image channel count: {image.shape[2]}")

    @staticmethod
    def _get_rotate_crop_image(image: np.ndarray, points: np.ndarray) -> np.ndarray:
        points = OnnxOcrPipeline._order_points_clockwise(points.astype(np.float32))
        width = int(
            max(
                np.linalg.norm(points[0] - points[1]),
                np.linalg.norm(points[2] - points[3]),
            )
        )
        height = int(
            max(
                np.linalg.norm(points[0] - points[3]),
                np.linalg.norm(points[1] - points[2]),
            )
        )
        width = max(width, 1)
        height = max(height, 1)
        dst = np.array([[0, 0], [width, 0], [width, height], [0, height]], dtype=np.float32)
        matrix = cv2.getPerspectiveTransform(points, dst)
        crop = cv2.warpPerspective(image, matrix, (width, height), borderMode=cv2.BORDER_REPLICATE)
        if crop.shape[0] / max(float(crop.shape[1]), 1.0) >= 1.5:
            crop = cv2.rotate(crop, cv2.ROTATE_90_CLOCKWISE)
        return crop

    @staticmethod
    def _box_score(pred: np.ndarray, points: np.ndarray) -> float:
        h, w = pred.shape[:2]
        x_min = max(int(np.floor(np.min(points[:, 0]))), 0)
        x_max = min(int(np.ceil(np.max(points[:, 0]))), w - 1)
        y_min = max(int(np.floor(np.min(points[:, 1]))), 0)
        y_max = min(int(np.ceil(np.max(points[:, 1]))), h - 1)
        if x_max <= x_min or y_max <= y_min:
            return 0.0
        mask = np.zeros((y_max - y_min + 1, x_max - x_min + 1), dtype=np.uint8)
        shifted = points.copy()
        shifted[:, 0] -= x_min
        shifted[:, 1] -= y_min
        cv2.fillPoly(mask, [shifted.astype(np.int32)], 1)
        return float(cv2.mean(pred[y_min : y_max + 1, x_min : x_max + 1], mask)[0])

    @staticmethod
    def _min_area_box(points: np.ndarray) -> np.ndarray:
        rect = cv2.minAreaRect(points.astype(np.float32))
        box = cv2.boxPoints(rect)
        return OnnxOcrPipeline._order_points_clockwise(box.astype(np.float32))

    @staticmethod
    def _expand_polygon(points: np.ndarray, ratio: float) -> np.ndarray:
        center = np.mean(points, axis=0, keepdims=True)
        scale = max(float(ratio), 1.0)
        return center + (points - center) * scale

    @staticmethod
    def _short_side(points: np.ndarray) -> float:
        points = OnnxOcrPipeline._order_points_clockwise(points.astype(np.float32))
        width = min(np.linalg.norm(points[0] - points[1]), np.linalg.norm(points[2] - points[3]))
        height = min(np.linalg.norm(points[0] - points[3]), np.linalg.norm(points[1] - points[2]))
        return float(min(width, height))

    @staticmethod
    def _order_points_clockwise(points: np.ndarray) -> np.ndarray:
        pts = np.asarray(points, dtype=np.float32)
        if pts.shape[0] < 4:
            rect = cv2.minAreaRect(pts)
            pts = cv2.boxPoints(rect)
        sums = pts.sum(axis=1)
        diffs = np.diff(pts, axis=1).reshape(-1)
        ordered = np.array(
            [
                pts[np.argmin(sums)],
                pts[np.argmin(diffs)],
                pts[np.argmax(sums)],
                pts[np.argmax(diffs)],
            ],
            dtype=np.float32,
        )
        return ordered

    @staticmethod
    def _sort_key(box: np.ndarray) -> tuple[int, int]:
        ordered = OnnxOcrPipeline._order_points_clockwise(box)
        return int(np.min(ordered[:, 1]) // 10), int(np.min(ordered[:, 0]))

    @staticmethod
    def _chunks(items: Sequence[Any], size: int) -> Iterable[Sequence[Any]]:
        for start in range(0, len(items), max(int(size), 1)):
            yield items[start : start + max(int(size), 1)]

    @staticmethod
    def _session_input_width(session: Any) -> int | None:
        try:
            shape = list(session.get_inputs()[0].shape)
        except Exception:
            return None
        if len(shape) >= 4 and isinstance(shape[3], int) and shape[3] > 0:
            return int(shape[3])
        return None

    def _orientation_label(self, label_index: int) -> str:
        labels = self.metadata.textline_orientation.labels
        if 0 <= int(label_index) < len(labels):
            return str(labels[int(label_index)])
        return str(label_index)

    def _use_textline_orientation(self) -> bool:
        return (
            bool(self.metadata.pipeline.use_textline_orientation)
            and bool(self.metadata.textline_orientation.enabled)
            and self.sessions.textline_orientation is not None
        )
