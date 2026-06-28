# -*- coding: utf-8 -*-
from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Any, Iterable, List

import psutil
import win32api
import win32gui
import win32process

from ..contracts import TargetRuntimeError
from ..runtime_config import RuntimeTargetConfig


@dataclass(frozen=True)
class WindowCandidate:
    hwnd: int
    pid: int | None
    process_name: str | None
    exe_path: str | None
    title: str | None
    class_name: str | None
    visible: bool
    enabled: bool
    is_child: bool
    parent_hwnd: int | None
    foreground: bool
    client_rect: tuple[int, int, int, int] | None
    client_rect_screen: tuple[int, int, int, int] | None
    window_rect_screen: tuple[int, int, int, int] | None
    monitor_index: int | None
    process_create_time: float | None

    @property
    def client_area(self) -> int:
        if not self.client_rect:
            return 0
        return max(int(self.client_rect[2]), 0) * max(int(self.client_rect[3]), 0)

    def to_dict(self) -> dict[str, Any]:
        return {
            "hwnd": int(self.hwnd),
            "pid": self.pid,
            "process_name": self.process_name,
            "exe_path": self.exe_path,
            "title": self.title,
            "class_name": self.class_name,
            "visible": self.visible,
            "enabled": self.enabled,
            "is_child": self.is_child,
            "parent_hwnd": self.parent_hwnd,
            "foreground": self.foreground,
            "client_rect": list(self.client_rect) if self.client_rect else None,
            "client_rect_screen": list(self.client_rect_screen) if self.client_rect_screen else None,
            "window_rect_screen": list(self.window_rect_screen) if self.window_rect_screen else None,
            "monitor_index": self.monitor_index,
            "client_area": self.client_area,
            "process_create_time": self.process_create_time,
        }


def describe_window(hwnd: int) -> WindowCandidate:
    resolved_hwnd = int(hwnd)
    if not win32gui.IsWindow(resolved_hwnd):
        raise TargetRuntimeError(
            "window_not_found",
            "The requested hwnd does not point to a valid window.",
            {"hwnd": resolved_hwnd},
        )
    return _candidate_from_hwnd(resolved_hwnd)


def list_window_candidates(
    config: RuntimeTargetConfig | None = None,
    *,
    require_visible: bool | None = None,
    allow_child_window: bool | None = None,
    allow_empty_title: bool | None = None,
) -> list[WindowCandidate]:
    if config is not None:
        require_visible = config.require_visible if require_visible is None else require_visible
        allow_child_window = config.allow_child_window if allow_child_window is None else allow_child_window
        allow_empty_title = config.allow_empty_title if allow_empty_title is None else allow_empty_title
    require_visible = bool(require_visible) if require_visible is not None else True
    allow_child_window = bool(allow_child_window) if allow_child_window is not None else False
    allow_empty_title = bool(allow_empty_title) if allow_empty_title is not None else False

    hwnds: list[int] = []

    def callback(hwnd: int, _lparam: int) -> bool:
        if not win32gui.IsWindow(hwnd):
            return True
        if require_visible and not win32gui.IsWindowVisible(hwnd):
            return True
        if not allow_child_window and win32gui.GetParent(hwnd):
            return True
        title = win32gui.GetWindowText(hwnd) or ""
        if not allow_empty_title and not title.strip():
            return True
        hwnds.append(int(hwnd))
        return True

    win32gui.EnumWindows(callback, 0)
    return [_candidate_from_hwnd(hwnd) for hwnd in hwnds]


def resolve_window_candidate(config: RuntimeTargetConfig) -> WindowCandidate:
    if config.mode == "hwnd":
        candidate = describe_window(int(config.hwnd or 0))
        filtered = _apply_common_filters([candidate], config)
        if not filtered:
            raise TargetRuntimeError(
                "window_not_found",
                "The configured hwnd does not satisfy the active target filters.",
                {"hwnd": int(config.hwnd or 0)},
            )
        return filtered[0]

    candidates = list_window_candidates(config)
    candidates = _apply_mode_filter(candidates, config)
    candidates = _apply_common_filters(candidates, config)
    return _pick_best_candidate(candidates, config)


def resolve_window_handle(config: RuntimeTargetConfig) -> int:
    return int(resolve_window_candidate(config).hwnd)


def _apply_mode_filter(candidates: list[WindowCandidate], config: RuntimeTargetConfig) -> list[WindowCandidate]:
    if config.mode == "process":
        return [candidate for candidate in candidates if _matches_process_selector(candidate, config)]
    if config.mode == "title":
        return [candidate for candidate in candidates if _matches_title_selector(candidate, config)]
    return list(candidates)


def _apply_common_filters(candidates: list[WindowCandidate], config: RuntimeTargetConfig) -> list[WindowCandidate]:
    filtered = [candidate for candidate in candidates if _matches_common_filters(candidate, config)]
    non_launcher = [
        candidate
        for candidate in filtered
        if (candidate.process_name or "").lower() not in {name.lower() for name in config.launcher_process_names}
    ]
    if non_launcher:
        filtered = non_launcher
    return filtered


def _pick_best_candidate(candidates: list[WindowCandidate], config: RuntimeTargetConfig) -> WindowCandidate:
    if not candidates:
        raise TargetRuntimeError(
            "window_not_found",
            "No candidate window matched the configured selectors.",
            {"target": _summarize_target_config(config)},
        )
    if len(candidates) == 1:
        return candidates[0]

    foreground = [candidate for candidate in candidates if candidate.foreground]
    if len(foreground) == 1:
        return foreground[0]
    if foreground:
        candidates = foreground

    if config.prefer_largest_client_area:
        max_area = max(candidate.client_area for candidate in candidates)
        largest = [candidate for candidate in candidates if candidate.client_area == max_area]
        if len(largest) == 1:
            return largest[0]
        candidates = largest

    if config.prefer_newest_process:
        newest_ts = max(candidate.process_create_time or 0.0 for candidate in candidates)
        newest = [candidate for candidate in candidates if (candidate.process_create_time or 0.0) == newest_ts]
        if len(newest) == 1:
            return newest[0]
        candidates = newest

    raise TargetRuntimeError(
        "window_target_ambiguous",
        "Multiple windows matched the configured selectors.",
        {
            "target": _summarize_target_config(config),
            "count": len(candidates),
            "matches": [candidate.to_dict() for candidate in candidates[:10]],
        },
    )


def _matches_process_selector(candidate: WindowCandidate, config: RuntimeTargetConfig) -> bool:
    checks: list[bool] = []
    if config.pid is not None:
        checks.append(candidate.pid == config.pid)
    if config.process_name:
        checks.append((candidate.process_name or "").lower() == str(config.process_name).lower())
    if config.exe_path_contains:
        checks.append(str(config.exe_path_contains).lower() in (candidate.exe_path or "").lower())
    if config.class_name:
        checks.append(_matches_text((candidate.class_name or ""), str(config.class_name), exact=config.class_exact))
    if config.class_regex:
        checks.append(_matches_regex((candidate.class_name or ""), str(config.class_regex)))
    return all(checks) if checks else True


def _matches_title_selector(candidate: WindowCandidate, config: RuntimeTargetConfig) -> bool:
    checks: list[bool] = []
    if config.title:
        checks.append(_matches_text((candidate.title or ""), str(config.title), exact=config.title_exact))
    if config.title_regex:
        checks.append(_matches_regex((candidate.title or ""), str(config.title_regex)))
    return all(checks) if checks else True


def _matches_common_filters(candidate: WindowCandidate, config: RuntimeTargetConfig) -> bool:
    if config.require_visible and not candidate.visible:
        return False
    if config.require_foreground and not candidate.foreground:
        return False
    if not config.allow_child_window and candidate.is_child:
        return False
    if not config.allow_empty_title and not (candidate.title or "").strip():
        return False
    if config.process_name and config.mode != "process":
        if (candidate.process_name or "").lower() != str(config.process_name).lower():
            return False
    if config.pid is not None and config.mode != "process" and candidate.pid != config.pid:
        return False
    if config.exe_path_contains and str(config.exe_path_contains).lower() not in (candidate.exe_path or "").lower():
        return False
    if config.title and config.mode != "title":
        if not _matches_text((candidate.title or ""), str(config.title), exact=config.title_exact):
            return False
    if config.title_regex and config.mode != "title":
        if not _matches_regex((candidate.title or ""), str(config.title_regex)):
            return False
    if config.class_name:
        if not _matches_text((candidate.class_name or ""), str(config.class_name), exact=config.class_exact):
            return False
    if config.class_regex and not _matches_regex((candidate.class_name or ""), str(config.class_regex)):
        return False
    if config.exclude_titles and _matches_any((candidate.title or ""), config.exclude_titles):
        return False
    if config.exclude_process_names and _matches_any((candidate.process_name or ""), config.exclude_process_names):
        return False
    if config.monitor_index is not None and candidate.monitor_index != config.monitor_index:
        return False
    if config.client_size_exact and not _matches_exact_size(candidate.client_rect, config.client_size_exact):
        return False
    if config.client_size_min and not _matches_min_size(candidate.client_rect, config.client_size_min):
        return False
    if config.client_size_max and not _matches_max_size(candidate.client_rect, config.client_size_max):
        return False
    return True


def _candidate_from_hwnd(hwnd: int) -> WindowCandidate:
    visible = bool(win32gui.IsWindowVisible(hwnd))
    enabled = bool(win32gui.IsWindowEnabled(hwnd))
    parent_hwnd = int(win32gui.GetParent(hwnd) or 0)
    is_child = parent_hwnd != 0
    title = win32gui.GetWindowText(hwnd) or None
    class_name = _safe_get(lambda: win32gui.GetClassName(hwnd))
    pid = None
    process_name = None
    exe_path = None
    process_create_time = None
    try:
        _, pid = win32process.GetWindowThreadProcessId(hwnd)
        if pid:
            process = psutil.Process(pid)
            process_name = process.name()
            exe_path = process.exe()
            process_create_time = process.create_time()
    except Exception:
        pid = None
    client_rect = _safe_client_rect(hwnd)
    client_rect_screen = _safe_client_rect_screen(hwnd)
    window_rect_screen = _safe_window_rect_screen(hwnd)
    foreground = int(win32gui.GetForegroundWindow() or 0) == int(hwnd)
    monitor_index = _safe_monitor_index(hwnd)
    return WindowCandidate(
        hwnd=int(hwnd),
        pid=pid,
        process_name=process_name,
        exe_path=exe_path,
        title=title,
        class_name=class_name or None,
        visible=visible,
        enabled=enabled,
        is_child=is_child,
        parent_hwnd=parent_hwnd or None,
        foreground=foreground,
        client_rect=client_rect,
        client_rect_screen=client_rect_screen,
        window_rect_screen=window_rect_screen,
        monitor_index=monitor_index,
        process_create_time=process_create_time,
    )


def _safe_client_rect(hwnd: int) -> tuple[int, int, int, int] | None:
    try:
        rect = win32gui.GetClientRect(hwnd)
        width = max(int(rect[2] - rect[0]), 0)
        height = max(int(rect[3] - rect[1]), 0)
        return 0, 0, width, height
    except Exception:
        return None


def _safe_client_rect_screen(hwnd: int) -> tuple[int, int, int, int] | None:
    try:
        left_top = win32gui.ClientToScreen(hwnd, (0, 0))
        rect = win32gui.GetClientRect(hwnd)
        width = max(int(rect[2] - rect[0]), 0)
        height = max(int(rect[3] - rect[1]), 0)
        return int(left_top[0]), int(left_top[1]), width, height
    except Exception:
        return None


def _safe_window_rect_screen(hwnd: int) -> tuple[int, int, int, int] | None:
    try:
        left, top, right, bottom = win32gui.GetWindowRect(hwnd)
        return int(left), int(top), int(right - left), int(bottom - top)
    except Exception:
        return None


def _safe_monitor_index(hwnd: int) -> int | None:
    try:
        monitor = win32api.MonitorFromWindow(hwnd, 1)
        monitors = win32api.EnumDisplayMonitors()
        for index, monitor_entry in enumerate(monitors):
            if monitor_entry[0] == monitor:
                return int(index)
    except Exception:
        return None
    return None


def _safe_get(getter: Any) -> Any:
    try:
        return getter()
    except Exception:
        return None


def _matches_text(actual: str, expected: str, *, exact: bool) -> bool:
    normalized_actual = str(actual or "")
    normalized_expected = str(expected or "")
    if exact:
        return normalized_actual == normalized_expected
    return normalized_expected.lower() in normalized_actual.lower()


def _matches_regex(actual: str, pattern: str) -> bool:
    try:
        return re.search(pattern, actual or "", flags=re.IGNORECASE) is not None
    except re.error as exc:
        raise TargetRuntimeError(
            "target_config_invalid",
            f"Invalid regex pattern '{pattern}': {exc}",
            {"pattern": pattern},
        ) from exc


def _matches_any(actual: str, candidates: Iterable[str]) -> bool:
    normalized_actual = str(actual or "").lower()
    return any(str(candidate).lower() in normalized_actual for candidate in candidates)


def _matches_exact_size(client_rect: tuple[int, int, int, int] | None, size: tuple[int, int]) -> bool:
    if client_rect is None:
        return False
    return int(client_rect[2]) == int(size[0]) and int(client_rect[3]) == int(size[1])


def _matches_min_size(client_rect: tuple[int, int, int, int] | None, size: tuple[int, int]) -> bool:
    if client_rect is None:
        return False
    return int(client_rect[2]) >= int(size[0]) and int(client_rect[3]) >= int(size[1])


def _matches_max_size(client_rect: tuple[int, int, int, int] | None, size: tuple[int, int]) -> bool:
    if client_rect is None:
        return False
    return int(client_rect[2]) <= int(size[0]) and int(client_rect[3]) <= int(size[1])


def _summarize_target_config(config: RuntimeTargetConfig) -> dict[str, Any]:
    payload = config.to_dict()
    payload.pop("adb_serial", None)
    payload.pop("connect_on_start", None)
    return payload
