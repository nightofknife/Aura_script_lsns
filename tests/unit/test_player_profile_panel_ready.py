"""The profile panel is confirmed by a stable, panel-specific template."""

import itertools
from pathlib import Path

import cv2
import pytest

from plans.resonance_pc.src.actions import player_data_pc_actions as player_data


ROOT = Path(__file__).resolve().parents[2]


def _clock(monkeypatch):
    now = [0.0]
    monkeypatch.setattr(player_data.time, "monotonic", lambda: now[0])
    monkeypatch.setattr(player_data.time, "sleep", lambda seconds: now.__setitem__(0, now[0] + seconds))
    return now


def test_profile_panel_needs_two_consecutive_template_hits(monkeypatch):
    _clock(monkeypatch)
    calls = []
    hits = iter((True, False, True, True))

    def match(app, vision, template, region, *, threshold):
        calls.append((app, vision, template, region, threshold))
        return {"found": next(hits), "confidence": 0.96}

    monkeypatch.setattr(player_data, "_match_navigation_template", match)
    app, vision = object(), object()
    player_data._wait_for_profile_panel(app, vision, timeout_sec=2.0, interval_sec=0.1)

    assert calls == [
        (app, vision, player_data._PROFILE_MENU_TEMPLATE,
         player_data._PROFILE_MENU_MARKER_REGION, 0.85)
    ] * 4


def test_profile_panel_timeout_reports_last_template_confidence(monkeypatch):
    _clock(monkeypatch)
    hits = itertools.cycle((True, False))
    monkeypatch.setattr(
        player_data, "_match_navigation_template",
        lambda *args, **kwargs: {"found": next(hits), "confidence": 0.73},
    )

    with pytest.raises(player_data.StopTaskException, match="did not match twice consecutively") as exc:
        player_data._wait_for_profile_panel(object(), object(), timeout_sec=0.3, interval_sec=0.1)
    assert "last_confidence=0.730" in str(exc.value)


def test_profile_template_matches_supplied_panel_but_not_bento_cabinet():
    template = cv2.imread(str(ROOT / "plans/resonance_pc" / player_data._PROFILE_MENU_TEMPLATE),
                          cv2.IMREAD_GRAYSCALE)
    panel = cv2.imread(
        str(ROOT / "tests/fixtures/profile_status_digits/profile_panel_marker.png"),
        cv2.IMREAD_GRAYSCALE,
    )
    cabinet = cv2.imread(str(ROOT / "tests/fixtures/bento_consumption/love_usable.png"),
                         cv2.IMREAD_GRAYSCALE)
    x, y, w, h = player_data._PROFILE_MENU_MARKER_REGION
    assert template is not None and panel is not None and cabinet is not None
    assert panel.shape == (h, w)
    positive = float(cv2.matchTemplate(panel, template, cv2.TM_CCOEFF_NORMED).max())
    negative = float(cv2.matchTemplate(cabinet[y:y + h, x:x + w], template,
                                       cv2.TM_CCOEFF_NORMED).max())
    assert positive >= 0.85
    assert negative < 0.85
