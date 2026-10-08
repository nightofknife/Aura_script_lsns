"""Failure/cancellation ownership through fake app services, with real drain logic."""
import asyncio
import json
import threading
import time
from types import SimpleNamespace

import numpy as np
import pytest

from plans.resonance_pc.src.actions import _deep_dive_four_view_scan as scan
from plans.resonance_pc.src.actions import consciousness_deep_dive_scan_pc_actions as runtime


_REAL_CANCEL_CHECK = runtime._cancel_check


class SharedResource:
    def __init__(self, events):
        self.events = events
        self.close_calls = 0

    def close(self):
        self.close_calls += 1
        raise AssertionError('Scan must not close a shared task service')


class Controller(SharedResource):
    async def mouse_up_async(self, button):
        assert button == 'left'
        self.events.append('mouse_up')


class FakeSession(SharedResource):
    def capture_stream_frame(self, after_generation, *, expected_session_id=None):
        assert after_generation == -1 and expected_session_id is None
        self.events.append('capture_finished')
        return dict(session_id=1, generation=1, arrived_at_monotonic=time.monotonic(),
                    capture=SimpleNamespace(success=True, backend='wgc',
                        image=np.zeros((720, 1280, 3), dtype=np.uint8)))


class Detector(SharedResource):
    def __init__(self, events, *, started=None, release=None):
        super().__init__(events)
        self.started = started
        self.release = release

    def detect_packet(self, _rgb):
        self.events.append('detector_started')
        if self.started is not None:
            self.started.set()
            assert self.release.wait(2.), 'Test did not release its fake worker'
            self.events.append('detector_finished')
            return dict(targets=[])
        self.events.append('detector_failed')
        raise RuntimeError('fixture_detector_failed')


class App(SharedResource):
    def __init__(self, events):
        super().__init__(events)
        self.controller = Controller(events)
        self.session = FakeSession(events)
        self.target_runtime = SimpleNamespace(_get_or_create_session=lambda: self.session)

    def get_window_size(self):
        return 1280, 720


@pytest.fixture
def services(monkeypatch):
    events = []
    app = App(events)
    monkeypatch.setattr(scan, 'FourViewGeometry', lambda: object())
    monkeypatch.setattr(scan, 'write_layout_report', lambda _layout, _output: {})
    monkeypatch.setattr(runtime, '_cancel_check', lambda: None)
    monkeypatch.setattr(runtime, '_progress', lambda **_kwargs: None)
    monkeypatch.setattr(runtime, '_observe', lambda *_args:
                        dict(valid=True, scene='board', player_turn=True))
    async def reset(*_args):
        events.append('reset_finished')
        return dict(success=True)
    monkeypatch.setattr(runtime, '_reset_view', reset)
    return app, events


def assert_shared_ownership(app, detector, events, output):
    assert events.count('mouse_up') == 1
    assert app.close_calls == app.controller.close_calls == app.session.close_calls == detector.close_calls == 0
    cleanup = json.loads((output / 'cleanup.json').read_text('utf-8'))
    assert cleanup['mouse_released'] is True and cleanup['owned_operations_drained'] is True
    assert cleanup['shared_runtime_closed'] is False


def test_reset_failure_releases_mouse_without_touching_shared_services(tmp_path, monkeypatch, services):
    app, events = services
    detector = Detector(events)
    async def fail_reset(*_args):
        events.append('reset_failed')
        raise RuntimeError('fixture_reset_failed')
    monkeypatch.setattr(runtime, '_reset_view', fail_reset)
    result = asyncio.run(scan.run_four_view_scan(app, output_dir=tmp_path, entity_detector=detector))
    assert result['status'] == 'blocked' and 'fixture_reset_failed' in result['reason']
    assert events == ['reset_failed', 'mouse_up']
    assert_shared_ownership(app, detector, events, tmp_path)


def test_detector_failure_finishes_worker_before_releasing_mouse(tmp_path, services):
    app, events = services
    detector = Detector(events)
    result = asyncio.run(scan.run_four_view_scan(app, output_dir=tmp_path, entity_detector=detector))
    assert result['status'] == 'blocked' and 'fixture_detector_failed' in result['reason']
    assert events == ['reset_finished', 'capture_finished', 'detector_started', 'detector_failed', 'mouse_up']
    assert_shared_ownership(app, detector, events, tmp_path)


def test_task_cancellation_drains_inflight_detector_before_mouse_release(tmp_path, services):
    app, events = services
    started, release = threading.Event(), threading.Event()
    detector = Detector(events, started=started, release=release)

    async def exercise():
        task = asyncio.create_task(scan.run_four_view_scan(app, output_dir=tmp_path,
                                                         entity_detector=detector))
        try:
            assert await asyncio.to_thread(started.wait, 2.), 'Detector did not start'
            task.cancel()
            await asyncio.sleep(.02)
            # Real _await_serial shields the worker: cleanup cannot race it.
            assert not task.done() and 'mouse_up' not in events
            release.set()
            return await asyncio.wait_for(task, 2.)
        finally:
            release.set()
            if not task.done():
                await asyncio.wait_for(task, 2.)

    result = asyncio.run(exercise())
    assert result['status'] == 'cancelled' and result['reason'] == 'cancel_requested'
    assert events.index('detector_finished') < events.index('mouse_up')
    assert_shared_ownership(app, detector, events, tmp_path)


def test_cancel_hook_writes_cancelled_summary_and_releases_after_capture(tmp_path, monkeypatch, services):
    app, events = services
    detector = Detector(events)
    # Exercise the actual StopTaskException hook immediately after a serial
    # capture completes, rather than substituting asyncio task cancellation.
    monkeypatch.setattr(runtime, '_cancel_check', _REAL_CANCEL_CHECK)
    token = runtime._SCAN_CONTROL.set(dict(cancel_check=lambda: 'capture_finished' in events,
                                          latest_frame_metadata={}))
    try:
        result = asyncio.run(scan.run_four_view_scan(app, output_dir=tmp_path,
                                                    entity_detector=detector))
    finally:
        runtime._SCAN_CONTROL.reset(token)
    assert result['status'] == 'cancelled' and result['reason'] == 'cancel_requested'
    summary = json.loads((tmp_path / 'summary.json').read_text('utf-8'))
    assert summary['status'] == 'cancelled' and summary['success'] is False
    assert events == ['reset_finished', 'capture_finished', 'mouse_up']
    assert_shared_ownership(app, detector, events, tmp_path)


def test_actual_local_feedback_response_cannot_expand_into_coarse_jump():
    # scan03 frames 23 -> 26: a -20 px local adjustment changed the image
    # orientation error from 3.668 to 2.955 degrees. A Newton extrapolation
    # would request roughly -83 px; the local correction must remain bounded.
    dy = scan.bounded_feedback_step(2.955, previous=3.668, previous_dy=-20)
    assert -20 <= dy <= -4


@pytest.mark.parametrize('noisy_error', [3.668, 3.669, 3.8])
def test_flat_or_inverted_feedback_does_not_reverse_local_correction(noisy_error):
    # Flat/inverted measurements cannot prove a reversed input response. The
    # still-positive current error requires a small upward corrective drag.
    dy = scan.bounded_feedback_step(noisy_error, previous=3.668, previous_dy=-20)
    assert -20 <= dy <= -4
