from __future__ import annotations

import math
from dataclasses import dataclass
from typing import Any, Iterable, Sequence

import cv2
import numpy as np

from .ocr_contract import OcrModelMetadata
from ._ocr_timing import OcrTiming, timed
from ._ocr_recognition import OcrRecognitionOptions


@dataclass(frozen=True)
class OcrOnnxSessions:
    det: Any
    rec: Any
    textline_orientation: Any | None = None
    doc_orientation: Any | None = None


class OnnxOcrPipeline:
    """Metadata-driven PP-OCR ONNX Runtime inference pipeline."""

    def __init__(
        self, *, metadata: OcrModelMetadata, sessions: OcrOnnxSessions, provider: str,
        recognition_options: OcrRecognitionOptions | None = None,
    ):
        self.metadata = metadata
        self.sessions = sessions
        self.provider = provider
        self.recognition_options = recognition_options or OcrRecognitionOptions()
        if (self.recognition_options.width_mode == "adaptive"
                and self.recognition_options.min_width > metadata.rec_preprocess.max_width):
            raise ValueError("ocr.recognition.min_width must not exceed the model's max_width.")

    def warmup(self) -> tuple[str, ...]:
        """Exercise every enabled session independently of detected text.

        Warm representative full-screen detection and both single-line and
        configured batch inference, reusing the sessions used by task OCR.
        """
        completed: list[str] = []

        def run(stage: str, operation: Any) -> None:
            try:
                operation()
            except Exception as exc:
                raise RuntimeError(f"OCR {stage} warmup failed: {exc}") from exc
            completed.append(stage)

        det_long = int(self.metadata.det_preprocess.resize_long)
        detection_image = np.zeros((max(32, round(det_long * 9 / 16)), det_long, 3), dtype=np.uint8)
        run("det", lambda: self._detect_text_boxes(detection_image))

        if self._use_textline_orientation():
            width, height = self.metadata.textline_orientation.image_size
            crop = np.zeros((height, width, 3), dtype=np.uint8)
            for batch_size in self._warmup_batch_sizes(self.sessions.textline_orientation):
                run(
                    f"textline_orientation(batch={batch_size})",
                    lambda size=batch_size: self._apply_textline_orientation([crop] * size),
                )

        _channels, height, width = self.metadata.rec_preprocess.image_shape
        warmup_widths = [width]
        if (self.recognition_options.width_mode == "adaptive"
                and self._session_input_width(self.sessions.rec) is None):
            warmup_widths = list(dict.fromkeys(
                min(self.metadata.rec_preprocess.max_width,
                    math.ceil(max(value, self.recognition_options.min_width) / 32) * 32)
                for value in (self.recognition_options.min_width, max(192, self.recognition_options.min_width), width)
            ))
        for warmup_width in warmup_widths:
            crop = np.zeros((height, warmup_width, 3), dtype=np.uint8)
            for batch_size in self._warmup_batch_sizes(self.sessions.rec):
                label = f"rec(batch={batch_size})"
                if len(warmup_widths) > 1:
                    label = f"rec(width={warmup_width},batch={batch_size})"
                run(label, lambda size=batch_size, image=crop: self._recognize_crops([image] * size))

        if self.metadata.pipeline.use_doc_orientation and self.sessions.doc_orientation is not None:
            run("doc_orientation", self._warmup_doc_orientation)
        return tuple(completed)

    def _warmup_batch_sizes(self, session: Any) -> tuple[int, ...]:
        shape = session.get_inputs()[0].shape
        if shape and isinstance(shape[0], int) and shape[0] > 0:
            return (shape[0],)
        batch_size = int(self.metadata.pipeline.batch_size)
        if session is self.sessions.rec:
            batch_size = self.recognition_options.batch_size or batch_size
        return tuple(dict.fromkeys((1, batch_size)))

    def _warmup_doc_orientation(self) -> None:
        # Document orientation has no preprocessing contract in this pipeline.
        # Use its declared tensor dimensions rather than guessing image sizes.
        session = self.sessions.doc_orientation
        inputs = session.get_inputs()
        if len(inputs) != 1:
            raise ValueError("Document orientation warmup requires exactly one model input.")
        model_input = inputs[0]
        if getattr(model_input, "type", "tensor(float)") != "tensor(float)":
            raise ValueError("Document orientation warmup requires a float32 model input.")
        shape = list(model_input.shape)
        if shape and not isinstance(shape[0], int):
            shape[0] = 1
        if not shape or any(not isinstance(size, int) or size <= 0 for size in shape):
            raise ValueError("Document orientation warmup requires concrete non-batch input dimensions.")
        self._run_session(session, np.zeros(shape, dtype=np.float32))

    def predict(
        self, image: np.ndarray, *, recognition_only: bool = False,
        det_resize_long: int | None = None, timing: OcrTiming | None = None,
        use_textline_orientation: bool = True,
        rotate_tall_crops: bool = False,
    ) -> list[dict[str, Any]]:
        """Recognize a full image, or one already cropped upright text line.

        In recognition-only mode the returned polygon describes the input
        region, not a detected text boundary.
        det_resize_long explicitly sets the detection long-side target; when
        omitted, the metadata size is an upper limit without proactive upscaling.
        use_textline_orientation only controls this call's 0/180 classifier;
        detection and geometric crop correction remain enabled in full mode.
        rotate_tall_crops opts into the legacy 90-degree rotation of crops
        whose height/width is at least 1.5; upright crops keep their direction
        by default. It is independent of the 0/180 classifier.
        """
        with timed(timing, "image_prepare"):
            image_bgr = self._ensure_bgr_image(image)
        timing_kwargs = {"timing": timing} if timing is not None else {}
        if recognition_only:
            height, width = image_bgr.shape[:2]
            if height <= 0 or width <= 0:
                raise ValueError("Recognition-only OCR requires a non-empty input image.")
            boxes = [np.asarray([[0, 0], [width, 0], [width, height], [0, height]], dtype=np.float32)]
            crops = [image_bgr]
        else:
            boxes = self._detect_text_boxes(image_bgr, det_resize_long=det_resize_long, **timing_kwargs)
            if not boxes:
                return [{"rec_texts": [], "rec_scores": [], "rec_polys": []}]

            with timed(timing, "crop"):
                crops = [self._get_rotate_crop_image(image_bgr, box, rotate_tall_crops=rotate_tall_crops)
                         for box in boxes]
            if use_textline_orientation and self._use_textline_orientation():
                crops = self._apply_textline_orientation(crops, **timing_kwargs)
        texts, scores = self._recognize_crops(crops, **timing_kwargs)

        with timed(timing, "result_pack"):
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
                    **({"recognition_only": True} if recognition_only else {}),
                }
            ]

    def _detect_text_boxes(
        self, image_bgr: np.ndarray, *, det_resize_long: int | None = None,
        timing: OcrTiming | None = None,
    ) -> list[np.ndarray]:
        with timed(timing, "det_preprocess"):
            tensor, shape_info = self._preprocess_det(image_bgr, det_resize_long=det_resize_long)
        with timed(timing, "det_inference"):
            outputs = self._run_session(self.sessions.det, tensor)
        with timed(timing, "det_postprocess"):
            pred = np.asarray(outputs[0], dtype=np.float32)
            while pred.ndim > 2:
                pred = pred[0]
            return self._postprocess_det(pred, shape_info)

    def _preprocess_det(
        self, image_bgr: np.ndarray, *, det_resize_long: int | None = None,
    ) -> tuple[np.ndarray, dict[str, float]]:
        src_h, src_w = image_bgr.shape[:2]
        resize_long = int(
            self.metadata.det_preprocess.resize_long if det_resize_long is None else det_resize_long
        )
        if resize_long <= 0:
            raise ValueError("det_resize_long must be positive.")
        scale = resize_long / max(float(src_h), float(src_w))
        if det_resize_long is None:
            scale = min(1.0, scale)
        resized_h = max(32, int(round(src_h * scale / 32.0)) * 32)
        resized_w = max(32, int(round(src_w * scale / 32.0)) * 32)
        resized = (
            image_bgr if (resized_h, resized_w) == (src_h, src_w)
            else cv2.resize(image_bgr, (resized_w, resized_h), interpolation=cv2.INTER_LINEAR)
        )

        tensor = np.empty((1, 3, resized_h, resized_w), dtype=np.float32)
        tensor[0] = np.transpose(resized, (2, 0, 1))
        tensor *= float(self.metadata.det_preprocess.scale)
        tensor -= np.asarray(self.metadata.det_preprocess.mean, dtype=np.float32).reshape(1, 3, 1, 1)
        tensor /= np.asarray(self.metadata.det_preprocess.std, dtype=np.float32).reshape(1, 3, 1, 1)
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

    def _apply_textline_orientation(
        self, crops: list[np.ndarray], *, timing: OcrTiming | None = None,
    ) -> list[np.ndarray]:
        session = self.sessions.textline_orientation
        if session is None or not crops:
            return crops

        output: list[np.ndarray] = []
        batch_size = int(self.metadata.pipeline.batch_size)
        width, height = [int(item) for item in self.metadata.textline_orientation.image_size]
        for chunk in self._chunks(crops, batch_size):
            with timed(timing, "ori_preprocess"):
                tensor = np.empty((len(chunk), 3, height, width), dtype=np.float32)
                for index, crop in enumerate(chunk):
                    self._preprocess_textline_orientation(crop, out=tensor[index])
            with timed(timing, "ori_inference"):
                raw_preds = self._run_session(session, tensor)[0]
            with timed(timing, "ori_postprocess"):
                preds = np.asarray(raw_preds, dtype=np.float32)
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

    def _recognize_crops(
        self, crops: list[np.ndarray], *, timing: OcrTiming | None = None,
    ) -> tuple[list[str], list[float]]:
        if not crops:
            return [], []

        with timed(timing, "rec_preprocess"):
            texts = [""] * len(crops)
            scores = [0.0] * len(crops)
            channels, height, _base_width = [int(item) for item in self.metadata.rec_preprocess.image_shape]
            batches = self._plan_recognition_batches(crops)
        for chunk_indices, target_width in batches:
            with timed(timing, "rec_preprocess"):
                chunk = [crops[index] for index in chunk_indices]
                tensor = np.empty((len(chunk), channels, height, target_width), dtype=np.float32)
                for index, crop in enumerate(chunk):
                    self._preprocess_rec_image(crop, target_width, out=tensor[index])
            if timing is not None:
                timing.record_recognition_batch(
                    tensor.shape,
                    [min(target_width, max(1, math.ceil(height * image.shape[1] / max(image.shape[0], 1))))
                     for image in chunk],
                )
            with timed(timing, "rec_inference"):
                raw_preds = self._run_session(self.sessions.rec, tensor)[0]
            with timed(timing, "rec_decode"):
                preds = np.asarray(raw_preds, dtype=np.float32)
                chunk_texts, chunk_scores = self._ctc_decode(preds)
                for index, text, score in zip(chunk_indices, chunk_texts, chunk_scores):
                    texts[index] = text
                    scores[index] = score
        return texts, scores

    def _plan_recognition_batches(self, crops: list[np.ndarray]) -> list[tuple[list[int], int]]:
        """Split only when removing padding saves enough input area.

        Shapes are chosen before preprocessing, so glyph scaling is independent
        of how the crops are grouped. Original indices preserve result order.
        """
        options = self.recognition_options
        batch_size = options.batch_size or int(self.metadata.pipeline.batch_size)
        sorted_indices = sorted(
            range(len(crops)), key=lambda index: crops[index].shape[1] / max(float(crops[index].shape[0]), 1.0),
        )
        input_shape = self.sessions.rec.get_inputs()[0].shape
        fixed_batch = bool(input_shape and isinstance(input_shape[0], int) and input_shape[0] > 0)

        def width(indices: list[int]) -> int:
            return self._resolve_recognition_width([crops[index] for index in indices])

        def split(indices: list[int]) -> list[tuple[list[int], int]]:
            target_width = width(indices)
            if options.batch_mode == "fixed" or fixed_batch or len(indices) < 2:
                return [(indices, target_width)]
            original_cost = len(indices) * target_width
            best_cost, best_cut = original_cost, None
            for cut in range(1, len(indices)):
                cost = cut * width(indices[:cut]) + (len(indices) - cut) * width(indices[cut:])
                if cost < best_cost:
                    best_cost, best_cut = cost, cut
            if best_cut is not None and (original_cost - best_cost) / original_cost >= options.split_min_saving:
                return split(indices[:best_cut]) + split(indices[best_cut:])
            return [(indices, target_width)]

        return [batch for chunk in self._chunks(sorted_indices, batch_size) for batch in split(list(chunk))]

    def _preprocess_textline_orientation(
        self, image_bgr: np.ndarray, *, out: np.ndarray | None = None,
    ) -> np.ndarray:
        width, height = [int(item) for item in self.metadata.textline_orientation.image_size]
        resized = cv2.resize(image_bgr, (width, height), interpolation=cv2.INTER_LINEAR)
        if out is None:
            out = np.empty((3, height, width), dtype=np.float32)
        out[...] = np.transpose(resized, (2, 0, 1))
        out *= float(self.metadata.textline_orientation.scale)
        out -= np.asarray(self.metadata.textline_orientation.mean, dtype=np.float32).reshape(3, 1, 1)
        out /= np.asarray(self.metadata.textline_orientation.std, dtype=np.float32).reshape(3, 1, 1)
        return out

    def _preprocess_rec_image(
        self, image_bgr: np.ndarray, target_width: int, *, out: np.ndarray | None = None,
    ) -> np.ndarray:
        _channels, target_height, base_width = [int(item) for item in self.metadata.rec_preprocess.image_shape]
        if self._session_input_width(self.sessions.rec) is None and self.recognition_options.width_mode == "legacy":
            target_width = max(int(target_width), int(base_width))
        else:
            target_width = int(target_width)
        h, w = image_bgr.shape[:2]
        ratio = w / max(float(h), 1.0)
        resized_width = min(target_width, max(1, int(math.ceil(target_height * ratio))))
        resized = cv2.resize(image_bgr, (resized_width, target_height), interpolation=cv2.INTER_LINEAR)
        if out is None:
            out = np.empty((3, target_height, target_width), dtype=np.float32)
        out.fill(0.0)
        image = out[:, :, :resized_width]
        image[...] = np.transpose(resized, (2, 0, 1))
        image /= 255.0
        image -= 0.5
        image /= 0.5
        return out

    def _resolve_recognition_width(self, crops: Sequence[np.ndarray]) -> int:
        _channels, target_height, base_width = [int(item) for item in self.metadata.rec_preprocess.image_shape]
        session_width = self._session_input_width(self.sessions.rec)
        if session_width is not None:
            return int(session_width)
        max_ratio = max((crop.shape[1] / max(float(crop.shape[0]), 1.0) for crop in crops), default=base_width / target_height)
        dynamic_width = int(math.ceil(max_ratio * target_height / 32.0) * 32)
        minimum = base_width
        if self.recognition_options.width_mode == "adaptive":
            minimum = math.ceil(self.recognition_options.min_width / 32) * 32
        return min(max(dynamic_width, minimum), int(self.metadata.rec_preprocess.max_width))

    def _ctc_decode(self, preds: np.ndarray) -> tuple[list[str], list[float]]:
        if preds.ndim != 3:
            raise ValueError(f"Unsupported OCR recognition output shape: {preds.shape}")
        characters = self.metadata.rec_postprocess.character_dict
        blank_index = int(self.metadata.rec_postprocess.blank_index)
        texts: list[str] = []
        scores: list[float] = []

        for batch_pred in preds:
            indices = np.argmax(batch_pred, axis=1)
            probs = batch_pred[np.arange(batch_pred.shape[0]), indices]
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
        return list(session.run(None, {input_name: np.ascontiguousarray(tensor, dtype=np.float32)}))

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
    def _get_rotate_crop_image(
        image: np.ndarray, points: np.ndarray, *, rotate_tall_crops: bool = False,
    ) -> np.ndarray:
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
        if rotate_tall_crops and crop.shape[0] / max(float(crop.shape[1]), 1.0) >= 1.5:
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
