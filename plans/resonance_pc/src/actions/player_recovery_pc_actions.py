"""Plan-internal, read-only navigation through fatigue recovery information pages."""

from __future__ import annotations

import json
import math
import time
from pathlib import Path
from typing import Any, Callable, Mapping, Sequence

import cv2
import numpy as np

from packages.aura_core.scheduler.cancellation import is_current_task_cancel_requested
from packages.aura_core.utils.exceptions import StopTaskException

_PLAN_ROOT = Path(__file__).resolve().parents[2]
_CONFIG_PATH = _PLAN_ROOT / "data/meta/player_recovery.json"
_TEMPLATE_KEYS = (
    "profile", "fatigue_plus", "fatigue_page", "rest_info", "popup",
    "bento_button", "bento_page", "back", "bento_present", "bento_absent",
)


def _error(message: str) -> StopTaskException:
    return StopTaskException(f"Player recovery refresh failed: {message}", success=False)


def check_cancelled() -> None:
    if is_current_task_cancel_requested():
        raise _error("cancelled")


def _roi(raw: Any, reference: Sequence[int], label: str) -> tuple[int, int, int, int]:
    if not isinstance(raw, (list, tuple)) or len(raw) != 4:
        raise ValueError(f"{label}: ROI must contain x, y, width, height")
    if any(isinstance(v, bool) or not isinstance(v, int) for v in raw):
        raise ValueError(f"{label}: ROI coordinates must be integers")
    x, y, w, h = raw
    if min(x, y) < 0 or min(w, h) <= 0 or x+w > reference[0] or y+h > reference[1]:
        raise ValueError(f"{label}: ROI is outside reference client")
    return x, y, w, h


def load_recovery_layout(vision: Any, *, config_path: Path = _CONFIG_PATH,
                         plan_root: Path = _PLAN_ROOT) -> dict[str, Any]:
    """Validate all selected-feature assets before any game input."""
    config = json.loads(config_path.read_text(encoding="utf-8"))
    if not isinstance(config, dict) or config.get("schema_version") != 1:
        raise ValueError("unsupported player recovery layout")
    if config.get("reference_client") != [1280, 720]:
        raise ValueError("player recovery requires a 1280x720 reference client")
    root = plan_root.resolve()
    reference = config["reference_client"]
    for key in ("sparkling_water_roi", "bento_region"):
        config[key] = _roi(config[key], reference, key)
    digits = config["sparkling_water_digits"]
    if digits.get("template_size") != [32, 48]:
        raise ValueError("free uses digit templates must be 32x48")
    for key in ("threshold", "min_score_margin"):
        value = float(digits[key])
        if not math.isfinite(value) or not 0 < value <= 1:
            raise ValueError(f"invalid free uses digit {key}")
        digits[key] = value
    for key in ("remaining_roi", "limit_roi"):
        digits[key] = _roi(digits[key], config["sparkling_water_roi"][2:], key)
        if digits[key][2] < 32 or digits[key][3] < 48:
            raise ValueError(f"free uses digit template exceeds {key}")
    digit_paths, mask_paths = [], []
    for digit in range(7):
        for suffix, paths in (("", digit_paths), ("_mask", mask_paths)):
            ref = str(Path(digits["directory"]) / f"{digit}{suffix}.png")
            path = Path(vision.resolve_template("resonance_pc", ref, root)).resolve()
            if not path.is_relative_to(root):
                raise ValueError("free uses digit template escapes plan root")
            image = vision.load_image_file(path, cv2.IMREAD_UNCHANGED)
            if image is None or image.shape[:2] != (48, 32) or image.dtype != np.uint8:
                raise ValueError(f"invalid free uses digit asset: {digit}{suffix}")
            if suffix:
                if image.ndim != 2 or not np.any(image) or not np.all((image == 0) | (image == 255)):
                    raise ValueError(f"invalid free uses digit mask: {digit}")
            elif image.ndim != 3 or image.shape[2] != 3:
                raise ValueError(f"free uses digit template must be RGB: {digit}")
            paths.append(str(path))
    digits["resolved_templates"] = digit_paths
    digits["resolved_masks"] = mask_paths
    for key in ("timeout_sec", "poll_interval_sec", "click_interval_sec"):
        value = float(config[key])
        if not 0 < value <= 3:
            raise ValueError(f"invalid recovery timing: {key}")
        config[key] = value
    slots = config["bento_slots"]
    if not isinstance(slots, list) or [s.get("issue_time") for s in slots] != ["05:00", "12:00", "18:00"]:
        raise ValueError("bento must have exactly the three work-meal slots")
    bx, by, bw, bh = config["bento_region"]
    for slot in slots:
        slot["roi"] = _roi(slot["roi"], reference, "bento slot")
        x, y, w, h = slot["roi"]
        if x < bx or y < by or x+w > bx+bw or y+h > by+bh:
            raise ValueError("bento slot is outside capture ROI")
    targets = config["templates"]
    for key in _TEMPLATE_KEYS:
        target = targets[key]
        if not 0 < float(target["threshold"]) <= 1:
            raise ValueError(f"invalid recovery template threshold: {key}")
        resolved = Path(vision.resolve_template("resonance_pc", target["path"], root)).resolve()
        if not resolved.is_relative_to(root):
            raise ValueError(f"recovery template escapes plan root: {key}")
        # The framework owns Unicode-safe loading. Paths passed to matching retain
        # the framework's BGR-file -> RGB-source conversion contract.
        image = vision.load_image_file(resolved, cv2.IMREAD_UNCHANGED)
        if image is None or image.size == 0 or image.ndim not in (2, 3):
            raise ValueError(f"unreadable recovery template: {key}")
        target["resolved_path"] = str(resolved)
        regions = [slot["roi"] for slot in slots] if key in {"bento_present", "bento_absent"} else [target.get("roi")]
        for raw_region in regions:
            region = _roi(raw_region, reference, key)
            if image.shape[1] > region[2] or image.shape[0] > region[3]:
                raise ValueError(f"recovery template exceeds ROI: {key}")
        if "roi" in target:
            target["roi"] = _roi(target["roi"], reference, key)
    return config


class RecoveryReader:
    """One navigation session; only information controls and Back are clicked."""

    def __init__(self, app: Any, ocr: Any, vision: Any, layout: Mapping[str, Any],
                 *, on_page: Callable[[str], None] = lambda page: None):
        self.app, self.ocr, self.vision, self.layout = app, ocr, vision, layout
        self.on_page = on_page
        self.page = "profile"

    def capture(self, region: Sequence[int]) -> np.ndarray:
        check_cancelled()
        result = self.app.capture(rect=tuple(region))
        image = getattr(result, "image", None)
        if not getattr(result, "success", False) or not isinstance(image, np.ndarray):
            raise _error(f"capture failed for ROI {tuple(region)}")
        if image.shape[:2] != (region[3], region[2]):
            raise _error(f"capture size does not match ROI {tuple(region)}")
        return image

    def match(self, key: str) -> Any:
        target = self.layout["templates"][key]
        result = self.vision.find_template(
            source_image=self.capture(target["roi"]),
            template_image=target["resolved_path"],
            threshold=target["threshold"], use_grayscale=True,
            match_method=cv2.TM_CCOEFF_NORMED, preprocess="none",
        )
        if getattr(result, "debug_info", {}).get("error"):
            raise _error(f"template matching failed: {key}")
        return result

    def is_page(self, page: str) -> bool:
        key = {"profile": "profile", "fatigue_recovery": "fatigue_page",
               "sparkling_water_popup": "popup", "bento_cabinet": "bento_page"}[page]
        if not self.match(key).found:
            return False
        # Fatigue title stays visible behind the tooltip.
        if page == "fatigue_recovery" and self.match("popup").found:
            return False
        return True

    def set_page(self, page: str) -> None:
        self.page = page
        self.on_page(page)

    def move(self, source: str, target: str, control: str) -> None:
        deadline = time.monotonic() + self.layout["timeout_sec"]
        next_click = 0.0
        while time.monotonic() < deadline:
            check_cancelled()
            if self.is_page(target):
                self.set_page(target)
                return
            if time.monotonic() >= next_click and self.is_page(source):
                hit = self.match(control)
                if not hit.found:
                    time.sleep(self.layout["poll_interval_sec"])
                    continue
                roi = self.layout["templates"][control]["roi"]
                cx, cy = hit.center_point
                if not (0 <= cx < roi[2] and 0 <= cy < roi[3]):
                    raise _error(f"template click is outside ROI: {control}")
                point = (roi[0] + cx, roi[1] + cy)
                check_cancelled()
                self.set_page("unknown")
                self.app.click(x=int(point[0]), y=int(point[1]))
                next_click = time.monotonic() + self.layout["click_interval_sec"]
            time.sleep(self.layout["poll_interval_sec"])
        raise _error(f"page transition timed out: {source} -> {target}")

    def _match_free_uses_digit(self, image: np.ndarray) -> tuple[int | None, str]:
        digits = self.layout["sparkling_water_digits"]
        check_cancelled()
        results = self.vision.find_templates_batch(
            source_image=image, template_images=digits["resolved_templates"],
            mask_images=digits["resolved_masks"], threshold=digits["threshold"],
            use_grayscale=False, match_method=cv2.TM_SQDIFF_NORMED, preprocess="none",
        )
        if not isinstance(results, list) or len(results) != 7:
            raise _error("free uses digit result count mismatch")
        if any(getattr(hit, "debug_info", {}).get("error") for hit in results):
            raise _error("free uses digit template matching failed")
        scores = [float(hit.confidence) for hit in results]
        if not all(math.isfinite(score) for score in scores):
            return None, "non-finite digit score"
        ranked = sorted(range(7), key=lambda digit: scores[digit], reverse=True)
        best, second = ranked[:2]
        margin = scores[best] - scores[second]
        diagnostic = f"candidate={best}, score={scores[best]:.4f}, margin={margin:.4f}"
        if not results[best].found or scores[best] < digits["threshold"] or margin < digits["min_score_margin"]:
            return None, diagnostic
        return best, diagnostic

    def read_sparkling_water(self) -> dict[str, int]:
        deadline = time.monotonic() + self.layout["timeout_sec"]
        last_diagnostic = "no reliable digits"
        digits = self.layout["sparkling_water_digits"]
        while time.monotonic() < deadline:
            if not self.is_page("sparkling_water_popup"):
                raise _error("free-uses popup disappeared")
            # One raw RGB frame, two small independent ROIs. Never search the
            # denominator in the numerator ROI or fall back to OCR/default zero.
            image = self.capture(self.layout["sparkling_water_roi"])
            values, diagnostics = [], []
            for key in ("remaining_roi", "limit_roi"):
                x, y, w, h = digits[key]
                value, diagnostic = self._match_free_uses_digit(image[y:y+h, x:x+w])
                values.append(value)
                diagnostics.append(f"{key}: {diagnostic}")
            remaining, limit = values
            if remaining is not None and limit is not None and 0 <= remaining <= limit and limit > 0:
                return {"remaining_free_uses": remaining, "daily_free_limit": limit}
            last_diagnostic = "; ".join(diagnostics) + f"; ratio={remaining}/{limit}"
            time.sleep(self.layout["poll_interval_sec"])
        raise _error(f"unable to match free uses: {last_diagnostic}")

    def read_bento(self) -> dict[str, Any]:
        deadline = time.monotonic() + self.layout["timeout_sec"]
        templates = self.layout["templates"]
        keys = ("bento_present", "bento_absent")
        paths = [templates[key]["resolved_path"] for key in keys]
        bx, by, _, _ = self.layout["bento_region"]
        while time.monotonic() < deadline:
            if not self.is_page("bento_cabinet"):
                raise _error("bento cabinet page disappeared")
            image = self.capture(self.layout["bento_region"])
            slots = []
            for slot in self.layout["bento_slots"]:
                check_cancelled()
                x, y, w, h = slot["roi"]
                results = self.vision.find_templates_batch(
                    source_image=image[y-by:y-by+h, x-bx:x-bx+w], template_images=paths,
                    threshold=min(templates[key]["threshold"] for key in keys),
                    use_grayscale=True, match_method=cv2.TM_CCOEFF_NORMED, preprocess="none",
                )
                if not isinstance(results, list) or len(results) != 2:
                    raise _error("bento template result count mismatch")
                if any(getattr(hit, "debug_info", {}).get("error") for hit in results):
                    raise _error("bento template matching failed")
                present, absent = [hit.found and hit.confidence >= templates[key]["threshold"]
                                   for key, hit in zip(keys, results)]
                if present == absent:
                    break
                slots.append({"issue_time": slot["issue_time"], "available": bool(present)})
            if len(slots) == 3:
                return {"available_count": sum(slot["available"] for slot in slots), "slots": slots}
            time.sleep(self.layout["poll_interval_sec"])
        raise _error("bento slot is ambiguous or unrecognized")

    def restore_profile(self) -> None:
        """Use observed page markers to unwind; never blindly click Back."""
        if self.is_page("bento_cabinet"):
            self.move("bento_cabinet", "fatigue_recovery", "back")
        if self.is_page("sparkling_water_popup"):
            self.move("sparkling_water_popup", "fatigue_recovery", "rest_info")
        if self.is_page("fatigue_recovery"):
            self.move("fatigue_recovery", "profile", "back")
        if not self.is_page("profile"):
            self.set_page("unknown")
            raise _error("could not confirm profile after recovery pages")
        self.set_page("profile")

    def read(self, sections: Sequence[str], *, on_updated: Callable[[str], None]) -> dict[str, Any]:
        result = {}
        try:
            self.move("profile", "fatigue_recovery", "fatigue_plus")
            if "sparkling_water" in sections:
                self.move("fatigue_recovery", "sparkling_water_popup", "rest_info")
                result["sparkling_water"] = self.read_sparkling_water()
                on_updated("sparkling_water")
                self.move("sparkling_water_popup", "fatigue_recovery", "rest_info")
            if "bento" in sections:
                self.move("fatigue_recovery", "bento_cabinet", "bento_button")
                result["bento"] = self.read_bento()
                on_updated("bento")
                self.move("bento_cabinet", "fatigue_recovery", "back")
            self.move("fatigue_recovery", "profile", "back")
            return result
        except Exception:
            try:
                self.restore_profile()
            except Exception:
                self.set_page("unknown")
            raise
