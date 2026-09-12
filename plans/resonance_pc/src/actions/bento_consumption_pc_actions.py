"""Reusable, main-screen-only single-portion bento consumption."""
from __future__ import annotations

import copy
import json
import math
import re
import time
import uuid
from pathlib import Path
from typing import Any, List

import cv2
import numpy as np

from packages.aura_core.api import action_info, requires_services
from packages.aura_core.observability.logging.core_logger import logger
from packages.aura_core.scheduler.cancellation import is_current_task_cancel_requested
from ._bento_consumption_policy import validate_meals, resolve_meal
from ._bento_consumption_store import load_cached_inventory, record_portion
from ._bento_target_locator import find_target_on_frame
from .love_bento_pc_actions import LoveBentoScanner, load_love_bento_catalog
from .player_recovery_pc_actions import RecoveryReader, load_recovery_layout, _roi
from .player_data_pc_actions import _CLICK_PROFILE
from .passenger_pc_actions import _MAIN_SCREEN_TEMPLATE, _MAIN_SCREEN_REGION
from .runtime_preflight_pc_actions import resonance_pc_require_client_resolution


ROOT = Path(__file__).resolve().parents[2]


class BentoConsumptionError(RuntimeError):
    def __init__(self, code, message):
        super().__init__(message)
        self.code = code


def _normalized(text):
    return re.sub(r"\s+", "", str(text)).replace("／", "/")


def load_consumption_layout(vision):
    layout = json.loads((ROOT / "data/meta/bento_consumption.json").read_text(encoding="utf-8"))
    if layout.get("reference_client") != [1280, 720]:
        raise ValueError("Bento consumption requires reference client 1280x720")
    confidence = layout["ocr_min_confidence"]
    if isinstance(confidence, bool) or not isinstance(confidence, (int, float)) or not 0 < confidence <= 1:
        raise ValueError("Invalid bento OCR confidence threshold")
    layout["templates"]["main"] = {
        "path": _MAIN_SCREEN_TEMPLATE, "roi": list(_MAIN_SCREEN_REGION), "threshold": 0.8,
    }
    for name in ("timeout_sec", "animation_timeout_sec", "poll_interval_sec",
                 "after_click_sec", "total_timeout_sec"):
        value = layout[name]
        if isinstance(value, bool) or not isinstance(value, (int, float)) or not math.isfinite(value) or value <= 0:
            raise ValueError(f"Invalid bento timing: {name}")
    for key in ("detail_name_roi", "confirm_text_roi"):
        layout[key] = _roi(layout[key], (1280, 720), key)
    for key in ("work_selected_rois",):
        if len(layout[key]) != 3:
            raise ValueError(f"Three work-meal ROIs are required: {key}")
        layout[key] = [_roi(value, (1280, 720), key) for value in layout[key]]
    for point in [layout["blank_point"], *layout["star_points"]]:
        if len(point) != 2 or any(type(v) is not int for v in point) or not (0 <= point[0] < 1280 and 0 <= point[1] < 720):
            raise ValueError("Invalid bento click point")
    if len(layout["star_points"]) != 5:
        raise ValueError("Exactly five rating star points are required")
    for key, spec in layout["templates"].items():
        spec["roi"] = _roi(spec["roi"], (1280, 720), key)
        threshold = float(spec["threshold"])
        if not math.isfinite(threshold) or not 0 < threshold <= 1:
            raise ValueError(f"Invalid bento threshold: {key}")
        for field in ("path", "mask"):
            if not spec.get(field):
                continue
            path = Path(vision.resolve_template("resonance_pc", spec[field], ROOT)).resolve()
            if not path.is_relative_to(ROOT):
                raise ValueError(f"Bento asset escapes plan: {key}")
            image = vision.load_image_file(path, cv2.IMREAD_UNCHANGED)
            if image is None or image.size == 0 or image.shape[0] > spec["roi"][3] or image.shape[1] > spec["roi"][2]:
                raise ValueError(f"Invalid bento asset: {key}")
            spec["resolved_" + field] = str(path)
    return layout


class _GuardedApp:
    def __init__(self, app, guard):
        self.app, self.guard = app, guard

    def __getattr__(self, name):
        value = getattr(self.app, name)
        if name not in {"capture", "click", "drag"}:
            return value
        def call(*args, **kwargs):
            self.guard()
            result = value(*args, **kwargs)
            if name != "capture" and getattr(result, "success", True) is False:
                raise BentoConsumptionError("bento_input_failed", f"Bento {name} failed")
            return result
        return call


class BentoConsumptionSession:
    def __init__(self, app, ocr, vision, store, layout, recovery_layout, catalog):
        self.layout, self.catalog = layout, copy.deepcopy(catalog)
        self.ocr, self.vision, self.store = ocr, vision, store
        self.started = time.monotonic()
        self.deadline = self.started + layout["total_timeout_sec"]
        self.app = _GuardedApp(app, self.guard)
        self.reader = RecoveryReader(self.app, ocr, vision, recovery_layout, on_page=self.set_page)
        self.session_id = uuid.uuid4().hex
        self.page = "unknown"
        self.stage = "preflight"
        self.items = []
        self.requested_count = 0
        self.inventory = {}
        self.requires_refresh = False
        self.selected_roi = None

    def set_page(self, page):
        self.page = page

    def guard(self):
        if is_current_task_cancel_requested():
            raise BentoConsumptionError("bento_cancelled", "Bento consumption cancelled")
        if time.monotonic() >= self.deadline:
            raise BentoConsumptionError("bento_timeout", "Bento consumption exceeded total timeout")

    def sleep(self, seconds=None):
        until = min(self.deadline, time.monotonic() + (self.layout["poll_interval_sec"] if seconds is None else seconds))
        while time.monotonic() < until:
            self.guard()
            time.sleep(max(0.0, min(0.05, until-time.monotonic())))
        self.guard()

    def fail(self, code, message):
        raise BentoConsumptionError(code, message)

    def capture(self, roi):
        self.guard()
        result = self.app.capture(rect=tuple(roi))
        image = getattr(result, "image", None)
        if not getattr(result, "success", False) or not isinstance(image, np.ndarray) or image.shape[:2] != (roi[3], roi[2]):
            self.fail("bento_capture_failed", f"Invalid capture for {roi}")
        return image

    def match(self, key, roi=None):
        spec = self.layout["templates"][key]
        roi = spec["roi"] if roi is None else roi
        hit = self.vision.find_template(
            source_image=self.capture(roi), template_image=spec["resolved_path"],
            mask_image=spec.get("resolved_mask"), threshold=spec["threshold"],
            use_grayscale=False, match_method=cv2.TM_SQDIFF_NORMED, preprocess="none",
        )
        score = float(hit.confidence)
        if (getattr(hit, "debug_info", None) or {}).get("error") or not math.isfinite(score):
            self.fail("bento_match_failed", f"Invalid template result: {key}")
        center = getattr(hit, "center_point", None)
        found = bool(hit.found) and score >= spec["threshold"]
        logger.debug("[BentoConsumption] template=%s found=%s confidence=%s roi=%s", key, found, score, roi)
        return {"found": found, "center": [roi[0]+int(center[0]), roi[1]+int(center[1])] if center else None}

    def text(self, roi):
        source = cv2.resize(self.capture(roi), None, fx=3, fy=3, interpolation=cv2.INTER_CUBIC)
        result = self.ocr.recognize_all(source_image=source)
        if getattr(result, "success", True) is False or (getattr(result, "debug_info", None) or {}).get("error"):
            self.fail("bento_ocr_failed", "Bento OCR failed")
        observations = [{"text": str(getattr(row, "text", "")), "confidence": getattr(row, "confidence", None)}
                        for row in getattr(result, "results", [])]
        accepted = [row for row in observations if isinstance(row["confidence"], (int, float))
                    and math.isfinite(row["confidence"]) and row["confidence"] >= self.layout["ocr_min_confidence"]]
        value = _normalized("".join(row["text"] for row in accepted))
        logger.info("[BentoConsumption] ocr roi=%s observations=%s normalized=%s", roi, observations, value)
        return value

    def wait(self, probe, label, timeout=None, confirmations=1):
        until = min(self.deadline, time.monotonic() + (timeout or self.layout["timeout_sec"]))
        count = 0
        while time.monotonic() < until:
            self.guard()
            value = probe()
            count = count + 1 if value else 0
            if count >= confirmations:
                return value
            self.sleep()
        self.fail("bento_transition_timeout", f"Timed out waiting for {label}")

    def click(self, point):
        if not point or not (0 <= point[0] < 1280 and 0 <= point[1] < 720):
            self.fail("bento_invalid_click", "Bento click is outside client")
        logger.info("[BentoConsumption] stage=%s click=%s", self.stage, point)
        self.app.click(x=int(point[0]), y=int(point[1]))

    def is_cabinet(self):
        self.guard()
        return self.reader.is_page("bento_cabinet")

    def require_main(self):
        self.stage = "require_main"
        self.wait(lambda: self.match("main")["found"], "main screen", confirmations=2)
        self.page = "city_main"

    def enter(self):
        self.require_main()
        self.stage = "enter_profile"
        self.click(_CLICK_PROFILE)
        self.page = "unknown"
        self.wait(lambda: self.reader.is_page("profile"), "profile")
        self.reader.set_page("profile")
        self.reader.move("profile", "fatigue_recovery", "fatigue_plus")
        self.reader.move("fatigue_recovery", "bento_cabinet", "bento_button")

    def scroll_top(self):
        scanner = LoveBentoScanner(self.reader, self.catalog)
        frame = scanner.stable_frame()
        stationary = 0
        vx, vy, vw, vh = scanner.cfg["viewport"]
        for _ in range(scanner.cfg["max_drags"]):
            self.guard()
            x, y = int(vx+vw/2), int(vy+vh*.25)
            self.app.drag(x, y, x, y+scanner.cfg["scroll_distance"], duration=.6, hold_before_release_sec=.2)
            next_frame = scanner.stable_frame()
            stationary = stationary+1 if scanner.unchanged(frame, next_frame) else 0
            frame = next_frame
            if stationary >= scanner.cfg["stationary_confirmations"]:
                return
        self.fail("bento_top_not_found", "Could not confirm bento list top")

    def locate(self, meal):
        self.stage = "locate"
        if meal["kind"] == "work_meals":
            self.scroll_top()
            roi = next(s["roi"] for s in self.reader.layout["bento_slots"] if s["issue_time"] == meal["issue_time"])
            index = [s["issue_time"] for s in self.reader.layout["bento_slots"]].index(meal["issue_time"])
            self.selected_roi = self.layout["work_selected_rois"][index]
            return [roi[0]+roi[2]//2, roi[1]+roi[3]//2]
        scanner = LoveBentoScanner(self.reader, self.catalog)
        def target_here():
            frame = scanner.stable_frame()
            point = find_target_on_frame(frame, meal, self.catalog, self.vision)
            if point is None:
                return None
            second = scanner.stable_frame()
            return point if find_target_on_frame(second, meal, self.catalog, self.vision) == point else None

        # Check only the requested card on the current viewport first. No full
        # inventory read and no mandatory trip to the bottom before consuming.
        point = target_here()
        if point is not None:
            return self.target_click_point(point)
        self.scroll_top()
        scanner = LoveBentoScanner(self.reader, self.catalog)
        for index in range(scanner.cfg["max_drags"]+1):
            self.guard()
            point = target_here()
            if point is not None:
                return self.target_click_point(point)
            if index < scanner.cfg["max_drags"]:
                vx, vy, vw, vh = scanner.cfg["viewport"]
                x, y = int(vx+vw/2), int(vy+vh*.8)
                self.app.drag(x, y, x, y-scanner.cfg["scroll_distance"], duration=.6, hold_before_release_sec=.2)
        self.fail("bento_target_not_found", "Selected love bento is not in the current list")

    def target_click_point(self, point):
        x, y = point
        rx, ry, _, _ = self.catalog["scanner"]["capture_roi"]
        dx, dy, w, h = self.layout["love_selected_offset_roi"]
        self.selected_roi = [rx+x+dx, ry+y+dy, w, h]
        x1, y1, x2, y2 = self.catalog["geometry"]["food_crop_xyxy"]
        return [rx+x+(x1+x2)//2, ry+y+(y1+y2)//2]

    def record(self, meal, phase):
        # Cancellation stops new input, not persistence of consumption evidence
        # that has already returned successfully from the observation.
        if phase == "consumption_pending":
            self.guard()
        row = self.items[-1]
        row["phase"] = phase
        if phase == "consumption_confirmed":
            row["consumed"] = True
        self.requires_refresh = True
        self.inventory[meal["kind"]] = record_portion(
            self.store, self.session_id, len(self.items), meal, phase,
        )
        if phase == "completed":
            row["completed"] = True
            self.requires_refresh = False
        logger.info("[BentoConsumption] portion=%s phase=%s meal=%s", len(self.items), phase, meal)

    def consume(self, meal):
        point = self.locate(meal)
        self.click(point)
        use_key = "use_work" if meal["kind"] == "work_meals" else "use_love"
        expected_name = _normalized(meal.get("food_name") or "铁盟工作餐")
        self.stage = "select"
        def selected():
            return (self.is_cabinet() and expected_name in self.text(self.layout["detail_name_roi"])
                    and self.match("selected_marker", self.selected_roi)["found"]
                    and self.match(use_key)["found"] and not self.match("use_disabled")["found"])
        self.wait(selected, "selected usable meal", confirmations=2)
        if not selected():
            self.fail("bento_target_changed", "Selection changed before use")
        self.items.append({"meal": copy.deepcopy(meal), "consumed": False, "completed": False,
                           "phase": "selected"})
        self.stage = "use"
        self.record(meal, "consumption_pending")
        use = self.match(use_key)
        if not use["found"]:
            self.fail("bento_target_changed", "Use button disappeared before clicking")
        self.click(use["center"])
        self.page = "transition"
        confirmed = False
        confirmation_matches = 0
        def effect():
            nonlocal confirmed, confirmation_matches
            if self.match("effect")["found"]:
                return True
            if self.match("confirm_use")["found"]:
                confirmation_text = self.text(self.layout["confirm_text_roi"])
                if not confirmation_text:
                    confirmation_matches = 0
                    return False
                if expected_name not in confirmation_text:
                    self.fail("bento_confirmation_mismatch", "Use confirmation names a different meal")
                confirmation_matches += 1
                if confirmation_matches < 2:
                    return False
                if not confirmed:
                    self.stage = "confirm_use"
                    self.click(self.match("confirm_use")["center"])
                    confirmed = True
            else:
                confirmation_matches = 0
            return False
        self.wait(effect, "consumption effect", self.layout["animation_timeout_sec"], confirmations=2)
        self.page = "effect"
        self.stage = "confirm_effect"
        self.record(meal, "consumption_confirmed")
        self.click(self.layout["blank_point"])
        self.page = "transition"
        if meal["kind"] == "love_bentos":
            self.stage = "rating"
            self.wait(lambda: self.match("rating_page")["found"], "rating page")
            self.page = "rating"
            self.record(meal, "rating_pending")
            stars = meal["rating_stars"]
            self.click(self.layout["star_points"][stars-1])
            def rated():
                if not self.match("rating_page")["found"]:
                    return False
                for index, point in enumerate(self.layout["star_points"]):
                    key = "star_on" if index < stars else "star_off"
                    spec = self.layout["templates"][key]
                    w, h = spec["roi"][2:]
                    if not self.match(key, [point[0]-w//2, point[1]-h//2, w, h])["found"]:
                        return False
                return self.match("rating_confirm")["found"]
            self.wait(rated, "selected rating", confirmations=2)
            self.click(self.match("rating_confirm")["center"])
        self.wait(self.is_cabinet, "cabinet after consumption", confirmations=2)
        self.page = "bento_cabinet"
        self.record(meal, "completed")
        return True

    def return_main(self):
        self.stage = "return_main"
        if not self.is_cabinet():
            self.fail("bento_return_wrong_page", "Cannot leave an unconfirmed cabinet")
        def train():
            hit = self.match("train_return")
            return hit if hit["found"] else None
        hit = self.wait(train, "train icon")
        self.click(hit["center"])
        self.page = "transition"
        self.wait(lambda: self.match("main")["found"], "main after train icon", confirmations=2)
        self.page = "city_main"

    def run(self, meals):
        self.requested_count = len(meals)
        if not meals:
            self.require_main()
            return self.result("skipped", "empty_request")
        self.stage = "load_cached_inventory"
        kinds = list(dict.fromkeys(meal["kind"] for meal in meals))
        self.inventory = load_cached_inventory(self.store, kinds, self.catalog)
        # Resolve the whole request before the first use; never silently replace
        # a missing target with another food or a different work-meal slot.
        for request in meals:
            resolve_meal(request, self.inventory, self.catalog)
        self.enter()
        for request in meals:
            self.guard()
            self.consume(resolve_meal(request, self.inventory, self.catalog))
        self.return_main()
        return self.result("completed", "requested_meals_completed")

    def result(self, status, reason, error=None):
        consumed = [row for row in self.items if row["consumed"]]
        return {
            "success": status in {"completed", "skipped"}, "status": status, "reason": reason,
            "page_state": self.page, "requested_count": self.requested_count,
            "consumed_count": len(consumed), "completed_count": sum(row["completed"] for row in self.items),
            "work_meals_used": sum(row["meal"]["kind"] == "work_meals" for row in consumed),
            "love_bentos_used": sum(row["meal"]["kind"] == "love_bentos" for row in consumed),
            "recovered_fatigue": sum(row["meal"]["fatigue_recovery"] for row in consumed),
            "items": copy.deepcopy(self.items), "requires_refresh": self.requires_refresh,
            "failure_stage": self.stage if error else None, "error": str(error) if error else None,
            "elapsed_ms": int((time.monotonic()-self.started)*1000),
        }


@action_info(name="resonance_pc.consume_bentos", public=True, read_only=False,
             description="Consume individual bentos from the main screen and return to it; no route or GUI required.")
@requires_services(app="plans/aura_base/app", ocr="plans/aura_base/ocr", vision="plans/aura_base/vision",
                   persistent_data="core/persistent_data")
def resonance_pc_consume_bentos(
    meals: List[dict],
    app: Any = None, ocr: Any = None, vision: Any = None, persistent_data: Any = None,
) -> dict:
    meals = validate_meals(meals)
    if any(value is None for value in (app, ocr, vision, persistent_data)):
        raise ValueError("Bento consumption requires app, ocr, vision and persistent_data")
    if is_current_task_cancel_requested():
        raise BentoConsumptionError("bento_cancelled", "Cancelled before bento preflight")
    resonance_pc_require_client_resolution(app=app)
    layout = load_consumption_layout(vision)
    recovery_layout = load_recovery_layout(vision)
    catalog = load_love_bento_catalog(
        vision, navigation_only=not any(meal["kind"] == "love_bentos" for meal in meals),
    )
    session = BentoConsumptionSession(app, ocr, vision, persistent_data, layout, recovery_layout, catalog)
    try:
        result = session.run(meals)
    except Exception as exc:
        cancelled = is_current_task_cancel_requested() or getattr(exc, "code", None) == "bento_cancelled"
        result = session.result("cancelled" if cancelled else "failed",
                                getattr(exc, "code", "bento_execution_failed"), exc)
        logger.error("[BentoConsumption] stopped result=%s", result)
        return result
    logger.info("[BentoConsumption] completed result=%s", result)
    return result
