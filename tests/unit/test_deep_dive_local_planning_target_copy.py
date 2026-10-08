"""A real single positive can inform navigation without becoming an atlas label."""
from copy import deepcopy
import json
import math
from pathlib import Path

import numpy as np
import pytest

from plans.resonance_pc.src.actions._deep_dive_target_first_scan_policy import TargetFirstCellScanPolicy
from plans.resonance_pc.src.actions._deep_dive_scan_policy import _matrix, _unit, _pose_angle


def subject():
    saved=json.loads((Path(__file__).parent/'fixtures/deep_dive_local_boss_17.json').read_text(encoding='utf8'))
    source,current=saved['source'],saved['current'];obs=source['observation']
    rotation=np.array(obs['glyph_anchor_rotation']);stamp=source['frame_time']
    source_basis=np.array(source['geometry_body_basis'])@np.array(source['pose']['rotation']).T@rotation
    feedback=dict(session_id=source['session_id'],map_revision=0,semantic_map_revision=0,
        geometry_body_basis=current['geometry_body_basis'],refine_diagnostic=obs['refine_diagnostic'],
        glyph_anchor_at=stamp,glyph_anchor_frame_id=823,glyph_anchor_age_sec=obs['glyph_anchor_age_sec'],
        accepted_anchor_observation=dict(frame_id=823,frame_time=stamp,session_id=source['session_id'],
            map_revision=0,rotation=rotation.tolist(),body_basis=source_basis.tolist(),
            accepted_faces=obs['refine_diagnostic']['accepted_faces']))
    rows=[dict(occupant='unknown',node_status='unknown',confidence=0.,evidence=[]) for _ in range(54)]
    for index in obs['refine_diagnostic']['confirmed_cell_indices']:
        rows[index].update(occupant='none',node_status='known',confidence=.7)
    rows[31]=deepcopy(saved['target_cell'])
    policy=TargetFirstCellScanPolicy();policy._remember_target_source(feedback);policy._mixed_feedback=feedback
    origin=policy._evidence_pose(rows[31]['evidence'][0],0)
    assert origin is not None
    policy._local_task=dict(signature=('positive','singularity',31),kind='singularity',index=31,
        source_frame_id=823,origin=origin)
    policy.tvec=np.array(current['fused_pose']['tvec']).reshape(3)
    axes={i:np.array(a['rotation_vector'])/a['pixels'] for i,a in enumerate(saved['calibrations'])}
    return policy,rows,np.array(current['fused_pose']['rotation']),axes


def test_actual_unknown_boss_single_vote_is_only_changed_in_private_planning_copy():
    policy,rows,_,_=subject();before=deepcopy(rows)
    planning=policy._local_planning_rows(rows)
    assert planning is not rows and planning[31] is not rows[31]
    assert planning[31]['occupant']=='singularity'
    assert planning[31]['occupant_status']=='unknown'
    assert planning[31]['occupant_evidence_counts']=={'singularity':1}
    assert planning[31]['evidence'][0]['frame_id']==823
    assert policy._sprite_footprint(planning[31]) is not None
    assert rows==before and rows[31]['occupant']=='unknown'
    planning[31]['evidence'][0]['target_box'][0]=0
    assert rows==before  # The measured source itself also belongs to the copy.


@pytest.mark.parametrize('defect',['frame','time','map','rotation','kind','bbox','quad',
                                 'unregistered','context','basis','source_context',
                                 'task_source','task_origin','generic','index'])
def test_invalid_positive_or_task_proof_keeps_original_planning_rows(defect):
    policy,rows,_,_=subject();entry=rows[31]['evidence'][0];proof=entry['association_evidence']
    if defect=='frame':proof['source_frame_id']=822
    elif defect=='time':proof['source_frame_time']+=1
    elif defect=='map':proof['source_map_revision']=1
    elif defect=='rotation':proof['rotation']=np.eye(3).tolist()
    elif defect=='kind':entry['occupant']='inspiration'
    elif defect=='bbox':entry['target_box'][0]=float('inf')
    elif defect=='quad':entry['quad']=[]
    elif defect=='unregistered':policy._target_sources.clear()
    elif defect=='context':policy._mixed_feedback['session_id']+=1
    elif defect=='basis':policy._mixed_feedback['geometry_body_basis'][0][0]=2.
    elif defect=='source_context':policy._target_sources[823]['context']=(0,1)
    elif defect=='task_source':policy._local_task['source_frame_id']=824
    elif defect=='task_origin':policy._local_task['origin']=np.eye(3)
    elif defect=='generic':policy._local_task['signature']=('candidate','singularity',31)
    elif defect=='index':policy._local_task['index']=None
    assert policy._local_planning_rows(rows) is rows


def test_actual_eight_candidate_ranking_accounts_for_single_positive_sprite_without_changing_votes():
    policy,rows,observed,axes=subject();before=deepcopy(rows)
    candidates=np.array([_matrix(_unit(axes[axis])*math.radians(sign*angle))@observed
        for axis in (0,1) for sign in (-1,1) for angle in (12.,16.)])
    origin=policy._local_task['origin']
    novelty=np.array([_pose_angle(p,origin)>=math.radians(12.) for p in candidates])
    def scores(atlas):
        support=policy._planning_anchor_support(candidates,atlas)
        route=policy._route_anchor_support([[p] for p in candidates],atlas,allow_potential=True)
        gain=policy._readability(candidates,atlas)[:,31]
        score=(1.+gain)*np.sqrt(support*route)/(1.+np.radians([12.,16.]*4)/math.radians(20.))
        score[~novelty]=-1
        return support,score
    old_support,old=scores(rows);new_support,new=scores(policy._local_planning_rows(rows))
    assert int(np.argmax(old))==4  # Vertical -12° formerly relied on unmasked D potential.
    assert new_support[4]<old_support[4] and int(np.argmax(new))==6
    assert policy._set_local_route(observed,rows,axes,72.)
    assert np.allclose(policy._local_goal,candidates[6],atol=1e-12)
    assert policy._plan_votes[31]==1 and rows==before
    assert not policy.opportunity_navigation
