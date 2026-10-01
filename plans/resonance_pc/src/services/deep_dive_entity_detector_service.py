"""Plan-owned entity inference with explicit providers and exact-frame caching."""
from __future__ import annotations

from copy import deepcopy
from pathlib import Path
import threading
import time

import numpy as np

from packages.aura_core.api import service_info
from packages.aura_core.config.service import ConfigService

from ._deep_dive_entity_fusion import fuse_entities, local_player_heads
from ._deep_dive_entity_onnx import EntityOnnxBackend, ROI, decode_raw_entities, prepare_rgb_roi, validate_model

_PREFIX = "resonance_pc.deep_dive.entity_detector"
_REPOSITORY = Path(__file__).resolve().parents[4]
_DEFAULT_MODEL = "plans/resonance_pc/data/models/deep_dive_entities.onnx"


class _DefaultConfig:
    def get(self, _key, default=None):
        return default


@service_info(alias="resonance_pc_deep_dive_entity_detector", public=True, singleton=True,
    deps={"config": "core/config"}, description="Three-class deep-dive ONNX entity inference and local pink-head fusion.")
class DeepDiveEntityDetectorService:
    """One serialized model/session and at most one exact RGB result.

    Loading is lazy. close() releases the session and cache; a later detect()
    can reload for a new run. The caller owns source-frame/pose synchronization.
    """

    def __init__(self, config: ConfigService | None = None):
        self._config = config if config is not None else _DefaultConfig()
        self._lock = threading.RLock()
        model = Path(str(self._get("model_path", _DEFAULT_MODEL))).expanduser()
        self.model_path = (model if model.is_absolute() else _REPOSITORY/model).resolve()
        self.weak_confidence = float(self._get("weak_confidence", .05))
        self.confirm_confidence = float(self._get("confirm_confidence", .6))
        self.nms_iou = float(self._get("nms_iou", .5))
        self.max_det = int(self._get("max_det", 64))
        self.hybrid = bool(self._get("hybrid", True))
        if not (0 < self.weak_confidence <= self.confirm_confidence <= 1 and self.confirm_confidence >= .6):
            raise ValueError("Entity weak_confidence must be positive and at most confirm_confidence; positive confirmation must remain >= .6.")
        if not (0 < self.nms_iou <= 1) or self.max_det < 1:
            raise ValueError("Entity nms_iou must be (0,1] and max_det must be positive.")
        self._backend = EntityOnnxBackend(config=self._config, config_prefix=_PREFIX,
            runtime_name="deep-dive entity detector", install_hint="requirements/optional-vision-onnx-cpu.txt; DirectML: onnxruntime-directml")
        self._session = None
        self._input_name = None
        self._provider = None
        self._cached_rgb = None
        self._cached_packet = None
        self._executions = 0
        self._cache_hits = 0
        self._last_diagnostic = {}

    def _get(self, suffix, default):
        return self._config.get(f"{_PREFIX}.{suffix}", default)

    def initialize(self):
        with self._lock:
            if self._session is None:
                if not self.model_path.is_file():
                    raise FileNotFoundError(f"Deep-dive entity ONNX model not found: {self.model_path}")
                session, provider = self._backend.create_session(self.model_path)
                name = validate_model(session)
                self._session, self._input_name, self._provider = session, name, provider
            return self.status()

    @staticmethod
    def _validate_rgb(image):
        if not isinstance(image, np.ndarray) or image.dtype != np.uint8 or image.shape != (720, 1280, 3):
            raise ValueError("Deep-dive entity detection requires uint8 RGB [720,1280,3].")

    def detect(self, image_rgb):
        return self.detect_packet(image_rgb)["targets"]

    def detect_packet(self, image_rgb):
        self._validate_rgb(image_rgb)
        with self._lock:
            started = time.perf_counter()
            if self._cached_rgb is not None and np.array_equal(image_rgb, self._cached_rgb):
                self._cache_hits += 1
                packet = deepcopy(self._cached_packet)
                packet.update(model_executed=False, cached=True)
                return packet
            # This snapshot is the model/fusion/cache source; caller arrays may
            # be reused after the call without corrupting the cached identity.
            rgb = image_rgb.copy()
            self.initialize()
            tensor, letterbox = prepare_rgb_roi(rgb)
            infer_started = time.perf_counter()
            raw = self._session.run(None, {self._input_name: tensor})[0]
            inference_sec = time.perf_counter()-infer_started
            targets = decode_raw_entities(raw, letterbox, weak_confidence=self.weak_confidence,
                confirm_confidence=self.confirm_confidence, nms_iou=self.nms_iou, max_det=self.max_det)
            proposal_limit_reached = len(targets) >= self.max_det
            head_diagnostic, decisions = {}, []
            if self.hybrid:
                heads, head_diagnostic = local_player_heads(rgb)
                targets, decisions = fuse_entities(targets, heads, image_rgb=rgb)
            self._executions += 1
            packet = dict(targets=targets, model_executed=True, coverage_valid=not proposal_limit_reached, cached=False,
                proposal_limit_reached=proposal_limit_reached,
                coverage_roi=list(ROI), weak_confidence=self.weak_confidence,
                confirm_confidence=self.confirm_confidence)
            self._cached_rgb, self._cached_packet = rgb, deepcopy(packet)
            self._last_diagnostic = dict(inference_sec=inference_sec, total_sec=time.perf_counter()-started,
                head=head_diagnostic, fusion=decisions, target_count=len(targets))
            return packet

    def status(self):
        with self._lock:
            return dict(ready=self._session is not None, model_path=str(self.model_path), provider=self._provider,
                hybrid=self.hybrid, weak_confidence=self.weak_confidence, confirm_confidence=self.confirm_confidence,
                model_executions=self._executions, cache_hits=self._cache_hits, last=deepcopy(self._last_diagnostic))

    def close(self):
        with self._lock:
            self._session = None
            self._input_name = None
            self._provider = None
            self._cached_rgb = None
            self._cached_packet = None
            self._last_diagnostic = {}
