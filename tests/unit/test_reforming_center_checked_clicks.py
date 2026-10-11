"""Input acknowledgement is not a substitute for fresh visual confirmation."""
from types import SimpleNamespace

import numpy as np
import pytest

from plans.resonance_pc.src.actions import _reforming_center_clicks as clicks


class App:
    def __init__(self, response=None, error=None):
        self.response, self.error = response, error
        self.clicks, self.captures = [], []
        self.rgb = np.zeros((720, 1280, 3), dtype=np.uint8)
        self.rgb[0, 0] = [200, 100, 10]

    def capture(self, *, rect):
        self.captures.append(rect)
        return SimpleNamespace(success=True, image=self.rgb.copy())

    def click(self, *, x, y):
        self.clicks.append((x, y))
        if self.error:
            raise self.error
        return self.response


@pytest.fixture(autouse=True)
def no_sleep(monkeypatch):
    monkeypatch.setattr(clicks, "_sleep", lambda _: None)


def test_capture_converts_rgb_once_without_mutating_source():
    app = App()
    frame = clicks.read_frame(app)
    assert frame.shape == (720, 1280, 3)
    assert frame[0, 0].tolist() == [10, 100, 200]
    assert app.rgb[0, 0].tolist() == [200, 100, 10]
    assert app.captures == [(0, 0, 1280, 720)]


@pytest.mark.parametrize("image,success", [(None, True), (np.zeros((10, 10, 3)), True),
                                         (np.zeros((720, 1280, 3)), False)])
def test_bad_capture_aborts_before_input(image, success):
    app = App()
    app.capture = lambda **_: SimpleNamespace(success=success, image=image)
    with pytest.raises(clicks.CheckedClickError):
        clicks.read_frame(app)
    assert app.clicks == []


def test_none_input_return_remains_unacknowledged_and_quantized():
    app = App()
    record = clicks.send_click(app, (399, 438))
    assert len(app.clicks) == 1
    assert all(value % 5 == 0 for value in app.clicks[0])
    assert record["input_attempted"] is True
    assert record["input_acknowledged"] is None


@pytest.mark.parametrize("response", [False, SimpleNamespace(success=False)])
def test_explicit_input_failure_does_not_probe_or_retry(response):
    app = App(response=response)
    probes = []
    with pytest.raises(clicks.CheckedClickError):
        clicks.checked_click(app, (400, 440), lambda frame: probes.append(frame) or True)
    assert app.clicks == [(400, 440)]
    assert not probes


def test_input_exception_does_not_probe_or_retry():
    app = App(error=RuntimeError("send failed"))
    with pytest.raises(clicks.CheckedClickError):
        clicks.checked_click(app, (400, 440), lambda frame: True)
    assert app.clicks == [(400, 440)]
    assert not app.captures


def test_wait_requires_two_consecutive_equal_fresh_observations():
    app = App()
    values = iter(["dialog", False, "dialog", "dialog"])
    result = clicks.wait_confirmed(app, lambda frame: next(values))
    assert result == "dialog"
    assert len(app.captures) == 4


def test_effect_then_destination_readiness_use_separate_frames():
    app = App()
    seen = []
    def effect(frame):
        seen.append("effect")
        return True
    def ready(frame):
        seen.append("ready")
        return True
    record = clicks.checked_click(app, (960, 535), effect, ready=ready)
    assert record["input_acknowledged"] is None
    assert app.clicks == [(960, 535)]
    assert seen == ["effect", "ready", "ready"]
    assert len(app.captures) == 3


def test_visual_confirmation_failure_never_repeats_submit():
    app = App()
    with pytest.raises(clicks.CheckedClickError):
        clicks.checked_click(app, (960, 535), lambda frame: False)
    assert app.clicks == [(960, 535)]
