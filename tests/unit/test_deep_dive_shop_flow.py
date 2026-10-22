"""Shop entry/back handling never buys and only commits a verified return."""


from plans.resonance_pc.src.actions import consciousness_deep_dive_single_run_pc_actions as runtime


def test_settlement_interrupts_shop_return():
    state = {"phase": "shop_return", "status": "running"}
    assert runtime._advance_state(state, {"scene": "settlement", "outcome": "failure"}) == ("settlement", None)
    assert state["status"] == "completed"
