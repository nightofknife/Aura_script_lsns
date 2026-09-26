"""Read-only probe of the Deep Dive node confirmation card."""

from __future__ import annotations

from typing import Any

from packages.aura_core.api import action_info, requires_services
from packages.aura_core.scheduler.cancellation import is_current_task_cancel_requested
from packages.aura_core.utils.exceptions import StopTaskException

from ._deep_dive_entry_card_vision import classify_entry_card


@action_info(
    name="resonance_pc.deep_dive_entry_card_probe",
    public=True,
    read_only=True,
    timeout=10,
    description="Identify the node confirmation card without clicking or entering it.",
)
@requires_services(app="plans/aura_base/app")
def probe_deep_dive_entry_card(app: Any = None) -> dict:
    if app is None:
        raise RuntimeError("app service is required")
    if is_current_task_cancel_requested():
        raise StopTaskException("识海深潜节点识别已取消。", success=False)
    capture = app.capture()
    if not getattr(capture, "success", False) or getattr(capture, "image", None) is None:
        raise RuntimeError("无法截取识海深潜节点确认卡画面。")
    result = classify_entry_card(capture.image)
    return {"success": True, **result}


__all__ = ["probe_deep_dive_entry_card"]
