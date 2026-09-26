"""Drain inspiration before node content and passive rewards after node content."""
RETURN_PHASES = {'battle_loot','battle_obtain','battle_notice_wait','shop_return',
                 'empty_wait','rotate_ready','rotate_options'}


def changed(previous, current):
    return (previous is not None and current is not None and len(previous)==len(current)
            and len(current)>0 and sum(abs(a-b) for a,b in zip(previous,current))/len(current)>8.)


def step(state, o, now):
    """Return None to resume the original phase; otherwise one guarded action."""
    scene=o['scene'];phase=state['phase']
    result=scene in {'event_result','reward_obtained'}
    selection=scene in {'event_card_selection','battle_reward_selection'}
    prefix='vortex' if phase=='vortex_dispatch' else 'event'
    entry=(phase in {'battle_prepare','shop_wait'} or
           phase in {'event_dispatch','vortex_dispatch'} and
           state.get('pending_move',{}).get('event') in {'healing','vortex'} and
           state.get(prefix+'_pending',{}).get('kind')=='entry')
    # Existing battle selection/ordinary obtained steps retain their normal path.
    intercept=(phase in RETURN_PHASES and (scene=='event_result' or
               phase in {'shop_return','empty_wait','rotate_ready','rotate_options','battle_notice_wait'}
               and (result or selection)))
    intercept = intercept or entry and (result or selection)
    active=state.get('node_reward')
    if not intercept and active is None:return None
    if active is None and phase=='battle_notice_wait' and now-state.get('phase_started',0)<2:
        return 'wait',None
    if active is None:
        active={'started':now,'progress_at':now,'attempts':0,'at':0,'pending':None,'signature':None,'stable':0,
                'entry':entry}
        state['node_reward']=active
    if now-active['progress_at']>=30:
        state['event_error']='节点额外奖励30秒未取得进展'
        return 'event_blocked',None
    if not result and not selection:
        active['stable']=0
        active['between_pages']=True
        resume = (scene in {'board','choose_rotate','choose_move','event_ending'}
                  if not active['entry'] else
                  scene=='battle_formation' if phase=='battle_prepare' else
                  scene=='shop' if phase=='shop_wait' else scene=='event_options')
        if resume:
            state.pop('node_reward',None)
            state['phase_started']=now
            state['battle_stable_frames']=state['shop_stable_frames']=state['empty_board_frames']=0
            state.pop('battle_observation_signature',None)
            if active['entry'] and phase in {'event_dispatch','vortex_dispatch'}:
                state[prefix+'_progress_at']=now
                state.pop(prefix+'_signature',None)
                state[prefix+'_stable']=0
            return 'wait',None
        return 'wait',None
    cards=sorted(o.get('cards',[]),key=lambda r:r['point'][0])
    if not o.get('click'):
        active['stable']=0
        return 'wait',None
    if active.pop('between_pages',False):
        active['pending']=None
        active['attempts']=0
        active['progress_at']=now
    if result and changed(active.get('observed_visual'),o.get('result_visual')):
        active['stable']=0
    active['observed_visual']=o.get('result_visual')
    signature=(scene,tuple((round(r['point'][0]/10),r['selected']) for r in cards))
    if signature==active['signature']:active['stable']+=1
    else:active['signature']=signature;active['stable']=1
    if active['stable']<2:return 'wait',None
    target=None
    if selection:
        if len(cards) not in (1,2,3):
            state['event_error']='额外奖励候选数量未支持'
            return 'event_blocked',None
        target=cards[0 if len(cards)<3 else 1]
        if any(c['selected'] and c is not target for c in cards):
            state['event_error']='额外奖励非目标卡片已选中'
            return 'event_blocked',None
        kind='confirm' if target['selected'] else 'select'
        point=o['click'] if target['selected'] else target['point']
    else:
        kind='dismiss';point=o['click']
    visual=o.get('result_visual')
    new_result=kind=='dismiss' and changed(active.get('visual'),visual)
    if kind==active['pending'] and not new_result:
        if now-active['at']<2:return 'wait',None
        if active['attempts']>=3:
            state['event_error']='额外奖励点击3次后未确认页面变化'
            return 'event_blocked',None
    elif now-active['at']<1 and active['pending'] is not None:
        return 'wait',None
    else:active['attempts']=0
    active.update(pending=kind,at=now,progress_at=now,visual=visual,attempts=active['attempts']+1,stable=0)
    state['phase_started']=now
    if phase in {'battle_loot','battle_obtain'}:
        state['phase']='battle_notice_wait'
    state['node_reward_clicks']=state.get('node_reward_clicks',0)+1
    return 'node_reward_'+kind,list(point)
