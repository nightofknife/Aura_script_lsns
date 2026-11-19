"""Finite-horizon encounter and inspiration planning using NumPy.

Inspiration yield is credited on branches that meet before the deadline.
Actual pickups on other branches are tracked separately. Boss consumption
is irreversible in the model. For a fixed observed layout each surviving
inspiration has at most 24 possible slots on any supported cube size.
"""
from __future__ import annotations

from collections import OrderedDict
from dataclasses import dataclass
from copy import deepcopy
import threading
import time
from typing import Callable

import numpy as np

from ._deep_dive_bounded_inspiration import bounded_reward_choice
from ._deep_dive_planner_rules import (
    boss_outcomes, coord_dict, geometry, remaining_player_actions,
    validate_cube_size, validate_move_distance, validate_slot,
)

MAX_FREE_INSPIRATIONS = 4
_TOLERANCE = 1e-12
_YIELD_ORDER = ((1, 1), (0, 1), (2, 1), (4, -1))
_ENCOUNTER_ORDER = ((0, 1), (1, 1), (2, 1), (4, -1))
_TABLE_CACHE = OrderedDict()
_TABLE_CACHE_LIMIT_BYTES = 160 * 1024**2
_BUILD_LOCK = threading.Lock()
_CONTINUATION_CACHE = OrderedDict()
_CONTINUATION_LOCK = threading.Lock()
_CONTINUATION_CACHE_LIMIT_BYTES = 96 * 1024**2
_CONTINUATION_CACHE_LIMIT_ENTRIES = 24


class PlanningBudgetExceeded(TimeoutError):
    """No result for the requested horizon has been completed."""


class PlanningCancelled(Exception):
    """A caller's cooperative cancellation callback returned true."""


@dataclass(frozen=True)
class _StateIndex:
    player_orbit: np.ndarray
    player_frame: np.ndarray
    symmetries: np.ndarray
    oriented_placements: np.ndarray
    state_ids: np.ndarray

    def get(self, player, boss, first, second):
        frame = self.player_frame[player]
        boss_in_frame = self.symmetries[frame, boss]
        placement = self.oriented_placements[frame, first, second]
        return self.state_ids[self.player_orbit[player], boss_in_frame, placement]


@dataclass(frozen=True)
class _Tables:
    context: tuple[int, int, tuple[int, ...]]
    index: _StateIndex
    own_next: np.ndarray
    own_gain: np.ndarray
    pickup_slots: np.ndarray | None
    boss_next: np.ndarray
    boss_eat: np.ndarray
    boss_weight: np.ndarray  # Equal mass for each legal boss column in a row.
    free_count: np.ndarray

    @property
    def nbytes(self):
        arrays = (self.index.player_orbit, self.index.player_frame, self.index.oriented_placements,
                  self.index.state_ids, self.own_next, self.own_gain, self.boss_next,
                  self.boss_eat, self.boss_weight, self.free_count)
        return sum(array.nbytes for array in arrays) + (0 if self.pickup_slots is None else self.pickup_slots.nbytes)


def _deadline_check(deadline: float, cancel_check: Callable | None):
    if cancel_check is not None and cancel_check():
        raise PlanningCancelled("cancel_requested")
    if time.monotonic() >= deadline:
        raise PlanningBudgetExceeded("inspiration_planning_time_budget_exhausted")


def _placements(geo, signature: tuple[int, ...]):
    # A layer turn either fixes a sticker or acts on it as a whole-cube
    # rotation. Each surviving inspiration stays in its initial orbit (<=24
    # slots). Also include every submultiset left by pickups and consumption.
    empty = len(geo.moves)
    orbits = [np.unique(geo.symmetries[:, representative]) for representative in signature]
    placements = {(empty, empty)}
    for orbit in orbits:
        placements.update((slot, empty) for slot in orbit)
    if len(orbits) == 2:
        placements.update((min(i, j), max(i, j)) for i in orbits[0] for j in orbits[1] if i != j)
    return np.asarray(sorted(placements), dtype=np.int32)


def _transition_byte_bound(geo, inspirations):
    """Upper bound on retained two-target transitions and index, before allocating them."""
    slots = len(geo.moves)
    group = geo.symmetries[:, :slots]
    fixed = (group == np.arange(slots)).sum(axis=1)
    # Burnside's lemma on ordered, distinct player/boss pairs.
    pair_count = int((fixed * (fixed - 1)).sum()) // len(group)
    placement_count = len(_placements(geo, inspirations))
    player_orbits = len(np.unique(group.min(axis=0)))
    # One int32 successor and int8 gain for every own/enemy action. Each row
    # also stores a float64 boss weight and int8 count of surviving items.
    row_bytes = 5 * (8 * geo.moves.shape[1] + 16) + 9
    index_bytes = (4 * len(group) * (slots + 1)**2
                   + 4 * player_orbits * slots * (placement_count + 1)
                   + 2 * np.dtype(np.intp).itemsize * slots)
    return row_bytes * pair_count * placement_count + index_bytes


def _state_index(geo, signature: tuple[int, ...], checkpoint: Callable):
    empty = len(geo.moves)
    first, second = _placements(geo, signature).T
    placement_ids = np.full((empty + 1, empty + 1), -1, dtype=np.int32)
    placement_ids[first, second] = np.arange(len(first))
    placement_ids[second, first] = placement_ids[first, second]
    group = geo.symmetries
    oriented = placement_ids[group[:, :, None], group[:, None, :]]

    # Minimize (player, boss, first, second) lexicographically in two stages.
    # First rotate the player to its orbit representative. The remaining
    # rotations fixing that player determine the canonical boss/item tuple.
    player_images = group[:, :empty]
    player_frame = player_images.argmin(axis=0)
    representatives, player_orbit = np.unique(player_images.min(axis=0), return_inverse=True)
    ranks = np.arange(empty * len(first), dtype=np.int32).reshape(empty, -1)
    canonical = np.empty((len(representatives), *ranks.shape), dtype=np.int32)
    boss_slots = np.arange(empty)[:, None]
    for bucket, player in enumerate(representatives):
        row = canonical[bucket]
        row[:] = ranks
        for frame in np.flatnonzero(group[:, player] == player):
            rotated_rank = group[frame, :empty, None] * len(first) + oriented[frame, first, second]
            np.minimum(row, rotated_rank, out=row)
        row[(boss_slots == player) | (first == player) | (second == player)
            | (boss_slots == first) | (boss_slots == second)] = -1
        checkpoint()

    buckets, bosses, items = np.nonzero(canonical == ranks)
    counts = (first[items] != empty).astype(np.int8) + (second[items] != empty)
    order = np.argsort(counts, kind="stable")
    buckets, bosses, items, counts = buckets[order], bosses[order], items[order], counts[order]
    # The last rank/placement column is the invalid (-1) sentinel.
    rank_to_state = np.full((len(representatives), ranks.size + 1), -1, dtype=np.int32)
    rank_to_state[buckets, bosses * len(first) + items] = np.arange(len(items), dtype=np.int32)
    state_ids = np.full((*canonical.shape[:2], len(first) + 1), -1, dtype=np.int32)
    state_ids[:, :, :-1] = rank_to_state[np.arange(len(representatives))[:, None, None], canonical]
    index = _StateIndex(player_orbit, player_frame, group, oriented, state_ids)
    for array in (player_orbit, player_frame, oriented, state_ids):
        array.flags.writeable = False
    return index, representatives[buckets], bosses, first[items], second[items], counts


def _build_tables(cube_size: int, move_distance: int, signature: tuple[int, ...], checkpoint: Callable) -> _Tables:
    geo = geometry(cube_size=cube_size, move_distance=move_distance)
    empty = len(geo.moves)
    index, p, b, i, j, free_count = _state_index(geo, signature, checkpoint)
    length = len(p)
    # Bellman updates scan one action column at a time.
    move_columns = geo.moves.shape[1]
    order_width = move_columns * 4
    own_next = np.full((length, 2 * order_width), -1, dtype=np.int32, order="F")
    own_gain = np.zeros(own_next.shape, dtype=np.int8, order="F")
    # The pair-only solver also needs the original tile reached by each
    # physical action, to rank visible inspirations among encounter-optimal moves.
    pickup_slots = None if signature else np.full(own_next.shape, empty, dtype=np.int16, order="F")
    inverse_rotations = np.argsort(geo.rotations, axis=1)
    boss_next = np.full((length, 16), -1, dtype=np.int32, order="F")
    boss_eat = np.zeros((length, 16), dtype=np.int8, order="F")
    # Move then rotate. Four copies of an early meeting are harmless at a max
    # node; the fixed indexing avoids a Python loop for every state.
    for neighbour in range(move_columns):
        destination = geo.moves[p, neighbour]
        legal = destination >= 0
        safe_destination = np.maximum(destination, 0)
        meeting = legal & (destination == b)
        gain = ((i == destination).astype(np.int8) + (j == destination).astype(np.int8))
        for turn in range(4):
            rotation = geo.actor_rotations[safe_destination, turn]
            first_moved = geo.rotations[rotation, np.where(i == destination, empty, i)]
            second_moved = geo.rotations[rotation, np.where(j == destination, empty, j)]
            next_p = geo.rotations[rotation, safe_destination]
            next_b = geo.rotations[rotation, b]
            target = index.get(next_p, next_b, first_moved, second_moved)
            regular = legal & ~meeting
            if np.any(target[regular] < 0):
                raise RuntimeError("Incomplete player movement state table")
            own_next[:, neighbour * 4 + turn] = np.where(meeting, -2, np.where(legal, target, -1))
            own_gain[:, neighbour * 4 + turn] = np.where(regular, gain, 0)
            if pickup_slots is not None:
                pickup_slots[:, neighbour * 4 + turn] = np.where(legal, destination, empty)
        checkpoint()

    # Rotate then move. Only the destination receives a pickup.
    for turn in range(4):
        rotation = geo.actor_rotations[p, turn]
        rotated_p = geo.rotations[rotation, p]
        rotated_b = geo.rotations[rotation, b]
        rotated_i = geo.rotations[rotation, i]
        rotated_j = geo.rotations[rotation, j]
        for neighbour in range(move_columns):
            destination = geo.moves[rotated_p, neighbour]
            legal = destination >= 0
            meeting = legal & (destination == rotated_b)
            gain = ((rotated_i == destination).astype(np.int8)
                    + (rotated_j == destination).astype(np.int8))
            first_moved = np.where(rotated_i == destination, empty, rotated_i)
            second_moved = np.where(rotated_j == destination, empty, rotated_j)
            target = index.get(np.maximum(destination, 0), rotated_b, first_moved, second_moved)
            regular = legal & ~meeting
            if np.any(target[regular] < 0):
                raise RuntimeError("Incomplete player rotation state table")
            column = order_width + turn * move_columns + neighbour
            own_next[:, column] = np.where(meeting, -2, np.where(legal, target, -1))
            own_gain[:, column] = np.where(regular, gain, 0)
            if pickup_slots is not None:
                pickup_slots[:, column] = np.where(legal, inverse_rotations[rotation, destination], empty)
        checkpoint()

    face_area = cube_size**2
    distance = (np.abs((p % face_area) // cube_size - (b % face_area) // cube_size)
                + np.abs(p % cube_size - b % cube_size))
    destinations = geo.boss_moves[b]
    after_distance = (np.abs((p[:, None] % face_area) // cube_size - (destinations % face_area) // cube_size)
                      + np.abs(p[:, None] % cube_size - destinations % cube_size))
    allowed = ((destinations >= 0)
               & ((p[:, None] // face_area != b[:, None] // face_area)
                  | (after_distance == distance[:, None] - 1)))
    number_allowed = allowed.sum(axis=1)
    if np.any(number_allowed == 0):
        raise RuntimeError("Boss has no legal candidate in a nonterminal state")
    # All four turns following a legal move have equal mass. A terminal move
    # uses the same four columns even though the boss does not rotate.
    boss_weight = 1. / (number_allowed * 4)
    for neighbour in range(4):
        destination = destinations[:, neighbour]
        legal = allowed[:, neighbour]
        safe_destination = np.maximum(destination, 0)
        meeting = legal & (destination == p)
        eat = ((i == destination).astype(np.int8) + (j == destination).astype(np.int8))
        for turn in range(4):
            rotation = geo.actor_rotations[safe_destination, turn]
            first_moved = geo.rotations[rotation, np.where(i == destination, empty, i)]
            second_moved = geo.rotations[rotation, np.where(j == destination, empty, j)]
            target = index.get(geo.rotations[rotation, p],
                               geo.rotations[rotation, safe_destination], first_moved, second_moved)
            regular = legal & ~meeting
            if np.any(target[regular] < 0):
                raise RuntimeError("Incomplete boss state table")
            column = neighbour * 4 + turn
            boss_next[:, column] = np.where(meeting, -2, np.where(legal, target, -1))
            boss_eat[:, column] = np.where(regular, eat, 0)
        checkpoint()
    for array in (own_next, own_gain, boss_next, boss_eat, boss_weight, free_count):
        array.flags.writeable = False
    if pickup_slots is not None:
        pickup_slots.flags.writeable = False
    return _Tables((cube_size, move_distance, signature), index, own_next, own_gain, pickup_slots, boss_next, boss_eat,
                   boss_weight, free_count)


def _table_context(cube_size: int, move_distance: int, inspirations: tuple[int, ...]):
    group = geometry(cube_size=cube_size, move_distance=move_distance).symmetries
    signature = tuple(sorted(int(group[:, slot].min()) for slot in inspirations))
    return cube_size, move_distance, signature


def _tables(cube_size: int, move_distance: int, inspirations: tuple[int, ...], checkpoint: Callable) -> _Tables:
    key = _table_context(cube_size, move_distance, inspirations)
    while not _BUILD_LOCK.acquire(timeout=.05):
        checkpoint()
    try:
        if key in _TABLE_CACHE:
            _TABLE_CACHE.move_to_end(key)
            return _TABLE_CACHE[key]
        checkpoint()
        built = _build_tables(*key, checkpoint)
        checkpoint()
        if built.nbytes <= _TABLE_CACHE_LIMIT_BYTES:
            _TABLE_CACHE[key] = built
            size = sum(table.nbytes for table in _TABLE_CACHE.values())
            while size > _TABLE_CACHE_LIMIT_BYTES:
                _, evicted = _TABLE_CACHE.popitem(last=False)
                size -= evicted.nbytes
        return built
    finally:
        _BUILD_LOCK.release()


def _lex_better(candidate, current, selected, order):
    better = ~selected
    tied = selected.copy()
    for column, sign in order:
        difference = sign * (candidate[:, column] - current[:, column])
        better |= tied & (difference > _TOLERANCE)
        tied &= np.abs(difference) <= _TOLERANCE
    return better


def _boss_values(previous, tables: _Tables, rows, offsets, count, baseline: int, checkpoint: Callable):
    """Compute enemy expectations for a full lane or just the live player's successor rows."""
    weights = tables.boss_weight[rows, None]
    result = np.zeros((len(weights), 5))
    terminal_scores = np.broadcast_to(float(baseline) + count, (len(weights),))
    for action in range(16):
        target = tables.boss_next[rows, action]
        values = previous[offsets[count] + np.maximum(target, 0)]
        values[:, 3] += tables.boss_eat[rows, action]
        values[:, 4] += values[:, 0]
        terminal = target == -2
        values[terminal] = (1., 0., 0., 0., 1.)
        values[terminal, 1] = terminal_scores[terminal]
        values[target == -1] = 0.
        result += values * weights
        checkpoint()
    return result


def _step(previous, tables: _Tables, limits: tuple[int, ...], offsets, baseline: int, checkpoint: Callable, order):
    boss_values = np.empty_like(previous)
    for count, limit in enumerate(limits):
        start = offsets[count]
        boss_values[start:start + limit] = _boss_values(
            previous, tables, slice(0, limit), offsets, count, baseline, checkpoint)
    current = np.zeros_like(previous)
    for count, limit in enumerate(limits):
        start = offsets[count]
        best = current[start:start + limit]
        selected = np.zeros(limit, dtype=bool)
        for action in range(tables.own_next.shape[1]):
            target = tables.own_next[:limit, action]
            gain = tables.own_gain[:limit, action]
            values = boss_values[offsets[count + gain] + np.maximum(target, 0)]
            values[:, 2] += gain
            terminal = target == -2
            values[terminal] = (1., baseline + count, 0., 0., 1.)
            legal = target != -1
            take = legal & _lex_better(values, best, selected, order)
            best[take] = values[take]
            selected |= legal
            checkpoint()
        if not selected.all():
            raise RuntimeError("Nonterminal state has no player action")
    return current


def _continuation_values(tables: _Tables, limits: tuple[int, ...], offsets, horizon: int, baseline: int,
                         encounter_first: bool, checkpoint: Callable):
    """Reuse full future rounds while evaluating this round on the live state."""
    context = (encounter_first, baseline, tables.context)
    with _CONTINUATION_LOCK:
        reusable = max((key for key in _CONTINUATION_CACHE if key[:3] == context and key[3] <= horizon),
                       default=None)
        if reusable is None:
            values = np.zeros((sum(limits), 5))
            reused_rounds = 0
        else:
            _CONTINUATION_CACHE.move_to_end(reusable)
            values = _CONTINUATION_CACHE[reusable]
            reused_rounds = reusable[3]
    order = _ENCOUNTER_ORDER if encounter_first else _YIELD_ORDER
    for completed in range(reused_rounds + 1, horizon + 1):
        values = _step(values, tables, limits, offsets, baseline, checkpoint, order)
        checkpoint()
        values.flags.writeable = False
        key = (*context, completed)
        with _CONTINUATION_LOCK:
            values = _CONTINUATION_CACHE.setdefault(key, values)
            _CONTINUATION_CACHE.move_to_end(key)
            size = sum(item.nbytes for item in _CONTINUATION_CACHE.values())
            while (size > _CONTINUATION_CACHE_LIMIT_BYTES
                   or len(_CONTINUATION_CACHE) > _CONTINUATION_CACHE_LIMIT_ENTRIES):
                _, evicted = _CONTINUATION_CACHE.popitem(last=False)
                size -= evicted.nbytes
    return values, reused_rounds


def _cached_inspiration_continuation(context, horizon: int, collected: int,
                                     encounter_first: bool, checkpoint: Callable):
    """Find a completed layer whose state space includes the current layout."""
    cube_size, move_distance, orbits = context
    candidates = []
    with _CONTINUATION_LOCK:
        for key in reversed(_CONTINUATION_CACHE):
            objective, baseline, (size, distance, parents), depth = key
            if objective != encounter_first or depth != horizon or (size, distance) != (cube_size, move_distance):
                continue
            # A removed item accounts for either one pickup or one consumption.
            if (0 <= collected - baseline <= len(parents) - len(orbits)
                    and all(parents.count(orbit) >= orbits.count(orbit) for orbit in orbits)):
                candidates.append(key)
    if not candidates:
        return None
    # Prefer the smallest applicable profile, then the most recently used layer.
    candidates.sort(key=lambda key: len(key[2][2]))
    while not _BUILD_LOCK.acquire(timeout=.05):
        checkpoint()
    try:
        with _CONTINUATION_LOCK:
            for key in candidates:
                tables = _TABLE_CACHE.get(key[2])
                values = _CONTINUATION_CACHE.get(key)
                if tables is not None and values is not None:
                    _TABLE_CACHE.move_to_end(key[2])
                    _CONTINUATION_CACHE.move_to_end(key)
                    return tables, values, key[1]
    finally:
        _BUILD_LOCK.release()
    return None


def _index(tables: _Tables, player: int, boss: int, inspirations: tuple[int, ...]):
    empty = len(tables.index.player_orbit)
    first, second = (inspirations + (empty, empty))[:2]
    result = int(tables.index.get(player, boss, first, second))
    if result < 0:
        raise ValueError("Invalid or overlapping target coordinates")
    return result


def _after_player(action, inspirations, geo):
    remaining = tuple(i for i in inspirations if i != action.pickup_original_slot)
    gained = len(inspirations) - len(remaining)
    if action.rotation_id is not None:
        permutation = geo.rotations[action.rotation_id]
        remaining = tuple(sorted(int(permutation[i]) for i in remaining))
    return remaining, gained


def plan_inspiration(player: int, boss: int, inspirations: tuple[int, ...], turn_budget: int,
                     *, collected_count: int = 0, cancel_check: Callable | None = None,
                     time_budget_sec: float = 30., moves_left: int = 1,
                     rotations_left: int = 1, encounter_first: bool = False, cube_size: int = 3,
                     move_distance: int = 1) -> dict:
    """Optimize a known layout; no assumption about initial spawn is needed.

    collected_count supplies previously confirmed pickups for consistent
    rolling replanning. With encounter_first, deadline encounter probability
    takes precedence over inspiration yield at every decision. Up to four
    visible inspirations are supported. A large joint state space uses two
    reward turns followed by full-horizon pursuit; the returned solver
    metadata identifies that policy. The actual enemy branch should be
    observed before requesting the next plan.
    """
    started = time.monotonic()
    cube_size = validate_cube_size(cube_size)
    move_distance = validate_move_distance(move_distance)
    player, boss = validate_slot(player, cube_size=cube_size), validate_slot(boss, cube_size=cube_size)
    available_actions = remaining_player_actions(player, boss, moves_left, rotations_left, cube_size=cube_size,
                                                move_distance=move_distance)
    inspirations = tuple(validate_slot(i, cube_size=cube_size) for i in inspirations)
    if player == boss or len(set(inspirations)) != len(inspirations):
        raise ValueError("Player, boss and inspiration positions must be distinct")
    if len(inspirations) > MAX_FREE_INSPIRATIONS or player in inspirations or boss in inspirations:
        raise ValueError("Supports at most four non-overlapping free inspirations")
    if isinstance(turn_budget, bool) or not isinstance(turn_budget, int) or not 1 <= turn_budget <= 30:
        raise ValueError("turn_budget must be an integer from 1 to 30")
    if isinstance(collected_count, bool) or not isinstance(collected_count, int) or collected_count < 0:
        raise ValueError("collected_count must be a nonnegative integer")
    if not np.isfinite(time_budget_sec) or time_budget_sec <= 0:
        raise ValueError("time_budget_sec must be positive and finite")
    if moves_left == rotations_left == 0:
        return dict(status="waiting_enemy", success=False, next_action=None,
                    player_turn_actions=[], boss_branches=[], requires_observation_after_action=True)
    deadline = started + time_budget_sec
    checkpoint = lambda: _deadline_check(deadline, cancel_check)
    checkpoint()
    geo = geometry(cube_size=cube_size, move_distance=move_distance)
    options = [(action, *_after_player(action, inspirations, geo)) for action in available_actions]
    bounded_reason = None
    projected_bytes = None
    if turn_budget > 1 and len(inspirations) > 2:
        bounded_reason = "more_than_two_free_inspirations"
    elif turn_budget > 1 and len(inspirations) == 2:
        projected_bytes = _transition_byte_bound(geo, inspirations)
        if projected_bytes > _TABLE_CACHE_LIMIT_BYTES:
            bounded_reason = "projected_transition_storage_over_limit"
    if bounded_reason:
        pair_tables = _tables(cube_size, move_distance, (), checkpoint)
        chosen, metrics, solver, continuation_metrics = bounded_reward_choice(
            options, pair_tables, geo, cube_size, collected_count, turn_budget, encounter_first, checkpoint)
        solver["selection_reason"] = bounded_reason
        if projected_bytes is not None:
            solver.update(transition_bytes_upper_bound=projected_bytes, transition_bytes_limit=_TABLE_CACHE_LIMIT_BYTES)
    else:
        chosen, metrics, solver, continuation_metrics = _exact_choice(
            options, cube_size, move_distance, inspirations, collected_count, turn_budget, encounter_first, checkpoint)
    selected, selected_remaining, selected_gain = options[chosen]
    branches = []
    if not selected.terminal:
        for outcome in boss_outcomes(selected.final_player, selected.final_boss, cube_size=cube_size):
            checkpoint()
            survivors = tuple(i for i in selected_remaining if i != outcome.eat_original_slot)
            lost = len(selected_remaining) - len(survivors)
            if outcome.rotation_id is not None:
                permutation = geo.rotations[outcome.rotation_id]
                survivors = tuple(sorted(int(permutation[i]) for i in survivors))
            branch = dict(probability=outcome.probability, encounter=outcome.terminal,
                          boss_actions=deepcopy(list(outcome.sequence)),
                          player_cell=coord_dict(outcome.final_player, cube_size=cube_size),
                          boss_cell=coord_dict(outcome.final_boss, cube_size=cube_size),
                          inspiration_cells=[coord_dict(i, cube_size=cube_size) for i in survivors],
                          collected_count=collected_count + selected_gain,
                          newly_consumed_inspirations=lost,
                          remaining_rounds=turn_budget - 1)
            if not outcome.terminal:
                branch.update(continuation_metrics(outcome.final_player, outcome.final_boss, survivors))
            branches.append(branch)
    solver.update(cube_size=cube_size, move_distance=move_distance, completed_horizon=turn_budget,
                  elapsed_sec=time.monotonic() - started, remaining_free_inspiration_limit=MAX_FREE_INSPIRATIONS,
                  current_turn_partial=(moves_left, rotations_left) != (1, 1),
                  current_moves_left=int(moves_left), current_rotations_left=int(rotations_left))
    sequence = deepcopy(list(selected.sequence))
    objective = ("max_deadline_encounter_probability_then_inspiration" if encounter_first
                 else "max_expected_inspiration_at_encounter_before_deadline")
    if bounded_reason and turn_budget > 2:
        objective = ("max_deadline_encounter_probability_then_two_turn_inspiration" if encounter_first
                     else "max_two_turn_inspiration_at_encounter_before_deadline")
    return dict(
        status="solved", success=True,
        objective=objective,
        next_action=sequence[0], player_turn_actions=sequence,
        player_turn_terminal=selected.terminal, boss_branches=branches,
        planned_player_pickup_count=selected_gain,
        inspiration_after_player_turn=[coord_dict(i, cube_size=cube_size) for i in selected_remaining],
        metrics=metrics, solver=solver,
        guarantee_type="probabilistic", requires_observation_after_action=True,
    )


def _exact_choice(options, cube_size, move_distance, inspirations, collected_count, turn_budget, encounter_first, checkpoint):
    live_rows = [idx for idx, (action, _, _) in enumerate(options) if not action.terminal]
    option_values = np.zeros((len(options), 5))
    tables = None
    state_count = reused_rounds = 0
    already_picked_up = 0
    reused_subset = False
    if turn_budget > 1:
        horizon = turn_budget - 1
        context = _table_context(cube_size, move_distance, inspirations)
        cached = _cached_inspiration_continuation(context, horizon, collected_count, encounter_first, checkpoint)
        if cached is None:
            tables = _tables(cube_size, move_distance, inspirations, checkpoint)
            baseline = collected_count
        else:
            tables, continuation_values, baseline = cached
            already_picked_up = collected_count - baseline
            reused_rounds = horizon
            reused_subset = tables.context != context
        # After k pickups, only the prefix with at most |I|-k free items is
        # reachable. The offsets must match the table that supplied the values.
        limits = tuple(int(np.searchsorted(tables.free_count, free, side="right"))
                       for free in range(len(tables.context[2]), -1, -1))
        offsets = np.cumsum((0, *limits[:-1]), dtype=np.int32)
        state_count = limits[0]
        if cached is None:
            continuation_values, reused_rounds = _continuation_values(
                tables, limits, offsets, horizon, baseline, encounter_first, checkpoint)
        successors = np.array([_index(tables, options[k][0].final_player, options[k][0].final_boss, options[k][1])
                               for k in live_rows], dtype=np.int32)
        gains = np.array([options[k][2] for k in live_rows], dtype=np.int32)
        option_values[live_rows] = _boss_values(
            continuation_values, tables, successors, offsets, already_picked_up + gains, baseline, checkpoint)
    else:
        # No future player decisions. Resolve only the physical boss outcomes
        # of this turn; no canonical index or transition table is needed.
        for k in live_rows:
            action, remaining, gain = options[k]
            for outcome in boss_outcomes(action.final_player, action.final_boss, cube_size=cube_size):
                if outcome.terminal:
                    option_values[k] += outcome.probability * np.array((1., collected_count + gain, 0., 0., 1.))
                elif outcome.eat_original_slot in remaining:
                    option_values[k, 3] += outcome.probability
            checkpoint()
    order = _ENCOUNTER_ORDER if encounter_first else _YIELD_ORDER
    selected_idx = None
    selected_value = None
    for k, (action, remaining, gain) in enumerate(options):
        checkpoint()
        if action.terminal:
            value = np.array((1., float(collected_count + gain), float(gain), 0., 1.))
        else:
            value = option_values[k]
            value[2] += gain
        if selected_idx is None or _lex_better(value[None, :], selected_value[None, :], np.ones(1, bool), order)[0]:
            selected_idx, selected_value = k, value
    if selected_idx is None:
        raise RuntimeError("No legal player action selected")
    probability, score, picked_up, consumed, moment = map(float, selected_value)

    def continuation_metrics(player, boss, free):
        probability_after = score_after = 0.
        if tables is not None:
            target = _index(tables, player, boss, free)
            lane = already_picked_up + options[selected_idx][2]
            probability_after, score_after = continuation_values[offsets[lane] + target, :2]
        return dict(continuation_deadline_probability=float(probability_after),
                    continuation_expected_successful_total_inspirations=float(score_after))

    metrics = dict(deadline_encounter_probability=probability,
                   expected_new_inspirations=picked_up,
                   expected_successful_new_inspirations=max(0., score - collected_count * probability),
                   expected_successful_total_inspirations=score,
                   expected_boss_consumed_inspirations=consumed,
                   expected_encounter_turns_given_deadline=moment / probability if probability else None)
    solver = dict(method="finite_horizon_exhaustive_dynamic_programming", state_count=state_count,
                  reused_future_rounds=reused_rounds, reused_inspiration_subset=reused_subset,
                  model_optimality="numerical_exhaustive",
                  comparison_tolerance=_TOLERANCE)
    return selected_idx, metrics, solver, continuation_metrics
