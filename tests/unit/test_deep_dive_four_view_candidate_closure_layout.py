"""Layout contract checks with explicit simulated capture/geometry fixtures."""
from plans.resonance_pc.src.actions import _deep_dive_four_view_layout as layout_module


def source(frame):
    return dict(generation_source='atomic_wgc', capture_backend='wgc', session_id=7,
                generation=frame, frame_time=float(frame), frame_id=frame, map_revision=0)


def partial_views():
    views = []
    for frame, group, kind in ((1, 100, 'standard'), (2, 100, 'standard'),
                                (3, 100, 'standard'), (4, 101, 'supplement'), (5, 102, 'supplement')):
        cells = [dict(face=face, row=row, col=col, quad=[[10, 10], [60, 10], [60, 60], [10, 60]])
                 for face in ('F', 'R') for row in range(3) for col in range(3)]
        nodes = [dict(c, source=source(frame), view_group=group, scan_epoch='fixture',
                      stable=True, kind=kind, reading=dict(icon_id='blue_scales', confidence=.8, conflict=False))
                 for c in cells]
        views.append(dict(view=1, group=group, kind=kind, source=source(frame), cells=cells,
                          nodes=nodes, target_detection_complete=True))
    groups = [dict(group=100+i, view_index=1, kind='standard' if i == 0 else 'supplement',
                   source=source(1 if i == 0 else i+3), scan_epoch='fixture',
                   actual_structure_supported=True, image_orientation_error_deg=float(i),
                   parent_group=None if i == 0 else 100, vertical_input_evidence=[] if i == 0 else
                   [dict(dx=0, dy=40, horizontal_recenter_while_down=False)]) for i in range(3)]
    return views, groups


def test_partial_real_group_shape_does_not_fabricate_unseen_faces(monkeypatch):
    monkeypatch.setattr(layout_module.time, 'monotonic', lambda: 5.1)
    views, groups = partial_views()
    result = layout_module.build_layout(views, [], groups, expected_inspirations=2,
                                       scan_epoch='fixture', last_source=source(5))
    assert len(result['cells']) == 54
    assert result['faces_observed'] == 2
    assert not result['targets_ready']
    assert result['reason'] == 'four_standard_views_incomplete'
    visible = [c for c in result['cells'] if c['face'] in ('F', 'R')]
    assert all(c['occupant'] == 'none' and c['occupant_evidence_counts']['none'] == 3 for c in visible)
    assert all(c['occupant'] == 'unknown' for c in result['cells'] if c['face'] not in ('F', 'R'))


def test_temporal_three_frames_do_not_fabricate_negative_groups(monkeypatch):
    monkeypatch.setattr(layout_module.time, 'monotonic', lambda: 3.1)
    views, groups = partial_views()
    result = layout_module.build_layout(views[:3], [], groups[:1], expected_inspirations=2,
                                       scan_epoch='fixture', last_source=source(3))
    visible = [c for c in result['cells'] if c['face'] in ('F', 'R')]
    assert all(c['icon_id'] == 'blue_scales' for c in visible)
    assert all(c['occupant_status'] == 'unknown' for c in visible)
    assert all(c['occupant_evidence_counts']['none'] == 1 for c in visible)


def test_missing_detector_completion_keeps_occupancy_unknown(monkeypatch):
    monkeypatch.setattr(layout_module.time, 'monotonic', lambda: 5.1)
    views, groups = partial_views()
    for view in views:
        view.pop('target_detection_complete')
    result = layout_module.build_layout(views, [], groups, expected_inspirations=2,
                                       scan_epoch='fixture', last_source=source(5))
    assert all(c['occupant_status'] == 'unknown' for c in result['cells'])
    assert not result['diagnostics']['actual_direct_negative_evidence']
