"""Finite-horizon inspiration yield planning using only NumPy.

The objective is expected collected inspiration credited to a meeting before
the deadline. A deadline miss contributes zero to that objective; actual
pickups before a miss are separately reported. Boss consumption is irreversible
in this model. No training assets, game-source files or offline analysis caches
are dependencies of this solver.
"""
from __future__ import annotations

from dataclasses import dataclass
from copy import deepcopy
import threading
import time
from typing import Callable

import numpy as np

from ._deep_dive_planner_rules import (
    EMPTY_INSPIRATION, boss_outcomes, coord_dict, geometry,
    player_actions, remaining_player_actions, validate_slot,
)

_EMPTY = EMPTY_INSPIRATION
_BASE = 55
_TOLERANCE = 1e-12
_TABLES = None
_BUILD_LOCK = threading.Lock()


class PlanningBudgetExceeded(TimeoutError):
    """No result for the requested horizon has been completed."""


class PlanningCancelled(Exception):
    """A caller's cooperative cancellation callback returned true."""


@dataclass(frozen=True)
class _Tables:
    states: np.ndarray
    lookup: np.ndarray
    own_next: np.ndarray
    own_gain: np.ndarray
    boss_next: np.ndarray
    boss_eat: np.ndarray
    boss_weight: np.ndarray
    free_count: np.ndarray


def _key(player, boss, first, second):
    return ((player * _BASE + boss) * _BASE + first) * _BASE + second


def _deadline_check(deadline: float, cancel_check: Callable | None):
    if cancel_check is not None and cancel_check():
        raise PlanningCancelled("cancel_requested")
    if time.monotonic() >= deadline:
        raise PlanningBudgetExceeded("inspiration_planning_time_budget_exhausted")


def _build_tables(checkpoint: Callable) -> _Tables:
    geo = geometry()
    # Canonical player's surface location is a corner, edge or centre of U.
    first, second = np.triu_indices(55, 1)
    first = np.append(first, _EMPTY)
    second = np.append(second, _EMPTY)
    candidates = []
    for player in (0, 1, 4):
        for boss in range(54):
            if player == boss:
                continue
            mask = ((first != player) & (first != boss)
                    & (second != player) & (second != boss))
            candidates.append(np.column_stack((
                np.full(mask.sum(), player), np.full(mask.sum(), boss),
                first[mask], second[mask],
            )))
        checkpoint()
    candidates = np.concatenate(candidates).astype(np.int32)
    p, b, i, j = candidates.T
    raw_keys = _key(p, b, i, j)
    canonical_keys = raw_keys.copy()
    for symmetry in geo.symmetries:
        a, z = symmetry[i], symmetry[j]
        keys = _key(symmetry[p], symmetry[b], np.minimum(a, z), np.maximum(a, z))
        np.minimum(canonical_keys, keys, out=canonical_keys)
        checkpoint()
    states = candidates[raw_keys == canonical_keys]
    p, b, i, j = states.T
    lookup = np.full(_BASE ** 4, -1, dtype=np.int32)
    indices = np.arange(len(states), dtype=np.int32)
    for symmetry in geo.symmetries:
        a, z = symmetry[i], symmetry[j]
        lookup[_key(symmetry[p], symmetry[b], np.minimum(a, z), np.maximum(a, z))] = indices
        checkpoint()

    length = len(states)
    own_next = np.full((length, 32), -1, dtype=np.int32)
    own_gain = np.zeros((length, 32), dtype=np.int8)
    boss_next = np.full((length, 16), -1, dtype=np.int32)
    boss_eat = np.zeros((length, 16), dtype=np.int8)
    boss_weight = np.zeros((length, 16), dtype=np.float64)

    def mapped_indices(player, boss, first, second):
        result = lookup[_key(player, boss, np.minimum(first, second), np.maximum(first, second))]
        return result

    # Move then rotate. Four copies of an early meeting are harmless at a max
    # node; the fixed indexing avoids a Python loop for every state.
    for neighbour in range(4):
        destination = geo.moves[p, neighbour]
        legal = destination >= 0
        safe_destination = np.maximum(destination, 0)
        meeting = legal & (destination == b)
        gain = ((i == destination).astype(np.int8) + (j == destination).astype(np.int8))
        for turn in range(4):
            rotation = geo.actor_rotations[safe_destination, turn]
            first_moved = geo.rotations[rotation, np.where(i == destination, _EMPTY, i)]
            second_moved = geo.rotations[rotation, np.where(j == destination, _EMPTY, j)]
            next_p = geo.rotations[rotation, safe_destination]
            next_b = geo.rotations[rotation, b]
            target = mapped_indices(next_p, next_b, first_moved, second_moved)
            regular = legal & ~meeting
            if np.any(target[regular] < 0):
                raise RuntimeError("Incomplete player movement state table")
            own_next[:, neighbour * 4 + turn] = np.where(meeting, -2, np.where(legal, target, -1))
            own_gain[:, neighbour * 4 + turn] = np.where(regular, gain, 0)
        checkpoint()

    # Rotate then move. Only the destination receives a pickup.
    for turn in range(4):
        rotation = geo.actor_rotations[p, turn]
        rotated_p = geo.rotations[rotation, p]
        rotated_b = geo.rotations[rotation, b]
        rotated_i = geo.rotations[rotation, i]
        rotated_j = geo.rotations[rotation, j]
        for neighbour in range(4):
            destination = geo.moves[rotated_p, neighbour]
            legal = destination >= 0
            meeting = legal & (destination == rotated_b)
            gain = ((rotated_i == destination).astype(np.int8)
                    + (rotated_j == destination).astype(np.int8))
            first_moved = np.where(rotated_i == destination, _EMPTY, rotated_i)
            second_moved = np.where(rotated_j == destination, _EMPTY, rotated_j)
            target = mapped_indices(np.maximum(destination, 0), rotated_b, first_moved, second_moved)
            regular = legal & ~meeting
            if np.any(target[regular] < 0):
                raise RuntimeError("Incomplete player rotation state table")
            own_next[:, 16 + turn * 4 + neighbour] = np.where(meeting, -2, np.where(legal, target, -1))
            own_gain[:, 16 + turn * 4 + neighbour] = np.where(regular, gain, 0)
        checkpoint()

    distance = np.abs((p % 9) // 3 - (b % 9) // 3) + np.abs(p % 3 - b % 3)
    destinations = geo.moves[b]
    after_distance = (np.abs((p[:, None] % 9) // 3 - (destinations % 9) // 3)
                      + np.abs(p[:, None] % 3 - destinations % 3))
    allowed = ((destinations >= 0)
               & ((p[:, None] // 9 != b[:, None] // 9)
                  | (after_distance == distance[:, None] - 1)))
    number_allowed = allowed.sum(axis=1)
    if np.any(number_allowed == 0):
        raise RuntimeError("Boss has no legal candidate in a nonterminal state")
    for neighbour in range(4):
        destination = destinations[:, neighbour]
        legal = allowed[:, neighbour]
        safe_destination = np.maximum(destination, 0)
        meeting = legal & (destination == p)
        eat = ((i == destination).astype(np.int8) + (j == destination).astype(np.int8))
        for turn in range(4):
            rotation = geo.actor_rotations[safe_destination, turn]
            first_moved = geo.rotations[rotation, np.where(i == destination, _EMPTY, i)]
            second_moved = geo.rotations[rotation, np.where(j == destination, _EMPTY, j)]
            target = mapped_indices(geo.rotations[rotation, p],
                                    geo.rotations[rotation, safe_destination], first_moved, second_moved)
            regular = legal & ~meeting
            if np.any(target[regular] < 0):
                raise RuntimeError("Incomplete boss state table")
            column = neighbour * 4 + turn
            boss_next[:, column] = np.where(meeting, -2, np.where(legal, target, -1))
            boss_eat[:, column] = np.where(regular, eat, 0)
            # A terminal move is repeated four times, each with one quarter
            # of its move mass. No actual boss rotation occurs in that case.
            boss_weight[:, column] = np.where(legal, 1. / (number_allowed * 4), 0.)
        checkpoint()
    free_count = (i != _EMPTY).astype(np.int8) + (j != _EMPTY).astype(np.int8)
    for array in (states, lookup, own_next, own_gain, boss_next, boss_eat, boss_weight, free_count):
        array.flags.writeable = False
    return _Tables(states, lookup, own_next, own_gain, boss_next, boss_eat, boss_weight, free_count)


def _tables(checkpoint: Callable) -> _Tables:
    global _TABLES
    if _TABLES is not None:
        return _TABLES
    while not _BUILD_LOCK.acquire(timeout=.05):
        checkpoint()
    try:
        if _TABLES is None:
            checkpoint()
            built = _build_tables(checkpoint)
            checkpoint()
            _TABLES = built
        return _TABLES
    finally:
        _BUILD_LOCK.release()


def _lex_better(candidate, current, selected):
    better = ~selected
    tied = selected.copy()
    # Success-weighted total inspiration, then arrival probability, then
    # actual pickups, then the first moment of successful meeting time.
    for column, sign in ((1, 1), (0, 1), (2, 1), (4, -1)):
        difference = sign * (candidate[:, column] - current[:, column])
        better |= tied & (difference > _TOLERANCE)
        tied &= np.abs(difference) <= _TOLERANCE
    return better


def _boss_step(previous, tables: _Tables, baseline: int, checkpoint: Callable):
    """Enemy end of the current round, followed by the supplied future turns."""
    boss_values = np.zeros_like(previous)
    for count in range(3):
        valid = count + tables.free_count <= 2
        for action in range(16):
            target = tables.boss_next[:, action]
            values = previous[count, np.maximum(target, 0)].copy()
            values[:, 3] += tables.boss_eat[:, action]
            values[:, 4] += previous[count, np.maximum(target, 0), 0]
            terminal = target == -2
            values[terminal] = (1., baseline + count, 0., 0., 1.)
            boss_values[count] += values * tables.boss_weight[:, action, None]
            checkpoint()
        boss_values[count, ~valid] = 0.
    return boss_values


def _step(previous, tables: _Tables, baseline: int, checkpoint: Callable):
    length = len(tables.states)
    boss_values = _boss_step(previous, tables, baseline, checkpoint)
    current = np.zeros_like(previous)
    for count in range(3):
        valid = count + tables.free_count <= 2
        best = np.zeros((length, 5))
        selected = np.zeros(length, dtype=bool)
        for action in range(32):
            target = tables.own_next[:, action]
            gain = tables.own_gain[:, action]
            values = boss_values[np.minimum(count + gain, 2), np.maximum(target, 0)].copy()
            values[:, 2] += gain
            terminal = target == -2
            values[terminal] = (1., baseline + count, 0., 0., 1.)
            legal = valid & (target != -1)
            take = legal & _lex_better(values, best, selected)
            best[take] = values[take]
            selected |= legal
            checkpoint()
        if np.any(valid & ~selected):
            raise RuntimeError("Nonterminal state has no player action")
        current[count] = best
    return current, boss_values


def _index(tables: _Tables, player: int, boss: int, inspirations: tuple[int, ...]):
    values = sorted(inspirations) + [_EMPTY] * (2 - len(inspirations))
    result = int(tables.lookup[_key(player, boss, values[0], values[1])])
    if result < 0:
        raise ValueError("Invalid or overlapping target coordinates")
    return result


def _after_player(action, inspirations):
    remaining = tuple(i for i in inspirations if i != action.pickup_original_slot)
    gained = len(inspirations) - len(remaining)
    if action.rotation_id is not None:
        permutation = geometry().rotations[action.rotation_id]
        remaining = tuple(sorted(int(permutation[i]) for i in remaining))
    return remaining, gained


def plan_inspiration(player: int, boss: int, inspirations: tuple[int, ...], turn_budget: int,
                     *, collected_count: int = 0, cancel_check: Callable | None = None,
                     time_budget_sec: float = 30., moves_left: int = 1,
                     rotations_left: int = 1) -> dict:
    """Optimize a known layout; no assumption about initial spawn is needed.

    collected_count supplies previously confirmed pickups for consistent
    rolling replanning. At most two remaining freely visible inspirations are
    supported. Results contain one player turn and immediate boss branches;
    the actual branch should be observed before requesting the next plan.
    """
    player, boss = validate_slot(player), validate_slot(boss)
    available_actions = remaining_player_actions(player, boss, moves_left, rotations_left)
    inspirations = tuple(validate_slot(i) for i in inspirations)
    if player == boss or len(set(inspirations)) != len(inspirations):
        raise ValueError("Player, boss and inspiration positions must be distinct")
    if len(inspirations) > 2 or player in inspirations or boss in inspirations:
        raise ValueError("Supports at most two non-overlapping free inspirations")
    if isinstance(turn_budget, bool) or not isinstance(turn_budget, int) or not 1 <= turn_budget <= 30:
        raise ValueError("turn_budget must be an integer from 1 to 30")
    if isinstance(collected_count, bool) or not isinstance(collected_count, int) or collected_count < 0:
        raise ValueError("collected_count must be a nonnegative integer")
    if not np.isfinite(time_budget_sec) or time_budget_sec <= 0:
        raise ValueError("time_budget_sec must be positive and finite")
    if moves_left == rotations_left == 0:
        return dict(status="waiting_enemy", success=False, next_action=None,
                    player_turn_actions=[], boss_branches=[], requires_observation_after_action=True)
    started = time.monotonic()
    deadline = started + time_budget_sec
    checkpoint = lambda: _deadline_check(deadline, cancel_check)
    checkpoint()
    tables = _tables(checkpoint)
    root = _index(tables, player, boss, inspirations)
    previous = np.zeros((3, len(tables.states), 5))
    partial_turn = moves_left != 1 or rotations_left != 1
    for horizon in range(1, turn_budget + 1):
        if horizon == turn_budget and partial_turn:
            before_last = previous
            boss_values = _boss_step(previous, tables, collected_count, checkpoint)
            current = None
            break
        current, boss_values = _step(previous, tables, collected_count, checkpoint)
        if horizon == turn_budget:
            before_last = previous
            break
        previous = current
    selected = None
    selected_value = None
    selected_remaining = ()
    selected_gain = 0
    for action in available_actions:
        checkpoint()
        remaining, gain = _after_player(action, inspirations)
        if action.terminal:
            value = np.array((1., float(collected_count + gain), float(gain), 0., 1.))
        else:
            target = _index(tables, action.final_player, action.final_boss, remaining)
            value = boss_values[gain, target].copy()
            value[2] += gain
        if selected is None or _lex_better(value[None, :], selected_value[None, :], np.ones(1, bool))[0]:
            selected, selected_value = action, value
            selected_remaining, selected_gain = remaining, gain
    if selected is None:
        raise RuntimeError("No legal player action selected")
    root_value = selected_value if partial_turn else current[0, root]
    if not partial_turn and np.max(np.abs(root_value - selected_value)) > 1e-8:
        raise RuntimeError("Selected action does not match the exhaustive value table")
    probability, score, picked_up, consumed, moment = map(float, root_value)
    branches = []
    if not selected.terminal:
        for outcome in boss_outcomes(selected.final_player, selected.final_boss):
            checkpoint()
            survivors = tuple(i for i in selected_remaining if i != outcome.eat_original_slot)
            lost = len(selected_remaining) - len(survivors)
            if outcome.rotation_id is not None:
                permutation = geometry().rotations[outcome.rotation_id]
                survivors = tuple(sorted(int(permutation[i]) for i in survivors))
            branch = dict(probability=outcome.probability, encounter=outcome.terminal,
                          boss_actions=deepcopy(list(outcome.sequence)),
                          player_cell=coord_dict(outcome.final_player),
                          boss_cell=coord_dict(outcome.final_boss),
                          inspiration_cells=[coord_dict(i) for i in survivors],
                          collected_count=collected_count + selected_gain,
                          newly_consumed_inspirations=lost,
                          remaining_rounds=turn_budget - 1)
            if not outcome.terminal:
                target = _index(tables, outcome.final_player, outcome.final_boss, survivors)
                continuation = before_last[selected_gain, target]
                branch["continuation_deadline_probability"] = float(continuation[0])
                branch["continuation_expected_successful_total_inspirations"] = float(continuation[1])
            branches.append(branch)
    sequence = deepcopy(list(selected.sequence))
    return dict(
        status="solved", success=True,
        objective="max_expected_inspiration_at_encounter_before_deadline",
        next_action=sequence[0], player_turn_actions=sequence,
        player_turn_terminal=selected.terminal, boss_branches=branches,
        planned_player_pickup_count=selected_gain,
        inspiration_after_player_turn=[coord_dict(i) for i in selected_remaining],
        metrics=dict(deadline_encounter_probability=probability,
                     expected_new_inspirations=picked_up,
                     expected_successful_new_inspirations=max(0., score - collected_count * probability),
                     expected_successful_total_inspirations=score,
                     expected_boss_consumed_inspirations=consumed,
                     expected_encounter_turns_given_deadline=moment / probability if probability else None),
        solver=dict(method="finite_horizon_exhaustive_dynamic_programming",
                    completed_horizon=turn_budget, state_count=len(tables.states),
                    model_optimality="numerical_exhaustive", comparison_tolerance=_TOLERANCE,
                    elapsed_sec=time.monotonic() - started,
                    remaining_free_inspiration_limit=2, current_turn_partial=partial_turn,
                    current_moves_left=int(moves_left), current_rotations_left=int(rotations_left)),
        guarantee_type="probabilistic", requires_observation_after_action=True,
    )
