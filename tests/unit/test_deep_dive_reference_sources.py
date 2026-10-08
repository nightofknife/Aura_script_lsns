"""Reference stability needs two distinct renderer frames, not repeated reads."""
from plans.resonance_pc.src.actions import consciousness_deep_dive_planned_run_pc_actions as flow


def frame():
    return dict(grid_cells=[dict(centre=[i, 0]) for i in range(27)],
                actor_operation_slot=4, Q_candidates=[dict(symmetry_id=1)])


def source(session='wgc', generation=1, stamp=10.):
    return dict(session_id=session, generation=generation, frame_time=stamp)


def test_repeat_and_out_of_order_frames_do_not_increase_stability():
    state = {}
    assert not flow._stable_current_reference(state, frame(), source())
    assert not flow._stable_current_reference(state, frame(), source())
    assert not flow._stable_current_reference(state, frame(), source(generation=0, stamp=9.))
    assert not flow._stable_current_reference(state, frame(), source(generation=2, stamp=10.))
    assert state['stable'] == 1
    assert flow._stable_current_reference(state, frame(), source(generation=2, stamp=10.1))


def test_new_capture_session_starts_a_new_stability_pair():
    state = {}
    assert not flow._stable_current_reference(state, frame(), source())
    assert not flow._stable_current_reference(state, frame(), source('rebind', 5, 11.))
    assert state['stable'] == 1
    assert flow._stable_current_reference(state, frame(), source('rebind', 6, 11.1))


def test_missing_capture_source_cannot_promote_reference():
    state = {}
    assert not flow._stable_current_reference(state, frame(), source())
    assert not flow._stable_current_reference(state, frame(), None)
    assert state == {}
