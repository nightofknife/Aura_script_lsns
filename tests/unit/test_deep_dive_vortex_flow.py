import json

import numpy as np
import pytest

from plans.resonance_pc.src.actions import _deep_dive_vortex_flow as flow
from plans.resonance_pc.src.actions import _deep_dive_vortex_vision as vision
from plans.resonance_pc.src.actions._deep_dive_native_choices import observe_choice
from tools.build_deep_dive_vortex_catalog import option_policy


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


def native_options(event_id, *, selected=None, disabled=(), revision=1):
    event = next(item for item in vision.catalog()['events'] if item['id'] == event_id)
    rows = [dict(index=index, id=option, selected=option == selected, disabled=option in disabled)
            for index, option in enumerate(event['options'], 1)]
    return observe_choice({}, dict(status='ready', revision=revision, kind='options',
                                   subject_id=event_id, rows=rows), 'vortex')


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


def test_option_selection_survives_saved_session_round_trips():
    state = init()
    observation = {'scene': 'event_options', 'event_options': [row()]}
    assert flow.step(state, observation, 1)[0] == 'wait'
    state = json.loads(json.dumps(state))
    assert flow.step(state, observation, 1.4)[0] == 'vortex_option_select'
    observation['event_options'][0]['state'] = 'selected'
    state = json.loads(json.dumps(state))
    assert flow.step(state, observation, 2)[0] == 'wait'
    state = json.loads(json.dumps(state))
    assert flow.step(state, observation, 2.4)[0] == 'vortex_option_submit'


def test_unsupported_is_skipped_when_simple_supported_choice_exists():
    s=init();o={'scene':'event_options','event_options':[row(False),row(priority=20,title='离开')]}
    assert stable(s,o,1)[0]=='vortex_option_select'
    assert s['vortex_pending']['index']==1


def test_only_unsupported_choices_stop():
    s=init();o={'scene':'event_options','event_options':[row(False)]}
    assert stable(s,o,1)==('vortex_blocked',None)


@pytest.mark.parametrize('event_id,option_id,level_id', [
    (89800039, 86100250, 20101954),
    (89800042, 86100259, 20101959),
    (89800040, 86100252, 20101955),
    (89800085, 86100297, 20101977),
    (89800078, 86100178, 20101997),
])
def test_native_battle_option_waits_for_its_sub_battle(event_id, option_id, level_id):
    event = next(item for item in vision.catalog()['events'] if item['id'] == event_id)
    disabled = set(event['options']) - {option_id}
    index = event['options'].index(option_id) + 1
    identity = dict(revision=1, kind='options', subject_id=event_id, index=index, id=option_id)
    state = init()
    observation = native_options(event_id, disabled=disabled)
    assert stable(state,observation,1)==('vortex_option_select',dict(identity,command='select'))
    observation = native_options(event_id, selected=option_id, disabled=disabled)
    assert stable(state,observation,2)==('vortex_option_submit',dict(identity,command='confirm'))
    assert stable(state,{'scene':'board','event_board':True},6)==('wait',None)
    assert state['vortex_pending']['battle_level'] == level_id


@pytest.mark.parametrize('event_id,expected', [
    (89800040, 86100253),  # 防御系统故障，躲避优于战斗
    (89800027, 86100210),  # 地板塌陷，避开会增加行动次数的选项
    (89800011, 86100120),  # 求真者，免费混合奖励
    (89800085, 86100298),  # 原型机，非战斗分支
    (89800086, 86100299),  # 原型机后续，选消耗最低的 HP 分支
    (89800079, 86100176),  # 扭蛋机首段，免费尘埃
    (89800076, 86100339),  # 骑士的承诺，免费尘埃
    (89800042, 86100258),  # 狭路相逢，上交尘埃优于战斗
])
def test_native_catalog_uses_preferred_branch(event_id, expected):
    action, target = stable(init(), native_options(event_id), 1)
    assert action == 'vortex_option_select'
    assert target['id'] == expected


def test_free_reward_precedes_free_exit_and_paid_noncombat():
    free = dict(row(title='免费奖励'), effects=[{'orderType':'Reward'}])
    leave = dict(row(title='免费离开', priority=-10), effects=[])
    paid = dict(row(title='付代价拿奖励'), effects=[{'orderType':'Recover','param':-.1}, {'orderType':'Reward'}])
    state = init()
    page = dict(scene='event_options', event_options=[leave, paid, free])
    assert stable(state, page, 1)[0] == 'vortex_option_select'
    assert state['vortex_pending']['index'] == 2

    free['state'] = 'disabled'
    state = init()
    assert stable(state, page, 1)[0] == 'vortex_option_select'
    assert state['vortex_pending']['index'] == 0


def test_prototype_keeps_expected_followup_through_queued_rewards():
    state = init()
    assert stable(state, native_options(89800085), 1)[1]['id'] == 86100298
    submit = native_options(89800085, selected=86100298)
    assert stable(state, submit, 2)[0] == 'vortex_option_submit'

    award = dict(scene='reward_obtained', click=[280, 630])
    assert stable(state, award, 3)[0] == 'vortex_dismiss_result'
    assert stable(state, submit, 4) == ('wait', None)
    assert stable(state, dict(scene='board', event_board=True), 5) == ('wait', None)
    followup = native_options(89800086, revision=2)
    assert stable(state, followup, 6)[1]['id'] == 86100299
    assert 'vortex_next_event_id' not in state


def test_prototype_rejects_an_unexpected_followup():
    state = init()
    stable(state, native_options(89800085), 1)
    stable(state, native_options(89800085, selected=86100298), 2)
    assert stable(state, native_options(89800079, revision=2), 3) == ('vortex_blocked', None)


def test_mixed_reward_dismisses_both_awards_before_returning_to_board():
    state = init()
    assert stable(state, native_options(89800011), 1)[1]['id'] == 86100120
    assert stable(state, native_options(89800011, selected=86100120), 2)[0] == 'vortex_option_submit'
    first = dict(scene='reward_obtained', click=[280, 630], result_visual=[0, 0, 0])
    second = dict(first, result_visual=[25, 25, 25])

    assert stable(state, first, 3)[0] == 'vortex_dismiss_result'
    assert stable(state, second, 5)[0] == 'vortex_dismiss_result'
    assert stable(state, dict(scene='board', event_board=True), 7)[0] == 'vortex_returned'
    assert sum(row.get('action') == 'option_submit' for row in state['vortex_trace']) == 1


def test_every_catalog_event_has_a_native_path_when_options_are_enabled():
    for event in vision.catalog()['events']:
        action, target = stable(init(), native_options(event['id']), 1)
        assert action == 'vortex_option_select', (event['id'], event['name'])
        assert target['id'] in event['options']


def test_all_no_effect_exits_remain_supported_even_in_recurring_events():
    exits=[o for o in vision.catalog()['options'] if not o['effects']]
    assert len(exits)==29
    assert all(o['supported'] and not o['blocked'] and o['priority']==-10 for o in exits)
    assert option_policy([],recurring=True)['supported']


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


