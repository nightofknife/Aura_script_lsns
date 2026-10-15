"""Towing wraps existing destination/medicine/arrival flow without double departure."""

import asyncio

import pytest

from plans.resonance_pc.src.actions import city_travel_pc_actions as m
from plans.resonance_pc.src.actions import trailer_service_pc_actions as trailer
from tests.unit.test_departure_gate_flow import flow, run
from tests.unit.test_trailer_freight_policy import skip


def departed(**changes):
    return {"success": True, "status": "departed", "submitted": True, "departed": True,
            "reason": "departure_confirmed", "page_state": "traveling", **changes}


def setup(monkeypatch, flow, result):
    calls = []
    def tow(**kwargs):
        calls.append(kwargs)
        return result
    monkeypatch.setattr(trailer, "resonance_pc_use_trailer_service_from_map", tow)
    monkeypatch.setattr(m, "_wait_and_click_go_destination", lambda **kwargs:
                        flow.events.append("normal_go") or {"clicked": True})
    monkeypatch.setattr(m, "_wait_departure_gate", lambda **kwargs: {"state": "confirm_clicked"})
    return calls


def test_tow_success_bypasses_ordinary_departure_and_pickup(flow, monkeypatch):
    calls = setup(monkeypatch, flow, departed())
    def arrive(**kwargs):
        assert not kwargs.get("auto_pickup")
        flow.events.append("arrival")
        return {"status": "arrived"}
    monkeypatch.setattr(m, "resonance_pc_wait_intercity_arrival", arrive)
    result = run(flow, use_trailer=True, trailer_daily_first_n=3, trailer_payment="mileage", auto_pickup=True)
    assert result["success"] and result["travel_method"] == "trailer"
    assert calls[0]["daily_first_n"] == 3 and calls[0]["payment"] == "mileage"
    assert flow.events == ["open_map", ("select", "B", "A"), "arrival"]


@pytest.mark.parametrize("reason", ["quota_exhausted", "daily_limit_reached"])
def test_quota_skip_falls_through_to_normal_on_the_already_selected_map(flow, monkeypatch, reason):
    calls = setup(monkeypatch, flow, skip(reason))
    result = run(flow, use_trailer=True)
    assert result["success"] and result["travel_method"] == "normal"
    assert result["trailer_service"]["reason"] == reason and len(calls) == 1
    assert flow.events == ["open_map", ("select", "B", "A"), "normal_go"]


@pytest.mark.parametrize("value", [
    {"success": False, "status": "blocked", "reason": "trailer_departure_unconfirmed", "submitted": True},
    {"success": False, "status": "blocked", "reason": "trailer_map_or_quota_unconfirmed"},
    {"success": False, "status": "blocked", "reason": "trailer_payment_unconfirmed"},
    skip(submitted=True), skip(page_state="unknown"), departed(page_state="unknown"),
])
def test_unknown_payment_or_departure_never_drives_or_waits_as_if_departed(flow, monkeypatch, value):
    setup(monkeypatch, flow, value)
    monkeypatch.setattr(m, "resonance_pc_wait_intercity_arrival", lambda **kwargs: pytest.fail("Not departed"))
    result = run(flow, use_trailer=True)
    assert not result["success"] and result["status"] == "blocked"
    assert "normal_go" not in flow.events and not flow.clicks


def test_disabled_never_calls_towing_operation(flow, monkeypatch):
    setup(monkeypatch, flow, departed())
    monkeypatch.setattr(trailer, "resonance_pc_use_trailer_service_from_map",
                        lambda **kwargs: pytest.fail("No quota reads for normal legs"))
    result = run(flow)
    assert result["success"] and "normal_go" in flow.events
    assert "trailer_service" not in result


def test_quota_skip_stays_disabled_during_same_leg_medicine_retry(flow, monkeypatch):
    calls = setup(monkeypatch, flow, skip())
    gates = iter(["fatigue_panel", "confirm_clicked"])
    monkeypatch.setattr(m, "_wait_departure_gate", lambda **kwargs: {"state": next(gates)})
    monkeypatch.setattr(m, "wait_depart_button", lambda **kwargs: {"confirmed": True})
    result = run(flow, use_trailer=True, use_fatigue_medicine=True, allowed_fatigue_medicines=["提神棒棒糖"])
    assert result["success"] and result["travel_method"] == "normal" and len(calls) == 1
    assert flow.events.count("normal_go") == 2 and result["fatigue_medicine_use_count"] == 1


@pytest.mark.parametrize("use_medicine", [False, True])
def test_submitted_towing_fatigue_never_recovers_and_repeats_payment(flow, monkeypatch, use_medicine):
    calls = setup(monkeypatch, flow, {"success": False, "status": "blocked", "submitted": True,
                "departed": False, "reason": "fatigue_recovery_required", "page_state": "fatigue_panel"})
    monkeypatch.setattr(m, "_find_allowed_fatigue_medicine", lambda **kwargs: pytest.fail("Uncertain payment"))
    result = run(flow, use_trailer=True, use_fatigue_medicine=use_medicine,
                 allowed_fatigue_medicines=["提神棒棒糖"])
    assert not result["success"] and result["reason"] == "trailer_submission_requires_review"
    assert result["page_state"] == "fatigue_panel" and len(calls) == 1
    assert len(result["trailer_attempts"]) == 1 and result["fatigue_medicine_use_count"] == 0
    assert "normal_go" not in flow.events and not flow.clicks


def test_towing_fatigue_without_medicine_returns_blocked_with_metadata(flow, monkeypatch):
    setup(monkeypatch, flow, {"success": False, "status": "blocked", "submitted": True, "departed": False,
                              "reason": "fatigue_recovery_required", "page_state": "fatigue_panel"})
    monkeypatch.setattr(m, "_click_fatigue_back", lambda **kwargs: {"clicked": True})
    result = run(flow, use_trailer=True)
    assert result["status"] == "blocked" and result["trailer_service"]["page_state"] == "fatigue_panel"
    assert "normal_go" not in flow.events


def test_cancellation_propagates_without_normal_fallback(flow, monkeypatch):
    setup(monkeypatch, flow, None)
    def cancelled(**kwargs):
        raise asyncio.CancelledError()
    monkeypatch.setattr(trailer, "resonance_pc_use_trailer_service_from_map", cancelled)
    with pytest.raises(asyncio.CancelledError):
        run(flow, use_trailer=True)
    assert "normal_go" not in flow.events


@pytest.mark.parametrize("params", [{"use_trailer": 1}, {"trailer_daily_first_n": 0},
                                    {"trailer_daily_first_n": True}, {"trailer_payment": "other"}])
def test_invalid_options_stop_before_opening_map(flow, params):
    with pytest.raises(ValueError):
        run(flow, **params)
    assert not flow.events
