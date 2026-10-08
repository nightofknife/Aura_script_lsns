from copy import deepcopy
from plans.resonance_pc.src.actions._deep_dive_four_view_layout import validate_view_groups


def groups():
    return [dict(group=i*100,view_index=i,kind='standard',source=dict(generation_source='atomic_wgc',
        capture_backend='wgc',session_id=1,map_revision=0,generation=i,frame_id=i,frame_time=float(i)),
        scan_epoch='test_epoch',actual_structure_supported=True,image_orientation_error_deg=0.) for i in range(1,5)]


def test_four_actual_views_and_no_semantic_labels():
    assert validate_view_groups(groups())==(True,'actual_four_view_groups_verified')


def test_repeated_frame_does_not_become_independent_group():
    values=groups();values[1]['source']=deepcopy(values[0]['source'])
    assert not validate_view_groups(values)[0]


def test_same_pose_temporal_frame_cannot_be_a_supplement():
    values=groups();extra=deepcopy(values[0]);extra.update(group=101,kind='supplement',parent_group=100)
    extra['source'].update(generation=2,frame_time=1.5)
    values.insert(1,extra)
    assert validate_view_groups(values)[1]=='supplement_not_a_measured_distinct_view'


def test_measured_supplement_needs_real_input_record():
    values=groups();extra=deepcopy(values[0]);extra.update(group=101,kind='supplement',parent_group=100,image_orientation_error_deg=-1.2)
    extra['source'].update(generation=2,frame_time=1.5)
    for v in values[1:]:v['source']['generation']+=1
    values.insert(1,extra)
    assert validate_view_groups(values)[1]=='supplement_vertical_input_evidence_required'
    extra['vertical_input_evidence']=[dict(dx=0,dy=-20,horizontal_recenter_while_down=False)]
    assert validate_view_groups(values)[0]


def test_missing_face_source_revision_and_boolean_view_are_rejected():
    assert not validate_view_groups(groups()[:3])[0]
    values=groups();values[-1]['source']['map_revision']=1
    assert not validate_view_groups(values)[0]
    values=groups();values[0]['view_index']=True
    assert not validate_view_groups(values)[0]
