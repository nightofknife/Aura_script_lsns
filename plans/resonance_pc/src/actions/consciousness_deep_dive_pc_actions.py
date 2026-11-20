"""Entry-state automation for the Resonance PC Consciousness Deep Dive mode."""

from __future__ import annotations

from dataclasses import dataclass
import time
from typing import Any, Dict, Optional, Tuple
from uuid import uuid4

from packages.aura_core.api import action_info, requires_services
from packages.aura_core.engine import ExecutionEngine
from packages.aura_core.observability.logging.core_logger import logger
from packages.aura_core.scheduler.cancellation import is_current_task_cancel_requested
from packages.aura_core.scheduler.utils import resolve_base_path
from packages.aura_core.utils.exceptions import StopTaskException

from ....aura_base.src.actions.input_actions import click as aura_click
from ....aura_base.src.actions.input_actions import drag as aura_drag
from ....aura_base.src.actions.wait_actions import sleep as aura_sleep
from ....aura_base.src.actions.wait_actions import wait_for_image as aura_wait_for_image
from ._deep_dive_opening_options import DIVE_DEPTHS, OPENING_STRATEGIES, validate_opening_options
from ._deep_dive_opening_vision import OpeningChoice, OpeningFrame


Region = Tuple[int, int, int, int]
Point = Tuple[int, int]

_CONTROL_WAIT_TIMEOUT_SEC = 20.0
_CLICK_EFFECT_TIMEOUT_SEC = 12.0
_DIFFICULTY_SEARCH_TIMEOUT_SEC = 20.0
_DIFFICULTY_SELECTION_TIMEOUT_SEC = 5.0
_DIFFICULTY_MAX_DRAGS = 12
_NEXT_STATE_TIMEOUT_SEC = 45.0
_BOARD_READY_TIMEOUT_SEC = 60.0
_POLL_INTERVAL_SEC = 0.2
_CLICK_RECHECK_DELAY_SEC = 0.3
_DIFFICULTY_DRAG_SETTLE_SEC = 0.3
_DIFFICULTY_DRAG_DURATION_SEC = 0.5
_DIFFICULTY_DRAG_HOLD_SEC = 0.4
_STABLE_CENTER_TOLERANCE_PX = 2
# Lower depths are left of the current selection, so drag the strip right.
_DIFFICULTY_DRAG_START = (500, 360)
_DIFFICULTY_DRAG_END = (900, 360)
_REWARD_DISMISS_POINT = (640, 680)


@dataclass(frozen=True)
class _TemplateTarget:
    key: str
    template: str
    region: Region
    threshold: float


_START_DIVE = _TemplateTarget(
    "start_dive",
    "templates/deep_dive/consciousness_deep_dive/start_dive.png",
    (530, 500, 235, 115),
    0.86,
)
_FORMATION_CONFIRM = _TemplateTarget(
    "formation_confirm",
    "templates/deep_dive/consciousness_deep_dive/formation_confirm.png",
    (960, 565, 320, 150),
    0.86,
)
_BOON_MIDDLE_UNSELECTED = _TemplateTarget(
    "boon_middle_unselected",
    "templates/deep_dive/consciousness_deep_dive/boon_middle_unselected.png",
    (590, 470, 100, 100),
    0.90,
)
_BOON_MIDDLE_SELECTED = _TemplateTarget(
    "boon_middle_selected",
    "templates/deep_dive/consciousness_deep_dive/boon_middle_selected.png",
    (590, 470, 100, 100),
    0.90,
)
_BOON_CONFIRM = _TemplateTarget(
    "boon_confirm",
    "templates/deep_dive/consciousness_deep_dive/boon_confirm.png",
    (550, 595, 210, 100),
    0.88,
)
_REWARD_MARKER = _TemplateTarget(
    "reward_marker",
    "templates/deep_dive/consciousness_deep_dive/reward_marker.png",
    (520, 55, 250, 100),
    0.88,
)
_BOARD_READY = _TemplateTarget(
    "board_ready",
    "templates/deep_dive/consciousness_deep_dive/board_ready.png",
    (20, 190, 280, 90),
    0.88,
)


class ConsciousnessDeepDiveError(RuntimeError):
    """Structured expected failure raised by the Deep Dive entry flow."""

    def __init__(self, code: str, message: str, detail: Optional[Dict[str, Any]] = None):
        super().__init__(message)
        self.code = str(code)
        self.message = str(message)
        self.detail = dict(detail or {})

    def __str__(self) -> str:
        return f"{self.code}: {self.message}"

    def to_dict(self) -> Dict[str, Any]:
        return {"code": self.code, "message": self.message, "detail": dict(self.detail)}


def _check_cancelled() -> None:
    if is_current_task_cancel_requested():
        raise StopTaskException("识海深潜任务已取消。", success=False)


def _match_payload(match: Any, target: _TemplateTarget) -> Dict[str, Any]:
    center = getattr(match, "center_point", None)
    payload: Dict[str, Any] = {
        "key": target.key,
        "template": target.template,
        "region": list(target.region),
        "threshold": target.threshold,
        "found": bool(getattr(match, "found", False)),
        "confidence": float(getattr(match, "confidence", 0.0) or 0.0),
    }
    if isinstance(center, (list, tuple)) and len(center) == 2:
        payload["center"] = [int(center[0]), int(center[1])]
    return payload


async def _find_target(
    target: _TemplateTarget,
    *,
    app: Any,
    vision: Any,
    engine: ExecutionEngine,
) -> Any:
    return await aura_wait_for_image(
        app=app,
        vision=vision,
        engine=engine,
        template=target.template,
        timeout=0.0,
        interval=0.0,
        region=target.region,
        threshold=target.threshold,
        use_grayscale=True,
        stable_scans=1,
        stable_center_tolerance_px=_STABLE_CENTER_TOLERANCE_PX,
    )


async def _wait_for_target(
    target: _TemplateTarget,
    *,
    app: Any,
    vision: Any,
    engine: ExecutionEngine,
    timeout_sec: float,
    stable_scans: int,
    error_code: str,
) -> Any:
    _check_cancelled()
    match = await aura_wait_for_image(
        app=app,
        vision=vision,
        engine=engine,
        template=target.template,
        timeout=max(float(timeout_sec), 0.1),
        interval=_POLL_INTERVAL_SEC,
        region=target.region,
        threshold=target.threshold,
        use_grayscale=True,
        stable_scans=max(int(stable_scans), 1),
        stable_center_tolerance_px=_STABLE_CENTER_TOLERANCE_PX,
    )
    if not bool(getattr(match, "found", False)):
        raise ConsciousnessDeepDiveError(
            error_code,
            f"未能确认识海深潜界面控件：{target.key}",
            {"target": _match_payload(match, target), "timeout_sec": timeout_sec},
        )
    return match


async def _click_until_original_absent(
    target: _TemplateTarget,
    *,
    app: Any,
    vision: Any,
    engine: ExecutionEngine,
    fixed_click_point: Optional[Point] = None,
) -> Dict[str, Any]:
    current = await _wait_for_target(
        target,
        app=app,
        vision=vision,
        engine=engine,
        timeout_sec=_CONTROL_WAIT_TIMEOUT_SEC,
        stable_scans=2,
        error_code="deep_dive_control_not_found",
    )
    started_at = time.monotonic()
    attempts = 0
    clicks: list[Dict[str, Any]] = []

    while True:
        _check_cancelled()
        elapsed_sec = time.monotonic() - started_at
        if elapsed_sec >= _CLICK_EFFECT_TIMEOUT_SEC:
            raise ConsciousnessDeepDiveError(
                "deep_dive_click_not_effective",
                f"重复点击后控件仍未消失：{target.key}",
                {
                    "target": _match_payload(current, target),
                    "click_attempts": attempts,
                    "clicks": clicks,
                    "elapsed_ms": int(elapsed_sec * 1000),
                },
            )

        if fixed_click_point is None:
            center = getattr(current, "center_point", None)
            if not isinstance(center, (list, tuple)) or len(center) != 2:
                raise ConsciousnessDeepDiveError(
                    "deep_dive_control_not_clickable",
                    f"模板没有可用点击中心：{target.key}",
                    {"target": _match_payload(current, target)},
                )
            x, y = int(center[0]), int(center[1])
        else:
            x, y = int(fixed_click_point[0]), int(fixed_click_point[1])

        attempts += 1
        aura_click(app=app, x=x, y=y)
        await aura_sleep(_CLICK_RECHECK_DELAY_SEC)
        recheck = await _find_target(
            target,
            app=app,
            vision=vision,
            engine=engine,
        )
        click_record = {
            "attempt": attempts,
            "point": {"x": x, "y": y},
            "visible_after_recheck": bool(getattr(recheck, "found", False)),
            "confidence_after_recheck": float(getattr(recheck, "confidence", 0.0) or 0.0),
        }
        clicks.append(click_record)
        logger.info(
            "[DeepDiveEntry] control=%s attempt=%s visible_after_300ms=%s confidence=%.4f",
            target.key,
            attempts,
            click_record["visible_after_recheck"],
            click_record["confidence_after_recheck"],
        )
        if not bool(getattr(recheck, "found", False)):
            return {
                "control": target.key,
                "click_attempts": attempts,
                "clicks": clicks,
                "elapsed_ms": int((time.monotonic() - started_at) * 1000),
                "last_recheck": _match_payload(recheck, target),
            }
        current = recheck


async def _opening_frame(app: Any) -> OpeningFrame | None:
    capture = await app.capture_async()
    if not capture.success or capture.image is None or capture.image.shape != (720, 1280, 3):
        return None
    return OpeningFrame(capture.image)


def _same_choice(previous: OpeningChoice | None, current: OpeningChoice) -> bool:
    return (previous is not None and previous.state == current.state
            and all(abs(a - b) <= _STABLE_CENTER_TOLERANCE_PX
                    for a, b in zip(previous.center, current.center)))


async def _open_depth_page(app: Any, vision: Any, engine: ExecutionEngine) -> dict[str, Any]:
    match = await _wait_for_target(_START_DIVE, app=app, vision=vision, engine=engine,
                                   timeout_sec=_CONTROL_WAIT_TIMEOUT_SEC, stable_scans=2,
                                   error_code="deep_dive_start_dive_not_found")
    _check_cancelled()
    aura_click(app=app, x=match.center_point[0], y=match.center_point[1])
    deadline = time.monotonic() + _NEXT_STATE_TIMEOUT_SEC
    previous = None
    returned_from = []
    while time.monotonic() < deadline:
        _check_cancelled()
        frame = await _opening_frame(app)
        scene = None
        if frame is not None:
            if frame.depth_page():
                scene = "depth"
            elif frame.strategy_page():
                scene = "strategy"
            elif frame.formation_page():
                scene = "formation"
        if scene is not None and scene == previous:
            if scene == "depth":
                logger.info("[DeepDiveEntry] depth page ready returned_from=%s", returned_from)
                return {"step": "start_dive", "returned_from_draft": returned_from}
            # Start may reopen a saved pre-dive draft. Back out to apply both requested settings.
            back = frame.back() if scene not in returned_from else None
            if back is not None:
                aura_click(app=app, x=back[0], y=back[1])
                returned_from.append(scene)
        previous = scene
        await aura_sleep(_POLL_INTERVAL_SEC)
    raise ConsciousnessDeepDiveError("deep_dive_depth_page_unconfirmed", "未能进入选择下潜深度页面。",
                                     {"returned_from_draft": returned_from})


async def _start_at_depth(app: Any, depth: int) -> dict[str, Any]:
    label = DIVE_DEPTHS[depth - 1]
    deadline = time.monotonic() + _DIFFICULTY_SEARCH_TIMEOUT_SEC
    previous = None
    drags = 0
    clicked = False
    while time.monotonic() < deadline:
        _check_cancelled()
        frame = await _opening_frame(app)
        if frame is None or not frame.depth_page():
            previous = None
            await aura_sleep(_POLL_INTERVAL_SEC)
            continue
        depths = frame.depths()
        target = depths.get(depth)
        if target is None and not clicked and drags < _DIFFICULTY_MAX_DRAGS:
            start, end = _DIFFICULTY_DRAG_START, _DIFFICULTY_DRAG_END
            if depths and depth > max(depths):
                start, end = end, start
            aura_drag(app=app, start_x=start[0], start_y=start[1], end_x=end[0], end_y=end[1],
                      duration=_DIFFICULTY_DRAG_DURATION_SEC, hold_before_release_sec=_DIFFICULTY_DRAG_HOLD_SEC)
            drags += 1
            previous = None
            await aura_sleep(_DIFFICULTY_DRAG_SETTLE_SEC)
            continue
        if target is not None and _same_choice(previous, target):
            if target.state == "locked":
                raise ConsciousnessDeepDiveError("deep_dive_depth_locked", f"深度{label}尚未解锁。")
            if target.state == "selected":
                start_button = frame.start_game()
                if start_button is not None:
                    aura_click(app=app, x=start_button[0], y=start_button[1])
                    logger.info("[DeepDiveEntry] depth=%s confirmed center=%s drags=%s", depth, target.center, drags)
                    return {"step": "start_game", "dive_depth": depth, "selected_center": target.center,
                            "drag_attempts": drags, "click_attempts": int(clicked)}
            if target.state == "available" and not clicked:
                aura_click(app=app, x=target.center[0], y=target.center[1])
                clicked = True
                deadline = time.monotonic() + _DIFFICULTY_SELECTION_TIMEOUT_SEC
        previous = target
        await aura_sleep(_POLL_INTERVAL_SEC)
    raise ConsciousnessDeepDiveError(
        "deep_dive_depth_not_confirmed", f"未能确认已选中深度{label}，无法开始游戏。",
        {"drag_attempts": drags, "selection_clicked": clicked},
    )


async def _confirm_opening_strategy(app: Any, strategy: str) -> dict[str, Any]:
    label = OPENING_STRATEGIES[strategy]
    deadline = time.monotonic() + _NEXT_STATE_TIMEOUT_SEC
    previous = None
    clicked = False
    while time.monotonic() < deadline:
        _check_cancelled()
        frame = await _opening_frame(app)
        target = frame.strategy(strategy) if frame is not None and frame.strategy_page() else None
        if target is not None and _same_choice(previous, target):
            if target.state == "selected":
                confirm = frame.confirm_strategy()
                if confirm is not None:
                    aura_click(app=app, x=confirm[0], y=confirm[1])
                    logger.info("[DeepDiveEntry] opening_strategy=%s confirmed center=%s", strategy, target.center)
                    return {"step": "confirm_strategy", "opening_strategy": strategy,
                            "selected_center": target.center, "click_attempts": int(clicked)}
            if target.state == "available" and not clicked:
                aura_click(app=app, x=target.center[0], y=target.center[1])
                clicked = True
                deadline = time.monotonic() + _DIFFICULTY_SELECTION_TIMEOUT_SEC
        previous = target
        await aura_sleep(_POLL_INTERVAL_SEC)
    reason = f"未能确认已选中{label}。" if clicked else f"未找到可选的{label}。"
    raise ConsciousnessDeepDiveError("deep_dive_opening_strategy_not_confirmed", reason)


async def _save_opening_failure(app: Any) -> str:
    try:
        capture = await app.capture_async()
        if capture.success and capture.image is not None:
            path = resolve_base_path() / "logs" / "deep_dive_loop" / uuid4().hex / "entry_blocked.png"
            path.parent.mkdir(parents=True, exist_ok=True)
            capture.save(str(path))
            if path.is_file():
                return str(path)
    except Exception as exc:
        logger.warning("[DeepDiveEntry] Could not save blocked entry frame: %s", exc)
    return ""


async def _run_transition(
    step: str,
    control: _TemplateTarget,
    next_state: _TemplateTarget,
    *,
    app: Any,
    vision: Any,
    engine: ExecutionEngine,
    fixed_click_point: Optional[Point] = None,
    next_timeout_sec: float = _NEXT_STATE_TIMEOUT_SEC,
    next_stable_scans: int = 2,
    next_error_code: Optional[str] = None,
    board_also_ready: bool = False,
) -> Dict[str, Any]:
    started_at = time.monotonic()
    click_result = await _click_until_original_absent(
        control,
        app=app,
        vision=vision,
        engine=engine,
        fixed_click_point=fixed_click_point,
    )
    if board_also_ready:
        next_state, next_match = await _wait_for_boon_destination(app=app, vision=vision, engine=engine)
    else:
        next_match = await _wait_for_target(
            next_state,
            app=app,
            vision=vision,
            engine=engine,
            timeout_sec=next_timeout_sec,
            stable_scans=next_stable_scans,
            error_code=next_error_code
            or (
                "deep_dive_board_not_ready"
                if next_state.key == _BOARD_READY.key
                else "deep_dive_next_state_timeout"
            ),
        )
    result = {
        "step": step,
        **click_result,
        "next_state": _match_payload(next_match, next_state),
        "elapsed_ms": int((time.monotonic() - started_at) * 1000),
    }
    logger.info(
        "[DeepDiveEntry] transition=%s control=%s next_state=%s attempts=%s elapsed_ms=%s",
        step,
        control.key,
        next_state.key,
        result["click_attempts"],
        result["elapsed_ms"],
    )
    return result


async def _wait_for_boon_destination(*, app: Any, vision: Any, engine: ExecutionEngine):
    """After confirming the boon, accept either its announcement or a ready board."""
    deadline = time.monotonic() + _BOARD_READY_TIMEOUT_SEC
    previous_target = None
    previous_center = None
    consecutive = 0
    while time.monotonic() < deadline:
        _check_cancelled()
        for target, required in ((_REWARD_MARKER, 2), (_BOARD_READY, 3)):
            match = await _find_target(target, app=app, vision=vision, engine=engine)
            center = getattr(match, "center_point", None)
            if not getattr(match, "found", False) or not isinstance(center, (list, tuple)) or len(center) != 2:
                continue
            if (target == previous_target and previous_center is not None
                    and all(abs(a - b) <= _STABLE_CENTER_TOLERANCE_PX for a, b in zip(center, previous_center))):
                consecutive += 1
            else:
                consecutive = 1
            previous_target, previous_center = target, center
            if consecutive >= required:
                return target, match
            break
        else:
            previous_target, previous_center, consecutive = None, None, 0
        await aura_sleep(_POLL_INTERVAL_SEC)
    raise ConsciousnessDeepDiveError(
        "deep_dive_board_not_ready",
        "确认开局增益后未能识别授奖页或玩家棋盘。",
        {"timeout_sec": _BOARD_READY_TIMEOUT_SEC},
    )


@action_info(
    name="resonance_pc.consciousness_deep_dive_enter_stage",
    public=True,
    read_only=False,
    timeout=300,
    description="Enter Deep Dive with the requested depth and opening strategy, then select the middle boon.",
)
@requires_services(
    app="plans/aura_base/app",
    vision="plans/aura_base/vision",
)
async def resonance_pc_consciousness_deep_dive_enter_stage(
    app: Any = None,
    vision: Any = None,
    engine: ExecutionEngine | None = None,
    dive_depth: int = 1,
    opening_strategy: str = "source",
) -> Dict[str, Any]:
    if app is None or vision is None or engine is None:
        raise RuntimeError("app/vision services and engine are required")

    try:
        dive_depth, opening_strategy = validate_opening_options(dive_depth, opening_strategy)
    except ValueError as exc:
        return {"success": False, "status": "blocked", "reason": str(exc),
                "reason_code": "deep_dive_opening_options_invalid"}

    try:
        return await _enter_stage(app, vision, engine, dive_depth, opening_strategy)
    except ConsciousnessDeepDiveError as exc:
        logger.warning("[DeepDiveEntry] depth=%s opening_strategy=%s blocked: %s", dive_depth, opening_strategy, exc)
        return {
            "success": False,
            "status": "blocked",
            "reason": exc.message,
            "reason_code": exc.code,
            "detail": exc.detail,
            "last_frame": await _save_opening_failure(app),
            "dive_depth": dive_depth,
            "opening_strategy": opening_strategy,
        }


async def _enter_stage(app: Any, vision: Any, engine: ExecutionEngine, dive_depth: int,
                       opening_strategy: str) -> Dict[str, Any]:
    started_at = time.monotonic()
    transitions: list[Dict[str, Any]] = []
    transitions.append(await _open_depth_page(app, vision, engine))
    transitions.append(await _start_at_depth(app, dive_depth))
    transitions.append(await _confirm_opening_strategy(app, opening_strategy))
    transitions.append(
        await _run_transition(
            "confirm_formation",
            _FORMATION_CONFIRM,
            _BOON_MIDDLE_UNSELECTED,
            app=app,
            vision=vision,
            engine=engine,
        )
    )
    transitions.append(
        await _run_transition(
            "select_middle_boon",
            _BOON_MIDDLE_UNSELECTED,
            _BOON_MIDDLE_SELECTED,
            app=app,
            vision=vision,
            engine=engine,
            next_error_code="deep_dive_boon_selection_not_confirmed",
        )
    )
    boon = await _run_transition(
        "confirm_boon",
        _BOON_CONFIRM,
        _REWARD_MARKER,
        app=app,
        vision=vision,
        engine=engine,
        board_also_ready=True,
    )
    transitions.append(boon)
    if boon["next_state"]["key"] == _REWARD_MARKER.key:
        transitions.append(
            await _run_transition(
                "dismiss_reward",
                _REWARD_MARKER,
                _BOARD_READY,
                app=app,
                vision=vision,
                engine=engine,
                fixed_click_point=_REWARD_DISMISS_POINT,
                next_timeout_sec=_BOARD_READY_TIMEOUT_SEC,
                next_stable_scans=3,
            )
        )
    return {
        "success": True,
        "status": "completed",
        "stage": "enter_stage",
        "page_state": "deep_dive_board",
        "dive_depth": dive_depth,
        "opening_strategy": opening_strategy,
        "strategy": opening_strategy,
        "boon_slot": 2,
        "transitions": transitions,
        "elapsed_ms": int((time.monotonic() - started_at) * 1000),
    }


__all__ = [
    "ConsciousnessDeepDiveError",
    "resonance_pc_consciousness_deep_dive_enter_stage",
]
