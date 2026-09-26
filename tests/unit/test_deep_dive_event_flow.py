from pathlib import Path
import copy

import cv2
import numpy as np
import pytest

from plans.resonance_pc.src.actions import _deep_dive_event_flow as flow
from plans.resonance_pc.src.actions import _deep_dive_event_vision as vision
from plans.resonance_pc.src.actions import consciousness_deep_dive_single_run_pc_actions as runtime
from plans.resonance_pc.src.actions._deep_dive_single_run_vision import observe

IMAGES=Path(__file__).resolve().parents[2]/'docs/developer-guide/images/resonance-pc-deep-dive-simple'


def frame(name):
    im=cv2.imread(str(IMAGES/name));assert im is not None
    if im.shape[:2]==(802,1332):im=im[56:776,25:1305]
    return cv2.cvtColor(im,cv2.COLOR_BGR2RGB)


@pytest.mark.parametrize('name,scene',[
    ('15-healing-stone-three-options.png','event_options'),
    ('16-healing-stone-option-selected.png','event_options'),
    ('20-healing-stone-event-ending.png','event_ending'),
    ('followup-template-review/event_selected_source.png','event_options'),
    ('followup-template-review/dice_unselected_source.png','event_card_selection'),
    ('followup-template-review/dice_selected_source.png','event_card_selection'),
    ('followup-template-review/dice_lost_source.png','event_result'),
    ('21-purple-workshop-options-disabled.png','event_options_unknown'),
    ('38-healing-two-options.png','event_options'),
    ('39-healing-disabled-option.png','event_options'),
])
def test_screens(name,scene):
    assert observe(frame(name))['scene']==scene


def test_mask_excludes_dynamic_background_and_fails_closed(monkeypatch):
    im=frame('20-healing-stone-event-ending.png')
    ref=vision.template('event_heading');mask=vision.template('event_heading_mask')
    roi=im[25:25+ref.shape[0],560:560+ref.shape[1]]
    roi[mask==0]=np.random.default_rng(2).integers(0,256,(np.count_nonzero(mask==0),3),dtype=np.uint8)
    assert vision.heading_present(cv2.cvtColor(im,cv2.COLOR_RGB2GRAY))
    monkeypatch.setattr(vision.cv2,'matchTemplate',lambda *a,**k:np.array([[float('nan')]],np.float32))
    assert vision.heading_present(cv2.cvtColor(im,cv2.COLOR_RGB2GRAY)) is None


def test_board_numbers_are_not_card_selection():
    result=observe(frame('followup-template-review/board_negative_source.png'))
    assert result['scene']=='board' and result['event_board']
    assert not result.get('cards')


def test_count_text_is_not_required():
    im=frame('followup-template-review/dice_selected_source.png')
    im[665:720,540:800]=0
    result=observe(im)
    assert result['scene']=='event_card_selection' and result['cards'][0]['selected']


def test_disabled_effect_is_excluded_and_numeric_recovery_is_grouped():
    a=observe(frame('38-healing-two-options.png'))['event_options']
    b=observe(frame('39-healing-disabled-option.png'))['event_options']
    assert a[1]['group']==b[1]['group']=='Recover+'
    assert b[0]['state']=='disabled' and b[1]['priority']==0


def test_four_slot_composition_uses_same_catalog_logic():
    # Synthetic geometry check, explicitly not a real-client four-option test.
    data=vision.catalog();event=next(e for e in data['events'] if len(e['options'])==4)
    byid={o['id']:o for o in data['options']}
    im=np.zeros((720,1280,3),np.uint8)
    for y,oid in zip([116,266,416,566],event['options']):
        ref=vision.template(byid[oid]['text_key']+'_normal',True)
        im[y:y+ref.shape[0],760:760+ref.shape[1]]=ref
    rows=vision.event_options(im)
    assert len(rows)==4 and rows[-1]['group']=='End'


def test_known_selected_target_only_confirms_without_toggling():
    s=init();page={'scene':'event_card_selection','cards':[{'point':[640,340],'selected':True}],
                  'click':[640,640]}
    assert stable(s,page,1)[0]=='event_card_confirm'


def test_invalid_frame_resets_stability(monkeypatch):
    clock=[10.];monkeypatch.setattr(runtime.time,'time',lambda:clock[0])
    s=init();s['phase']='event_dispatch'
    page={'scene':'event_options','valid':True,'event_options':[option()]}
    assert runtime._advance_state(s,page)==('wait',None)
    assert runtime._advance_state(s,{'scene':'capture_failed','valid':False})==('wait',None)
    assert runtime._advance_state(s,page)==('wait',None)
    assert runtime._advance_state(s,page)[0]=='event_option_select'


def init():
    state={'pending_move':{'event':'healing'}}
    flow.start(state,{'click':[1090,540]},0)
    return state


def stable(state,o,now):
    flow.step(state,o,now)
    return flow.step(state,o,now+.4)


def option(state='normal',blocked=False,priority=10,group='Recover+',x=820):
    return {'state':state,'blocked':blocked,'priority':priority,'group':group,'point':[x,280]}


def test_direct_end_requires_blue_then_submission_then_verified_board():
    s=init();o={'scene':'event_options','event_options':[option()]}
    assert stable(s,o,1)[0]=='event_option_select'
    assert flow.step(s,o,1.5)==('wait',None)
    o['event_options'][0]['state']='selected'
    assert stable(s,o,2)[0]=='event_option_submit'
    ending={'scene':'event_ending','click':[1124,633]}
    assert stable(s,ending,3)==('wait',None)
    assert flow.step(s,ending,4.5)[0]=='event_dismiss_ending'
    board={'scene':'board','event_board':True,'move_done':False}
    assert stable(s,board,6)[0]=='event_returned'


def test_intro_animation_is_not_skipped_and_transient_board_not_completion():
    s=init()
    assert stable(s,{'scene':'event_ending','click':[1124,633]},1)==('wait',None)
    assert stable(s,{'scene':'board','event_board':True},6)==('wait',None)


@pytest.mark.parametrize('count,index',[(1,0),(2,0),(3,1)])
def test_cards_choose_left_for_two_middle_for_three_and_verify_target(count,index):
    s=init();cards=[{'point':[300+i*300,340],'selected':False} for i in range(count)]
    page={'scene':'event_card_selection','cards':cards,'click':[640,640]}
    assert stable(s,page,1)==('event_card_select',cards[index]['point'])
    assert flow.step(s,page,1.6)==('wait',None)
    page['cards'][index]['selected']=True
    assert stable(s,page,2)[0]=='event_card_confirm'
    result={'scene':'event_result','click':[280,630]}
    assert stable(s,result,3)[0]=='event_dismiss_result'
    assert stable(s,{'scene':'board','event_board':True},6)[0]=='event_returned'


def test_wrong_selected_card_cannot_be_confirmed():
    s=init();page={'scene':'event_card_selection','cards':[
        {'point':[300,340],'selected':False},{'point':[900,340],'selected':True}], 'click':[640,640]}
    assert stable(s,page,1)[0]=='event_blocked'


def test_bounded_retries_and_unknown_timeout():
    s=init();page={'scene':'event_options','event_options':[option()]}
    assert stable(s,page,1)[0]=='event_option_select'
    assert stable(s,page,4)[0]=='event_retry_option_select'
    assert stable(s,page,7)[0]=='event_retry_option_select'
    assert stable(s,page,10)[0]=='event_blocked'
    s=init()
    assert stable(s,{'scene':'unknown'},31)[0]=='event_blocked'


def test_forbidden_and_disabled_options_are_never_chosen():
    s=init();page={'scene':'event_options','event_options':[
        option(blocked=True,priority=-1),option(state='disabled',priority=0),
        option(group='End',priority=20,x=900)]}
    assert stable(s,page,1)==('event_option_select',[900,280])


def test_runtime_healing_chain_commits_once_without_move_digits(monkeypatch):
    clock=[100.];monkeypatch.setattr(runtime.time,'time',lambda:clock[0])
    s={'phase':'move_followup','pending_move':{'event':None},'move_history':[],
       'events':[],'status':'running','turns_completed':0}
    entry=observe(frame('30-node-preview-heal.png'))
    assert runtime._advance_state(s,entry)[0]=='transition'
    assert runtime._advance_state(s,entry)[0]=='click_event_enter'
    def call(o):
        clock[0]+=2
        runtime._advance_state(s,o)
        clock[0]+=.4
        return runtime._advance_state(s,o)
    opts=observe(frame('15-healing-stone-three-options.png'))
    assert call(opts)[0]=='event_option_select'
    selected=observe(frame('followup-template-review/event_selected_source.png'))
    assert call(selected)[0]=='event_option_submit'
    assert call(observe(frame('followup-template-review/dice_unselected_source.png')))[0]=='event_card_select'
    assert call(observe(frame('followup-template-review/dice_selected_source.png')))[0]=='event_card_confirm'
    assert call(observe(frame('followup-template-review/dice_lost_source.png')))[0]=='event_dismiss_result'
    ending=observe(frame('followup-template-review/event_ending_source.png'))
    call(ending);clock[0]+=2
    assert runtime._advance_state(s,ending)[0]=='event_dismiss_ending'
    assert s['move_history']==[]
    assert call(observe(frame('followup-template-review/board_negative_source.png')))[0]=='event_completed'
    assert len(s['move_history'])==1 and s['phase']=='rotate_ready' and s['turns_completed']==0


def test_settlement_preempts_pending_event():
    s={'phase':'event_dispatch','status':'running'}
    assert runtime._advance_state(s,{'scene':'settlement'})[0]=='settlement'
    assert s['status']=='completed'
