"""Entry-state automation for the Resonance PC Consciousness Deep Dive mode."""

from __future__ import annotations

from dataclasses import dataclass
import time
from typing import Any, Dict, Optional, Tuple

from packages.aura_core.api import action_info, requires_services
from packages.aura_core.engine import ExecutionEngine
from packages.aura_core.observability.logging.core_logger import logger
from packages.aura_core.scheduler.cancellation import is_current_task_cancel_requested
from packages.aura_core.utils.exceptions import StopTaskException

from ....aura_base.src.actions.input_actions import click as aura_click
from ....aura_base.src.actions.input_actions import drag as aura_drag
from ....aura_base.src.actions.wait_actions import sleep as aura_sleep
from ....aura_base.src.actions.wait_actions import wait_for_image as aura_wait_for_image


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
_DIFFICULTY_DRAG_START = (900, 360)
_DIFFICULTY_DRAG_END = (500, 360)
_REWARD_DISMISS_POINT = (640, 680)


@dataclass(frozen=True)
class _TemplateTarget:
    key: str
    template: str
    region: Region
    threshold: float


_START_DIVE = _TemplateTarget(
    "start_dive",
    "templates/consciousness_deep_dive/start_dive.png",
    (530, 500, 235, 115),
    0.86,
)
_START_GAME = _TemplateTarget(
    "start_game",
    "templates/consciousness_deep_dive/start_game.png",
    (950, 585, 270, 100),
    0.86,
)
_DIFFICULTY_1_SELECTED = _TemplateTarget(
    "difficulty_1_selected",
    "templates/consciousness_deep_dive/difficulty_1_selected.png",
    (120, 90, 340, 330),
    0.88,
)
_DIFFICULTY_1_UNSELECTED = _TemplateTarget(
    "difficulty_1_unselected",
    "templates/consciousness_deep_dive/difficulty_1_unselected.png",
    (80, 90, 570, 350),
    0.95,
)
_STRATEGY_SOURCE = _TemplateTarget(
    "strategy_source_unselected",
    "templates/consciousness_deep_dive/strategy_source_unselected.png",
    (675, 505, 170, 170),
    0.88,
)
_STRATEGY_CONFIRM = _TemplateTarget(
    "strategy_confirm",
    "templates/consciousness_deep_dive/strategy_confirm.png",
    (950, 580, 235, 100),
    0.86,
)
_FORMATION_CONFIRM = _TemplateTarget(
    "formation_confirm",
    "templates/consciousness_deep_dive/formation_confirm.png",
    (960, 565, 320, 150),
    0.86,
)
_BOON_MIDDLE_UNSELECTED = _TemplateTarget(
    "boon_middle_unselected",
    "templates/consciousness_deep_dive/boon_middle_unselected.png",
    (590, 470, 100, 100),
    0.90,
)
_BOON_MIDDLE_SELECTED = _TemplateTarget(
    "boon_middle_selected",
    "templates/consciousness_deep_dive/boon_middle_selected.png",
    (590, 470, 100, 100),
    0.90,
)
_BOON_CONFIRM = _TemplateTarget(
    "boon_confirm",
    "templates/consciousness_deep_dive/boon_confirm.png",
    (550, 595, 210, 100),
    0.88,
)
_REWARD_MARKER = _TemplateTarget(
    "reward_marker",
    "templates/consciousness_deep_dive/reward_marker.png",
    (520, 55, 250, 100),
    0.88,
)
_BOARD_READY = _TemplateTarget(
    "board_ready",
    "templates/consciousness_deep_dive/board_ready.png",
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


async def _ensure_difficulty_one(
    *,
    app: Any,
    vision: Any,
    engine: ExecutionEngine,
) -> Dict[str, Any]:
    started_at = time.monotonic()
    drag_attempts = 0

    while True:
        _check_cancelled()
        selected = await _find_target(
            _DIFFICULTY_1_SELECTED,
            app=app,
            vision=vision,
            engine=engine,
        )
        if bool(getattr(selected, "found", False)):
            selected = await _wait_for_target(
                _DIFFICULTY_1_SELECTED,
                app=app,
                vision=vision,
                engine=engine,
                timeout_sec=_DIFFICULTY_SELECTION_TIMEOUT_SEC,
                stable_scans=2,
                error_code="deep_dive_difficulty_1_selection_not_confirmed",
            )
            result = {
                "step": "ensure_difficulty_1",
                "status": "already_selected" if drag_attempts == 0 else "selected",
                "drag_attempts": drag_attempts,
                "click_attempts": 0,
                "selection_click": None,
                "selected_state": _match_payload(selected, _DIFFICULTY_1_SELECTED),
                "elapsed_ms": int((time.monotonic() - started_at) * 1000),
            }
            logger.info(
                "[DeepDiveEntry] difficulty=1 status=%s drag_attempts=%s elapsed_ms=%s",
                result["status"],
                drag_attempts,
                result["elapsed_ms"],
            )
            return result

        unselected = await _find_target(
            _DIFFICULTY_1_UNSELECTED,
            app=app,
            vision=vision,
            engine=engine,
        )
        if bool(getattr(unselected, "found", False)):
            selection_click = await _click_until_original_absent(
                _DIFFICULTY_1_UNSELECTED,
                app=app,
                vision=vision,
                engine=engine,
            )
            selected = await _wait_for_target(
                _DIFFICULTY_1_SELECTED,
                app=app,
                vision=vision,
                engine=engine,
                timeout_sec=_DIFFICULTY_SELECTION_TIMEOUT_SEC,
                stable_scans=2,
                error_code="deep_dive_difficulty_1_selection_not_confirmed",
            )
            result = {
                "step": "ensure_difficulty_1",
                "status": "selected",
                "drag_attempts": drag_attempts,
                "click_attempts": selection_click["click_attempts"],
                "selection_click": selection_click,
                "selected_state": _match_payload(selected, _DIFFICULTY_1_SELECTED),
                "elapsed_ms": int((time.monotonic() - started_at) * 1000),
            }
            logger.info(
                "[DeepDiveEntry] difficulty=1 status=selected drag_attempts=%s "
                "click_attempts=%s elapsed_ms=%s",
                drag_attempts,
                selection_click["click_attempts"],
                result["elapsed_ms"],
            )
            return result

        elapsed_sec = time.monotonic() - started_at
        if (
            elapsed_sec >= _DIFFICULTY_SEARCH_TIMEOUT_SEC
            or drag_attempts >= _DIFFICULTY_MAX_DRAGS
        ):
            raise ConsciousnessDeepDiveError(
                "deep_dive_difficulty_1_not_found",
                "向左拖动后仍未找到难度Ⅰ。",
                {
                    "drag_attempts": drag_attempts,
                    "elapsed_ms": int(elapsed_sec * 1000),
                    "selected": _match_payload(selected, _DIFFICULTY_1_SELECTED),
                    "unselected": _match_payload(
                        unselected,
                        _DIFFICULTY_1_UNSELECTED,
                    ),
                },
            )

        aura_drag(
            app=app,
            start_x=_DIFFICULTY_DRAG_START[0],
            start_y=_DIFFICULTY_DRAG_START[1],
            end_x=_DIFFICULTY_DRAG_END[0],
            end_y=_DIFFICULTY_DRAG_END[1],
            duration=_DIFFICULTY_DRAG_DURATION_SEC,
            hold_before_release_sec=_DIFFICULTY_DRAG_HOLD_SEC,
        )
        drag_attempts += 1
        logger.info(
            "[DeepDiveEntry] difficulty=1 drag_attempt=%s start=%s end=%s "
            "duration_sec=%.1f hold_before_release_sec=%.1f",
            drag_attempts,
            _DIFFICULTY_DRAG_START,
            _DIFFICULTY_DRAG_END,
            _DIFFICULTY_DRAG_DURATION_SEC,
            _DIFFICULTY_DRAG_HOLD_SEC,
        )
        await aura_sleep(_DIFFICULTY_DRAG_SETTLE_SEC)


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
) -> Dict[str, Any]:
    started_at = time.monotonic()
    click_result = await _click_until_original_absent(
        control,
        app=app,
        vision=vision,
        engine=engine,
        fixed_click_point=fixed_click_point,
    )
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


@action_info(
    name="resonance_pc.consciousness_deep_dive_enter_stage",
    public=True,
    read_only=False,
    timeout=300,
    description="Enter the Consciousness Deep Dive board using fixed strategy and middle boon selection.",
)
@requires_services(
    app="plans/aura_base/app",
    vision="plans/aura_base/vision",
)
async def resonance_pc_consciousness_deep_dive_enter_stage(
    app: Any = None,
    vision: Any = None,
    engine: ExecutionEngine | None = None,
) -> Dict[str, Any]:
    if app is None or vision is None or engine is None:
        raise RuntimeError("app/vision services and engine are required")

    started_at = time.monotonic()
    transitions: list[Dict[str, Any]] = []
    transitions.append(
        await _run_transition(
            "start_dive",
            _START_DIVE,
            _START_GAME,
            app=app,
            vision=vision,
            engine=engine,
        )
    )
    transitions.append(
        await _ensure_difficulty_one(
            app=app,
            vision=vision,
            engine=engine,
        )
    )
    transitions.append(
        await _run_transition(
            "start_game",
            _START_GAME,
            _STRATEGY_SOURCE,
            app=app,
            vision=vision,
            engine=engine,
        )
    )
    transitions.append(
        await _run_transition(
            "select_strategy",
            _STRATEGY_SOURCE,
            _STRATEGY_CONFIRM,
            app=app,
            vision=vision,
            engine=engine,
        )
    )
    transitions.append(
        await _run_transition(
            "confirm_strategy",
            _STRATEGY_CONFIRM,
            _FORMATION_CONFIRM,
            app=app,
            vision=vision,
            engine=engine,
        )
    )
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
    transitions.append(
        await _run_transition(
            "confirm_boon",
            _BOON_CONFIRM,
            _REWARD_MARKER,
            app=app,
            vision=vision,
            engine=engine,
        )
    )
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
        "strategy": "source",
        "boon_slot": 2,
        "transitions": transitions,
        "elapsed_ms": int((time.monotonic() - started_at) * 1000),
    }


__all__ = [
    "ConsciousnessDeepDiveError",
    "resonance_pc_consciousness_deep_dive_enter_stage",
]
