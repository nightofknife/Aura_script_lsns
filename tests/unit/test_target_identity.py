"""Focus may change during input; bindings and coordinate geometry may not."""
import asyncio
from types import SimpleNamespace
from uuid import uuid4

import numpy as np
import pytest

from packages.aura_core.scheduler.operation_scope import OperationScope
from plans.aura_base.src.services.interaction_service import (
    InputSession, InteractionError, InteractionService, TransitionProbe,
)
from plans.aura_base.src.services.observation_service import (
    CaptureError, Frame, Observation, ObservationService,
)


BINDING_CHANGES = [
    ("hwnd", 321), ("pid", 654), ("create_time", 999.),
    ("binding_epoch", 2), ("client_rect", [0, 0, 20, 20]),
    ("client_rect_screen", [50, 50, 10, 10]),
]


class Target:
    def __init__(self):
        self.summary = dict(hwnd=123, pid=456, create_time=789., binding_epoch=1,
                            client_rect=[0, 0, 10, 10], client_rect_screen=[20, 20, 10, 10],
                            foreground=False)

    def target_summary(self):
        return dict(self.summary)


class App:
    def __init__(self):
        self.target_runtime = Target()
        self.events = []
        self.on_capture = self.on_move = self.on_click = lambda: None

    def capture(self, rect=None):
        self.events.append("capture")
        self.on_capture()
        return SimpleNamespace(success=True, image=np.zeros((10, 10, 3), np.uint8))

    def move_to(self, *args, **kwargs):
        self.events.append("move")
        self.target_runtime.summary["foreground"] = True
        self.on_move()

    def click(self, **kwargs):
        self.events.append("click")
        self.target_runtime.summary["foreground"] = True
        self.on_click()


def setup():
    app = App()
    observations = ObservationService(app)
    interactions = InteractionService(app, observations)
    scope = OperationScope(target=app.target_runtime, cid=uuid4().hex)
    return app, observations, interactions, scope


def click_source(app, observations, *, manual_frame=False):
    frame = (Frame.from_image(np.zeros((10, 10, 3), np.uint8),
                              target_identity=app.target_runtime.target_summary())
             if manual_frame else observations.capture())
    return frame, Observation("present", frame.id, client_center=(5, 5))


@pytest.mark.parametrize("asynchronous", [False, True])
@pytest.mark.parametrize("manual_frame", [False, True])
def test_background_window_activation_during_move_allows_click(asynchronous, manual_frame):
    app, observations, interactions, scope = setup()
    frame, source = click_source(app, observations, manual_frame=manual_frame)
    session = InputSession()
    if asynchronous:
        receipt = asyncio.run(interactions.click_async(source, frame=frame, scope=scope, session=session))
    else:
        receipt = interactions.click(source, frame=frame, scope=scope, session=session)
    assert receipt.sent and receipt.call_returned
    assert receipt.target_identity == frame.target_identity
    assert app.events == ([] if manual_frame else ["capture"]) + ["move", "click"]


@pytest.mark.parametrize("asynchronous", [False, True])
def test_focus_change_during_capture_retains_valid_frame(asynchronous):
    app, observations, _, scope = setup()
    app.on_capture = lambda: app.target_runtime.summary.update(foreground=True)
    frame = (asyncio.run(observations.capture_async(scope=scope)) if asynchronous
             else observations.capture(scope=scope))
    assert frame.image.shape == (10, 10, 3)
    assert app.events == ["capture"]


@pytest.mark.parametrize("asynchronous", [False, True])
def test_input_callback_can_activate_background_window_without_frame(asynchronous):
    app, _, interactions, scope = setup()
    receipt = (asyncio.run(interactions.send_input_async(app.click, scope=scope)) if asynchronous
               else interactions.send_input(app.click, scope=scope))
    assert receipt.sent and receipt.call_returned
    assert app.events == ["click"]


@pytest.mark.parametrize("asynchronous", [False, True])
def test_transition_survives_its_own_input_focus_change(asynchronous):
    app, _, interactions, scope = setup()

    def probe(allow_source):
        if "click" in app.events:
            return TransitionProbe("target", key="done")
        return TransitionProbe("source", key="button", hit={"center": (5, 5)})

    click = lambda *_args: app.click()
    result = (asyncio.run(interactions.run_transition_async(probe, click, scope=scope)) if asynchronous
              else interactions.run_transition(probe, click, scope=scope))
    assert result.confirmed
    assert len(result.receipts) == 1 and result.receipts[0].sent
    assert app.events == ["click"]


@pytest.mark.parametrize("field,value", BINDING_CHANGES)
def test_capture_rejects_real_binding_or_geometry_change(field, value):
    app, observations, _, scope = setup()
    app.on_capture = lambda: app.target_runtime.summary.update({field: value})
    with pytest.raises(CaptureError, match="identity changed"):
        observations.capture(scope=scope)
    assert app.events == ["capture"]


@pytest.mark.parametrize("stage", ["before_move", "during_move", "during_click"])
@pytest.mark.parametrize("field,value", BINDING_CHANGES)
def test_binding_and_geometry_changes_still_reject_input(field, value, stage):
    app, observations, interactions, scope = setup()
    frame, source = click_source(app, observations)
    mutate = lambda: app.target_runtime.summary.update({field: value})
    if stage == "before_move":
        mutate()
    elif stage == "during_move":
        app.on_move = mutate
    else:
        app.on_click = mutate
    with pytest.raises(InteractionError, match="input_target_changed") as error:
        interactions.click(source, frame=frame, scope=scope, session=InputSession())
    expected = {"before_move": ["capture"], "during_move": ["capture", "move"],
                "during_click": ["capture", "move", "click"]}
    assert app.events == expected[stage]
    if stage == "during_click":
        assert error.value.receipts[0].sent
