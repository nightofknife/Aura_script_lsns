"""Exercise the real daily dispatcher loop with an isolated execution facade."""
import asyncio
from copy import deepcopy
from types import SimpleNamespace

import pytest

from packages.resonance_shared.battle_control import request_stop
from plans.resonance_pc.src.actions import _battle_dispatch_runtime as runtime


def ordinary(outcome):
    return {"outcome": outcome, "battle_started": outcome in {"victory", "defeat"},
            "ready_for_next": True, "page_after": "battle_preparation", "branch": outcome}


def native(*, stopped=True):
    return {"schema": "resonance_pc.structural_native_result.v1", "mode": "native_auto",
            "outcome": "completed", "reason": "no_matching_targets",
            "configured_types": ["echo_buoy", "disordered_roots"],
            "statistics_complete": False, "observed": {"won": 0, "lost": 0},
            "native_stopped": stopped, "ready_for_next": stopped, "page_after": "world_scan"}


class Execution:
    def __init__(self, outcomes=(), *, structural=None, stop_after=None):
        self.outcomes = iter(outcomes)
        self.structural = structural
        self.stop_after = stop_after
        self.calls = []
        self.ordinary_count = 0

    def next_ordinary(self):
        self.ordinary_count += 1
        result = ordinary(next(self.outcomes))
        if self.ordinary_count == self.stop_after:
            assert request_stop("isolated-battle")
        return result

    async def call_action(self, name, params, **kwargs):
        self.calls.append(("action", name, deepcopy(params)))
        if name in {"resonance_pc.normalize_battle_jobs", "resonance_pc.validate_battle_jobs"}:
            return {"normalized_jobs": deepcopy(params["jobs"])}
        if name == "resonance_pc.try_battle_sweep":
            return self.next_ordinary()
        if name == "resonance_pc.wait_and_click_back_button":
            return {"already_at_target": True}
        if name == "resonance_pc.run_structural_exploration":
            assert self.structural is not None, "unexpected structural start"
            return deepcopy(self.structural)
        raise AssertionError(f"unexpected action {name}")

    async def run_task(self, name, inputs, **kwargs):
        self.calls.append(("task", name.rsplit(":", 1)[-1], deepcopy(inputs)))
        if name.endswith((":auto_battle_enter_terminal_pc", ":auto_battle_enter_category_pc")):
            return {"entered": True}
        if name.endswith(":auto_battle_gp_action_summary_run_group_pc"):
            return self.next_ordinary()
        raise AssertionError(f"unexpected task {name}")


def setup(monkeypatch, execution, *, count=3, structural=True):
    job = {"route_id": "ordinary-test", "run_count": count,
           "main_category": "gp", "gp_subcategory": "action_summary",
           "gp_group_name": "group", "gp_stage_name": "stage", "formation_index": 2}
    plan = {"effective_date": "2026-10-29", "weekday": "thu", "jobs": [job] if count else [],
            "structural_types": ["echo_buoy", "disordered_roots"] if structural else []}
    monkeypatch.setattr(runtime, "resolve_battle_plan", lambda **kwargs: deepcopy(plan))
    monkeypatch.setattr(runtime, "_load_catalog", lambda: {})
    context = SimpleNamespace(data={"cid": "isolated-battle"})
    engine = SimpleNamespace(root_context=context, bind_execution=lambda ctx: execution)
    return context, engine


def run(context, engine):
    return asyncio.run(runtime.execute_battle_plan(context=context, engine=engine))


def names(execution, name):
    return [row for row in execution.calls if row[1] == name]


def test_consecutive_sweeps_reuse_stage_and_forward_root_control_and_formation(monkeypatch):
    execution = Execution(["swept"] * 3)
    context, engine = setup(monkeypatch, execution, structural=False)
    result = run(context, engine)
    assert result["status"] == "completed" and result["continue_workflow"] is True
    assert result["counts"] == {"planned": 3, "started": 0, "won": 0, "lost": 0, "swept": 3, "not_started": 0}
    assert len(names(execution, "auto_battle_enter_terminal_pc")) == 1
    assert len(names(execution, "auto_battle_enter_category_pc")) == 1
    initial = names(execution, "auto_battle_gp_action_summary_run_group_pc")
    assert len(initial) == 1 and initial[0][2]["control_cid"] == "isolated-battle"
    repeats = names(execution, "resonance_pc.try_battle_sweep")
    assert len(repeats) == 2
    assert all(row[2] == {"capture_count": None, "formation_index": 2,
                          "control_cid": "isolated-battle"} for row in repeats)
    assert len(names(execution, "resonance_pc.wait_and_click_back_button")) == 1


def test_sweep_then_insufficient_stamina_skips_remaining_runs_and_structural(monkeypatch):
    execution = Execution(["swept", "insufficient_stamina"])
    context, engine = setup(monkeypatch, execution)
    result = run(context, engine)
    assert (result["status"], result["stop_reason"], result["continue_workflow"]) == (
        "stopped", "insufficient_stamina", True)
    assert result["counts"] == {"planned": 3, "started": 0, "won": 0, "lost": 0, "swept": 1, "not_started": 2}
    assert execution.ordinary_count == 2 and result["structural"] is None
    assert not names(execution, "resonance_pc.run_structural_exploration")
    assert result["cleanup"]["returned_to_main"] is True


def test_defeat_ends_battle_step_and_preserves_following_workflow(monkeypatch):
    execution = Execution(["defeat"])
    context, engine = setup(monkeypatch, execution)
    result = run(context, engine)
    assert (result["status"], result["stop_reason"], result["continue_workflow"]) == ("stopped", "defeat", True)
    assert result["counts"] == {"planned": 3, "started": 1, "won": 0, "lost": 1, "swept": 0, "not_started": 2}
    assert execution.ordinary_count == 1 and result["structural"] is None
    assert not names(execution, "resonance_pc.run_structural_exploration")


@pytest.mark.parametrize("outcome", ["swept", "victory"])
def test_soft_stop_does_not_start_next_run_or_structural(monkeypatch, outcome):
    execution = Execution([outcome], stop_after=1)
    context, engine = setup(monkeypatch, execution)
    result = run(context, engine)
    assert (result["status"], result["stop_reason"], result["continue_workflow"]) == ("cancelled", "cancelled", False)
    assert execution.ordinary_count == 1 and result["counts"]["not_started"] == 2
    assert not names(execution, "resonance_pc.run_structural_exploration")
    assert result["cleanup"]["returned_to_main"] is True


def test_unconfirmed_native_stop_blocks_generic_cleanup_and_workflow(monkeypatch):
    execution = Execution(structural=native(stopped=False))
    context, engine = setup(monkeypatch, execution, count=0)
    with pytest.raises(RuntimeError, match="native stop and handoff"):
        run(context, engine)
    progress = context.data["battle_plan_progress"]
    result = progress["result"]
    assert progress["stage"] == "failed" and result["continue_workflow"] is False
    assert result["structural"]["native_stopped"] is False
    assert result["cleanup"]["error"]["type"] == "NativeStopUnconfirmed"
    assert result["cleanup"]["returned_to_main"] is False
    assert not names(execution, "resonance_pc.wait_and_click_back_button")


def test_soft_stop_at_next_sweep_boundary_still_returns_to_main(monkeypatch):
    execution = Execution(["swept"])
    context, engine = setup(monkeypatch, execution)
    checks_after_sweep = 0

    def stop_on_next_iteration(_cid):
        nonlocal checks_after_sweep
        if execution.ordinary_count:
            checks_after_sweep += 1
        # The first check keeps the confirmed detail for reuse. The stop lands
        # at the following loop's first checkpoint, before its next input.
        return checks_after_sweep >= 2

    monkeypatch.setattr(runtime, "stop_requested", stop_on_next_iteration)
    result = run(context, engine)
    assert (result["status"], result["continue_workflow"]) == ("cancelled", False)
    assert execution.ordinary_count == 1
    assert result["cleanup"]["returned_to_main"] is True
    assert len(names(execution, "resonance_pc.wait_and_click_back_button")) == 1
    assert not names(execution, "resonance_pc.run_structural_exploration")
