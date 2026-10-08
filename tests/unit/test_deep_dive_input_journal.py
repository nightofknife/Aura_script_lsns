"""Diagnostic journal provenance, boundedness and physical-basis contracts."""
import json
from copy import deepcopy

import pytest

from plans.resonance_pc.src.actions._deep_dive_input_journal import InputJournal


IDENTITY = [[1.,0.,0.],[0.,1.,0.],[0.,0.,1.]]


def sample(**changes):
    value = dict(seq=7, frame_id=18, generation=90, session_id=123,
                 map_revision=0, frame_time=100., published_at=100.03,
                 quality=.9, tracking_ok=True, geometry_tracking_ok=True,
                 rotation=deepcopy(IDENTITY), geometry_body_basis=deepcopy(IDENTITY),
                 correction_epoch=0)
    value.update(changes)
    return value


def test_actual_completed_input_and_cancelled_completion_are_preserved():
    journal = InputJournal()
    assert journal.record_input(sample(), 3, [4,-2], [19,8], 100.1,100.12,True)
    row = journal.payload()['records']['input'][0]
    assert row['before_at'] == 100.1 and row['completed_at'] == 100.12
    assert row['displacement'] == [4,-2] and row['cumulative'] == [19,8]
    assert row['cancelled'] is True and row['grip_id'] == 3
    assert row['source_frame_time'] == 100. and row['context_partition'] == 0


@pytest.mark.parametrize('changes', [
    {'frame_id':None}, {'generation':True}, {'generation':-1},
    {'frame_time':float('inf')}, {'frame_time':-.1},
])
def test_actual_input_with_missing_source_is_retained_as_invalid(changes):
    journal = InputJournal()
    assert journal.record_input(sample(**changes),1,[2,0],[2,0],100.1,100.2,True)
    row = journal.payload()['records']['input'][0]
    assert row['status'] == 'invalid_atomic_source'
    assert row['cancelled'] is True and row['displacement'] == [2,0]
    json.dumps(journal.payload(),allow_nan=False)


def test_numpy_matrices_convert_but_numpy_atomic_header_does_not_gain_native_credit():
    import numpy as np
    journal = InputJournal()
    assert journal.record_geometry(sample(rotation=np.eye(3), geometry_body_basis=np.eye(3)))
    assert journal.payload()['records']['geometry'][0]['base_rotation'] == IDENTITY
    other = sample(frame_id=np.int64(19),generation=np.int64(91),seq=8,frame_time=100.1)
    assert journal.record_geometry(other)
    assert journal.payload()['records']['geometry'][1]['status'] == 'invalid_atomic_source'
    assert 'base_rotation' not in journal.payload()['records']['geometry'][1]
    assert journal.record_input(other,1,np.array([2,0],dtype=int),np.array([2,0],dtype=int),100.2,100.3)
    assert journal.payload()['records']['input'][0]['status'] == 'invalid_atomic_source'
    assert not journal.record_input(sample(),1,[2,0],[2,0],np.float64(100.2),100.3)


@pytest.mark.parametrize('delta,total,before,completed,cancelled', [
    ([1.2,0],[1,0],1.,2.,False), ([True,0],[1,0],1.,2.,False),
    ([1,0],[1.,0.],1.,2.,False), ([1,0],[1,0],2.,1.,False),
    ([1,0],[1,0],1.,float('nan'),False), ([1,0],[1,0],1.,2.,1),
])
def test_invalid_actual_inputs_do_not_invent_a_record(delta,total,before,completed,cancelled):
    journal = InputJournal()
    assert not journal.record_input(sample(),0,delta,total,before,completed,cancelled)
    assert journal.summary()['counts']['input'] == 0
    assert sum(journal.summary()['rejected']['input'].values()) == 1


def test_atomic_geometry_not_repeated_by_semantic_revision_or_changed_seq():
    journal = InputJournal()
    assert journal.record_geometry(sample(semantic_revision=1))
    assert not journal.record_geometry(sample(semantic_revision=2))
    assert not journal.record_geometry(sample(seq=8, semantic_revision=3))
    assert journal.summary()['counts']['geometry'] == 1
    assert journal.record_geometry(sample(seq=8, frame_id=19,generation=91,frame_time=100.1))


def test_applied_right_body_correction_cancels_in_base_rotation():
    journal = InputJournal()
    physical = [[1.,0.,0.],[0.,0.,-1.],[0.,1.,0.]]
    basis = [[0.,-1.,0.],[1.,0.,0.],[0.,0.,1.]]
    corrected = [[0.,-1.,0.],[0.,0.,-1.],[1.,0.,0.]]
    assert journal.record_geometry(sample(rotation=corrected,geometry_body_basis=basis,correction_epoch=1))
    row = journal.payload()['records']['geometry'][0]
    assert row['base_rotation'] == physical
    assert row['rotation'] == corrected and row['geometry_body_basis'] == basis


@pytest.mark.parametrize('changes,reason', [
    ({'geometry_body_basis':None},'invalid_body_basis'),
    ({'geometry_body_basis':[[1.,0.,0.],[0.,1.,0.],[0.,0.,-1.]]},'invalid_body_basis'),
    ({'rotation':[[float('nan'),0.,0.],[0.,1.,0.],[0.,0.,1.]]},'invalid_rotation'),
    ({'session_id':True},'invalid_atomic_source'),
    ({'generation':None},'invalid_atomic_source'),
])
def test_invalid_basis_or_source_retained_without_fake_identity(changes,reason):
    journal = InputJournal()
    assert journal.record_geometry(sample(**changes))
    row = journal.payload()['records']['geometry'][0]
    assert row['status'] == reason
    assert 'base_rotation' not in row
    json.dumps(journal.payload(),allow_nan=False)


def test_contexts_are_partitioned_without_claiming_context_continuity():
    journal = InputJournal()
    assert journal.record_geometry(sample())
    assert journal.record_geometry(sample(session_id=124))
    assert journal.record_geometry(sample(map_revision=1))
    assert journal.record_input(sample(session_id=True),1,[2,0],[2,0],100.1,100.2)
    rows = journal.payload()['records']
    assert [r['context_partition'] for r in rows['geometry']] == [0,1,2]
    assert rows['input'][0]['context_partition'] is None
    assert rows['input'][0]['status'] == 'invalid_source_context'


def test_cap_is_append_only_independent_per_channel_and_explicit():
    journal = InputJournal(max_records=1)
    assert journal.record_input(sample(),1,[1,0],[1,0],100.1,100.2)
    assert not journal.record_input(sample(),1,[2,0],[3,0],100.3,100.4)
    assert journal.record_geometry(sample())
    assert not journal.record_geometry(sample(seq=8,frame_id=19,generation=91,frame_time=100.1))
    payload = journal.payload()
    assert payload['counts'] == {'input':1,'geometry':1}
    assert payload['truncated'] == {'input':True,'geometry':True}
    assert payload['overflow'] == {'input':1,'geometry':1}
    assert payload['records']['input'][0]['displacement'] == [1,0]
    assert payload['records']['geometry'][0]['frame_id'] == 18


def test_payload_is_copied_and_contains_no_readiness_or_vote_credit():
    journal = InputJournal()
    source = sample()
    assert journal.record_geometry(source)
    source['rotation'][0][0] = 77
    payload = journal.payload()
    payload['records']['geometry'][0]['rotation'][0][0] = 88
    assert journal.payload()['records']['geometry'][0]['rotation'][0][0] == 1.
    assert not {'ready','fresh','votes','pose_support'} & journal.payload().keys()
    json.dumps(journal.payload(),allow_nan=False)
