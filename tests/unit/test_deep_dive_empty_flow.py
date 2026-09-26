from pathlib import Path

import cv2
import pytest

from plans.resonance_pc.src.actions import consciousness_deep_dive_single_run_pc_actions as runtime
from plans.resonance_pc.src.actions._deep_dive_single_run_vision import observe

IMAGES=Path(__file__).resolve().parents[2]/'docs/developer-guide/images/resonance-pc-deep-dive-simple'


def frame(name):
    image=cv2.imread(str(IMAGES/name))
    assert image is not None
    if image.shape[:2]==(802,1332):image=image[56:776,25:1305]
    return cv2.cvtColor(image,cv2.COLOR_BGR2RGB)


def setup(monkeypatch):
    clock=[100.]
    monkeypatch.setattr(runtime.time,'time',lambda:clock[0])
    state={'status':'running','phase':'move_followup','pending_move':{'event':None},
           'move_history':[],'events':[],'turns_completed':0}
    entry=observe(frame('40-empty-node-preview.png'))
    assert entry['scene']=='event_entry' and entry['event_type']=='empty'
    assert runtime._advance_state(state,entry)==('transition',None)
    action,point=runtime._advance_state(state,entry)
    assert action=='click_empty_enter' and point==entry['click']
    assert state['move_history']==[] and state['phase']=='empty_wait'
    return state,clock,entry


def test_empty_enters_then_waits_for_stable_board_and_commits_once(monkeypatch):
    state,clock,entry=setup(monkeypatch)
    board=observe(frame('followup-template-review/board_negative_source.png'))
    assert board['event_board'] and not board['move_done']
    for at in [100.4,100.8,101.2,102.,104.9]:
        clock[0]=at
        assert runtime._advance_state(state,board)==('wait',None)
        assert state['move_history']==[]
    clock[0]=105.1
    assert runtime._advance_state(state,board)==('event_completed',None)
    assert len(state['move_history'])==1 and state['phase']=='rotate_ready'
    assert state['turns_completed']==0
    assert state['events']==[{'type':'empty','status':'completed','entry_clicks':1}]
    assert runtime._advance_state(state,board)[0]=='click_rotate_button'
    assert len(state['move_history'])==1


def test_board_motion_does_not_block_but_invalid_frames_reset_ui_checks(monkeypatch):
    state,clock,_=setup(monkeypatch)
    board=observe(frame('followup-template-review/board_negative_source.png'))
    assert 'board_visual' not in board
    for at in [106.,106.4]:
        clock[0]=at;assert runtime._advance_state(state,board)==('wait',None)
    assert runtime._advance_state(state,{'valid':False,'scene':'capture_failed'})==('wait',None)
    for i,at in enumerate([107.,107.4]):
        clock[0]=at
        assert runtime._advance_state(state,{**board,'board_visual':[i*200]*768})==('wait',None)
    clock[0]=107.8
    assert runtime._advance_state(state,board)[0]=='event_completed'


def test_retry_only_same_empty_card_and_stop_after_three_clicks(monkeypatch):
    state,clock,entry=setup(monkeypatch)
    for at in [102.1,104.2]:
        clock[0]=at
        assert runtime._advance_state(state,entry)==('wait',None)
        assert runtime._advance_state(state,entry)[0]=='retry_empty_enter'
    clock[0]=106.3
    assert runtime._advance_state(state,entry)==('wait',None)
    assert runtime._advance_state(state,entry)[0]=='event_blocked'
    assert state['empty_clicks']==3 and not state['move_history']


@pytest.mark.parametrize('scene',['unknown','ambiguous_event_entry','event_result','event_ending'])
def test_other_pages_never_finish_or_supply_clicks(monkeypatch,scene):
    state,clock,_=setup(monkeypatch);clock[0]=110
    o={'valid':True,'scene':scene,'player_turn':True,'event_board':True,'board_visual':[50]*768}
    for _ in range(4):assert runtime._advance_state(state,o)==('wait',None)
    assert not state['move_history']


def test_settlement_preempts_empty_animation(monkeypatch):
    state,_,_=setup(monkeypatch)
    assert runtime._advance_state(state,{'scene':'settlement','outcome':'failure'})[0]=='settlement'
    assert state['status']=='completed' and not state['move_history']
