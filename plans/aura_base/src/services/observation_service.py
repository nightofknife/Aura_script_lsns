"""Immutable frames, valid observations and deadline-bound synchronous polling.

Coordinates on this boundary are client coordinates. Recognition kernels stay in
VisionService and OcrService; failed recognition never proves absence.
"""
from __future__ import annotations

import math
import re
import time
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass, replace
from pathlib import Path
from typing import Any, Literal
from uuid import uuid4

import cv2
import numpy as np

from packages.aura_core.api import service_info
from packages.aura_core.scheduler.operation_scope import OperationScope, OperationTimeout, current_operation_scope

from .ocr_service import OcrResult
from .prepared_template import PreparedTemplate
from ._target_identity import freeze_target_identity, read_target_identity
from .vision_service import MatchResult

Rect = tuple[int, int, int, int]
Presence = Literal["present", "absent", "unknown"]
_COMPACT_TEXT = re.compile(r"[\s\u3000\|:：,，。.!！?？（）()\[\]【】<>《》'\"`~\-]+")


def normalize_targets(text_to_find: str | list[str]) -> list[str]:
    if isinstance(text_to_find, str):
        values = [text_to_find]
    elif isinstance(text_to_find, list):
        values = [str(value) for value in text_to_find]
    else:
        raise TypeError("text_to_find must be a string or a list of strings")
    values = [value for value in values if value.strip()]
    if not values:
        raise ValueError("text_to_find must contain at least one non-empty target")
    return values


def normalize_match_text(value: Any) -> str:
    return _COMPACT_TEXT.sub("", str(value or "")).casefold()


def matches_text(actual: str, target: str, match_mode: str, *, normalize: bool) -> bool:
    actual = normalize_match_text(actual) if normalize else str(actual or "")
    target = normalize_match_text(target) if normalize and match_mode != "regex" else str(target or "")
    if match_mode == "exact":
        return actual == target
    if match_mode == "contains":
        return target in actual
    if match_mode == "regex":
        try:
            return bool(re.search(target, actual))
        except re.error:
            # Preserve the public find_text action's literal fallback.
            return target in actual
    return False


def _rect(value: Sequence[int]) -> Rect:
    if len(value) != 4:
        raise ValueError("ROI must be (client_x, client_y, width, height)")
    if any(isinstance(item, bool) or not math.isfinite(float(item)) or int(item) != item for item in value):
        raise ValueError("ROI values must be finite integers")
    x, y, width, height = map(int, value)
    if x < 0 or y < 0 or width <= 0 or height <= 0:
        raise ValueError("ROI must have nonnegative origin and positive size")
    return x, y, width, height


@dataclass(frozen=True, eq=False)
class Frame:
    id: str
    timestamp: float
    image: np.ndarray
    color_space: Literal["RGB", "BGR", "GRAY"]
    roi: Rect
    target_identity: Any = None

    def __post_init__(self):
        image = self.image
        if not isinstance(image, np.ndarray) or image.dtype != np.uint8 or image.size == 0:
            raise ValueError("Frame requires nonempty uint8 image pixels")
        if image.ndim not in (2, 3) or (image.ndim == 3 and image.shape[2] not in (1, 3, 4)):
            raise ValueError("Unsupported frame image shape")
        if self.color_space not in ("RGB", "BGR", "GRAY"):
            raise ValueError("Frame color_space must be RGB, BGR or GRAY")
        if self.color_space == "GRAY" and image.ndim == 3 and image.shape[2] != 1:
            raise ValueError("GRAY frame requires one channel")
        roi = _rect(self.roi)
        if roi[2:] != (image.shape[1], image.shape[0]):
            raise ValueError("Frame ROI dimensions must equal image dimensions")
        if not math.isfinite(float(self.timestamp)):
            raise ValueError("Frame timestamp must be finite")
        # Bytes backing prevents callers from re-enabling write access.
        snapshot = np.frombuffer(image.tobytes(order="C"), dtype=image.dtype).reshape(image.shape)
        object.__setattr__(self, "image", snapshot)
        object.__setattr__(self, "roi", roi)
        object.__setattr__(self, "target_identity", freeze_target_identity(self.target_identity))

    @classmethod
    def from_image(cls, image: np.ndarray, *, color_space: str = "RGB", roi: Rect | None = None,
                   target_identity: Any = None, timestamp: float | None = None,
                   frame_id: str | None = None) -> Frame:
        if not isinstance(image, np.ndarray) or image.ndim not in (2, 3):
            raise ValueError("Frame requires an image ndarray with two or three dimensions")
        return cls(frame_id or uuid4().hex, time.monotonic() if timestamp is None else timestamp,
                   image, color_space, roi if roi is not None else (0, 0, image.shape[1], image.shape[0]),
                   target_identity)

    def crop(self, roi: Rect | None) -> Frame:
        """Crop an absolute client ROI; the stored origin is applied once."""
        if roi is None:
            return self
        x, y, width, height = _rect(roi)
        ox, oy, fw, fh = self.roi
        if x < ox or y < oy or x + width > ox + fw or y + height > oy + fh:
            raise ValueError("ROI must lie entirely inside the captured frame")
        if (x, y, width, height) == self.roi:
            return self
        return Frame.from_image(self.image[y-oy:y-oy+height, x-ox:x-ox+width],
                                color_space=self.color_space, roi=(x, y, width, height),
                                target_identity=self.target_identity, timestamp=self.timestamp, frame_id=self.id)

    def rgb_image(self) -> np.ndarray:
        if self.color_space != "BGR":
            return self.image
        if self.image.ndim == 2 or self.image.shape[2] == 1:
            return self.image
        code = cv2.COLOR_BGRA2RGBA if self.image.shape[2] == 4 else cv2.COLOR_BGR2RGB
        return cv2.cvtColor(self.image, code)


@dataclass(frozen=True)
class Observation:
    state: Presence
    frame_id: str | None = None
    value: Any = None
    semantic_key: Any = None
    client_center: tuple[int, int] | None = None
    reason: str | None = None

    def __post_init__(self):
        if self.state not in ("present", "absent", "unknown"):
            raise ValueError(f"Invalid observation state: {self.state}")


@dataclass(frozen=True)
class WaitResult:
    matched: bool
    observation: Observation
    probes: int = 0
    reason: str | None = None

    @property
    def value(self) -> Any:
        return self.observation.value


class CaptureError(RuntimeError):
    """No valid current frame was obtained."""

    def __init__(self, message: str = "capture_failed", *, reason: str = "capture_failed", roi: Rect | None = None):
        super().__init__(message)
        self.reason = reason
        self.roi = roi


@dataclass
class _Stability:
    required: int
    tolerance: float | None
    count: int = 0
    key: Any = None
    center: tuple[int, int] | None = None
    frames: set[str | None] | None = None

    def feed(self, observation: Observation, accepted: bool, key: Any) -> bool:
        if observation.state == "unknown" or not accepted:
            self.count, self.frames = 0, None
            return False
        same = self.count > 0 and key == self.key
        if same and self.tolerance is not None:
            a, b = self.center, observation.client_center
            same = (a is None and b is None) or (
                a is not None and b is not None
                and abs(a[0] - b[0]) <= self.tolerance
                and abs(a[1] - b[1]) <= self.tolerance)
        if not same:
            self.count, self.key, self.center, self.frames = 0, key, observation.client_center, set()
        if observation.frame_id not in self.frames:
            self.frames.add(observation.frame_id)
            self.count += 1
            self.center = observation.client_center
        return self.count >= self.required


@service_info(alias="observation", public=True, deps={"app": "app", "vision": "vision", "ocr": "ocr"})
class ObservationService:
    def __init__(self, app=None, vision=None, ocr=None):
        self.app, self.vision, self.ocr = app, vision, ocr

    def _scope(self, scope: OperationScope | None) -> OperationScope:
        if scope is None:
            scope = current_operation_scope()
        target = getattr(self.app, "target_runtime", None)
        if scope is not None:
            if target is not None and scope.target is not None and target is not scope.target:
                raise ValueError("Observation app and operation scope have different targets")
            if target is not None and scope.target is None:
                return OperationScope(parent=scope, target=target)
            return scope
        return OperationScope(target=target)

    def _identity(self):
        runtime = getattr(self.app, "target_runtime", None)
        return read_target_identity(runtime)

    def capture(self, *, scope: OperationScope | None = None, roi: Rect | None = None) -> Frame:
        scope = self._scope(scope)
        with scope.activate():
            scope.checkpoint()
            if self.app is None:
                raise CaptureError("Capture requires an app service", roi=roi)
            if roi is not None:
                roi = _rect(roi)
            try:
                before = self._identity()
                result = self.app.capture(rect=roi)
                scope.checkpoint()
                after = self._identity()
                scope.checkpoint()
                if before != after:
                    raise CaptureError("Capture target identity changed", roi=roi)
                if not result.success or result.image is None:
                    raise CaptureError(getattr(result, "error_message", "") or "capture_failed", roi=roi)
                actual_roi = getattr(result, "relative_rect", None) or roi
                if (actual_roi is not None and isinstance(result.image, np.ndarray)
                        and result.image.shape[:2] != (actual_roi[3], actual_roi[2])):
                    raise CaptureError("Capture image dimensions do not match the ROI",
                                       reason="capture_size_invalid", roi=roi)
                frame = Frame.from_image(result.image, color_space="RGB", roi=actual_roi, target_identity=after)
                scope.checkpoint()
                return frame
            except OperationTimeout:
                raise
            except CaptureError:
                scope.checkpoint()
                raise
            except Exception as exc:
                scope.checkpoint()
                raise CaptureError(str(exc), roi=roi) from exc

    def capture_regions(self, regions: Sequence[Rect], *, scope: OperationScope | None = None) -> Frame:
        """Capture the enclosing rectangle of nonempty absolute client ROIs once.

        The caller owns this observation round and passes the returned Frame to
        crop/matching readers. Every call captures anew; no frame is retained
        across polling rounds or input, and no probes are evaluated here.
        """
        scope = self._scope(scope)
        with scope.activate():
            scope.checkpoint()
            regions = tuple(_rect(region) for region in regions)
            if not regions:
                raise ValueError("capture_regions requires at least one ROI")
            left = min(region[0] for region in regions)
            top = min(region[1] for region in regions)
            right = max(region[0] + region[2] for region in regions)
            bottom = max(region[1] + region[3] for region in regions)
            roi = (left, top, right - left, bottom - top)
            frame = self.capture(scope=scope, roi=roi)
            if frame.roi[2:] != roi[2:]:
                raise CaptureError("Capture image dimensions do not match the requested regions",
                                   reason="capture_size_invalid", roi=roi)
            if frame.roi != roi:
                raise CaptureError("Capture region differs from the requested regions",
                                   reason="capture_region_mismatch", roi=roi)
            scope.checkpoint()
            return frame

    async def capture_regions_async(self, regions: Sequence[Rect], *, scope: OperationScope | None = None) -> Frame:
        scope = self._scope(scope)
        return await scope.run_async(self.capture_regions, regions, scope=scope)

    async def capture_async(self, *, scope: OperationScope | None = None, roi: Rect | None = None) -> Frame:
        scope = self._scope(scope)
        return await scope.run_async(self.capture, scope=scope, roi=roi)

    def observe(self, probes, *, scope: OperationScope | None = None, roi: Rect | None = None,
                frame: Frame | None = None):
        """Evaluate callable probes on one frame, isolating each probe failure.

        A callable returns one Observation; mappings and sequences retain their
        keys/order. Probes must return Observation, never bool or None.
        """
        scope = self._scope(scope)
        single = callable(probes)
        mapping = isinstance(probes, Mapping)
        items = [(None, probes)] if single else list(probes.items()) if mapping else list(enumerate(probes))
        if not items or any(not callable(probe) for _, probe in items):
            raise ValueError("observe requires one or more callable probes")
        with scope.activate():
            scope.checkpoint()
            try:
                current = frame.crop(roi) if frame is not None else self.capture(scope=scope, roi=roi)
            except CaptureError as exc:
                scope.checkpoint()
                values = [(key, Observation("unknown", reason=str(exc))) for key, _ in items]
            else:
                values = []
                for key, probe in items:
                    scope.checkpoint()
                    try:
                        result = probe(current)
                        if not isinstance(result, Observation):
                            raise TypeError("A probe must return Observation")
                        if result.frame_id not in (None, current.id):
                            raise ValueError("Probe returned evidence from a different frame")
                        result = replace(result, frame_id=current.id)
                    except OperationTimeout:
                        raise
                    except Exception as exc:
                        result = Observation("unknown", current.id, reason=f"probe_failed: {exc}")
                    scope.checkpoint()
                    values.append((key, result))
            scope.checkpoint()
            return values[0][1] if single else dict(values) if mapping else tuple(value for _, value in values)

    async def observe_async(self, probes, *, scope: OperationScope | None = None, roi: Rect | None = None,
                            frame: Frame | None = None):
        scope = self._scope(scope)
        return await scope.run_async(self.observe, probes, scope=scope, roi=roi, frame=frame)

    def _recognize(self, frame: Frame, scope: OperationScope, call: Callable, key: Any) -> Observation:
        scope.checkpoint()
        try:
            result = call()
            scope.checkpoint()
            error = (getattr(result, "debug_info", None) or {}).get("error")
            if error:
                return Observation("unknown", frame.id, result, key, reason=str(error))
            if not isinstance(result.found, (bool, np.bool_)):
                return Observation("unknown", frame.id, result, key, reason="invalid_found_flag")
            if not math.isfinite(float(result.confidence)):
                return Observation("unknown", frame.id, result, key, reason="nonfinite_confidence")
            x, y = frame.roi[:2]
            updates = {"debug_info": dict(result.debug_info)}
            if result.found:
                center = result.center_point
                if (not isinstance(center, (tuple, list, np.ndarray)) or len(center) != 2
                        or any(isinstance(item, (bool, np.bool_)) or not math.isfinite(float(item)) for item in center)
                        or not (0 <= center[0] < frame.roi[2] and 0 <= center[1] < frame.roi[3])):
                    return Observation("unknown", frame.id, result, key, reason="invalid_match_center")
                updates["center_point"] = (center[0] + x, center[1] + y)
                if result.rect is not None:
                    updates["rect"] = (result.rect[0] + x, result.rect[1] + y, *result.rect[2:])
                if isinstance(result, MatchResult) and result.top_left is not None:
                    updates["top_left"] = (result.top_left[0] + x, result.top_left[1] + y)
            result = replace(result, **updates)
            scope.checkpoint()
            return Observation("present" if result.found else "absent", frame.id, result, key, result.center_point)
        except OperationTimeout:
            raise
        except Exception as exc:
            scope.checkpoint()
            return Observation("unknown", frame.id, semantic_key=key, reason=f"recognition_failed: {exc}")

    def _validate_template(self, frame: Frame, template, mask=None, *, use_grayscale=True, preprocess="none"):
        if isinstance(template, PreparedTemplate):
            image = template.template_image
            if mask is not None:
                raise ValueError("PreparedTemplate owns its mask")
            if template.use_grayscale != use_grayscale or template.preprocess != (preprocess or "none").lower():
                raise ValueError("PreparedTemplate preparation parameters must match the request")
            mask = template.mask_image
        elif isinstance(template, (str, Path)):
            image = self.vision.load_template_image(template)
        else:
            image = template
        if not isinstance(image, np.ndarray) or image.ndim not in (2, 3) or image.size == 0:
            raise ValueError("Template must have valid image pixels")
        if image.shape[0] > frame.image.shape[0] or image.shape[1] > frame.image.shape[1]:
            # OpenCV may swap its two inputs when both dimensions are reversed.
            raise ValueError("Template must fit inside the source ROI")
        if mask is not None:
            mask_image = self.vision.load_template_image(mask) if isinstance(mask, (str, Path)) else mask
            if not isinstance(mask_image, np.ndarray) or mask_image.shape[:2] != image.shape[:2]:
                raise ValueError("Mask dimensions must equal template dimensions")

    def match_template(self, frame: Frame, template: np.ndarray | str | Path | PreparedTemplate, *, scope: OperationScope | None = None,
                       roi: Rect | None = None, threshold: float = .8, use_grayscale: bool = True,
                       match_method: int = cv2.TM_CCOEFF_NORMED, preprocess: str = "none",
                       mask=None, score_scale: float | None = None) -> Observation:
        if not math.isfinite(float(threshold)):
            raise ValueError("threshold must be finite")
        scope = self._scope(scope)
        with scope.activate():
            scope.checkpoint()
            current = frame.crop(roi)
            key = str(template) if isinstance(template, (str, Path)) else id(template)

            def match():
                self._validate_template(current, template, mask, use_grayscale=use_grayscale, preprocess=preprocess)
                scope.checkpoint()
                return self.vision.find_template(
                    source_image=current.rgb_image(), template_image=str(template) if isinstance(template, Path) else template,
                    mask_image=str(mask) if isinstance(mask, Path) else mask, threshold=threshold,
                    use_grayscale=use_grayscale, match_method=match_method, preprocess=preprocess, score_scale=score_scale)

            return self._recognize(current, scope, match, key)

    def match_templates(self, frame: Frame, templates, *, scope: OperationScope | None = None,
                        roi: Rect | None = None, threshold: float = .8, use_grayscale: bool = True,
                        match_method: int = cv2.TM_CCOEFF_NORMED, preprocess: str = "none",
                        masks=None, score_scale: float | None = None) -> tuple[Observation, ...]:
        """One VisionService batch on one frame; retain individual validity."""
        if not math.isfinite(float(threshold)):
            raise ValueError("threshold must be finite")
        templates = tuple(templates)
        if not templates:
            raise ValueError("Template collection must not be empty")
        if masks is not None:
            masks = tuple(masks)
            if len(masks) != len(templates):
                raise ValueError("masks must have one entry for each template")
        scope = self._scope(scope)
        with scope.activate():
            scope.checkpoint()
            current = frame.crop(roi)
            keys = [str(item) if isinstance(item, (str, Path)) else id(item) for item in templates]
            observations = [None] * len(templates)
            valid_indices = []
            for index, template in enumerate(templates):
                scope.checkpoint()
                try:
                    self._validate_template(current, template, masks[index] if masks is not None else None,
                                            use_grayscale=use_grayscale, preprocess=preprocess)
                except OperationTimeout:
                    raise
                except Exception as exc:
                    observations[index] = Observation("unknown", current.id, semantic_key=keys[index],
                                                      reason=f"recognition_failed: {exc}")
                else:
                    valid_indices.append(index)
                scope.checkpoint()
            if not valid_indices:
                return tuple(observations)
            try:
                results = self.vision.find_templates_batch(
                    source_image=current.rgb_image(),
                    template_images=[str(templates[index]) if isinstance(templates[index], Path) else templates[index]
                                     for index in valid_indices],
                    mask_images=[str(masks[index]) if isinstance(masks[index], Path) else masks[index]
                                 for index in valid_indices] if masks is not None else None,
                    threshold=threshold, use_grayscale=use_grayscale, match_method=match_method,
                    preprocess=preprocess, score_scale=score_scale)
                scope.checkpoint()
                if len(results) != len(valid_indices):
                    raise ValueError("VisionService batch result count differs from template count")
            except OperationTimeout:
                raise
            except Exception as exc:
                scope.checkpoint()
                for index in valid_indices:
                    observations[index] = Observation("unknown", current.id, semantic_key=keys[index],
                                                      reason=f"recognition_failed: {exc}")
                return tuple(observations)
            for index, result in zip(valid_indices, results):
                observations[index] = self._recognize(current, scope, lambda item=result: item, keys[index])
            scope.checkpoint()
            return tuple(observations)

    def find_text(self, frame: Frame, text_to_find: str | list[str], *, scope: OperationScope | None = None,
                  roi: Rect | None = None, match_mode: str = "exact", normalize: bool = False,
                  min_confidence: float = 0., recognition_only: bool = False,
                  det_resize_long: int | None = None,
                  use_textline_orientation: bool = True,
                  rotate_tall_crops: bool = False) -> Observation:
        targets = normalize_targets(text_to_find)
        floor = float(min_confidence)
        if not 0. <= floor <= 1.:
            raise ValueError("min_confidence must be between 0.0 and 1.0")
        if match_mode not in ("exact", "contains", "regex"):
            raise ValueError(f"Unsupported text match mode: {match_mode}")
        scope = self._scope(scope)
        with scope.activate():
            scope.checkpoint()
            current = frame.crop(roi)

            def recognize():
                kwargs = {"source_image": current.rgb_image(), "recognition_only": recognition_only,
                          "det_resize_long": det_resize_long,
                          "use_textline_orientation": use_textline_orientation,
                          "rotate_tall_crops": rotate_tall_crops}
                if isinstance(text_to_find, str) and not normalize and floor == 0.:
                    result = self.ocr.find_text(text_to_find=text_to_find, match_mode=match_mode, **kwargs)
                    if result.found:
                        result = replace(result, debug_info={**result.debug_info, "matched_target": text_to_find})
                    return result
                recognized = self.ocr.recognize_all(**kwargs)
                if getattr(recognized, "debug_info", {}).get("error"):
                    raise RuntimeError(recognized.debug_info["error"])
                candidates = []
                for result in recognized.results:
                    if not math.isfinite(float(result.confidence)):
                        raise ValueError("nonfinite_confidence")
                    if result.confidence >= floor:
                        for target in targets:
                            if matches_text(result.text, target, match_mode, normalize=normalize):
                                candidates.append((result.confidence, result, target))
                                break
                if candidates:
                    _, result, target = max(candidates, key=lambda item: item[0])
                    return replace(result, found=True, debug_info={**result.debug_info, "matched_target": target})
                return OcrResult(debug_info={"all_recognized_results": list(recognized.results)})

            observation = self._recognize(current, scope, recognize, (tuple(targets), match_mode, bool(normalize)))
            if observation.state == "present":
                matched_target = observation.value.debug_info["matched_target"]
                observation = replace(observation, semantic_key=(matched_target, match_mode, bool(normalize)))
            scope.checkpoint()
            return observation

    def wait(self, observe: Callable[[], Observation], accept: Callable[[Observation], bool], *,
             scope: OperationScope, stable_scans: int = 1, semantic_key: Callable | None = None,
             center_tolerance: float | None = None, interval: float = .1,
             max_probes: int | None = None) -> WaitResult:
        if isinstance(stable_scans, bool) or int(stable_scans) != stable_scans or stable_scans < 1:
            raise ValueError("stable_scans must be a positive integer")
        if not math.isfinite(float(interval)) or interval < 0:
            raise ValueError("interval must be finite and nonnegative")
        if center_tolerance is not None and (not math.isfinite(float(center_tolerance)) or center_tolerance < 0):
            raise ValueError("center_tolerance must be finite and nonnegative")
        if max_probes is not None and (isinstance(max_probes, bool) or int(max_probes) != max_probes or max_probes < 1):
            raise ValueError("max_probes must be a positive integer")
        scope = self._scope(scope)
        tracker = _Stability(int(stable_scans), center_tolerance)
        last, count = Observation("unknown", reason="not_observed"), 0
        try:
            with scope.activate():
                while True:
                    scope.checkpoint()
                    count += 1
                    try:
                        last = observe()
                        if not isinstance(last, Observation):
                            raise TypeError("wait observer must return Observation")
                    except OperationTimeout:
                        raise
                    except Exception as exc:
                        last = Observation("unknown", reason=f"observation_failed: {exc}")
                    scope.checkpoint()
                    accepted = last.state != "unknown" and bool(accept(last))
                    key = semantic_key(last) if accepted and semantic_key is not None else last.semantic_key
                    stable = tracker.feed(last, accepted, key)
                    scope.checkpoint()
                    if stable:
                        return WaitResult(True, last, count)
                    if max_probes is not None and count >= max_probes:
                        return WaitResult(False, last, count, "max_probes")
                    scope.sleep(interval)
        except OperationTimeout:
            scope.check_cancelled()
            return WaitResult(False, last, count, "timeout")

    async def wait_async(self, observe: Callable[[], Observation], accept: Callable[[Observation], bool], *,
                         scope: OperationScope, stable_scans: int = 1, semantic_key: Callable | None = None,
                         center_tolerance: float | None = None, interval: float = .1,
                         max_probes: int | None = None) -> WaitResult:
        # One tracked worker owns capture, recognition, comparisons and sleep.
        # The scope drains it on cancellation; no event loop is created here.
        scope = self._scope(scope)
        result = []

        def run():
            value = self.wait(observe, accept, scope=scope, stable_scans=stable_scans,
                              semantic_key=semantic_key, center_tolerance=center_tolerance,
                              interval=interval, max_probes=max_probes)
            result.append(value)
            return value

        try:
            return await scope.run_async(run)
        except OperationTimeout:
            scope.check_cancelled()
            return replace(result[0], matched=False, reason="timeout") if result else WaitResult(
                False, Observation("unknown", reason="not_observed"), reason="timeout")
