"""Adapt measured four-view pixels into the existing planner layout contract."""
from __future__ import annotations

from copy import deepcopy
import math
import time

from ._deep_dive_planner_rules import BASES, FACES, cell_to_slot, slot_to_cell
from ._deep_dive_four_view_reader import fuse_node_readings, source_key
from ._deep_dive_four_view_targets import fuse_target_associations
from ._deep_dive_four_view_candidate_closure import resolve_candidate_closure
from ._deep_dive_target_readiness import targets_readiness


def validate_view_groups(groups):
    """Each group is a measured camera view, not another timestamp at one pose."""
    if not isinstance(groups, list) or not groups:
        return False, 'four_view_groups_missing'
    identifiers=set();session_revision=set();epochs=set();standard=set();previous=None
    for group in groups:
        try:
            key=source_key(group['source'])
            number=group['group'];view=group['view_index']
            if type(number) is not int or number<0 or number in identifiers:
                return False,'duplicate_or_invalid_four_view_group'
            if type(view) is not int or view not in (1,2,3,4) or group.get('actual_structure_supported') is not True:
                return False,'actual_four_view_structure_required'
            if previous is not None and (key[2]<=previous[2] or key[3]<=previous[3]):
                return False,'repeated_or_out_of_order_four_view_group'
            previous=key
            if not isinstance(group.get('scan_epoch'),str) or not group['scan_epoch']:
                return False,'invalid_four_view_scan_epoch'
            error=group['image_orientation_error_deg']
            if isinstance(error,bool) or not isinstance(error,(int,float)) or not math.isfinite(error):
                return False,'actual_four_view_orientation_required'
            if group['kind']=='standard':
                standard.add(view)
            elif group['kind']=='supplement':
                parent=next((g for g in groups if g['group']==group['parent_group']),None)
                if (parent is None or parent['kind']!='standard' or parent['view_index']!=view
                    or abs(error-parent['image_orientation_error_deg'])<.5):
                    return False,'supplement_not_a_measured_distinct_view'
                inputs=group.get('vertical_input_evidence',[])
                if not inputs or any(i.get('dx')!=0 or not i.get('dy') or
                    i.get('horizontal_recenter_while_down') is not False for i in inputs):
                    return False,'supplement_vertical_input_evidence_required'
            else:return False,'unsupported_four_view_group_kind'
            identifiers.add(number);session_revision.add(key[:2]);epochs.add(group['scan_epoch'])
        except (KeyError,TypeError,ValueError):
            return False,'invalid_four_view_group_source'
    if len(session_revision)!=1 or len(epochs)!=1:return False,'mixed_four_view_scan_source'
    if standard!={1,2,3,4}:return False,'four_standard_views_incomplete'
    return True,'actual_four_view_groups_verified'


def build_layout(views, associations, groups, *, expected_inspirations, scan_epoch, last_source):
    """No expected node/target labels enter runtime layout construction."""
    valid,reason=validate_view_groups(groups)
    observations={slot:[] for slot in range(54)}
    for view in views:
        for record in view.get('nodes',[]):
            observations[cell_to_slot(record)].append(record)
    targets=fuse_target_associations(associations)
    closure=resolve_candidate_closure(views,associations,targets,scan_epoch=scan_epoch)
    target_slots={t['cell_index']:t for t in targets['targets'] if t['occupant_status']=='confirmed'}
    disputed=set(targets.get('conflicting_slots',[]))
    for t in targets['targets']:
        if t['occupant_status']!='confirmed':disputed.add(t['cell_index'])
    for row in closure['unresolved']:
        disputed.update(c['cell_index'] for c in row.get('candidates',[]))
    cells=[]
    for slot,samples in observations.items():
        coordinate=slot_to_cell(slot)
        standard_samples=[s for s in samples if s.get('kind')=='standard']
        node=fuse_node_readings(standard_samples[:3])
        negative=closure['negative_evidence'].get(slot,{})
        seen=set(negative.get('groups',[]))
        negative_sources={(source_key(e['source']),e['group']) for e in negative.get('evidence',[])}
        evidence=[dict(group=s['view_group'],frame_id=s['source']['frame_id'],source=deepcopy(s['source']),
                       scan_epoch=scan_epoch,occupant='none' if (source_key(s['source']),s['view_group']) in negative_sources else 'unknown',
                       icon_id=s['reading'].get('icon_id')) for s in samples]
        row=dict(face=coordinate.face,row=coordinate.row,col=coordinate.col,slot=slot,
                 icon_id=node['icon_id'],node_kind=node['node_kind'],node_status=node['node_status'],
                 occupant='none' if len(seen)>=3 else 'unknown',occupant_status='confirmed' if len(seen)>=3 else 'unknown',
                 confidence=node['confidence'],evidence=evidence,
                 occupant_evidence_counts={'none':len(seen)} if seen else {},
                 actual_quad=standard_samples[-1]['quad'] if standard_samples else None,
                 node_temporal_evidence=node)
        if slot in disputed:
            row.update(occupant='unknown',occupant_status='unknown',confidence=0.)
        if slot in target_slots:
            target=target_slots[slot]
            row.update(occupant=target['kind'],occupant_status='confirmed',
                       confidence=target['confidence'],evidence=target['evidence'],
                       occupant_evidence_counts={target['kind']:target['independent_view_count']},
                       node_status='not_required_target')
        cells.append(row)
    coord=lambda t:dict(t['cell'])
    declared={kind:[t for t in target_slots.values() if t['kind']==kind] for kind in ('player','singularity','inspiration')}
    source_key(last_source)
    age=max(0.,time.monotonic()-last_source['frame_time'])
    known=sum(c['occupant_status']=='confirmed' and c['node_status'] in ('known','not_required_target') for c in cells)
    layout=dict(schema='resonance_pc.deep_dive_layout.v1',coordinate_frame='scan_local',scan_epoch=scan_epoch,
        frame_convention_version='four_views_reset_player_up_v1',reset_player_face='U',map_revision=0,
        cells=cells,faces=[dict(face_id=f,normal=BASES[f][0],column_axis=BASES[f][1],row_axis=BASES[f][2]) for f in FACES],
        known_cells=known,faces_observed=len({c['face'] for c in cells if c['evidence']}),
        layout_complete=False,semantic_mapping_complete=False,recognition_goal='targets',scan_route='four_views',
        player_cell=coord(declared['player'][0]) if len(declared['player'])==1 else None,
        singularity_cell=coord(declared['singularity'][0]) if len(declared['singularity'])==1 else None,
        inspiration_cells=[coord(t) for t in declared['inspiration']],expected_inspirations=expected_inspirations,
        target_candidate_associations=closure['effective_associations'],
        raw_target_candidate_associations=deepcopy(associations),
        explained_target_candidates=closure['explained'],
        unassociated_target_candidates=closure['unresolved'],
        target_clues=[],glyph_anchor_age_sec=age,glyph_anchor_reason='actual_four_view_structural_registration',
        diagnostics=dict(geometry='actual_four_view_grid_structure',glyph_anchor_age_sec=age,
            glyph_anchor_reason='actual_four_view_structural_registration',view_groups=deepcopy(groups),
            actual_direct_negative_evidence=closure['negative_evidence']),
        evidence_mode='four_view_actual_image_geometry_v1',actual_view_groups_verified=valid,
        prediction_only=False,map_valid=valid)
    ready=targets_readiness(layout) if valid else dict(ready=False,reason=reason)
    layout.update(targets_ready=bool(ready['ready']),success=bool(ready['ready']),
                  status='targets_ready' if ready['ready'] else 'partial',reason=ready['reason'],
                  readiness=ready,layout_complete=False,
                  visible_node_patterns_complete=bool(known==54))
    return layout
