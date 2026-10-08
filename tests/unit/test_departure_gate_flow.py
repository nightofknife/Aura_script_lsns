"""Offline integration for template entry and post-medicine main readiness."""
import asyncio
from hashlib import sha256
import json
from pathlib import Path
from types import SimpleNamespace

import pytest

from plans.resonance_pc.src.actions import city_travel_pc_actions as m
from plans.resonance_pc.src.actions._depart_button_vision import DepartButtonError
from plans.resonance_pc.src.actions import _depart_button_vision as vision


@pytest.fixture
def flow(monkeypatch):
    events, clicks = [], []
    monkeypatch.setattr(m.time, "sleep", lambda seconds: None)
    monkeypatch.setattr(m, "_open_intercity_map", lambda *args: events.append("open_map"))
    def select(**kwargs):
        events.append(("select", kwargs["to_city_name"], kwargs["from_city_name"]))
        return {"to_city_name": kwargs["to_city_name"], "to_city_key": "B", "selected_point": [600, 350],
                "mode": "direct", "attempts_used": 1}
    monkeypatch.setattr(m, "resonance_pc_select_intercity_destination", select)
    monkeypatch.setattr(m, "_wait_and_click_go_destination", lambda **kwargs: {"clicked": True})
    monkeypatch.setattr(m, "resonance_pc_wait_intercity_arrival", lambda **kwargs:
                        {"status": "arrived", "encounter_actions": 0})
    monkeypatch.setattr(m, "_wait_for_marker_hit", lambda **kwargs: pytest.fail("No entry/main OCR allowed"))
    monkeypatch.setattr(m, "_find_allowed_fatigue_medicine", lambda **kwargs:
        {"name": "\u63d0\u795e\u68d2\u68d2\u7cd6", "match": {"found": True, "center": [600, 350]}})
    monkeypatch.setattr(m, "_wait_and_click_fatigue_medicine_confirm", lambda **kwargs: {"clicked": True})
    app = SimpleNamespace(click=lambda **kwargs: clicks.append(kwargs))
    return SimpleNamespace(events=events, clicks=clicks, app=app)


def run(flow, **kwargs):
    return m.resonance_pc_intercity_depart_and_wait(to_city_name="B", from_city_name="A",
                app=flow.app, ocr=object(), vision=object(), **kwargs)


def test_normal_route_keeps_map_selection_without_entry_ocr(flow, monkeypatch):
    monkeypatch.setattr(m, "_wait_departure_gate", lambda **kwargs: {"state": "confirm_clicked"})
    result = run(flow)
    assert result["success"] is True and result["arrival_status"] == "arrived"
    assert flow.events == ["open_map", ("select", "B", "A")]


@pytest.mark.parametrize("ready", [True, False])
def test_medicine_return_uses_shared_template_and_never_fullscreen_ocr(flow, monkeypatch, ready):
    gates = iter(["fatigue_panel", "confirm_clicked"])
    monkeypatch.setattr(m, "_wait_departure_gate", lambda **kwargs: {"state": next(gates)})
    def confirm(**kwargs):
        assert kwargs["timeout_sec"] == 5.0
        flow.events.append("main_template")
        return {"confirmed": ready}
    monkeypatch.setattr(m, "wait_depart_button", confirm)
    result = run(flow, use_fatigue_medicine=True,
                 allowed_fatigue_medicines=["\u63d0\u795e\u68d2\u68d2\u7cd6"])
    assert flow.clicks == [{"x": 600, "y": 350}]
    assert flow.events[:3] == ["open_map", ("select", "B", "A"), "main_template"]
    if ready:
        assert flow.events[3:] == ["open_map", ("select", "B", "A")]
        assert result["success"] is True and result["fatigue_medicine_use_count"] == 1
    else:
        assert result["status"] == "blocked" and result["reason"] == "fatigue_medicine_return_to_city_failed"
        assert len(flow.events) == 3


def test_medicine_template_failure_blocks_departure(flow, monkeypatch):
    monkeypatch.setattr(m, "_wait_departure_gate", lambda **kwargs: {"state": "fatigue_panel"})
    def confirm(**kwargs):
        raise DepartButtonError("depart_capture_failed", "capture failed")
    monkeypatch.setattr(m, "wait_depart_button", confirm)
    with pytest.raises(m.IntercityDestinationError) as error:
        run(flow, use_fatigue_medicine=True,
            allowed_fatigue_medicines=["\u63d0\u795e\u68d2\u68d2\u7cd6"])
    assert error.value.code == "depart_capture_failed"
    assert flow.events.count("open_map") == 1


def test_medicine_readiness_propagates_cancellation(flow, monkeypatch):
    monkeypatch.setattr(m, "_wait_departure_gate", lambda **kwargs: {"state": "fatigue_panel"})
    def confirm(**kwargs):
        raise asyncio.CancelledError()
    monkeypatch.setattr(m, "wait_depart_button", confirm)
    with pytest.raises(asyncio.CancelledError):
        run(flow, use_fatigue_medicine=True,
            allowed_fatigue_medicines=["\u63d0\u795e\u68d2\u68d2\u7cd6"])
    assert flow.events.count("open_map") == 1


def test_fatigue_required_does_not_open_a_second_map(flow, monkeypatch):
    monkeypatch.setattr(m, "_wait_departure_gate", lambda **kwargs: {"state": "fatigue_panel"})
    monkeypatch.setattr(m, "_click_fatigue_back", lambda **kwargs: {"clicked": True})
    result = run(flow)
    assert result["status"] == "blocked" and result["reason"] == "fatigue_recovery_required"
    assert flow.events.count("open_map") == 1


def test_entry_failure_never_runs_city_selector(flow, monkeypatch):
    def open_failed(*args):
        raise m.IntercityDestinationError("depart_transition_unconfirmed", "not opened")
    monkeypatch.setattr(m, "_open_intercity_map", open_failed)
    with pytest.raises(m.IntercityDestinationError):
        run(flow)
    assert flow.events == []


def test_packaged_departure_template_matches_its_provenance_and_runtime_contract():
    plan = Path(__file__).resolve().parents[2] / "plans/resonance_pc"
    metadata = json.loads((plan / "data/meta/depart_button_template.json").read_text(encoding="utf-8"))
    assert metadata["template"] == vision.TEMPLATE
    assert metadata["sha256"] == sha256((plan / vision.TEMPLATE).read_bytes()).hexdigest()
    assert metadata["mode"] == "RGB" and metadata["size"] == [106, 42]
    assert metadata["match"] == {
        "method": "TM_SQDIFF_NORMED", "threshold": vision.THRESHOLD,
        "use_grayscale": False, "preprocess": "none", "mask": False, "region": list(vision.REGION)}
    assert metadata["rendering"]["red_notification_included"] is False
