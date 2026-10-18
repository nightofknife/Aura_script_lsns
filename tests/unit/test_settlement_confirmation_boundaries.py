"""A settled transaction and a restored page require separate evidence."""
from types import SimpleNamespace as NS

import numpy as np
import pytest

from plans.resonance_pc.src.actions import city_trade_flow_pc_actions as trade
from plans.resonance_pc.src.actions import passenger_pc_actions as passenger
from plans.resonance_pc.src.actions._settlement_observation import observe_presence
from tests.unit.test_trade_buy_flow import buy_rig

_REAL_CLOSE_SETTLEMENT = trade._close_settlement


@pytest.mark.parametrize("mode", ["capture_fails", "still_visible"])
def test_close_does_not_claim_absence_without_valid_evidence(monkeypatch, mode):
    now, clicks = [0.0], []
    def capture(rect):
        success = not clicks or mode == "still_visible"
        return NS(success=success, image=np.zeros((rect[3], rect[2], 3), dtype=np.uint8) if success else None)
    monkeypatch.setattr(trade, "time", NS(monotonic=lambda: now[0], sleep=lambda seconds: now.__setitem__(0, now[0]+seconds)))
    monkeypatch.setattr(trade, "is_current_task_cancel_requested", lambda: False)
    result = trade._close_settlement(
        NS(capture=capture, click=lambda **kwargs: clicks.append(kwargs)),
        NS(find_template=lambda **kwargs: NS(found=True, confidence=.99, center_point=(10, 10))), "buy",
    )
    assert result["closed"] is False
    assert result["settlement_confirmed"] is True
    assert result["final_absence_verified"] is False
    assert len(clicks) == (1 if mode == "capture_fails" else 2)


def test_passenger_capture_failure_is_unknown_not_absent(monkeypatch):
    monkeypatch.setattr(passenger, "is_current_task_cancel_requested", lambda: False)
    result = passenger._wait_template_absent(
        NS(capture=lambda **kwargs: NS(success=False)), NS(),
        passenger._SETTLEMENT_TEMPLATE, passenger._SETTLEMENT_REGION, timeout_sec=1,
    )
    assert result["absent"] is False
    assert result["last_match"]["found"] is None
    assert result["last_match"]["valid"] is False


def test_confirmed_purchase_cannot_retry_transaction_when_closing_failed(buy_rig):
    buy_rig.settlement = {"closed": False, "settlement_confirmed": True,
                          "final_absence_verified": False}
    with pytest.raises(trade.CityTradeFlowError) as caught:
        buy_rig.run(books_used=2)
    assert caught.value.code == "buy_settlement_close_unconfirmed"
    assert caught.value.detail["bought_confirmed"] is True
    assert caught.value.detail["book_result"] == {"ok": True, "used": 2}
    assert buy_rig.names().count("books") == 1
    assert buy_rig.names().count("settlement") == 1
    assert buy_rig.names().count("click") == 1
    assert [args[2] for name, args, kwargs in buy_rig.calls if name == "text_hit"] == [("买入",)]


def test_unknown_initial_settlement_cannot_authorize_confirmation_click(buy_rig):
    buy_rig.settlement = {"closed": False, "settlement_confirmed": False, "observation_unknown": True}
    with pytest.raises(trade.CityTradeFlowError) as caught:
        buy_rig.run()
    assert caught.value.code == "buy_transaction_not_confirmed"
    assert buy_rig.names().count("settlement") == 1
    assert buy_rig.names().count("click") == 1
    assert [args[2] for name, args, kwargs in buy_rig.calls if name == "text_hit"] == [("买入",)]


def test_purchase_success_requires_positive_shop_evidence(buy_rig):
    buy_rig.settlement = {"closed": True, "settlement_confirmed": True, "final_absence_verified": True}
    result = buy_rig.run()
    assert result["bought_confirmed"] is True
    assert result["shop_ready"]["success"] is True
    assert buy_rig.names().count("shop_ready") == 1
    assert buy_rig.names().count("click") == 1


def test_missing_shop_after_purchase_preserves_transaction_evidence(buy_rig, monkeypatch):
    buy_rig.settlement = {"closed": True, "settlement_confirmed": True, "final_absence_verified": True}
    def fail(*args, **kwargs):
        raise trade.CityTradeFlowError("shop_menu_not_ready", "shop page unknown")
    monkeypatch.setattr(trade, "_wait_for_shop_menu_ready", fail)
    with pytest.raises(trade.CityTradeFlowError) as caught:
        buy_rig.run(books_used=2)
    assert caught.value.code == "shop_menu_not_ready"
    assert caught.value.detail["bought_confirmed"] is True
    assert caught.value.detail["book_result"] == {"ok": True, "used": 2}
    assert buy_rig.names().count("click") == 1
    assert buy_rig.names().count("settlement") == 1


@pytest.mark.parametrize("hit", [None, NS(found=False, confidence=float("nan")),
    NS(found=False, confidence=float("inf")), NS(found=None, confidence=.9),
    NS(found=False, confidence=.9, debug_info={"error": "broken"})])
def test_failed_match_is_never_a_valid_absence(hit):
    result = observe_presence(
        app=NS(capture=lambda **kwargs: NS(success=True, image=np.zeros((20, 20, 3), dtype=np.uint8))),
        vision=NS(find_template=lambda **kwargs: hit), template="test.png", region=(0, 0, 20, 20),
        threshold=.82, check_cancelled=lambda: None,
    )
    assert result["valid"] is False and result["found"] is None


@pytest.mark.parametrize("found", [False, True])
def test_valid_presence_retains_matching_parameters(found):
    calls = []
    def match(**kwargs):
        calls.append(kwargs)
        return NS(found=found, confidence=.83)
    result = observe_presence(
        app=NS(capture=lambda **kwargs: NS(success=True, image=np.zeros((20, 20, 3), dtype=np.uint8))),
        vision=NS(find_template=match), template="test.png", region=(1, 2, 20, 20),
        threshold=.82, check_cancelled=lambda: None,
    )
    assert result["valid"] is True and result["found"] is found
    assert calls[0]["use_grayscale"] is True and calls[0]["threshold"] == .82
    assert set(calls[0]) == {"source_image", "template_image", "threshold", "use_grayscale"}


def test_cancel_during_matching_propagates_before_observation_can_authorize_input():
    cancelled = [False]
    error = trade.CityTradeFlowError("trade_cancelled", "cancelled")
    def check():
        if cancelled[0]:
            raise error
    def match(**kwargs):
        cancelled[0] = True
        return NS(found=False, confidence=.1)
    with pytest.raises(trade.CityTradeFlowError) as caught:
        observe_presence(
            app=NS(capture=lambda **kwargs: NS(success=True, image=np.zeros((20, 20, 3), dtype=np.uint8))),
            vision=NS(find_template=match), template="test.png", region=(0, 0, 20, 20),
            threshold=.82, check_cancelled=check,
        )
    assert caught.value is error


def test_passenger_unknown_closure_stops_before_another_dismiss_click(monkeypatch):
    clicks = []
    captures = [True, False]
    def capture(**kwargs):
        valid = captures.pop(0)
        return NS(success=valid, image=np.zeros((180, 540, 3), dtype=np.uint8) if valid else None)
    monkeypatch.setattr(passenger, "_check_cancelled", lambda: None)
    monkeypatch.setattr(passenger, "_wait_template", lambda *args, **kwargs: {"found": True})
    monkeypatch.setattr(passenger, "_capture_text_items", lambda *args, **kwargs: [])
    with pytest.raises(passenger.PassengerPcError) as caught:
        passenger.resonance_pc_enter_city_and_settle_passengers(
            app=NS(click=lambda **kwargs: clicks.append(kwargs), capture=capture),
            ocr=NS(), vision=NS(find_template=lambda **kwargs: NS(found=True, confidence=.99)),
        )
    assert caught.value.code == "passenger_settlement_observation_failed"
    assert caught.value.detail["settlement_confirmed"] is True
    assert len(clicks) == 1


def test_cancel_while_closing_retains_positive_transaction_evidence(monkeypatch, buy_rig):
    original_close = _REAL_CLOSE_SETTLEMENT
    monkeypatch.setattr(trade, "_wait_settlement_template", lambda *args, **kwargs: {"valid": True, "found": True})
    calls = []
    def sleep(seconds):
        calls.append(seconds)
        if seconds == .8:
            raise trade.CityTradeFlowError("trade_cancelled", "cancelled during closure")
    monkeypatch.setattr(trade, "_sleep_with_cancel", sleep)
    # The rig passes an inert app; only the already-confirmed settlement dismiss is allowed here.
    fake_app = NS(click=lambda **kwargs: None)
    monkeypatch.setattr(trade, "_close_settlement", lambda app, vision, *args, **kwargs:
                        original_close(fake_app, vision, *args, **kwargs))
    with pytest.raises(trade.CityTradeFlowError) as caught:
        buy_rig.run(books_used=2)
    assert caught.value.code == "trade_cancelled"
    assert caught.value.detail["bought_confirmed"] is True
    assert caught.value.detail["settlement_confirmed"] is True
    assert caught.value.detail["book_result"] == {"ok": True, "used": 2}
    assert calls == [.5, .8]
    assert buy_rig.names().count("click") == 1


@pytest.mark.parametrize("first_recheck_visible", [False, True])
def test_settlement_closure_requires_a_valid_absent_frame(monkeypatch, first_recheck_visible):
    now, clicks, matches = [0.0], [], [0]
    def match(**kwargs):
        matches[0] += 1
        found = matches[0] == 1 or (first_recheck_visible and matches[0] == 2)
        return NS(found=found, confidence=.99 if found else .1)
    monkeypatch.setattr(trade, "time", NS(monotonic=lambda: now[0], sleep=lambda seconds: now.__setitem__(0, now[0]+seconds)))
    monkeypatch.setattr(trade, "is_current_task_cancel_requested", lambda: False)
    result = trade._close_settlement(
        NS(capture=lambda rect: NS(success=True, image=np.zeros((rect[3], rect[2], 3), dtype=np.uint8)),
           click=lambda **kwargs: clicks.append(kwargs)), NS(find_template=match), "buy",
    )
    assert result["closed"] is True and result["settlement_confirmed"] is True
    assert result["final_absence_verified"] is True
    assert len(clicks) == (2 if first_recheck_visible else 1)


def test_passenger_does_not_dismiss_again_after_positive_absence(monkeypatch):
    clicks, matches = [], [0]
    def match(**kwargs):
        matches[0] += 1
        return NS(found=matches[0] == 1, confidence=.99)
    monkeypatch.setattr(passenger, "_check_cancelled", lambda: None)
    monkeypatch.setattr(passenger, "_wait_template", lambda *args, **kwargs: {"found": True})
    monkeypatch.setattr(passenger, "_capture_text_items", lambda *args, **kwargs: [])
    with pytest.raises(passenger.PassengerPcError) as caught:
        passenger.resonance_pc_enter_city_and_settle_passengers(
            app=NS(click=lambda **kwargs: clicks.append(kwargs),
                   capture=lambda **kwargs: NS(success=True, image=np.zeros((180, 540, 3), dtype=np.uint8))),
            ocr=NS(), vision=NS(find_template=match),
        )
    assert caught.value.code == "settlement_dismiss_failed"
    assert len(clicks) == 1


def test_dismiss_input_exception_retains_transaction_receipt(monkeypatch):
    monkeypatch.setattr(trade, "_check_trade_cancelled", lambda: None)
    monkeypatch.setattr(trade, "_wait_settlement_template", lambda *args, **kwargs: {"valid": True, "found": True})
    def fail(**kwargs):
        raise OSError("input disconnected")
    with pytest.raises(trade.CityTradeFlowError) as caught:
        trade._close_settlement(NS(click=fail), NS(), "buy")
    assert caught.value.code == "trade_settlement_close_failed"
    assert caught.value.detail["settlement_confirmed"] is True


def test_legitimate_confirmation_panel_submits_only_once(buy_rig, monkeypatch):
    pending = {"closed": False, "settlement_confirmed": False, "observation_unknown": False}
    complete = {"closed": True, "settlement_confirmed": True, "final_absence_verified": True}
    results, closes, confirmations = iter((pending, complete)), [], []
    def close(*args, **kwargs):
        closes.append(True)
        return next(results)
    monkeypatch.setattr(trade, "_close_settlement", close)
    monkeypatch.setattr(trade, "_wait_for_text_hit", lambda *args, **kwargs: {"center": [10, 20]})
    monkeypatch.setattr(trade, "_wait_and_click_text", lambda *args, **kwargs:
                        confirmations.append(True) or {"clicked": True})
    result = buy_rig.run(books_used=2)
    assert result["bought_confirmed"] is True
    assert result["settlement_after_confirm"] == complete
    assert closes == [True, True] and confirmations == [True]
    assert buy_rig.names().count("click") == 1
    assert buy_rig.names().count("books") == 1
