"""Resonance PC process lifecycle and startup-screen recovery actions."""

from __future__ import annotations

import re
import time
from pathlib import Path
from typing import Any, Dict, Iterable, List, Optional, Tuple

import psutil
import win32con
import win32gui

from packages.aura_core.api import action_info, requires_services
from packages.aura_core.observability.logging.core_logger import logger
from packages.aura_core.utils.exceptions import StopTaskException
from packages.aura_game.executable_locator import validate_executable_path


PROCESS_IDENTIFIER = "resonance_pc"
PROCESS_NAME = "雷索纳斯.exe"
DEFAULT_STARTUP_REGION = (0, 0, 1280, 720)

MAIN_MARKERS = ("访问城市", "访问地区", "启程", "STARTENGINE", "资产")
UPDATE_BUTTON_MARKERS = (
    "立即更新",
    "开始更新",
    "确认更新",
    "开始下载",
    "确认下载",
    "重新连接",
    "更新",
    "下载",
)


def _coerce_region(region: Optional[List[int]]) -> Tuple[int, int, int, int]:
    if region is None:
        return DEFAULT_STARTUP_REGION
    if len(region) != 4:
        raise ValueError("region must contain four integers: x, y, width, height")
    return tuple(int(value) for value in region)  # type: ignore[return-value]


def _normalize_text(text: str) -> str:
    return re.sub(r"[\s:：,，.。!！\-_/\\|·]+", "", str(text or "")).upper()


def _collect_hits(
    items: Iterable[Dict[str, Any]],
    markers: Tuple[str, ...],
    *,
    exact: bool = False,
) -> List[Dict[str, Any]]:
    hits: List[Dict[str, Any]] = []
    normalized_markers = [(_normalize_text(marker), marker) for marker in markers]
    for item in items:
        normalized = str(item.get("norm") or "")
        for marker_norm, marker in normalized_markers:
            matched = normalized == marker_norm if exact else marker_norm in normalized
            if matched:
                hits.append(
                    {
                        "marker": marker,
                        "text": item["text"],
                        "center": item["center"],
                        "confidence": item["confidence"],
                    }
                )
                break
    hits.sort(key=lambda row: float(row.get("confidence") or 0.0), reverse=True)
    return hits


def _detect_state(app: Any, ocr: Any, region: Tuple[int, int, int, int]) -> Dict[str, Any]:
    capture = app.capture(rect=region)
    if not capture.success or capture.image is None:
        return {
            "ok": False,
            "state": "other",
            "main": False,
            "matched": {"main": [], "update": []},
            "item_count": 0,
        }

    offset_x, offset_y = region[0], region[1]
    recognized = ocr.recognize_all(source_image=capture.image)
    items: List[Dict[str, Any]] = []
    for item in getattr(recognized, "results", []):
        center = getattr(item, "center_point", None) or (0, 0)
        items.append(
            {
                "text": str(getattr(item, "text", "") or ""),
                "norm": _normalize_text(getattr(item, "text", "") or ""),
                "center": [int(center[0]) + offset_x, int(center[1]) + offset_y],
                "confidence": float(getattr(item, "confidence", 0.0) or 0.0),
            }
        )

    matched = {
        "main": _collect_hits(items, MAIN_MARKERS),
        "update": _collect_hits(items, UPDATE_BUTTON_MARKERS, exact=True),
    }
    main = bool(matched["main"])
    if main:
        state = "main"
    elif matched["update"]:
        state = "update"
    else:
        state = "other"
    return {
        "ok": True,
        "state": state,
        "main": main,
        "matched": {key: value[:3] for key, value in matched.items()},
        "item_count": len(items),
    }


def _compact_state(result: Dict[str, Any], round_index: int, action: str = "detect") -> Dict[str, Any]:
    return {
        "round": int(round_index),
        "action": action,
        "state": str(result.get("state") or "other"),
        "main": bool(result.get("main")),
        "item_count": int(result.get("item_count") or 0),
        "matched": result.get("matched") or {},
    }


def _matching_processes() -> List[Dict[str, Any]]:
    matches: List[Dict[str, Any]] = []
    for process in psutil.process_iter(["pid", "name", "exe", "create_time"]):
        try:
            if str(process.info.get("name") or "").casefold() != PROCESS_NAME.casefold():
                continue
            matches.append(
                {
                    "pid": int(process.info["pid"]),
                    "name": str(process.info.get("name") or ""),
                    "exe": str(process.info.get("exe") or ""),
                    "create_time": float(process.info.get("create_time") or 0.0),
                }
            )
        except (psutil.AccessDenied, psutil.NoSuchProcess, psutil.ZombieProcess):
            continue
    return sorted(matches, key=lambda item: float(item["create_time"]), reverse=True)


def _resolve_target(windows_diagnostics: Any) -> Optional[Dict[str, Any]]:
    try:
        result = windows_diagnostics.resolve_target_preview()
    except Exception as exc:
        if getattr(exc, "code", None) in {"window_not_found", "window_target_not_found"}:
            return None
        raise
    target = result.get("target") if isinstance(result, dict) else None
    return dict(target) if isinstance(target, dict) else None


def _resolve_executable(explicit_path: Optional[str]) -> Path:
    resolved = validate_executable_path(explicit_path, executable_name=PROCESS_NAME)
    if resolved is not None:
        return resolved
    raise StopTaskException(
        "Resonance PC startup failed: 未配置有效的游戏路径，请在设置中选择雷索纳斯.exe。",
        success=False,
    )


def _wait_for_target(windows_diagnostics: Any, timeout_sec: float, launched_pid: Optional[int]) -> Dict[str, Any]:
    deadline = time.monotonic() + max(float(timeout_sec), 0.1)
    last_error: Optional[BaseException] = None
    while time.monotonic() < deadline:
        try:
            target = _resolve_target(windows_diagnostics)
            if target is not None:
                return target
        except BaseException as exc:  # Keep the final diagnostics detail for the task error.
            last_error = exc
        if launched_pid is not None and not psutil.pid_exists(int(launched_pid)):
            raise StopTaskException(
                f"Resonance PC exited before its target window appeared (pid={launched_pid}).",
                success=False,
            )
        time.sleep(0.25)
    detail = f" Last resolver error: {last_error}" if last_error else ""
    raise StopTaskException(
        f"Resonance PC target window did not appear within {float(timeout_sec):.1f}s.{detail}",
        success=False,
    )


def _first_update_button_hit(state: Dict[str, Any]) -> Optional[Dict[str, Any]]:
    hits = (state.get("matched") or {}).get("update") or []
    return hits[0] if hits else None


@action_info(
    name="resonance_pc.enter_main",
    public=True,
    read_only=False,
    timeout=900,
    description="Start Resonance PC when needed and settle on the game main screen.",
)
@requires_services(
    process_manager="plans/aura_base/process_manager",
    windows_diagnostics="plans/aura_base/windows_diagnostics",
    app="plans/aura_base/app",
    ocr="plans/aura_base/ocr",
)
def resonance_pc_enter_main(
    executable_path: Optional[str] = None,
    launch_if_not_running: bool = True,
    window_timeout_sec: float = 90.0,
    max_settle_rounds: int = 300,
    round_interval_sec: float = 1.0,
    click_x: int = 450,
    click_y: int = 660,
    region: Optional[List[int]] = None,
    process_manager: Any = None,
    windows_diagnostics: Any = None,
    app: Any = None,
    ocr: Any = None,
) -> Dict[str, Any]:
    configured_executable = _resolve_executable(executable_path)
    if any(service is None for service in (process_manager, windows_diagnostics, app, ocr)):
        raise RuntimeError("process_manager/windows_diagnostics/app/ocr services are required")
    target = _resolve_target(windows_diagnostics)
    running = _matching_processes()
    launched = False
    launch_result: Optional[Dict[str, Any]] = None
    launched_pid: Optional[int] = None
    if target is None and not running:
        if not bool(launch_if_not_running):
            raise StopTaskException("Resonance PC is not running and launch_if_not_running is false.", success=False)
        launch_result = dict(
            process_manager.start_process(
                identifier=PROCESS_IDENTIFIER,
                executable_path=str(configured_executable),
                cwd=str(configured_executable.parent),
            )
            or {}
        )
        if launch_result.get("status") not in {"success", "already_running"}:
            raise StopTaskException(
                f"Resonance PC launch failed: {launch_result.get('message') or launch_result.get('status') or 'unknown error'}",
                success=False,
            )
        launched_pid = int(launch_result["pid"]) if launch_result.get("pid") is not None else None
        launched = launch_result.get("status") == "success"
        logger.info("[PcEnterMain] detached launch result=%s", launch_result)
    elif running:
        launched_pid = int(running[0]["pid"])

    if target is None:
        target = _wait_for_target(windows_diagnostics, window_timeout_sec, launched_pid)
    target_pid = int(target["pid"]) if target.get("pid") is not None else launched_pid
    logger.info("[PcEnterMain] target ready hwnd=%s pid=%s launched=%s", target.get("hwnd"), target_pid, launched)

    region_tuple = _coerce_region(region)
    max_rounds = max(int(max_settle_rounds), 1)
    interval = max(float(round_interval_sec), 0.0)
    history: List[Dict[str, Any]] = []
    initial_state: Optional[str] = None

    for round_index in range(max_rounds):
        state = _detect_state(app, ocr, region_tuple)
        if initial_state is None:
            initial_state = str(state.get("state") or "unknown")
        entry = _compact_state(state, round_index)
        history.append(entry)
        logger.info(
            "[PcEnterMain] round=%s/%s state=%s main=%s items=%s",
            round_index + 1,
            max_rounds,
            state.get("state"),
            state.get("main"),
            state.get("item_count"),
        )
        if state.get("main"):
            entry["action"] = "complete_main"
            logger.info("[PcEnterMain] main detected; startup task completed")
            return {
                "success": True,
                "status": "completed",
                "reason": None,
                "message": "Resonance PC reached the main screen.",
                "reached_main": True,
                "launched": launched,
                "launch_result": launch_result,
                "pid": target_pid,
                "hwnd": target.get("hwnd"),
                "initial_state": initial_state,
                "final_state": "main",
                "rounds": round_index + 1,
                "history": history[-30:],
            }
        else:
            state_name = str(state.get("state") or "unknown")
            update_hit = _first_update_button_hit(state) if state_name == "update" else None
            if update_hit is not None:
                center = update_hit.get("center") or [click_x, click_y]
                app.click(x=int(center[0]), y=int(center[1]))
                entry["action"] = "click_update"
                logger.info(
                    "[PcEnterMain] clicked update button text=%s at=(%s,%s)",
                    update_hit.get("text"),
                    int(center[0]),
                    int(center[1]),
                )
            else:
                app.click(x=int(click_x), y=int(click_y))
                entry["action"] = "click_other"
                logger.info("[PcEnterMain] clicked fixed point at=(%s,%s) for other state", click_x, click_y)
        if interval > 0:
            time.sleep(interval)

    last_state = history[-1]["state"] if history else "unknown"
    raise StopTaskException(
        f"Resonance PC main screen was not reached within {max_rounds} rounds; last state={last_state}.",
        success=False,
    )


def _process_is_alive(pid: int) -> bool:
    try:
        process = psutil.Process(int(pid))
        return process.is_running() and process.status() != psutil.STATUS_ZOMBIE
    except (psutil.NoSuchProcess, psutil.AccessDenied, psutil.ZombieProcess):
        return False


def _wait_for_process_exit(pid: int, timeout_sec: float) -> bool:
    deadline = time.monotonic() + max(float(timeout_sec), 0.0)
    while time.monotonic() < deadline:
        if not _process_is_alive(pid):
            return True
        time.sleep(0.1)
    return not _process_is_alive(pid)


@action_info(
    name="resonance_pc.close_game",
    public=True,
    read_only=False,
    description="Close the verified Resonance PC target window and optionally terminate it after timeout.",
)
@requires_services(windows_diagnostics="plans/aura_base/windows_diagnostics")
def resonance_pc_close_game(
    graceful_timeout_sec: float = 10.0,
    force_after_timeout: bool = True,
    windows_diagnostics: Any = None,
) -> Dict[str, Any]:
    if windows_diagnostics is None:
        raise RuntimeError("windows_diagnostics service is required")

    target = _resolve_target(windows_diagnostics)
    running = _matching_processes()
    if target is None and not running:
        return {
            "success": True,
            "status": "already_stopped",
            "reason": None,
            "message": "Resonance PC is already stopped.",
            "pid": None,
            "method": "none",
        }

    if target is not None:
        pid = int(target.get("pid") or 0)
        hwnd = int(target.get("hwnd") or 0)
        process_name = str(target.get("process_name") or "")
        if not pid or process_name.casefold() != PROCESS_NAME.casefold():
            raise StopTaskException("Refusing to close an unverified Resonance PC target process.", success=False)
    else:
        if len(running) != 1:
            raise StopTaskException(
                f"Resonance PC close is ambiguous: {len(running)} matching processes exist without a target window.",
                success=False,
            )
        pid = int(running[0]["pid"])
        hwnd = 0

    method = "wm_close"
    if hwnd and win32gui.IsWindow(hwnd):
        win32gui.PostMessage(hwnd, win32con.WM_CLOSE, 0, 0)
        logger.info("[PcCloseGame] posted WM_CLOSE hwnd=%s pid=%s", hwnd, pid)
    if _wait_for_process_exit(pid, graceful_timeout_sec):
        return {
            "success": True,
            "status": "stopped",
            "reason": None,
            "message": "Resonance PC closed.",
            "pid": pid,
            "method": method,
        }

    if not bool(force_after_timeout):
        raise StopTaskException(
            f"Resonance PC did not exit within {float(graceful_timeout_sec):.1f}s after WM_CLOSE.",
            success=False,
        )

    try:
        process = psutil.Process(pid)
        if process.name().casefold() != PROCESS_NAME.casefold():
            raise StopTaskException("Refusing to terminate a PID that no longer belongs to Resonance PC.", success=False)
        process.terminate()
        process.wait(timeout=5.0)
        method = "terminate"
    except psutil.NoSuchProcess:
        pass
    except psutil.TimeoutExpired as exc:
        raise StopTaskException("Resonance PC did not exit after explicit termination.", success=False) from exc
    return {
        "success": True,
        "status": "stopped",
        "reason": None,
        "message": "Resonance PC closed.",
        "pid": pid,
        "method": method,
    }
