from copy import deepcopy

from plans.resonance_pc.src.actions._deep_dive_four_view_candidate_closure import resolve_candidate_closure
from plans.resonance_pc.src.actions._deep_dive_planner_rules import slot_to_cell


EPOCH = 'scan1'
DIGEST = 'a'*64


def source(group):
    return dict(generation_source='atomic_wgc', capture_backend='wgc', session_id=17,
                generation=group, frame_id=group, frame_time=float(group), map_revision=0)


def coord(slot):
    c = slot_to_cell(slot)
    return dict(face=c.face, row=c.row, col=c.col)


def candidate(slot, projected=False):
    return dict(coord(slot), cell_index=slot, candidate_only=projected,
        projection_evidence=dict(source_rgb_sha256=DIGEST, actual_fit_supported=True,
            visible_fit_rms_px=1.2, inlier_count=9, actual_matched_data=[{'actual': 'quad'}]) if projected else None)


def direct(slot, group, kind='inspiration'):
    return dict(kind=kind, cell_index=slot, cell=coord(slot), confidence=.9, confirmable=True,
        scan_epoch=EPOCH, source=source(group), group=group, frame_id=group,
        target_packet=dict(point=[100+slot, 200], box=[90+slot, 190, 20, 20]),
        candidates=[candidate(slot)], track_id=slot)


def owner(slot=31, groups=(1, 2), kind='inspiration'):
    return dict(kind=kind, cell_index=slot, cell=coord(slot), confidence=.9,
        occupant_status='confirmed', independent_view_count=len(groups), track_ids=[slot],
        evidence=[dict(source=source(g), frame_id=g, group=g, scan_epoch=EPOCH,
                       candidates=[candidate(slot)]) for g in groups])


def view(group, slots=(31, 32), complete=True):
    return dict(view=1, group=group, source=source(group), target_detection_complete=complete,
        cells=[dict(coord(slot), quad=[[10, 10], [60, 10], [60, 60], [10, 60]]) for slot in slots],
        nodes=[dict(coord(slot), source=source(group), view_group=group, stable=True,
                    scan_epoch=EPOCH, reading=dict(icon_id='blue_scales')) for slot in slots])


def peek(group=3, slots=(31, 32)):
    return dict(kind='inspiration', cell_index=None, confidence=.8, confirmable=True,
        group=group, source=source(group), frame_id=group, scan_epoch=EPOCH,
        source_rgb_sha256=DIGEST, candidates=[candidate(slot, True) for slot in slots],
        body_candidate_fit_supported=True, body_candidate_fit_source=source(group),
        body_candidate_fit_evidence=dict(source_rgb_sha256=DIGEST),
        candidate_projection_complete=True, target_packet=dict(point=[400, 500], box=[390, 490, 20, 20]))


def scenario():
    return [view(g) for g in (1, 2, 3)], [direct(31, 1), direct(31, 2), peek()], dict(targets=[owner()])


def test_alias_closes_only_through_actual_direct_negative_views():
    views, rows, fused = scenario()
    result = resolve_candidate_closure(views, rows, fused, scan_epoch=EPOCH)
    assert not result['unresolved']
    assert result['explained'][0]['cell_index'] == 31
    assert result['explained'][0]['ownership_status'] == 'identity_explained_body_candidate'
    assert result['explained'][0]['adds_ownership_group'] is False
    assert result['negative_evidence'][32]['group_count'] == 3
    assert rows[-1]['cell_index'] is None
    assert result['raw_associations'][-1]['cell_index'] is None
    assert result['effective_associations'][-1]['cell_index'] == 31
    assert not result['adds_positive_evidence']


def test_two_negatives_are_insufficient():
    views, rows, fused = scenario()
    views.pop()
    result = resolve_candidate_closure(views, rows, fused, scan_epoch=EPOCH)
    assert len(result['unresolved']) == 1
    assert not result['explained']


def test_three_timestamps_at_one_group_are_one_negative_view():
    views, rows, fused = scenario()
    views = [view(1)]*3
    assert resolve_candidate_closure(views, rows, fused, scan_epoch=EPOCH)['negative_evidence'][32]['group_count'] == 1


def test_related_direct_candidate_blocks_negative_even_unassigned():
    views, rows, fused = scenario()
    blocker = direct(32, 3)
    blocker.update(cell_index=None, cell=None, confirmable=False, confidence=.3)
    rows.append(blocker)
    result = resolve_candidate_closure(views, rows, fused, scan_epoch=EPOCH)
    assert result['negative_evidence'][32]['group_count'] == 2
    assert result['unresolved']


def test_hidden_candidates_never_erase_actual_visible_negative():
    views, rows, fused = scenario()
    assert resolve_candidate_closure(views, rows, fused, scan_epoch=EPOCH)['negative_evidence'][32]['group_count'] == 3


def test_projected_nodes_never_become_negative():
    views, rows, fused = scenario()
    views[2]['cells'][1]['candidate_only'] = True
    result = resolve_candidate_closure(views, rows, fused, scan_epoch=EPOCH)
    assert result['negative_evidence'][32]['group_count'] == 2
    assert result['unresolved']


def test_partial_detection_view_is_not_negative_proof():
    views, rows, fused = scenario()
    views[2]['target_detection_complete'] = False
    assert resolve_candidate_closure(views, rows, fused, scan_epoch=EPOCH)['unresolved']


def test_mixed_session_cannot_supply_third_negative():
    views, rows, fused = scenario()
    views[2]['source']['session_id'] = 99
    result = resolve_candidate_closure(views, rows, fused, scan_epoch=EPOCH)
    assert result['reason'] == 'mixed_closure_view_source'
    assert not result['explained']


def test_foreign_epoch_cannot_supply_negative():
    views, rows, fused = scenario()
    views[2]['nodes'][1]['scan_epoch'] = 'old'
    assert resolve_candidate_closure(views, rows, fused, scan_epoch=EPOCH)['unresolved']


def test_two_confirmed_owners_are_ambiguous_not_category_winner():
    views, rows, fused = scenario()
    fused['targets'].append(owner(32))
    rows.extend([direct(32, 1), direct(32, 2)])
    assert resolve_candidate_closure(views, rows, fused, scan_epoch=EPOCH)['unresolved']


def test_single_actual_owner_group_cannot_be_promoted_by_declared_count():
    views, rows, fused = scenario()
    fused['targets'][0]['evidence'].pop()
    assert resolve_candidate_closure(views, rows, fused, scan_epoch=EPOCH)['unresolved']


def test_unique_candidate_without_actual_fit_does_not_close():
    views, rows, fused = scenario()
    rows[-1]['candidates'] = [candidate(31, True)]
    rows[-1]['body_candidate_fit_supported'] = False
    assert resolve_candidate_closure(views, rows, fused, scan_epoch=EPOCH)['unresolved']


def test_empty_or_incomplete_candidate_sets_remain_pending():
    for change in ({'candidates': []}, {'candidate_projection_complete': False}):
        views, rows, fused = scenario()
        rows[-1].update(change)
        assert resolve_candidate_closure(views, rows, fused, scan_epoch=EPOCH)['unresolved']


def test_candidate_projection_from_other_rgb_stays_pending():
    views, rows, fused = scenario()
    rows[-1]['candidates'][0]['projection_evidence']['source_rgb_sha256'] = 'b'*64
    assert resolve_candidate_closure(views, rows, fused, scan_epoch=EPOCH)['unresolved']


def test_owner_not_in_candidates_cannot_explain_unique_category():
    views, rows, fused = scenario()
    rows[-1]['candidates'] = [candidate(32, True)]
    assert resolve_candidate_closure(views, rows, fused, scan_epoch=EPOCH)['unresolved']


def test_tracker_explanations_rewrite_effective_rows_without_new_groups():
    views, rows, fused = scenario()
    pending = rows[-1]
    pending['target_packet'] = dict(point=[132, 203], box=[122, 193, 20, 20])
    pending['track_id'] = 31
    history = []
    for row in rows:
        history.append(dict(source=row['source'], vertical_verified=True,
                            point=row['target_packet']['point'], box=row['target_packet']['box'],
                            match_candidates=[] if not history else [[.1, 31]]))
        row['tracking_evidence'] = dict(reason='measured_consecutive_vertical_trajectory',
            track_id=31, trajectory=deepcopy(history))
    explained = deepcopy(pending)
    explained.update(cell_index=31, adds_ownership_group=False)
    fused['identity_explained_candidates'] = [explained]
    pending['body_candidate_fit_supported'] = False
    result = resolve_candidate_closure(views, rows, fused, scan_epoch=EPOCH)
    assert not result['unresolved']
    assert result['effective_associations'][-1]['cell_index'] == 31
    assert result['explained'][0]['ownership_status'] == 'measured_track_identity_with_direct_owner'
    assert result['explained'][0]['adds_ownership_group'] is False


def segment_neighbors():
    views, rows, fused = scenario()
    seed = rows[-1]
    seed['track_id'] = 99
    pending = peek(4)
    pending.update(track_id=99, candidates=[], anchor_point=None)
    pending['target_packet'] = dict(point=[401, 502], box=[391, 492, 20, 20])
    history = []
    for row in (seed, pending):
        history.append(dict(source=row['source'], vertical_verified=True,
            point=row['target_packet']['point'], box=row['target_packet']['box'],
            match_candidates=[] if not history else [[.1, 99]]))
        row['tracking_evidence'] = dict(reason='measured_consecutive_vertical_trajectory',
            track_id=99, segment_start_reason='new_segment_after_observation_gap',
            previous_ids_not_linked=[31], trajectory=deepcopy(history))
    rows.append(pending); views.append(view(4))
    return views, rows, fused


def test_anchorless_animation_frame_uses_actual_body_explained_segment_neighbor():
    views, rows, fused = segment_neighbors()
    result = resolve_candidate_closure(views, rows, fused, scan_epoch=EPOCH)
    assert not result['unresolved']
    resolved = result['effective_associations'][-1]
    assert resolved['cell_index'] == 31
    assert resolved['ownership_status'] == 'identity_explained_measured_segment_neighbor'
    assert resolved['adds_ownership_group'] is False
    assert len(resolved['closure_evidence']['actual_segment_path']) == 2
    assert fused['targets'][0]['independent_view_count'] == 2
    assert fused['targets'][0]['track_ids'] == [31]  # new segment99 did not link to old31


def test_anchorless_frame_requires_zero_trajectory_competition():
    views, rows, fused = segment_neighbors()
    rows[-1]['tracking_evidence']['trajectory'][-1]['match_candidates'] = [[.1, 99], [.9, 100]]
    result = resolve_candidate_closure(views, rows, fused, scan_epoch=EPOCH)
    assert any(row['frame_id'] == 4 for row in result['unresolved'])


def test_anchorless_gap_above_four_seconds_remains_pending():
    views, rows, fused = segment_neighbors()
    pending = rows[-1]
    pending['source'] = source(9)
    pending['frame_id'] = 9
    pending['tracking_evidence']['trajectory'][-1]['source'] = source(9)
    result = resolve_candidate_closure(views, rows, fused, scan_epoch=EPOCH)
    assert any(row['frame_id'] == 9 for row in result['unresolved'])


def test_new_segment_never_joins_old_id_by_category():
    views, rows, fused = segment_neighbors()
    rows[-1]['track_id'] = 100
    rows[-1]['tracking_evidence']['track_id'] = 100
    result = resolve_candidate_closure(views, rows, fused, scan_epoch=EPOCH)
    assert any(row['frame_id'] == 4 for row in result['unresolved'])


def test_trajectory_pixel_packet_mismatch_does_not_explain_anchorless_frame():
    views, rows, fused = segment_neighbors()
    rows[-1]['tracking_evidence']['trajectory'][-1]['point'] = [410, 510]
    result = resolve_candidate_closure(views, rows, fused, scan_epoch=EPOCH)
    assert any(row['frame_id'] == 4 for row in result['unresolved'])


def test_same_segment_two_valid_neighbor_owners_is_ambiguous():
    views, rows, fused = segment_neighbors()
    rows[-2]['candidates'] = [candidate(31, True)]
    fused['targets'].append(owner(32))
    rows.extend([direct(32, 1), direct(32, 2)])
    second = peek(5, slots=(32,))
    second.update(track_id=99, target_packet=dict(point=[402, 503], box=[392, 493, 20, 20]))
    history = deepcopy(rows[3]['tracking_evidence']['trajectory'])
    history.append(dict(source=source(5), point=[402, 503], box=[392, 493, 20, 20],
                        vertical_verified=True, match_candidates=[[.1, 99]]))
    second['tracking_evidence'] = dict(reason='measured_consecutive_vertical_trajectory',
                                     track_id=99, trajectory=history)
    rows.append(second); views.append(view(5))
    result = resolve_candidate_closure(views, rows, fused, scan_epoch=EPOCH)
    assert any(row['frame_id'] == 4 for row in result['unresolved'])


def test_visible_partial_anchor_with_continuous_slow_nn_transitions_can_explain():
    views, rows, fused = segment_neighbors()
    pending = rows[-1]
    pending.update(anchor_point=[401, 502], source=source(6), frame_id=6)
    pending['source']['frame_time'] = 7.4  # endpoints4.4s, three real observed steps
    path = deepcopy(pending['tracking_evidence']['trajectory'][:1])
    for frame, timestamp, point in ((4, 4.5, [400.4, 500.8]), (5, 6., [400.7, 501.4]), (6, 7.4, [401, 502])):
        capture = source(frame); capture['frame_time'] = timestamp
        path.append(dict(source=capture, point=point, box=[point[0]-10, point[1]-10, 20, 20],
                         vertical_verified=True, match_candidates=[[.1, 99]]))
    pending['tracking_evidence']['trajectory'] = path
    views[-1]['source'] = deepcopy(pending['source'])
    for node in views[-1]['nodes']:
        node['source'] = deepcopy(pending['source'])
    result = resolve_candidate_closure(views, rows, fused, scan_epoch=EPOCH)
    assert not result['unresolved']
    explanation = result['effective_associations'][-1]
    assert explanation['cell_index'] == 31
    assert explanation['closure_evidence']['endpoint_gap_sec'] > 4.
    assert len(explanation['closure_evidence']['actual_segment_path']) == 4


def test_complete_longer_path_is_valid_within_same_standard_view_block():
    views, rows, fused = segment_neighbors()
    pending = rows[-1]
    pending.update(source=source(7), frame_id=7)
    path = deepcopy(pending['tracking_evidence']['trajectory'][:1])
    for frame in range(4, 8):
        point = [400+(frame-3)/4, 500+(frame-3)/2]
        path.append(dict(source=source(frame), point=point, box=[point[0]-10, point[1]-10, 20, 20],
                         vertical_verified=True, match_candidates=[[.1, 99]]))
    pending['tracking_evidence']['trajectory'] = path
    views[-1]['source'] = deepcopy(pending['source'])
    for node in views[-1]['nodes']:
        node['source'] = deepcopy(pending['source'])
    result = resolve_candidate_closure(views, rows, fused, scan_epoch=EPOCH)
    assert not result['unresolved']
    explanation = result['effective_associations'][-1]
    assert explanation['closure_evidence']['actual_transition_count'] == 4
    assert explanation['closure_evidence']['standard_view_block'] == 1


def test_existing_geometry_candidate_excluding_owner_cannot_be_overwritten_by_segment():
    views, rows, fused = segment_neighbors()
    rows[-1]['candidates'] = [candidate(32, True)]
    result = resolve_candidate_closure(views, rows, fused, scan_epoch=EPOCH)
    assert any(row['frame_id'] == 4 for row in result['unresolved'])


def test_continuous_neighborhood_still_rejects_one_transition_over_four_seconds():
    views, rows, fused = segment_neighbors()
    pending = rows[-1]
    pending.update(source=source(9), frame_id=9)
    path = deepcopy(pending['tracking_evidence']['trajectory'][:1])
    path.append(dict(source=source(8), point=[400.5, 501], box=[390.5, 491, 20, 20],
                     vertical_verified=True, match_candidates=[[.1, 99]]))
    path.append(dict(source=source(9), point=[401, 502], box=[391, 492, 20, 20],
                     vertical_verified=True, match_candidates=[[.1, 99]]))
    pending['tracking_evidence']['trajectory'] = path
    views[-1]['source'] = deepcopy(pending['source'])
    for node in views[-1]['nodes']:
        node['source'] = deepcopy(pending['source'])
    result = resolve_candidate_closure(views, rows, fused, scan_epoch=EPOCH)
    assert any(row['frame_id'] == 9 for row in result['unresolved'])


def test_measured_identity_never_propagates_across_standard_face_blocks():
    views, rows, fused = segment_neighbors()
    views[-1]['view'] = 2
    result = resolve_candidate_closure(views, rows, fused, scan_epoch=EPOCH)
    assert any(row['frame_id'] == 4 for row in result['unresolved'])


def test_disagreeing_encoded_group_and_actual_view_block_is_invalid():
    views, rows, fused = segment_neighbors()
    for actual in views:
        actual['group'] += 100
        for node in actual['nodes']:
            node['view_group'] += 100
    for row in rows:
        row['group'] += 100
    for evidence in fused['targets'][0]['evidence']:
        evidence['group'] += 100
    views[-1]['view'] = 2  # group104 encodes1, declared2 contradicts
    result = resolve_candidate_closure(views, rows, fused, scan_epoch=EPOCH)
    assert any(row['frame_id'] == 4 for row in result['unresolved'])


def direct_cross_block_scenario():
    views, rows, fused = scenario()
    pending = rows[-1]
    pending.update(track_id=31, candidates=[], body_candidate_fit_supported=False,
                   target_packet=dict(point=[132, 203], box=[122, 193, 20, 20]))
    history = []
    for row in rows:
        history.append(dict(source=row['source'], vertical_verified=True,
            point=row['target_packet']['point'], box=row['target_packet']['box'],
            match_candidates=[] if not history else [[.1, 31]]))
        row['tracking_evidence'] = dict(reason='measured_consecutive_vertical_trajectory',
            track_id=31, trajectory=deepcopy(history))
    views[-1]['view'] = 2
    explained = deepcopy(pending); explained['cell_index'] = 31
    fused['identity_explained_candidates'] = [explained]
    return views, rows, fused


def test_actual_direct_owner_can_explain_intact_trajectory_across_view_blocks():
    views, rows, fused = direct_cross_block_scenario()
    result = resolve_candidate_closure(views, rows, fused, scan_epoch=EPOCH)
    assert not result['unresolved']
    resolved = result['effective_associations'][-1]
    assert resolved['cell_index'] == 31
    assert resolved['closure_evidence']['direct_owner_seed'] is True
    assert resolved['closure_evidence']['permits_cross_block_actual_trajectory'] is True
    assert resolved['adds_ownership_group'] is False


def test_direct_cross_block_trajectory_still_rejects_competition():
    views, rows, fused = direct_cross_block_scenario()
    rows[-1]['tracking_evidence']['trajectory'][-1]['match_candidates'] = [[.1, 31], [.8, 32]]
    assert resolve_candidate_closure(views, rows, fused, scan_epoch=EPOCH)['unresolved']


def test_direct_cross_block_trajectory_does_not_override_conflicting_candidates():
    views, rows, fused = direct_cross_block_scenario()
    rows[-1]['candidates'] = [candidate(32, True)]
    assert resolve_candidate_closure(views, rows, fused, scan_epoch=EPOCH)['unresolved']
