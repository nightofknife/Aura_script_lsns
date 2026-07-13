from __future__ import annotations

import argparse
import json
import time
from datetime import datetime
from pathlib import Path
from typing import Any

import win32con
import win32gui
import win32process


def _window_state(hwnd: int, expected_pid: int | None) -> dict[str, Any]:
    is_window = bool(win32gui.IsWindow(hwnd))
    state: dict[str, Any] = {
        "is_window": is_window,
        "visible": False,
        "ws_visible": False,
        "iconic": False,
        "enabled": False,
        "foreground": False,
        "title": "",
        "class_name": "",
        "pid": None,
        "pid_matches": None,
        "window_rect": None,
        "client_rect": None,
    }
    if not is_window:
        return state

    style = int(win32gui.GetWindowLong(hwnd, win32con.GWL_STYLE))
    process_id = int(win32process.GetWindowThreadProcessId(hwnd)[1])
    state.update(
        {
            "visible": bool(win32gui.IsWindowVisible(hwnd)),
            "ws_visible": bool(style & win32con.WS_VISIBLE),
            "iconic": bool(win32gui.IsIconic(hwnd)),
            "enabled": bool(win32gui.IsWindowEnabled(hwnd)),
            "foreground": int(win32gui.GetForegroundWindow() or 0) == int(hwnd),
            "title": win32gui.GetWindowText(hwnd),
            "class_name": win32gui.GetClassName(hwnd),
            "pid": process_id,
            "pid_matches": None if expected_pid is None else process_id == expected_pid,
            "window_rect": list(win32gui.GetWindowRect(hwnd)),
            "client_rect": list(win32gui.GetClientRect(hwnd)),
        }
    )
    return state


def _event_payload(event: str, hwnd: int, state: dict[str, Any]) -> dict[str, Any]:
    return {
        "timestamp": datetime.now().astimezone().isoformat(timespec="milliseconds"),
        "monotonic": round(time.monotonic(), 6),
        "event": event,
        "hwnd": int(hwnd),
        **state,
    }


def main() -> int:
    parser = argparse.ArgumentParser(description="Monitor Windows visibility state without changing the target window.")
    parser.add_argument("--hwnd", required=True, type=int)
    parser.add_argument("--pid", type=int)
    parser.add_argument("--output", required=True, type=Path)
    parser.add_argument("--interval-ms", type=int, default=50)
    parser.add_argument("--heartbeat-sec", type=float, default=1.0)
    parser.add_argument("--duration-sec", type=float, default=0.0)
    args = parser.parse_args()

    args.output.parent.mkdir(parents=True, exist_ok=True)
    interval = max(int(args.interval_ms), 10) / 1000.0
    heartbeat = max(float(args.heartbeat_sec), interval)
    duration = max(float(args.duration_sec), 0.0)
    started = time.monotonic()
    last_heartbeat = 0.0
    previous: dict[str, Any] | None = None

    with args.output.open("a", encoding="utf-8", buffering=1) as handle:
        while duration <= 0 or time.monotonic() - started < duration:
            sampled_at = time.monotonic()
            try:
                state = _window_state(args.hwnd, args.pid)
            except Exception as exc:  # noqa: BLE001
                state = {"sample_error": f"{type(exc).__name__}: {exc}"}

            changed = previous is None or state != previous
            heartbeat_due = sampled_at - last_heartbeat >= heartbeat
            if changed or heartbeat_due:
                event = "initial" if previous is None else "state_change" if changed else "heartbeat"
                handle.write(json.dumps(_event_payload(event, args.hwnd, state), ensure_ascii=False) + "\n")
                if heartbeat_due:
                    last_heartbeat = sampled_at
            previous = state
            time.sleep(interval)

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
