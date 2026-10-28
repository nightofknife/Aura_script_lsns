"""Offline completion of the mandatory visible-stock preparation step."""
from packages.resonance_gui.logic import PC_GAME_NAME, PC_PLAYER_DATA_REFRESH_TASK_REF


def complete_prison_products_refresh(window):
    """Deliver a successful prison-only result through normal GUI callbacks."""
    current = window._workflow_current
    assert current["step"] == "refresh_recovery"
    assert current["task_ref"] == PC_PLAYER_DATA_REFRESH_TASK_REF
    assert current["inputs"]["stages"] == ["prison_products"]
    window._active_game_name = PC_GAME_NAME
    window._active_kind = "workflow_task"
    window._on_busy_changed(True)
    window._on_task_finished({
        "status": "success", "cid": "offline-prison-refresh",
        "gui_item": {"task_ref": PC_PLAYER_DATA_REFRESH_TASK_REF, "kind": "workflow_task"},
        "user_data": {"player_data": {
            "prison_products": {
                "success": True, "scope": "visible", "visible_complete": True,
                "returned_to_profile": True, "inventory_complete": False,
                "goods": [], "unresolved": [], "requested_goods": None,
                "refreshed_at": "2026-10-29T00:00:00Z",
            },
            "metadata": {"persisted": True, "executed_stages": ["prison_products"]},
        }},
    })
    window._on_busy_changed(False)
