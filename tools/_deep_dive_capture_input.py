"""Read-only Windows input observation for the independent capture tool.

Input hooks only observe events while the selected game HWND is foreground.
The foreground hook records handle changes only, including leaving the game.
No hook suppresses input, decodes typed text, changes focus, or injects events.
The supplied ``emit`` callback must enqueue without blocking or doing I/O.
"""
from __future__ import annotations

import ctypes
from ctypes import wintypes
import math
import os
import threading
import time
from typing import Any, Callable


_WH_KEYBOARD_LL = 13
_WH_MOUSE_LL = 14
_WM_QUIT = 0x0012
_EVENT_SYSTEM_FOREGROUND = 0x0003
_MOUSE_MESSAGES = {
    0x0200: "move", 0x0201: "left_down", 0x0202: "left_up",
    0x0204: "right_down", 0x0205: "right_up", 0x0207: "middle_down",
    0x0208: "middle_up", 0x020A: "wheel", 0x020B: "xbutton_down",
    0x020C: "xbutton_up", 0x020E: "horizontal_wheel",
}
_KEY_MESSAGES = {
    0x0100: "key_down", 0x0101: "key_up",
    0x0104: "system_key_down", 0x0105: "system_key_up",
}


class _MouseEvent(ctypes.Structure):
    _fields_ = [
        ("pt", wintypes.POINT), ("mouseData", wintypes.DWORD),
        ("flags", wintypes.DWORD), ("time", wintypes.DWORD),
        ("dwExtraInfo", ctypes.c_size_t),
    ]


class _KeyboardEvent(ctypes.Structure):
    _fields_ = [
        ("vkCode", wintypes.DWORD), ("scanCode", wintypes.DWORD),
        ("flags", wintypes.DWORD), ("time", wintypes.DWORD),
        ("dwExtraInfo", ctypes.c_size_t),
    ]


class _Message(ctypes.Structure):
    _fields_ = [
        ("hwnd", wintypes.HWND), ("message", wintypes.UINT),
        ("wParam", ctypes.c_size_t), ("lParam", ctypes.c_ssize_t),
        ("time", wintypes.DWORD), ("pt", wintypes.POINT),
        ("lPrivate", wintypes.DWORD),
    ]


class InputObserver:
    """Own two input hooks, a foreground hook and their Windows message pump.

    ``monotonic_ns`` bounds hook observation, not game consumption. The native
    ``os_event_time_ms`` is the Windows event clock and may wrap at 32 bits.
    ``injected`` distinguishes OS-marked injected input from physical input;
    it does not identify which process injected an event. Join tool commands
    with their separate sending intervals instead of assuming ownership.
    """

    def __init__(self, hwnd: int, emit: Callable[[dict], None], fail: Callable[[str], None]):
        if os.name != "nt":
            raise OSError("Windows low-level input observation requires Windows")
        if not int(hwnd):
            raise ValueError("A nonzero target HWND is required")
        self.hwnd = int(hwnd)
        self._emit = emit
        self._fail = fail
        self._lock = threading.Lock()
        self._started = threading.Event()
        self._finished = threading.Event()
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None
        self._thread_id = 0
        self._mouse_hook: Any = None
        self._keyboard_hook: Any = None
        self._foreground_hook: Any = None
        self._mouse_callback: Any = None
        self._keyboard_callback: Any = None
        self._foreground_callback: Any = None
        self._ready = False
        self._startup_error: str | None = None
        self._errors: list[str] = []
        self._error_count = 0
        self._counts = {"mouse": 0, "keyboard": 0, "foreground": 0, "filtered": 0, "emit_failed": 0}
        self._user32 = ctypes.WinDLL("user32", use_last_error=True)
        self._kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
        self._hook_proc = ctypes.WINFUNCTYPE(
            ctypes.c_ssize_t, ctypes.c_int, ctypes.c_size_t, ctypes.c_ssize_t,
        )
        self._win_event_proc = ctypes.WINFUNCTYPE(
            None, wintypes.HANDLE, wintypes.DWORD, wintypes.HWND,
            wintypes.LONG, wintypes.LONG, wintypes.DWORD, wintypes.DWORD,
        )
        self._configure_api()

    def _configure_api(self) -> None:
        user32, kernel32 = self._user32, self._kernel32
        user32.SetWindowsHookExW.argtypes = [ctypes.c_int, self._hook_proc, wintypes.HINSTANCE, wintypes.DWORD]
        user32.SetWindowsHookExW.restype = wintypes.HANDLE
        user32.CallNextHookEx.argtypes = [wintypes.HANDLE, ctypes.c_int, ctypes.c_size_t, ctypes.c_ssize_t]
        user32.CallNextHookEx.restype = ctypes.c_ssize_t
        user32.UnhookWindowsHookEx.argtypes = [wintypes.HANDLE]
        user32.UnhookWindowsHookEx.restype = wintypes.BOOL
        user32.GetForegroundWindow.argtypes = []
        user32.GetForegroundWindow.restype = wintypes.HWND
        user32.SetWinEventHook.argtypes = [
            wintypes.DWORD, wintypes.DWORD, wintypes.HMODULE,
            self._win_event_proc, wintypes.DWORD, wintypes.DWORD, wintypes.DWORD,
        ]
        user32.SetWinEventHook.restype = wintypes.HANDLE
        user32.UnhookWinEvent.argtypes = [wintypes.HANDLE]
        user32.UnhookWinEvent.restype = wintypes.BOOL
        user32.PeekMessageW.argtypes = [ctypes.POINTER(_Message), wintypes.HWND, wintypes.UINT, wintypes.UINT, wintypes.UINT]
        user32.PeekMessageW.restype = wintypes.BOOL
        user32.GetMessageW.argtypes = [ctypes.POINTER(_Message), wintypes.HWND, wintypes.UINT, wintypes.UINT]
        user32.GetMessageW.restype = wintypes.BOOL
        user32.TranslateMessage.argtypes = [ctypes.POINTER(_Message)]
        user32.TranslateMessage.restype = wintypes.BOOL
        user32.DispatchMessageW.argtypes = [ctypes.POINTER(_Message)]
        user32.DispatchMessageW.restype = ctypes.c_ssize_t
        user32.PostThreadMessageW.argtypes = [wintypes.DWORD, wintypes.UINT, ctypes.c_size_t, ctypes.c_ssize_t]
        user32.PostThreadMessageW.restype = wintypes.BOOL
        kernel32.GetCurrentThreadId.argtypes = []
        kernel32.GetCurrentThreadId.restype = wintypes.DWORD
        kernel32.GetModuleHandleW.argtypes = [wintypes.LPCWSTR]
        kernel32.GetModuleHandleW.restype = wintypes.HMODULE

    @staticmethod
    def _timeout(value: float) -> float:
        value = float(value)
        if not math.isfinite(value) or value <= 0:
            raise ValueError("Observer timeout must be finite and positive")
        return value

    def start(self, timeout: float = 5) -> None:
        timeout = self._timeout(timeout)
        if self._thread is not None:
            raise RuntimeError("Input observer is single-use and was already started")
        self._thread = threading.Thread(target=self._run, name="deep-dive-input-observer", daemon=True)
        self._thread.start()
        if not self._started.wait(timeout):
            self._stop.set()
            self._error("input_observer_start_timeout")
            # Queue creation precedes hook installation. If installation is
            # delayed, _run also observes _stop before entering GetMessage.
            self.close(timeout=timeout)
            raise TimeoutError("Input observer did not finish startup")
        if self._startup_error is not None or not self._ready:
            self.close(timeout=timeout)
            raise RuntimeError(self._startup_error or "Input observer failed to start")

    def _error(self, message: str) -> None:
        with self._lock:
            self._error_count += 1
            if len(self._errors) < 50:
                self._errors.append(str(message))
        try:
            self._fail(str(message))
        except BaseException:
            # A recorder failure must never propagate through a Windows hook.
            pass

    def _callback(self, device: str, code: int, message: int, pointer: int) -> int:
        try:
            if code >= 0 and not self._stop.is_set():
                observed_at = time.monotonic_ns()
                foreground = int(self._user32.GetForegroundWindow() or 0)
                if foreground != self.hwnd:
                    with self._lock:
                        self._counts["filtered"] += 1
                else:
                    row: dict[str, Any] = {
                        "event": "input_observed", "device": device,
                        "monotonic_ns": observed_at, "foreground_hwnd": foreground,
                        "message": int(message), "observer_thread_id": self._thread_id,
                    }
                    if device == "mouse":
                        native = ctypes.cast(pointer, ctypes.POINTER(_MouseEvent)).contents
                        data = int(native.mouseData)
                        wheel = ctypes.c_int16((data >> 16) & 0xFFFF).value
                        action = _MOUSE_MESSAGES.get(int(message), "other")
                        button = next((name for name in ("left", "right", "middle", "xbutton")
                                       if action.startswith(name + "_")), None)
                        row.update(
                            action=action, button=button,
                            button_state="down" if action.endswith("_down") else "up" if action.endswith("_up") else None,
                            screen_xy=[int(native.pt.x), int(native.pt.y)],
                            mouse_data=data, flags=int(native.flags),
                            injected=bool(native.flags & 0x01),
                            lower_integrity_injected=bool(native.flags & 0x02),
                            wheel_delta=wheel if message in (0x020A, 0x020E) else None,
                            xbutton=((data >> 16) & 0xFFFF) if message in (0x020B, 0x020C) else None,
                        )
                    else:
                        native = ctypes.cast(pointer, ctypes.POINTER(_KeyboardEvent)).contents
                        row.update(
                            action=_KEY_MESSAGES.get(int(message), "other"),
                            vk_code=int(native.vkCode), scan_code=int(native.scanCode),
                            flags=int(native.flags), injected=bool(native.flags & 0x10),
                            lower_integrity_injected=bool(native.flags & 0x02),
                            extended=bool(native.flags & 0x01),
                            alt_down=bool(native.flags & 0x20), key_up=bool(native.flags & 0x80),
                        )
                    row.update(os_event_time_ms=int(native.time), extra_info=int(native.dwExtraInfo))
                    try:
                        self._emit(row)
                    except BaseException as exc:
                        with self._lock:
                            self._counts["emit_failed"] += 1
                        self._error(f"input_observer_emit_failed:{type(exc).__name__}")
                    else:
                        with self._lock:
                            self._counts[device] += 1
        except BaseException as exc:
            self._error(f"input_observer_callback_failed:{type(exc).__name__}")
        # Every path, including filtered and erroneous events, continues the
        # hook chain. Returning a nonzero value would suppress user input.
        return int(self._user32.CallNextHookEx(None, code, message, pointer))

    def _foreground_event(self, hwnd: int, os_event_time: int | None, *, initial: bool = False) -> None:
        """Observe handle changes only; never inspect another app's content."""
        try:
            if self._stop.is_set():
                return
            self._emit({
                "event": "foreground_observed", "monotonic_ns": time.monotonic_ns(),
                "foreground_hwnd": int(hwnd), "is_target": int(hwnd) == self.hwnd,
                "os_event_time_ms": os_event_time, "initial_snapshot": initial,
            })
        except BaseException as exc:
            with self._lock:
                self._counts["emit_failed"] += 1
            self._error(f"foreground_observer_emit_failed:{type(exc).__name__}")
        else:
            with self._lock:
                self._counts["foreground"] += 1

    def _on_win_event(self, hook, event, hwnd, object_id, child_id, source_thread, event_time) -> None:
        # Out-of-context WinEvent callbacks execute on this observer's message
        # thread. Event HWND preserves the reported transition even if focus has
        # changed again by the time this callback runs.
        try:
            if event == _EVENT_SYSTEM_FOREGROUND:
                self._foreground_event(int(hwnd or 0), int(event_time))
        except BaseException as exc:
            self._error(f"foreground_observer_callback_failed:{type(exc).__name__}")

    def _run(self) -> None:
        try:
            self._thread_id = int(self._kernel32.GetCurrentThreadId())
            message = _Message()
            self._user32.PeekMessageW(ctypes.byref(message), None, 0, 0, 0)
            self._mouse_callback = self._hook_proc(lambda c, m, p: self._callback("mouse", c, m, p))
            self._keyboard_callback = self._hook_proc(lambda c, m, p: self._callback("keyboard", c, m, p))
            module = self._kernel32.GetModuleHandleW(None)
            if not module:
                raise ctypes.WinError(ctypes.get_last_error())
            self._mouse_hook = self._user32.SetWindowsHookExW(_WH_MOUSE_LL, self._mouse_callback, module, 0)
            if not self._mouse_hook:
                raise ctypes.WinError(ctypes.get_last_error())
            self._keyboard_hook = self._user32.SetWindowsHookExW(_WH_KEYBOARD_LL, self._keyboard_callback, module, 0)
            if not self._keyboard_hook:
                raise ctypes.WinError(ctypes.get_last_error())
            self._foreground_callback = self._win_event_proc(self._on_win_event)
            # WINEVENT_OUTOFCONTEXT=0; no process/thread filter is intentional:
            # leaving the game must be recorded too, using HWNDs only.
            self._foreground_hook = self._user32.SetWinEventHook(
                _EVENT_SYSTEM_FOREGROUND, _EVENT_SYSTEM_FOREGROUND,
                None, self._foreground_callback, 0, 0, 0,
            )
            if not self._foreground_hook:
                raise ctypes.WinError(ctypes.get_last_error())
            self._foreground_event(int(self._user32.GetForegroundWindow() or 0), None, initial=True)
            self._ready = True
            self._started.set()
            while not self._stop.is_set():
                result = int(self._user32.GetMessageW(ctypes.byref(message), None, 0, 0))
                if result == -1:
                    raise ctypes.WinError(ctypes.get_last_error())
                if result == 0:
                    break
                self._user32.TranslateMessage(ctypes.byref(message))
                self._user32.DispatchMessageW(ctypes.byref(message))
        except BaseException as exc:
            text = f"input_observer_thread_failed:{type(exc).__name__}:{exc}"
            if not self._ready:
                self._startup_error = text
            self._error(text)
        finally:
            self._ready = False
            if self._foreground_hook:
                try:
                    if not self._user32.UnhookWinEvent(self._foreground_hook):
                        raise ctypes.WinError(ctypes.get_last_error())
                except BaseException as exc:
                    self._error(f"foreground_observer_unhook_failed:{type(exc).__name__}:{exc}")
                else:
                    self._foreground_hook = None
            for name in ("_keyboard_hook", "_mouse_hook"):
                handle = getattr(self, name)
                if handle:
                    try:
                        if not self._user32.UnhookWindowsHookEx(handle):
                            raise ctypes.WinError(ctypes.get_last_error())
                    except BaseException as exc:
                        self._error(f"input_observer_unhook_failed:{name}:{type(exc).__name__}:{exc}")
                    else:
                        setattr(self, name, None)
            self._started.set()
            self._finished.set()

    def close(self, timeout: float = 5) -> None:
        timeout = self._timeout(timeout)
        self._stop.set()
        thread = self._thread
        if thread is None:
            return
        if thread is threading.current_thread():
            self._error("input_observer_close_from_hook_thread")
            raise RuntimeError("Input observer must be joined from its owner thread")
        if thread.is_alive() and self._thread_id:
            posted = self._user32.PostThreadMessageW(self._thread_id, _WM_QUIT, 0, 0)
            if not posted and not self._finished.is_set():
                self._error(f"input_observer_quit_post_failed:{ctypes.get_last_error()}")
        thread.join(timeout)
        if thread.is_alive():
            self._error("input_observer_close_timeout")
            raise TimeoutError("Input observer thread is still running")
        if self._mouse_hook or self._keyboard_hook or self._foreground_hook:
            raise RuntimeError("Input observer did not confirm removal of every installed hook")

    def summary(self) -> dict[str, Any]:
        with self._lock:
            counts, errors, error_count = dict(self._counts), list(self._errors), self._error_count
        alive = self._thread is not None and self._thread.is_alive()
        return {
            "target_hwnd": self.hwnd, "foreground_filter": "exact_target_hwnd",
            "thread_id": self._thread_id, "thread_alive": alive, "ready": self._ready,
            "started": self._thread is not None, "finished": self._finished.is_set(),
            "mouse_hook_installed": bool(self._mouse_hook),
            "keyboard_hook_installed": bool(self._keyboard_hook),
            "foreground_hook_installed": bool(self._foreground_hook),
            "closed": not alive and not self._mouse_hook and not self._keyboard_hook and not self._foreground_hook,
            "counts": counts, "error_count": error_count, "errors": errors,
            "text_decoding": False, "injected_process_attribution": False,
            "native_event_loss_detection": "not_provable_from_low_level_hooks_alone",
            "foreground_observation": "EVENT_SYSTEM_FOREGROUND handles only; initial snapshot has no OS event timestamp",
            "timestamp_semantics": "monotonic hook observation; OS event clock retained separately",
        }
