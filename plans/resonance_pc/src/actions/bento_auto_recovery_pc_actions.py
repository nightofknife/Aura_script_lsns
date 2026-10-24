"""Consume top-position bentos using measured fatigue and separate start/stop thresholds."""

from __future__ import annotations

import copy
import math
import time
from typing import Any

import cv2

from packages.aura_core.api import action_info, requires_services
from packages.aura_core.observability.logging.core_logger import logger
from packages.aura_core.scheduler.cancellation import is_current_task_cancel_requested

from ._bento_observation import work_meal_slot_state
from ._fatigue_observation import FatigueConfirmation, read_fatigue_pair
from .bento_consumption_pc_actions import (
    BentoConsumptionError,
    BentoConsumptionSession,
    load_consumption_layout,
)
from .love_bento_pc_actions import LoveBentoScanner, load_love_bento_catalog
from .player_recovery_pc_actions import _roi, load_recovery_layout
from .runtime_preflight_pc_actions import resonance_pc_require_client_resolution


KINDS = ("work_meals", "love_bentos")


def validate_auto_inputs(
    eat_work_meals: bool,
    eat_love_bentos: bool,
    bento_priority: list[str],
    bento_start_threshold: int | None,
    bento_stop_threshold: int | None,
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
    for name, value in (("bento_start_threshold", bento_start_threshold),
                        ("bento_stop_threshold", bento_stop_threshold)):
        if type(value) is not int or value < 0:
            raise ValueError(f"{name} must be an explicitly supplied nonnegative integer")
    if bento_start_threshold <= bento_stop_threshold:
        raise ValueError("bento_start_threshold must be greater than bento_stop_threshold")
    return tuple(bento_priority)


def load_auto_layout(vision: Any) -> dict:
    layout = load_consumption_layout(vision)
    reference = (1280, 720)
    for key in ("fatigue_ratio_roi", "first_love_food_roi",
                "first_love_selected_roi"):
        layout[key] = _roi(layout[key], reference, key)
    if type(layout["max_portions"]) is not int or not 1 <= layout["max_portions"] <= 24:
        raise ValueError("Invalid bento portion limit")
    return layout


class AutoBentoConsumptionSession(BentoConsumptionSession):
    def __init__(self, app, ocr, vision, layout, recovery_layout, catalog, *, priority,
                 bento_start_threshold, bento_stop_threshold):
        super().__init__(app, ocr, vision, None, layout, recovery_layout, catalog)
        self.priority = priority
        self.bento_start_threshold = bento_start_threshold
        self.bento_stop_threshold = bento_stop_threshold
        self.initial_fatigue: int | None = None
        self.final_fatigue: int | None = None
        self.last_observed_fatigue: int | None = None

    def locate(self, meal):
        self.stage = "locate"
        self.selected_roi = meal["selected_roi"]
        return meal["click_point"]

    def record(self, meal, phase):
        if phase == "consumption_pending":
            self.guard()
        row = self.items[-1]
        row["phase"] = phase
        if phase == "consumption_pending":
            # Use may succeed even if its effect cannot subsequently be read.
            # Never report the preceding observation as the post-use fatigue.
            self.final_fatigue = None
        if phase == "consumption_confirmed":
            row["consumed"] = True
        self.requires_refresh = True
        if phase == "completed":
            row["completed"] = True
        logger.info("[AutoBento] portion=%s phase=%s kind=%s",
                    len(self.items), phase, meal["kind"])

    def read_fatigue(self) -> int:
        self.stage = "read_fatigue"
        confirmation = FatigueConfirmation()
        deadline = min(self.deadline, time.monotonic() + self.layout["timeout_sec"])
        while time.monotonic() < deadline:
            self.guard()
            frame = self.snapshot()
            if not self.is_cabinet(frame=frame):
                self.fail("bento_cabinet_missing", "Cabinet disappeared before fatigue reading")
            pair, reason = read_fatigue_pair(
                frame, self.layout["fatigue_ratio_roi"], self.ocr, guard=self.guard,
            )
            self.guard()
            if time.monotonic() >= deadline:
                break
            if confirmation.feed(pair, frame.id):
                current, maximum = pair
                logger.info("[AutoBento] measured_fatigue=%s/%s", current, maximum)
                return current
            if pair is None:
                logger.debug("[AutoBento] fatigue_read_rejected reason=%s", reason)
            self.sleep()
        self.fail("bento_fatigue_unrecognized", "Could not read stable fatigue ratio in cabinet")

    def observe_fatigue(self) -> int:
        self.final_fatigue = None
        value = self.read_fatigue()
        self.final_fatigue = self.last_observed_fatigue = value
        self.requires_refresh = False
        return value

    def work_slots(self) -> list[int]:
        self.stage = "scan_work_slots"
        targets = self.reader.layout["templates"]
        keys = ("bento_present", "bento_absent")
        paths = [targets[key]["resolved_path"] for key in keys]
        previous = None
        deadline = min(self.deadline, time.monotonic() + self.layout["timeout_sec"])
        while time.monotonic() < deadline:
            self.guard()
            frame = self.snapshot()
            if not self.is_cabinet(frame=frame):
                self.fail("bento_cabinet_missing", "Cabinet disappeared during work-meal scan")
            states = []
            for slot in self.reader.layout["bento_slots"]:
                hits = self.vision.find_templates_batch(
                    source_image=self.capture(slot["roi"], frame=frame), template_images=paths,
                    threshold=min(targets[key]["threshold"] for key in keys),
                    use_grayscale=True, match_method=cv2.TM_CCOEFF_NORMED, preprocess="none",
                )
                if len(hits) != 2 or any((getattr(hit, "debug_info", None) or {}).get("error") for hit in hits):
                    self.fail("bento_work_slot_match_failed", "Work-meal template matching failed")
                state = work_meal_slot_state(
                    hits, [targets[key]["threshold"] for key in keys], require_finite=True,
                )
                if state not in ("present", "absent"):
                    states = []
                    break
                states.append(state == "present")
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
        logger.info("[AutoBento] first_love food=%s score=%.4f margin=%.4f roi=%s",
                    food["name"], score, margin, self.layout["first_love_food_roi"])
        return {"kind": "love_bentos", "food_id": food["id"], "food_name": food["name"],
                "rating_stars": food["rating_stars"],
                "click_point": click_point, "selected_roi": selected_roi}

    def choose_next(self) -> tuple[dict | None, str]:
        for kind in self.priority:
            if kind == "work_meals":
                for index in self.work_slots():
                    slot = self.reader.layout["bento_slots"][index]
                    roi = slot["roi"]
                    candidate = {"kind": "work_meals", "issue_time": slot["issue_time"],
                                 "food_name": "铁盟工作餐", "rating_stars": None,
                                 "click_point": [roi[0]+roi[2]//2, roi[1]+roi[3]//2],
                                 "selected_roi": self.layout["work_selected_rois"][index]}
                    return candidate, "selected"
            else:
                candidate = self.first_love()
                if candidate is not None:
                    return candidate, "selected"
        return None, "no_available_meals"

    def run_auto(self) -> dict:
        self.enter()
        self.scroll_top()
        self.initial_fatigue = self.observe_fatigue()
        if self.initial_fatigue <= self.bento_start_threshold:
            self.return_main()
            return self.result("skipped", "start_threshold_not_exceeded")
        reason = "stop_threshold_reached"
        for _ in range(self.layout["max_portions"]):
            self.guard()
            candidate, reason = self.choose_next()
            if candidate is None:
                break
            before = self.final_fatigue
            self.consume(candidate)
            current = self.observe_fatigue()
            if current >= before:
                self.fail("bento_fatigue_not_decreased", "Confirmed bento consumption did not lower measured fatigue")
            if current <= self.bento_stop_threshold:
                reason = "stop_threshold_reached"
                break
        else:
            self.fail("bento_portion_limit_reached", "Bento portion limit reached before the stop threshold")
        self.return_main()
        return self.result("completed" if self.items else "skipped", reason)

    def result(self, status, reason, error=None):
        consumed = [row for row in self.items if row["consumed"]]
        recovered = (self.initial_fatigue - self.final_fatigue
                     if self.initial_fatigue is not None and self.final_fatigue is not None else None)
        return {
            "success": status in {"completed", "skipped"}, "status": status, "reason": reason,
            "page_state": self.page, "consumed_count": len(consumed),
            "completed_count": sum(row["completed"] for row in self.items),
            "work_meals_used": sum(row["meal"]["kind"] == "work_meals" for row in consumed),
            "love_bentos_used": sum(row["meal"]["kind"] == "love_bentos" for row in consumed),
            "initial_fatigue": self.initial_fatigue, "final_fatigue": self.final_fatigue,
            "last_observed_fatigue": self.last_observed_fatigue,
            "recovered_fatigue": recovered, "recovered_fatigue_source": "measured_difference",
            "bento_start_threshold": self.bento_start_threshold,
            "bento_stop_threshold": self.bento_stop_threshold, "priority": list(self.priority),
            "items": copy.deepcopy(self.items), "requires_refresh": self.requires_refresh,
            "failure_stage": self.stage if error else None, "error": str(error) if error else None,
            "elapsed_ms": int((time.monotonic() - self.started) * 1000),
        }


@action_info(name="resonance_pc.consume_bentos_to_floor", public=True, read_only=False, timeout=660,
             description="Start above the supplied fatigue threshold; measure after each portion until the stop threshold.")
@requires_services(app="plans/aura_base/app", ocr="plans/aura_base/ocr", vision="plans/aura_base/vision")
def resonance_pc_consume_bentos_to_floor(
    eat_work_meals: bool = True,
    eat_love_bentos: bool = True,
    bento_priority: list[str] | None = None,
    bento_start_threshold: int | None = 600,
    bento_stop_threshold: int | None = 100,
    app: Any = None,
    ocr: Any = None,
    vision: Any = None,
) -> dict:
    priority = validate_auto_inputs(
        eat_work_meals, eat_love_bentos,
        ["work_meals", "love_bentos"] if bento_priority is None else bento_priority,
        bento_start_threshold, bento_stop_threshold,
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
        bento_start_threshold=bento_start_threshold, bento_stop_threshold=bento_stop_threshold,
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
