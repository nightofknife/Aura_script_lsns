"""Input evidence is exercised against fakes; no game or native input is used."""
from types import SimpleNamespace
import asyncio
import threading
import time

import pytest
from PIL import Image

from packages.aura_core.observability import runtime_trace
from packages.aura_core.observability.logging.core_logger import set_cid, reset_cid
from plans.aura_base.src.platform.contracts import TargetRuntimeError
from plans.aura_base.src.platform.windows import input_backends as inputs
from plans.aura_base.src.platform.windows import window_target as windows
from plans.aura_base.src.services import target_runtime_service as runtimes
from plans.resonance_pc.src.services import input_bridge_service as bridges


@pytest.fixture(autouse=True)
def no_native_diagnostics(monkeypatch):
    # Failures must request evidence, but tests never inspect the real desktop.
    monkeypatch.setattr(runtime_trace, "request_diagnostic", lambda *a, **kw: None)
    monkeypatch.setattr(inputs, "request_diagnostic", lambda *a, **kw: None)
    monkeypatch.setattr(windows, "request_diagnostic", lambda *a, **kw: None)


def test_failed_release_remains_held_and_retry_is_recorded(monkeypatch):
    backend = inputs.WindowsSendInputBackend(SimpleNamespace())
    backend._held_mouse_buttons.update({"left", "right"})
    backend._held_keys.add(65)
    events = []
    monkeypatch.setattr(inputs, "log_event", lambda event, **kw: events.append((event, kw)))
    def mouse(*, flags):
        if flags == inputs.win32con.MOUSEEVENTF_LEFTUP:
            raise TargetRuntimeError("windows_input_failed", "fake failure")
    monkeypatch.setattr(inputs, "_send_mouse_input", mouse)
    monkeypatch.setattr(inputs, "_send_key_input", lambda *a, **kw: None)
    report = backend.release_all()
    assert not report["success"]
    assert report["remaining_mouse_buttons"] == ["left"]
    assert report["remaining_keys"] == []
    assert any(e == "input.release_item" and not p["success"] for e, p in events)
    monkeypatch.setattr(inputs, "_send_mouse_input", lambda **kw: None)
    assert backend.release_all()["success"]
    assert not backend._held_mouse_buttons


def test_click_up_failure_retains_down_for_final_release(monkeypatch):
    backend = inputs.WindowsSendInputBackend(SimpleNamespace())
    monkeypatch.setattr(backend, "_ensure_integrity_compatible", lambda: None)
    monkeypatch.setattr(backend, "_ensure_focus", lambda: None)
    monkeypatch.setattr(backend, "_resolve_point", lambda *a: (12, 23))
    monkeypatch.setattr(backend, "move_to", lambda *a, **kw: None)
    def send(*, flags):
        if flags == inputs.win32con.MOUSEEVENTF_LEFTUP:
            raise RuntimeError("fake up failure")
    monkeypatch.setattr(inputs, "_send_mouse_input", send)
    with pytest.raises(RuntimeError):
        backend.click(12, 23)
    assert backend._held_mouse_buttons == {"left"}


def test_native_edges_share_operation_and_unicode_is_redacted(monkeypatch):
    monkeypatch.setattr(inputs.ctypes.windll.user32, "SendInput", lambda count, *a: count)
    with runtime_trace.activity("input", "type_text", cid="edge-evidence") as operation:
        inputs._send_mouse_input(flags=inputs.win32con.MOUSEEVENTF_LEFTDOWN)
        inputs._send_key_input(65, key_up=True)
        inputs._send_unicode_char("密")
    with runtime_trace._lock:
        events = [e for e in runtime_trace._events if e["activity_id"] == operation and e["event"] == "input.edge"]
    assert len(events) == 4
    assert "密" not in str(events)
    assert all(e["success"] for e in events)


def test_window_message_char_does_not_log_text_or_codepoint(monkeypatch):
    monkeypatch.setattr(inputs.win32gui, "SendMessage", lambda *a: 0)
    events = []
    monkeypatch.setattr(inputs, "log_event", lambda event, **kw: events.append((event, kw)))
    inputs._send_window_message(123, inputs.win32con.WM_CHAR, ord("密"), 0)
    assert events[0][1]["success"]
    assert "wparam" not in events[0][1]
    assert str(ord("密")) not in str(events)


def test_detach_failure_is_logged_without_changing_focus_fallback(monkeypatch):
    target = windows.WindowTarget.__new__(windows.WindowTarget)
    target.binding = SimpleNamespace(hwnd=111, pid=222)
    monkeypatch.setattr(target, "ensure_alive", lambda: None)
    foreground = iter([False, True])
    monkeypatch.setattr(target, "is_foreground", lambda: next(foreground))
    monkeypatch.setattr(windows.win32gui, "IsIconic", lambda hwnd: False)
    monkeypatch.setattr(windows.win32gui, "ShowWindow", lambda *a: None)
    monkeypatch.setattr(windows.win32gui, "GetForegroundWindow", lambda: 333)
    monkeypatch.setattr(windows.win32api, "GetCurrentThreadId", lambda: 10)
    monkeypatch.setattr(windows.win32process, "GetWindowThreadProcessId", lambda hwnd: (20, 222))
    for name in ("BringWindowToTop", "SetForegroundWindow", "SetActiveWindow"):
        monkeypatch.setattr(windows.win32gui, name, lambda *a: None)
    def attach(a, b, enable):
        if not enable:
            raise RuntimeError("fake detach failure")
    monkeypatch.setattr(windows.win32process, "AttachThreadInput", attach)
    monkeypatch.setattr(windows.time, "sleep", lambda _: None)
    events = []
    monkeypatch.setattr(windows, "log_event", lambda event, **kw: events.append((event, kw)))
    assert target.focus()
    assert [(name, data["success"]) for name, data in events[:2]] == [
        ("input.focus_attach", True), ("input.focus_detach", False)]
    assert events[1][1]["target_pid"] == 222


def fake_runtime(monkeypatch, *, frame_age=0.1):
    runtime = runtimes.TargetRuntimeService.__new__(runtimes.TargetRuntimeService)
    runtime._lock = threading.RLock()
    runtime._trace_cids = {"owned-cid"}
    runtime._trace_runtime = {"plan": "resonance_pc", "provider": "windows"}
    def forbidden(*a, **kw):
        raise AssertionError("Diagnostic must not bind, capture, or focus")
    runtime._get_or_create_session = forbidden
    runtime._session = SimpleNamespace(
        capture=forbidden, focus=forbidden,
        input_backend=SimpleNamespace(_held_mouse_buttons={"left"}, _held_keys={65}),
        capture_backend=SimpleNamespace(capture_stream_frame=lambda: {
            "capture": SimpleNamespace(image=Image.new("RGB", (4, 3))), "generation": 5,
            "arrived_at_monotonic": time.monotonic() - frame_age}),
    )
    monkeypatch.setattr(windows.win32gui, "GetForegroundWindow", lambda: 123)
    monkeypatch.setattr(windows.win32process, "GetWindowThreadProcessId", lambda _: (3, 4))
    monkeypatch.setattr(windows.win32api, "GetCursorPos", lambda: (10, 20))
    monkeypatch.setattr(windows.win32gui, "WindowFromPoint", lambda _: 456)
    return runtime


def test_diagnostic_only_uses_owned_fresh_cached_frame(monkeypatch):
    runtime = fake_runtime(monkeypatch)
    assert runtime._diagnostic_snapshot({"cid": "other"})["status"] == "unavailable"
    assert runtime._diagnostic_snapshot({"cid": "owned-cid", "runtime": {"plan": "other"}})["status"] == "unavailable"
    result = runtime._diagnostic_snapshot({"cid": "owned-cid", "runtime": {"plan": "resonance_pc"}})
    assert result["png"].startswith(b"\x89PNG")
    assert result["held_mouse_buttons"] == ["left"]
    assert result["window_at_cursor"] == 456


def test_stale_capture_cache_is_not_saved(monkeypatch):
    runtime = fake_runtime(monkeypatch, frame_age=10)
    result = runtime._diagnostic_snapshot({"cid": "owned-cid"})
    assert result["screenshot_status"] == "cache_stale"
    assert "png" not in result


def test_diagnostic_returns_promptly_when_runtime_lock_busy(monkeypatch):
    runtime = fake_runtime(monkeypatch)
    held, release = threading.Event(), threading.Event()
    def hold():
        with runtime._lock:
            held.set()
            release.wait(2)
    thread = threading.Thread(target=hold)
    thread.start()
    try:
        assert held.wait(1)
        assert runtime._diagnostic_snapshot({"cid": "owned-cid"}) == {"status": "busy"}
    finally:
        release.set()
        thread.join(2)


def test_diagnostic_native_read_does_not_hold_runtime_lock(monkeypatch):
    runtime = fake_runtime(monkeypatch)
    entered, release = threading.Event(), threading.Event()
    def foreground():
        entered.set()
        release.wait(2)
        return 123
    monkeypatch.setattr(windows.win32gui, "GetForegroundWindow", foreground)
    thread = threading.Thread(target=lambda: runtime._diagnostic_snapshot({"cid": "owned-cid"}))
    thread.start()
    try:
        assert entered.wait(1)
        acquired = runtime._lock.acquire(blocking=False)
        assert acquired
        if acquired:
            runtime._lock.release()
    finally:
        release.set()
        thread.join(2)


def test_binding_reports_actual_backend_and_accepts_root_cid(monkeypatch):
    runtime = runtimes.TargetRuntimeService({})
    session = SimpleNamespace(target=SimpleNamespace(binding=SimpleNamespace(pid=222, hwnd=111)),
                              input_backend=SimpleNamespace(backend_name="sendinput", focus_before_input=False))
    resolved = SimpleNamespace(provider="windows", capture=SimpleNamespace(backend="wgc"),
                               input=SimpleNamespace(backend="window_message", focus_before_input=True,
                                                     activation=SimpleNamespace(to_dict=lambda: {"mode": "focus_sleep"})))
    monkeypatch.setattr(runtime, "_build_session_key", lambda value: "one")
    monkeypatch.setattr(runtime, "_build_session", lambda value: session)
    monkeypatch.setattr(runtimes, "current_cid", lambda: "child-cid")
    monkeypatch.setattr(runtimes, "current_root_cid", lambda: "root-cid")
    assert runtime._get_or_create_session(resolved=resolved) is session
    assert runtime._trace_cids == {"child-cid", "root-cid"}
    assert runtime._trace_runtime["input_backend"] == "sendinput"
    assert not runtime._trace_runtime["focus_before_input"]
    assert runtime._trace_runtime["target_pid"] == 222


def test_text_summary_never_contains_typed_content():
    assert runtimes.TargetRuntimeService._trace_parameters("type_text", ("secret正文", 0.1), {}) == {
        "text_length": 8, "interval": 0.1}


def test_bridge_release_failure_keeps_held_state(monkeypatch):
    service = bridges.ResonancePcInputBridgeService(SimpleNamespace(target_runtime=SimpleNamespace(config={})))
    service._held.add("left")
    def fail(*a, **kw):
        raise TargetRuntimeError("input_bridge_unavailable", "fake")
    service._client = SimpleNamespace(request=fail)
    with pytest.raises(TargetRuntimeError):
        service.release_all()
    assert service._held == {"left"}


def test_bridge_actual_mode_includes_override_and_deduplicates(monkeypatch):
    monkeypatch.setenv("AURA_GUI_INPUT_BRIDGE", "0")
    events = []
    monkeypatch.setattr(bridges, "log_event", lambda event, **kw: events.append((event, kw)))
    service = bridges.ResonancePcInputBridgeService(SimpleNamespace(
        target_runtime=SimpleNamespace(config={"resonance_pc.input.mode": "bridge"})))
    assert not service.enabled
    assert not service.enabled
    assert len(events) == 1
    assert events[0][1] == {"input_mode": "system", "configured_input_mode": "bridge",
                             "gui_bridge_override": "0", "bridge_enabled": False}


def test_bridge_worker_is_tracked_until_cancel_cleanup_finishes(monkeypatch):
    service = bridges.ResonancePcInputBridgeService(SimpleNamespace(target_runtime=SimpleNamespace(config={})))
    entered, release = threading.Event(), threading.Event()
    def work():
        entered.set()
        release.wait(2)
    monkeypatch.setattr(service, "cancel_input", release.set)
    async def scenario():
        token = set_cid("bridge-worker-test")
        try:
            task = asyncio.create_task(service.run_async(work))
            try:
                assert await asyncio.to_thread(entered.wait, 1)
                assert any(row["kind"] == "worker" for row in runtime_trace.pending_activities("bridge-worker-test"))
                task.cancel()
                with pytest.raises(asyncio.CancelledError):
                    await task
                assert not runtime_trace.pending_activities("bridge-worker-test")
            finally:
                release.set()
        finally:
            reset_cid(token)
    asyncio.run(scenario())


def test_runtime_keeps_failure_capture_provider_alongside_activity_diagnostics(monkeypatch):
    from packages.aura_core.observability import failure_evidence
    import numpy as np

    registered_failure, registered_trace = [], []
    monkeypatch.setattr(failure_evidence, 'register_capture_provider', registered_failure.append)
    monkeypatch.setattr(runtimes, 'register_diagnostic_provider',
                        lambda name, provider: registered_trace.append((name, provider)))
    runtime = runtimes.TargetRuntimeService({})
    assert registered_failure == [runtime._capture_failure_png]
    assert registered_trace == [('target_runtime', runtime._diagnostic_snapshot)]

    def forbidden(*args, **kwargs):
        raise AssertionError('Failure capture must not bind, recover, or focus a target')

    monkeypatch.setattr(runtime, '_get_or_create_session', forbidden)
    runtime._session_key = '{"game_scope": "resonance_pc"}'
    runtime._session = SimpleNamespace(capture=forbidden, focus=forbidden,
        capture_diagnostic=lambda: SimpleNamespace(
            success=True, image=np.zeros((3, 4, 3), dtype=np.uint8), backend='fake',
        ))
    result = registered_failure[0]({'plan': 'resonance_pc'})
    assert result['png'].startswith(b'\x89PNG')
    assert (result['width'], result['height'], result['backend']) == (4, 3, 'fake')
    with pytest.raises(TargetRuntimeError, match='another plan'):
        registered_failure[0]({'plan': 'other'})
