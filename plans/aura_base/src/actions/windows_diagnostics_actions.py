# -*- coding: utf-8 -*-
from __future__ import annotations

from typing import Any, Optional

from packages.aura_core.api import action_info, requires_services

from ..services.windows_diagnostics_service import WindowsDiagnosticsService


@action_info(name="windows.list_candidate_windows", public=True, read_only=True)
@requires_services(windows_diagnostics="windows_diagnostics")
def windows_list_candidate_windows(
    windows_diagnostics: WindowsDiagnosticsService,
    require_visible: bool = True,
    include_children: bool = False,
    include_empty_title: bool = False,
    limit: int = 50,
) -> list[dict[str, Any]]:
    return windows_diagnostics.list_candidate_windows(
        require_visible=require_visible,
        include_children=include_children,
        include_empty_title=include_empty_title,
        limit=limit,
    )


@action_info(name="windows.describe_target_window", public=True, read_only=True)
@requires_services(windows_diagnostics="windows_diagnostics")
def windows_describe_target_window(
    windows_diagnostics: WindowsDiagnosticsService,
    hwnd: int,
) -> dict[str, Any]:
    return windows_diagnostics.describe_target_window(hwnd)


@action_info(name="windows.resolve_target_preview", public=True, read_only=True)
@requires_services(windows_diagnostics="windows_diagnostics")
def windows_resolve_target_preview(
    windows_diagnostics: WindowsDiagnosticsService,
    target_overrides: Optional[dict[str, Any]] = None,
) -> dict[str, Any]:
    return windows_diagnostics.resolve_target_preview(target_overrides=target_overrides)


@action_info(name="windows.test_focus_activation", public=True, read_only=False)
@requires_services(windows_diagnostics="windows_diagnostics")
def windows_test_focus_activation(
    windows_diagnostics: WindowsDiagnosticsService,
    mode: str | None = None,
    sleep_ms: int | None = None,
    click_point: Optional[list[int]] = None,
    click_button: str | None = None,
) -> dict[str, Any]:
    resolved_click_point = None
    if click_point is not None:
        if not isinstance(click_point, list) or len(click_point) != 2:
            raise ValueError("click_point must be a [x, y] list when provided.")
        resolved_click_point = (int(click_point[0]), int(click_point[1]))
    return windows_diagnostics.test_focus_activation(
        mode=mode,
        sleep_ms=sleep_ms,
        click_point=resolved_click_point,
        click_button=click_button,
    )


@action_info(name="windows.show_runtime_capabilities", public=True, read_only=True)
@requires_services(windows_diagnostics="windows_diagnostics")
def windows_show_runtime_capabilities(
    windows_diagnostics: WindowsDiagnosticsService,
) -> dict[str, Any]:
    return windows_diagnostics.show_runtime_capabilities()


@action_info(name="windows.show_dpi_info", public=True, read_only=True)
@requires_services(windows_diagnostics="windows_diagnostics")
def windows_show_dpi_info(
    windows_diagnostics: WindowsDiagnosticsService,
) -> dict[str, Any]:
    return windows_diagnostics.show_dpi_info()


@action_info(name="windows.transform_reference_point", public=True, read_only=True)
@requires_services(windows_diagnostics="windows_diagnostics")
def windows_transform_reference_point(
    windows_diagnostics: WindowsDiagnosticsService,
    x: int,
    y: int,
) -> dict[str, Any]:
    return windows_diagnostics.transform_reference_point(x, y)


@action_info(name="windows.transform_reference_rect", public=True, read_only=True)
@requires_services(windows_diagnostics="windows_diagnostics")
def windows_transform_reference_rect(
    windows_diagnostics: WindowsDiagnosticsService,
    rect: list[int],
) -> dict[str, Any]:
    if not isinstance(rect, list) or len(rect) != 4:
        raise ValueError("rect must be a [x, y, w, h] list.")
    resolved_rect = (int(rect[0]), int(rect[1]), int(rect[2]), int(rect[3]))
    return windows_diagnostics.transform_reference_rect(resolved_rect)


@action_info(name="windows.probe_capture_backend", public=True, read_only=True)
@requires_services(windows_diagnostics="windows_diagnostics")
def windows_probe_capture_backend(
    windows_diagnostics: WindowsDiagnosticsService,
    backend: str | None = None,
    rect: Optional[list[int]] = None,
) -> dict[str, Any]:
    resolved_rect = None
    if rect is not None:
        if not isinstance(rect, list) or len(rect) != 4:
            raise ValueError("rect must be a [x, y, w, h] list.")
        resolved_rect = (int(rect[0]), int(rect[1]), int(rect[2]), int(rect[3]))
    return windows_diagnostics.probe_capture_backend(backend=backend, rect=resolved_rect)


@action_info(name="windows.stress_capture_backend", public=True, read_only=True)
@requires_services(windows_diagnostics="windows_diagnostics")
def windows_stress_capture_backend(
    windows_diagnostics: WindowsDiagnosticsService,
    backend: str | None = None,
    iterations: int = 100,
    interval_ms: int = 0,
    settle_after_close_ms: int = 500,
) -> dict[str, Any]:
    return windows_diagnostics.stress_capture_backend(
        backend=backend,
        iterations=iterations,
        interval_ms=interval_ms,
        settle_after_close_ms=settle_after_close_ms,
    )


@action_info(name="windows.probe_capture_candidates", public=True, read_only=True)
@requires_services(windows_diagnostics="windows_diagnostics")
def windows_probe_capture_candidates(
    windows_diagnostics: WindowsDiagnosticsService,
) -> list[dict[str, Any]]:
    return windows_diagnostics.probe_capture_candidates()


@action_info(name="windows.capture_preview", public=True, read_only=True)
@requires_services(windows_diagnostics="windows_diagnostics")
def windows_capture_preview(
    windows_diagnostics: WindowsDiagnosticsService,
    backend: str | None = None,
    rect: Optional[list[int]] = None,
) -> dict[str, Any]:
    resolved_rect = None
    if rect is not None:
        if not isinstance(rect, list) or len(rect) != 4:
            raise ValueError("rect must be a [x, y, w, h] list.")
        resolved_rect = (int(rect[0]), int(rect[1]), int(rect[2]), int(rect[3]))
    return windows_diagnostics.capture_preview(backend=backend, rect=resolved_rect)


@action_info(name="windows.check_window_spec", public=True, read_only=True)
@requires_services(windows_diagnostics="windows_diagnostics")
def windows_check_window_spec(
    windows_diagnostics: WindowsDiagnosticsService,
) -> dict[str, Any]:
    return windows_diagnostics.check_window_spec()


@action_info(name="windows.ensure_window_spec", public=True, read_only=False)
@requires_services(windows_diagnostics="windows_diagnostics")
def windows_ensure_window_spec(
    windows_diagnostics: WindowsDiagnosticsService,
) -> dict[str, Any]:
    return windows_diagnostics.ensure_window_spec()


@action_info(name="windows.show_gamepad_info", public=True, read_only=True)
@requires_services(windows_diagnostics="windows_diagnostics")
def windows_show_gamepad_info(
    windows_diagnostics: WindowsDiagnosticsService,
) -> dict[str, Any]:
    return windows_diagnostics.show_gamepad_info()
