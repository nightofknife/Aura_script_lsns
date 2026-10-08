"""Owner-scoped node, battle and reward flows; the caller owns all input."""
from __future__ import annotations

import math
from uuid import uuid4

from . import _deep_dive_event_flow as healing
from . import _deep_dive_vortex_flow as vortex
from ._deep_dive_node_rewards import changed

BOARD = {'board', 'choose_move', 'choose_rotate'}
RESULT = {'event_result', 'reward_obtained'}
SELECTION = {'event_card_selection', 'battle_reward_selection'}
REWARD = RESULT | SELECTION


def create_context(event_type, owner, now, *, source_action_id=None, resume='scan_turn',
                   observation=None):
    if event_type not in {'empty', 'shop', 'battle', 'elite_battle', 'boss_battle',
                          'healing', 'item', 'vortex', 'passive_reward'}:
        raise ValueError('Unsupported node event: ' + str(event_type))
    context = dict(context_id=uuid4().hex, event_type=event_type, owner=owner,
                   source_action_id=source_action_id, resume=resume, phase='entry_wait',
                   started=now, progress_at=now, trace=[], flow={}, pending=None,
                   reward=None, seen_content=False, victory=False, signature=None, stable=0)
    if event_type in {'healing', 'item', 'vortex'}:
        flow = context['flow']
        flow['context_event_type'] = event_type
        if not observation or not observation.get('click'):
            raise ValueError('Node flow requires a recognized entry control')
        (vortex if event_type == 'vortex' else healing).start(flow, observation, now)
        context['phase'] = 'node_dispatch'
    elif event_type == 'passive_reward':
        context['phase'] = 'reward_chain'
    return context


def _signature(o):
    return repr((o.get('scene'), [(r.get('point'), r.get('selected'))
                                for r in o.get('cards', [])]))


def _stable(context, o, now):
    signature = _signature(o)
    if signature == context.get('signature'):
        context['stable'] += 1
    else:
        context.update(signature=signature, stable=1, stable_since=now)
    return context['stable'] >= 2


def _step(status='waiting', *, point=None, action=None, reason=None, evidence=None):
    result = {'status': status}
    if point is not None:
        result.update(point=list(map(int, point)), action=action)
    if reason:
        result['reason'] = reason
    if evidence:
        result['evidence'] = evidence
    return result


def _issue(context, action, point, now, **extra):
    context['pending'] = dict(action=action, point=list(point), at=now, tries=1, **extra)
    context['progress_at'] = now
    context['trace'].append(dict(action=action, at=now))
    context['signature'] = None
    context['stable'] = 0
    return _step('input_requested', point=point, action=action)


def _retry(context, now):
    pending = context['pending']
    if now - pending['at'] < 2:
        return _step()
    if pending['tries'] >= 3:
        return _step('blocked', reason=pending['action'] + ': confirmation retry limit')
    pending.update(at=now, tries=pending['tries'] + 1)
    context['signature'] = None
    context['stable'] = 0
    return _step('input_requested', point=pending['point'], action='retry_' + pending['action'])


def _reward_step(context, o, now, resume_scenes):
    """Drain one chain, retaining the exact owner until its resume page is stable."""
    reward = context.get('reward')
    if reward is None:
        reward = dict(started=now, progress_at=now, pending=None, signature=None,
                      stable=0, seen=False, between=False, trace=[])
        context['reward'] = reward
    scene = o.get('scene')
    if now - reward['progress_at'] > 30:
        return _step('blocked', reason='reward_chain_no_progress')
    if scene not in REWARD:
        reward['between'] = True
        if not _stable(reward, o, now):
            return _step()
        board_delay = 5 if scene in BOARD else 1
        if (reward['seen'] and scene in resume_scenes
                and now - reward.get('stable_since', now) >= board_delay):
            context['reward'] = None
            context['progress_at'] = now
            return _step('completed', evidence='reward_chain_returned_to_owner')
        return _step()
    reward['seen'] = True
    if reward.pop('between', False):
        reward['pending'] = None
        reward['progress_at'] = now
    if not _stable(reward, o, now) or not o.get('click'):
        return _step()
    pending = reward.get('pending')
    if pending:
        kind = pending['action']
        cards = o.get('cards', [])
        target = next((r for r in cards if math.dist(r['point'], pending.get('target', r['point'])) < 20), None)
        accepted = ((kind == 'select_reward' and scene in SELECTION and target and target['selected'])
                    or (kind == 'confirm_reward' and scene in RESULT)
                    or (kind == 'dismiss_reward' and
                        (scene in SELECTION or changed(pending.get('visual'), o.get('result_visual')))))
        if accepted:
            reward['pending'] = None
            reward['progress_at'] = now
        else:
            return _retry(reward, now)
    if scene in SELECTION:
        cards = sorted(o.get('cards', []), key=lambda r: r['point'][0])
        if len(cards) not in (1, 2, 3):
            return _step('blocked', reason='unsupported_reward_card_count')
        target = cards[0 if len(cards) < 3 else 1]
        if any(r['selected'] and r is not target for r in cards):
            return _step('blocked', reason='non_target_reward_selected')
        kind = 'confirm_reward' if target['selected'] else 'select_reward'
        point = o['click'] if target['selected'] else target['point']
        return _issue(reward, kind, point, now, target=target['point'])
    return _issue(reward, 'dismiss_reward', o['click'], now, visual=o.get('result_visual'))


def advance_context(context, observation, now):
    """Return one guarded input or an owner completion; never mutate game quotas."""
    boss_boundary = (context.get('last_player_action') and context['owner'] == 'player_move_node'
                     and observation.get('scene') == 'event_entry'
                     and observation.get('event_type') == 'boss_battle')
    enemy_board = ((observation.get('scene') == 'enemy_turn' and observation.get('enemy_turn') is True)
                   or boss_boundary)
    o = (dict(observation, scene='board', event_board=True) if enemy_board else observation)
    scene = o.get('scene', 'unknown')
    kind = context['event_type']
    if not o.get('valid'):
        context['signature'] = None
        context['stable'] = 0
        if kind in {'healing', 'item', 'vortex'}:
            (vortex if kind == 'vortex' else healing).reset_observation(context['flow'])
        return _step()
    if scene == 'battle_defeat':
        return _step('blocked', reason='battle_defeat')
    total_limit = 900 if kind in {'battle', 'elite_battle', 'boss_battle'} else 180
    if now - context['started'] >= total_limit:
        return _step('blocked', reason='event_total_timeout')
    if kind == 'passive_reward':
        return _reward_step(context, o, now, BOARD | {'rest_area', 'event_options',
                                                     'event_ending', 'battle_formation', 'shop'})
    # Rewards inserted before a battle/shop or healing/vortex content retain
    # their parent. Returning to the board here does not finish that node.
    prefix = 'vortex' if kind == 'vortex' else 'event'
    awaiting_content = (context['phase'] == 'entry_wait' or
                        kind in {'healing', 'vortex'} and
                        context['flow'].get(prefix + '_pending', {}).get('kind') == 'entry')
    front_reward = awaiting_content and scene in REWARD and kind != 'item'
    if front_reward or context.get('reward') and awaiting_content:
        resume = ({'battle_formation'} if kind in {'battle', 'elite_battle', 'boss_battle'}
                  else {'shop'} if kind == 'shop'
                  else {'event_options', 'event_ending'} if kind in {'healing', 'vortex'}
                  else BOARD)
        result = _reward_step(context, o, now, resume)
        if result['status'] != 'completed':
            return result
        return _step()
    if kind == 'shop' and context['phase'] == 'shop_return' and (scene in REWARD or context.get('reward')):
        result = _reward_step(context, o, now, BOARD)
        if result['status'] == 'completed' and context['seen_content']:
            return _step('completed', evidence='shop_tail_rewards_returned')
        return result
    if kind in {'healing', 'item', 'vortex'}:
        flow = vortex if kind == 'vortex' else healing
        action, point = flow.step(context['flow'], o, now)
        if action in {'event_returned', 'vortex_returned'}:
            return _step('completed', evidence='node_flow_returned')
        if action in {'event_blocked', 'vortex_blocked'}:
            return _step('blocked', reason=context['flow'].get(prefix + '_error', action))
        if point is not None:
            return _step('input_requested', point=point, action=action)
        return _step()
    if not _stable(context, o, now):
        return _step()
    pending = context.get('pending')
    if pending:
        action = pending['action']
        accepted = ((action == 'battle_start' and scene != 'battle_formation')
                    or (action == 'battle_next' and scene != 'battle_victory')
                    or (action == 'shop_back' and scene in BOARD)
                    or (action == 'empty_enter_retry' and scene != 'event_entry'))
        if accepted:
            context['pending'] = None
            context['progress_at'] = now
        elif ((action == 'battle_start' and scene == 'battle_formation')
              or (action == 'battle_next' and scene == 'battle_victory')
              or (action == 'shop_back' and scene == 'shop')):
            return _retry(context, now)
        elif action == 'empty_enter_retry' and scene == 'event_entry':
            if now - pending['at'] < 2:
                return _step()
            if context.get('entry_clicks', 2) >= 3:
                return _step('blocked', reason='empty_entry_confirmation_limit')
            result = _retry(context, now)
            if result['status'] == 'input_requested':
                context['entry_clicks'] = context.get('entry_clicks', 2) + 1
            return result
    if kind == 'empty':
        if scene in BOARD and (o.get('player_turn') or enemy_board):
            if now - context['started'] >= 5 and context['stable'] >= 3:
                return _step('completed', evidence='empty_node_stable_board')
        elif scene == 'event_entry' and o.get('event_type') == 'empty':
            # The first entry click belongs to the parent move transaction.
            if now - context['progress_at'] >= 2:
                context['entry_clicks'] = 2
                return _issue(context, 'empty_enter_retry', o['click'], now)
        if now - context['started'] >= 30:
            return _step('blocked', reason='empty_node_return_timeout')
        return _step()
    if kind == 'shop':
        if scene == 'shop' and o.get('click'):
            context['seen_content'] = True
            context['phase'] = 'shop_return'
            return _issue(context, 'shop_back', o['click'], now)
        if scene in BOARD and context['seen_content'] and now - context['stable_since'] >= 1:
            return _step('completed', evidence='shop_returned')
        if now - context['progress_at'] >= 30:
            return _step('blocked', reason='shop_no_progress')
        return _step()
    if context['phase'] == 'entry_wait' and scene == 'battle_formation':
        context.update(phase='battle_wait', seen_content=True, progress_at=now)
        return _issue(context, 'battle_start', o['click'], now)
    if context['phase'] in {'entry_wait', 'battle_wait'} and scene == 'battle_victory':
        context.update(victory=True, phase='battle_rewards', progress_at=now)
        return _issue(context, 'battle_next', o['click'], now)
    if context['phase'] == 'battle_rewards':
        if scene in REWARD or context.get('reward'):
            return _reward_step(context, o, now, BOARD | {'rest_area'})
        if context['victory'] and scene in BOARD | {'rest_area'} and now - context['stable_since'] >= 1:
            return _step('completed', evidence='battle_victory_and_return')
    phase_limit = 600 if context['phase'] == 'battle_wait' else 45
    if now - context['progress_at'] >= phase_limit:
        return _step('blocked', reason='battle_no_progress:' + context['phase'])
    return _step()
