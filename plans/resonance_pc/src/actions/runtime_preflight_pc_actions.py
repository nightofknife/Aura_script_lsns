"""Runtime preflight guards for fixed-coordinate Resonance PC tasks."""

from __future__ import annotations

from typing import Any, Dict, Optional, Tuple

from packages.aura_core.api import action_info, requires_services
from packages.aura_core.observability.logging.core_logger import logger


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


__all__ = [
    "PcClientResolutionError",
    "resonance_pc_require_client_resolution",
]
