from __future__ import annotations

import itertools
from pathlib import Path
from types import SimpleNamespace

import cv2
import pytest

from plans.resonance_pc.src.actions import trade_negotiation_pc_actions as negotiation


REPO_ROOT = Path(__file__).resolve().parents[1]
PLAN_ROOT = REPO_ROOT / "plans" / "resonance_pc"
FIXTURE_ROOT = REPO_ROOT / "tests" / "fixtures" / "resonance_pc_negotiation"


class _FakeApp:
    def __init__(self) -> None:
        self.clicks: list[tuple[int, int]] = []

    def click(self, *, x: int, y: int) -> None:
        self.clicks.append((x, y))


def _sequence_match(monkeypatch, found_sequence: list[bool]) -> None:
    sequence = iter(found_sequence)

    def match_template(**kwargs):
        found = next(sequence)
        result = {
            "found": found,
            "confidence": 0.96 if found else 0.40,
            "template": kwargs["template"],
            "region": list(kwargs["region"]),
        }
        if found and "button" in str(kwargs["template"]):
            result["center"] = [1160, 460]
        return result

    monkeypatch.setattr(negotiation, "_match_template", match_template)
    monkeypatch.setattr(negotiation.time, "sleep", lambda _seconds: None)


@pytest.mark.parametrize(
    ("kind", "template_name", "cap_fixture", "zero_fixture"),
    [
        ("bargain", "trade_buy_cap20_digits.png", "buy_cap20.png", "buy_zero.png"),
        ("raise", "trade_sell_cap20_digits.png", "sell_cap20.png", "sell_zero.png"),
    ],
)
def test_cap_templates_match_cap_and_reject_zero(kind, template_name, cap_fixture, zero_fixture):
    del kind
    template = cv2.imread(str(PLAN_ROOT / "templates" / template_name), cv2.IMREAD_GRAYSCALE)
    cap_image = cv2.imread(str(FIXTURE_ROOT / cap_fixture), cv2.IMREAD_GRAYSCALE)
    zero_image = cv2.imread(str(FIXTURE_ROOT / zero_fixture), cv2.IMREAD_GRAYSCALE)

    assert template is not None
    assert cap_image is not None
    assert zero_image is not None

    cap_score = float(cv2.minMaxLoc(cv2.matchTemplate(cap_image, template, cv2.TM_CCOEFF_NORMED))[1])
    zero_score = float(cv2.minMaxLoc(cv2.matchTemplate(zero_image, template, cv2.TM_CCOEFF_NORMED))[1])

    assert cap_score >= 0.88
    assert zero_score < 0.88


def test_not_requested_skips_capture_and_input():
    class NoTouch:
        def __getattr__(self, name):
            raise AssertionError(f"unexpected access: {name}")

    result = negotiation.execute_bargain_to_cap(
        requested_to_cap=False,
        app=NoTouch(),
        vision=NoTouch(),
    )

    assert result["requested_to_cap"] is False
    assert result["completed_to_cap"] is False
    assert result["detection_method"] == "template"


def test_initial_cap_requires_two_frames_and_does_not_click(monkeypatch):
    app = _FakeApp()
    _sequence_match(monkeypatch, [True, True])

    result = negotiation.execute_raise_to_cap(
        requested_to_cap=True,
        app=app,
        vision=object(),
    )

    assert result["completed_to_cap"] is True
    assert result["cap_confidence"] == pytest.approx(0.96)
    assert app.clicks == []


def test_single_cap_frame_does_not_finish_before_animation_cycle(monkeypatch):
    app = _FakeApp()
    _sequence_match(monkeypatch, [True, False, True, False, True, True, True])

    result = negotiation.execute_bargain_to_cap(
        requested_to_cap=True,
        app=app,
        vision=object(),
    )

    assert result["completed_to_cap"] is True
    assert app.clicks == [(1160, 460)]


def test_click_without_animation_is_retried_and_can_reach_cap(monkeypatch):
    app = _FakeApp()
    _sequence_match(
        monkeypatch,
        [False, True, True, False, False, True, False, True, True, True],
    )

    result = negotiation.execute_raise_to_cap(
        requested_to_cap=True,
        app=app,
        vision=object(),
        animation_start_timeout_sec=0,
    )

    assert result["completed_to_cap"] is True
    assert app.clicks == [(1160, 460), (1160, 460)]


def test_repeated_clicks_without_animation_raise_start_timeout(monkeypatch):
    clock = itertools.count(start=0, step=1)
    monkeypatch.setattr(negotiation.time, "monotonic", lambda: float(next(clock)))
    monkeypatch.setattr(
        negotiation,
        "_confirm_cap",
        lambda **_kwargs: {"confirmed": False, "confidence": 0.4},
    )
    monkeypatch.setattr(
        negotiation,
        "_wait_for_template_state",
        lambda expected_found, **_kwargs: {
            "found": True,
            "confidence": 0.95,
            "center": [1160, 460],
        },
    )

    with pytest.raises(negotiation.NegotiationExecutionError) as exc_info:
        negotiation.execute_bargain_to_cap(
            requested_to_cap=True,
            app=_FakeApp(),
            vision=object(),
            total_timeout_sec=100,
            animation_start_timeout_sec=0.1,
        )

    assert exc_info.value.code == "negotiation_animation_start_timeout"


def test_animation_finish_timeout_is_explicit(monkeypatch):
    waits = iter(
        [
            {"found": True, "confidence": 0.95, "center": [1160, 460]},
            {"found": False, "confidence": 0.1},
            {"found": False, "confidence": 0.1},
        ]
    )
    monkeypatch.setattr(
        negotiation,
        "_confirm_cap",
        lambda **_kwargs: {"confirmed": False, "confidence": 0.4},
    )
    monkeypatch.setattr(
        negotiation,
        "_wait_for_template_state",
        lambda **_kwargs: next(waits),
    )

    with pytest.raises(negotiation.NegotiationExecutionError) as exc_info:
        negotiation.execute_raise_to_cap(
            requested_to_cap=True,
            app=_FakeApp(),
            vision=object(),
        )

    assert exc_info.value.code == "negotiation_animation_finish_timeout"


def test_capture_failure_is_reported_as_page_lost():
    app = SimpleNamespace(capture=lambda **_kwargs: SimpleNamespace(success=False))

    with pytest.raises(negotiation.NegotiationExecutionError) as exc_info:
        negotiation.execute_bargain_to_cap(
            requested_to_cap=True,
            app=app,
            vision=object(),
        )

    assert exc_info.value.code == "negotiation_page_lost"


def test_zero_total_timeout_is_reported_as_cap_detection_timeout():
    with pytest.raises(negotiation.NegotiationExecutionError) as exc_info:
        negotiation.execute_raise_to_cap(
            requested_to_cap=True,
            app=_FakeApp(),
            vision=object(),
            total_timeout_sec=0,
        )

    assert exc_info.value.code == "negotiation_cap_detection_timeout"
