"""Bounded, loss-accounted storage for the standalone deep-dive recorder."""
from __future__ import annotations

import hashlib
import json
from pathlib import Path
import queue
import threading
import time
import uuid


def atomic_json(path: Path, value: dict) -> None:
    temporary = path.with_name(path.name + '.' + uuid.uuid4().hex + '.part')
    with temporary.open('w', encoding='utf-8', newline='\n') as handle:
        json.dump(value, handle, ensure_ascii=False, indent=2, allow_nan=False)
        handle.write('\n')
    # Windows readers may briefly hold the old status file without delete sharing.
    for attempt in range(10):
        try:
            temporary.replace(path)
            break
        except PermissionError:
            if attempt == 9:
                raise
            time.sleep(.01)


class RunState:
    def __init__(self):
        self.stop = threading.Event()
        self.lock = threading.Lock()
        self.errors: list[dict] = []

    def fail(self, reason: str) -> None:
        with self.lock:
            if len(self.errors) < 64:
                self.errors.append(dict(monotonic_ns=time.monotonic_ns(), reason=str(reason)))
        self.stop.set()


class Timeline:
    """Callbacks enqueue only; a dedicated writer owns the append-only journal."""
    def __init__(self, folder: Path, state: RunState, capacity: int = 16384):
        self.state = state
        self.queue = queue.Queue(maxsize=capacity)
        self.done = threading.Event()
        self.written = 0
        self.lost = 0
        self.error = None
        self.thread = threading.Thread(target=self._write, args=(folder / 'timeline.jsonl',),
                                       name='deep-dive-input-journal', daemon=True)
        self.thread.start()

    def emit(self, row: dict) -> None:
        value = dict(row)
        value.setdefault('monotonic_ns', time.monotonic_ns())
        try:
            self.queue.put_nowait(value)
        except queue.Full:
            self.lost += 1
            self.state.fail('timeline_queue_full')

    def _write(self, path: Path) -> None:
        try:
            with path.open('x', encoding='utf-8', newline='\n', buffering=1) as handle:
                while not self.done.is_set() or not self.queue.empty():
                    try:
                        row = self.queue.get(timeout=.1)
                    except queue.Empty:
                        continue
                    row['journal_sequence'] = self.written
                    handle.write(json.dumps(row, ensure_ascii=False, allow_nan=False) + '\n')
                    self.written += 1
        except BaseException as exc:
            self.error = f'{type(exc).__name__}: {exc}'
            self.state.fail('timeline_write_failed:' + self.error)

    def close(self, timeout=10.) -> dict:
        self.done.set()
        self.thread.join(timeout)
        if self.thread.is_alive():
            self.error = 'timeline_drain_timeout'
            self.state.fail(self.error)
        return dict(written=self.written, lost=self.lost, pending=self.queue.qsize(), error=self.error,
                    writer_stopped=not self.thread.is_alive(),
                    complete=not self.error and not self.lost and self.queue.empty())


class FrameStore:
    """Save every accepted callback as native BGR(A) PNG, without resizing/cropping."""
    def __init__(self, folder: Path, state: RunState, timeline: Timeline, capacity: int):
        self.folder, self.state, self.timeline = folder, state, timeline
        (folder / 'frames').mkdir()
        self.queue = queue.Queue(maxsize=capacity)
        self.done = threading.Event()
        self.first = threading.Event()
        self.lock = threading.Lock()
        self.received = self.enqueued = self.saved = self.dropped = self.high_water = 0
        self.first_ns = self.last_ns = None
        self.max_interval_ns = 0
        self.shape = None
        self.error = None
        self.accepting = True
        self.after_seal = 0
        self.thread = threading.Thread(target=self._write, name='deep-dive-png-writer', daemon=True)
        self.thread.start()

    def offer(self, native, source: dict) -> None:
        # The WGC callback transfers an owned array; it is never mutated later.
        arrived = source['callback_monotonic_ns']
        with self.lock:
            if not self.accepting:
                self.after_seal += 1
                return
            frame_id = self.received
            self.received += 1
            if self.last_ns is not None:
                self.max_interval_ns = max(self.max_interval_ns, arrived - self.last_ns)
            self.first_ns = arrived if self.first_ns is None else self.first_ns
            self.last_ns = arrived
            shape = tuple(native.shape)
            if self.shape is not None and self.shape != shape:
                self.dropped += 1
                self.state.fail('native_frame_shape_changed')
                return
            self.shape = shape
            row = dict(source, frame_id=frame_id, path=f'frames/{frame_id:08d}.png',
                       native_shape=list(shape), pixel_format='BGRA' if shape[2] == 4 else 'BGR')
            try:
                self.queue.put_nowait((native, row))
            except queue.Full:
                self.dropped += 1
                self.timeline.emit(dict(event='frame_lost', reason='frame_queue_full', source=row))
                self.state.fail('frame_queue_full')
                return
            self.enqueued += 1
            self.high_water = max(self.high_water, self.queue.qsize())
        self.first.set()

    def _write(self) -> None:
        try:
            import cv2
            with (self.folder / 'frames.jsonl').open('x', encoding='utf-8', newline='\n', buffering=1) as index:
                while not self.done.is_set() or not self.queue.empty():
                    try:
                        native, row = self.queue.get(timeout=.1)
                    except queue.Empty:
                        continue
                    started = time.monotonic_ns()
                    ok, encoded = cv2.imencode('.png', native, [cv2.IMWRITE_PNG_COMPRESSION, 1])
                    if not ok:
                        raise OSError('PNG encoding returned false')
                    payload = encoded.tobytes()
                    path = self.folder / row['path']
                    temporary = path.with_suffix('.png.part')
                    with temporary.open('xb') as handle:
                        handle.write(payload)
                    temporary.replace(path)
                    row.update(png_sha256=hashlib.sha256(payload).hexdigest(), bytes=len(payload),
                               write_started_monotonic_ns=started, saved_monotonic_ns=time.monotonic_ns())
                    index.write(json.dumps(row, ensure_ascii=False, allow_nan=False) + '\n')
                    with self.lock:
                        self.saved += 1
        except BaseException as exc:
            self.error = f'{type(exc).__name__}: {exc}'
            self.state.fail('frame_write_failed:' + self.error)

    def snapshot(self) -> dict:
        with self.lock:
            elapsed_ns = self.last_ns - self.first_ns if self.last_ns is not None else 0
            return dict(received=self.received, enqueued=self.enqueued, saved=self.saved,
                        dropped=self.dropped, pending=self.queue.qsize(), queue_high_water=self.high_water,
                        accepting=self.accepting, callbacks_rejected_after_seal=self.after_seal,
                        first_callback_monotonic_ns=self.first_ns, last_callback_monotonic_ns=self.last_ns,
                        max_callback_interval_sec=self.max_interval_ns / 1e9,
                        actual_callback_fps=(self.received - 1) * 1e9 / elapsed_ns if elapsed_ns > 0 else 0.,
                        native_shape=list(self.shape) if self.shape else None, error=self.error)

    def seal(self) -> None:
        # Serializes with the entire offer, so no queue put can follow sealing.
        with self.lock:
            self.accepting = False

    def close(self, timeout=30.) -> dict:
        self.seal()
        self.done.set()
        self.thread.join(timeout)
        if self.thread.is_alive():
            self.error = 'frame_drain_timeout'
            self.state.fail(self.error)
        result = self.snapshot()
        result['writer_stopped'] = not self.thread.is_alive()
        result['complete'] = (not self.error and self.received > 0 and self.received == self.saved
                              and not self.dropped and self.queue.empty())
        return result
