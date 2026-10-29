"""Only confirmed sweep and native-stop receipts can hand off the workflow."""
from copy import deepcopy

import pytest

from packages.resonance_gui.logic import battle_continuation_detail


def result(*, planned=3, started=1, won=1, lost=0, swept=2, not_started=0):
    return {
        "schema": "resonance_pc.battle_plan_result.v1", "continue_workflow": True,
        "status": "completed", "stop_reason": None, "structural": None,
        "counts": dict(planned=planned, started=started, won=won, lost=lost,
                       swept=swept, not_started=not_started),
        "cleanup": {"returned_to_main": True, "required": True, "touched_game": True},
    }


@pytest.mark.parametrize("started,won,swept", [(0, 0, 3), (1, 1, 2), (3, 3, 0)])
def test_sweeps_complete_requested_runs_without_fabricating_battle_wins(started, won, swept):
    assert battle_continuation_detail(result(started=started, won=won, swept=swept))[0] == "success"


@pytest.mark.parametrize("field,value", [("swept", 3), ("swept", True), ("swept", -1),
                                          ("started", 3), ("won", 3)])
def test_inconsistent_or_invalid_sweep_statistics_block_workflow(field, value):
    receipt = result()
    receipt["counts"][field] = value
    with pytest.raises(ValueError):
        battle_continuation_detail(receipt)


def test_insufficient_stamina_after_sweeps_can_continue_only_after_main_return():
    receipt = result(planned=4, started=0, won=0, swept=2, not_started=2)
    receipt.update(status="stopped", stop_reason="insufficient_stamina")
    assert battle_continuation_detail(receipt)[0] == "skipped"
    receipt["cleanup"]["returned_to_main"] = False
    with pytest.raises(ValueError):
        battle_continuation_detail(receipt)


def test_native_auto_requires_confirmed_stop_even_after_ordinary_sweep_completion():
    receipt = result()
    receipt["structural"] = {
        "schema": "resonance_pc.structural_native_result.v1", "mode": "native_auto",
        "statistics_complete": False, "observed": {"won": 0, "lost": 0},
        "configured_types": ["echo_buoy", "disordered_roots"],
        "outcome": "completed", "reason": "no_matching_targets",
        "native_stopped": True, "ready_for_next": True, "page_after": "world_scan",
    }
    assert battle_continuation_detail(receipt)[0] == "success"
    for field, value in (("native_stopped", False), ("ready_for_next", False),
                         ("page_after", "battle"), ("statistics_complete", True)):
        incomplete = deepcopy(receipt)
        incomplete["structural"][field] = value
        with pytest.raises(ValueError):
            battle_continuation_detail(incomplete)


def test_explicit_user_cancellation_never_continues_after_sweeps():
    receipt = result()
    receipt.update(status="cancelled", stop_reason="cancelled", continue_workflow=False)
    with pytest.raises(ValueError):
        battle_continuation_detail(receipt)
