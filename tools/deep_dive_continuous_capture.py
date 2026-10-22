"""Standalone WGC + input recorder; never imports a deep-dive recognition action.

All commands are explicit. Starting a recording does not focus, click or reset.
See docs/developer-guide/deep-dive-continuous-capture.md before live use.
"""
from __future__ import annotations

import argparse
from dataclasses import replace
from datetime import datetime, timezone
import hashlib
import json
import math
import os
from pathlib import Path
import shutil
import signal
import sys
import threading
import time
import uuid

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from tools._deep_dive_capture_store import FrameStore, RunState, Timeline, atomic_json


def session_path(value: str) -> Path:
    path = (ROOT / value).resolve()
    allowed = (ROOT / 'logs' / 'deep_dive_capture', ROOT / '.pytest_tmp')
    if not any(path.is_relative_to(parent.resolve()) and path != parent.resolve() for parent in allowed):
        raise ValueError('Session must be below logs/deep_dive_capture/ or .pytest_tmp/ inside this repository')
    return path


def number(value, low: float, high: float) -> float:
    if isinstance(value, bool) or not isinstance(value, (int, float)) or not math.isfinite(value) or not low <= value <= high:
        raise ValueError(f'Expected finite number in [{low}, {high}]')
    return float(value)


def integer(value, low: int, high: int) -> int:
    if type(value) is not int or not low <= value <= high:
        raise ValueError(f'Expected integer in [{low}, {high}]')
    return value


def make_backend(target, options: dict, store: FrameStore, timeline: Timeline, state: RunState,
                 geometry: dict, max_gap: float):
    """Tool-local callback override; production capture/recognition code is untouched."""
    import numpy as np
    from plans.aura_base.src.platform.windows.capture_backends import WindowsWgcCaptureBackend
    from plans.aura_base.src.platform.windows.wgc_session import PersistentWgcSession

    class RecordingSession(PersistentWgcSession):
        def _register_callbacks(self, capturer):
            @capturer.event
            def on_frame_arrived(frame, control):
                # Serialize acceptance with close, including buffer ownership and enqueue.
                with self._lock:
                    if getattr(self, '_recorder_closing', False):
                        return
                    self.record_frame(frame)

            @capturer.event
            def on_closed():
                with self._lock:
                    self._closed = True
                    self._frame_event.set()
                if not getattr(self, '_recorder_closing', False):
                    state.fail('capture_closed_unexpectedly')

        def record_frame(self, frame):
            arrived_ns = time.monotonic_ns()
            try:
                array = np.asarray(getattr(frame, 'frame_buffer', frame))
                if array.dtype != np.uint8 or array.ndim != 3 or array.shape[2] not in (3, 4):
                    raise ValueError('Expected native uint8 BGR/BGRA frame')
                # Own the bytes before the native callback returns/reuses its buffer.
                native = array.copy()
                height, width = native.shape[:2]
                if (width, height) == (1280, 720):
                    roi = [0, 0, 1280, 720]
                else:
                    bounds = next((b for b in geometry['capture_bounds_candidates']
                                   if b[2:] == [width, height]), None)
                    if bounds is None:
                        raise ValueError('Native bounds cannot be mapped to the frozen client rectangle')
                    client = geometry['client_rect_screen']
                    roi = [client[0] - bounds[0], client[1] - bounds[1], 1280, 720]
                    if roi[0] < 0 or roi[1] < 0 or roi[0] + 1280 > width or roi[1] + 720 > height:
                        raise ValueError('Client crop outside native frame')
                previous_ns = getattr(self, '_previous_callback_ns', None)
                self._previous_callback_ns = arrived_ns
                self._generation += 1
                generation = self._generation
                self._latest_frame = native
                self._frame_shape = tuple(native.shape)
                self._latest_frame_dtype = native.dtype
                self._arrived_at_monotonic = arrived_ns / 1e9
                self._last_error = None
                self._frame_event.set()
                store.offer(native, dict(session_id=str(self.stream_id), generation=generation,
                    callback_monotonic_ns=arrived_ns, client_roi=roi,
                    callback_interval_sec=None if previous_ns is None else (arrived_ns - previous_ns) / 1e9))
                if previous_ns is not None and arrived_ns - previous_ns > max_gap * 1e9:
                    state.fail('callback_gap_exceeded')
            except BaseException as exc:
                reason = f'capture_callback_failed:{type(exc).__name__}:{exc}'
                self._set_last_error('recorder_callback_failed', reason)
                state.fail(reason)

        def close(self):
            # Unlike the production helper, report native close failures to the run.
            with self._lock:
                self._recorder_closing = True
                control = self._control
                self._closed = True
                timeline.emit(dict(event='capture_acceptance_closed', session_id=str(self.stream_id),
                                   last_generation=self._generation))
                # An optional-profile fallback before the first callback may
                # create another native session; it has no recording to seal.
                if store.received:
                    store.seal()
            errors = []
            if control is not None:
                try:
                    control.stop()
                except Exception as exc:
                    errors.append(f'stop:{exc}')
                try:
                    control.wait()
                except Exception as exc:
                    errors.append(f'wait:{exc}')
            if errors:
                raise RuntimeError('; '.join(errors))
            with self._lock:
                self._control = self._capturer = self._latest_frame = None
                self._frame_event.set()

    class RecordingBackend(WindowsWgcCaptureBackend):
        def close(self):
            with self._session_lock:
                if self._session is not None:
                    self._session.close()
                    self._session = None

        def _create_session(self, hwnd):
            compatible = self.effective_profile == 'compatible'
            session = RecordingSession(hwnd=int(hwnd), module_name=self.module_name,
                capture_cursor=False, draw_border=None if compatible else self.draw_border,
                secondary_window=None if compatible else False,
                minimum_update_interval_ms=None if compatible else self.minimum_update_interval_ms,
                dirty_region=None if compatible else False)
            if store.received:
                state.fail('capture_session_replaced_after_recording_started')
                raise RuntimeError('Capture continuity cannot survive a session replacement')
            timeline.emit(dict(event='capture_session_created', session_id=str(session.stream_id),
                               profile=self.effective_profile))
            return session

    return RecordingBackend(target, options)


class Controls:
    def __init__(self, target, backend, folder, state, timeline, store, geometry, args, observer, run_id):
        self.target, self.backend, self.folder = target, backend, folder
        self.state, self.timeline, self.store = state, timeline, store
        self.geometry, self.args = geometry, args
        self.deadline = time.monotonic() + args.duration
        self.pressed = False
        self.command_id = None
        self.observer, self.run_id = observer, run_id
        self.last_health = 0.
        self.foreground = None

    def check(self, *, foreground=False):
        if (self.folder / 'STOP.json').exists():
            self.timeline.emit(dict(event='stop_file_observed'))
            self.state.stop.set()
        if self.state.stop.is_set():
            raise InterruptedError('recording_stop_requested')
        if time.monotonic() >= self.deadline:
            self.state.stop.set()
            raise InterruptedError('duration_reached')
        self.target.ensure_valid()
        if (list(self.target.get_client_rect_screen()) != self.geometry['client_rect_screen']
                or list(self.target.get_window_rect_screen()) != self.geometry['window_rect_screen']):
            raise RuntimeError('target_geometry_changed')
        if foreground and not self.target.is_foreground():
            raise RuntimeError('target_not_foreground')
        snapshot = self.store.snapshot()
        last = snapshot['last_callback_monotonic_ns']
        if last is not None and time.monotonic_ns() - last > self.args.stall_sec * 1e9:
            raise RuntimeError('capture_stalled')
        if time.monotonic() - self.last_health >= .5:
            observer_status = self.observer.summary()
            if not observer_status['ready'] or not observer_status['thread_alive']:
                raise RuntimeError('input_observer_not_live')
            current_foreground = self.target.is_foreground()
            if current_foreground != self.foreground:
                self.timeline.emit(dict(event='target_foreground_changed', foreground=current_foreground))
                self.foreground = current_foreground
            if not self.pressed:
                # Never perform status-file writes or disk probes while holding the mouse.
                if shutil.disk_usage(self.folder).free < self.args.min_free_gib * 1024 ** 3:
                    raise OSError('disk_space_below_reserve')
                atomic_json(self.folder / 'status.json', dict(status='recording', run_id=self.run_id, pid=os.getpid(),
                    heartbeat_monotonic_ns=time.monotonic_ns(), frames=snapshot,
                    observer=observer_status, errors=self.state.errors))
                self.last_health = time.monotonic()

    def wait(self, seconds, *, foreground=False):
        until = time.monotonic() + seconds
        while time.monotonic() < until:
            self.check(foreground=foreground)
            self.state.stop.wait(min(.01, max(0., until - time.monotonic())))

    def call(self, operation, callback, **parameters):
        # Explicit zero-duration moves ensure hidden backend interpolation is not omitted.
        self.timeline.emit(dict(event='input_call_begin', command_id=self.command_id,
                                operation=operation, parameters=parameters))
        try:
            result = callback()
        except BaseException as exc:
            self.timeline.emit(dict(event='input_call_end', command_id=self.command_id,
                operation=operation, success=False, error=f'{type(exc).__name__}:{exc}'))
            raise
        self.timeline.emit(dict(event='input_call_end', command_id=self.command_id,
                                operation=operation, success=True, result=result))
        return result

    def release(self):
        if self.pressed:
            try:
                self.call('mouse_up', lambda: self.backend.mouse_up(button='left'), button='left')
            except BaseException as exc:
                self.state.fail('explicit_mouse_up_failed:' + str(exc))
        result = self.call('release_all', self.backend.release_all)
        self.pressed = False
        if not isinstance(result, dict) or result.get('success') is not True:
            self.state.fail('input_release_not_confirmed')
        return result

    def execute(self, command):
        self.check()
        op = command.get('op')
        label = command.get('label', '')
        if not isinstance(label, str) or len(label) > 256:
            raise ValueError('label must be a string of at most 256 characters')
        if op == 'mark':
            self.timeline.emit(dict(event='annotation', command_id=self.command_id, label=label))
        elif op == 'wait':
            self.wait(number(command.get('seconds', 3.), 0., 60.))
        elif op in ('focus', 'drag', 'click'):
            if not self.args.enable_input:
                raise ValueError('Input requires record --enable-input')
            if op == 'focus':
                if not self.call('focus', self.target.focus):
                    raise RuntimeError('focus_failed')
                return
            x, y = integer(command.get('x', 640), 0, 1279), integer(command.get('y', 480), 0, 719)
            duration = number(command.get('duration', .5), .1, 1.)
            after = number(command.get('after', 3.), 0., 15.)
            dy = integer(command.get('dy', 0), -160, 160)
            if op == 'drag' and (not dy or not 0 <= y + dy < 720):
                raise ValueError('Nonzero vertical drag must stay inside the client')
            self.check(foreground=True)
            import win32api
            if any(win32api.GetAsyncKeyState(vk) & 0x8000 for vk in (1, 2, 4, 16, 17, 18)):
                raise ValueError('Release physical mouse buttons and Shift/Ctrl/Alt before tool input')
            if self.backend._held_keys or self.backend._held_mouse_buttons:
                raise RuntimeError('unexpected_held_input')
            self.call('move_to', lambda: self.backend.move_to(x, y, duration=0.), x=x, y=y)
            self.wait(.08, foreground=True)
            try:
                # Set before attempting down so finally releases even if sending raises.
                self.pressed = True
                self.call('mouse_down', lambda: self.backend.mouse_down(button='left'), button='left')
                self.wait(.08, foreground=True)
                if op == 'drag':
                    steps = max(1, math.ceil(duration / .02))
                    started = time.monotonic()
                    for step in range(1, steps + 1):
                        self.wait(max(0., started + duration * step / steps - time.monotonic()), foreground=True)
                        self.check(foreground=True)
                        target_y = y + round(dy * step / steps)
                        self.call('move_to', lambda: self.backend.move_to(x, target_y, duration=0.),
                                  x=x, y=target_y, step=step, steps=steps)
            finally:
                self.release()
            self.wait(after)
        else:
            raise ValueError('Unsupported operation')


def record(args) -> int:
    # Imports here keep send/stop/status independent of Windows, WGC and inference.
    from tools._shared import build_overlay_config
    from plans.aura_base.src.platform.runtime_config import resolve_runtime_config, RuntimeVisibilityRecoveryConfig
    from plans.aura_base.src.platform.windows.dpi import ensure_process_dpi_awareness
    from plans.aura_base.src.platform.windows.window_target import WindowTarget
    from plans.aura_base.src.platform.windows.capture_backends import _get_dwm_extended_frame_bounds
    from plans.aura_base.src.platform.windows.input_backends import build_input_backend
    from tools._deep_dive_capture_input import InputObserver
    import win32api
    import win32event
    import winerror

    folder = session_path(args.session)
    folder.mkdir(parents=True, exist_ok=False)
    for name in ('commands', 'results', 'temp'):
        (folder / name).mkdir()
    for name in ('TEMP', 'TMP', 'TMPDIR'):
        os.environ[name] = str(folder / 'temp')
    state = RunState()
    timeline = store = None
    target = capture = input_backend = observer = controls = None
    mutex = None
    cleanup = {}
    reason = 'startup_failed'
    ready = False
    old_signals = {}
    started_ns = time.monotonic_ns()
    run_id = uuid.uuid4().hex
    try:
        atomic_json(folder / 'owner.json', dict(run_id=run_id, pid=os.getpid(),
                    started_monotonic_ns=started_ns))
        atomic_json(folder / 'status.json', dict(status='starting', run_id=run_id, pid=os.getpid(),
                    heartbeat_monotonic_ns=time.monotonic_ns()))
        timeline = Timeline(folder, state)
        store = FrameStore(folder, state, timeline, args.queue_frames)
        def stop_signal(signum, _frame):
            timeline.emit(dict(event='stop_signal', signal=signum))
            state.stop.set()
        for sig in (signal.SIGINT, signal.SIGTERM):
            old_signals[sig] = signal.signal(sig, stop_signal)
        dpi = ensure_process_dpi_awareness()
        if not dpi.get('ok'):
            raise RuntimeError('DPI awareness unavailable; native coordinate contract cannot be established')
        resolved = resolve_runtime_config(build_overlay_config(plan_name='resonance_pc'))
        config = replace(resolved.target, require_visible=True, require_foreground=False,
                         visibility_recovery=RuntimeVisibilityRecoveryConfig(enabled=False))
        if args.hwnd is not None:
            config = replace(config, mode='hwnd', hwnd=args.hwnd)
        target = WindowTarget.create(config)
        candidate_mutex = win32event.CreateMutex(None, True, f'Local\\AuraDeepDiveCapture_{target.hwnd}')
        if win32api.GetLastError() == winerror.ERROR_ALREADY_EXISTS:
            win32api.CloseHandle(candidate_mutex)
            raise RuntimeError('Another standalone recorder already owns this target')
        mutex = candidate_mutex
        client = list(target.get_client_rect_screen())
        if client[2:] != [1280, 720]:
            raise ValueError('Prepare a 1280x720 client; recorder never resizes the game')
        window = list(target.get_window_rect_screen())
        dwm = _get_dwm_extended_frame_bounds(target.hwnd)
        geometry = dict(client_rect_screen=client, window_rect_screen=window,
                        capture_bounds_candidates=[list(dwm), window] if dwm else [window])
        if shutil.disk_usage(folder).free < args.min_free_gib * 1024 ** 3:
            raise OSError('Insufficient free disk space')
        options = resolved.capture.provider_options('windows')
        options.update(minimum_update_interval_ms=math.ceil(1000 / args.fps), dirty_region=False,
                       capture_cursor=False)
        input_options = resolved.input.provider_options('windows')
        input_options['focus_before_input'] = False
        input_backend = build_input_backend('sendinput', target, input_options)
        source_paths = [Path(__file__).resolve(), ROOT / 'tools/_deep_dive_capture_store.py',
                        ROOT / 'tools/_deep_dive_capture_input.py', ROOT / 'plans/resonance_pc/config.yaml',
                        ROOT / 'plans/aura_base/src/platform/windows/wgc_session.py',
                        ROOT / 'plans/aura_base/src/platform/windows/capture_backends.py',
                        ROOT / 'plans/aura_base/src/platform/windows/input_backends.py']
        source_bytes = {str(p.relative_to(ROOT)): p.read_bytes() for p in source_paths}
        for name, payload in source_bytes.items():
            snapshot = folder / 'source' / name
            snapshot.parent.mkdir(parents=True, exist_ok=True)
            snapshot.write_bytes(payload)
        atomic_json(folder / 'manifest.json', dict(schema='resonance_pc.deep_dive_capture.v1', run_id=run_id,
            pid=os.getpid(), created_utc=datetime.now(timezone.utc).isoformat(), clock='time.monotonic_ns',
            started_monotonic_ns=started_ns, target=target.to_summary(), geometry=geometry, dpi=dpi,
            requested_fps=args.fps, duration_limit_sec=args.duration, input_enabled=args.enable_input,
            queue_capacity_frames=args.queue_frames, max_callback_gap_sec=args.max_gap,
            capture_options=options, input_backend='sendinput', input_options=input_options,
            timestamp_semantics='WGC callback arrival and API/hook observation; not game render/processing time',
            native_png='Unscaled, uncropped BGR/BGRA pixels; client_roi is separately indexed',
            source_sha256={name: hashlib.sha256(payload).hexdigest() for name, payload in source_bytes.items()}))
        observer = InputObserver(target.hwnd, timeline.emit, state.fail)
        observer.start()
        capture = make_backend(target, options, store, timeline, state, geometry, args.max_gap)
        timeline.emit(dict(event='recording_start', run_id=run_id))
        # Warm-up only: no geometry, detector, classifier or input is called here.
        capture.capture()
        if not store.first.wait(3.) or state.stop.is_set():
            raise RuntimeError('recording_not_ready')
        timeline.emit(dict(event='capture_ready', requested_profile=capture.requested_profile,
            effective_profile=capture.effective_profile,
            compatibility_fallback_used=capture.compatibility_fallback_used,
            compatibility_fallback_reason=capture.compatibility_fallback_reason,
            minimum_update_interval_ms=None if capture.effective_profile == 'compatible'
            else capture.minimum_update_interval_ms))
        controls = Controls(target, input_backend, folder, state, timeline, store, geometry, args, observer, run_id)
        controls.check()  # Publish the first heartbeat before exposing ready.json.
        atomic_json(folder / 'ready.json', dict(ready=True, run_id=run_id, pid=os.getpid(), hwnd=target.hwnd,
                    monotonic_ns=time.monotonic_ns(), input_enabled=args.enable_input))
        ready = True
        print(json.dumps(dict(status='recording', session=str(folder), pid=os.getpid()), ensure_ascii=False), flush=True)
        processed = set()
        while not state.stop.is_set():
            controls.check()
            pending = [p for p in sorted((folder / 'commands').glob('*.json')) if p.name not in processed]
            if not pending:
                state.stop.wait(.025)
                continue
            path = pending[0]
            processed.add(path.name)
            controls.command_id = path.stem
            try:
                if path.stat().st_size > 16384:
                    raise ValueError('Command too large')
                command = json.loads(path.read_text(encoding='utf-8'))
                if not isinstance(command, dict):
                    raise ValueError('Command must be an object')
                timeline.emit(dict(event='command_begin', command_id=path.stem, command=command))
                controls.execute(command)
                result = dict(status='success', command_id=path.stem)
            except (ValueError, json.JSONDecodeError) as exc:
                result = dict(status='rejected', command_id=path.stem, error=str(exc))
            except InterruptedError as exc:
                result = dict(status='interrupted', command_id=path.stem, error=str(exc))
                state.stop.set()
            except Exception as exc:
                result = dict(status='failed', command_id=path.stem, error=f'{type(exc).__name__}:{exc}')
                state.fail(result['error'])
            timeline.emit(dict(event='command_end', **result))
            atomic_json(folder / 'results' / path.name, result)
        reason = 'stop_requested'
    except InterruptedError as exc:
        reason = str(exc)
    except BaseException as exc:
        reason = f'{type(exc).__name__}:{exc}'
        state.fail(reason)
    finally:
        state.stop.set()
        if input_backend is not None:
            try:
                if controls is not None:
                    cleanup['input_release'] = controls.release()
                else:
                    cleanup['input_release'] = input_backend.release_all()
                    if cleanup['input_release'].get('success') is not True:
                        state.fail('cleanup_input_release_not_confirmed')
            except BaseException as exc:
                cleanup['input_release_error'] = str(exc)
                state.fail('cleanup_input_release_failed:' + str(exc))
        if capture is not None:
            close_errors = []
            def close_capture():
                try:
                    capture.close()
                except BaseException as exc:
                    close_errors.append(f'{type(exc).__name__}:{exc}')
            closer = threading.Thread(target=close_capture, name='deep-dive-capture-close', daemon=True)
            closer.start()
            closer.join(10.)
            cleanup['capture_closed'] = not closer.is_alive() and not close_errors
            cleanup['native_close_thread_stopped'] = not closer.is_alive()
            if not cleanup['capture_closed']:
                state.fail('cleanup_capture_close_unconfirmed:' + ';'.join(close_errors))
        cleanup['frames'] = store.close() if store is not None else dict(complete=False, received=0, saved=0)
        if observer is not None:
            try:
                observer.close()
            except BaseException as exc:
                state.fail('cleanup_input_observer_failed:' + str(exc))
            cleanup['observer'] = observer.summary()
        if mutex is not None:
            try:
                win32event.ReleaseMutex(mutex)
                win32api.CloseHandle(mutex)
                cleanup['target_mutex_released'] = True
            except BaseException as exc:
                state.fail('cleanup_mutex_failed:' + str(exc))
        if timeline is not None:
            timeline.emit(dict(event='recording_end', reason=reason, ready_was_reached=ready))
        cleanup['timeline'] = timeline.close() if timeline is not None else dict(complete=False)
        complete = (ready and not state.errors and cleanup['frames']['complete']
                    and cleanup['timeline']['complete'] and cleanup.get('capture_closed') is True
                    and cleanup.get('observer', {}).get('closed') is True
                    and cleanup.get('observer', {}).get('error_count') == 0)
        result = dict(status='closed' if complete else 'incomplete', recording_complete=complete,
                      formal_scan_success=False, run_id=run_id, pid=os.getpid(), reason=reason,
                      stopped_monotonic_ns=time.monotonic_ns(), errors=state.errors, cleanup=cleanup)
        atomic_json(folder / 'summary.json', result)
        atomic_json(folder / 'status.json', result)
        for sig, previous in old_signals.items():
            signal.signal(sig, previous)
    print(json.dumps(dict(status=result['status'], session=str(folder), frames=result['cleanup']['frames']),
                     ensure_ascii=False), flush=True)
    return 0 if result['recording_complete'] else 1


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    sub = parser.add_subparsers(dest='mode', required=True)
    live = sub.add_parser('record', help='Start a standalone live recording, without automatic input')
    live.add_argument('--session', required=True)
    live.add_argument('--hwnd', type=int)
    live.add_argument('--enable-input', action='store_true')
    live.add_argument('--fps', type=float, default=30.)
    live.add_argument('--duration', type=float, default=600.)
    live.add_argument('--queue-frames', type=int, default=64)
    live.add_argument('--max-gap', type=float, default=.25)
    live.add_argument('--stall-sec', type=float, default=3.)
    live.add_argument('--min-free-gib', type=float, default=2.)
    send = sub.add_parser('send', help='Atomically publish one command; inspect results/<id>.json for completion')
    send.add_argument('--session', required=True)
    send.add_argument('--op', required=True, choices=('focus', 'mark', 'wait', 'drag', 'click'))
    send.add_argument('--label', default='')
    send.add_argument('--x', type=int, default=640)
    send.add_argument('--y', type=int, default=480)
    send.add_argument('--dy', type=int, default=0)
    send.add_argument('--duration', type=float, default=.5)
    send.add_argument('--after', type=float, default=3.)
    send.add_argument('--seconds', type=float, default=3.)
    for action in ('stop', 'status'):
        command = sub.add_parser(action)
        command.add_argument('--session', required=True)
    args = parser.parse_args()
    if args.mode == 'record':
        number(args.fps, 1., 60.)
        number(args.duration, 5., 2400.)
        integer(args.queue_frames, 4, 256)
        number(args.max_gap, .05, 5.)
        number(args.stall_sec, .5, 15.)
        number(args.min_free_gib, 1., 1024.)
        return record(args)
    folder = session_path(args.session)
    if args.mode == 'status':
        for name in ('summary.json', 'status.json', 'ready.json'):
            if (folder / name).exists():
                print((folder / name).read_text(encoding='utf-8'))
                return 0
        raise FileNotFoundError('Session has no ready/status/summary record')
    if not (folder / 'owner.json').exists() or (folder / 'summary.json').exists():
        raise ValueError('Session has no owner or is already closed')
    command = dict(op='stop' if args.mode == 'stop' else args.op,
                   submitted_monotonic_ns=time.monotonic_ns())
    if args.mode == 'stop':
        atomic_json(folder / 'STOP.json', command)
        print(json.dumps(dict(status='stop_requested', session=str(folder))))
    else:
        if not (folder / 'ready.json').exists():
            raise ValueError('Recorder has not become ready')
        status = json.loads((folder / 'status.json').read_text(encoding='utf-8'))
        if time.monotonic_ns() - status.get('heartbeat_monotonic_ns', 0) > 10 * 1e9:
            raise ValueError('Recorder heartbeat is stale; command was not published')
        command.update({key: getattr(args, key) for key in ('label', 'x', 'y', 'dy', 'duration', 'after', 'seconds')})
        name = f'{time.monotonic_ns():020d}-{uuid.uuid4().hex[:8]}.json'
        atomic_json(folder / 'commands' / name, command)
        print(json.dumps(dict(status='queued', command_id=Path(name).stem,
                              result=str(folder / 'results' / name)), ensure_ascii=False))
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
