"""One bounded event transition per poll. The task's existing loop owns execution."""
import math
from ._deep_dive_node_rewards import changed


def _signature(o):
    return (o['scene'],tuple((r['group'],r['state']) for r in o.get('event_options',[])),
            tuple((round(r['point'][0]/10),r['selected']) for r in o.get('cards',[])))


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


def _issue(state,kind,point,now,**extra):
    state['event_pending']={'kind':kind,'point':list(point),'tries':1,'at':now,**extra}
    state['event_progress_at']=now
    state['event_trace'].append({'action':kind,'at':now,**extra})
    reset_observation(state)
    return 'event_'+kind,list(point)


def _fail(state,reason):
    state['event_error']=reason
    return 'event_blocked',None


def step(state,o,now):
    sig=_signature(o)
    if sig==state.get('event_signature'):state['event_stable']=state.get('event_stable',0)+1
    else:state['event_signature']=sig;state['event_stable']=1;state['event_stable_since']=now
    if state['event_stable']<2:return 'wait',None
    scene=o['scene'];pending=state.get('event_pending')
    option_page=scene=='event_options'
    selection=scene in ('event_card_selection','battle_reward_selection')
    result=scene in ('event_result','reward_obtained')
    board=scene in ('board','choose_move','choose_rotate') and o.get('event_board')
    known=option_page or selection or result or scene=='event_ending'
    if known:state['event_seen']=True
    if pending:
        kind=pending['kind'];accepted=False;retry=False
        if kind=='entry':
            # A centered portal is also shown during event entrance. Never skip
            # it as an outro until an actual choice/reward page has been observed.
            accepted=option_page or selection or result
            retry=scene=='event_entry' and o.get('event_type')==state['pending_move']['event']
        elif kind in ('option_select','option_submit'):
            rows=o.get('event_options',[]);i=pending['index']
            target=rows[i] if i<len(rows) else None
            same=target is not None and target['group']==pending['group']
            if kind=='option_select':
                accepted=option_page and same and target['state']=='selected'
                retry=option_page and same and target['state']=='normal'
            else:
                accepted=selection or result or scene=='event_ending' or board
                retry=option_page and same and target['state']=='selected'
                if accepted:state['event_submitted']=True
        elif kind in ('card_select','card_confirm'):
            target=next((r for r in o.get('cards',[]) if math.dist(r['point'],pending['target'])<20),None)
            if kind=='card_select':
                accepted=selection and target is not None and target['selected']
                retry=selection and target is not None and not target['selected']
            else:
                accepted=result or scene=='event_ending' or board
                retry=selection and target is not None and target['selected']
        elif kind=='dismiss_result':
            if scene=='unknown':pending['saw_transition']=True
            accepted=(selection or scene=='event_ending' or board or
                      result and now-pending['at']>=1. and (pending.get('saw_transition') or changed(pending.get('visual'),o.get('result_visual'))))
            retry=result
        elif kind=='dismiss_ending':
            accepted=board or selection or result or option_page
            retry=scene=='event_ending'
        if accepted:
            state.pop('event_pending',None);state['event_progress_at']=now
            state['event_trace'].append({'confirmed':kind,'scene':scene,'at':now})
        else:
            if retry and now-pending['at']>=2.0:
                if pending['tries']>=3:return _fail(state,kind+' 点击3次后未确认状态变化')
                pending['tries']+=1;pending['at']=now
                reset_observation(state)
                return 'event_retry_'+kind,list(pending['point'])
            if now-state['event_progress_at']>=30:return _fail(state,kind+' 后未进入可识别页面')
            return 'wait',None
    if board and state['event_seen'] and now-state['event_started']>=5:
        return 'event_returned',None
    if option_page:
        rows=o['event_options']
        allowed=[(r['priority'],i,r) for i,r in enumerate(rows) if not r['blocked'] and r['state']!='disabled']
        if not allowed:return _fail(state,'没有允许且可选的事件选项')
        _,i,target=min(allowed,key=lambda r:(r[0],r[1]))
        if target['state']=='selected':
            return _issue(state,'option_submit',target['point'],now,index=i,group=target['group'])
        return _issue(state,'option_select',target['point'],now,index=i,group=target['group'])
    if selection:
        cards=sorted(o['cards'],key=lambda r:r['point'][0])
        if len(cards) not in (1,2,3):return _fail(state,'不支持的候选卡数量')
        target=cards[0 if len(cards)<3 else 1]
        if any(r['selected'] and r is not target for r in cards):
            return _fail(state,'非目标卡片已选中，停止以避免提交错误道具')
        if target['selected']:
            return _issue(state,'card_confirm',o['click'],now,target=target['point'])
        return _issue(state,'card_select',target['point'],now,target=target['point'])
    if result:return _issue(state,'dismiss_result',o['click'],now,visual=o.get('result_visual'))
    if scene=='event_ending' and now-state['event_stable_since']>=1.:
        return _issue(state,'dismiss_ending',o['click'],now)
    if now-state['event_progress_at']>=30:return _fail(state,'未知事件页面或过渡超时')
    return 'wait',None
