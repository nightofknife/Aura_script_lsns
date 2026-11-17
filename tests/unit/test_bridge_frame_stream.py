import threading
from types import SimpleNamespace
import time
from unittest.mock import patch
from uuid import uuid4

import numpy as np
import pytest
import win32event

from packages.aura_core.context.plan import current_plan_name
from plans.aura_base.src.platform.contracts import TargetRuntimeError
from plans.resonance_pc.src.actions.runtime_preflight_pc_actions import resonance_pc_require_client_resolution
from plans.resonance_pc.src.actions._deep_dive_four_view_reader import source_key
from plans.resonance_pc.src.actions._deep_dive_frame_source import stream_metadata
from plans.resonance_pc.src.bridge.frame_stream import BACKEND, BridgeFrameStream, HEIGHT, WIDTH, _HEADER, _PIXEL_BYTES
from plans.resonance_pc.src.services.bridge_target_runtime_service import ResonancePcTargetRuntimeService
from plans.resonance_pc.src.services.bridge_app_service import ResonancePcAppService
from plans.resonance_pc.src.services import input_bridge_service
from plans.resonance_pc.src.services.input_bridge_service import ResonancePcInputBridgeService


@pytest.fixture
def stream():
    frames = BridgeFrameStream(uuid4().hex, max_stale_ms=250)
    try:
        yield frames
    finally:
        frames.close()


def publish(stream, generation, *, age=0., state=1):
    tick = int((time.monotonic() - stream._clock_origin - age) * stream._frequency)
    # Store RGBA in Unity's bottom-up order.
    pixels = np.zeros((HEIGHT, WIDTH, 4), dtype=np.uint8)
    pixels[-1, 17] = (2, 7, 11, 99)
    stream._buffer[:_HEADER.size] = _HEADER.pack(state, WIDTH, HEIGHT, _PIXEL_BYTES, generation, tick, 100)
    stream._buffer[_HEADER.size:] = pixels.tobytes()


def test_poll_is_atomic_and_rejects_old_or_replaced_frames(stream):
    publish(stream, 1, age=2)
    assert stream.capture_stream_frame() is None
    publish(stream, 2)
    frame = stream.capture_stream_frame()
    assert frame['capture'].backend == BACKEND
    assert tuple(frame['capture'].image[0, 17]) == (2, 7, 11)
    assert stream.capture_stream_frame(after_generation=2) is None
    with pytest.raises(TargetRuntimeError) as replaced:
        stream.capture_stream_frame(expected_session_id=stream.session_id + 1)
    assert replaced.value.code == 'capture_stream_replaced'
    publish(stream, 3, state=2)
    with pytest.raises(TargetRuntimeError) as changed:
        stream.capture_stream_frame()
    assert changed.value.code == 'viewport_changed'
    stream.stop_reading()
    with pytest.raises(TargetRuntimeError) as closed:
        stream.capture_stream_frame()
    assert closed.value.code == 'capture_stream_closed'


def test_frame_poll_is_independent_of_input_and_drops_busy_buffers(stream):
    publish(stream, 1)
    assert win32event.WaitForSingleObject(stream._mutex, 0) == win32event.WAIT_OBJECT_0
    results = []
    reader = threading.Thread(target=lambda: results.append(stream.capture_stream_frame()))
    try:
        reader.start()
        reader.join(1)
        assert results == [None]
    finally:
        win32event.ReleaseMutex(stream._mutex)
        reader.join(1)
    bridge = ResonancePcInputBridgeService(config={})
    bridge._active = True
    bridge._frame_stream = stream
    bridge._session_root_cid = '-'
    assert bridge.open_frame_stream().capture_stream_frame()['generation'] == 1
    publish(stream, 2)
    frame = bridge.open_frame_stream().capture_stream_frame(after_generation=1)
    assert source_key(dict(stream_metadata(frame), frame_id=0, map_revision=0))


def test_runtime_roi_and_pixel_reads_use_the_same_rgb_coordinates(stream, monkeypatch):
    config = {'resonance_pc.input.mode': 'bridge'}
    monkeypatch.delenv('AURA_GUI_INPUT_BRIDGE', raising=False)
    bridge = ResonancePcInputBridgeService(config=config)
    runtime = ResonancePcTargetRuntimeService(config, bridge)
    bridge._viewport = {'width': WIDTH, 'height': HEIGHT}
    bridge._frame_stream = stream
    bridge._session_root_cid = '-'
    monkeypatch.setattr(bridge, 'viewport', lambda: (0, 0, WIDTH, HEIGHT))
    poll = stream.capture_stream_frame
    def refresh(*args, **kwargs):
        time.sleep(.032)
        publish(stream, 1 + max(0, kwargs.get('after_generation', -1)))
        return poll(*args, **kwargs)
    monkeypatch.setattr(stream, 'capture_stream_frame', refresh)
    monkeypatch.setattr(runtime, '_build_session', lambda *_: pytest.fail('desktop adapter started'))
    token = current_plan_name.set('resonance_pc')
    try:
        assert runtime.get_client_rect() == (0, 0, WIDTH, HEIGHT)
        area = runtime.capture((16, 0, 2, 1))
        assert area.relative_rect == (16, 0, 2, 1) and area.image_size == (2, 1)
        assert tuple(area.image[0, 1]) == (2, 7, 11)
        assert runtime.get_pixel_color_at(17, 0) == (2, 7, 11)
        with pytest.raises(TargetRuntimeError) as invalid:
            runtime.capture((WIDTH, 0, 1, 1))
        assert invalid.value.code == 'capture_rect_invalid'
    finally:
        current_plan_name.reset(token)


def test_resolution_preflight_preserves_the_restart_hint_for_an_old_bridge(monkeypatch):
    config = {'resonance_pc.input.mode': 'bridge'}
    bridge = ResonancePcInputBridgeService(config=config)
    runtime = ResonancePcTargetRuntimeService(config, bridge)
    app = ResonancePcAppService(SimpleNamespace(target_runtime=runtime), SimpleNamespace(bridge=bridge))
    monkeypatch.setenv('AURA_GUI_INPUT_BRIDGE', '1')
    monkeypatch.setattr(bridge, '_bridge_target', lambda: (222, 333))
    monkeypatch.setattr(input_bridge_service.win32gui, 'IsIconic', lambda _: True)

    def request(op, **_):
        assert op == 'status'
        return {'result': {'state': 'ready', 'capabilities': {'absolute_pointer': True}}}

    monkeypatch.setattr(input_bridge_service, 'BridgeClient', lambda _: SimpleNamespace(uncertain=False, request=request))
    token = current_plan_name.set('resonance_pc')
    try:
        with pytest.raises(TargetRuntimeError, match='重启雷索纳斯') as rejected:
            resonance_pc_require_client_resolution(app=app)
        assert rejected.value.code == 'bridge_restart_required'
        assert bridge._client is None and runtime._session is None
    finally:
        current_plan_name.reset(token)


@pytest.mark.parametrize('terminal_status', ['success', 'cancelled', 'failed'])
def test_root_release_orders_stream_close_and_ignores_earlier_task(stream, terminal_status):
    from packages.aura_game.runner import EmbeddedGameRunner

    bridge = ResonancePcInputBridgeService(config={})
    bridge._frame_stream = stream
    bridge._session_root_cid = 'active'
    pending = True
    requests = []
    def request(op, **_):
        with pytest.raises(TargetRuntimeError) as closed:
            stream.capture_stream_frame()
        assert closed.value.code == 'capture_stream_closed'
        assert not stream._closed
        requests.append(op)
    bridge._client = SimpleNamespace(request=request)
    runner = EmbeddedGameRunner()
    runner.get_run = lambda _: {'plan_name': 'resonance_pc', 'status': terminal_status,
                                 'execution_pending': pending}
    with patch('packages.aura_game.runner.service_registry.get_service_instance', return_value=bridge):
        assert runner.close_task_input('active') is False
        pending = False
        assert runner.close_task_input('old') is True
        assert requests == []
        assert runner.close_task_input('active') is True
    assert requests == ['stream_stop', 'close']
    assert stream._closed and bridge._frame_stream is None and bridge._client is None
