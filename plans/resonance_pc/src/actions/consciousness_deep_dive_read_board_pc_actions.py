"""Read the live Deep Dive model through the plan-owned bridge."""

from packages.aura_core.api import action_info, requires_services
from ....aura_base.src.platform.contracts import TargetRuntimeError


@action_info(name="resonance_pc.require_deep_dive_bridge", public=True, read_only=True,
             description="Require the input bridge before running a Deep Dive task.")
@requires_services(bridge="resonance_pc_input_bridge")
def require_deep_dive_bridge(require_live: bool = True, require_turn: bool = False, bridge=None,
                             require_controls: bool = False, require_actions: bool = False,
                             require_choices: bool = False):
    if not bridge.enabled:
        raise TargetRuntimeError("input_bridge_required", "识海深潜需要桥接输入器。请在设置中启用并保存「使用桥接输入器」。")
    capabilities = bridge.ensure_ready() if require_live else {}
    if require_live and not capabilities.get("deep_dive_board"):
        raise TargetRuntimeError("deep_dive_board_unsupported", "当前桥接宿主未提供识海深潜棋盘直读。")
    if require_turn and not capabilities.get("deep_dive_turn"):
        raise TargetRuntimeError("deep_dive_turn_unavailable", "当前桥接宿主未提供识海深潜回合和行动额度。")
    if require_controls and not capabilities.get("deep_dive_controls"):
        raise TargetRuntimeError("deep_dive_controls_unavailable", "当前桥接宿主未提供识海深潜操作映射。")
    if require_actions and not capabilities.get("deep_dive_actions"):
        raise TargetRuntimeError("deep_dive_action_unsupported", "当前桥接宿主未提供识海深潜移动和层转操作。")
    if require_choices and not capabilities.get("deep_dive_choices"):
        raise TargetRuntimeError("deep_dive_choice_unsupported", "当前桥接宿主未提供识海深潜事件选项。")
    return True


@action_info(name="resonance_pc.read_consciousness_deep_dive_board", public=True, read_only=True,
             description="Read six faces, node types and object positions from the live Deep Dive board.")
@requires_services(bridge="resonance_pc_input_bridge")
def read_consciousness_deep_dive_board(bridge=None) -> dict:
    return bridge.read_deep_dive_board()
