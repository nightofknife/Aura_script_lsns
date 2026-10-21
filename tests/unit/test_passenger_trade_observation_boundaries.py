"""Deadline and unknown-evidence regressions at passenger/trade input boundaries."""
from types import SimpleNamespace as NS
import asyncio

import numpy as np
import pytest

from plans.resonance_pc.src.actions import passenger_pc_actions as passenger
from plans.resonance_pc.src.actions import city_trade_flow_pc_actions as trade
from tests.unit.test_trade_buy_flow import buy_rig


_REAL_CLOSE_SETTLEMENT = trade._close_settlement


def install_clock(monkeypatch, module):
    clock = NS(now=0.0, cancelled=False, sleeps=[])

    def sleep(seconds):
        clock.sleeps.append(seconds)
        clock.now += seconds

    monkeypatch.setattr(module, "time", NS(monotonic=lambda: clock.now, sleep=sleep))
    monkeypatch.setattr(module, "is_current_task_cancel_requested", lambda: clock.cancelled)
    return clock


def rig(clock, *, delay=0.0, hit=None, capture_hook=None):
    calls, clicks = [], []

    def capture(*, rect):
        calls.append(tuple(rect))
        clock.now += delay
        if capture_hook is not None:
            capture_hook()
        return NS(success=True, image=np.zeros((rect[3], rect[2], 3), np.uint8))

    matches = []

    def find_template(**options):
        matches.append(options)
        return hit if hit is not None else NS(found=True, confidence=.99, center_point=(5, 6), debug_info={})

    return NS(capture=capture, click=lambda **point: clicks.append(point)), NS(find_template=find_template), calls, clicks, matches


def click_required(module, app, vision):
    if module is passenger:
        return passenger._click_template_required(app, vision, "some.png", [10, 20, 30, 40],
                                                   error_code="required_not_found", timeout_sec=3.0)
    return trade._click_required_nav_button(app, vision, template="some.png", region=[10, 20, 30, 40],
                                           error_code="required_not_found", page_state="previous", wait_sec=0)


@pytest.mark.parametrize("module", [passenger, trade])
@pytest.mark.parametrize("delay", [3.0, 3.01])
def test_slow_capture_at_or_after_deadline_cannot_authorize_click(monkeypatch, module, delay):
    clock = install_clock(monkeypatch, module)
    app, vision, captures, clicks, _ = rig(clock, delay=delay)
    error_type = passenger.PassengerPcError if module is passenger else trade.CityTradeFlowError
    with pytest.raises(error_type) as caught:
        click_required(module, app, vision)
    assert caught.value.code == "required_not_found"
    assert caught.value.detail["match"]["found"] is None
    assert caught.value.detail["match"]["reason"] == "observation_timeout"
    assert len(captures) == 1 and not clicks


@pytest.mark.parametrize("module", [passenger, trade])
def test_in_budget_positive_preserves_region_threshold_offset_and_one_click(monkeypatch, module):
    clock = install_clock(monkeypatch, module)
    app, vision, captures, clicks, matches = rig(clock, delay=2.99)
    click_required(module, app, vision)
    assert captures == [(10, 20, 30, 40)]
    assert clicks == [{"x": 15, "y": 26}]
    assert matches[0]["threshold"] == (passenger._TEMPLATE_THRESHOLD if module is passenger else trade._NAV_BUTTON_THRESHOLD)
    assert matches[0]["use_grayscale"] is True


@pytest.mark.parametrize("module", [passenger, trade])
def test_cancellation_during_overdue_capture_wins_over_timeout(monkeypatch, module):
    clock = install_clock(monkeypatch, module)
    app, vision, captures, clicks, _ = rig(clock, delay=4,
        capture_hook=lambda: setattr(clock, "cancelled", True))
    expected = asyncio.CancelledError if module is passenger else trade.CityTradeFlowError
    with pytest.raises(expected) as caught:
        click_required(module, app, vision)
    if module is trade:
        assert caught.value.code == "trade_cancelled"
    assert len(captures) == 1 and not clicks


@pytest.mark.parametrize("module", [passenger, trade])
@pytest.mark.parametrize("hit", [
    NS(found=False, confidence=0., center_point=None, debug_info={"error": "broken-template"}),
    NS(found=False, confidence=float("nan"), center_point=None),
    NS(found=True, confidence=.99, center_point=None),
    NS(found=True, confidence=.99, center_point=(30, 10)),
])
def test_invalid_observation_is_a_structured_error_not_a_business_miss(monkeypatch, module, hit):
    clock = install_clock(monkeypatch, module)
    app, vision, _, clicks, _ = rig(clock, hit=hit)
    error_type = passenger.PassengerPcError if module is passenger else trade.CityTradeFlowError
    with pytest.raises(error_type) as caught:
        click_required(module, app, vision)
    assert caught.value.code == ("passenger_template_observation_failed" if module is passenger else "trade_template_observation_failed")
    assert caught.value.detail["valid"] is False and caught.value.detail["found"] is None
    assert not clicks


def test_failed_passenger_score_observation_cannot_start_navigation_fallback(monkeypatch):
    clock = install_clock(monkeypatch, passenger)
    app, vision, _, clicks, _ = rig(clock,
        hit=NS(found=False, confidence=.0, debug_info={"error": "broken"}))
    monkeypatch.setattr(passenger, "resonance_pc_open_passenger_management", lambda **_: pytest.fail("unknown must stop before navigation"))
    with pytest.raises(passenger.PassengerPcError, match="passenger_template_observation_failed"):
        passenger.resonance_pc_recruit_passengers_by_flyer("海角城", app=app, vision=vision, ocr=object())
    assert not clicks


@pytest.mark.parametrize("module", [passenger, trade])
def test_explicit_zero_timeout_retains_exactly_one_read_only_probe(monkeypatch, module):
    clock = install_clock(monkeypatch, module)
    app, vision, captures, clicks, _ = rig(clock, delay=5)
    result = module._wait_template(app, vision, "some.png", [10, 20, 30, 40], timeout_sec=0)
    assert result["valid"] is True and result["found"] is True
    assert len(captures) == 1 and not clicks


def test_main_confirmation_cannot_accept_slow_second_positive(monkeypatch):
    clock = install_clock(monkeypatch, passenger)
    app, vision, captures, clicks, _ = rig(clock, delay=.4)
    result = passenger._wait_main_stable(app, vision, timeout_sec=1.0)
    assert not result["confirmed"] and result["confirmations"] == 1
    assert len(captures) == 2 and not clicks


@pytest.mark.parametrize("broken", [False, True])
def test_passenger_absence_requires_valid_in_budget_negative(monkeypatch, broken):
    clock = install_clock(monkeypatch, passenger)
    hit = NS(found=False, confidence=.1, debug_info={"error": "broken"} if broken else {})
    app, vision, _, clicks, _ = rig(clock, delay=0 if broken else 1.0, hit=hit)
    result = passenger._wait_template_absent(app, vision, "some.png", [10, 20, 30, 40], timeout_sec=1.0)
    assert not result["absent"]
    assert result["last_match"]["valid"] is False
    assert result["last_match"]["found"] is None
    assert not clicks


def test_passenger_absence_wait_retains_timely_presence_for_existing_dismiss_retry(monkeypatch):
    clock = install_clock(monkeypatch, passenger)
    app, vision, captures, clicks, _ = rig(clock)
    result = passenger._wait_template_absent(app, vision, "some.png", [10, 20, 30, 40], timeout_sec=1.0)
    assert not result["absent"]
    assert result["last_match"]["valid"] is True and result["last_match"]["found"] is True
    assert clock.now == 1.0 and len(captures) == 3 and not clicks


def test_passenger_dispatch_confirmation_uses_its_own_budget_and_cancel_guard(monkeypatch):
    clock = install_clock(monkeypatch, passenger)
    app, vision, _, clicks, _ = rig(clock, delay=1.2)
    monkeypatch.setattr(passenger, "_detect_dispatch_locations", lambda *_: {"available_locations": [{"center": [100, 200]}]})
    with pytest.raises(passenger.PassengerPcError) as caught:
        passenger._select_rightmost_dispatch(app, vision)
    assert caught.value.code == "dispatch_selection_not_confirmed"
    assert clicks == [{"x": 100, "y": 200}]


@pytest.mark.parametrize("module", [passenger, trade])
def test_input_rechecks_same_budget_after_wait_returns(monkeypatch, module):
    clock = install_clock(monkeypatch, module)
    clicks = []

    def delayed_return(*args, **kwargs):
        clock.now = 3.0
        return {"valid": True, "found": True, "center": [15, 26]}

    monkeypatch.setattr(module, "_wait_template", delayed_return)
    expected = passenger.PassengerPcError if module is passenger else trade.CityTradeFlowError
    with pytest.raises(expected) as caught:
        click_required(module, NS(click=lambda **point: clicks.append(point)), object())
    assert caught.value.code == "required_not_found"
    assert not clicks


def test_shop_menu_requires_two_timely_confirmations(monkeypatch):
    clock = install_clock(monkeypatch, trade)
    app, vision, captures, clicks, _ = rig(clock, delay=.4)
    with pytest.raises(trade.CityTradeFlowError) as caught:
        trade._wait_for_shop_menu_ready(app, vision, timeout_sec=1.0)
    assert caught.value.code == "shop_menu_not_ready"
    assert caught.value.detail["stable_matches"] == 1
    assert len(captures) == 2 and not clicks


@pytest.mark.parametrize("found", [False, True])
def test_settlement_wait_rejects_overdue_positive_and_negative(monkeypatch, found):
    clock = install_clock(monkeypatch, trade)
    app, vision, captures, clicks, _ = rig(clock, delay=1.0,
        hit=NS(found=found, confidence=.99 if found else .1))
    result = trade._wait_settlement_template(app, vision, "settlement.png", [0, 0, 30, 40],
                                            threshold=.82, timeout_sec=1.0)
    assert result["valid"] is False and result["found"] is None
    assert result["reason"] == "observation_timeout"
    assert len(captures) == 1 and not clicks


def test_settlement_presence_wait_retains_last_timely_negative_for_closure(monkeypatch):
    clock = install_clock(monkeypatch, trade)
    app, vision, captures, clicks, _ = rig(clock, hit=NS(found=False, confidence=.1))
    result = trade._wait_settlement_template(app, vision, "settlement.png", [0, 0, 30, 40],
                                            threshold=.82, timeout_sec=1.0)
    assert result["valid"] is True and result["found"] is False
    assert clock.now == 1.0 and not clicks
    assert len(captures) == 4


@pytest.mark.parametrize("late_stage", ["first", "recheck", "final"])
@pytest.mark.parametrize("recheck_found", [False, True])
@pytest.mark.parametrize("slow_stage", ["capture", "match"])
def test_closure_timeout_preserves_receipts_and_never_reuses_books(
        buy_rig, monkeypatch, late_stage, recheck_found, slow_stage):
    clock = install_clock(monkeypatch, trade)
    captures, dismissals = [], []

    def late():
        return (late_stage == "first" or (late_stage == "recheck" and len(captures) > 1)
                or (late_stage == "final" and len(captures) > 2))

    def capture(*, rect):
        captures.append(rect)
        if slow_stage == "capture" and late():
            clock.now += 4.0  # Exceeds the first 3s or the separate recheck 1.5s budget.
        return NS(success=True, image=np.zeros((rect[3], rect[2], 3), np.uint8))

    def match(**options):
        if slow_stage == "match" and late():
            clock.now += 4.0
        return NS(found=True if len(captures) == 1 or (late_stage == "final" and len(captures) == 2)
                  else recheck_found, confidence=.99)

    fake_app = NS(capture=capture, click=lambda **point: dismissals.append(point))
    fake_vision = NS(find_template=match)

    def close(*args, **kwargs):
        buy_rig.calls.append(("settlement", args, kwargs))
        return _REAL_CLOSE_SETTLEMENT(fake_app, fake_vision, "buy")

    monkeypatch.setattr(trade, "_close_settlement", close)
    monkeypatch.setattr(trade, "_wait_and_click_text", lambda *args, **kwargs: pytest.fail("cannot replay confirmation"))
    with pytest.raises(trade.CityTradeFlowError) as caught:
        buy_rig.run(books_used=2)
    assert caught.value.code == ("buy_transaction_not_confirmed" if late_stage == "first" else "buy_settlement_close_unconfirmed")
    if late_stage != "first":
        assert caught.value.detail["bought_confirmed"] is True
    assert caught.value.detail["settlement"]["closed"] is False
    assert caught.value.detail["settlement"]["observation_unknown"] is True
    assert caught.value.detail["book_result"] == {"ok": True, "used": 2}
    assert buy_rig.names().count("books") == buy_rig.names().count("click") == buy_rig.names().count("settlement") == 1
    assert len(dismissals) == {"first": 0, "recheck": 1, "final": 2}[late_stage]
    assert len(captures) == {"first": 1, "recheck": 2, "final": 3}[late_stage]


@pytest.mark.parametrize("delay", [2.5, 2.51])
@pytest.mark.parametrize("found", [False, True])
@pytest.mark.parametrize("slow_stage", ["capture", "match"])
def test_passenger_fresh_source_timeout_retains_revenue_and_never_clicks(
        monkeypatch, delay, found, slow_stage):
    clock = install_clock(monkeypatch, passenger)
    app, vision, captures, clicks, _ = rig(clock, delay=delay if slow_stage == "capture" else 0)
    monkeypatch.setattr(passenger, "_wait_template", lambda *args, **kwargs: {"valid": True, "found": True})
    monkeypatch.setattr(passenger, "_capture_text_items", lambda *args: [])
    monkeypatch.setattr(passenger, "_numeric_value_near_label", lambda items, aliases: 120)

    def match(**options):
        if slow_stage == "match":
            clock.now += delay
        return NS(found=found, confidence=.99)

    vision.find_template = match
    monkeypatch.setattr(passenger, "_wait_template_absent", lambda *args, **kwargs: pytest.fail("late source cannot advance"))
    with pytest.raises(passenger.PassengerPcError) as caught:
        passenger.resonance_pc_enter_city_and_settle_passengers(app=app, vision=vision, ocr=object())
    assert caught.value.code == "passenger_settlement_observation_failed"
    assert caught.value.detail["settlement_confirmed"] is True
    assert caught.value.detail["last_match"]["reason"] == "observation_timeout"
    assert caught.value.detail["revenue"]["total_revenue"] == 120
    assert len(captures) == 1 and not clicks
