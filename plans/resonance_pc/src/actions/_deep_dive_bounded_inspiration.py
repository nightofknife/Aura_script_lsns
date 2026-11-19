"""Two reward turns over all visible inspirations, with full-horizon pursuit values."""
from __future__ import annotations

from functools import lru_cache

import numpy as np

from ._deep_dive_planner_rules import boss_outcomes


def _successors(values, targets, meeting, illegal=0):
    # The -2/-1 indices select these two entries. Keep the Python integer
    # dtype even when a terminal numerator grows past machine integer range.
    return np.concatenate((values, np.array([meeting, illegal], dtype=object)))[targets]


def _deadline_by_tile(tables, horizon: int, slot_count: int, checkpoint):
    """Best success probability and success-time moment for picking each tile.

    At depth h each numerator has denominator 48**h. Each player's choice
    precedes 4*d equally weighted enemy columns (d = 1, 2, 3 or 4), so these
    integer Bellman updates retain even extremely close primary differences.
    """
    rows = np.arange(len(tables.free_count))
    probability = np.zeros(len(rows), dtype=object)
    moment = np.zeros_like(probability)
    unit = 1
    weights = 48 // (tables.boss_next != -1).sum(axis=1)
    for depth in range(1, horizon + 1):
        checkpoint()
        boss_p = _successors(probability, tables.boss_next, unit).sum(axis=1) * weights
        boss_m = _successors(moment + probability, tables.boss_next, unit).sum(axis=1) * weights
        unit *= 48
        action_p = _successors(boss_p, tables.own_next, unit, illegal=-1)
        action_m = _successors(boss_m, tables.own_next, unit)
        probability = action_p.max(axis=1)
        moment = np.where(action_p == probability[:, None], action_m, (depth + 1) * unit).min(axis=1)

    # Each full action can pick up at most one tile. Index its best possible
    # (P, -M) by the tile's position BEFORE either primitive of the action.
    tile_p = np.full((len(rows), slot_count + 1), -1, dtype=object)
    tile_m = np.zeros_like(tile_p)
    for column in range(action_p.shape[1]):
        checkpoint()
        slots = tables.pickup_slots[:, column]
        p, m = action_p[:, column], action_m[:, column]
        old_p, old_m = tile_p[rows, slots], tile_m[rows, slots]
        take = (p > old_p) | ((p == old_p) & (m < old_m))
        tile_p[rows[take], slots[take]] = p[take]
        tile_m[rows[take], slots[take]] = m[take]
    return probability, moment, tile_p, tile_m, unit


def bounded_reward_choice(options, tables, geo, cube_size, collected, turns, encounter_first, checkpoint):
    """Reward decisions this turn and next, followed by deadline-optimal pursuit."""
    slots = len(geo.moves)
    probability, moment, tile_p, tile_m, unit = _deadline_by_tile(tables, turns - 1, slots, checkpoint)

    @lru_cache(maxsize=None)
    def pair_frame(player, boss):
        group = geo.symmetries
        frame = int(np.argmin(group[:, player] * slots + group[:, boss]))
        index = int(tables.index.get(player, boss, slots, slots))
        return index, group[frame]

    def rank(p, score, gain, m):
        return (p, score, gain, -m) if encounter_first else (score, p, gain, -m)

    def next_turn(player, boss, free, held):
        index, frame = pair_frame(player, boss)
        p, m = probability[index], moment[index]
        best = (p, held * p, 0, m)
        for slot in free:
            tile = frame[slot]
            pickup_p = tile_p[index, tile]
            if pickup_p < 0:
                continue
            pickup = (pickup_p, (held + 1) * pickup_p, 1, tile_m[index, tile])
            if rank(*pickup) > rank(*best):
                best = pickup
        # If the unconstrained action itself picked a visible item, the
        # corresponding pickup candidate dominates its zero-reward surrogate.
        return best

    total_unit = 48 * unit
    best_key = None
    for index, (action, remaining, gain) in enumerate(options):
        checkpoint()
        held = collected + gain
        if action.terminal:
            p, score, pickups, m = total_unit, held * total_unit, 48 * gain, total_unit
        else:
            p = score = m = 0
            pickups = 48 * gain
            for outcome in boss_outcomes(action.final_player, action.final_boss, cube_size=cube_size):
                # The rules choose uniformly among d moves and, if still
                # playing, four rotations. Every branch mass is a multiple of 1/48.
                weight = round(48 * outcome.probability)
                if outcome.terminal:
                    p += weight * unit
                    score += weight * held * unit
                    m += weight * unit
                    continue
                surviving = tuple(int(geo.rotations[outcome.rotation_id, slot]) for slot in remaining
                                  if slot != outcome.eat_original_slot)
                next_p, next_score, next_gain, next_m = next_turn(
                    outcome.final_player, outcome.final_boss, surviving, held)
                p += weight * next_p
                score += weight * next_score
                pickups += weight * next_gain
                m += weight * (next_m + next_p)
        key = rank(p, score, pickups, m)
        if best_key is None or key > best_key:
            best_index, best_key, best_values = index, key, (p, score, pickups, m)

    p, score, pickups, m = best_values
    covered = turns == 2
    metrics = dict(deadline_encounter_probability=p / total_unit,
                   expected_encounter_turns_given_deadline=m / p if p else None,
                   expected_successful_total_inspirations=score / total_unit if covered else None,
                   expected_successful_new_inspirations=(score - collected * p) / total_unit if covered else None,
                   expected_new_inspirations=pickups / 48 if covered else None,
                   expected_boss_consumed_inspirations=None,
                   reward_lookahead_successful_total_inspirations=score / total_unit,
                   reward_lookahead_expected_new_inspirations=pickups / 48)
    solver = dict(method="finite_horizon_pair_dp_with_two_reward_turns", state_count=len(probability),
                  model_optimality=("exact_deadline_probability_with_bounded_reward_lookahead" if encounter_first
                                    else "bounded_reward_lookahead_then_deadline_pursuit"),
                  probability_arithmetic="integer_numerator_over_48_to_horizon",
                  reward_lookahead_turns=2, full_horizon_reward_optimized=covered,
                  continuation_policy="two_reward_turns_then_max_deadline_probability_then_min_success_time")
    held = collected + options[best_index][2]

    def continuation_metrics(player, boss, free):
        p, score, _, _ = next_turn(player, boss, free, held)
        return dict(continuation_deadline_probability=p / unit,
                    continuation_expected_successful_total_inspirations=score / unit if covered else None,
                    continuation_reward_lookahead_successful_total_inspirations=score / unit)

    return best_index, metrics, solver, continuation_metrics
