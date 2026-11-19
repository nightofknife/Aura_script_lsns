"""One bounded event transition per poll. The task's existing loop owns execution."""
from ._deep_dive_choice_target import (
    can_retry, choice_signature, choice_target, matching_card, new_card_panel,
    option_set, ordered_cards, pending_target, result_progress, same_choice, target_fields,
)
from ._deep_dive_node_rewards import ITEM_REPLACEMENT, RESULT


def reset_observation(state):
    state.pop('event_signature',None)
    state['event_stable']=0


def start(state, observation, now):
    for key in list(state):
        if key.startswith('event_'):state.pop(key,None)
    state.update(event_started=now,event_progress_at=now,event_seen=False,
                 event_submitted=False,event_trace=[])
    state['event_pending']={'kind':'entry','point':list(observation['click']),
                            'tries':1,'at':now}


def _issue(state,kind,input_target,now,**extra):
    state['event_pending']={'kind':kind,'tries':1,'at':now,**target_fields(input_target),**extra}
    state['event_progress_at']=now
    state['event_trace'].append({'action':kind,'at':now,**extra})
    reset_observation(state)
    return 'event_'+kind,pending_target(state['event_pending'])


def _fail(state,reason):
    state['event_error']=reason
    return 'event_blocked',None


def step(state,o,now):
    if o['scene'] == ITEM_REPLACEMENT and not (o.get('native') or o.get('click')):
        reset_observation(state)
        if now - state['event_progress_at'] >= 30:
            return _fail(state, '道具已满，未能确认丢弃按钮')
        return 'wait', None
    sig=choice_signature(o)
    if sig==state.get('event_signature'):state['event_stable']=state.get('event_stable',0)+1
    else:state['event_signature']=sig;state['event_stable']=1;state['event_stable_since']=now
    if state['event_stable']<2:return 'wait',None
    scene=o['scene'];pending=state.get('event_pending')
    option_page=scene=='event_options'
    selection=scene in ('event_card_selection','battle_reward_selection')
    result=scene in RESULT
    board=scene in ('board','choose_move','choose_rotate') and o.get('event_board')
    known=option_page or selection or result or scene=='event_ending'
    if known:state['event_seen']=True
    if pending:
        kind=pending['kind'];accepted=False;retry=False
        if kind=='entry':
            # A centered portal is also shown during event entrance. Never skip
            # it as an outro until an actual choice/reward page has been observed.
            accepted=option_page or selection or result
            event_type=state.get('context_event_type') or state.get('pending_move',{}).get('event')
            retry=scene=='event_entry' and o.get('event_type')==event_type and not pending.get('native_submitted')
        elif kind in ('option_select','option_submit'):
            rows=o.get('event_options',[]);i=pending['index']
            if option_page and 'native' in pending and option_set(rows) != pending['option_set']:
                return _fail(state,'事件选项组合发生变化')
            target=rows[i] if i<len(rows) else None
            same=target is not None and target['group']==pending['group'] and same_choice(target,pending)
            if kind=='option_select':
                accepted=option_page and same and target['state']=='selected'
                retry=option_page and same and target['state']=='normal'
            else:
                accepted=selection or result or scene=='event_ending' or board
                retry=option_page and same and target['state']=='selected'
                if accepted:state['event_submitted']=True
        elif kind in ('card_select','card_confirm'):
            cards=o.get('cards',[])
            target=matching_card(cards,pending)
            if kind=='card_select':
                if selection and 'native' in pending and (target is None or any(
                        r['selected'] and r is not target for r in cards)):
                    return _fail(state,'候选卡片或其选中状态发生变化')
                accepted=selection and target is not None and target['selected']
                retry=selection and target is not None and not target['selected']
            else:
                accepted=result or scene=='event_ending' or board or selection and new_card_panel(cards,pending)
                retry=selection and target is not None and target['selected']
        elif kind in ('dismiss_result', 'discard_new_item'):
            if scene=='unknown':pending['saw_transition']=True
            accepted=(selection or scene=='event_ending' or board or
                      result and now-pending['at']>=1. and
                      (('native' not in pending and pending.get('saw_transition')) or
                       result_progress(pending,o)))
            retry=result
        elif kind=='dismiss_ending':
            accepted=board or selection or result or option_page
            retry=scene=='event_ending'
        if accepted:
            state.pop('event_pending',None);state['event_progress_at']=now
            state['event_trace'].append({'confirmed':kind,'scene':scene,'at':now})
        else:
            if retry and can_retry(pending) and now-pending['at']>=2.0:
                if pending['tries']>=3:return _fail(state,kind+' 点击3次后未确认状态变化')
                pending['tries']+=1;pending['at']=now
                reset_observation(state)
                return 'event_retry_'+kind,pending_target(pending)
            if now-state['event_progress_at']>=30:return _fail(state,kind+' 后未进入可识别页面')
            return 'wait',None
    if board and state['event_seen'] and now-state['event_started']>=5:
        return 'event_returned',None
    if option_page:
        rows=o['event_options']
        allowed=[(r['priority'],i,r) for i,r in enumerate(rows) if not r['blocked'] and r['state']!='disabled']
        if not allowed:return _fail(state,'没有允许且可选的事件选项')
        _,i,target=min(allowed,key=lambda r:(r[0],r[1]))
        selected=target['state']=='selected'
        extra={'option_set':option_set(rows)} if 'native' in target else {}
        return _issue(state,'option_submit' if selected else 'option_select',
                      choice_target(target,'confirm' if selected else 'select',target.get('point')),
                      now,index=i,group=target['group'],**extra)
    if selection:
        cards=ordered_cards(o['cards'])
        if len(cards) not in (1,2,3):return _fail(state,'不支持的候选卡数量')
        target=cards[0 if len(cards)<3 else 1]
        if any(r['selected'] and r is not target for r in cards):
            return _fail(state,'非目标卡片已选中，停止以避免提交错误道具')
        selected=target['selected']
        point=o.get('click') if selected else target.get('point')
        return _issue(state,'card_confirm' if selected else 'card_select',
                      choice_target(target,'confirm' if selected else 'select',point),now,
                      target=target.get('point'))
    if result:
        kind = 'discard_new_item' if scene == ITEM_REPLACEMENT else 'dismiss_result'
        target=choice_target(o,'discard',o.get('click')) if scene==ITEM_REPLACEMENT else o['click']
        return _issue(state,kind,target,now,visual=o.get('result_visual'),result_scene=scene)
    if scene=='event_ending' and now-state['event_stable_since']>=1.:
        return _issue(state,'dismiss_ending',o['click'],now)
    if now-state['event_progress_at']>=30:return _fail(state,'未知事件页面或过渡超时')
    return 'wait',None
