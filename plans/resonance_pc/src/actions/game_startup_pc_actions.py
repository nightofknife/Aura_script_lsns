"""Safe placeholders for the not-yet-designed PC game lifecycle actions."""

from __future__ import annotations

from typing import Any, Dict

from packages.aura_core.api import action_info


def _not_implemented(reason: str, message: str) -> Dict[str, Any]:
    return {
        "success": False,
        "status": "not_implemented",
        "reason": reason,
        "message": message,
    }


@action_info(
    name="resonance_pc.enter_main",
    public=True,
    read_only=True,
    description="Placeholder for entering the Resonance PC main screen.",
)
def resonance_pc_enter_main() -> Dict[str, Any]:
    """Report that PC startup/main-screen recovery has not been implemented."""

    return _not_implemented(
        "pc_game_startup_not_implemented",
        "PC 游戏启动与主界面恢复尚未实现。",
    )


@action_info(
    name="resonance_pc.close_game",
    public=True,
    read_only=True,
    description="Placeholder for closing the Resonance PC game.",
)
def resonance_pc_close_game() -> Dict[str, Any]:
    """Report that the PC game shutdown strategy has not been selected."""

    return _not_implemented(
        "pc_game_close_not_implemented",
        "PC 游戏关闭方式尚未确定。",
    )
