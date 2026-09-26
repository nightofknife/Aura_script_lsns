from pathlib import Path
import copy

import cv2
import numpy as np
import pytest

from plans.resonance_pc.src.actions import _deep_dive_vortex_flow as flow
from plans.resonance_pc.src.actions import _deep_dive_vortex_vision as vision
from plans.resonance_pc.src.actions import _deep_dive_event_vision as healing
from plans.resonance_pc.src.actions import consciousness_deep_dive_single_run_pc_actions as runtime
from plans.resonance_pc.src.actions._deep_dive_single_run_vision import observe
from tools.build_deep_dive_vortex_catalog import option_policy

IMAGES=Path(__file__).resolve().parents[2]/'docs/developer-guide/images/resonance-pc-deep-dive-simple'


def frame(name):
    im=cv2.imread(str(IMAGES/name));assert im is not None
    if im.shape[:2]==(802,1332):im=im[56:776,25:1305]
    return cv2.cvtColor(im,cv2.COLOR_BGR2RGB)


def test_workshop_observation_isolated_from_healing():
    im=frame('21-purple-workshop-options-disabled.png')
    base=observe(im)
    assert base['scene']=='event_options_unknown'

    result=observe(im,event_family='vortex')
    rows=result['event_options']
    assert rows[0]['title']=='按下按钮' and rows[0]['supported']
    assert rows[1]['blocked']
    assert rows[2]['state']=='disabled'
    assert healing.ROOT!=vision.ROOT
    assert len(healing.catalog()['events'])==9
    assert len(vision.catalog()['events'])==62
    assert base['scene']=='event_options_unknown'


def test_vortex_observation_does_not_call_healing_detector(monkeypatch):
    from plans.resonance_pc.src.actions import _deep_dive_single_run_vision as shared
    monkeypatch.setattr(shared,'detect_event_page',lambda *a:pytest.fail('healing vision called'))
    assert shared.observe(frame('21-purple-workshop-options-disabled.png'),event_family='vortex')['scene']=='event_options'


def init():
    s={'pending_move':{'event':'vortex'}}
    flow.start(s,{'click':[1090,540]},0)
    return s


def stable(s,o,t):
    flow.step(s,o,t)
    return flow.step(s,o,t+.4)


def row(supported=True,blocked=False,priority=10,state='normal',title='奖励'):
    return {'group':'Reward.Equip.Random','title':title,'state':state,'supported':supported,
            'blocked':blocked,'priority':priority,'point':[810,270],
            'unsupported_reasons':[] if supported else ['事件内战斗']}


def test_supported_workflow_blue_reward_ending_return():
    s=init();o={'scene':'event_options','event_options':[row()]}
    assert stable(s,o,1)[0]=='vortex_option_select'
    o['event_options'][0]['state']='selected'
    assert stable(s,o,2)[0]=='vortex_option_submit'
    assert stable(s,{'scene':'reward_obtained','click':[280,630]},3)[0]=='vortex_dismiss_result'
    ending={'scene':'event_ending','click':[1124,633]}
    stable(s,ending,4)
    assert flow.step(s,ending,5.5)[0]=='vortex_dismiss_ending'
    assert stable(s,{'scene':'board','event_board':True},7)[0]=='vortex_returned'
    assert 'event_trace' not in s and s['vortex_trace']


def test_unsupported_is_skipped_when_simple_supported_choice_exists():
    s=init();o={'scene':'event_options','event_options':[row(False),row(priority=20,title='离开')]}
    assert stable(s,o,1)[0]=='vortex_option_select'
    assert s['vortex_pending']['index']==1


def test_only_unsupported_choices_stop():
    s=init();o={'scene':'event_options','event_options':[row(False)]}
    assert stable(s,o,1)==('vortex_blocked',None)


def test_guardian_exit_and_non_portal_outro():
    options=observe(frame('41-guardian-options.png'),event_family='vortex')
    assert options['scene']=='event_options'
    s=init()
    assert stable(s,options,1)[0]=='vortex_option_select'
    assert s['vortex_pending']['index']==1
    options=copy.deepcopy(options)
    options['event_options'][1]['state']='selected'
    assert stable(s,options,2)[0]=='vortex_option_submit'
    ending=observe(frame('42-guardian-ending.png'),event_family='vortex')
    assert ending['scene']=='event_ending'
    stable(s,ending,3)
    assert flow.step(s,ending,4.5)[0]=='vortex_dismiss_ending'
    assert stable(s,{'scene':'board','event_board':True},6)[0]=='vortex_returned'


def test_all_no_effect_exits_are_preferred_even_in_recurring_events():
    exits=[o for o in vision.catalog()['options'] if not o['effects']]
    assert len(exits)==29
    assert all(o['supported'] and not o['blocked'] and o['priority']==-10 for o in exits)
    assert option_policy([],recurring=True)['supported']


@pytest.mark.parametrize('name,expected',[
    ('41-guardian-options.png',False),('42-guardian-ending.png',True),
    ('20-healing-stone-event-ending.png',True),('15-healing-stone-three-options.png',False),
    ('21-purple-workshop-options-disabled.png',False),
    ('followup-template-review/board_negative_source.png',False),
])
def test_shared_ending_emblem_position(name,expected):
    assert vision.ending_emblem_present(cv2.cvtColor(frame(name),cv2.COLOR_RGB2GRAY)) is expected


def test_npc_ending_does_not_depend_on_character_name_or_story():
    im=frame('42-guardian-ending.png')
    # Keep only heading and shared emblem; all character/name/narrative pixels gone.
    independent=np.zeros_like(im)
    independent[15:90,525:775]=im[15:90,525:775]
    independent[475:560,315:420]=im[475:560,315:420]
    assert vision.detect_event_page(independent)['scene']=='event_ending'


def test_shared_ending_nonfinite_fails_closed(monkeypatch):
    monkeypatch.setattr(vision.cv2,'matchTemplate',lambda *a,**k:np.array([[float('inf')]],np.float32))
    assert vision.ending_emblem_present(np.zeros((720,1280),np.uint8)) is None


def test_low_priority_exception_does_not_block_supported_choice():
    s=init();o={'scene':'event_options','event_options':[row(),row(False,priority=20)]}
    assert stable(s,o,1)[0]=='vortex_option_select'


def test_forbidden_option_is_excluded_not_treated_as_exception():
    s=init();o={'scene':'event_options','event_options':[row(False,blocked=True,priority=0),row()]}
    assert stable(s,o,1)[0]=='vortex_option_select'
    assert s['vortex_pending']['index']==1


def test_changed_option_set_is_not_clicked_as_old_selection():
    s=init();o={'scene':'event_options','event_options':[row()]}
    stable(s,o,1);o['event_options'][0]['state']='selected';stable(s,o,2)
    other={'scene':'event_options','event_options':[row(state='selected',title='新的后续选项')]}
    assert stable(s,other,3)[0]=='vortex_blocked'


@pytest.mark.parametrize('count,target',[(1,0),(2,0),(3,1)])
def test_vortex_card_choice(count,target):
    s=init();cards=[{'point':[300+300*i,340],'selected':False} for i in range(count)]
    o={'scene':'event_card_selection','cards':cards,'click':[640,640]}
    assert stable(s,o,1)==('vortex_card_select',cards[target]['point'])
    cards[target]['selected']=True
    assert stable(s,o,2)[0]=='vortex_card_confirm'


@pytest.mark.parametrize('effect',[
    {'orderType':'Level'}, {'orderType':'Event'}, {'orderType':'Discard'},
    {'orderType':'LoseBuff','loseType':'Random','param':1},
    {'orderType':'Intensify','param':3},
    {'orderType':'Reward','rewardList':[{'id':999,'numMax':50,'numMin':50}],
     'packages':[{'id':999}]},
])
def test_exception_policy(effect):
    assert not option_policy([effect])['supported']


def test_reward_policy_checks_package_count_and_selection_mode():
    p={'func':'Select','packageType':'Equip','selectNum':1,'refreshNum':3}
    effect={'orderType':'Reward','rewardList':[{'id':1,'numMax':1,'numMin':1}],'packages':[p]}
    assert option_policy([effect])['supported']
    p.update(func='Random',refreshNum=2,selectNum=2)
    assert not option_policy([effect])['supported']


@pytest.mark.parametrize('low,high',[(30,30),(50,100),(400,400)])
def test_direct_currency_amounts_share_flow(low,high):
    reward={'orderType':'Reward','rewardList':[{'id':11400607,'numMin':low,'numMax':high}],
            'packages':[{'id':11400607}]}
    policy=option_policy([reward])
    assert policy['supported'] and policy['group']=='Reward.Currency'
    assert not option_policy([{'orderType':'Recover','param':-.1},reward])['supported']
    assert not option_policy([{'orderType':'Event','eventId':1},reward])['supported']


def test_body_two_currency_screenshot_chain():
    s=init()
    o=observe(frame('53-body-two-options.png'),event_family='vortex')
    assert o['event_options'][0]['supported']
    assert o['event_options'][0]['group']=='Reward.Currency'
    assert not o['event_options'][1]['supported']
    assert stable(s,o,1)[0]=='vortex_option_select'
    assert s['vortex_pending']['index']==0
    selected=copy.deepcopy(o)
    selected['event_options'][0]['state']='selected'
    assert stable(s,selected,2)[0]=='vortex_option_submit'
    result=observe(frame('54-currency-obtained.png'),event_family='vortex')
    assert result['scene']=='event_result'
    assert stable(s,result,4)[0]=='vortex_dismiss_result'
    ending=observe(frame('55-body-two-ending.png'),event_family='vortex')
    assert ending['scene']=='event_ending'
    stable(s,ending,6)
    assert flow.step(s,ending,7.5)[0]=='vortex_dismiss_ending'
    assert stable(s,{'scene':'board','event_board':True},9)[0]=='vortex_returned'


def test_runtime_routes_vortex_separately(monkeypatch):
    monkeypatch.setattr(runtime.time,'time',lambda:100.)
    monkeypatch.setattr(runtime.event_flow,'start',lambda *a:pytest.fail('healing start called'))
    s={'phase':'move_followup','pending_move':{'event':None}}
    entry=observe(frame('25-node-preview-adventure.png'))
    assert runtime._advance_state(s,entry)[0]=='transition'
    assert runtime._advance_state(s,entry)[0]=='click_event_enter'
    assert s['phase']=='vortex_dispatch' and 'vortex_pending' in s
    assert runtime._advance_state(s,{'valid':False,'scene':'capture_failed'})==('wait',None)
    assert s['vortex_stable']==0
    assert runtime._advance_state(s,{'scene':'settlement'})[0]=='settlement'
