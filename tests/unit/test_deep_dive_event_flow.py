import numpy as np
import pytest

from plans.resonance_pc.src.actions import _deep_dive_event_flow as flow
from plans.resonance_pc.src.actions import _deep_dive_event_vision as vision
from plans.resonance_pc.src.actions import consciousness_deep_dive_single_run_pc_actions as runtime


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


def test_settlement_preempts_pending_event():
    s={'phase':'event_dispatch','status':'running'}
    assert runtime._advance_state(s,{'scene':'settlement'})[0]=='settlement'
    assert s['status']=='completed'
