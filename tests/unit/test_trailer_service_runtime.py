"""Towing state-machine tests. No game capture, input, or network access."""

import asyncio
from copy import deepcopy
from pathlib import Path
from types import SimpleNamespace

import numpy as np
import pytest
import yaml

from packages.aura_core.scheduler.validation import InputValidator, _MISSING
from plans.resonance_pc.src.actions import trailer_service_pc_actions as m
from plans.resonance_pc.src.actions import _trailer_service_vision as v


STATES = {name: index for index, name in enumerate(
    ("map", "gold", "mileage", "fatigue_panel", "traveling", "arrival_ready", "unknown", "both"))}


class Clock:
    now = 0.

    def monotonic(self):
        return self.now

    def sleep(self, seconds):
        self.now += seconds


class App:
    def __init__(self):
        self.state = "map"
        self.default_payment = "gold"
        self.quota = {"remaining": 7, "total": 8, "used_today": 1}
        self.events = []
        self.miss_open = self.miss_payment = 0
        self.after_confirm = "traveling"
        self.raise_submit = False
        self.frames = 0
        self.quotas = []

    def capture(self):
        self.frames += 1
        frame = np.zeros((720, 1280, 3), np.uint8)
        frame[0, 0, 0] = STATES[self.state]
        return SimpleNamespace(success=True, image=frame)

    def click(self, *, x, y):
        point = (x, y)
        if point == (830, 640):
            assert self.state == "map"
            self.events.append("open")
            if self.miss_open:
                self.miss_open -= 1
            else:
                self.state = self.default_payment
        elif point in v.PAYMENT_POINTS.values():
            assert self.state in ("gold", "mileage")
            payment = next(key for key, value in v.PAYMENT_POINTS.items() if value == point)
            self.events.append("select_" + payment)
            if self.miss_payment:
                self.miss_payment -= 1
            else:
                self.state = payment
        elif point == (900, 525):
            self.events.append("confirm")
            if self.raise_submit:
                raise RuntimeError("Input acknowledgement lost")
            self.state = self.after_confirm
        else:
            pytest.fail(f"Unexpected game input {point}")


class Reader:
    def __init__(self, app):
        self.app = app

    def state(self, frame):
        return next(name for name, value in STATES.items() if value == frame[0, 0, 0])

    def match(self, frame, name):
        state = self.state(frame)
        is_prompt = state in ("gold", "mileage", "both")
        found = {
            "trailer_prompt": is_prompt,
            "trailer_entry": state == "map",
            "confirm": is_prompt,
            "cancel": is_prompt,
            "gold_selected": state in ("gold", "both"),
            "mileage_selected": state in ("mileage", "both"),
        }[name]
        return {"found": found, "confidence": 1. if found else .1,
                "center": (830, 640) if name == "trailer_entry" else (900, 525)}

    prompt = v.TrailerVision.prompt

    def read_quota(self, frame, ocr):
        assert self.state(frame) == "map"
        if self.app.quotas:
            return self.app.quotas.pop(0)
        return deepcopy(self.app.quota)

    def match_existing(self, frame, template, region, threshold):
        expected = {m.travel._FATIGUE_PANEL_TEMPLATE: "fatigue_panel",
                    m.travel._TRAVEL_HORN_TEMPLATE: "traveling",
                    m.travel._ARRIVAL_BUTTON_TEMPLATE: "arrival_ready"}[template]
        return {"found": self.state(frame) == expected}


@pytest.fixture
def rig(monkeypatch):
    clock, app = Clock(), App()
    monkeypatch.setattr(m.time, "monotonic", clock.monotonic)
    monkeypatch.setattr(m.time, "sleep", clock.sleep)
    monkeypatch.setattr(m, "is_current_task_cancel_requested", lambda: False)
    reader = Reader(app)
    monkeypatch.setattr(m, "TrailerVision", lambda **kwargs: reader)
    return SimpleNamespace(app=app, reader=reader, clock=clock)


def run(rig, **kwargs):
    return m.resonance_pc_use_trailer_service_from_map(app=rig.app, ocr=object(), vision=object(),
                                                     daily_first_n=kwargs.pop("daily_first_n", 2), **kwargs)


@pytest.mark.parametrize("remaining,total,limit,reason", [
    (7, 8, 1, "daily_limit_reached"), (0, 8, 99, "quota_exhausted"),
    (2, 8, 6, "daily_limit_reached"), (0, 15, 100, "quota_exhausted"),
])
def test_daily_limit_uses_actual_game_usage_and_never_drives(rig, remaining, total, limit, reason):
    rig.app.quota = {"remaining": remaining, "total": total, "used_today": total - remaining}
    result = run(rig, daily_first_n=limit)
    assert result["success"] and result["status"] == "not_used" and result["reason"] == reason
    assert result["page_state"] == "map" and not result["submitted"] and not result["departed"]
    assert not rig.app.events


@pytest.mark.parametrize("default,payment,events", [
    ("gold", "gold", ["open", "confirm"]),
    ("gold", "mileage", ["open", "select_mileage", "confirm"]),
    ("mileage", "gold", ["open", "select_gold", "confirm"]),
    ("mileage", "mileage", ["open", "confirm"]),
])
def test_payment_confirmed_before_single_submission(rig, default, payment, events):
    rig.app.default_payment = default
    result = run(rig, payment=payment)
    assert result["success"] and result["status"] == "departed" and result["departed"]
    assert result["submitted"] and result["page_state"] == "traveling"
    assert result["quota"] == rig.app.quota
    assert rig.app.events == events


def test_limit_above_account_total_is_allowed(rig):
    assert run(rig, daily_first_n=100)["departed"]


def test_recheck_quota_immediately_before_opening(rig):
    old, changed = rig.app.quota, {"remaining": 6, "total": 8, "used_today": 2}
    rig.app.quotas = [old, old, changed, changed]
    result = run(rig)
    assert result["status"] == "not_used" and result["quota"] == changed
    assert not rig.app.events


@pytest.mark.parametrize("misses,success,expected", [(1, True, 2), (3, True, 4), (4, False, 4)])
def test_open_retries_have_confirmed_map_and_allow_three_supplemental_clicks(rig, misses, success, expected):
    rig.app.miss_open = misses
    result = run(rig)
    assert result["success"] is success
    assert result["open_clicks"] == expected
    assert rig.app.events.count("open") == expected
    assert rig.app.events.count("confirm") == int(success)
    if not success:
        assert result["reason"] == "trailer_open_unconfirmed" and not result["submitted"]


@pytest.mark.parametrize("misses,success,expected", [(1, True, 2), (3, True, 4), (4, False, 4)])
def test_payment_retries_never_fallback_to_other_currency(rig, misses, success, expected):
    rig.app.miss_payment = misses
    result = run(rig, payment="mileage")
    assert result["success"] is success
    assert result["payment_clicks"] == expected
    assert rig.app.events.count("select_mileage") == expected
    assert "select_gold" not in rig.app.events
    assert rig.app.events.count("confirm") == int(success)


@pytest.mark.parametrize("after", ["unknown", "map", "gold"])
def test_modal_disappearance_or_unchanged_modal_never_proves_departure(rig, after):
    rig.app.after_confirm = after
    result = run(rig)
    assert not result["success"] and not result["departed"] and result["submitted"]
    assert result["reason"] == "trailer_departure_unconfirmed"
    assert rig.app.events == ["open", "confirm"]


def test_fatigue_panel_is_reported_without_medicine_or_second_departure(rig):
    rig.app.after_confirm = "fatigue_panel"
    result = run(rig)
    assert not result["success"] and result["reason"] == "fatigue_recovery_required"
    assert result["page_state"] == "fatigue_panel" and result["submitted"] and not result["departed"]
    assert rig.app.events == ["open", "confirm"]


def test_fast_arrival_is_positive_evidence_without_clicking_arrival_button(rig):
    rig.app.after_confirm = "arrival_ready"
    result = run(rig)
    assert result["departed"] and result["page_state"] == "arrival_ready"
    assert rig.app.events == ["open", "confirm"]


def test_confirm_input_exception_is_uncertain_and_never_retried(rig):
    rig.app.raise_submit = True
    result = run(rig)
    assert result["reason"] == "trailer_submit_uncertain" and result["submitted"]
    assert rig.app.events == ["open", "confirm"]


def test_preexisting_modal_cannot_bypass_game_quota(rig):
    rig.app.state = "gold"
    result = run(rig)
    assert result["reason"] == "trailer_map_or_quota_unconfirmed" and not result["submitted"]
    assert not rig.app.events


def test_ambiguous_selected_currency_is_not_clicked(rig):
    rig.app.default_payment = "both"
    result = run(rig, payment="mileage")
    assert not result["success"] and not result["submitted"]
    assert rig.app.events == ["open"]


def test_unreadable_quota_never_means_exhaustion_or_permission(rig, monkeypatch):
    def unreadable(*args):
        raise v.TrailerRecognitionError("trailer_quota_unreadable", "Missing slash")
    monkeypatch.setattr(rig.reader, "read_quota", unreadable)
    result = run(rig)
    assert result["reason"] == "trailer_map_or_quota_unconfirmed"
    assert not rig.app.events


def test_cancellation_before_input_propagates(rig, monkeypatch):
    monkeypatch.setattr(m, "is_current_task_cancel_requested", lambda: True)
    with pytest.raises(asyncio.CancelledError):
        run(rig)
    assert not rig.app.events


def test_cancellation_after_confirmation_never_submits_again(rig, monkeypatch):
    original = rig.app.capture
    def capture():
        if "confirm" in rig.app.events:
            raise asyncio.CancelledError()
        return original()
    monkeypatch.setattr(rig.app, "capture", capture)
    with pytest.raises(asyncio.CancelledError):
        run(rig)
    assert rig.app.events == ["open", "confirm"]


@pytest.mark.parametrize("bad", [0, -1, True, False, 1.0, "1", None])
def test_invalid_limit_rejected_before_capture(rig, bad):
    with pytest.raises(ValueError):
        run(rig, daily_first_n=bad)
    assert rig.app.frames == 0


@pytest.mark.parametrize("bad", ["", "coins", "Gold", True, None])
def test_invalid_payment_rejected_before_capture(rig, bad):
    with pytest.raises(ValueError):
        run(rig, payment=bad)
    assert rig.app.frames == 0


@pytest.mark.parametrize("text,remaining,total", [("7/8", 7, 8), (" 0 / 15 ", 0, 15), ("08/08", 8, 8)])
def test_quota_parser(text, remaining, total):
    assert v.parse_quota(text) == {"remaining": remaining, "total": total, "used_today": total - remaining}


@pytest.mark.parametrize("text", ["", "7", "78", "7|8", "7/8/8", "8/7", "0/0", "-1/8", "7/O8", "7/8foo"])
def test_quota_parser_never_repairs_ocr_guesses(text):
    with pytest.raises(v.TrailerRecognitionError):
        v.parse_quota(text)


def test_task_schema_defaults_and_canonical_callable():
    path = Path(__file__).resolve().parents[2] / "plans/resonance_pc/tasks/use_trailer_service_pc.yaml"
    task = yaml.safe_load(path.read_text(encoding="utf-8"))["use_trailer_service_pc"]
    inputs = {entry["name"]: entry for entry in task["meta"]["inputs"]}
    validator = InputValidator(None)
    assert validator.validate_input_value(inputs["daily_first_n"], _MISSING, "daily_first_n") == (True, 1, None)
    assert validator.validate_input_value(inputs["payment"], _MISSING, "payment") == (True, "gold", None)
    for value in (0, -1, True, "1", 1.0):
        assert not validator.validate_input_value(inputs["daily_first_n"], value, "daily_first_n")[0]
    for value in ("gold", "mileage"):
        assert validator.validate_input_value(inputs["payment"], value, "payment")[0]
    assert not validator.validate_input_value(inputs["payment"], "other", "payment")[0]
    assert task["steps"]["use_trailer"]["action"] == "resonance_pc.use_trailer_service_from_map"
    assert m.resonance_pc_use_trailer_service_from_map._service_dependencies == {
        "app": "plans/aura_base/app", "ocr": "plans/aura_base/ocr", "vision": "plans/aura_base/vision"}


def test_real_reader_ocr_is_only_a_small_numeric_crop():
    reader = v.TrailerVision(vision=object())
    frame = np.zeros((720, 1280, 3), np.uint8)
    x, y, width, height = v.QUOTA_REGION
    frame[y:y + height, x:x + width] = 123
    def recognize_all(*, source_image):
        assert source_image.shape == (height, width, 3)
        assert np.all(source_image == 123)
        return SimpleNamespace(results=[SimpleNamespace(text=text, center_point=(pos, 12), confidence=.99)
                                        for text, pos in [("8", 60), ("7", 10), ("/", 35)]])
    assert reader.read_quota(frame, SimpleNamespace(recognize_all=recognize_all)) == {
        "remaining": 7, "total": 8, "used_today": 1}


def test_payment_is_reverified_after_selection_before_commit(rig, monkeypatch):
    original = m._select_payment
    def change_after_selection(*args):
        result = original(*args)
        rig.app.state = "mileage"
        return result
    monkeypatch.setattr(m, "_select_payment", change_after_selection)
    result = run(rig, payment="gold")
    assert result["reason"] == "trailer_state_timeout" and not result["submitted"]
    assert result["page_state"] == "unknown" and rig.app.events == ["open"]


@pytest.mark.parametrize("shape", [(720, 1280, 4), (1080, 1920, 3), (720, 1280)])
def test_wrong_capture_format_never_authorizes_clicks(rig, monkeypatch, shape):
    monkeypatch.setattr(rig.app, "capture", lambda: SimpleNamespace(success=True, image=np.zeros(shape, np.uint8)))
    result = run(rig)
    assert result["reason"] == "trailer_frame_invalid" and not rig.app.events


def test_capture_failure_is_not_an_absent_button(rig, monkeypatch):
    monkeypatch.setattr(rig.app, "capture", lambda: SimpleNamespace(success=False, image=None))
    result = run(rig)
    assert result["reason"] == "trailer_capture_failed" and not rig.app.events
