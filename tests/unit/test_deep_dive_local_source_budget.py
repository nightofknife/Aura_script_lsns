"""A cooldown or new same-pose frame cannot repeat an exhausted local search."""
from copy import deepcopy
import math

import numpy as np
import pytest

from test_deep_dive_local_planning_target_copy import subject
from plans.resonance_pc.src.actions._deep_dive_scan_policy import _matrix


def exhaust(policy, rows, observed, axes):
    task = deepcopy(policy._local_task)
    before = deepcopy(rows)
    for now in (72., 73.):
        policy._local_goal = None
        assert policy._set_local_route(observed, rows, axes, now)
    assert policy._local_attempts == 2
    assert rows == before
    return task


def test_same_real_source_cannot_restart_after_activation_and_cooldown():
    policy, rows, observed, axes = subject()
    task = exhaust(policy, rows, observed, axes)
    policy._local_task = None
    assert policy._tasks(rows, policy._mixed_feedback, task['origin'], 100.) == []
    policy._local_task = task
    policy._local_attempts = 0
    policy._local_started = 100.
    policy._local_goal = None
    assert not policy._set_local_route(observed, rows, axes, 100.)
    assert policy.summary()['local_source_budgets'][0]['attempts'] == 2


def test_second_route_is_allowed_to_finish_without_getting_third_budget():
    policy, rows, observed, axes = subject()
    task = exhaust(policy, rows, observed, axes)
    # Parent.choose still needs the signature to retain an in-flight route.
    tasks = policy._tasks(rows, policy._mixed_feedback, task['origin'], 73.1)
    assert any(item['signature'] == task['signature'] for item in tasks)
    assert not policy._set_local_route(observed, rows, axes, 73.2)


@pytest.mark.parametrize('degrees,allowed', [(0., False), (7.9, False), (9., True)])
def test_new_frame_only_reopens_budget_with_real_independent_pose(degrees, allowed):
    policy, rows, observed, axes = subject()
    task = exhaust(policy, rows, observed, axes)
    source = deepcopy(policy._target_sources[823])
    source['stamp'] += 1.
    source['base_rotation'] = _matrix(np.array([math.radians(degrees), 0., 0.])) @ source['base_rotation']
    basis = np.array(policy._mixed_feedback['geometry_body_basis'])
    source['rotation'] = source['base_rotation'] @ basis
    policy._target_sources[824] = source
    task.update(source_frame_id=824, origin=source['base_rotation'] @ basis)
    policy._local_task, policy._local_goal, policy._local_attempts = task, None, 0
    assert policy._local_budget_available(task) is allowed
    assert policy._set_local_route(observed, rows, axes, 100.) is allowed
    assert len(policy._local_source_budgets) == (2 if allowed else 1)


def test_body_correction_does_not_reopen_same_source_budget():
    policy, rows, observed, axes = subject()
    task = exhaust(policy, rows, observed, axes)
    old_basis = np.array(policy._mixed_feedback['geometry_body_basis'])
    new_basis = old_basis @ _matrix(np.array([.12, -.11, .03]))
    policy._mixed_feedback['geometry_body_basis'] = new_basis.tolist()
    task['origin'] = task['origin'] @ old_basis.T @ new_basis
    policy._local_task = task
    assert not policy._local_budget_available(task)
    assert len(policy._local_source_budgets) == 1


def test_changed_context_clears_budget_before_invalid_basis_return():
    policy, rows, observed, axes = subject()
    exhaust(policy, rows, observed, axes)
    changed = deepcopy(policy._mixed_feedback)
    changed['map_revision'] += 1
    changed['geometry_body_basis'] = None
    assert policy._remember_target_source(changed) is None
    assert policy._local_source_budgets == {}


def test_changed_source_timestamp_without_new_orientation_cannot_replenish():
    policy, rows, observed, axes = subject()
    task = exhaust(policy, rows, observed, axes)
    policy._target_sources[823]['stamp'] += 1.
    assert not policy._local_budget_available(task)
