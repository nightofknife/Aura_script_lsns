"""Real new-size departure evidence and bounded navigation-input contracts."""
from __future__ import annotations

import asyncio
from pathlib import Path
from types import SimpleNamespace

import numpy as np
from PIL import Image
import pytest

from plans.resonance_pc.src.actions import _depart_button_vision as depart
from plans.resonance_pc.src.actions import _mainui_control_vision as controls
from plans.resonance_pc.src.actions import city_trade_flow_pc_actions as trade
from plans.resonance_pc.src.actions import city_travel_pc_actions as travel


FIXTURES = Path(__file__).resolve().parents[1] / "fixtures"
REGION = (1120, 620, 160, 100)


def main_region():
    crop = np.asarray(Image.open(FIXTURES / "mainui_controls/actual-bottom-right-20261104.png").convert("RGB"))
    return crop[20:120, 220:380].copy()


class App:
    def __init__(self, image=None, states=None):
        self.image = main_region() if image is None else image
        self.states = list(states or [])
        self.captures, self.clicks = [], []
        self.success = True

    def capture(self, *, rect):
        self.captures.append(rect)
        if self.states:
            present = self.states.pop(0) if len(self.states) > 1 else self.states[0]
            image = main_region() if present else np.zeros((100, 160, 3), np.uint8)
        else:
            image = self.image
        return SimpleNamespace(success=self.success, image=image)

    def click(self, **point):
        self.clicks.append(point)


@pytest.fixture(autouse=True)
def clock(monkeypatch):
    state = SimpleNamespace(now=0., sleeps=[])
    def sleep(seconds):
        state.sleeps.append(seconds)
        state.now += seconds
    monkeypatch.setattr(depart.time, "monotonic", lambda: state.now)
    monkeypatch.setattr(depart.time, "sleep", sleep)
    controls.load_control_profile.cache_clear()
    controls._load_pixels.cache_clear()
    yield state
    controls.load_control_profile.cache_clear()
    controls._load_pixels.cache_clear()


def probe(app=None, check_cancelled=lambda: None):
    return depart.probe_depart_button(app=app or App(), vision=object(), check_cancelled=check_cancelled)


def wait(app=None, **options):
    return depart.wait_depart_button(app=app or App(), vision=object(), check_cancelled=lambda: None, **options)


def test_observed_new_size_crop_matches_without_inputs():
    app = App()
    result = probe(app)
    assert result["valid"] is True and result["found"] is True and result["confidence"] >= .9
    assert app.captures == [REGION] and not app.clicks


def test_return_main_then_open_map_shares_real_departure_evidence(monkeypatch):
    expected = probe()["center"]
    class NavigationApp(App):
        def click(self, **point):
            super().click(**point)
            self.image = main_region() if len(self.clicks) == 1 else np.zeros((100, 160, 3), np.uint8)
    app = NavigationApp(np.zeros((100, 160, 3), np.uint8))
    monkeypatch.setattr(trade, "_check_trade_cancelled", lambda: None)
    monkeypatch.setattr(travel, "_check_intercity_cancelled", lambda: None)
    monkeypatch.setattr(trade, "_wait_template", lambda *args, **kwargs: {"found": True, "center": [205, 40]})
    returned = trade.resonance_pc_go_city_main_direct(app=app, vision=object())
    opened = travel._open_intercity_map(app, object())
    assert returned["main_ready"]["confirmed"] and opened["transitioned"]
    assert returned["click_attempts"] == opened["click_attempts"] == 1
    assert app.clicks == [{"x": 205, "y": 40}, {"x": expected[0], "y": expected[1]}]
    assert len(app.captures) == 7 and all(region == REGION for region in app.captures)


@pytest.mark.parametrize("image", [None, np.zeros((720, 1280, 3), np.uint8),
                                   np.zeros((100, 160, 4), np.uint8), np.zeros((100, 160, 3), np.float32)])
def test_bad_capture_dimensions_and_type_are_structured_failure(image):
    app = App()
    app.image = image
    with pytest.raises(depart.DepartButtonError) as error:
        probe(app)
    assert error.value.code == "depart_capture_failed" and not app.clicks


def test_unsuccessful_capture_is_unknown_not_absence():
    app = App()
    app.success = False
    with pytest.raises(depart.DepartButtonError) as error:
        probe(app)
    assert error.value.code == "depart_capture_failed" and not app.clicks


@pytest.mark.parametrize("variant", ["missing", "wrong_size", "alpha"])
def test_invalid_new_template_is_structured_failure(tmp_path, monkeypatch, variant):
    for filename in ("mainui_control_templates.json", "depart_button_template.json"):
        target = tmp_path / "data/meta" / filename
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_bytes((controls.PLAN_ROOT / "data/meta" / filename).read_bytes())
    if variant != "missing":
        target = tmp_path / "templates/navigation/travel/depart.png"
        target.parent.mkdir(parents=True)
        shape = (51, 130, 4) if variant == "alpha" else (42, 106, 3)
        Image.fromarray(np.zeros(shape, np.uint8)).save(target)
    monkeypatch.setattr(controls, "PLAN_ROOT", tmp_path)
    with pytest.raises(depart.DepartButtonError) as error:
        probe()
    assert error.value.code == "depart_template_invalid"


def test_wait_requires_two_consecutive_matches_and_is_fast(clock):
    app = App()
    result = wait(app)
    assert result["confirmed"] and result["confirmations"] == 2
    assert len(app.captures) == 2 and result["elapsed_ms"] == 150
    assert clock.sleeps == [.15]


def test_wait_resets_stability_after_a_miss():
    app = App(states=[True, False, True, True])
    result = wait(app)
    assert result["confirmed"] and result["confirmations"] == 2
    assert len(app.captures) == 4 and result["elapsed_ms"] == 450


@pytest.mark.parametrize("requested,required", [(1, 2), (3, 3)])
def test_wait_enforces_required_distinct_confirmations(requested, required):
    app = App()
    result = wait(app, stable_matches=requested)
    assert result["confirmations"] == required and len(app.captures) == required


def test_wait_timeout_is_explicit_without_clicking(clock):
    app = App(np.zeros((100, 160, 3), np.uint8))
    result = wait(app, timeout_sec=.3)
    assert not result["confirmed"] and result["confirmations"] == 0
    assert result["elapsed_ms"] == 300 and not app.clicks and clock.now == .3


def test_zero_timeout_cannot_certify_one_matching_frame():
    result = wait(timeout_sec=0)
    assert not result["confirmed"] and result["confirmations"] == 1


@pytest.mark.parametrize("cancel_at", [1, 2, 3, 4, 5])
def test_cancellation_interrupts_probe_or_poll_without_input(cancel_at):
    app, checks = App(), []
    def check():
        checks.append(None)
        if len(checks) == cancel_at:
            raise asyncio.CancelledError()
    with pytest.raises(asyncio.CancelledError):
        depart.wait_depart_button(app=app, vision=object(), check_cancelled=check)
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
    probe_times = []

    def absent(**kwargs):
        probe_times.append(clock.now)
        return match(False)

    monkeypatch.setattr(travel, "probe_depart_button", absent)
    app = App()
    result = travel._open_intercity_map(app, object())
    assert result["transitioned"] and result["click_attempts"] == 1
    assert app.clicks == [{"x": 1200, "y": 670}]
    # The full initial settling delay and distinct confirmation frames remain;
    # only sleep granularity changes to make cancellation responsive.
    assert probe_times == pytest.approx([1., 1.15])
    assert sum(clock.sleeps) == pytest.approx(1.15)
    assert all(0 < duration <= .05 for duration in clock.sleeps)


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
