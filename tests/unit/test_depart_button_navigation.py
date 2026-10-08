"""Offline departure evidence and bounded navigation-input contracts."""
from __future__ import annotations

import asyncio
from pathlib import Path
from types import SimpleNamespace

import cv2
import numpy as np
from PIL import Image
import pytest

from plans.aura_base.src.services.vision_service import MatchResult, VisionService
from plans.resonance_pc.src.actions import _depart_button_vision as depart
from plans.resonance_pc.src.actions import city_trade_flow_pc_actions as trade
from plans.resonance_pc.src.actions import city_travel_pc_actions as travel


FIXTURES = Path(__file__).resolve().parents[1] / "fixtures/depart_button"


class App:
    def __init__(self, image=None):
        self.image = image if image is not None else np.zeros((100, 160, 3), np.uint8)
        self.captures = []
        self.clicks = []
        self.success = True

    def capture(self, *, rect):
        self.captures.append(rect)
        return SimpleNamespace(success=self.success, image=self.image)

    def click(self, **point):
        self.clicks.append(point)


def hit(found=True, confidence=.99, center=(70, 45), **changes):
    values = dict(found=found, confidence=confidence, center_point=center, debug_info={})
    values.update(changes)
    return SimpleNamespace(**values)


class SequenceVision:
    def __init__(self, *results):
        self.results = list(results) or [hit()]
        self.calls = []

    def find_template(self, **options):
        self.calls.append(options)
        return self.results.pop(0) if len(self.results) > 1 else self.results[0]


class CpuVision:
    def __init__(self):
        self.core = VisionService()
        self.calls = []

    def find_template(self, *, source_image, template_image, **options):
        self.calls.append(options)
        assert options == dict(threshold=.9, use_grayscale=False,
                               match_method=cv2.TM_SQDIFF_NORMED, preprocess="none")
        assert source_image.shape == (100, 160, 3)
        assert template_image.shape == (42, 106, 3)
        return self.core._match_template_prepared(source_image, template_image, None, **options)


@pytest.fixture(autouse=True)
def clock(monkeypatch):
    state = SimpleNamespace(now=0., sleeps=[])

    def sleep(seconds):
        state.sleeps.append(seconds)
        state.now += seconds

    monkeypatch.setattr(depart.time, "monotonic", lambda: state.now)
    monkeypatch.setattr(depart.time, "sleep", sleep)
    depart._load_template.cache_clear()
    yield state
    depart._load_template.cache_clear()


def probe(app=None, vision=None, check_cancelled=lambda: None):
    return depart.probe_depart_button(app=app or App(), vision=vision or SequenceVision(),
                                      check_cancelled=check_cancelled)


def wait(vision=None, app=None, **options):
    return depart.wait_depart_button(app=app or App(), vision=vision or SequenceVision(),
                                     check_cancelled=lambda: None, **options)


def test_retained_real_crop_matches_generated_template_with_framework_cpu():
    actual = np.array(Image.open(FIXTURES / "actual-matched-crop.png").convert("RGB"))
    assert actual.shape == (42, 106, 3)
    image = np.full((100, 160, 3), 17, np.uint8)
    image[30:72, 20:126] = actual
    app, vision = App(image), CpuVision()
    result = probe(app, vision)
    assert result["found"] and result["confidence"] >= .9
    assert result["center"] == [1193, 671]
    assert app.captures == [depart.REGION] and not app.clicks
    assert len(vision.calls) == 1


def test_return_main_then_open_map_shares_real_departure_evidence(monkeypatch):
    actual = np.array(Image.open(FIXTURES / "actual-matched-crop.png").convert("RGB"))
    main_image = np.full((100, 160, 3), 17, np.uint8)
    main_image[30:72, 20:126] = actual

    class NavigationApp(App):
        def click(self, **point):
            super().click(**point)
            self.image = main_image if len(self.clicks) == 1 else np.zeros((100, 160, 3), np.uint8)

    app, vision = NavigationApp(), CpuVision()
    monkeypatch.setattr(trade, "_check_trade_cancelled", lambda: None)
    monkeypatch.setattr(travel, "_check_intercity_cancelled", lambda: None)
    monkeypatch.setattr(trade, "_wait_template", lambda *args, **kwargs:
                        {"found": True, "center": [205, 40]})
    returned = trade.resonance_pc_go_city_main_direct(app=app, vision=vision)
    opened = travel._open_intercity_map(app, vision)
    assert returned["main_ready"]["confirmed"] and opened["transitioned"]
    assert returned["click_attempts"] == opened["click_attempts"] == 1
    assert app.clicks == [{"x": 205, "y": 40}, {"x": 1193, "y": 671}]
    assert len(app.captures) == 7 and all(region == depart.REGION for region in app.captures)


@pytest.mark.parametrize("value", [0, 255])
def test_blank_region_does_not_match_with_real_framework(value):
    result = probe(App(np.full((100, 160, 3), value, np.uint8)), CpuVision())
    assert not result["found"] and result["center"] is None


@pytest.mark.parametrize("score, expected", [(.89999, False), (.9, True), (.90001, True)])
def test_threshold_is_inclusive_and_uses_absolute_capture_coordinates(score, expected):
    vision = SequenceVision(hit(confidence=score))
    result = probe(vision=vision)
    assert result["found"] is expected
    assert result["center"] == ([1190, 665] if expected else None)
    options = vision.calls[0]
    assert options["threshold"] == .9
    assert options["match_method"] == cv2.TM_SQDIFF_NORMED
    assert options["use_grayscale"] is False and options["preprocess"] == "none"
    assert "mask" not in options and "mask_image" not in options


def test_found_flag_is_required_even_above_threshold():
    assert not probe(vision=SequenceVision(hit(found=False)))["found"]


@pytest.mark.parametrize("image", [None, np.zeros((720, 1280, 3), np.uint8),
                                   np.zeros((100, 160, 4), np.uint8),
                                   np.zeros((100, 160, 3), np.float32)])
def test_bad_capture_dimensions_and_type_are_structured_failure(image):
    app, vision = App(), SequenceVision()
    app.image = image
    with pytest.raises(depart.DepartButtonError) as error:
        probe(app, vision)
    assert error.value.code == "depart_capture_failed" and not vision.calls


def test_unsuccessful_capture_never_runs_matching():
    app, vision = App(), SequenceVision()
    app.success = False
    with pytest.raises(depart.DepartButtonError) as error:
        probe(app, vision)
    assert error.value.code == "depart_capture_failed" and not vision.calls


@pytest.mark.parametrize("result", [None, hit(confidence=float("nan")),
                                     hit(confidence=float("inf")),
                                     hit(debug_info={"error": "match failed"}),
                                     hit(center=(-1, 40)), hit(center=(160, 40)),
                                     hit(center=(70, 100)), hit(center=(70,)),
                                     hit(center=(float("nan"), 40))])
def test_invalid_matching_result_fails_closed(result):
    with pytest.raises(depart.DepartButtonError) as error:
        probe(vision=SequenceVision(result))
    assert error.value.code == "depart_template_match_failed"


@pytest.mark.parametrize("variant", ["missing", "wrong_size", "alpha"])
def test_missing_or_invalid_template_is_structured_and_does_not_capture(tmp_path, monkeypatch, variant):
    monkeypatch.setattr(depart, "_PLAN_ROOT", tmp_path)
    if variant != "missing":
        path = tmp_path / depart.TEMPLATE
        path.parent.mkdir(parents=True)
        shape = (42, 106, 4) if variant == "alpha" else (41, 106, 3)
        Image.fromarray(np.zeros(shape, np.uint8)).save(path)
    app = App()
    with pytest.raises(depart.DepartButtonError) as error:
        probe(app)
    assert error.value.code == "depart_template_invalid" and not app.captures


def test_wait_requires_two_consecutive_matches_and_is_fast(clock):
    vision = SequenceVision(hit())
    result = wait(vision)
    assert result["confirmed"] and result["confirmations"] == 2
    assert len(vision.calls) == 2 and result["elapsed_ms"] == 150
    assert clock.sleeps == [.15]


def test_wait_resets_stability_after_a_miss():
    vision = SequenceVision(hit(), hit(False), hit(), hit())
    result = wait(vision)
    assert result["confirmed"] and result["confirmations"] == 2
    assert len(vision.calls) == 4 and result["elapsed_ms"] == 450


def test_wait_cannot_reduce_confirmation_below_two():
    vision = SequenceVision(hit())
    result = wait(vision, stable_matches=1)
    assert result["confirmed"] and len(vision.calls) == 2


def test_wait_supports_stricter_confirmation_count():
    vision = SequenceVision(hit())
    assert wait(vision, stable_matches=3)["confirmations"] == 3
    assert len(vision.calls) == 3


def test_wait_timeout_is_explicit_without_clicking(clock):
    app, vision = App(), SequenceVision(hit(False))
    result = wait(vision, app, timeout_sec=.3)
    assert not result["confirmed"] and result["confirmations"] == 0
    assert result["elapsed_ms"] == 300 and not app.clicks
    assert clock.now == .3


def test_zero_timeout_cannot_certify_one_matching_frame():
    result = wait(timeout_sec=0)
    assert not result["confirmed"] and result["confirmations"] == 1


@pytest.mark.parametrize("cancel_at", [1, 2, 3, 4, 5])
def test_cancellation_interrupts_probe_or_poll_without_input(cancel_at):
    app, vision, checks = App(), SequenceVision(), []

    def check():
        checks.append(None)
        if len(checks) == cancel_at:
            raise asyncio.CancelledError()

    with pytest.raises(asyncio.CancelledError):
        depart.wait_depart_button(app=app, vision=vision, check_cancelled=check)
    assert not app.clicks


def match(found=True):
    return {"found": found, "confidence": .99 if found else .1,
            "center": [1200, 670] if found else None}


def ready(confirmed=True):
    return {"confirmed": confirmed, "match": match(confirmed),
            "confirmations": 2 if confirmed else 0, "elapsed_ms": 150}


def install_return_main(monkeypatch, *, initial=False, confirmations=(True,), train_after=True):
    calls, states = [], iter(confirmations)
    monkeypatch.setattr(trade, "_check_trade_cancelled", lambda: None)
    monkeypatch.setattr(trade, "probe_depart_button", lambda **kwargs: match(initial))

    def wait_main(**kwargs):
        assert kwargs["timeout_sec"] == 5
        calls.append("wait")
        return ready(next(states))

    monkeypatch.setattr(trade, "wait_depart_button", wait_main)
    monkeypatch.setattr(trade, "_wait_template", lambda *args, **kwargs:
                        {"found": True, "center": [205, 40]})
    monkeypatch.setattr(trade, "_match_template", lambda *args, **kwargs:
                        {"found": train_after, "center": [205, 40] if train_after else None})
    return calls


def test_return_main_clicks_train_then_confirms_without_fixed_two_second_wait(monkeypatch, clock):
    calls = install_return_main(monkeypatch)
    app = App()
    result = trade.resonance_pc_go_city_main_direct(app=app, vision=object())
    assert result["success"] and result["page_state"] == "city_main"
    assert result["main_ready"]["confirmed"] and result["click_attempts"] == 1
    assert app.clicks == [{"x": 205, "y": 40}] and calls == ["wait"]
    assert not any(clock.sleeps)


def test_return_main_already_present_stably_skips_train(monkeypatch):
    install_return_main(monkeypatch, initial=True)
    monkeypatch.setattr(trade, "_wait_template", lambda *args, **kwargs: pytest.fail("train probe"))
    app = App()
    result = trade.resonance_pc_go_city_main_direct(app=app, vision=object())
    assert result["skipped"] and result["click_attempts"] == 0 and not app.clicks


def test_return_main_retries_once_only_if_source_train_still_visible(monkeypatch):
    install_return_main(monkeypatch, confirmations=(False, True))
    app = App()
    result = trade.resonance_pc_go_city_main_direct(app=app, vision=object())
    assert result["click_attempts"] == 2 and len(app.clicks) == 2


def test_return_main_never_clicks_more_than_twice(monkeypatch):
    install_return_main(monkeypatch, confirmations=(False, False))
    app = App()
    with pytest.raises(trade.CityTradeFlowError) as error:
        trade.resonance_pc_go_city_main_direct(app=app, vision=object())
    assert error.value.code == "trade_main_not_restored" and len(app.clicks) == 2


def test_return_main_unknown_transition_does_not_retry(monkeypatch):
    install_return_main(monkeypatch, confirmations=(False,), train_after=False)
    app = App()
    with pytest.raises(trade.CityTradeFlowError) as error:
        trade.resonance_pc_go_city_main_direct(app=app, vision=object())
    assert error.value.code == "trade_main_not_restored" and len(app.clicks) == 1


def test_return_main_cancellation_before_train_click(monkeypatch):
    install_return_main(monkeypatch)
    app = App()
    def cancelled():
        raise trade.CityTradeFlowError("trade_cancelled", "Cancelled")
    monkeypatch.setattr(trade, "_check_trade_cancelled", cancelled)
    with pytest.raises(trade.CityTradeFlowError) as error:
        trade.resonance_pc_go_city_main_direct(app=app, vision=object())
    assert error.value.code == "trade_cancelled" and not app.clicks


def install_open_map(monkeypatch, states, confirmed=True):
    probes = iter(states)
    monkeypatch.setattr(travel, "_check_intercity_cancelled", lambda: None)
    monkeypatch.setattr(travel, "wait_depart_button", lambda **kwargs: ready(confirmed))
    monkeypatch.setattr(travel, "probe_depart_button", lambda **kwargs: match(next(probes)))


def test_open_map_requires_two_absent_frames_after_click_and_keeps_one_second(monkeypatch, clock):
    install_open_map(monkeypatch, [False, False])
    app = App()
    result = travel._open_intercity_map(app, object())
    assert result["transitioned"] and result["click_attempts"] == 1
    assert app.clicks == [{"x": 1200, "y": 670}]
    assert clock.sleeps == [1., .15]


def test_open_map_lost_click_retries_once_on_two_present_frames(monkeypatch):
    install_open_map(monkeypatch, [True, True, False, False])
    app = App()
    assert travel._open_intercity_map(app, object())["click_attempts"] == 2
    assert len(app.clicks) == 2


def test_open_map_still_present_after_two_clicks_is_failure(monkeypatch):
    install_open_map(monkeypatch, [True, True, True, True])
    app = App()
    with pytest.raises(travel.IntercityDestinationError) as error:
        travel._open_intercity_map(app, object())
    assert error.value.code == "depart_transition_unconfirmed" and len(app.clicks) == 2


def test_open_map_unstable_frames_do_not_allow_retry_or_claim_transition(monkeypatch):
    install_open_map(monkeypatch, [True, False, True, False, True])
    app = App()
    with pytest.raises(travel.IntercityDestinationError) as error:
        travel._open_intercity_map(app, object())
    assert error.value.code == "depart_transition_unconfirmed" and len(app.clicks) == 1


def test_open_map_initial_unstable_button_never_clicks(monkeypatch):
    install_open_map(monkeypatch, [], confirmed=False)
    app = App()
    with pytest.raises(travel.IntercityDestinationError) as error:
        travel._open_intercity_map(app, object())
    assert error.value.code == "depart_button_not_found" and not app.clicks


def test_open_map_cancellation_after_first_click_cannot_retry(monkeypatch):
    install_open_map(monkeypatch, [])
    app, checks = App(), []
    def cancelled():
        checks.append(None)
        if len(checks) == 2:
            raise asyncio.CancelledError()
    monkeypatch.setattr(travel, "_check_intercity_cancelled", cancelled)
    with pytest.raises(asyncio.CancelledError):
        travel._open_intercity_map(app, object())
    assert len(app.clicks) == 1


@pytest.mark.parametrize("target", ["trade", "travel"])
def test_depart_matching_error_propagates_with_no_click(monkeypatch, target):
    app = App()
    def failed(**kwargs):
        raise depart.DepartButtonError("depart_capture_failed", "Capture failed", {"fixture": True})
    if target == "trade":
        monkeypatch.setattr(trade, "probe_depart_button", failed)
        call = lambda: trade.resonance_pc_go_city_main_direct(app=app, vision=object())
        error_class = trade.CityTradeFlowError
    else:
        monkeypatch.setattr(travel, "wait_depart_button", failed)
        call = lambda: travel._open_intercity_map(app, object())
        error_class = travel.IntercityDestinationError
    with pytest.raises(error_class) as error:
        call()
    assert error.value.code == "depart_capture_failed" and error.value.detail == {"fixture": True}
    assert not app.clicks
