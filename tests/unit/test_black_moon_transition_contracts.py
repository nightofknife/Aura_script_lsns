"""Public Black Moon transition contracts with the real timing budgets.

Only a virtual clock and fake game services are used. Keeping the production
budgets here catches accidental changes when preparation and exit share their
bounded probe implementation.
"""
import asyncio
from types import SimpleNamespace

import numpy as np
import pytest

from plans.resonance_pc.src.actions import black_moon_local_exit_pc_actions as exits
from plans.resonance_pc.src.actions import black_moon_local_prepare_pc_actions as prepare


PREPARATION = {
    0: dict(page_state="unknown", local_selected=None, batch_enabled=None, ready=False),
    1: dict(page_state="shop_page", local_selected=False, batch_enabled=False, ready=False),
    2: dict(page_state="local_shop", local_selected=True, batch_enabled=False, ready=False),
    3: dict(page_state="local_shop", local_selected=True, batch_enabled=True, ready=True),
    4: dict(page_state="shop_page", local_selected=True, batch_enabled=False, ready=False),
}
EXIT = {
    0: dict(page_state="unknown", shop_ready=False, total_zero=False),
    1: dict(page_state="local_shop", shop_ready=True, total_zero=True),
    2: dict(page_state="local_shop", shop_ready=True, total_zero=False),
    3: dict(page_state="rest_menu", shop_ready=False, total_zero=False),
    4: dict(page_state="main", shop_ready=False, total_zero=False),
}
VERIFIED = dict(success=True, ready_to_exit=True, purchase_verified=True)


def image(state, shade=0):
    frame = np.full((720, 1280, 3), shade, dtype=np.uint8)
    frame[0, 0, 0] = state
    return frame


@pytest.fixture
def clock(monkeypatch):
    state = SimpleNamespace(now=0.0, sleeps=[], cancelled=False)
    original_sleep = asyncio.sleep

    async def sleep(seconds):
        state.sleeps.append(seconds)
        state.now += seconds
        await original_sleep(0)

    timer = SimpleNamespace(monotonic=lambda: state.now)
    monkeypatch.setattr(prepare, "time", timer)
    monkeypatch.setattr(exits, "time", timer)
    monkeypatch.setattr(asyncio, "sleep", sleep)
    monkeypatch.setattr(prepare, "is_current_task_cancel_requested", lambda: state.cancelled)
    return state


class App:
    def __init__(self, clock, frames, *, after_click=None, cancel_on_click=False):
        self.clock = clock
        self.frames = frames
        self.after_click = after_click
        self.cancel_on_click = cancel_on_click
        self.captures = 0
        self.clicks = []

    def capture(self):
        self.captures += 1
        assert self.captures < 1000, "A transition must stop within its bounded probes"
        frame = self.frames(self) if callable(self.frames) else self.frames[min(self.captures - 1, len(self.frames) - 1)]
        return SimpleNamespace(success=True, image=frame)

    def click(self, *, x, y):
        self.clicks.append(dict(x=x, y=y, captures=self.captures, time=self.clock.now))
        if self.after_click is not None:
            self.frames = self.after_click(self)
        if self.cancel_on_click:
            self.clock.cancelled = True

    def __getattr__(self, name):
        if name in {"drag", "scroll", "press_key"}:
            raise AssertionError(f"A Black Moon transition must not call {name}")
        raise AttributeError(name)


class PrepareReader:
    def __init__(self, center=None):
        self.center = center or (lambda frame: (760, 170))
        self.observations = []
        self.matches = []

    def observe(self, frame):
        state = int(frame[0, 0, 0])
        self.observations.append(state)
        return dict(PREPARATION[state])

    def match(self, frame, name):
        state = int(frame[0, 0, 0])
        self.matches.append(name)
        source = {"shop_entry": 0, "local_tab_unselected": 1, "batch_off": 2}
        assert name in source, "Preparation must not locate a purchase control"
        return dict(found=state == source[name], center=self.center(frame))


class ExitReader:
    VIEWPORT = (594, 143, 684, 500)

    def __init__(self, center=None):
        self.center = center or (lambda frame: (90, 36))
        self.observations = []
        self.buttons = []

    def observe(self, frame):
        state = int(frame[0, 0, 0])
        self.observations.append(state)
        return dict(EXIT[state])

    def button(self, frame, target):
        assert target in {"main", "rest_menu"}, "Exit must never locate a payment control"
        self.buttons.append(target)
        return dict(found=int(frame[0, 0, 0]) == 1, center=self.center(frame))


def execute(kind, app, reader=None):
    if kind == "prepare":
        return asyncio.run(prepare.execute_black_moon_prepare_step("enable_batch", app, reader or PrepareReader()))
    return asyncio.run(exits.execute_black_moon_exit(dict(VERIFIED), "rest_menu", app, reader or ExitReader()))


@pytest.mark.parametrize("kind,source,target,settle", [("prepare", 2, 3, .35), ("exit", 1, 3, .5)])
def test_shared_transition_preserves_poll_and_distinct_settle_budgets(clock, kind, source, target, settle):
    app = App(clock, [image(source)], after_click=lambda _: [image(target)])
    result = execute(kind, app)
    assert result["success"] is True and result["click_count"] == 1
    assert result["supplemental_clicks"] == 0
    assert app.clicks[0]["time"] == pytest.approx(.2)
    assert clock.sleeps == pytest.approx([.2, settle, .2])


@pytest.mark.parametrize("kind,source,settle,error,code", [
    ("prepare", 2, .35, prepare.BlackMoonLocalPrepareError, "black_moon_prepare_transition_timeout"),
    ("exit", 1, .5, exits.BlackMoonLocalExitError, "black_moon_exit_click_exhausted"),
])
def test_each_attempt_retains_its_own_seven_second_feedback_probe(clock, kind, source, settle, error, code):
    app = App(clock, [image(source)])
    with pytest.raises(error) as caught:
        execute(kind, app)
    assert caught.value.code == code
    assert len(app.clicks) == 4
    assert caught.value.detail["click_count"] == 4
    assert clock.now >= 4 * (7 + settle)
    assert all(following["time"] - previous["time"] >= 7 + settle
               for previous, following in zip(app.clicks, app.clicks[1:]))


@pytest.mark.parametrize("kind,source,target", [("prepare", 2, 3), ("exit", 1, 3)])
def test_dropped_click_relocates_after_full_feedback_wait(clock, kind, source, target):
    app = App(clock, [image(source)], after_click=lambda current: [image(target if len(current.clicks) == 2 else source)])
    center = lambda _: (700 + 10 * len(app.clicks), 170)
    reader = PrepareReader(center) if kind == "prepare" else ExitReader(center)
    result = execute(kind, app, reader)
    assert result["click_count"] == 2 and result["supplemental_clicks"] == 1
    assert [click["x"] for click in app.clicks] == [700, 710]
    assert app.clicks[1]["captures"] > app.clicks[0]["captures"] + 2
    assert app.clicks[1]["time"] > 7


@pytest.mark.parametrize("kind,source,error,code", [
    ("prepare", 2, prepare.BlackMoonLocalPrepareError, "black_moon_prepare_source_unconfirmed"),
    ("exit", 1, exits.BlackMoonLocalExitError, "black_moon_exit_source_unknown"),
])
def test_unknown_feedback_never_authorizes_a_supplemental_click(clock, kind, source, error, code):
    app = App(clock, [image(source)], after_click=lambda _: [image(0)])
    with pytest.raises(error) as caught:
        execute(kind, app)
    assert caught.value.code == code
    assert len(app.clicks) == 1
    assert clock.now >= 14


def test_initial_enter_shop_requires_one_hit_without_initial_page_inference(clock):
    app = App(clock, [image(0)], after_click=lambda _: [image(1)])
    reader = PrepareReader()
    result = asyncio.run(prepare.execute_black_moon_prepare_step("enter_shop", app, reader))
    assert result["success"] is True and result["page_state"] == "shop_page"
    assert app.clicks[0]["captures"] == 1
    assert reader.observations == [1, 1]
    assert reader.matches == ["shop_entry"]
    assert clock.sleeps == pytest.approx([.35, .2])


def test_verify_ready_requires_two_consecutive_targets_and_never_clicks(clock):
    app = App(clock, [image(state) for state in [3, 0, 3, 3]])
    reader = PrepareReader()
    result = asyncio.run(prepare.execute_black_moon_prepare_step("verify_ready", app, reader))
    assert result["success"] is True and result["ready"] is True
    assert result["skipped"] is True and result["click_count"] == 0
    assert app.captures == 4 and not app.clicks and not reader.matches


def test_preparation_target_stability_includes_observed_page_identity(clock):
    app = App(clock, [image(state) for state in [4, 2, 2]])
    result = asyncio.run(prepare.execute_black_moon_prepare_step("select_local", app, PrepareReader()))
    assert result["success"] is True and result["click_count"] == 0
    assert app.captures == 3


def test_source_point_must_stabilize_before_preparation_input(clock):
    app = App(clock, [image(2, shade) for shade in [1, 2, 2]], after_click=lambda _: [image(3)])
    reader = PrepareReader(lambda frame: (700 + int(frame[0, 0, 1]) * 10, 170))
    result = execute("prepare", app, reader)
    assert result["click_count"] == 1
    assert app.clicks[0]["captures"] == 3 and app.clicks[0]["x"] == 720


def test_exit_source_requires_stable_goods_viewport_before_input(clock):
    app = App(clock, [image(1, shade) for shade in [0, 240, 240]], after_click=lambda _: [image(3)])
    result = execute("exit", app)
    assert result["click_count"] == 1
    assert app.clicks[0]["captures"] == 3


def test_exit_target_confirmation_uses_destination_identity_without_goods_viewport(clock):
    app = App(clock, [image(1)], after_click=lambda _: [image(3, 0), image(3, 240)])
    result = execute("exit", app)
    assert result["success"] is True and result["page_state"] == "rest_menu"
    assert len(app.clicks) == 1 and app.captures == 4


@pytest.mark.parametrize("kind", ["prepare", "exit", "wait_purchase"])
def test_failed_capture_with_valid_pixels_cannot_become_a_success(clock, kind):
    app = App(clock, [image(3 if kind == "prepare" else 1)])
    app.capture = lambda: SimpleNamespace(success=False, image=app.frames[0])
    with pytest.raises(prepare.BlackMoonLocalPrepareError) as caught:
        if kind == "wait_purchase":
            asyncio.run(exits.execute_black_moon_wait_purchase(dict(success=True, purchase_submitted=True), app, ExitReader()))
        else:
            execute(kind, app)
    assert caught.value.code == "black_moon_prepare_capture_failed"
    assert not app.clicks


@pytest.mark.parametrize("kind,source,target", [("prepare", 2, 3), ("exit", 1, 3)])
def test_cancellation_after_input_preserves_the_existing_prepare_error_and_stops(clock, kind, source, target):
    app = App(clock, [image(source)], after_click=lambda _: [image(target)], cancel_on_click=True)
    with pytest.raises(prepare.BlackMoonLocalPrepareError) as caught:
        execute(kind, app)
    assert caught.value.code == "black_moon_prepare_cancelled"
    assert len(app.clicks) == 1


def test_pending_purchase_uses_read_only_thirty_second_wait_without_payment_retry(clock):
    app = App(clock, [image(2)])
    reader = ExitReader()
    with pytest.raises(exits.BlackMoonLocalExitError) as caught:
        asyncio.run(exits.execute_black_moon_wait_purchase(dict(success=True, purchase_submitted=True), app, reader))
    assert caught.value.code == "black_moon_purchase_not_settled"
    assert clock.now == pytest.approx(30, abs=.2)
    assert not app.clicks and not reader.buttons
