"""Runtime preflight guards for fixed-coordinate Resonance PC tasks."""

from __future__ import annotations

import asyncio
from typing import Any, Dict, Optional, Tuple

from packages.aura_core.api import action_info, requires_services
from packages.aura_core.observability.logging.core_logger import logger
from packages.aura_core.scheduler.cancellation import is_current_task_cancel_requested

from ._depart_button_vision import DepartButtonError, probe_depart_button


_REQUIRED_CLIENT_SIZE: Tuple[int, int] = (1280, 720)


class PcClientResolutionError(RuntimeError):
    """Structured error raised when the game client size cannot be accepted."""

    def __init__(self, code: str, message: str, detail: Optional[Dict[str, Any]] = None):
        super().__init__(message)
        self.code = str(code)
        self.message = str(message)
        self.detail = dict(detail or {})

    def __str__(self) -> str:
        return f"{self.code}: {self.message}"

    def to_dict(self) -> Dict[str, Any]:
        return {"code": self.code, "message": self.message, "detail": dict(self.detail)}


def _raise_resolution_error(code: str, message: str, detail: Dict[str, Any]) -> None:
    raise PcClientResolutionError(code=code, message=message, detail=detail)


@action_info(
    name="resonance_pc.require_client_resolution",
    public=True,
    read_only=True,
    description="Require the Resonance PC client area to be exactly 1280x720.",
)
@requires_services(app="plans/aura_base/app")
def resonance_pc_require_client_resolution(app: Any = None) -> Dict[str, Any]:
    if app is None:
        _raise_resolution_error(
            "pc_client_resolution_unavailable",
            "无法读取雷索纳斯游戏客户区尺寸。",
            {"expected": list(_REQUIRED_CLIENT_SIZE), "actual": None, "reason": "missing_app_service"},
        )

    try:
        window_size = app.get_window_size()
    except Exception as exc:
        detail = {
            "expected": list(_REQUIRED_CLIENT_SIZE),
            "actual": None,
            "reason": "window_size_read_failed",
            "exception_type": type(exc).__name__,
            "exception_message": str(exc),
        }
        logger.error(
            "[PcResolutionCheck] result=failed reason=window_size_read_failed "
            "expected=%sx%s exception=%s: %s",
            _REQUIRED_CLIENT_SIZE[0],
            _REQUIRED_CLIENT_SIZE[1],
            type(exc).__name__,
            exc,
        )
        raise PcClientResolutionError(
            code="pc_client_resolution_unavailable",
            message="无法读取雷索纳斯游戏客户区尺寸。",
            detail=detail,
        ) from exc

    if not isinstance(window_size, (list, tuple)) or len(window_size) != 2:
        detail = {
            "expected": list(_REQUIRED_CLIENT_SIZE),
            "actual": list(window_size) if isinstance(window_size, (list, tuple)) else None,
            "reason": "invalid_window_size",
        }
        logger.error(
            "[PcResolutionCheck] result=failed reason=invalid_window_size "
            "actual=%r expected=%sx%s",
            window_size,
            _REQUIRED_CLIENT_SIZE[0],
            _REQUIRED_CLIENT_SIZE[1],
        )
        _raise_resolution_error(
            "pc_client_resolution_unavailable",
            "无法读取雷索纳斯游戏客户区尺寸。",
            detail,
        )

    actual = (int(window_size[0]), int(window_size[1]))
    detail = {"expected": list(_REQUIRED_CLIENT_SIZE), "actual": list(actual)}
    if actual != _REQUIRED_CLIENT_SIZE:
        logger.error(
            "[PcResolutionCheck] result=failed actual=%sx%s expected=%sx%s",
            actual[0],
            actual[1],
            _REQUIRED_CLIENT_SIZE[0],
            _REQUIRED_CLIENT_SIZE[1],
        )
        _raise_resolution_error(
            "pc_client_resolution_mismatch",
            (
                f"游戏客户区当前为 {actual[0]}×{actual[1]}，"
                "Aura Resonance PC 版仅支持 1280×720。"
                "请在游戏设置中切换为窗口模式 1280×720 后重新运行。"
            ),
            detail,
        )

    logger.info(
        "[PcResolutionCheck] result=passed actual=%sx%s expected=%sx%s",
        actual[0],
        actual[1],
        _REQUIRED_CLIENT_SIZE[0],
        _REQUIRED_CLIENT_SIZE[1],
    )
    return {"ok": True, **detail}


@action_info(
    name="resonance_pc.require_trade_start_screen",
    public=True,
    read_only=True,
    description="Require the standard departure button on the city main screen before starting freight.",
)
@requires_services(app="plans/aura_base/app", vision="plans/aura_base/vision")
def resonance_pc_require_trade_start_screen(app: Any = None, vision: Any = None) -> Dict[str, Any]:
    def check_cancelled():
        if is_current_task_cancel_requested():
            raise asyncio.CancelledError("Trade start screen check cancelled")

    match = probe_depart_button(app=app, vision=vision, check_cancelled=check_cancelled,
                                diagnose_oversized=True)
    if match["found"]:
        return {"ok": True, "page_state": "city_main", "match": match}

    oversized = match["oversized_match"]
    logger.warning("[TradeStartScreen] standard_score=%.4f oversized_score=%.4f oversized=%s",
                   match["confidence"], oversized["confidence"], oversized["found"])
    if oversized["found"]:
        raise DepartButtonError(
            "trade_start_hud_oversized",
            "窗口已是 1280×720，但「启程」按钮仍偏大。请完全退出游戏，以 1280×720 窗口模式重新进入站场后再启动跑商。",
            match,
        )
    raise DepartButtonError(
        "trade_start_screen_unconfirmed",
        "未识别到站场主界面右下角的「启程」按钮。请返回可见「启程」的站场主界面后再启动跑商。",
        match,
    )


__all__ = [
    "PcClientResolutionError",
    "resonance_pc_require_client_resolution",
    "resonance_pc_require_trade_start_screen",
]
