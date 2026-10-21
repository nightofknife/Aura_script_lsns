"""Slow observation must not authorize black-moon selection or payment input."""
import asyncio
from types import SimpleNamespace

import pytest

from plans.resonance_pc.src.actions import black_moon_local_select_pc_actions as select
from tests.unit.test_black_moon_local_select_pc import App, Reader, card, completed_selection


@pytest.fixture
def clock(monkeypatch):
    state = SimpleNamespace(now=0.0, cancelled=False)

    async def sleep(seconds):
        state.now += seconds

    def cancelled():
        if state.cancelled:
            raise asyncio.CancelledError()

    monkeypatch.setattr(select, "time", SimpleNamespace(monotonic=lambda: state.now))
    monkeypatch.setattr(select.asyncio, "sleep", sleep)
    monkeypatch.setattr(select, "_cancel_check", cancelled)
    return state


def run_submit(app, reader, **kwargs):
    return asyncio.run(select.execute_black_moon_submit(completed_selection(), app, reader, **kwargs))


def run_select(app, reader):
    return asyncio.run(select.execute_black_moon_select({"a": True}, app, reader))


@pytest.mark.parametrize("second_capture,allowed", [(1.84, True), (1.85, False), (3.0, False)])
def test_submit_rejects_button_observation_finishing_at_or_after_deadline(clock, second_capture, allowed):
    app = App([[card(state="selected")]])
    reader = Reader(app)
    original_capture = app.capture
    costs = iter([3.0, second_capture])

    def capture():
        clock.now += next(costs)
        return original_capture()

    app.capture = capture
    if allowed:
        result = run_submit(app, reader)
        assert result["purchase_submitted"] and result["purchase_verified"] is False
    else:
        with pytest.raises(select.BlackMoonLocalSelectError) as caught:
            run_submit(app, reader)
        assert caught.value.code == "black_moon_submit_unconfirmed"
    assert app.purchase_clicks == len(app.clicks) == int(allowed)


def test_slow_button_recognition_cannot_submit(clock):
    app = App([[card(state="selected")]])
    reader = Reader(app)
    button = reader.button

    def slow_button(frame):
        clock.now += 3.0
        return button(frame)

    reader.button = slow_button
    with pytest.raises(select.BlackMoonLocalSelectError) as caught:
        run_submit(app, reader)
    assert caught.value.code == "black_moon_submit_unconfirmed"
    assert not app.clicks


@pytest.mark.parametrize("callback_cost,allowed", [(4.84, True), (4.85, False), (6.0, False)])
def test_submit_rechecks_deadline_after_before_submit_callback(clock, callback_cost, allowed):
    app = App([[card(state="selected")]])
    calls = []

    def before_submit():
        calls.append("before_submit")
        clock.now += callback_cost

    if allowed:
        result = run_submit(app, Reader(app), before_submit=before_submit)
        assert result["purchase_verified"] is False
    else:
        with pytest.raises(select.BlackMoonLocalSelectError) as caught:
            run_submit(app, Reader(app), before_submit=before_submit)
        assert caught.value.code == "black_moon_submit_unconfirmed"
    assert calls == ["before_submit"]
    assert len(app.clicks) == app.purchase_clicks == int(allowed)


@pytest.mark.parametrize("phase", ["capture", "callback"])
def test_submit_cancellation_takes_precedence_when_observation_also_times_out(clock, phase):
    app = App([[card(state="selected")]])
    callback = None
    if phase == "capture":
        original_capture = app.capture

        def capture():
            frame = original_capture()
            clock.now += 5.0
            clock.cancelled = True
            return frame

        app.capture = capture
    else:
        def callback():
            clock.now += 5.0
            clock.cancelled = True

    with pytest.raises(asyncio.CancelledError):
        run_submit(app, Reader(app), before_submit=callback)
    assert not app.clicks


def test_submit_missing_button_resets_two_frame_confirmation(clock):
    app = App([[card(state="selected")]])
    reader = Reader(app)
    hits = iter([True, False, True, True])
    reader.button = lambda frame: {"found": next(hits), "center": (1180, 680)}
    result = run_submit(app, reader)
    assert app.captures == 4 and app.purchase_clicks == 1
    assert result["purchase_verified"] is False


def test_submit_button_movement_requires_two_matching_centers(clock):
    app = App([[card(state="selected")]])
    reader = Reader(app)
    points = iter([(1100, 680), (1180, 680), (1180, 680)])
    reader.button = lambda frame: {"found": True, "center": next(points)}
    run_submit(app, reader)
    assert app.captures == 3 and app.purchase_clicks == 1
    assert app.clicks[0][:2] == (1180, 680)


def test_slow_stable_capture_does_not_select_or_scroll(clock):
    app = App([[card()]])
    reader = Reader(app)
    original_capture = app.capture

    def capture():
        clock.now += 3.0
        return original_capture()

    app.capture = capture
    with pytest.raises(select.BlackMoonLocalSelectError) as caught:
        run_select(app, reader)
    assert caught.value.code == "black_moon_select_not_ready"
    assert not app.clicks and not app.drags


def test_slow_frame_comparison_does_not_select_or_scroll(monkeypatch, clock):
    app = App([[card()]])
    reader = Reader(app)

    def same_view(*args):
        clock.now += 5.0
        return True

    monkeypatch.setattr(select, "_same_view", same_view)
    with pytest.raises(select.BlackMoonLocalSelectError) as caught:
        run_select(app, reader)
    assert caught.value.code == "black_moon_select_not_ready"
    assert not app.clicks and not app.drags


@pytest.mark.parametrize("phase", ["identity", "badge"])
def test_slow_preclick_identity_or_badge_check_does_not_use_expired_frame(clock, phase):
    app = App([[card()]])
    reader = Reader(app)
    if phase == "identity":
        identify = reader.identify

        def slow_identity(frame, current, candidates=None):
            if candidates is not None:
                clock.now += 5.0
            return identify(frame, current, candidates)

        reader.identify = slow_identity
    else:
        selected_mark = reader.selected_mark

        def slow_badge(frame, current):
            clock.now += 5.0
            return selected_mark(frame, current)

        reader.selected_mark = slow_badge
    with pytest.raises(select.BlackMoonLocalSelectError) as caught:
        run_select(app, reader)
    assert caught.value.code == "black_moon_select_not_ready"
    assert not app.clicks and not app.drags


def test_slow_postclick_badge_pair_cannot_confirm_selection(monkeypatch, clock):
    app = App([[card()]])
    reader = Reader(app)
    selected_mark = reader.selected_mark

    def slow_selected_mark(frame, current):
        if app.clicks:
            clock.now += 3.0
        return selected_mark(frame, current)

    reader.selected_mark = slow_selected_mark
    monkeypatch.setattr(select, "_MAX_SUPPLEMENTAL_CLICKS", 0)
    with pytest.raises(select.BlackMoonLocalSelectError) as caught:
        run_select(app, reader)
    assert caught.value.code == "black_moon_select_click_exhausted"
    assert len(app.clicks) == 1 and not app.drags and not app.purchase_clicks


def test_cancel_during_expensive_comparison_does_not_become_not_ready(monkeypatch, clock):
    app = App([[card()]])

    def same_view(*args):
        clock.now += 5.0
        clock.cancelled = True
        return True

    monkeypatch.setattr(select, "_same_view", same_view)
    with pytest.raises(asyncio.CancelledError):
        run_select(app, Reader(app))
    assert not app.clicks and not app.drags


@pytest.mark.parametrize("phase", ["cards", "identity"])
def test_cancellation_after_invalid_reader_result_precedes_business_error(clock, phase):
    app = App([[card()]])
    reader = Reader(app)
    if phase == "cards":
        cards = reader.cards
        calls = 0

        def invalid_cards(frame):
            nonlocal calls
            calls += 1
            if calls >= 2:
                clock.cancelled = True
                clock.now += 5.0
                return []
            return cards(frame)

        reader.cards = invalid_cards
    else:
        identify = reader.identify

        def invalid_identity(frame, current, candidates=None):
            if candidates is not None:
                clock.cancelled = True
                clock.now += 5.0
                return None
            return identify(frame, current, candidates)

        reader.identify = invalid_identity
    with pytest.raises(asyncio.CancelledError):
        run_select(app, reader)
    assert not app.clicks and not app.drags
