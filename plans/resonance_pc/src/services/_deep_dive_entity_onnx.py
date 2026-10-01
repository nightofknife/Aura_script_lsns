"""RGB ROI inference for the audited three-class YOLO11 raw ONNX export."""
from __future__ import annotations

import ast
from pathlib import Path

import cv2
import numpy as np

from packages.aura_core.observability.logging.core_logger import logger
from packages.aura_core.services.onnx_runtime_backend import OnnxRuntimeBackend

CLASS_NAMES = ("player_head", "singularity", "inspiration")
KINDS = ("player", "singularity", "inspiration")
ROI = (250, 40, 1000, 650)


class EntityOnnxBackend(OnnxRuntimeBackend):
    """Use the shared ORT backend, adding the DirectML provider contract."""

    def create_session_options(self, ort):
        options = super().create_session_options(ort)
        options.intra_op_num_threads = int(self._config_get("session.intra_op_num_threads", 2))
        options.inter_op_num_threads = int(self._config_get("session.inter_op_num_threads", 1))
        if min(options.intra_op_num_threads, options.inter_op_num_threads) < 1:
            raise ValueError("Entity ONNX session thread counts must be positive.")
        return options

    def create_session(self, model_path: Path):
        mode = self._provider_mode()
        if mode not in ("auto", "cpu", "cuda", "dml"):
            raise ValueError("Entity execution_provider must be auto, cpu, cuda or dml.")
        if mode in ("cpu", "cuda"):
            return super().create_session(model_path)
        ort = self.load_onnxruntime_module()
        available = ort.get_available_providers()
        if "DmlExecutionProvider" not in available:
            if mode == "dml":
                raise RuntimeError("DirectML is unavailable; install onnxruntime-directml in the active Python environment.")
            return super().create_session(model_path)
        options = self.create_session_options(ort)
        options.enable_mem_pattern = False
        options.execution_mode = ort.ExecutionMode.ORT_SEQUENTIAL
        device = int(self._config_get("dml_device_id", 0))
        if device < 0:
            raise ValueError("Entity dml_device_id must be nonnegative.")
        try:
            session = ort.InferenceSession(str(model_path), sess_options=options,
                providers=[("DmlExecutionProvider", {"device_id": device}), "CPUExecutionProvider"])
            provider = session.get_providers()[0]
            if provider != "DmlExecutionProvider":
                raise RuntimeError(f"DirectML was requested but ONNX Runtime selected {provider}.")
            return session, provider
        except Exception:
            if mode == "dml":
                raise
            logger.warning("Deep-dive DirectML initialization failed; using the shared CUDA/CPU provider selection.")
            return super().create_session(model_path)


def validate_model(session):
    inputs, outputs = session.get_inputs(), session.get_outputs()
    if len(inputs) != 1 or inputs[0].shape != [1, 3, 640, 640] or inputs[0].type != "tensor(float)":
        raise ValueError("Entity model requires one static FP32 input [1,3,640,640].")
    if len(outputs) != 1 or len(outputs[0].shape) != 3 or outputs[0].shape[:2] != [1, 7]:
        raise ValueError("Entity model requires raw output [1,7,N] without embedded NMS.")
    raw = session.get_modelmeta().custom_metadata_map.get("names", "")
    if not isinstance(raw, str) or len(raw) > 4096:
        raise ValueError("Entity model class metadata is missing or oversized.")
    try:
        names = ast.literal_eval(raw)
        names = tuple(names[i] for i in range(3)) if isinstance(names, dict) and set(names) == {0, 1, 2} else tuple(names)
    except (ValueError, SyntaxError, TypeError, KeyError) as exc:
        raise ValueError("Invalid entity model class metadata.") from exc
    if names != CLASS_NAMES:
        raise ValueError(f"Entity model classes must be {CLASS_NAMES}; got {names}.")
    return inputs[0].name


def prepare_rgb_roi(rgb):
    """Keep the exact integer letterbox padding of the validated research export."""
    x0, y0, x1, y1 = ROI
    roi = rgb[y0:y1, x0:x1]
    height, width = roi.shape[:2]
    ratio = min(640 / height, 640 / width)
    new_width, new_height = round(width * ratio), round(height * ratio)
    dw, dh = (640 - new_width) / 2, (640 - new_height) / 2
    left, top = round(dw - .1), round(dh - .1)
    resized = cv2.resize(roi, (new_width, new_height), interpolation=cv2.INTER_LINEAR)
    padded = cv2.copyMakeBorder(resized, top, round(dh + .1), left, round(dw + .1),
        cv2.BORDER_CONSTANT, value=(114, 114, 114))
    tensor = np.ascontiguousarray(padded.transpose(2, 0, 1)[None], dtype=np.float32) / 255.
    return tensor, (ratio, left, top, width, height)


def decode_raw_entities(raw, letterbox, *, weak_confidence=.05, confirm_confidence=.6, nms_iou=.5, max_det=64):
    raw = np.asarray(raw)
    if raw.ndim != 3 or raw.shape[:2] != (1, 7) or not np.isfinite(raw).all():
        raise ValueError("Entity inference returned invalid or nonfinite raw [1,7,N] output.")
    rows = raw[0].T
    classes = rows[:, 4:].argmax(axis=1)
    scores = rows[np.arange(len(rows)), classes + 4]
    keep = (scores >= weak_confidence) & (rows[:, 2] > 0) & (rows[:, 3] > 0)
    xywh, classes, scores = rows[keep, :4], classes[keep], scores[keep]
    boxes = np.concatenate((xywh[:, :2] - xywh[:, 2:] / 2, xywh[:, :2] + xywh[:, 2:] / 2), axis=1)
    order = np.argsort(-scores, kind="stable")
    selected = []
    areas = np.maximum(0, boxes[:, 2] - boxes[:, 0]) * np.maximum(0, boxes[:, 3] - boxes[:, 1])
    while len(order) and len(selected) < max_det:
        index = int(order[0]); selected.append(index); others = order[1:]
        extent = np.maximum(np.minimum(boxes[index, 2:], boxes[others, 2:]) - np.maximum(boxes[index, :2], boxes[others, :2]), 0)
        intersection = extent[:, 0] * extent[:, 1]
        overlap = intersection / (areas[index] + areas[others] - intersection + 1e-7)
        order = others[(classes[others] != classes[index]) | (overlap <= nms_iou)]
    ratio, left, top, width, height = letterbox
    targets = []
    for index in selected:
        box = boxes[index].copy()
        box[[0, 2]] = np.clip((box[[0, 2]] - left) / ratio, 0, width) + ROI[0]
        box[[1, 3]] = np.clip((box[[1, 3]] - top) / ratio, 0, height) + ROI[1]
        x1, y1, x2, y2 = map(float, box)
        if x2 <= x1 or y2 <= y1:
            continue
        kind = KINDS[int(classes[index])]
        confidence = float(scores[index])
        targets.append(dict(kind=kind, point=[(x1+x2)/2, (y1+y2)/2],
            box=[round(x1), round(y1), max(1, round(x2-x1)), max(1, round(y2-y1))],
            anchor_type="head" if kind == "player" else None, confidence=confidence,
            confirmable=confidence >= confirm_confidence, source="deep_dive_entity_onnx"))
    return targets
