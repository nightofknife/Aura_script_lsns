from __future__ import annotations

from types import SimpleNamespace

import numpy as np

from plans.aura_base.src.services.ocr_service import MultiOcrResult, OcrResult
from plans.aura_base.src.services.vision_service import MatchResult
from plans.resonance.src.actions.city_travel_actions import (
    _find_allowed_fatigue_medicine,
    _wait_and_click_fatigue_medicine_confirm,
    IntercityDestinationError,
    resonance_intercity_depart_and_wait,
    resonance_select_intercity_destination,
    resonance_wait_intercity_arrival,
)


class _FakeApp:
    def __init__(self):
        self.clicks = []
        self.moves = []

    def capture(self, rect=None):
        return SimpleNamespace(success=True, image=np.zeros((720, 1280, 3), dtype=np.uint8), rect=rect)

    def click(self, x=None, y=None, **_kwargs):
        self.clicks.append((x, y))

    def move_to(self, x=None, y=None, duration=0, **_kwargs):
        self.moves.append((x, y, duration))

    def get_window_size(self):
        return (1280, 720)


class _FakeController:
    def mouse_down(self, _button):
        pass

    def mouse_up(self, _button):
        pass


class _FakeOcr:
    def __init__(self, text_batches):
        self.text_batches = list(text_batches)

    def recognize_all(self, source_image):
        texts = self.text_batches.pop(0) if self.text_batches else []
        return MultiOcrResult(
            count=len(texts),
            results=[
                OcrResult(
                    found=True,
                    text=text,
                    center_point=(100 + index, 200 + index),
                    confidence=0.99 - index * 0.01,
                )
                for index, text in enumerate(texts)
            ],
        )


class _FakeVision:
    def __init__(self, found_templates):
        self.found_templates = list(found_templates)
        self.calls = []

    def find_template(self, source_image, template_image, threshold=0.8, use_grayscale=True, **_kwargs):
        name = str(template_image).replace("\\", "/").split("/")[-1]
        self.calls.append(name)
        if name in self.found_templates:
            return MatchResult(
                found=True,
                center_point=(30, 20),
                rect=(10, 5, 40, 30),
                confidence=1.0,
            )
        return MatchResult(found=False, confidence=0.0)


class _CountedVision:
    def __init__(self, template_counts):
        self.template_counts = dict(template_counts)
        self.calls = []

    def find_template(self, source_image, template_image, threshold=0.8, use_grayscale=True, **_kwargs):
        name = str(template_image).replace("\\", "/").split("/")[-1]
        self.calls.append(name)
        remaining = int(self.template_counts.get(name, 0) or 0)
        if remaining > 0:
            self.template_counts[name] = remaining - 1
            return MatchResult(
                found=True,
                center_point=(30, 20),
                rect=(10, 5, 40, 30),
                confidence=1.0,
            )
        return MatchResult(found=False, confidence=0.0)


def test_wait_intercity_arrival_clicks_enter_station():
    app = _FakeApp()
    ocr = _FakeOcr([["进入站点"]])

    result = resonance_wait_intercity_arrival(
        app=app,
        ocr=ocr,
        timeout_sec=1,
        interval_sec=0.1,
        post_action_sec=0,
    )

    assert result["status"] == "arrived"
    assert result["encounter_actions"] == 0
    assert app.clicks == [(100, 200)]


def test_wait_intercity_arrival_zero_timeout_waits_until_arrival():
    app = _FakeApp()
    ocr = _FakeOcr([[], ["进入站点"]])

    result = resonance_wait_intercity_arrival(
        app=app,
        ocr=ocr,
        timeout_sec=0,
        interval_sec=0.15,
        post_action_sec=0,
    )

    assert result["status"] == "arrived"
    assert result["poll_count"] == 2
    assert app.clicks == [(100, 200)]


def test_wait_intercity_arrival_clicks_fight_even_when_bait_balloon_is_present():
    app = _FakeApp()
    ocr = _FakeOcr(
        [
            ["立即返航", "护卫队迎击", "敌方等级：10", "应对方式", "诱饵气球"],
            ["进入站点"],
        ]
    )

    result = resonance_wait_intercity_arrival(
        app=app,
        ocr=ocr,
        timeout_sec=1,
        interval_sec=0.1,
        post_action_sec=0,
    )

    assert result["status"] == "arrived"
    assert result["encounter_actions"] == 1
    assert result["trace"][1]["action"] == "click_fight"
    assert app.clicks == [(101, 201), (100, 200)]


def test_wait_intercity_arrival_can_be_configured_to_fail_without_clicking_encounter():
    app = _FakeApp()
    ocr = _FakeOcr(
        [
            ["立即返航", "护卫队迎击", "敌方等级：10", "应对方式", "暂不可用", "诱饵气球"],
        ]
    )

    try:
        resonance_wait_intercity_arrival(
            app=app,
            ocr=ocr,
            encounter_policy="fail",
            timeout_sec=1,
            interval_sec=0.1,
            post_action_sec=0,
        )
    except IntercityDestinationError as exc:
        assert exc.code == "travel_encounter_requires_manual_resolution"
        assert exc.detail["bait_balloon_found"] is True
        assert exc.detail["unavailable_found"] is True
    else:
        raise AssertionError("expected IntercityDestinationError")

    assert app.clicks == []


def test_intercity_depart_and_wait_blocks_without_fatigue_medicine():
    app = _FakeApp()
    ocr = _FakeOcr([["启程"], ["7号自由港"]])
    vision = _FakeVision(
        [
            "go_destination_button.png",
            "fatigue_recovery_panel_title.png",
            "fatigue_recovery_back_button.png",
        ]
    )

    result = resonance_intercity_depart_and_wait(
        to_city_name="7号自由港",
        location_file_path="data/meta/location_mumu.json",
        use_fatigue_medicine=False,
        app=app,
        ocr=ocr,
        vision=vision,
        controller=_FakeController(),
    )

    assert result["status"] == "blocked"
    assert result["reason"] == "fatigue_recovery_required"
    assert result["blocked_at"] == "departure"
    assert result["fatigue_medicine_used"] == []


def test_select_intercity_destination_does_not_fallback_drag_without_mappable_city():
    app = _FakeApp()
    ocr = _FakeOcr([["未知文本"]])

    try:
        resonance_select_intercity_destination(
            to_city_name="7号自由港",
            location_file_path="data/meta/location_mumu.json",
            max_search_steps=3,
            fallback_enabled=True,
            app=app,
            ocr=ocr,
            controller=_FakeController(),
        )
    except IntercityDestinationError as exc:
        assert exc.code == "destination_not_found_after_drag"
        assert exc.detail["selected_mode"] == "no_mappable"
        assert exc.detail["attempt_trace"][0]["mode"] == "no_mappable"
        assert exc.detail["attempt_trace"][0]["plan"]["fallback_drag_disabled"] is True
    else:
        raise AssertionError("expected IntercityDestinationError")

    assert app.moves == []
    assert app.clicks == []


def test_intercity_depart_and_wait_can_confirm_and_arrive_without_fatigue_panel():
    app = _FakeApp()
    ocr = _FakeOcr([["启程"], ["7号自由港"], ["立即出发"], ["进入站点"]])
    vision = _FakeVision(["go_destination_button.png"])

    result = resonance_intercity_depart_and_wait(
        to_city_name="7号自由港",
        location_file_path="data/meta/location_mumu.json",
        app=app,
        ocr=ocr,
        vision=vision,
        controller=_FakeController(),
        enter_station_timeout_seconds=1,
    )

    assert result["status"] == "ok"
    assert result["arrival_status"] == "arrived"
    assert result["fatigue_medicine_use_count"] == 0


def test_fatigue_medicine_selection_uses_fixed_chinese_order():
    app = _FakeApp()
    vision = _FakeVision(
        [
            "fatigue_medicine_stimulant_gum_button.png",
            "fatigue_medicine_cactus_jump_candy_button.png",
            "fatigue_medicine_birch_stone_button.png",
        ]
    )

    selected = _find_allowed_fatigue_medicine(
        app=app,
        vision=vision,
        allowed_names={"桦石", "仙人掌提神跳糖", "提神口香糖"},
        threshold=0.95,
    )

    assert selected is not None
    assert selected["name"] == "提神口香糖"
    assert "huashi" not in str(selected)
    assert "shard" not in str(selected)


def test_fatigue_medicine_confirm_button_uses_template():
    app = _FakeApp()
    ocr = _FakeOcr([])
    vision = _FakeVision(["fatigue_medicine_confirm_button.png"])

    result = _wait_and_click_fatigue_medicine_confirm(
        app=app,
        vision=vision,
        ocr=ocr,
        threshold=0.95,
        timeout_sec=0.1,
        interval_sec=0.1,
    )

    assert result["clicked"] is True
    assert result["method"] == "template"
    assert "fatigue_medicine_confirm_button.png" in vision.calls
    assert app.clicks == [(650, 540)]


def test_intercity_depart_and_wait_confirms_lollipop_and_retries_departure():
    app = _FakeApp()
    ocr = _FakeOcr(
        [
            ["启程"],
            ["7号自由港"],
            ["启程"],
            ["启程"],
            ["7号自由港"],
            ["立即出发"],
            ["进入站点"],
        ]
    )
    vision = _CountedVision(
        {
            "go_destination_button.png": 2,
            "fatigue_recovery_panel_title.png": 1,
            "fatigue_medicine_stimulant_lollipop_button.png": 1,
            "fatigue_medicine_confirm_button.png": 1,
        }
    )

    result = resonance_intercity_depart_and_wait(
        to_city_name="7号自由港",
        location_file_path="data/meta/location_mumu.json",
        use_fatigue_medicine=True,
        allowed_fatigue_medicines=["提神棒棒糖"],
        app=app,
        ocr=ocr,
        vision=vision,
        controller=_FakeController(),
        enter_station_timeout_seconds=1,
        medicine_button_threshold=0.95,
    )

    assert result["status"] == "ok"
    assert result["arrival_status"] == "arrived"
    assert result["fatigue_medicine_used"] == [{"name": "提神棒棒糖", "count": 1}]
    assert result["fatigue_medicine_use_count"] == 1
    assert "fatigue_medicine_confirm_button.png" in vision.calls
