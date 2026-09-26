from pathlib import Path
import cv2
import pytest
from plans.resonance_pc.src.actions import _deep_dive_node_rewards as rewards
from plans.resonance_pc.src.actions import consciousness_deep_dive_single_run_pc_actions as runtime
from plans.resonance_pc.src.actions._deep_dive_single_run_vision import observe


def result(value=50):
    return {'scene':'event_result','valid':True,'click':[280,630],'result_visual':[value]*576}


def stable(s,o,t):
    rewards.step(s,o,t)
    return rewards.step(s,o,t+.4)


@pytest.mark.parametrize('phase',['battle_notice_wait','shop_return','empty_wait','rotate_ready','rotate_options'])
def test_passive_reward_drained_before_original_phase_resumes(phase):
    s={'phase':phase,'phase_started':0}
    assert stable(s,result(),10)==('node_reward_dismiss',[280,630])
    assert s['phase']==phase
    assert rewards.step(s,result(),10.5)==('wait',None)
    assert stable(s,result(130),12)[0]=='node_reward_dismiss'
    assert s['node_reward']['attempts']==1
    board={'scene':'board','valid':True}
    assert rewards.step(s,board,14)==('wait',None)
    assert rewards.step(s,board,14.4) is None
    assert s['phase']==phase


def test_same_popup_retries_bounded():
    s={'phase':'shop_return'}
    assert stable(s,result(),10)[0]=='node_reward_dismiss'
    assert stable(s,result(),13)[0]=='node_reward_dismiss'
    assert stable(s,result(),16)[0]=='node_reward_dismiss'
    assert stable(s,result(),19)[0]=='event_blocked'


def test_additional_selection_left_of_two_then_result():
    s={'phase':'shop_return'}
    cards=[{'point':[400,340],'selected':False},{'point':[850,340],'selected':False}]
    o={'scene':'event_card_selection','cards':cards,'click':[640,640]}
    assert stable(s,o,10)==('node_reward_select',[400,340])
    cards[0]['selected']=True
    assert stable(s,o,12)==('node_reward_confirm',[640,640])
    assert stable(s,result(),14)[0]=='node_reward_dismiss'


def test_live_extra_activation_screenshot_and_battle_return(monkeypatch):
    p=Path(__file__).resolve().parents[2]/'docs/developer-guide/images/resonance-pc-deep-dive-simple/43-extra-activation-result.png'
    o=observe(cv2.cvtColor(cv2.imread(str(p)),cv2.COLOR_BGR2RGB))
    assert o['scene']=='event_result'
    clock=[100.];monkeypatch.setattr(runtime.time,'time',lambda:clock[0])
    s={'phase':'battle_notice_wait','phase_started':90,'pending_move':{'event':'elite_battle'},
       'move_history':[],'events':[],'turns_completed':0}
    assert runtime._advance_state(s,o)==('wait',None)
    clock[0]+=.4
    assert runtime._advance_state(s,o)[0]=='node_reward_dismiss'
    assert not s['move_history']
    board={'scene':'board','valid':True,'player_turn':True,'move_done':True}
    clock[0]+=3
    assert runtime._advance_state(s,board)==('wait',None)
    assert runtime._advance_state(s,board)==('wait',None)
    clock[0]+=1.1
    assert runtime._advance_state(s,board)[0]=='battle_completed'
    assert len(s['move_history'])==1 and s['turns_completed']==0


def test_settlement_preempts_extra_rewards():
    s={'phase':'shop_return','status':'running','node_reward':{'pending':'dismiss'}}
    assert runtime._advance_state(s,{'scene':'settlement'})[0]=='settlement'


@pytest.mark.parametrize('phase,main_scene,event',[
    ('battle_prepare','battle_formation','elite_battle'),
    ('shop_wait','shop','shop'),
    ('event_dispatch','event_options','healing'),
    ('vortex_dispatch','event_options','vortex'),
])
def test_three_inspiration_pages_resume_node_content_not_board(phase,main_scene,event):
    prefix='vortex' if event=='vortex' else 'event'
    s={'phase':phase,'phase_started':0,'pending_move':{'event':event},
       prefix+'_pending':{'kind':'entry'},'move_history':[]}
    p=Path(__file__).resolve().parents[2]/'docs/developer-guide/images/resonance-pc-deep-dive-simple'
    for i,name in enumerate(['44-inspiration-buff','45-inspiration-intuition','46-inspiration-item']):
        im=cv2.cvtColor(cv2.imread(str(p/(name+'.png'))),cv2.COLOR_BGR2RGB)
        page=observe(im,event_family='vortex' if event=='vortex' else 'healing')
        assert page['scene'] in {'event_result','reward_obtained'}
        assert stable(s,page,10+3*i)[0]=='node_reward_dismiss'
        assert s['phase']==phase and not s['move_history']
    board={'scene':'board','valid':True,'event_board':True}
    for t in [20,21,22]:assert rewards.step(s,board,t)==('wait',None)
    assert 'node_reward' in s
    assert rewards.step(s,{'scene':main_scene},23)==('wait',None)
    assert 'node_reward' not in s and s['phase']==phase and not s['move_history']
    assert rewards.step(s,{'scene':main_scene},24) is None


def test_inspiration_then_formation_runs_battle_start(monkeypatch):
    clock=[100.];monkeypatch.setattr(runtime.time,'time',lambda:clock[0])
    s={'phase':'battle_prepare','phase_started':99,'pending_move':{'event':'elite_battle'},
       'move_history':[],'events':[],'turns_completed':0}
    assert runtime._advance_state(s,result())==('wait',None)
    clock[0]+=.4
    assert runtime._advance_state(s,result())[0]=='node_reward_dismiss'
    formation={'valid':True,'scene':'battle_formation','click':[1100,620]}
    clock[0]+=3
    assert runtime._advance_state(s,formation)==('wait',None)
    assert runtime._advance_state(s,formation)==('wait',None)
    assert runtime._advance_state(s,formation)[0]=='click_battle_start'
    assert s['phase']=='battle_wait' and not s['move_history']


def test_direct_item_node_still_uses_original_reward_flow():
    s={'phase':'event_dispatch','pending_move':{'event':'item'},'event_pending':{'kind':'entry'}}
    assert rewards.step(s,result(),10) is None


def test_arbitrary_reward_sequence_has_no_page_count_or_total_time_limit():
    s={'phase':'battle_prepare','pending_move':{'event':'elite_battle'}}
    for i in range(40):
        # 400 seconds in total, with steady progress; no three-page/90s cap.
        assert stable(s,result(30 if i%2 else 180),10+i*10)[0]=='node_reward_dismiss'
        assert s['phase']=='battle_prepare'
    assert s['node_reward_clicks']==40
    assert rewards.step(s,{'scene':'battle_formation'},411)==('wait',None)
    assert 'node_reward' not in s


def test_identical_rewards_separated_by_transition_reset_per_page_attempts():
    s={'phase':'shop_wait'}
    for i in range(8):
        assert stable(s,result(),10+i*4)[0]=='node_reward_dismiss'
        assert rewards.step(s,{'scene':'unknown'},11+i*4)==('wait',None)
    assert s['node_reward_clicks']==8
