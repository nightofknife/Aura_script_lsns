"""Settlement preempts the battle reward flow."""


from plans.resonance_pc.src.actions import consciousness_deep_dive_single_run_pc_actions as runtime


def test_battle_settlement_preempts_rewards():
    state = {"phase": "battle_obtain", "status": "running"}
    result = runtime._advance_state(state, {"valid": True, "scene": "settlement", "outcome": "failure"})
    assert result == ("settlement", None)
    assert state["status"] == "completed"


