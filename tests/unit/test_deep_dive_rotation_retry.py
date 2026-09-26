from plans.resonance_pc.src.actions import consciousness_deep_dive_single_run_pc_actions as runtime


def test_confirm_retry_then_disappearance_then_next_round(monkeypatch):
    clock=[100.];monkeypatch.setattr(runtime.time,'time',lambda:clock[0])
    s={'phase':'rotate_preview','turns_completed':0,'round_budget':20,'rotations':[],
       'pending_rotation':[540,140]}
    preview={'valid':True,'scene':'rotate_preview','click':[788,542],
             'scores':{'rotate_confirm':1.}}
    assert runtime._advance_state(s,preview)[0]=='click_rotate_confirm'
    clock[0]+=.5
    assert runtime._advance_state(s,preview)==('wait',None)
    clock[0]+=.6
    assert runtime._advance_state(s,preview)==('retry_rotate_confirm',[788,542])
    clock[0]+=1.1
    assert runtime._advance_state(s,{'valid':False,'scene':'capture_failed'})==('wait',None)
    assert s['phase']=='rotate_commit'
    assert runtime._advance_state(s,{'valid':True,'scene':'unknown','scores':{'rotate_confirm':.1}})==('wait',None)
    assert s['phase']=='rotate_next_wait' and s['turns_completed']==0
    next_round={'valid':True,'scene':'board','player_turn':True,'move_pending':True,'rotate_pending':True}
    assert runtime._advance_state(s,next_round)[0]=='rotation_confirmed'
    assert s['turns_completed']==1 and len(s['rotations'])==1
    assert runtime._advance_state(s,next_round)[0]=='next_turn'
    assert len(s['rotations'])==1


def test_only_confirm_template_is_required_and_retries_bounded(monkeypatch):
    clock=[100.];monkeypatch.setattr(runtime.time,'time',lambda:clock[0])
    s={'phase':'rotate_preview'}
    runtime._advance_state(s,{'valid':True,'scene':'rotate_preview','click':[788,542]})
    confirm_only={'valid':True,'scene':'unknown','scores':{'rotate_confirm':.95},'rotate_confirm_point':[790,544]}
    for _ in range(2):
        clock[0]+=1.1
        assert runtime._advance_state(s,confirm_only)==('retry_rotate_confirm',[790,544])
    clock[0]+=1.1
    assert runtime._advance_state(s,confirm_only)[0]=='rotation_blocked'
    assert s['rotation_confirm_clicks']==3
