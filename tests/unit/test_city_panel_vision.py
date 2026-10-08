"""Offline masked city identification and guarded city-entry navigation."""
from __future__ import annotations

import asyncio
import inspect
from pathlib import Path
from types import SimpleNamespace

import cv2
import numpy as np
from PIL import Image
import pytest

from plans.aura_base.src.services.vision_service import VisionService
from plans.resonance_pc.src.actions import _city_panel_vision as city
from plans.resonance_pc.src.actions import city_trade_flow_pc_actions as trade
from plans.resonance_pc.src.actions import sparkling_water_pc_actions as water


FIXTURES = Path(__file__).resolve().parents[1] / "fixtures/city_identity"


def hit(score=.1, found=None, center=(50, 30), **changes):
    values = dict(confidence=score, found=score >= city.CITY_THRESHOLD if found is None else found,
                  center_point=center, debug_info={})
    values.update(changes)
    return SimpleNamespace(**values)


def batch(**scores):
    result = [hit() for _ in range(21)]
    for index, value in scores.items():
        result[int(index)] = value if not isinstance(value, (int, float)) else hit(value)
    return result


class App:
    def __init__(self, fixture=None):
        self.fixture = fixture
        self.captures, self.clicks = [], []
        self.on_capture = self.on_click = None

    def capture(self, *, rect):
        self.captures.append(rect)
        if self.on_capture:
            self.on_capture()
        if self.fixture:
            kind = "city" if rect == city.CITY_REGION else "visit"
            image = np.array(Image.open(FIXTURES / f"{self.fixture}_{kind}_roi.png").convert("RGB"))
        else:
            image = np.zeros((rect[3], rect[2], 3), np.uint8)
        return SimpleNamespace(success=True, image=image)

    def click(self, **point):
        self.clicks.append(point)
        if self.on_click:
            self.on_click()


class Vision:
    def __init__(self, batches=None, entries=None):
        self.batches = list(batches or [batch()])
        self.entries = list(entries or [hit(.99)])
        self.batch_calls, self.single_calls = [], []
        self.on_batch = self.on_single = None

    def find_templates_batch(self, **options):
        self.batch_calls.append(options)
        if self.on_batch:
            self.on_batch()
        return self.batches.pop(0) if len(self.batches) > 1 else self.batches[0]

    def find_template(self, **options):
        self.single_calls.append(options)
        if self.on_single:
            self.on_single()
        return self.entries.pop(0) if len(self.entries) > 1 else self.entries[0]


class CpuVision:
    def __init__(self):
        self.core = VisionService()

    def find_templates_batch(self, **options):
        return self.core._find_templates_batch_sync(**options)

    def find_template(self, *, source_image, template_image, **options):
        source = self.core._prepare_image(source_image, use_grayscale=options["use_grayscale"],
                                          preprocess=options["preprocess"])
        template = self.core._prepare_image(template_image, use_grayscale=options["use_grayscale"],
                                            preprocess=options["preprocess"])
        return self.core._match_template_prepared(source, template, None, **options)


@pytest.fixture(autouse=True)
def clock(monkeypatch):
    value = SimpleNamespace(now=0., sleeps=[], on_sleep=None)
    def sleep(seconds):
        value.sleeps.append(seconds)
        value.now += seconds
        if value.on_sleep:
            value.on_sleep()
    monkeypatch.setattr(city.time, "monotonic", lambda: value.now)
    monkeypatch.setattr(city.time, "sleep", sleep)
    city.load_templates.cache_clear()
    yield value
    city.load_templates.cache_clear()


def probe(vision=None, app=None, check_cancelled=lambda: None):
    return city.probe_city(app=app or App(), vision=vision or Vision(), check_cancelled=check_cancelled)


def wait(vision=None, app=None, **options):
    return city.wait_city(app=app or App(), vision=vision or Vision(), check_cancelled=lambda: None, **options)


def open_panel(vision=None, app=None, **options):
    return city.open_city_panel(app=app or App(), vision=vision or Vision(),
                                check_cancelled=lambda: None, **options)


def test_highest_only_wins_even_when_runner_up_is_almost_equal():
    rows = city.load_templates()[0]
    result = probe(Vision([batch(**{"0": .600, "1": .599})]))
    assert result["found"] and result["city_key"] == rows[0]["city_key"]
    assert result["confidence"] == .6 and "margin" not in result


@pytest.mark.parametrize("score, expected", [(.549, False), (.55, True), (.551, True)])
def test_city_threshold_is_inclusive(score, expected):
    result = probe(Vision([batch(**{"2": hit(score, found=True)})]))
    assert result["found"] is expected
    assert bool(result["city_key"]) is expected and bool(result["city_name"]) is expected


def test_highest_candidate_must_itself_be_found_not_lower_fallback():
    result = probe(Vision([batch(**{"0": hit(.8, found=False), "1": .7})]))
    assert not result["found"] and result["candidate_city_key"] == city.load_templates()[0][0]["city_key"]


@pytest.mark.parametrize("invalid", [float("nan"), float("inf"), float("-inf"), None, "bad"])
def test_invalid_city_score_is_not_ranked_as_success(invalid):
    result = probe(Vision([batch(**{"0": hit(invalid, found=True), "1": .6})]))
    assert result["found"] and result["city_key"] == city.load_templates()[0][1]["city_key"]
    assert result["invalid_candidates"] == 1


def test_all_invalid_city_scores_are_unknown():
    result = probe(Vision([[hit(float("nan"), found=True) for _ in range(21)]]))
    assert not result["found"] and result["confidence"] is None
    assert result["invalid_candidates"] == 21 and result["city_key"] is None


def test_framework_error_score_is_excluded():
    result = probe(Vision([batch(**{"0": hit(.99, debug_info={"error": "failed"}), "1": .6})]))
    assert result["city_key"] == city.load_templates()[0][1]["city_key"]
    assert result["invalid_candidates"] == 1


@pytest.mark.parametrize("results", [[], [hit()], None, "invalid"])
def test_incomplete_or_invalid_city_batch_is_structured_failure(results):
    vision = Vision()
    vision.batches = [results]
    with pytest.raises(city.CityPanelVisionError) as error:
        probe(vision)
    assert error.value.code == "city_identity_match_failed"


def test_city_probe_region_batch_mask_edge_correlation_contract():
    app, vision = App(), Vision()
    probe(vision, app)
    assert app.captures == [(25, 490, 175, 170)] and not app.clicks
    options = vision.batch_calls[0]
    assert options["source_image"].shape == (170, 175, 3)
    assert len(options["template_images"]) == len(options["mask_images"]) == 21
    assert all(template.shape == (133, 133, 3) for template in options["template_images"])
    assert all(mask.shape == (133, 133) and np.any(mask) for mask in options["mask_images"])
    assert options["threshold"] == .55 and options["use_grayscale"] is True
    assert options["match_method"] == cv2.TM_CCORR_NORMED and options["preprocess"] == "edge"
    assert not vision.single_calls


@pytest.mark.parametrize("target", ["city", "visit"])
@pytest.mark.parametrize("image", [None, np.zeros((720, 1280, 3), np.uint8),
                                   np.zeros((170, 175, 4), np.uint8),
                                   np.zeros((170, 175, 3), np.float32)])
def test_invalid_capture_fails_before_matching(target, image):
    app, vision = App(), Vision()
    app.capture = lambda **kwargs: SimpleNamespace(success=True, image=image)
    function = city.probe_city if target == "city" else city.probe_visit_entry
    with pytest.raises(city.CityPanelVisionError) as error:
        function(app=app, vision=vision, check_cancelled=lambda: None)
    assert error.value.code == "city_identity_capture_failed"
    assert not vision.batch_calls and not vision.single_calls


def test_unsuccessful_capture_cannot_succeed_even_with_pixels():
    app = App()
    app.capture = lambda **kwargs: SimpleNamespace(success=False, image=np.zeros((170, 175, 3), np.uint8))
    with pytest.raises(city.CityPanelVisionError) as error:
        probe(app=app)
    assert error.value.code == "city_identity_capture_failed"


@pytest.mark.parametrize("score, expected", [(.899, False), (.9, True), (.901, True)])
def test_visit_entry_uses_rgb_sqdiff_without_mask(score, expected):
    app, vision = App(), Vision(entries=[hit(score, found=True)])
    result = city.probe_visit_entry(app=app, vision=vision, check_cancelled=lambda: None)
    assert result["found"] is expected
    assert result["center"] == ([1050, 480] if expected else None)
    assert app.captures == [(1000, 450, 250, 70)] and not vision.batch_calls
    options = vision.single_calls[0]
    assert options["threshold"] == .9 and options["use_grayscale"] is False
    assert options["match_method"] == cv2.TM_SQDIFF_NORMED and options["preprocess"] == "none"
    assert options["template_image"].shape == (29, 35, 3)
    assert "mask_images" not in options and "mask_image" not in options


@pytest.mark.parametrize("score", [float("nan"), float("inf"), None, "invalid"])
def test_visit_entry_invalid_score_is_not_clickable(score):
    result = city.probe_visit_entry(app=App(), vision=Vision(entries=[hit(score, found=True)]),
                                    check_cancelled=lambda: None)
    assert not result["found"] and result["center"] is None


@pytest.mark.parametrize("point", [(-1, 30), (250, 30), (40, 70), (40,), (float("nan"), 0)])
def test_visit_entry_invalid_coordinates_block(point):
    with pytest.raises(city.CityPanelVisionError) as error:
        city.probe_visit_entry(app=App(), vision=Vision(entries=[hit(.99, center=point)]),
                               check_cancelled=lambda: None)
    assert error.value.code == "city_entry_match_failed"


def test_wait_two_consecutive_same_city_finishes_without_three_second_sleep(clock):
    vision = Vision([batch(**{"3": .7})])
    result = wait(vision)
    assert result["success"] and result["confirmations"] == 2
    assert result["page_state"] == "city_panel" and len(vision.batch_calls) == 2
    assert result["elapsed_ms"] == 150 and clock.sleeps == [.15]


@pytest.mark.parametrize("middle", [batch(), batch(**{"1": .7})])
def test_wait_missing_or_changing_city_resets_confirmation(middle):
    a = batch(**{"0": .7})
    vision = Vision([a, middle, a, a])
    result = wait(vision)
    assert result["success"] and len(vision.batch_calls) == 4 and result["elapsed_ms"] == 450


def test_wait_zero_timeout_cannot_accept_single_hit():
    with pytest.raises(city.CityPanelVisionError) as error:
        wait(Vision([batch(**{"0": .7})]), timeout_sec=0)
    assert error.value.code == "city_panel_not_confirmed"


def test_unknown_wait_does_not_click_and_honors_timeout(clock):
    app = App()
    with pytest.raises(city.CityPanelVisionError) as error:
        wait(app=app, timeout_sec=.3)
    assert error.value.code == "city_panel_not_confirmed" and not app.clicks
    assert clock.now == .3


@pytest.mark.parametrize("function", [city.wait_city, city.open_city_panel])
@pytest.mark.parametrize("timeout", [-1, float("nan"), float("inf")])
def test_invalid_timeout_rejected_before_capture(function, timeout):
    app = App()
    with pytest.raises(ValueError):
        function(app=app, vision=Vision(), check_cancelled=lambda: None, timeout_sec=timeout)
    assert not app.captures and not app.clicks


def test_already_in_city_panel_never_probes_entry_or_clicks(clock):
    app, vision = App(), Vision([batch(**{"3": .7})])
    result = open_panel(vision, app)
    assert result["skipped"] and result["click_attempts"] == 0 and result["city"]["success"]
    assert not app.clicks and not vision.single_calls and clock.now == .15


def test_first_entry_click_then_two_city_matches_no_three_second_sleep(clock):
    app, vision = App(), Vision([batch(), batch(**{"3": .7}), batch(**{"3": .7})])
    result = open_panel(vision, app)
    assert result["success"] and not result["skipped"] and result["click_attempts"] == 1
    assert app.clicks == [{"x": 1050, "y": 480}]
    assert clock.sleeps == [.3, .15] and result["city"]["elapsed_ms"] == 450


def test_four_total_clicks_are_guarded_by_fresh_entry_evidence_and_two_second_spacing(clock):
    app, vision = App(), Vision()
    click_times = []
    app.on_click = lambda: click_times.append(clock.now)
    with pytest.raises(city.CityPanelVisionError) as error:
        open_panel(vision, app, timeout_sec=10)
    assert error.value.code == "open_city_panel_failed" and error.value.detail["click_attempts"] == 4
    assert len(app.clicks) == len(vision.single_calls) == 4
    assert click_times[0] == 0 and all(b-a >= 2. for a, b in zip(click_times, click_times[1:]))
    assert all(options["source_image"].shape == (70, 250, 3) for options in vision.single_calls)


def test_unknown_screen_never_blind_clicks():
    app, vision = App(), Vision(entries=[hit(.1, found=False)])
    with pytest.raises(city.CityPanelVisionError):
        open_panel(vision, app, timeout_sec=.6)
    assert not app.clicks and len(vision.single_calls) > 1


def test_old_entry_match_is_not_reused_after_it_disappears():
    app, vision = App(), Vision(entries=[hit(.99), hit(.1, found=False)])
    with pytest.raises(city.CityPanelVisionError) as error:
        open_panel(vision, app, timeout_sec=5)
    assert error.value.detail["click_attempts"] == 1 and len(app.clicks) == 1
    assert len(vision.single_calls) > 1


@pytest.mark.parametrize("target", ["city", "visit"])
@pytest.mark.parametrize("cancel_at", [1, 2, 3, 4])
def test_probe_cancellation_before_and_after_capture_and_match(target, cancel_at):
    checks, app, vision = [], App(), Vision()
    def check():
        checks.append(None)
        if len(checks) == cancel_at:
            raise asyncio.CancelledError()
    function = city.probe_city if target == "city" else city.probe_visit_entry
    with pytest.raises(asyncio.CancelledError):
        function(app=app, vision=vision, check_cancelled=check)
    assert not app.clicks
    if cancel_at <= 2:
        assert not app.captures
    if cancel_at <= 3:
        assert not vision.batch_calls and not vision.single_calls


@pytest.mark.parametrize("when, expected_clicks", [("entry_match", 0), ("click", 1), ("sleep", 1)])
def test_open_cancellation_at_match_click_or_sleep_never_retries(clock, when, expected_clicks):
    state, app, vision = {"cancelled": False}, App(), Vision()
    def cancel():
        state["cancelled"] = True
    def check():
        if state["cancelled"]:
            raise asyncio.CancelledError()
    if when == "entry_match":
        vision.on_single = cancel
    elif when == "click":
        app.on_click = cancel
    else:
        clock.on_sleep = cancel
    with pytest.raises(asyncio.CancelledError):
        city.open_city_panel(app=app, vision=vision, check_cancelled=check)
    assert len(app.clicks) == expected_clicks


@pytest.mark.parametrize("function", [trade.resonance_pc_open_city_panel_from_main,
                                      trade.resonance_pc_read_city_name_on_city_panel])
def test_public_wrappers_have_no_ocr_or_stale_settle_parameters(function):
    assert set(inspect.signature(function).parameters) == {"timeout_sec", "app", "vision"}
    assert "ocr" not in inspect.getsource(function).lower()


@pytest.mark.parametrize("wrapper, internal", [(trade.resonance_pc_open_city_panel_from_main, "open_city_panel"),
                                              (trade.resonance_pc_read_city_name_on_city_panel, "wait_city")])
def test_wrappers_preserve_structured_error_detail(monkeypatch, wrapper, internal):
    def failed(**options):
        raise city.CityPanelVisionError("city_identity_capture_failed", "Capture failed", {"fixture": True})
    monkeypatch.setattr(trade, internal, failed)
    with pytest.raises(trade.CityTradeFlowError) as error:
        wrapper(app=App(), vision=Vision())
    assert error.value.code == "city_identity_capture_failed" and error.value.detail == {"fixture": True}


@pytest.mark.parametrize("fixture, expected", [("city", "gronru_city"), ("bridge", "farstar_bridge")])
def test_actual_user_city_rois_with_real_framework_cpu(fixture, expected):
    app, vision = App(fixture), CpuVision()
    result = city.wait_city(app=app, vision=vision, check_cancelled=lambda: None)
    assert result["success"] and result["city_key"] == expected and result["confidence"] >= .55
    assert app.captures == [city.CITY_REGION, city.CITY_REGION] and not app.clicks


def test_actual_main_roi_is_unknown_city_but_valid_visit_entry():
    app, vision = App("main"), CpuVision()
    badge = city.probe_city(app=app, vision=vision, check_cancelled=lambda: None)
    entry = city.probe_visit_entry(app=app, vision=vision, check_cancelled=lambda: None)
    assert not badge["found"] and entry["found"] and entry["confidence"] >= .9
    assert not app.clicks


@pytest.mark.parametrize("code", ["city_panel_not_confirmed", "city_identity_capture_failed", "trade_cancelled"])
def test_water_return_retries_only_unconfirmed_city_not_capture_or_cancel_errors(monkeypatch, code):
    session = water.SparklingWaterSession(app=App(), vision=Vision(), layout={})
    session.page = "rest_menu"
    reads = []

    def read_city():
        reads.append(None)
        if len(reads) == 1:
            raise trade.CityTradeFlowError(code, "Not ready")
        return {"city_key": "gronru_city"}

    async def no_click(*args, **kwargs):
        pass

    async def wait_for(probe, predicate, **kwargs):
        first = probe()
        assert not predicate(first)
        assert predicate(probe())

    monkeypatch.setattr(session, "click_until_gone", no_click)
    monkeypatch.setattr(session, "wait_for", wait_for)
    if code == "city_panel_not_confirmed":
        asyncio.run(session.return_to_city(read_city, "gronru_city"))
        assert session.page == "city_panel" and len(reads) == 2
    else:
        with pytest.raises(trade.CityTradeFlowError) as error:
            asyncio.run(session.return_to_city(read_city, "gronru_city"))
        assert error.value.code == code and len(reads) == 1
