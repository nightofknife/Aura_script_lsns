from copy import deepcopy

import numpy as np
import pytest

from plans.resonance_pc.src.actions import _deep_dive_required_cell_reader as reader
from plans.resonance_pc.src.actions._deep_dive_planner_rules import coord_dict
from plans.resonance_pc.src.actions import consciousness_deep_dive_scan_pc_actions as scan


def atlas():
    cells = [dict(coord_dict(i), occupant='unknown', occupant_status='unknown',
                  node_status='unknown', confidence=0., evidence=[], occupant_evidence_counts={})
             for i in range(54)]
    for i in range(0, 54, 9):
        cells[i]['evidence'] = [dict(frame_id=i+1, group=1, occupant='none')]
    for slot, kind in ((4, 'player'), (31, 'singularity'), (3, 'inspiration'), (35, 'inspiration')):
        cells[slot].update(occupant=kind, occupant_status='confirmed', confidence=.9,
                          node_status='not_required_target', occupant_evidence_counts={kind: 2},
                          evidence=[dict(frame_id=90+g, group=g, occupant=kind) for g in (1, 2)])
    return dict(schema='resonance_pc.deep_dive_layout.v1', coordinate_frame='scan_local',
                cells=cells, faces_observed=6, layout_complete=False,
                player_cell=coord_dict(4), singularity_cell=coord_dict(31),
                inspiration_cells=[coord_dict(3), coord_dict(35)], expected_inspirations=2,
                diagnostics=dict(glyph_anchor_age_sec=.2, glyph_anchor_reason='known_multi_face_joint_fit'),
                target_clues=[], target_candidate_associations=[])


def frame(layout):
    cells = {i: row for i, row in enumerate(layout['cells'])}
    return dict(status='ready', layout_digest=reader._layout_digest(cells), image_digest='actual_image',
                geometry_quality=dict(anchors=8, face_anchors=[3, 3, 2], rmse_px=2., consensus=.95),
                Q_candidates=[dict(op_to_logical=list(range(54)))],
                grid_cells=[dict(operation_slot=1, logical_slots=[1],
                                 quad=[[400, 200], [470, 200], [470, 270], [400, 270]])])


@pytest.fixture
def setup_reader(monkeypatch):
    monkeypatch.setattr(reader, 'classify_icon', lambda _image: dict(icon_id='blue_wall', confidence=.85))
    layout = atlas()
    return np.zeros((720, 1280, 3), np.uint8), layout, frame(layout), {}


def test_three_fresh_registered_captures_fill_only_requested_node(setup_reader):
    image, layout, reference, votes = setup_reader
    before = deepcopy(layout)
    for source, now in ((1, 10.), (2, 10.2), (3, 10.5)):
        result = reader.read_required_cells(image, layout, reference, [1], votes,
                                            source_id=source, source_time=now)
    assert result['ready']
    assert result['cells'][0]['icon_id'] == 'blue_wall'
    assert result['cells'][0]['node_read_evidence']['source_ids'] == [1, 2, 3]
    assert layout == before and result['cells'][0]['occupant'] == 'none'
    assert len(result['cells']) == 1


def test_same_capture_cannot_be_counted_three_times(setup_reader):
    image, layout, reference, votes = setup_reader
    for now in (10., 10.3, 10.6):
        result = reader.read_required_cells(image, layout, reference, [1], votes,
                                            source_id=1, source_time=now)
    assert not result['ready'] and votes['1']['sources'] == [1]


def test_read_promotes_same_frame_reference_without_changing_geometry(setup_reader):
    image, layout, reference, votes = setup_reader
    reference['map_revision'] = 4
    for source, now in ((1, 10.), (2, 10.2), (3, 10.5)):
        result = reader.read_required_cells(image, layout, reference, [1], votes,
                                            source_id=source, source_time=now)
    promoted = reader.promote_read_reference(reference, layout, result['cells'], map_revision=5)
    changed = deepcopy(layout)
    changed['cells'][1] = result['cells'][0]
    assert promoted['layout_digest'] == reader._layout_digest(dict(enumerate(changed['cells'])))
    assert promoted['Q_candidates'] == reference['Q_candidates']
    assert promoted['grid_cells'] == reference['grid_cells']
    assert promoted['map_revision'] == 5 and reference['map_revision'] == 4


@pytest.mark.parametrize('change', ['target', 'frame', 'sources', 'coordinate', 'glyph'])
def test_reference_promotion_rejects_unproven_update(setup_reader, change):
    image, layout, reference, votes = setup_reader
    for source, now in ((1, 10.), (2, 10.2), (3, 10.5)):
        result = reader.read_required_cells(image, layout, reference, [1], votes,
                                            source_id=source, source_time=now)
    row = result['cells'][0]
    if change == 'target': row['occupant'] = 'player'
    if change == 'frame': row['node_read_evidence']['image_digest'] = 'other'
    if change == 'sources': row['node_read_evidence']['source_ids'] = [1, 1, 1]
    if change == 'coordinate': row['col'] = 2
    if change == 'glyph': row['icon_id'] = None
    with pytest.raises(ValueError):
        reader.promote_read_reference(reference, layout, [row], map_revision=5)


@pytest.mark.parametrize('change', ['stale_digest', 'ambiguous_mapping', 'weak_anchor', 'weak_glyph', 'sprite_overlap'])
def test_registration_or_sprite_uncertainty_blocks_node_confirmation(setup_reader, monkeypatch, change):
    image, layout, reference, votes = setup_reader
    targets = []
    if change == 'stale_digest': reference['layout_digest'] = 'other_atlas'
    if change == 'ambiguous_mapping': reference['grid_cells'][0]['logical_slots'] = [1, 2]
    if change == 'weak_anchor': reference['geometry_quality']['face_anchors'] = [1, 4, 3]
    if change == 'weak_glyph': monkeypatch.setattr(reader, 'classify_icon', lambda _: dict(icon_id='blue_wall', confidence=.69))
    if change == 'sprite_overlap': targets = [dict(box=[425, 210, 15, 20], confidence=.05)]
    for source, now in ((1, 10.), (2, 10.2), (3, 10.5)):
        result = reader.read_required_cells(image, layout, reference, [1], votes,
                                            source_id=source, source_time=now, targets=targets)
    assert not result['ready'] and result['cells'] == []


def test_targets_goal_does_not_advertise_a_complete_glyph_atlas():
    layout = atlas()
    token = scan._SCAN_CONTROL.set(dict(recognition_goal='targets', expected_inspirations=2))
    try:
        assert scan._completion(layout) == (True, 'targets_ready')
        assert not layout['layout_complete']
    finally:
        scan._SCAN_CONTROL.reset(token)
    assert scan._completion(layout) == (False, 'layout_complete')
