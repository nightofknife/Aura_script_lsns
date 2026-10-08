"""Page freshness must remain independent of slow cell processing."""
from threading import Event, Thread
import time
from types import SimpleNamespace

import numpy as np

from plans.resonance_pc.src.actions._deep_dive_layout_vision import LayoutScanner
from plans.resonance_pc.src.actions._deep_dive_scan_stream import ScanVisionStream


class Scanner(LayoutScanner):
    def __init__(self, entered, release):
        super().__init__()
        self.ready = True
        self.quality = 1.
        self.entered, self.release = entered, release

    def fork_semantic(self):
        return Scanner(self.entered, self.release)

    def track_frame(self, image, frame_id, **kwargs):
        return dict(tracking_ok=True, pose=self.pose_snapshot())

    def semantic_view(self, image, frame_id, pose, **kwargs):
        self.entered.set()
        if not self.release.wait(3):
            raise RuntimeError('test_semantic_release_timeout')
        return dict(tracking_ok=True)

    def annotate(self, image):
        return image.copy()


class Adapter:
    def __init__(self, old=False):
        self.generation = 0
        self.old = old
        self.last_at = 0.
        self.image = np.broadcast_to(np.arange(1280, dtype=np.uint8)[None, :, None],
                                     (720, 1280, 3)).copy()

    def capture_stream_frame(self, **kwargs):
        now = time.monotonic()
        if now-self.last_at < .025:
            return None
        self.last_at = now
        self.generation += 1
        return dict(capture=SimpleNamespace(success=True, image=self.image),
                    generation=self.generation, session_id=1,
                    arrived_at_monotonic=now-(2 if self.old else 0))


def wait_until(predicate, timeout=2):
    deadline = time.monotonic()+timeout
    while time.monotonic() < deadline:
        if predicate():
            return
        time.sleep(.01)
    raise AssertionError('condition did not become true')


def make_stream(tmp_path, observer, *, old=False):
    entered, release = Event(), Event()
    scanner = Scanner(entered, release)
    stream = ScanVisionStream(scanner, Adapter(old), tmp_path, [], time.monotonic(),
                              observe_fn=observer, evidence_kind='saved_frame_replay')
    return stream, entered, release


def assert_stopped(stream):
    assert all(not thread.is_alive() for thread in
               (stream._thread, stream._scene_thread, stream._semantic_thread, stream._writer))


def test_page_keeps_up_while_cell_owner_is_blocked(tmp_path):
    calls = []
    def observer(image):
        calls.append(time.monotonic())
        return dict(valid=True, scene='board', player_turn=True)
    stream, entered, release = make_stream(tmp_path, observer)
    stream.start()
    try:
        assert entered.wait(2)
        before = len(calls)
        time.sleep(.65)  # longer than the unchanged page freshness limit
        assert len(calls) >= before+3
        snapshot = stream.snapshot()
        assert snapshot['tracking_ok'] and snapshot['scene_tracking_ok']
        assert snapshot['scene_age_sec'] < .5
        assert stream._stats['semantic_frames'] == 0
        assert snapshot['scene_metadata']['frame_id'] > 0
    finally:
        release.set()
        stream.stop()
    assert_stopped(stream)


def test_non_board_page_interrupts_even_while_semantic_owner_is_busy(tmp_path):
    switched = Event()
    def observer(image):
        return (dict(valid=True, scene='enemy_turn', player_turn=False) if switched.is_set()
                else dict(valid=True, scene='board', player_turn=True))
    stream, entered, release = make_stream(tmp_path, observer)
    stream.start()
    try:
        assert entered.wait(2)
        switched.set()
        wait_until(lambda: stream.snapshot()['scene'] == 'enemy_turn')
        snapshot = stream.snapshot()
        assert not snapshot['tracking_ok']
        assert snapshot['scene_observation']['scene'] == 'enemy_turn'
    finally:
        release.set()
        stream.stop()
    assert_stopped(stream)
    assert stream.stats['error'] == 'scene_not_board'


def test_completing_page_check_does_not_freshen_old_source_frame(tmp_path):
    stream, entered, release = make_stream(
        tmp_path, lambda image: dict(valid=True, scene='board', player_turn=True), old=True)
    release.set()
    stream.start()
    try:
        wait_until(lambda: stream.snapshot()['scene_observation'] is not None)
        snapshot = stream.snapshot()
        assert snapshot['geometry_tracking_ok']
        assert not snapshot['tracking_ok']
        assert snapshot['pause_reason'] == 'scene_feedback_stale'
        assert snapshot['scene_age_sec'] >= 2
    finally:
        stream.stop()
    assert_stopped(stream)


def test_scene_from_a_different_capture_session_is_rejected(tmp_path):
    stream, entered, release = make_stream(
        tmp_path, lambda image: dict(valid=True, scene='board', player_turn=True))
    with stream._lock:
        stream._snapshot.update(session_id=1, map_revision=0)
        stream._scene_valid = True
        stream._scene_metadata = dict(session_id=2, map_revision=0)
    snapshot = stream.snapshot()
    assert not snapshot['tracking_ok']
    assert snapshot['pause_reason'] == 'scene_source_mismatch'


def test_stationary_verified_frames_replace_pending_semantics_without_waiting(tmp_path):
    stream, _, _ = make_stream(
        tmp_path, lambda image: dict(valid=True, scene='board', player_turn=True))
    scene_thread = Thread(target=stream._scene_loop)
    scene_thread.start()
    try:
        source_at = time.monotonic()
        for frame_id in (1, 2):
            packet = dict(image=stream._adapter.image, frame_id=frame_id,
                          generation=frame_id, session_id=1, map_revision=0,
                          frame_time=source_at + (frame_id-1)*.01,
                          pose=stream._scanner.pose_snapshot(),
                          observation=dict(tracking_ok=True))
            with stream._condition:
                stream._scene_slot = packet
                stream._condition.notify_all()
            wait_until(lambda: stream._stats['scene_frames'] == frame_id)
        with stream._lock:
            assert stream._slot['frame_id'] == 2
            assert stream._slot['frame_time'] == source_at + .01
            assert stream._slot['scene_observation']['player_turn']
            assert stream._stats['semantic_replaced'] == 1
            assert stream._stats['semantic_queue_peak'] == 1
            assert stream._stats['semantic_frames'] == 0
    finally:
        stream._stop.set()
        with stream._condition:
            stream._condition.notify_all()
        scene_thread.join(2)
    assert not scene_thread.is_alive()


def test_control_readers_cannot_mutate_published_evidence_or_scanner_history(tmp_path):
    from copy import deepcopy
    stream, _, _ = make_stream(
        tmp_path, lambda image: dict(valid=True, scene='board', player_turn=True))
    working = stream._semantic.result()
    entry = dict(frame_id=1, group=1, occupant='player', confidence=.9,
                 quad=[[1, 2], [3, 4]],
                 association_evidence=dict(rotation=np.eye(3).tolist()))
    working['cells'][0]['evidence'] = [entry]
    with stream._lock:
        stream._semantic_result = deepcopy(working)
        stream._semantic_state = stream._result_state(stream._semantic_result, copy_values=False)
    first = stream.snapshot()
    first['cells'][0]['evidence'][0]['quad'][0][0] = 999
    first['cells'][0]['evidence'][0]['association_evidence']['rotation'][0][0] = 999
    # The semantic owner may continue with new mutable source data.
    entry['quad'][0][0] = 777
    entry['association_evidence']['rotation'][0][0] = 777
    second = stream.snapshot()['cells'][0]['evidence'][0]
    assert second['quad'][0][0] == 1
    assert second['association_evidence']['rotation'][0][0] == 1
    assert stream._semantic_result['cells'][0]['evidence'][0] == second
