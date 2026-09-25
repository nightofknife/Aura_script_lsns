"""Consume only top-row work meals or the first love bento, using live card values."""

from __future__ import annotations

import json
import math
import re
import time
from pathlib import Path
from typing import Any

import cv2
import numpy as np

from packages.aura_core.api import action_info, requires_services
from packages.aura_core.observability.logging.core_logger import logger
from packages.aura_core.scheduler.cancellation import is_current_task_cancel_requested

from .bento_consumption_pc_actions import (
    BentoConsumptionError,
    BentoConsumptionSession,
    load_consumption_layout,
)
from .love_bento_pc_actions import LoveBentoScanner, load_love_bento_catalog
from .player_recovery_pc_actions import _roi, load_recovery_layout
from .runtime_preflight_pc_actions import resonance_pc_require_client_resolution


ROOT = Path(__file__).resolve().parents[2]
KINDS = ("work_meals", "love_bentos")


def validate_auto_inputs(
    eat_work_meals: bool,
    eat_love_bentos: bool,
    bento_priority: list[str],
    target_recovery_amount: int,
    allow_exceed_target: bool,
    base_fatigue_reserve: int,
) -> tuple[str, ...]:
    if type(eat_work_meals) is not bool or type(eat_love_bentos) is not bool:
        raise ValueError("Bento type switches must be booleans")
    enabled = {kind for kind, selected in zip(KINDS, (eat_work_meals, eat_love_bentos)) if selected}
    if not enabled:
        raise ValueError("Select at least one bento type")
    if (not isinstance(bento_priority, list) or len(bento_priority) != len(enabled)
            or any(type(kind) is not str or kind not in KINDS for kind in bento_priority)
            or set(bento_priority) != enabled):
        raise ValueError("bento_priority must list every enabled type exactly once")
    if type(target_recovery_amount) is not int or target_recovery_amount < 0:
        raise ValueError("target_recovery_amount must be a nonnegative integer")
    if type(allow_exceed_target) is not bool:
        raise ValueError("allow_exceed_target must be a boolean")
    if type(base_fatigue_reserve) is not int:
        raise ValueError("base_fatigue_reserve must be an integer")
    return tuple(bento_priority)


def load_auto_layout(vision: Any) -> dict:
    layout = load_consumption_layout(vision)
    reference = (1280, 720)
    for key in ("fatigue_ratio_roi", "love_recovery_roi", "first_love_food_roi",
                "first_love_selected_roi"):
        layout[key] = _roi(layout[key], reference, key)
    raw_work = layout["work_recovery_rois"]
    if not isinstance(raw_work, list) or len(raw_work) != 3:
        raise ValueError("Three work-meal recovery ROIs are required")
    layout["work_recovery_rois"] = [_roi(raw, reference, "work recovery") for raw in raw_work]
    if any(roi[2:] != (22, 16) for roi in [*layout["work_recovery_rois"], layout["love_recovery_roi"]]):
        raise ValueError("Recovery digit ROIs must be 22x16")
    if type(layout["max_portions"]) is not int or not 1 <= layout["max_portions"] <= 24:
        raise ValueError("Invalid bento portion limit")
    cfg = layout["recovery_digits"]
    manifest_path = Path(vision.resolve_template("resonance_pc", cfg["manifest"], ROOT)).resolve()
    if not manifest_path.is_relative_to(ROOT):
        raise ValueError("Bento recovery digit manifest escapes plan root")
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    if manifest.get("template_size") != [12, 16]:
        raise ValueError("Bento recovery digit templates must be 12x16")
    for key in ("work_threshold", "love_threshold", "min_score_margin"):
        value = float(cfg[key])
        if not math.isfinite(value) or not 0 < value <= 1:
            raise ValueError(f"Invalid bento recovery digit threshold: {key}")
        cfg[key] = value
    banks: dict[str, dict[int, list[np.ndarray]]] = {}
    for style in ("work", "love"):
        bank = {}
        variants = manifest["styles"][style]["variants"]
        for digit in range(10):
            files = variants[str(digit)]
            if not isinstance(files, list) or not files:
                raise ValueError(f"Missing bento recovery digit: {style}/{digit}")
            images = []
            for name in files:
                path = (manifest_path.parent / style / name).resolve()
                if not path.is_relative_to(manifest_path.parent):
                    raise ValueError("Bento recovery digit template escapes directory")
                image = vision.load_image_file(path, cv2.IMREAD_GRAYSCALE)
                if (image is None or image.shape != (16, 12) or image.dtype != np.uint8
                        or not np.any(image) or not np.all((image == 0) | (image == 255))):
                    raise ValueError(f"Invalid bento recovery digit: {path.name}")
                images.append(image)
            bank[digit] = images
        banks[style] = bank
    cfg["banks"] = banks
    return layout


def normalize_digit(image: np.ndarray) -> np.ndarray | None:
    points = np.argwhere(image > 0)
    if not len(points):
        return None
    top, left = points.min(axis=0)
    bottom, right = points.max(axis=0) + 1
    glyph = image[top:bottom, left:right]
    if glyph.shape[0] > 16 or glyph.shape[1] > 12:
        return None
    output = np.zeros((16, 12), dtype=np.uint8)
    y = (16 - glyph.shape[0]) // 2
    x = (12 - glyph.shape[1]) // 2
    output[y:y+glyph.shape[0], x:x+glyph.shape[1]] = glyph
    return output


def decode_recovery(frame: np.ndarray, bank: dict[int, list[np.ndarray]], *, threshold: float,
                    margin: float) -> tuple[int | None, list[dict]]:
    if frame.shape != (16, 22, 3):
        raise BentoConsumptionError("bento_recovery_capture_invalid", "Recovery digit capture must be 22x16 RGB")
    low, high = frame.min(axis=2), frame.max(axis=2)
    mask = np.where((low >= 175) & (high - low < 58), 255, 0).astype(np.uint8)
    digits, diagnostics = [], []
    for half in (mask[:, :11], mask[:, 11:]):
        glyph = normalize_digit(half)
        if glyph is None:
            return None, diagnostics
        scores = {digit: max(float(cv2.matchTemplate(glyph, template, cv2.TM_CCOEFF_NORMED)[0, 0])
                             for template in variants)
                  for digit, variants in bank.items()}
        if not all(math.isfinite(value) for value in scores.values()):
            return None, diagnostics
        ranked = sorted(scores, key=scores.get, reverse=True)
        best, second = ranked[:2]
        lead = scores[best] - scores[second]
        diagnostics.append({"digit": best, "score": round(scores[best], 4), "margin": round(lead, 4)})
        if scores[best] < threshold or lead < margin:
            return None, diagnostics
        digits.append(best)
    number = digits[0] * 10 + digits[1]
    return (number if 10 <= number <= 99 else None), diagnostics


class AutoBentoConsumptionSession(BentoConsumptionSession):
    def __init__(self, app, ocr, vision, layout, recovery_layout, catalog, *, priority,
                 target_recovery_amount, allow_exceed_target, base_fatigue_reserve):
        super().__init__(app, ocr, vision, None, layout, recovery_layout, catalog)
        self.priority = priority
        self.target_recovery_amount = target_recovery_amount
        self.allow_exceed_target = allow_exceed_target
        self.base_fatigue_reserve = base_fatigue_reserve
        self.initial_fatigue: int | None = None
        self.computed_fatigue: int | None = None
        self.total_recovered = 0

    def locate(self, meal):
        self.stage = "locate"
        self.selected_roi = meal["selected_roi"]
        return meal["click_point"]

    def record(self, meal, phase):
        if phase == "consumption_pending":
            self.guard()
        row = self.items[-1]
        row["phase"] = phase
        if phase == "consumption_confirmed" and not row["consumed"]:
            row["consumed"] = True
            self.total_recovered += meal["fatigue_recovery"]
            self.computed_fatigue -= meal["fatigue_recovery"]
        self.requires_refresh = phase != "completed"
        if phase == "completed":
            row["completed"] = True
        logger.info("[AutoBento] portion=%s phase=%s kind=%s recovery=%s computed_fatigue=%s",
                    len(self.items), phase, meal["kind"], meal["fatigue_recovery"], self.computed_fatigue)

    def read_initial_fatigue(self) -> int:
        self.stage = "read_fatigue"
        previous = None
        deadline = min(self.deadline, time.monotonic() + self.layout["timeout_sec"])
        while time.monotonic() < deadline:
            self.guard()
            if not self.is_cabinet():
                self.fail("bento_cabinet_missing", "Cabinet disappeared before fatigue reading")
            value = self.text(self.layout["fatigue_ratio_roi"])
            match = re.fullmatch(r"(\d+)/(\d+)", value)
            current = int(match[1]) if match else None
            maximum = int(match[2]) if match else None
            if current is not None and maximum is not None and 0 <= current <= maximum and maximum > 0:
                if previous == (current, maximum):
                    logger.info("[AutoBento] initial_fatigue=%s/%s", current, maximum)
                    return current
                previous = (current, maximum)
            else:
                previous = None
            self.sleep()
        self.fail("bento_fatigue_unrecognized", "Could not read stable fatigue ratio in cabinet")

    def read_recovery(self, style: str, roi: tuple[int, int, int, int]) -> int:
        self.stage = "read_recovery"
        cfg = self.layout["recovery_digits"]
        threshold = cfg[f"{style}_threshold"]
        previous = None
        deadline = min(self.deadline, time.monotonic() + self.layout["timeout_sec"])
        last = []
        while time.monotonic() < deadline:
            self.guard()
            if not self.is_cabinet():
                self.fail("bento_cabinet_missing", "Cabinet disappeared before recovery reading")
            value, last = decode_recovery(
                self.capture(roi), cfg["banks"][style], threshold=threshold,
                margin=cfg["min_score_margin"],
            )
            if value is not None:
                if value == previous:
                    logger.info("[AutoBento] recovery=%s style=%s roi=%s scores=%s", value, style, roi, last)
                    return value
                previous = value
            else:
                previous = None
            self.sleep()
        self.fail("bento_recovery_unrecognized", f"Could not read stable {style} recovery: {last}")

    def work_slots(self) -> list[int]:
        self.stage = "scan_work_slots"
        targets = self.reader.layout["templates"]
        keys = ("bento_present", "bento_absent")
        paths = [targets[key]["resolved_path"] for key in keys]
        previous = None
        deadline = min(self.deadline, time.monotonic() + self.layout["timeout_sec"])
        while time.monotonic() < deadline:
            self.guard()
            if not self.is_cabinet():
                self.fail("bento_cabinet_missing", "Cabinet disappeared during work-meal scan")
            states = []
            for slot in self.reader.layout["bento_slots"]:
                hits = self.vision.find_templates_batch(
                    source_image=self.capture(slot["roi"]), template_images=paths,
                    threshold=min(targets[key]["threshold"] for key in keys),
                    use_grayscale=True, match_method=cv2.TM_CCOEFF_NORMED, preprocess="none",
                )
                if len(hits) != 2 or any((getattr(hit, "debug_info", None) or {}).get("error") for hit in hits):
                    self.fail("bento_work_slot_match_failed", "Work-meal template matching failed")
                flags = [bool(hit.found) and math.isfinite(float(hit.confidence))
                         and float(hit.confidence) >= targets[key]["threshold"]
                         for key, hit in zip(keys, hits)]
                if flags[0] == flags[1]:
                    states = []
                    break
                states.append(flags[0])
            if len(states) == 3:
                if previous == states:
                    logger.info("[AutoBento] work_slots=%s", states)
                    return [index for index, available in enumerate(states) if available]
                previous = states
            else:
                previous = None
            self.sleep()
        self.fail("bento_work_slots_unrecognized", "Could not classify all three work-meal slots")

    def first_love(self) -> dict | None:
        self.stage = "scan_first_love"
        scanner = LoveBentoScanner(self.reader, self.catalog)
        frame = scanner.stable_frame()
        rx, ry, _, _ = scanner.cfg["capture_roi"]
        x, y, w, h = self.layout["first_love_food_roi"]
        crop = frame[y-ry:y-ry+h, x-rx:x-rx+w]
        if crop.shape[:2] != (h, w):
            self.fail("bento_first_food_capture_invalid", "First love-bento food ROI is outside the stable capture")
        rows = self.catalog["items"]
        hits = self.vision.find_templates_batch(
            source_image=crop, template_images=[row["resolved"] for row in rows],
            threshold=scanner.cfg["food_threshold"], use_grayscale=False,
            match_method=cv2.TM_CCOEFF_NORMED, preprocess="none",
        )
        if (len(hits) != len(rows) or any((getattr(hit, "debug_info", None) or {}).get("error") for hit in hits)
                or any(not math.isfinite(float(hit.confidence)) for hit in hits)):
            self.fail("bento_first_food_match_failed", "First love-bento food matching failed")
        ranked = sorted(zip(rows, hits), key=lambda pair: float(pair[1].confidence), reverse=True)
        food, hit = ranked[0]
        score = float(hit.confidence)
        second = float(ranked[1][1].confidence) if len(ranked) > 1 else 0.0
        margin = score - second
        if not hit.found or score < scanner.cfg["food_threshold"] or margin < scanner.cfg["food_margin"]:
            logger.info("[AutoBento] first_love=empty food_score=%.4f margin=%.4f roi=%s",
                        score, margin, self.layout["first_love_food_roi"])
            return None
        click_point = [x + w // 2, y + h // 2]
        selected_roi = list(self.layout["first_love_selected_roi"])
        recovery = self.read_recovery("love", self.layout["love_recovery_roi"])
        logger.info("[AutoBento] first_love food=%s score=%.4f margin=%.4f recovery=%s roi=%s",
                    food["name"], score, margin, recovery, self.layout["first_love_food_roi"])
        return {"kind": "love_bentos", "food_id": food["id"], "food_name": food["name"],
                "rating_stars": food["rating_stars"], "fatigue_recovery": recovery,
                "click_point": click_point, "selected_roi": selected_roi}

    def choose_next(self) -> tuple[dict | None, str]:
        any_available = False
        for kind in self.priority:
            if kind == "work_meals":
                for index in self.work_slots():
                    any_available = True
                    slot = self.reader.layout["bento_slots"][index]
                    recovery = self.read_recovery("work", self.layout["work_recovery_rois"][index])
                    roi = slot["roi"]
                    candidate = {"kind": "work_meals", "issue_time": slot["issue_time"],
                                 "food_name": "铁盟工作餐", "rating_stars": None,
                                 "fatigue_recovery": recovery,
                                 "click_point": [roi[0]+roi[2]//2, roi[1]+roi[3]//2],
                                 "selected_roi": self.layout["work_selected_rois"][index]}
                    if self.fits(recovery):
                        return candidate, "selected"
            else:
                candidate = self.first_love()
                if candidate is not None:
                    any_available = True
                    if self.fits(candidate["fatigue_recovery"]):
                        return candidate, "selected"
        return None, "no_fitting_meal" if any_available else "no_available_meals"

    def fits(self, recovery: int) -> bool:
        if self.computed_fatigue - recovery < self.base_fatigue_reserve:
            return False
        if not self.allow_exceed_target and self.total_recovered + recovery > self.target_recovery_amount:
            return False
        return True

    def run_auto(self) -> dict:
        self.enter()
        self.scroll_top()
        self.initial_fatigue = self.read_initial_fatigue()
        self.computed_fatigue = self.initial_fatigue
        reason = "target_reached"
        for _ in range(self.layout["max_portions"]):
            self.guard()
            if self.total_recovered >= self.target_recovery_amount:
                break
            if self.computed_fatigue <= self.base_fatigue_reserve:
                reason = "fatigue_floor_reached"
                break
            candidate, reason = self.choose_next()
            if candidate is None:
                break
            self.consume(candidate)
            if self.allow_exceed_target and self.total_recovered >= self.target_recovery_amount:
                reason = "target_reached"
                break
        else:
            reason = "portion_limit_reached"
        self.return_main()
        return self.result("completed" if self.items else "skipped", reason)

    def result(self, status, reason, error=None):
        result = super().result(status, reason, error)
        result.pop("requested_count", None)
        result.update(initial_fatigue=self.initial_fatigue, computed_fatigue=self.computed_fatigue,
                      target_recovery_amount=self.target_recovery_amount,
                      base_fatigue_reserve=self.base_fatigue_reserve,
                      allow_exceed_target=self.allow_exceed_target, priority=list(self.priority))
        return result


@action_info(name="resonance_pc.consume_bentos_to_floor", public=True, read_only=False, timeout=660,
             description="Consume top-position bentos until target, floor or available meals stop the run.")
@requires_services(app="plans/aura_base/app", ocr="plans/aura_base/ocr", vision="plans/aura_base/vision")
def resonance_pc_consume_bentos_to_floor(
    eat_work_meals: bool = True,
    eat_love_bentos: bool = True,
    bento_priority: list[str] | None = None,
    target_recovery_amount: int = 2000,
    allow_exceed_target: bool = False,
    base_fatigue_reserve: int = -100,
    app: Any = None,
    ocr: Any = None,
    vision: Any = None,
) -> dict:
    priority = validate_auto_inputs(
        eat_work_meals, eat_love_bentos,
        ["work_meals", "love_bentos"] if bento_priority is None else bento_priority,
        target_recovery_amount, allow_exceed_target, base_fatigue_reserve,
    )
    if any(value is None for value in (app, ocr, vision)):
        raise ValueError("Bento recovery requires app, ocr and vision")
    if is_current_task_cancel_requested():
        raise BentoConsumptionError("bento_cancelled", "Cancelled before bento preflight")
    resonance_pc_require_client_resolution(app=app)
    layout = load_auto_layout(vision)
    recovery_layout = load_recovery_layout(vision)
    catalog = load_love_bento_catalog(vision) if eat_love_bentos else load_love_bento_catalog(vision, navigation_only=True)
    session = AutoBentoConsumptionSession(
        app, ocr, vision, layout, recovery_layout, catalog, priority=priority,
        target_recovery_amount=target_recovery_amount, allow_exceed_target=allow_exceed_target,
        base_fatigue_reserve=base_fatigue_reserve,
    )
    try:
        result = session.run_auto()
    except Exception as exc:
        cancelled = is_current_task_cancel_requested() or getattr(exc, "code", None) == "bento_cancelled"
        result = session.result("cancelled" if cancelled else "failed",
                                getattr(exc, "code", "bento_execution_failed"), exc)
        logger.error("[AutoBento] stopped result=%s", result)
        return result
    logger.info("[AutoBento] completed result=%s", result)
    return result
