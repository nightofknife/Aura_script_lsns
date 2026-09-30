"""Public, input-free entry for the two Deep Dive movement algorithms."""
from __future__ import annotations

from copy import deepcopy
import hashlib
import json
import math
from numbers import Integral, Real
import time

from ._deep_dive_chase_planner import plan_chase
from ._deep_dive_inspiration_planner import (
    PlanningBudgetExceeded, PlanningCancelled, plan_inspiration,
)
from ._deep_dive_planner_rules import (
    FACES, RULES_VERSION, cell_to_slot, coord_dict, geometry, player_actions,
    remaining_player_actions,
)

SCHEMA = "resonance_pc.deep_dive_plan.v1"


def _integer(value, name: str, minimum: int, maximum: int | None = None) -> int:
    if not isinstance(value, Integral) or isinstance(value, bool) or value < minimum:
        raise ValueError(f"{name} must be an integer >= {minimum}")
    if maximum is not None and value > maximum:
        raise ValueError(f"{name} must be <= {maximum}")
    return int(value)


def _layout_targets(layout: dict, *, allow_partial_turn: bool = False):
    if not isinstance(layout, dict):
        raise ValueError("layout must be a scan result dictionary")
    if layout.get("schema") != "resonance_pc.deep_dive_layout.v1":
        raise ValueError("Unsupported cube layout schema")
    if layout.get("coordinate_frame") != "scan_local":
        raise ValueError("Layout requires explicit scan_local coordinates")
    if not (layout.get("status") == "completed" and layout.get("layout_complete") is True
            and layout.get("success") is True):
        raise ValueError("A complete successful scan is required")
    cells = layout.get("cells")
    if not isinstance(cells, list) or len(cells) != 54:
        raise ValueError("Only a three-by-three cube with 54 cells is supported")
    occupants = {}
    fingerprint_cells = []
    for cell in cells:
        if not isinstance(cell, dict):
            raise ValueError("Each cell must be an object")
        slot = cell_to_slot(cell)
        if slot in occupants:
            raise ValueError("Duplicate cube coordinate")
        kind = cell.get("occupant")
        if cell.get("occupant_status") != "confirmed" or kind not in (
                "none", "player", "singularity", "inspiration"):
            raise ValueError("Unknown or conflicting target occupancy cannot be planned")
        occupants[slot] = kind
        fingerprint_cells.append((slot, kind, cell.get("icon_id"), cell.get("node_status")))
    if set(occupants) != set(range(54)):
        raise ValueError("Cube coordinates are incomplete")
    players = [slot for slot, kind in occupants.items() if kind == "player"]
    bosses = [slot for slot, kind in occupants.items() if kind == "singularity"]
    inspirations = tuple(sorted(slot for slot, kind in occupants.items() if kind == "inspiration"))
    if len(players) != 1 or len(bosses) != 1:
        raise ValueError("Exactly one player and one singularity must be confirmed")
    player, boss = players[0], bosses[0]
    for field, actual in (("player_cell", player), ("singularity_cell", boss)):
        if layout.get(field) is not None and cell_to_slot(layout[field]) != actual:
            raise ValueError(f"{field} contradicts the cell occupancy map")
    if layout.get("inspiration_cells") is not None:
        declared = layout["inspiration_cells"]
        if not isinstance(declared, list) or sorted(cell_to_slot(cell) for cell in declared) != list(inspirations):
            raise ValueError("inspiration_cells contradicts the cell occupancy map")
    phase = layout.get("planning_state", {})
    if not isinstance(phase, dict):
        raise ValueError("planning_state must be an object")
    for name in ("moves_left", "rotations_left", "move_distance"):
        if name not in phase:
            continue
        value = _integer(phase[name], name, 0)
        if allow_partial_turn and name != "move_distance":
            if value not in (0, 1):
                raise ValueError("This version supports at most one remaining move and rotation")
        elif value != 1:
            raise ValueError("This version requires one move, one rotation and adjacent movement")
    fingerprint = hashlib.sha256(json.dumps(
        sorted(fingerprint_cells), ensure_ascii=False, separators=(",", ":"),
    ).encode("utf-8")).hexdigest()
    return player, boss, inspirations, fingerprint


def _chase_inspiration_branches(result: dict, player: int, boss: int,
                                inspirations: tuple[int, ...], collected_count: int,
                                moves_left: int = 1, rotations_left: int = 1):
    """The chase choice ignores inspiration, but branch state must not lose it."""
    sequence = result.get("player_turn_actions", [])
    selected = next((action for action in remaining_player_actions(player, boss, moves_left, rotations_left)
                     if list(action.sequence) == sequence), None)
    if selected is None:
        raise RuntimeError("Chase result has no corresponding legal player action")
    remaining = tuple(i for i in inspirations if i != selected.pickup_original_slot)
    gained = len(inspirations) - len(remaining)
    if selected.rotation_id is not None:
        permutation = geometry().rotations[selected.rotation_id]
        remaining = tuple(sorted(int(permutation[i]) for i in remaining))
    result["planned_player_pickup_count"] = gained
    result["inspiration_after_player_turn"] = [coord_dict(i) for i in remaining]
    branches = deepcopy(result.get("predicted_boss_branches", []))
    for branch in branches:
        survivors = tuple(i for i in remaining if i != branch["eat_original_slot"])
        lost = len(remaining) - len(survivors)
        if branch["rotation_id"] is not None:
            permutation = geometry().rotations[branch["rotation_id"]]
            survivors = tuple(sorted(int(permutation[i]) for i in survivors))
        branch.update(inspiration_cells=[coord_dict(i) for i in survivors],
                      player_cell=branch["player"], boss_cell=branch["boss"],
                      encounter=branch["terminal"],
                      collected_count=collected_count + gained,
                      newly_consumed_inspirations=lost,
                      remaining_rounds=branch["remaining_turns"])
    result["boss_branches"] = branches


def plan_layout(layout: dict, strategy: str = "chase", turn_budget: int = 6,
                time_budget_sec: float = 30., cancel_check=None,
                *, collected_count: int = 0,
                chase_objective: str = "min_expected_encounter_turns") -> dict:
    """Plan a known complete layout; this function never captures or clicks.

    chase minimizes expected encounter time; inspiration maximizes total
    confirmed-plus-future inspiration at a meeting before the deadline.
    Previously collected quantities can be supplied for consistent replanning.
    """
    started = time.monotonic()
    if strategy not in ("chase", "inspiration"):
        raise ValueError("strategy must be chase or inspiration")
    turns = _integer(turn_budget, "turn_budget", 1, 30)
    collected_count = _integer(collected_count, "collected_count", 0)
    if (not isinstance(time_budget_sec, Real) or isinstance(time_budget_sec, bool)
            or not math.isfinite(time_budget_sec) or time_budget_sec <= 0):
        raise ValueError("time_budget_sec must be positive and finite")
    player, boss, inspirations, fingerprint = _layout_targets(layout)
    result = dict(schema=SCHEMA, strategy=strategy, turn_budget=turns,
                  rules_version=RULES_VERSION, coordinate_frame="scan_local",
                  player_cell=coord_dict(player), boss_cell=coord_dict(boss),
                  inspiration_cells=[coord_dict(i) for i in inspirations],
                  collected_count=collected_count,
                  planned_layout_fingerprint=fingerprint, execution_mode="planning_only",
                  requires_observation_after_action=True,
                  rule_assumptions=[
                      "three-by-three cube; one adjacent same-face move and one layer rotation per turn",
                      "player may move then rotate or rotate then move",
                      "same-face boss uniformly chooses a neighbour reducing Manhattan distance",
                      "different-face boss uniformly chooses a same-face neighbour",
                      "boss rotates uniformly after movement, unless movement causes an encounter",
                      "all attached targets and nodes move with the selected layer",
                      "boss-consumed inspiration is not credited to the player or assumed recoverable",
                      "each freely visible inspiration is one collectible unit",
                      "node event effects, extra actions, items and battle victory are not simulated",
                  ])

    def check_cancel():
        if cancel_check is not None and cancel_check():
            raise PlanningCancelled("cancel_requested")

    try:
        check_cancel()
        remaining_time = float(time_budget_sec) - (time.monotonic() - started)
        if remaining_time <= 0:
            raise PlanningBudgetExceeded("planning_time_budget_exhausted")
        if strategy == "chase":
            calculated = plan_chase(player, boss, turns, cancel_check=check_cancel,
                                    time_budget_sec=remaining_time, objective=chase_objective)
            result.update(calculated)
            result["metrics"] = dict(
                optimal_expected_turns=calculated.get("optimal_expected_turns"),
                expected_encounter_turns=calculated.get("expected_turns"),
                deadline_encounter_probability=calculated.get("deadline_probability"),
                maximum_deadline_encounter_probability=calculated.get("maximum_deadline_probability"),
                expected_encounter_turns_given_deadline=calculated.get("conditional_expected_turns_by_deadline"),
            )
            if calculated.get("status") == "solved":
                _chase_inspiration_branches(result, player, boss, inspirations, collected_count)
        else:
            if len(inspirations) > 2:
                raise ValueError("Inspiration planning currently supports at most two free inspirations")
            calculated = plan_inspiration(player, boss, inspirations, turns,
                                          collected_count=collected_count, cancel_check=check_cancel,
                                          time_budget_sec=remaining_time)
            result.update(calculated)
        check_cancel()
    except PlanningCancelled:
        result.update(status="cancelled", reason="cancel_requested")
    except PlanningBudgetExceeded as error:
        result.update(status="search_budget_exhausted", reason=str(error))
    result["success"] = result.get("status") == "solved"
    probability = result.get("metrics", {}).get("deadline_encounter_probability")
    if result["success"] and probability == 0:
        result.update(status="blocked", success=False,
                      reason="selected_policy_cannot_encounter_within_turn_budget")
    result["schema"] = SCHEMA
    result["strategy"] = strategy
    result["requires_observation_after_action"] = True
    result["execution_mode"] = "planning_only"
    result["computation_elapsed_sec"] = time.monotonic() - started
    if not result["success"]:
        result.setdefault("reason", {
            "cancelled": "cancel_requested",
            "search_budget_exhausted": "planning_time_budget_exhausted",
        }.get(result.get("status"), "planning_not_completed"))
        result.pop("next_action", None)
        result.pop("player_turn_actions", None)
        result.pop("boss_branches", None)
        result.pop("predicted_boss_branches", None)
    return result


def plan_current_state(layout: dict, strategy: str = "chase", rounds_remaining: int = 6,
                       moves_left: int = 1, rotations_left: int = 1, collected_count: int = 0,
                       *, time_budget_sec: float = 30., cancel_check=None,
                       chase_objective: str = "min_expected_encounter_turns") -> dict:
    """Plan from a fresh scan in the current turn, including already spent quotas.

    rounds_remaining includes this turn's enemy stage, even in the last round.
    Only future rounds replenish the move and rotation quotas. Every coordinate
    belongs to this scan epoch; no previous scan's local frame is registered or
    reused. collected_count is externally confirmed history, never inferred
    from an inspiration disappearing in the new scan.

    solved contains remaining player primitives and immediate enemy branches.
    waiting_enemy contains no player actions and requires observing the enemy
    result. deadline_exhausted, cancelled, search_budget_exhausted and blocked
    likewise never expose an actionable next_action. Invalid inputs raise
    ValueError before search.
    """
    started = time.monotonic()
    if strategy not in ("chase", "inspiration"):
        raise ValueError("strategy must be chase or inspiration")
    rounds = _integer(rounds_remaining, "rounds_remaining", 0, 30)
    moves = _integer(moves_left, "moves_left", 0, 1)
    rotations = _integer(rotations_left, "rotations_left", 0, 1)
    collected = _integer(collected_count, "collected_count", 0)
    if (not isinstance(time_budget_sec, Real) or isinstance(time_budget_sec, bool)
            or not math.isfinite(time_budget_sec) or time_budget_sec <= 0):
        raise ValueError("time_budget_sec must be positive and finite")
    if chase_objective not in ("min_expected_encounter_turns", "max_deadline_encounter_probability"):
        raise ValueError("Unsupported chase objective")
    player, boss, inspirations, fingerprint = _layout_targets(layout, allow_partial_turn=True)
    phase = layout.get("planning_state", {})
    for name, actual in (("moves_left", moves), ("rotations_left", rotations),
                         ("rounds_remaining", rounds), ("collected_count", collected)):
        if name in phase and _integer(phase[name], name, 0) != actual:
            raise ValueError(f"planning_state.{name} contradicts the supplied current state")
    for cell in layout["cells"]:
        allowed = ("known",) if cell["occupant"] == "none" else ("known", "not_required_target")
        if cell.get("node_status") not in allowed:
            raise ValueError("Unknown or conflicting node content cannot be used for runtime planning")
    if strategy == "inspiration" and len(inspirations) > 2:
        raise ValueError("Inspiration planning currently supports at most two free inspirations")
    result = dict(schema=SCHEMA, strategy=strategy, turn_budget=rounds, rounds_remaining=rounds,
                  moves_left=moves, rotations_left=rotations, collected_count=collected,
                  rules_version=RULES_VERSION, coordinate_frame="scan_local",
                  player_cell=coord_dict(player), boss_cell=coord_dict(boss),
                  inspiration_cells=[coord_dict(i) for i in inspirations],
                  planned_layout_fingerprint=fingerprint,
                  planned_scan_epoch=layout.get("scan_epoch", layout.get("run_id")),
                  execution_mode="planning_only", requires_observation_after_action=True,
                  current_turn_partial=moves != 1 or rotations != 1,
                  metrics={}, next_action=None, player_turn_actions=[], boss_branches=[])

    def checkpoint():
        if cancel_check is not None and cancel_check():
            raise PlanningCancelled("cancel_requested")
        if time.monotonic() - started >= float(time_budget_sec):
            raise PlanningBudgetExceeded("planning_time_budget_exhausted")

    try:
        checkpoint()
        if rounds == 0:
            result.update(status="deadline_exhausted", success=False, reason="no_rounds_remaining")
        elif moves == rotations == 0:
            result.update(status="waiting_enemy", success=False, reason="current_player_quotas_exhausted")
        else:
            remaining_time = float(time_budget_sec) - (time.monotonic() - started)
            if moves == rotations == 1:
                calculated = plan_layout(layout, strategy=strategy, turn_budget=rounds,
                                         time_budget_sec=remaining_time, cancel_check=checkpoint,
                                         collected_count=collected, chase_objective=chase_objective)
                result.update(calculated)
            elif strategy == "chase":
                calculated = plan_chase(player, boss, rounds, cancel_check=checkpoint,
                                        time_budget_sec=remaining_time, objective=chase_objective,
                                        moves_left=moves, rotations_left=rotations)
                result.update(calculated)
                result["metrics"] = dict(
                    optimal_expected_turns=calculated.get("optimal_expected_turns"),
                    expected_encounter_turns=calculated.get("expected_turns"),
                    deadline_encounter_probability=calculated.get("deadline_probability"),
                    maximum_deadline_encounter_probability=calculated.get("maximum_deadline_probability"),
                    expected_encounter_turns_given_deadline=calculated.get("conditional_expected_turns_by_deadline"),
                )
                if calculated.get("status") == "solved":
                    _chase_inspiration_branches(result, player, boss, inspirations, collected, moves, rotations)
            else:
                calculated = plan_inspiration(player, boss, inspirations, rounds,
                                              collected_count=collected, cancel_check=checkpoint,
                                              time_budget_sec=remaining_time,
                                              moves_left=moves, rotations_left=rotations)
                result.update(calculated)
            checkpoint()
            result["success"] = result.get("status") == "solved"
            if result["success"] and result.get("metrics", {}).get("deadline_encounter_probability") == 0:
                result.update(status="blocked", success=False,
                              reason="selected_policy_cannot_encounter_within_turn_budget")
    except PlanningCancelled:
        result.update(status="cancelled", success=False, reason="cancel_requested")
    except PlanningBudgetExceeded as error:
        result.update(status="search_budget_exhausted", success=False, reason=str(error))
    result["computation_elapsed_sec"] = time.monotonic() - started
    result["execution_mode"] = "planning_only"
    result["requires_observation_after_action"] = True
    if not result.get("success"):
        result["next_action"] = None
        result["player_turn_actions"] = []
        result["boss_branches"] = []
        result.pop("predicted_boss_branches", None)
        result.pop("selected_action", None)
        result.setdefault("reason", "planning_not_completed")
    return result
