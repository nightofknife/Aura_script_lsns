"""Navigation-only framing source and finite pair contracts."""
from copy import deepcopy

import pytest
from plans.resonance_pc.src.actions import _deep_dive_target_framing as module


def packet(index=50, kind='inspiration'):
    identity = [[1., 0., 0.], [0., 1., 0.], [0., 0., 1.]]
    context = (123, 0)
    face = 'URFDLB'[index//9]
    faces = {'U': 7, face: 3}
    interest = dict(index=index, kind=kind, source_frame_id=963,
                    source_frame_time=100., context=context, expires=112., attempts=0)
    source = dict(session_id=123, map_revision=0, frame_id=963, frame_time=100.,
                  rotation=identity, body_basis=identity, accepted_faces=faces)
    pose = dict(rotation=identity, tvec=[[0.], [.5], [42.]], map_revision=0)
    proof = dict(source_frame_id=963, source_frame_time=100., source_map_revision=0,
                 rotation=identity, tvec=[0., .5, 42.], target_face_glyph_count=3,
                 error=.08, margin=.31, anchor_uncertainty=dict(ready=True, winner_index=index))
    candidate = dict(kind=kind, cell_index=index, confidence=.804, confirmable=True,
                     box=[599, 592, 73, 51], point=[635.822, 617.351], association_evidence=proof)
    feedback = dict(session_id=123, map_revision=0, accepted_anchor_observation=source,
                    semantic_metadata=dict(session_id=123, map_revision=0, frame_id=963,
                                           generation=39, frame_time=100., pose=pose),
                    refine_diagnostic=dict(renewed=True, source_frame_id=963, source_frame_time=100.,
                                           source_map_revision=0, accepted_faces=deepcopy(faces)),
                    target_coverage=dict(model_executed=True, coverage_valid=True,
                                         source_frame_id=963, source_frame_time=100., source_map_revision=0),
                    glyph_anchor_at=100., glyph_anchor_frame_id=963, glyph_anchor_age_sec=.5,
                    target_candidate_associations=[candidate])
    projection = dict(session_id=123, map_revision=0, frame_id=963, frame_time=100.,
                      face=face, cell_index=index, rotation=identity, tvec=[0., .5, 42.], cosine=.88,
                      quad=[[625.7,545.2],[709.,588.3],[679.2,651.7],[597.,609.1]])
    return interest, feedback, candidate, projection


def freeze(parts=None, **kwargs):
    interest, feedback, candidate, projection = parts or packet()
    return module.freeze_framing_interest(interest, context=(123,0), now=101.,
        source_feedback=feedback, candidate=candidate, source_projection=projection, **kwargs)


def test_creation_copies_original_budget_and_pixel_proof():
    parts = packet()
    result = freeze(parts)
    assert result['status'] == 'valid'
    assert result['interest'] == parts[0]
    assert result['source_projection']['quad'] == parts[3]['quad']
    parts[0]['expires'] = 999.
    parts[2]['box'][0] = 0
    assert result['interest']['expires'] == 112.
    assert result['candidate']['box'][0] == 599
    assert 'votes' not in result and 'ready' not in result


@pytest.mark.parametrize('field,value', [('expires',101.),('attempts',2),('context',(124,0)),
    ('source_frame_id',964),('source_frame_time',100.1),('index',49),('kind','player')])
def test_existing_budget_identity_is_not_recreated(field,value):
    parts = packet()
    parts[0][field] = value
    assert freeze(parts)['status'] == 'invalid'


@pytest.mark.parametrize('change', ['missing_proof','different_rotation','different_tvec',
    'old_projection','failed_model','no_renewal','wrong_winner','weak_source','borrowed_counts','bool_context'])
def test_mismatched_or_missing_original_source_fail_closed(change):
    parts = packet()
    interest, feedback, candidate, projection = parts
    if change == 'missing_proof': candidate.pop('association_evidence')
    if change == 'different_rotation': projection['rotation'] = [[0.,-1.,0.],[1.,0.,0.],[0.,0.,1.]]
    if change == 'different_tvec': projection['tvec'][1] += .01
    if change == 'old_projection': projection['frame_id'] -= 1
    if change == 'failed_model': feedback['target_coverage']['model_executed'] = False
    if change == 'no_renewal': feedback['refine_diagnostic']['renewed'] = False
    if change == 'wrong_winner': candidate['association_evidence']['anchor_uncertainty']['winner_index'] = 49
    if change == 'weak_source': candidate['confidence'] = .249
    if change == 'borrowed_counts': feedback['refine_diagnostic']['accepted_faces'] = {'U':7,'B':1}
    if change == 'bool_context': interest['context'] = (123,False)
    assert freeze(parts)['status'] == 'invalid'


def test_retained_positive_requires_original_packet_and_existing_budget():
    interest, feedback, candidate, projection = packet()
    evidence = dict(frame_id=963, occupant='inspiration', confidence=.804,
                    target_box=deepcopy(candidate['box']), target_point=deepcopy(candidate['point']),
                    association_evidence=deepcopy(candidate['association_evidence']))
    result = module.freeze_framing_interest(interest, context=(123,0), now=101.,
        source_feedback=feedback, retained_evidence=evidence, source_projection=projection)
    assert result['status'] == 'valid' and result['interest']['expires'] == 112.
    evidence['target_box'][0] += 1
    assert module.freeze_framing_interest(interest, context=(123,0), now=101.,
        source_feedback=feedback, retained_evidence=evidence, source_projection=projection)['status'] == 'invalid'


def select(rows, options, records=None, **kwargs):
    records = [freeze()] if records is None else records
    return module.select_target_framing_pair(options, readable=rows,
        endpoint_valid=kwargs.pop('endpoint_valid',[True]*len(rows)), face_indices=list(range(45,54)),
        interests=records,
        current_budgets=kwargs.pop('current_budgets',{r['interest']['index']:deepcopy(r['interest']) for r in records}),
        context=kwargs.pop('context',(123,0)),
        now=kwargs.pop('now',102.), **kwargs)


def rows_with(indices):
    return [[1. if i in visible else 0. for i in range(54)] for visible in indices]


def test_target_must_be_readable_at_both_endpoints():
    rows = rows_with([range(45,54),[45,46,47,48,49,51],range(45,54)])
    options = [dict(left=0,right=1,eligible=True,utility=999.),
               dict(left=0,right=2,eligible=True,utility=1.)]
    assert select(rows,options)['option_ordinal'] == 1
    assert select(rows,options[:1])['reason'] == 'no_target_readable_pair'


def test_union_nine_then_count_then_original_utility():
    rows = rows_with([[45,46,47,48,49,50],[45,46,47,48,49,50],
                      [45,46,47,50,51,52,53],[45,46,47,48,50,51,52,53]])
    options = [dict(left=0,right=1,eligible=True,utility=100.),
               dict(left=0,right=2,eligible=True,utility=1.),
               dict(left=0,right=3,eligible=True,utility=2.)]
    result = select(rows,options)
    assert result['option_ordinal'] == 2 and result['full_union']


@pytest.mark.parametrize('eligible,valid', [(False,[True,True]),(True,[True,False])])
def test_original_geometry_path_eligibility_never_bypassed(eligible,valid):
    result = select(rows_with([range(45,54)]*2),[dict(left=0,right=1,eligible=eligible,utility=9.)],
                    endpoint_valid=valid)
    assert result['status'] == 'partial' and result['fallback_required']


def test_expired_interest_cannot_rebirth_but_original_pairs_remain():
    record = freeze()
    saved = deepcopy(record)
    rows = rows_with([[45,46,47,48,49,51]]*2)
    result = select(rows,[dict(left=0,right=1,eligible=True,utility=1.)],records=[record],now=112.)
    assert result['status'] == 'selected' and result['target_indices'] == []
    assert record == saved


def test_foreign_context_has_no_target_constraint_or_budget_refresh():
    result = select(rows_with([[45,46,47,48,49,51]]*2),
        [dict(left=0,right=1,eligible=True,utility=1.)],context=(124,0))
    assert result['target_indices'] == []


def test_nonfinite_readability_fail_closed():
    rows = rows_with([range(45,54)]*2)
    rows[0][50] = float('nan')
    assert select(rows,[dict(left=0,right=1,eligible=True,utility=1.)])['status'] == 'partial'


def test_other_face_interest_does_not_steal_page():
    record = freeze(packet(index=31,kind='singularity'))
    result = select(rows_with([[45,46,47,48,49,51]]*2),
        [dict(left=0,right=1,eligible=True,utility=1.)],records=[record])
    assert result['target_indices'] == []


def test_missing_or_rewritten_frozen_snapshot_cannot_impose_target():
    record = freeze()
    record['interest']['index'] = 31
    result = select(rows_with([range(45,54)]*2),
        [dict(left=0,right=1,eligible=True,utility=1.)],records=[record])
    assert result['reason'] == 'missing_or_rewritten_frozen_proof'


def test_authoritative_attempt_exhaustion_and_ttl_rewrite():
    record = freeze()
    budget = deepcopy(record['interest'])
    budget['attempts'] = 2
    options = [dict(left=0,right=1,eligible=True,utility=1.)]
    rows = rows_with([[45,46,47,48,49,51]]*2)
    result = select(rows,options,records=[record],current_budgets={50:budget})
    assert result['status'] == 'selected' and result['target_indices'] == []
    budget['attempts'] = 0
    budget['expires'] += 12.
    result = select(rows,options,records=[record],current_budgets={50:budget})
    assert result['status'] == 'partial' and result['reason'] == 'original_budget_rewritten'


def test_numpy_native_arrays_preserve_numeric_and_bool_gates():
    import numpy as np
    result = select(np.asarray(rows_with([range(45,54)]*2),dtype=np.float64),
        [dict(left=0,right=1,eligible=True,utility=np.float64(1.))],endpoint_valid=np.asarray([True,True]))
    assert result['status'] == 'selected'


@pytest.mark.parametrize('candidates', [None, False, {}])
def test_missing_original_candidate_list_fails_closed(candidates):
    parts = packet()
    parts[1]['target_candidate_associations'] = candidates
    assert freeze(parts)['status'] == 'invalid'


def test_missing_original_body_basis_fails_closed():
    parts = packet()
    parts[1]['accepted_anchor_observation'].pop('body_basis')
    assert freeze(parts)['status'] == 'invalid'


def test_private_planning_row_uses_only_frozen_source_pixels():
    result = freeze()
    candidate, projection = result['candidate'], result['source_projection']
    row = dict(occupant=result['interest']['kind'], evidence=[dict(
        occupant=candidate['kind'], confidence=candidate['confidence'],
        target_box=deepcopy(candidate['box']), target_point=deepcopy(candidate['point']),
        quad=deepcopy(projection['quad']), cosine=projection['cosine'],
        association_evidence=deepcopy(candidate['association_evidence']))])
    assert row['evidence'][0]['association_evidence']['source_frame_id'] == 963
    assert row['evidence'][0]['quad'][-2][1] > 615
    assert 'occupant_status' not in row
