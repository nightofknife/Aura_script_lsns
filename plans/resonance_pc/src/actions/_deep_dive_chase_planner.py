"""Optimal stochastic pursuit under the standard deep-dive turn rules.

The 2,862 distinct player/boss pairs are reduced by the cube's 24 rotations.
Expected-time optimization uses finite-horizon lower bounds and evaluates an
actual stationary policy for its upper bound. No symbolic algebra or analysis
directory is required. A deadline goal is a separate finite-horizon objective.
"""
from __future__ import annotations

from dataclasses import dataclass
from copy import deepcopy
from numbers import Integral
import time
from typing import Callable

import numpy as np

from ._deep_dive_planner_rules import (
    RULES_VERSION, PlayerAction, action_dict, boss_branch_dict, boss_outcomes,
    coord_dict, geometry, player_actions, remaining_player_actions, validate_slot,
)


class _Interrupted(Exception):
    def __init__(self, status: str):
        self.status = status


class _Budget:
    def __init__(self, seconds: float, cancel: Callable | None):
        if not np.isfinite(seconds) or seconds <= 0:
            raise ValueError("time_budget_sec must be finite and positive")
        self.deadline = time.monotonic() + seconds
        self.cancel = cancel

    def check(self):
        if self.cancel is not None and self.cancel():
            raise _Interrupted("cancelled")
        if time.monotonic() >= self.deadline:
            raise _Interrupted("search_budget_exhausted")


@dataclass(frozen=True)
class _Graph:
    states: tuple[tuple[int, int], ...]
    pair_indices: np.ndarray
    next_states: np.ndarray
    weights: np.ndarray
    legal: np.ndarray
    distributions: tuple[tuple[tuple[tuple[int, float], ...], ...], ...]
    guaranteed_turns: np.ndarray


_GRAPH: _Graph | None = None
_EXPECTED: dict | None = None


def _action_distribution(action: PlayerAction, pair_indices: np.ndarray) -> tuple:
    if action.terminal:
        return ((-1, 1.0),)
    mass: dict[int, float] = {}
    for outcome in boss_outcomes(action.final_player, action.final_boss):
        state = -1 if outcome.terminal else int(pair_indices[outcome.final_player, outcome.final_boss])
        mass[state] = mass.get(state, 0.0) + outcome.probability
    return tuple(sorted(mass.items()))


def _get_graph(budget: _Budget) -> _Graph:
    global _GRAPH
    if _GRAPH is not None:
        return _GRAPH
    group = geometry().symmetries
    representatives = {}
    for player in range(54):
        budget.check()
        for boss in range(54):
            if player != boss:
                pair_keys = group[:, player] * 54 + group[:, boss]
                key = int(pair_keys.min())
                representatives[player, boss] = divmod(key, 54)
    states = tuple(sorted(set(representatives.values())))
    index = {state: i for i, state in enumerate(states)}
    pair_indices = np.full((54, 54), -1, dtype=np.int64)
    for pair, representative in representatives.items():
        pair_indices[pair] = index[representative]
    distributions = []
    for player, boss in states:
        budget.check()
        distributions.append(tuple(sorted({_action_distribution(action, pair_indices)
                                            for action in player_actions(player, boss)})))
    max_actions = max(map(len, distributions))
    next_states = np.full((len(states), max_actions, 16), -1, dtype=np.int64)
    weights = np.zeros_like(next_states, dtype=np.float64)
    legal = np.zeros((len(states), max_actions), dtype=bool)
    for s, actions in enumerate(distributions):
        for a, branches in enumerate(actions):
            legal[s, a] = True
            for k, (successor, weight) in enumerate(branches):
                next_states[s, a, k] = successor
                weights[s, a, k] = weight
    # The fixed point also certifies the complement: every action has some
    # nonterminal successor still outside the finite-guarantee set.
    guaranteed = np.full(len(states), np.inf)
    while True:
        budget.check()
        extended = np.r_[guaranteed, 0.0]
        continuation = np.where(weights > 0, extended[next_states], 0.0).max(axis=2)
        updated = np.where(legal, 1.0 + continuation, np.inf).min(axis=1)
        if np.array_equal(updated, guaranteed):
            break
        guaranteed = updated
    for array in (pair_indices, next_states, weights, legal, guaranteed):
        array.flags.writeable = False
    graph = _Graph(states, pair_indices, next_states, weights, legal, tuple(distributions), guaranteed)
    _GRAPH = graph
    return graph


def _expected_candidates(graph: _Graph, values: np.ndarray) -> np.ndarray:
    extended = np.r_[values, 0.0]
    return np.where(graph.legal, 1.0 + (extended[graph.next_states] * graph.weights).sum(axis=2), np.inf)


def _evaluate_policy(graph: _Graph, policy: np.ndarray) -> tuple[np.ndarray, float, float]:
    size = len(graph.states)
    matrix = np.eye(size)
    for state in range(size):
        for successor, weight in graph.distributions[state][int(policy[state])]:
            if successor >= 0:
                matrix[state, successor] -= weight
    values = np.linalg.solve(matrix, np.ones(size))
    if not np.isfinite(values).all() or np.min(values) < 1.0 - 1e-8:
        raise np.linalg.LinAlgError("Selected policy is not a proper absorbing policy")
    applied = matrix @ values
    residual = float(np.max(np.abs(applied - 1.0)))
    # M = I-P is an absorbing-chain M-matrix. M^-1 is nonnegative; its
    # infinity norm is max(M^-1 1), the longest expected lifetime. The
    # residual times that lifetime bounds policy evaluation error. An extra
    # floating arithmetic allowance is explicit, rather than claiming exact
    # rational optimality from a small Bellman residual.
    roundoff = float(np.finfo(np.float64).eps * size * max(1.0, np.max(values)) * 16)
    minimum_drift = float(np.min(applied)) - roundoff
    if minimum_drift <= 0:
        raise np.linalg.LinAlgError("Policy evaluation cannot certify absorption")
    amplification = float(np.max(values)) / minimum_drift
    evaluation_bound = (residual + roundoff) * amplification
    return values, evaluation_bound, residual


def _solve_expected(graph: _Graph, budget: _Budget, tolerance: float = 1e-9) -> dict:
    global _EXPECTED
    if _EXPECTED is not None:
        return _EXPECTED
    lower = np.zeros(len(graph.states))
    best = None
    for depth in range(1, 10001):
        budget.check()
        lower = _expected_candidates(graph, lower).min(axis=1)
        if depth == 1 or depth % 4 == 0:
            policy = _expected_candidates(graph, lower).argmin(axis=1)
            try:
                upper, evaluation_bound, residual = _evaluate_policy(graph, policy)
            except np.linalg.LinAlgError:
                continue
            # Truncated optimal costs are lower bounds. Explicitly allow
            # accumulated floating arithmetic error in that bound too.
            lower_roundoff = float(np.finfo(np.float64).eps * depth * max(1.0, lower.max()) * 32)
            error_bound = float(np.maximum(upper - lower, 0).max()) + evaluation_bound + lower_roundoff
            bellman_residual = float(np.max(np.abs(_expected_candidates(graph, upper).min(axis=1) - upper)))
            best = dict(values=upper, lower=lower.copy(), policy=policy,
                        error_bound=error_bound, evaluation_error_bound=evaluation_bound,
                        policy_equation_residual=residual, bellman_residual=bellman_residual,
                        iterations=depth, tolerance=tolerance, converged=error_bound <= tolerance)
            if best["converged"]:
                for field in ("values", "lower", "policy"):
                    best[field].flags.writeable = False
                _EXPECTED = best
                return best
    return best or dict(converged=False)


def _fixed_policy_deadline(graph: _Graph, policy: np.ndarray, turns: int, budget: _Budget) -> tuple:
    probability = np.zeros(len(graph.states))
    moment = np.zeros(len(graph.states))
    rows = np.arange(len(graph.states))
    selected_next = graph.next_states[rows, policy]
    selected_weights = graph.weights[rows, policy]
    for _ in range(turns):
        budget.check()
        extension = np.r_[probability, 1.0]
        moment_extension = np.r_[moment + probability, 1.0]
        probability = (extension[selected_next] * selected_weights).sum(axis=1)
        moment = (moment_extension[selected_next] * selected_weights).sum(axis=1)
    return probability, moment


def _deadline_solution(graph: _Graph, turns: int, budget: _Budget) -> dict:
    probability = np.zeros(len(graph.states))
    moment = np.zeros(len(graph.states))
    policies = []
    for _ in range(turns):
        budget.check()
        extension = np.r_[probability, 1.0]
        moment_extension = np.r_[moment + probability, 1.0]
        candidates = (extension[graph.next_states] * graph.weights).sum(axis=2)
        candidate_moments = (moment_extension[graph.next_states] * graph.weights).sum(axis=2)
        candidates = np.where(graph.legal, candidates, -np.inf)
        maxima = candidates.max(axis=1)
        tied = graph.legal & (candidates == maxima[:, None])
        policy = np.where(tied, candidate_moments, np.inf).argmin(axis=1)
        rows = np.arange(len(graph.states))
        probability = candidates[rows, policy]
        moment = candidate_moments[rows, policy]
        policies.append(policy)
    return dict(probability=probability, moment=moment, policies=policies)


def _physical_action(graph: _Graph, player: int, boss: int, policy: np.ndarray) -> PlayerAction:
    state = int(graph.pair_indices[player, boss])
    distribution = graph.distributions[state][int(policy[state])]
    # Whole-cube normalization only indexes values. Return primitives in the
    # caller's original coordinates, with their actual world-axis directions.
    return next(action for action in player_actions(player, boss)
                if _action_distribution(action, graph.pair_indices) == distribution)


def _partial_choice(graph: _Graph, expected: dict, player: int, boss: int, turns: int,
                    moves_left: int, rotations_left: int, objective: str, budget: _Budget) -> dict:
    """Finish this turn, then continue with complete future turns only."""
    fast_probability, fast_moment = _fixed_policy_deadline(graph, expected["policy"], turns - 1, budget)
    deadline = _deadline_solution(graph, turns - 1, budget)
    candidates = []
    for action in remaining_player_actions(player, boss, moves_left, rotations_left):
        budget.check()
        expected_turns = 1.0
        fast_p = fast_m = deadline_p = deadline_m = 0.0
        worst = 0.0
        for successor, weight in _action_distribution(action, graph.pair_indices):
            if successor == -1:
                fast_p += weight
                fast_m += weight
                deadline_p += weight
                deadline_m += weight
            else:
                expected_turns += weight * expected["values"][successor]
                fast_p += weight * fast_probability[successor]
                fast_m += weight * (fast_moment[successor] + fast_probability[successor])
                deadline_p += weight * deadline["probability"][successor]
                deadline_m += weight * (deadline["moment"][successor] + deadline["probability"][successor])
                worst = max(worst, graph.guaranteed_turns[successor])
        candidates.append(dict(action=action, expected=expected_turns, fast_p=fast_p, fast_m=fast_m,
                               deadline_p=deadline_p, deadline_m=deadline_m, worst=1.0 + worst))
    if not candidates:
        raise ValueError("No remaining player action")
    fastest = min(candidates, key=lambda candidate: candidate["expected"])
    best_deadline = max(candidates, key=lambda candidate: (candidate["deadline_p"], -candidate["deadline_m"]))
    use_deadline = objective == "max_deadline_encounter_probability"
    chosen = best_deadline if use_deadline else fastest
    return dict(action=chosen["action"], selected_expected=chosen["expected"],
                optimal_expected=fastest["expected"],
                probability=chosen["deadline_p"] if use_deadline else chosen["fast_p"],
                moment=chosen["deadline_m"] if use_deadline else chosen["fast_m"],
                maximum_deadline=best_deadline["deadline_p"], fastest_deadline=fastest["fast_p"],
                guaranteed=min(candidate["worst"] for candidate in candidates),
                continuation_policy=deadline["policies"][-1] if use_deadline and turns > 1
                else expected["policy"])


def plan_chase(player: int, boss: int, turn_budget: int, cancel_check=None,
               time_budget_sec: float = 30.0,
               objective: str = "min_expected_encounter_turns", *,
               moves_left: int = 1, rotations_left: int = 1) -> dict:
    """Return the next logical turn and stochastic continuation information.

    min_expected_encounter_turns selects an unlimited stationary policy and
    reports its actual deadline probability. max_deadline_encounter_probability
    selects a budget-dependent policy; success-time averages then condition on
    meeting by that deadline. Neither objective promises a finite worst case.
    """
    started = time.monotonic()
    player, boss = validate_slot(player), validate_slot(boss)
    remaining_player_actions(player, boss, moves_left, rotations_left)
    if (not isinstance(turn_budget, Integral) or isinstance(turn_budget, bool)
            or not 0 <= turn_budget <= 256):
        raise ValueError("turn_budget must be an integer in [0, 256]")
    turn_budget = int(turn_budget)
    if objective not in ("min_expected_encounter_turns", "max_deadline_encounter_probability"):
        raise ValueError("Unsupported chase objective")
    result = dict(strategy="chase", objective=objective, rules_version=RULES_VERSION,
                  turn_budget=turn_budget, player=coord_dict(player), boss=coord_dict(boss),
                  rule_assumptions=["one face-local adjacent move and one layer rotation per player turn",
                                    "boss chooses uniformly among legal face-local moves then four rotations",
                                    "same-face boss moves only reduce Manhattan distance",
                                    "move collision ends the turn before any remaining rotation",
                                    "node events, extra actions and battle victory are outside this model"],
                  next_action=None, player_turn_actions=[], predicted_boss_branches=[])
    if player == boss:
        result.update(status="already_encountered", expected_turns=0.0, optimal_expected_turns=0.0,
                      deadline_probability=1.0, guaranteed_max_turns=0,
                      solver_converged=True, proven_optimal=True, terminal=True)
        return result
    if turn_budget == 0:
        result.update(status="deadline_exhausted", deadline_probability=0.0,
                      expected_turns=None, solver_converged=True, proven_optimal=True, terminal=False)
        return result
    if moves_left == rotations_left == 0:
        result.update(status="waiting_enemy", solver_converged=True, proven_optimal=False, terminal=False)
        return result
    budget = _Budget(time_budget_sec, cancel_check)
    try:
        budget.check()
        graph = _get_graph(budget)
        expected = _solve_expected(graph, budget)
        if not expected.get("converged"):
            raise _Interrupted("search_budget_exhausted")
        use_deadline = objective == "max_deadline_encounter_probability"
        partial_turn = moves_left != 1 or rotations_left != 1
        if partial_turn:
            partial = _partial_choice(graph, expected, player, boss, turn_budget,
                                      moves_left, rotations_left, objective, budget)
            chosen = partial["action"]
            probability, success_moment = float(partial["probability"]), float(partial["moment"])
            guaranteed = partial["guaranteed"]
            optimal_expected, selected_expected = float(partial["optimal_expected"]), float(partial["selected_expected"])
            maximum_deadline, fastest_deadline = float(partial["maximum_deadline"]), float(partial["fastest_deadline"])
            continuation_policy = partial["continuation_policy"]
        else:
            state = int(graph.pair_indices[player, boss])
            fastest_probability, fastest_moment = _fixed_policy_deadline(
                graph, expected["policy"], turn_budget, budget)
            deadline = _deadline_solution(graph, turn_budget, budget)
            policy = deadline["policies"][-1] if use_deadline else expected["policy"]
            chosen = _physical_action(graph, player, boss, policy)
            probability = float(deadline["probability"][state] if use_deadline else fastest_probability[state])
            success_moment = float(deadline["moment"][state] if use_deadline else fastest_moment[state])
            guaranteed = graph.guaranteed_turns[state]
            optimal_expected = selected_expected = float(expected["values"][state])
            maximum_deadline, fastest_deadline = float(deadline["probability"][state]), float(fastest_probability[state])
            continuation_policy = (deadline["policies"][-2] if use_deadline and turn_budget > 1
                                   else expected["policy"])
        branch_dicts = []
        if not chosen.terminal:
            for outcome in boss_outcomes(chosen.final_player, chosen.final_boss):
                branch = boss_branch_dict(outcome)
                branch["remaining_turns"] = turn_budget - 1
                if not outcome.terminal and turn_budget > 1:
                    successor_action = _physical_action(graph, outcome.final_player, outcome.final_boss,
                                                        continuation_policy)
                    branch["next_player_turn_actions"] = deepcopy(list(successor_action.sequence))
                branch_dicts.append(branch)
        result.update(status="solved", terminal=chosen.terminal,
                      next_action=deepcopy(chosen.sequence[0]), player_turn_actions=deepcopy(list(chosen.sequence)),
                      selected_action=action_dict(chosen), predicted_boss_branches=branch_dicts,
                      expected_turns=(success_moment / probability if probability > 0 else None)
                      if use_deadline else selected_expected,
                      expected_turns_scope="conditional_on_encounter_by_deadline" if use_deadline
                      else ("remaining_current_turn_then_unlimited_stationary_full_turns" if partial_turn
                            else "unlimited_rounds_under_selected_stationary_policy"),
                      optimal_expected_turns=optimal_expected,
                      deadline_probability=probability,
                      maximum_deadline_probability=maximum_deadline,
                      fastest_expected_policy_deadline_probability=fastest_deadline,
                      conditional_expected_turns_by_deadline=success_moment / probability if probability > 0 else None,
                      guaranteed_max_turns=int(guaranteed) if np.isfinite(guaranteed) else None,
                      guarantee_scope="minimum worst-case turn bound over all modeled policies",
                      solver_converged=True,
                      # Finite DP enumerates every modeled action. Unlimited
                      # numerical evaluation instead states its error bound.
                      proven_optimal=use_deadline,
                      numerically_optimal_within_error_bound=True,
                      certificate=dict(type="finite_horizon_lower_bound_and_absorbing_policy_upper_bound",
                                       expected_turns_absolute_error_bound=expected["error_bound"],
                                       policy_evaluation_error_bound=expected["evaluation_error_bound"],
                                       policy_equation_residual=expected["policy_equation_residual"],
                                       bellman_residual=expected["bellman_residual"],
                                       floating_point_arithmetic=True, exact_rational_certificate=False,
                                       requested_tolerance=expected["tolerance"],
                                       value_iteration_depth=expected["iterations"],
                                       symmetry_classes=len(graph.states), symmetry_count=24,
                                       deadline_dp_completed=True, current_turn_partial=partial_turn))
    except _Interrupted as interrupted:
        result.update(status=interrupted.status, solver_converged=False, proven_optimal=False,
                      expected_turns=None, deadline_probability=None)
    finally:
        result["computation_elapsed_sec"] = time.monotonic() - started
    return result
