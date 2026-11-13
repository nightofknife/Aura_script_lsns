"""The profile panel is confirmed by a stable, panel-specific template."""

import itertools
from pathlib import Path
from types import SimpleNamespace

import cv2
import pytest

from plans.resonance_pc.src.actions import player_data_pc_actions as player_data
from plans.resonance_pc.src.actions import player_recovery_pc_actions as recovery
from plans.aura_base.src.platform.contracts import TargetRuntimeError


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


def _open_scenario(monkeypatch, *, opens_on_click=1, initial_page="main"):
    now = _clock(monkeypatch)
    state = SimpleNamespace(page=initial_page, clicks=[], main_checks=[], profile_checks=[])
    monkeypatch.setattr(player_data, "check_cancelled", lambda: None)

    def profile(app, vision, template, region, *, threshold):
        state.profile_checks.append(now[0])
        assert template == player_data._PROFILE_MENU_TEMPLATE
        assert region == player_data._PROFILE_MENU_MARKER_REGION
        return {"found": state.page == "profile", "confidence": 0.97 if state.page == "profile" else 0.16}

    def main(app, vision, *, timeout_sec):
        state.main_checks.append((now[0], timeout_sec))
        now[0] += min(0.2, timeout_sec)
        return {"confirmed": state.page == "main"}

    def click(**kwargs):
        state.clicks.append((now[0], kwargs))
        if len(state.clicks) == opens_on_click:
            state.page = "profile"

    monkeypatch.setattr(player_data, "_match_navigation_template", profile)
    monkeypatch.setattr(player_data, "_wait_main_stable", main)
    return now, state, SimpleNamespace(click=click)


def test_open_profile_confirms_first_effective_click_without_retry(monkeypatch):
    now, state, app = _open_scenario(monkeypatch)

    player_data._open_profile_panel(app, object())

    assert state.clicks == [(0.2, {"x": 150, "y": 655})]
    assert len(state.profile_checks) == 4
    assert now[0] < 2.0


def test_open_profile_retries_an_ineffective_click_only_after_confirming_main(monkeypatch):
    now, state, app = _open_scenario(monkeypatch, opens_on_click=2)

    player_data._open_profile_panel(app, object())

    assert len(state.clicks) == 2
    assert len(state.main_checks) == 2
    assert state.clicks[1][0] - state.clicks[0][0] >= 2.0
    assert now[0] < 8.0


def test_open_profile_does_not_click_a_panel_that_is_already_open(monkeypatch):
    _, state, app = _open_scenario(monkeypatch, initial_page="profile")

    player_data._open_profile_panel(app, object())

    assert state.clicks == []
    assert state.main_checks == []
    assert len(state.profile_checks) == 2


def test_open_profile_rechecks_destination_after_waiting_for_main(monkeypatch):
    _, state, app = _open_scenario(monkeypatch)

    def main(*args, **kwargs):
        # A delayed menu open arrives while checking the previous source page.
        state.page = "profile"
        return {"confirmed": True}

    monkeypatch.setattr(player_data, "_wait_main_stable", main)
    player_data._open_profile_panel(app, object())

    assert state.clicks == []
    assert len(state.profile_checks) == 3


def test_open_profile_does_not_click_unknown_pages(monkeypatch):
    now, state, app = _open_scenario(monkeypatch, initial_page="unknown")

    with pytest.raises(player_data.StopTaskException, match="clicks=0/3.*last_page=unknown"):
        player_data._open_profile_panel(app, object())

    assert state.clicks == []
    assert now[0] == pytest.approx(8.0)
    assert all(start + timeout <= 8.0 for start, timeout in state.main_checks)


def test_open_profile_stops_after_three_clicks_with_one_shared_budget(monkeypatch):
    now, state, app = _open_scenario(monkeypatch, opens_on_click=None)

    with pytest.raises(player_data.StopTaskException, match="within 8.0s.*clicks=3/3"):
        player_data._open_profile_panel(app, object())

    assert len(state.clicks) == 3
    assert all(second[0] - first[0] >= 2.0 for first, second in zip(state.clicks, state.clicks[1:]))
    assert now[0] == pytest.approx(8.0)


def test_open_profile_does_not_catch_up_retries_after_an_unknown_page(monkeypatch):
    now, state, app = _open_scenario(monkeypatch, opens_on_click=None, initial_page="unknown")

    def main(*args, **kwargs):
        return {"confirmed": now[0] >= 5.0}

    monkeypatch.setattr(player_data, "_wait_main_stable", main)
    with pytest.raises(player_data.StopTaskException, match="clicks=2/3"):
        player_data._open_profile_panel(app, object())

    assert [when for when, _ in state.clicks] == [5.0, 7.0]
    assert now[0] == pytest.approx(8.0)


def test_open_profile_discards_main_confirmation_arriving_after_deadline(monkeypatch):
    now, state, app = _open_scenario(monkeypatch)

    def slow_main(*args, **kwargs):
        now[0] = 8.1
        return {"confirmed": True}

    monkeypatch.setattr(player_data, "_wait_main_stable", slow_main)
    with pytest.raises(player_data.StopTaskException, match="clicks=0/3"):
        player_data._open_profile_panel(app, object())
    assert state.clicks == []


def test_open_profile_discards_late_second_destination_hit(monkeypatch):
    now, state, app = _open_scenario(monkeypatch, initial_page="profile")
    calls = []

    def slow_profile(*args, **kwargs):
        calls.append(True)
        if len(calls) == 2:
            now[0] = 8.1
        return {"found": True, "confidence": 0.99}

    monkeypatch.setattr(player_data, "_match_navigation_template", slow_profile)
    with pytest.raises(player_data.StopTaskException, match="not confirmed within 8.0s"):
        player_data._open_profile_panel(app, object())
    assert state.clicks == []


def test_open_profile_resets_destination_stability_after_a_miss(monkeypatch):
    _, state, app = _open_scenario(monkeypatch, initial_page="unknown")
    hits = iter((True, False, True, False, True, True))
    seen = []

    def profile(*args, **kwargs):
        seen.append(next(hits))
        return {"found": seen[-1], "confidence": 0.99}

    monkeypatch.setattr(player_data, "_match_navigation_template", profile)
    player_data._open_profile_panel(app, object())
    assert seen == [True, False, True, False, True, True]
    assert state.clicks == []


@pytest.mark.parametrize("when", ["before_observation", "after_main_confirmation"])
def test_open_profile_cancellation_prevents_input(monkeypatch, when):
    _, state, app = _open_scenario(monkeypatch)
    cancelled = [when == "before_observation"]
    monkeypatch.setattr(recovery, "is_current_task_cancel_requested", lambda: cancelled[0])
    monkeypatch.setattr(player_data, "check_cancelled", recovery.check_cancelled)

    if when == "after_main_confirmation":
        def main(*args, **kwargs):
            cancelled[0] = True
            return {"confirmed": True}
        monkeypatch.setattr(player_data, "_wait_main_stable", main)

    with pytest.raises(player_data.StopTaskException, match="cancelled"):
        player_data._open_profile_panel(app, object())
    assert state.clicks == []
    if when == "before_observation":
        assert state.profile_checks == []


@pytest.mark.parametrize("stage", ["destination", "source", "click"])
def test_open_profile_propagates_observation_and_input_failures(monkeypatch, stage):
    _, state, app = _open_scenario(monkeypatch)
    error = TargetRuntimeError("window_target_lost", "target disappeared")

    def fail(*args, **kwargs):
        raise error

    if stage == "destination":
        monkeypatch.setattr(player_data, "_match_navigation_template", fail)
    elif stage == "source":
        monkeypatch.setattr(player_data, "_wait_main_stable", fail)
    else:
        app.click = fail

    with pytest.raises(TargetRuntimeError) as caught:
        player_data._open_profile_panel(app, object())
    assert caught.value is error
    assert state.clicks == []
