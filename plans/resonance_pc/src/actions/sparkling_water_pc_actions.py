"""Drink a bounded number of free cups, returning to the same city panel."""
from __future__ import annotations

import asyncio
import copy
import json
import math
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable, Mapping
from uuid import uuid4

import cv2
import numpy as np

from packages.aura_core.api import action_info, requires_services
from packages.aura_core.context.persistence.persistent_data_service import PersistentDataService
from packages.aura_core.observability.logging.core_logger import current_cid, logger
from packages.aura_core.scheduler.cancellation import is_current_task_cancel_requested
from ....aura_base.src.actions._shared import poll_until
from ....aura_base.src.actions.input_actions import click as aura_click
from ._player_data_persistence import USER_INFO_FILE, load_pc_user_info

PLAN_ROOT = Path(__file__).resolve().parents[2]
CONFIG_PATH = PLAN_ROOT / "data/meta/sparkling_water.json"
DIAGNOSTIC_ROOT = PLAN_ROOT.parent.parent / "logs/diagnostics/sparkling_water"


class _UncertainFrame(RuntimeError):
    """A captured frame has no usable match score; it proves neither presence nor absence."""


class SparklingWaterError(RuntimeError):
    def __init__(self, code: str, message: str, detail: dict | None = None):
        super().__init__(message)
        self.code, self.message, self.detail = code, message, detail or {}

    def to_dict(self) -> dict:
        return {"code": self.code, "message": self.message, "detail": self.detail}


def _check_cancelled() -> None:
    if is_current_task_cancel_requested():
        raise asyncio.CancelledError("Sparkling water task cancelled")


def load_sparkling_water_layout(vision: Any) -> dict:
    layout = json.loads(CONFIG_PATH.read_text(encoding="utf-8"))
    if layout.get("reference_client") != [1280, 720]:
        raise ValueError("Sparkling water layout requires a 1280x720 client")
    for name, spec in layout["templates"].items():
        for field in ("path", "mask"):
            if not spec.get(field):
                continue
            path = vision.resolve_template("resonance_pc", spec[field], PLAN_ROOT).resolve()
            if not path.is_relative_to(PLAN_ROOT):
                raise ValueError(f"Invalid sparkling water template path: {name}")
            spec[f"resolved_{field}"] = str(path)
        template = vision.load_image_file(spec["resolved_path"], cv2.IMREAD_COLOR)
        x, y, w, h = spec["roi"]
        if not (0 <= x < x + w <= 1280 and 0 <= y < y + h <= 720):
            raise ValueError(f"Invalid sparkling water ROI: {name}")
        if template.shape[0] > h or template.shape[1] > w:
            raise ValueError(f"Sparkling water template exceeds ROI: {name}")
        if spec.get("resolved_mask"):
            mask = vision.load_image_file(spec["resolved_mask"], cv2.IMREAD_UNCHANGED)
            if mask.ndim != 2 or mask.shape != template.shape[:2] or not np.any(mask):
                raise ValueError(f"Invalid sparkling water mask: {name}")
    return layout


def _water_counts(player: Mapping[str, Any], *, allow_in_progress: bool = False) -> tuple[int, int]:
    water = (player.get("recovery") or {}).get("sparkling_water")
    if not isinstance(water, Mapping):
        raise ValueError("Refresh sparkling water information before drinking")
    if water.get("requires_refresh") is True and not allow_in_progress:
        raise ValueError("Sparkling water consumption was interrupted; refresh player data before drinking again")
    remaining, limit = water.get("remaining_free_uses"), water.get("daily_free_limit")
    if type(remaining) is not int or type(limit) is not int or not 0 <= remaining <= limit <= 6 or limit <= 0:
        raise ValueError("Invalid sparkling water free-use count")
    return remaining, limit


def mark_cup_in_progress(persistent_data: PersistentDataService) -> None:
    def update(document: dict) -> dict:
        _water_counts(document)
        document["recovery"]["sparkling_water"]["requires_refresh"] = True
        return document
    persistent_data.update(USER_INFO_FILE, update)


def record_completed_cup(persistent_data: PersistentDataService, *, exhausted: bool, city_name: str) -> dict:
    timestamp = datetime.now(timezone.utc).isoformat()

    def update(document: dict) -> dict:
        remaining, _ = _water_counts(document, allow_in_progress=True)
        water = document["recovery"]["sparkling_water"]
        water["remaining_free_uses"] = 0 if exhausted else max(0, remaining - 1)
        water.pop("requires_refresh", None)
        metadata = document.setdefault("metadata", {})
        metadata.setdefault("profile_section_updated_at", {})["sparkling_water"] = timestamp
        metadata["sparkling_water_last_use"] = {
            "city_name": city_name, "cid": current_cid(), "confirmed_at": timestamp,
            "confirmation": "option_disappeared_then_menu_returned",
        }
        return document

    result = persistent_data.update(USER_INFO_FILE, update)
    return copy.deepcopy(result.new_value["recovery"]["sparkling_water"])


class SparklingWaterSession:
    def __init__(self, *, app: Any, vision: Any, layout: dict):
        self.app, self.vision, self.layout = app, vision, layout
        self.stage = "city_panel"
        self.page = "city_panel"
        self.last_matches: dict[str, dict] = {}
        self.completed = 0
        self.uncertain_frames = 0
        self.first_uncertain_frame: dict | None = None

    def fail(self, code: str, message: str) -> None:
        raise SparklingWaterError(code, message, {
            "stage": self.stage, "page_state": self.page,
            "completed_count": self.completed, "matches": copy.deepcopy(self.last_matches),
            "uncertain_frames": self.uncertain_frames,
            "first_uncertain_frame": copy.deepcopy(self.first_uncertain_frame),
        })

    def record_uncertain_frame(self, key: str, image: np.ndarray, score: float) -> None:
        self.uncertain_frames += 1
        if self.first_uncertain_frame is not None:
            return
        evidence = {
            "cid": current_cid(), "stage": self.stage, "page_state": self.page,
            "target": key, "raw_score": str(score), "completed_count": self.completed,
            "roi": list(self.layout["templates"][key]["roi"]),
            "captured_at": datetime.now(timezone.utc).isoformat(),
        }
        self.first_uncertain_frame = evidence
        try:
            DIAGNOSTIC_ROOT.mkdir(parents=True, exist_ok=True)
            path = DIAGNOSTIC_ROOT / f"uncertain-{uuid4().hex}.png"
            ok, encoded = cv2.imencode(".png", image)
            if not ok:
                raise ValueError("Could not encode uncertain-frame ROI")
            path.write_bytes(encoded.tobytes())
            evidence["image_path"] = str(path)
            path.with_suffix(".json").write_text(
                json.dumps(evidence, ensure_ascii=False, indent=2), encoding="utf-8",
            )
        except (OSError, ValueError, cv2.error) as exc:
            evidence["diagnostic_error"] = str(exc)
        logger.warning("Sparkling water uncertain frame; waiting without input evidence=%s", evidence)

    def capture(self, roi: list[int]) -> np.ndarray:
        _check_cancelled()
        result = self.app.capture(rect=tuple(roi))
        image = getattr(result, "image", None)
        if not getattr(result, "success", False) or not isinstance(image, np.ndarray):
            self.fail("sparkling_water_capture_failed", "Sparkling water capture failed")
        if image.shape[:2] != (roi[3], roi[2]):
            self.fail("sparkling_water_capture_size_invalid", "Sparkling water ROI capture has unexpected dimensions")
        return image

    def match(self, key: str) -> dict:
        spec = self.layout["templates"][key]
        roi = spec["roi"]
        image = self.capture(roi)
        hit = self.vision.find_template(
            source_image=image, template_image=spec["resolved_path"],
            mask_image=spec.get("resolved_mask"), threshold=spec["threshold"],
            use_grayscale=False, match_method=cv2.TM_SQDIFF_NORMED, preprocess="none",
        )
        _check_cancelled()
        error = (getattr(hit, "debug_info", None) or {}).get("error")
        if error:
            self.last_matches[key] = {"valid": False, "error": str(error)}
            self.fail("sparkling_water_match_failed", f"Template matching failed: {key}")
        score = float(hit.confidence)
        if not math.isfinite(score):
            self.last_matches[key] = {"valid": False, "raw_score": str(score)}
            self.record_uncertain_frame(key, image, score)
            raise _UncertainFrame(key)
        center = getattr(hit, "center_point", None)
        result = {"found": bool(hit.found), "confidence": float(hit.confidence)}
        if center is not None:
            result["center"] = [roi[0] + int(center[0]), roi[1] + int(center[1])]
        self.last_matches[key] = result
        logger.debug("Sparkling water template stage=%s key=%s result=%s", self.stage, key, result)
        return result

    async def wait_for(self, probe: Callable, predicate: Callable, *, label: str, timeout: float | None = None):
        _check_cancelled()

        def observe():
            _check_cancelled()
            try:
                return probe()
            except _UncertainFrame as exc:
                return exc

        found, value = await poll_until(
            self.layout["timeout_sec"] if timeout is None else timeout,
            self.layout["poll_interval_sec"], observe,
            lambda value: not isinstance(value, _UncertainFrame) and predicate(value),
        )
        if not found:
            self.fail("sparkling_water_transition_timeout", f"Timed out waiting for {label}")
        return value

    async def observe_clicked_source(self, source: Callable, *, label: str) -> dict:
        uncertain_seen = False

        def observe():
            nonlocal uncertain_seen
            try:
                return source()
            except _UncertainFrame:
                uncertain_seen = True
                raise

        # After an unobserved transition, a returned button could be a new menu.
        # Never authorize another consumption click from that ambiguous presence.
        return await self.wait_for(
            observe, lambda hit: not uncertain_seen or not hit["found"], label=label,
        )

    def click(self, point: list[int]) -> None:
        _check_cancelled()
        logger.debug("Sparkling water click stage=%s point=%s", self.stage, point)
        aura_click(app=self.app, x=point[0], y=point[1])

    async def click_until_gone(self, source: Callable, click: Callable, *, label: str, before_click: Callable | None = None) -> None:
        """Retry only while the old target is positively observed; never replay after it disappears."""
        self.stage = label
        original = await self.wait_for(source, lambda hit: hit["found"], label=label)
        if before_click is not None:
            await asyncio.to_thread(before_click)
        for attempt in range(self.layout["max_clicks"]):
            if attempt:
                original = await self.observe_clicked_source(source, label=label)
                if not original["found"]:
                    return
            _check_cancelled()
            await asyncio.to_thread(click, original)
            self.page = "transition"
            await asyncio.sleep(self.layout["after_click_sec"])
            recheck = await self.observe_clicked_source(source, label=label)
            if not recheck["found"]:
                return
        self.fail("sparkling_water_click_not_effective", f"Original target still present after clicking: {label}")

    async def click_icon_until_gone(self, key: str, *, before_click: Callable | None = None) -> None:
        await self.click_until_gone(
            lambda: self.match(key), lambda hit: self.click(hit["center"]), label=key,
            before_click=before_click,
        )

    def menu_page(self) -> str | None:
        if self.match("rest_menu")["found"]:
            return "rest_menu"
        if self.match("sparkling_water")["found"]:
            return "drink_menu"
        return None

    async def enter_rest(self, point: dict) -> None:
        roi = self.layout["city_marker_roi"]
        city_marker = await asyncio.to_thread(self.capture, roi)

        def city_still_visible() -> dict:
            hit = self.vision.find_template(
                source_image=self.capture(roi), template_image=city_marker,
                use_grayscale=True, threshold=0.90,
            )
            if (getattr(hit, "debug_info", None) or {}).get("error"):
                self.fail("sparkling_water_city_probe_failed", "City marker matching failed")
            return {"found": bool(hit.found), "confidence": float(hit.confidence)}

        await self.click_until_gone(
            city_still_visible, lambda _: self.click([point["x"], point["y"]]), label="enter_rest",
        )
        await self.wait_for(lambda: self.match("rest_menu"), lambda hit: hit["found"], label="rest menu")
        self.page = "rest_menu"
        await self.click_icon_until_gone("drink_entry")
        await self.wait_for(lambda: self.match("sparkling_water"), lambda hit: hit["found"], label="drink menu")
        self.page = "drink_menu"

    async def finish_cup(self) -> tuple[str, bool]:
        """No SKIP template: click the fixed point until an actual menu returns."""
        self.stage = "drink_animation"
        deadline = time.monotonic() + self.layout["animation_timeout_sec"]
        confirmation_seen = False
        # The underlying menu can linger briefly before the drink animation starts.
        await asyncio.sleep(1.0)
        while time.monotonic() < deadline:
            _check_cancelled()
            try:
                confirmation = await asyncio.to_thread(self.match, "drink_again")
                page = None if confirmation["found"] else await asyncio.to_thread(self.menu_page)
            except _UncertainFrame:
                await asyncio.sleep(self.layout["poll_interval_sec"])
                continue
            if confirmation["found"]:
                confirmation_seen = True
                await self.click_icon_until_gone("drink_again")
                self.stage = "drink_animation"
                await asyncio.sleep(1.0)
                continue
            if page:
                self.page = page
                return page, confirmation_seen
            await asyncio.to_thread(self.click, self.layout["skip_point"])
            await asyncio.sleep(self.layout["poll_interval_sec"])
        self.fail("sparkling_water_animation_timeout", "Drink animation did not return to a menu")

    async def return_to_city(self, read_city: Callable, city_key: str) -> None:
        from .city_trade_flow_pc_actions import (
            _BACK_BUTTON_REGION, _BACK_BUTTON_TEMPLATE, _NAV_BUTTON_THRESHOLD, _wait_template,
        )

        def back(_hit: dict) -> None:
            _check_cancelled()
            hit = _wait_template(
                self.app, self.vision, _BACK_BUTTON_TEMPLATE, _BACK_BUTTON_REGION,
                threshold=_NAV_BUTTON_THRESHOLD, timeout_sec=self.layout["timeout_sec"],
                interval_sec=self.layout["poll_interval_sec"],
            )
            if not hit.get("found"):
                self.fail("sparkling_water_back_not_found", "Return arrow was not found")
            self.click(hit["center"])

        if self.page == "drink_menu":
            await self.click_until_gone(lambda: self.match("sparkling_water"), back, label="back_to_rest_menu")
            await self.wait_for(lambda: self.match("rest_menu"), lambda hit: hit["found"], label="rest menu after Back")
            self.page = "rest_menu"
        await self.click_until_gone(lambda: self.match("rest_menu"), back, label="back_to_city")

        def probe_city() -> dict:
            _check_cancelled()
            try:
                return read_city()
            except Exception as exc:
                from ..services.city_shop_data_pc_service import CityShopDataError
                if isinstance(exc, CityShopDataError):
                    return {"city_key": None, "error": str(exc)}
                raise

        await self.wait_for(probe_city, lambda city: city.get("city_key") == city_key, label="original city panel")
        self.page = "city_panel"


@action_info(
    name="resonance_pc.drink_sparkling_water_from_city_panel", public=True,
    read_only=False, timeout=240,
    description="Drink the requested free sparkling-water cups at a rest area and return to the city panel.",
)
@requires_services(
    app="plans/aura_base/app", vision="plans/aura_base/vision", ocr="plans/aura_base/ocr",
    resonance_pc_city_shop_data="resonance_pc_city_shop_data", persistent_data="core/persistent_data",
)
async def resonance_pc_drink_sparkling_water_from_city_panel(
    city_name: str, drink_count: int, app: Any = None, vision: Any = None, ocr: Any = None,
    resonance_pc_city_shop_data: Any = None, persistent_data: PersistentDataService | None = None,
) -> dict:
    from .city_trade_flow_pc_actions import resonance_pc_read_city_name_on_city_panel

    if type(drink_count) is not int or not 1 <= drink_count <= 6:
        raise ValueError("drink_count must be an integer between 1 and 6")
    if any(value is None for value in (app, vision, ocr, resonance_pc_city_shop_data, persistent_data)):
        raise RuntimeError("Sparkling water requires app/vision/ocr/city-shop/persistent-data services")
    _check_cancelled()
    started = time.monotonic()
    layout = await asyncio.to_thread(load_sparkling_water_layout, vision)
    player = await asyncio.to_thread(load_pc_user_info, persistent_data)
    remaining, limit = _water_counts(player)
    if drink_count > remaining:
        raise ValueError("Requested sparkling-water cups exceed recorded free uses; refresh player data")
    point = resonance_pc_city_shop_data.resolve_shop_point(city_name=city_name, shop_name="rest")
    session = SparklingWaterSession(app=app, vision=vision, layout=layout)
    cups = []
    reason = None
    logger.info("Sparkling water started city=%s requested=%s remaining=%s", city_name, drink_count, remaining)
    try:
        await session.enter_rest(point)
        for cup_index in range(drink_count):
            await session.click_icon_until_gone(
                "sparkling_water", before_click=lambda: mark_cup_in_progress(persistent_data),
            )
            page, confirmation_seen = await session.finish_cup()
            session.completed += 1
            water = await asyncio.to_thread(
                record_completed_cup, persistent_data,
                exhausted=page == "rest_menu", city_name=point["city_name"],
            )
            remaining = water["remaining_free_uses"]
            cups.append({"number": cup_index + 1, "confirmation_seen": confirmation_seen, "returned_page": page})
            logger.info("Sparkling water cup completed city=%s completed=%s/%s remaining=%s page=%s confirmation=%s",
                        city_name, session.completed, drink_count, remaining, page, confirmation_seen)
            if page == "rest_menu":
                if session.completed < drink_count:
                    reason = "free_uses_exhausted"
                    logger.warning("Sparkling water ended early city=%s requested=%s completed=%s",
                                   city_name, drink_count, session.completed)
                break
            await asyncio.sleep(layout["after_click_sec"])
        await session.return_to_city(
            lambda: resonance_pc_read_city_name_on_city_panel(
                app=app, ocr=ocr, resonance_pc_city_shop_data=resonance_pc_city_shop_data,
            ), point["city_key"],
        )
    except asyncio.CancelledError:
        logger.info("Sparkling water cancelled city=%s completed=%s stage=%s page=%s",
                    city_name, session.completed, session.stage, session.page)
        raise
    except Exception as exc:
        logger.error("Sparkling water failed city=%s code=%s stage=%s page=%s completed=%s matches=%s error=%s",
                     city_name, getattr(exc, "code", type(exc).__name__), session.stage, session.page,
                     session.completed, session.last_matches, exc)
        raise
    result = {
        "success": True, "status": "completed", "reason": reason,
        "city_name": point["city_name"], "city_key": point["city_key"],
        "requested_count": drink_count, "completed_count": session.completed,
        "remaining_free_uses": remaining, "daily_free_limit": limit,
        "recovered_fatigue": session.completed * 50, "cups": cups,
        "page_state": session.page, "elapsed_ms": int((time.monotonic() - started) * 1000),
        "uncertain_frames": session.uncertain_frames,
        "first_uncertain_frame": session.first_uncertain_frame,
    }
    logger.info("Sparkling water completed result=%s", result)
    return result
