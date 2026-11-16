from types import SimpleNamespace

import numpy as np
import pytest

from plans.aura_base.src.platform.contracts import TargetRuntimeError
from plans.aura_base.src.platform.windows import capture_backends, wgc_hdr
from plans.aura_base.src.platform.windows.wgc_session import PersistentWgcSession


class DisplayApi:
    def __init__(self):
        self.epoch = 3  # generation 1, reliable notifications
        self.white = 0
        self.code = 0
        self.queries = []
        self.closed = []

    def aura_hdr_watch_open(self, hwnd):
        return hwnd

    def aura_hdr_watch_epoch(self, watch):
        return self.epoch

    def aura_hdr_watch_close(self, watch):
        self.closed.append(watch)

    def aura_hdr_display_query(self, hwnd, out):
        self.queries.append(hwnd)
        out._obj.value = self.white
        return self.code


@pytest.fixture
def api(monkeypatch):
    api = DisplayApi()
    monkeypatch.setattr(wgc_hdr, "_native", lambda: api)
    return api


def test_display_is_lazy_and_stable_frames_reuse_the_detection(api):
    state = wgc_hdr.DisplayColorState()
    assert api.queries == []
    assert all(state.current(11) == 0 for _ in range(1000))
    assert api.queries == [11]
    api.white = 2000
    api.epoch += 8  # a burst of notifications is consumed with one query
    assert state.current(11) == 2000
    assert api.queries == [11, 11]
    state.current(22)
    assert api.queries == [11, 11, 22]
    assert api.closed == [11]
    state.close()
    assert api.closed == [11, 22]


def test_legacy_detection_has_a_bounded_refresh_and_no_idle_timer(api, monkeypatch):
    api.epoch = 2
    clock = SimpleNamespace(value=100.)
    monkeypatch.setattr(wgc_hdr, "time", SimpleNamespace(monotonic=lambda: clock.value))
    state = wgc_hdr.DisplayColorState()
    assert state.current(11) == 0
    clock.value += 4.9
    assert state.current(11) == 0
    clock.value += .2
    assert api.queries == [11]
    api.white = 2500
    assert state.current(11) == 2500
    assert api.queries == [11, 11]


def test_detect_failure_is_not_misreported_as_sdr(api):
    api.code = -1
    state = wgc_hdr.DisplayColorState()
    for _ in range(2):
        with pytest.raises(TargetRuntimeError) as failed:
            state.current(11)
        assert failed.value.code == "windows_capture_hdr_display_unknown"
    assert api.queries == [11]


def test_backend_switches_format_but_keeps_session_for_white_only(api, monkeypatch):
    class Session:
        def __init__(self, *, hwnd, hdr_white, **kwargs):
            self.hwnd = hwnd
            self.hdr_white = hdr_white
            self.closed = False

        def close(self):
            self.closed = True

        def update_hdr_white(self, white):
            self.hdr_white = white

    monkeypatch.setattr(capture_backends, "PersistentWgcSession", Session)
    backend = capture_backends.WindowsWgcCaptureBackend(SimpleNamespace(hwnd=11), {"hdr_sdr_normalization": True})
    try:
        assert not api.queries
        sdr = backend._ensure_session_matches_target(check_color=True)
        assert sdr.hdr_white == 0
        api.white = 2000
        api.epoch += 2
        hdr = backend._ensure_session_matches_target(check_color=True)
        assert sdr.closed and hdr.hdr_white == 2000
        api.white = 2400
        api.epoch += 2
        assert backend._ensure_session_matches_target(check_color=True) is hdr
        assert not hdr.closed and hdr.hdr_white == 2400
        api.white = 0
        api.epoch += 2
        assert backend._ensure_session_matches_target(check_color=True).hdr_white == 0
        assert hdr.closed
    finally:
        backend.close()


def test_white_update_never_publishes_a_frame_with_the_previous_gain():
    session = PersistentWgcSession(hwnd=11, hdr_white=2000)
    capturer = SimpleNamespace(event=lambda fn: setattr(capturer, fn.__name__, fn))
    session._register_callbacks(capturer)
    pixels = np.zeros((2, 3, 4), dtype=np.uint8)
    capturer.on_frame_arrived(SimpleNamespace(frame_buffer=pixels, hdr_white=2000), None)
    np.testing.assert_array_equal(session.snapshot_full_frame(), pixels)
    session.update_hdr_white(2400)
    capturer.on_frame_arrived(SimpleNamespace(frame_buffer=pixels, hdr_white=2000), None)
    with pytest.raises(TargetRuntimeError) as failed:
        session.snapshot_full_frame()
    assert failed.value.code == "windows_capture_frame_unavailable"
    capturer.on_frame_arrived(SimpleNamespace(frame_buffer=pixels, hdr_white=2400), None)
    assert session.generation == 2
    np.testing.assert_array_equal(session.snapshot_full_frame(), pixels)
