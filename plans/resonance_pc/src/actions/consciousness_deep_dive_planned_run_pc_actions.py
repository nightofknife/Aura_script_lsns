"""Observed 3x3 in-level play. No activity entry or settlement inputs exist here."""
from __future__ import annotations

import asyncio
from copy import deepcopy
from contextvars import ContextVar
from datetime import datetime
import json
import math
from numbers import Integral
from pathlib import Path
import threading
import time
from typing import Any
from uuid import uuid4

import cv2
import numpy as np

from packages.aura_core.api import action_info, requires_services
from packages.aura_core.observability.events import Event
from packages.aura_core.observability.logging.core_logger import current_cid, logger
from packages.aura_core.scheduler.cancellation import is_current_task_cancel_requested
from packages.aura_core.scheduler.utils import resolve_base_path
from packages.aura_core.utils.exceptions import StopTaskException

from ._deep_dive_planned_run_vision import observe, read_hud, enrich_observation, NODE_EVENT_TYPES
from .consciousness_deep_dive_scan_pc_actions import run_layout_scan, reset_layout_view
from ._deep_dive_movement_planner import plan_current_state
from ._deep_dive_planner_rules import cell_to_slot
from . import _deep_dive_directed_operation_flow as directed
from ._deep_dive_operation_frame import (
    build_operation_frame, build_wide_reference_frame, bind_move, bind_rotation, verify_rotation_preview,
)
from ._deep_dive_runtime_events import create_context, advance_context, REWARD

SCHEMA = 'resonance_pc.deep_dive_planned_run.v1'
PROGRESS = 'task.resonance_pc_deep_dive_planned_run_progress'
POLL = .35
HUD_KEYS = ('plane_index', 'rounds_remaining', 'moves_used', 'moves_total',
            'rotations_used', 'rotations_total', 'collected_count', 'inspiration_total')
PHASE_LIMITS = {'scan_turn': 90, 'accept_scan': 30, 'planning': 45,
                'reset_wide': 20, 'wide_reference': 45,
                'operation': 100, 'operation_outcome': 45, 'verify_move': 45,
                'enemy_wait': 180, 'plane_transition': 90, 'rest_area': 45,
                'next_plane_wait': 90, 'event_dispatch': 960}
_RUN_OCR = ContextVar('deep_dive_planned_run_ocr', default=None)


def _cancel_check():
    if is_current_task_cancel_requested():
        raise StopTaskException('识海深潜关卡内运行已取消', success=False)


def _jsonable(value):
    if isinstance(value, np.ndarray):
        return value.tolist()
    if isinstance(value, np.generic):
        return value.item()
    if isinstance(value, Path):
        return str(value)
    if isinstance(value, dict):
        return {str(k): _jsonable(v) for k, v in value.items()}
    if isinstance(value, (list, tuple)):
        return [_jsonable(v) for v in value]
    if isinstance(value, float) and not math.isfinite(value):
        return None
    return value


async def _owned(operation, cancellation=None, *, propagate_cancel=True):
    """Drain this task's worker before allowing input, persistence or task exit."""
    worker = asyncio.ensure_future(operation)
    cancelled = False
    while True:
        try:
            result = await asyncio.shield(worker)
            break
        except asyncio.CancelledError:
            cancelled = True
            if cancellation is not None:
                cancellation.set()
            if worker.done():
                result = worker.result()
                break
    if cancelled and propagate_cancel:
        raise asyncio.CancelledError()
    return result


async def _capture(app, family='healing'):
    _cancel_check()
    capture = await _owned(app.capture_async())
    if not getattr(capture, 'success', False) or getattr(capture, 'image', None) is None:
        return None, dict(valid=False, scene='capture_failed')
    return capture.image, observe(capture.image, event_family=family)


async def _click_owned(state, app, x, y):
    """Hold a recognized control across a Unity frame and always release it."""
    await _owned(app.move_to_async(x, y, duration=0.))
    _cancel_check()
    state['input_maybe_held'] = True
    try:
        await _owned(app.controller.mouse_down_async('left'))
        await asyncio.sleep(.08)
    finally:
        await _owned(app.controller.mouse_up_async('left'), propagate_cancel=False)
        state['input_maybe_held'] = False


def _phase(state, phase):
    if state.get('phase') != phase:
        state['phase'] = phase
        state['phase_started'] = time.monotonic()
        state.pop('scene_signature', None)
        state['scene_stable'] = 0


def _invalidate(state, reason):
    state['layout'] = None
    state['plan'] = None
    state['scan_candidate'] = None
    state['operation_frame'] = None
    state['wide_reference'] = None
    state.pop('wide_reference_stability', None)
    state['known_cells'] = 0
    state['map_revision'] += 1
    state['pose_epoch'] += 1
    state['snapshot_invalidated_by'] = reason
    state.pop('hud_signature', None)
    state['hud_stable'] = 0


def _directory(state):
    return Path(state['output_dir'])


def _write_json(path, value):
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_suffix(path.suffix + '.writing')
    temporary.write_text(json.dumps(_jsonable(value), ensure_ascii=False, indent=2), encoding='utf-8')
    temporary.replace(path)


def _frame(state, image, name):
    if image is None:
        return None
    path = _directory(state) / 'frames' / (datetime.now().strftime('%H%M%S_%f') + '_' + name + '.png')
    path.parent.mkdir(parents=True, exist_ok=True)
    if not cv2.imwrite(str(path), cv2.cvtColor(image, cv2.COLOR_RGB2BGR)):
        raise OSError('无法保存识海深潜截图')
    state['last_frame'] = str(path)
    return str(path)


def _log(state, kind, **fields):
    row = dict(at=time.time(), kind=kind, plane_index=state.get('plane_index'),
               turn=state.get('turns_completed'), scan_epoch=state.get('scan_epoch'), **fields)
    state['last_event'] = _jsonable(row)
    with (_directory(state) / 'events.jsonl').open('a', encoding='utf-8') as handle:
        handle.write(json.dumps(_jsonable(row), ensure_ascii=False) + '\n')


def _summary(state):
    keys = ('schema', 'session_key', 'cid', 'sequence', 'status', 'phase', 'reason', 'strategy',
            'plane_index', 'planes_completed', 'turns_completed', 'rounds_remaining',
            'scan_epoch', 'map_revision', 'known_cells', 'terminal_reached', 'terminal',
            'outcome', 'final_frame', 'last_frame', 'output_dir', 'json_path', 'report_path',
            'latest_scan', 'latest_plan', 'reward_ledger')
    result = {key: _jsonable(state.get(key)) for key in keys}
    result['success'] = state['status'] == 'completed' and state['terminal_reached']
    result['game_success'] = (True if state.get('outcome') == 'victory' else
                              False if state.get('outcome') == 'failure' else None)
    return result


async def _publish(state, event_bus):
    if event_bus is None:
        return
    payload = _summary(state)
    try:
        await event_bus.publish(Event(name=PROGRESS, payload=payload))
    except Exception as exc:
        logger.warning('Deep Dive planned progress delivery failed: %s', exc)


async def _save(state, state_store, event_bus=None):
    state['sequence'] += 1
    clean = _jsonable(state)
    await state_store.set(state['session_key'], clean)
    _write_json(Path(state['json_path']), clean)
    await _publish(state, event_bus)


def _stop(state, reason, image=None, *, status='blocked'):
    state.update(status=status, reason=str(reason), input_owner=None)
    if state.get('pending_action') and not state['pending_action'].get('confirmed'):
        state['pending_action']['interrupted'] = True
        state['pending_action']['outcome'] = 'unknown'
    _frame(state, image, status)
    _log(state, status, reason=reason)


def _stable_scene(state, observation):
    signature = (observation.get('scene'), observation.get('event_type'), observation.get('outcome'))
    if list(signature) == state.get('scene_signature'):
        state['scene_stable'] += 1
    else:
        state.update(scene_signature=list(signature), scene_stable=1, scene_since=time.monotonic())
    return state['scene_stable'] >= 2


def _terminal(state, image, observation):
    if observation.get('scene') != 'settlement':
        state['terminal_stable'] = 0
        state['terminal_signature'] = None
        return False
    signature = observation.get('outcome', 'unknown')
    if signature == state.get('terminal_signature'):
        state['terminal_stable'] += 1
    else:
        state.update(terminal_signature=signature, terminal_stable=1)
    # Freeze all other input even before the second confirmation.
    if state['terminal_stable'] >= 2:
        state.update(status='completed', phase='stopped', input_owner=None,
                     reason='settlement_observed', terminal_reached=True, terminal='settlement',
                     outcome=signature)
        boss_victory = any(context.get('victory') and context.get('owner') in {
            'player_move_encounter', 'enemy_move_encounter'} for context in state.get('event_stack', []))
        if state.get('awaiting_plane_end') or signature == 'victory' and boss_victory:
            _complete_plane(state)
        state['final_frame'] = _frame(state, image, 'settlement')
        if state.get('pending_action') and not state['pending_action'].get('confirmed'):
            state['pending_action'].update(interrupted=True, outcome='unknown')
        _log(state, 'terminal', outcome=signature)
    return True


def _hud_consensus(state, raw):
    values = {key: raw.get(key) for key in HUD_KEYS}
    if any(type(value) is not int for value in values.values()):
        state['hud_stable'] = 0
        state['last_hud_unknown'] = [key for key, value in values.items() if type(value) is not int]
        return None
    if not (1 <= values['plane_index'] <= 3 and 0 <= values['rounds_remaining'] <= 30):
        state['hud_rule_error'] = 'unsupported_plane_or_round_count'
        return None
    for used, total in (('moves_used', 'moves_total'), ('rotations_used', 'rotations_total')):
        if values[total] != 1 or not 0 <= values[used] <= 1:
            state['hud_rule_error'] = 'unsupported_action_quota'
            return None
    if min(values['collected_count'], values['inspiration_total']) < 0:
        return None
    if values['collected_count'] > values['inspiration_total']:
        state['last_hud_unknown'] = ['inspiration_progress_pair_inconsistent']
        state['hud_stable'] = 0
        return None
    signature = [values[key] for key in HUD_KEYS]
    if signature == state.get('hud_signature'):
        state['hud_stable'] += 1
    else:
        state.update(hud_signature=signature, hud_stable=1)
    if state['hud_stable'] < 2:
        return None
    state['hud'] = values
    state.pop('last_hud_unknown', None)
    state['rounds_remaining'] = values['rounds_remaining']
    return values


def _adopt_plane(state, hud):
    index = hud['plane_index']
    previous = state.get('plane_index')
    if previous is not None and previous != index:
        if not state.get('awaiting_plane_end') and state['phase'] not in {'next_plane_wait', 'plane_transition'}:
            raise ValueError('unexpected_plane_change')
        _complete_plane(state)
    if previous != index:
        state.update(plane_index=index, plane_epoch=uuid4().hex, plane_turns=0,
                     awaiting_plane_end=False, collected_count=hud['collected_count'])
        state['reward_ledger'].setdefault(str(index), dict(initial=hud['collected_count'],
                                                        confirmed=hud['collected_count'], gained=0))
        _log(state, 'plane_observed', index=index)


def _progress_ledger(state, hud):
    ledger = state['reward_ledger'].setdefault(str(hud['plane_index']),
                                             dict(initial=hud['collected_count'], confirmed=hud['collected_count'], gained=0))
    previous = ledger['confirmed']
    current = hud['collected_count']
    if current != previous:
        ledger['confirmed'] = current
        ledger['gained'] += max(0, current - previous)
        _log(state, 'inspiration_progress', previous=previous, current=current,
             evidence='stable_hud_progress')
    state['collected_count'] = current


def _complete_plane(state):
    plane = state.get('plane_index')
    if plane is not None and plane not in state['completed_plane_indices']:
        state['completed_plane_indices'].append(plane)
        state['planes_completed'] = len(state['completed_plane_indices'])
    state['awaiting_plane_end'] = False


def _compact_layout(layout, state):
    result = {key: deepcopy(layout.get(key)) for key in (
        'schema', 'coordinate_frame', 'status', 'success', 'layout_complete', 'faces',
        'player_cell', 'singularity_cell', 'inspiration_cells', 'known_cells', 'run_id')}
    result['cells'] = sorted([{key: deepcopy(row.get(key)) for key in (
        'face', 'row', 'col', 'occupant', 'occupant_status', 'icon_id', 'node_kind',
        'node_status', 'confidence')} for row in layout.get('cells', [])], key=cell_to_slot)
    result.update(scan_epoch=state['scan_epoch'], plane_epoch=state['plane_epoch'])
    return result


def _snapshot(state):
    return {key: state.get(key) for key in (
        'plane_epoch', 'scan_epoch', 'map_revision', 'pose_epoch', 'layout', 'wide_reference')}


def _commit_action(state, evidence):
    action = state.get('pending_action')
    if not action or action.get('confirmed'):
        return
    action.update(confirmed=True, confirmed_at=time.time(), outcome='confirmed',
                  confirmation_evidence=evidence)
    record = {key: deepcopy(action.get(key)) for key in (
        'action_id', 'kind', 'destination_slot', 'rotation_id', 'snapshot_version',
        'target_occupant', 'confirmed_at', 'outcome', 'confirmation_evidence')}
    state['actions'].append(record)
    _write_json(_directory(state) / 'operations' / action['action_id'] / 'action.json', action)
    state['pending_action'] = None
    state['map_revision'] += 1
    _log(state, 'action_confirmed', action=record)


def _enemy_wait(state, *, observed=False):
    state['waiting_round'] = state.get('rounds_remaining')
    state['enemy_seen'] = bool(observed)
    _invalidate(state, 'enemy_stage')
    _phase(state, 'enemy_wait')


async def _load(session_key, state_store):
    state = await state_store.get(session_key)
    if not isinstance(state, dict) or state.get('schema') != SCHEMA or state.get('session_key') != session_key:
        raise RuntimeError('关卡内运行会话不存在')
    return state


@action_info(name='resonance_pc.deep_dive_planned_run_initialize', public=True,
             read_only=False, timeout=-1, description='Start observed 3x3 in-level play without activity entry.')
@requires_services(app='plans/aura_base/app', state_store='core/state_store', event_bus='core/event_bus')
async def initialize_deep_dive_planned_run(strategy: str = 'chase', safety_round_limit: int = 100,
                                          scan_time_budget_sec: float = 60,
                                          planning_time_budget_sec: float = 30,
                                          app: Any = None, state_store: Any = None, event_bus: Any = None):
    if strategy not in {'chase', 'inspiration'}:
        raise ValueError('Unknown planning strategy')
    if isinstance(safety_round_limit, bool) or not isinstance(safety_round_limit, Integral) or not 1 <= safety_round_limit <= 100:
        raise ValueError('safety_round_limit must be an integer from 1 to 100')
    for value, minimum, maximum in ((scan_time_budget_sec, 5, 900), (planning_time_budget_sec, .1, 300)):
        if isinstance(value, bool) or not math.isfinite(float(value)) or not minimum <= float(value) <= maximum:
            raise ValueError('Invalid scan or planning time budget')
    if app is None or state_store is None:
        raise RuntimeError('app and state_store are required')
    key = 'deep_dive_planned_run:' + uuid4().hex
    directory = resolve_base_path() / 'logs' / 'deep_dive_planned_run' / key.split(':')[-1]
    directory.mkdir(parents=True, exist_ok=False)
    state = dict(schema=SCHEMA, session_key=key, cid=str(current_cid() or ''), sequence=0,
                 strategy=strategy, safety_round_limit=int(safety_round_limit),
                 scan_time_budget_sec=float(scan_time_budget_sec), planning_time_budget_sec=float(planning_time_budget_sec),
                 status='running', phase='scan_turn', phase_started=time.monotonic(), reason='',
                 plane_index=None, plane_epoch=uuid4().hex, planes_completed=0, completed_plane_indices=[],
                 plane_turns=0, turns_completed=0, rounds_remaining=None,
                 scan_epoch=0, map_revision=0, pose_epoch=0, known_cells=0,
                 layout=None, plan=None, scan_candidate=None, pending_action=None,
                 operation_frame=None, wide_reference=None,
                 event_stack=[], event_history=[], actions=[], reward_ledger={}, collected_count=0,
                 terminal_reached=False, terminal=None, outcome='unknown', final_frame=None, last_frame=None,
                 output_dir=str(directory), json_path=str(directory / 'session.json'), report_path=str(directory / 'report.md'),
                 input_owner=None, hud_stable=0, terminal_stable=0, scene_stable=0,
                 awaiting_plane_end=False, enemy_seen=False)
    image = None
    try:
        _cancel_check()
        if tuple(app.get_window_size() or ()) != (1280, 720):
            raise ValueError('关卡内运行要求 1280×720 游戏客户区')
        for _ in range(2):
            image, observation = await _capture(app)
            if state.get('last_frame') is None and image is not None:
                _frame(state, image, 'initial')
            if _terminal(state, image, observation):
                await asyncio.sleep(POLL)
                continue
            if observation.get('scene') in {'choose_move', 'choose_rotate'} and observation.get('player_turn'):
                # These buttons toggle their selection modes in the client.
                # Returning to the ordinary board spends no action quota and
                # lets a restart after an unresolved mapping obtain a new scan.
                if not _stable_scene(state, observation):
                    await asyncio.sleep(POLL)
                    continue
                mode = 'move' if observation['scene'] == 'choose_move' else 'rotate'
                if await _fresh_click(state, app, directed.MODE_POINTS[mode], observation, image):
                    state['start_normalization'] = dict(mode=mode, action='close_selection_mode')
                    await asyncio.sleep(.65)
                continue
            if observation.get('scene') == 'rest_area':
                _phase(state, 'rest_area')
            elif observation.get('scene') != 'board' or not observation.get('player_turn'):
                _stop(state, 'start_requires_player_board_or_rest_area', image)
                break
            await asyncio.sleep(POLL)
    except (asyncio.CancelledError, StopTaskException):
        _stop(state, 'cancel_requested', image, status='cancelled')
    except Exception as exc:
        _stop(state, f'{type(exc).__name__}: {exc}', image)
    if state['status'] != 'running':
        await _release_owned_input(state, app)
        _report(state)
    await _save(state, state_store, event_bus)
    return dict(session_key=key, status=state['status'], success=True)


def _family(state):
    return 'vortex' if state.get('event_stack') and state['event_stack'][-1]['event_type'] == 'vortex' else 'healing'


async def _fresh_click(state, app, point, expected, image, *, action=None, intent=None):
    """A mapped selection cannot be sent after an overlay or camera transition."""
    fresh, observed = await _capture(app, _family(state))
    if _terminal(state, fresh, observed):
        return False
    ocr = _RUN_OCR.get()
    if ocr is not None and expected.get('scene') in {'event_entry', 'rest_area', 'board', 'choose_move', 'choose_rotate'}:
        raw = await _owned(asyncio.to_thread(read_hud, fresh, ocr, observation=observed))
        observed = enrich_observation(observed, raw)
        if action is not None and expected.get('scene') in {'board', 'choose_move', 'choose_rotate'}:
            expected_hud = action.get('before_hud') or state.get('scan_start_hud') or {}
            if any(type(raw.get(key)) is not int for key in HUD_KEYS):
                state['input_guard_reason'] = 'fresh_hud_unresolved'
                return False
            if not _hud_unchanged(expected_hud, raw):
                _stop(state, 'game_state_changed_before_input', fresh)
                return False
            # OCR can take time. Reacquire the input frame after that work,
            # while retaining the numeric read as an additional state guard.
            fresh, observed = await _capture(app, _family(state))
            if _terminal(state, fresh, observed):
                return False
    if not observed.get('valid') or observed.get('scene') != expected.get('scene'):
        state['input_guard_reason'] = 'scene_changed_before_input'
        return False
    if observed.get('scene') in {'settlement', 'uncertain_settlement', 'insufficient_battle_roles', 'enemy_turn'}:
        return False
    fresh_point = list(point)
    if intent and intent.get('action') in {'select_move', 'select_rotate'}:
        if action is None or action['snapshot_version'] != directed.snapshot_version(_snapshot(state)):
            state['input_guard_reason'] = 'selection_snapshot_changed'
            return False
        if action['kind'] == 'move' and len(observed.get('raw_move_options', [])) > 4:
            _stop(state, 'unmodelled_move_candidates', fresh)
            return False
        registered = await _owned(asyncio.to_thread(
            build_operation_frame, fresh, state['layout'], action['kind'],
            scan_epoch=state['scan_epoch'], map_revision=state['map_revision'],
            view_epoch=state['pose_epoch'],
            registration_frame=action.get('registration_frame') if action['kind'] == 'move' else None))
        if registered.get('status') != 'ready':
            state['input_guard_reason'] = registered.get('reason', 'fresh_mapping_unresolved')
            if registered.get('status') == 'blocked':
                _stop(state, state['input_guard_reason'], fresh)
            return False
        previous = action.get('frame') or {}
        old_grid, new_grid = previous.get('stability_signature'), registered.get('stability_signature')
        if old_grid is None or new_grid is None or np.max(np.linalg.norm(
                np.asarray(old_grid) - np.asarray(new_grid), axis=1)) > 6:
            state['input_guard_reason'] = 'operation_view_still_moving'
            return False
        if action['kind'] == 'move':
            binding = bind_move(registered, action['destination_slot'])
        elif (action.get('binding') or {}).get('probe'):
            label = action['binding']['candidate'].get('arrow_label')
            matches = [row for row in registered['candidates'] if row.get('arrow_label') == label]
            binding = (dict(status='ready', point=matches[0]['point'], candidate=matches[0], probe=True)
                       if len(matches) == 1 else dict(status='blocked', reason='probe_arrow_changed'))
        else:
            binding = bind_rotation(registered, action['rotation_id'])
        if binding.get('status') != 'ready':
            state['input_guard_reason'] = binding.get('reason', 'fresh_target_unresolved')
            return False
        # Keep the safe interior point from the fresh logical binding. A pawn
        # can cover the cyan candidate centre, which is not a safe substitute.
        fresh_point = binding['point']
        action.update(frame=registered, binding=binding)
    elif intent and intent.get('action') in {'confirm_rotation', 'cancel_rotation'}:
        if intent['action'] == 'confirm_rotation':
            verification = verify_rotation_preview(fresh, state['layout'], action['rotation_id'], action['frame'])
            if verification.get('status') != 'matched':
                state['input_guard_reason'] = 'preview_changed_before_confirm'
                return False
            fresh_point = observed.get('rotate_confirm_point')
        else:
            fresh_point = observed.get('rotate_cancel_point')
    elif observed.get('scene') == 'event_entry':
        if observed.get('event_type') != expected.get('event_type'):
            state['input_guard_reason'] = 'entry_changed_before_input'
            return False
        fresh_point = observed.get('click')
    elif observed.get('scene') == 'rest_area':
        fresh_point = observed.get('next_point')
    elif observed.get('scene') in {'rotate_preview', 'shop', 'battle_formation', 'battle_victory',
                                  'event_result', 'reward_obtained'}:
        fresh_point = observed.get('click', fresh_point)
    elif observed.get('scene') in {'event_card_selection', 'battle_reward_selection'}:
        old_cards = [(round(r['point'][0] / 20), bool(r.get('selected'))) for r in expected.get('cards', [])]
        new_cards = [(round(r['point'][0] / 20), bool(r.get('selected'))) for r in observed.get('cards', [])]
        if old_cards != new_cards:
            return False
    elif observed.get('scene') == 'event_options':
        old_options = [(r.get('group'), r.get('state')) for r in expected.get('event_options', [])]
        new_options = [(r.get('group'), r.get('state')) for r in observed.get('event_options', [])]
        if old_options != new_options:
            return False
    if not isinstance(fresh_point, (list, tuple)) or len(fresh_point) != 2:
        return False
    x, y = map(int, fresh_point)
    if not 0 <= x < 1280 or not 0 <= y < 720:
        raise ValueError('input_point_out_of_client')
    state['input_inflight'] = dict(point=[x, y], at=time.time(), scene=observed['scene'],
                                   action_id=action.get('action_id') if action else None,
                                   intent=_jsonable(intent), transaction=_jsonable(action))
    _frame(state, fresh, 'before_input')
    _cancel_check()
    await _click_owned(state, app, x, y)
    _cancel_check()
    state['input_inflight'] = None
    state.pop('input_guard_reason', None)
    _log(state, 'input', point=[x, y], scene=observed['scene'],
         action=intent.get('action') if intent else None)
    return True


def _hud_unchanged(first, second):
    return all(first.get(key) == second.get(key) for key in HUD_KEYS)


async def _scan_turn(state, app, state_store, event_bus, hud):
    if hud is None:
        return
    state.pop('resume_after_reward', None)
    _adopt_plane(state, hud)
    if state['turns_completed'] >= state['safety_round_limit']:
        _stop(state, 'safety_round_limit_reached')
        return
    if hud['moves_used'] == hud['rotations_used'] == 1:
        _enemy_wait(state)
        return
    if hud['rounds_remaining'] == 0:
        _phase(state, 'enemy_wait')
        return
    _progress_ledger(state, hud)
    state['scan_epoch'] += 1
    state.update(scan_start_hud=deepcopy(hud), input_owner='scan', known_cells=0,
                 layout=None, plan=None, operation_frame=None)
    await _save(state, state_store, event_bus)
    pending_events = set()

    def progress(values):
        state['known_cells'] = int(values.get('known_cells', state.get('known_cells', 0)) or 0)
        state['scan_progress'] = _jsonable(values)
        state['sequence'] += 1
        if event_bus is not None:
            payload = _summary(state)
            async def publish():
                try:
                    await event_bus.publish(Event(name=PROGRESS, payload=payload))
                except Exception as exc:
                    logger.warning('Scan progress delivery failed: %s', exc)
            worker = asyncio.create_task(publish())
            pending_events.add(worker)
            worker.add_done_callback(pending_events.discard)

    try:
        outcome = await run_layout_scan(app, time_budget_sec=state['scan_time_budget_sec'],
                                        output_dir=_directory(state) / 'scans' / f"{state['scan_epoch']:04d}",
                                        observe_fn=observe, cancel_check=_cancel_check, on_progress=progress)
    finally:
        if pending_events:
            await _owned(asyncio.gather(*list(pending_events), return_exceptions=True))
        state['input_owner'] = None
    state['latest_scan'] = _jsonable(outcome.get('summary', {}))
    state['last_frame'] = outcome.get('last_frame', state.get('last_frame'))
    if outcome.get('status') == 'cancelled':
        _stop(state, 'cancel_requested', status='cancelled')
        return
    if outcome.get('interruption') or outcome.get('status') == 'interrupted':
        state['scan_interruption'] = _jsonable(outcome.get('interruption') or {})
        observation = (outcome.get('interruption') or {}).get('observation', outcome.get('latest_scene_observation', {}))
        _invalidate(state, 'scan_interrupted')
        if observation.get('scene') in REWARD:
            state['resume_after_reward'] = 'scan_turn'
        elif observation.get('scene') not in {'settlement', 'uncertain_settlement'}:
            _stop(state, 'unexpected_scene_during_scan:' + str(observation.get('scene')))
        return
    layout = outcome.get('layout', {})
    if not (outcome.get('success') is True and layout.get('success') is True
            and layout.get('layout_complete') is True and len(layout.get('cells', [])) == 54):
        _stop(state, 'scan_not_complete:' + str(outcome.get('reason')))
        return
    state['scan_candidate'] = _compact_layout(layout, state)
    state['known_cells'] = layout.get('known_cells', 54)
    state.pop('hud_signature', None)
    state['hud_stable'] = 0
    _phase(state, 'accept_scan')


async def _plan(state, state_store, event_bus):
    if (state.get('wide_reference') or {}).get('status') != 'ready':
        _stop(state, 'wide_reference_required_before_planning')
        return
    hud = state['hud']
    layout = deepcopy(state['layout'])
    quota = dict(rounds_remaining=hud['rounds_remaining'], moves_left=1-hud['moves_used'],
                 rotations_left=1-hud['rotations_used'], collected_count=state['collected_count'], move_distance=1)
    layout['planning_state'] = quota
    state['input_owner'] = None
    await _save(state, state_store, event_bus)
    cancellation = threading.Event()
    def cancel():
        return cancellation.is_set() or is_current_task_cancel_requested()
    result = await _owned(asyncio.to_thread(plan_current_state, layout,
                                             strategy=state['strategy'],
                                             rounds_remaining=quota['rounds_remaining'],
                                             moves_left=quota['moves_left'], rotations_left=quota['rotations_left'],
                                             collected_count=quota['collected_count'],
                                             time_budget_sec=state['planning_time_budget_sec'], cancel_check=cancel),
                          cancellation)
    _cancel_check()
    result = _jsonable(result)
    result['snapshot_version'] = directed.snapshot_version(_snapshot(state))
    plan_path = _directory(state) / 'plans' / f"{state['scan_epoch']:04d}.json"
    _write_json(plan_path, result)
    state['latest_plan'] = dict(status=result.get('status'), metrics=result.get('metrics'),
                                next_action=result.get('next_action'), json_path=str(plan_path))
    if result.get('status') == 'waiting_enemy':
        _enemy_wait(state)
        return
    if not result.get('success') or result.get('status') != 'solved' or not result.get('next_action'):
        _stop(state, 'planning_not_actionable:' + str(result.get('reason', result.get('status'))))
        return
    state['plan'] = result
    primitive = result['next_action']
    actor = cell_to_slot(state['layout']['player_cell'])
    snapshot = _snapshot(state)
    if primitive['kind'] == 'move':
        if cell_to_slot(primitive['origin']) != actor:
            raise ValueError('planned_actor_mismatch')
        action = directed.begin_move(cell_to_slot(primitive['destination']), actor, snapshot)
    elif primitive['kind'] == 'rotate_layer':
        action = directed.begin_layer_rotation(primitive['rotation_id'], actor, snapshot)
    else:
        raise ValueError('unsupported_planned_primitive')
    action['before_hud'] = deepcopy(hud)
    state['pending_action'] = _jsonable(action)
    state['pose_epoch'] += 1
    _phase(state, 'operation')


async def _begin_node(state, app, image, observation, action):
    kind = observation.get('event_type')
    if kind not in {'empty', 'shop', 'battle', 'elite_battle', 'boss_battle', 'healing', 'item', 'vortex'}:
        _stop(state, 'unsupported_node_entry:' + str(kind), image)
        return
    boss_target = action.get('target_occupant') == 'singularity'
    if boss_target != (kind == 'boss_battle'):
        _stop(state, 'boss_target_entry_mismatch', image)
        return
    expected = action.get('target_node_kind') or NODE_EVENT_TYPES.get(action.get('target_icon_id'))
    if expected and expected != kind:
        _stop(state, 'target_node_entry_type_mismatch', image)
        return
    owner = 'player_move_encounter' if boss_target else 'player_move_node'
    context = create_context(kind, owner, time.monotonic(), source_action_id=action['action_id'],
                             resume='plane_transition' if boss_target else 'verify_move', observation=observation)
    context['last_player_action'] = action['before_hud']['rotations_used'] == 1
    # Commit the event context only if its recognized Enter control is sent.
    if not await _fresh_click(state, app, observation['click'], observation, image):
        return
    state['event_stack'].append(context)
    state['pending_action']['phase'] = 'node_entered'
    _phase(state, 'event_dispatch')
    _log(state, 'node_entered', event_type=kind, owner=owner, action_id=action['action_id'])


async def _operation(state, app, image, observation):
    action = state.get('pending_action')
    if action is None:
        raise RuntimeError('missing_directed_transaction')
    working = deepcopy(action)
    step = directed.advance_operation(working, image, observation, _snapshot(state))
    status = step['status']
    if status == 'blocked':
        state['pending_action'] = _jsonable(working)
        _stop(state, step.get('reason', 'directed_operation_blocked'), image)
        return
    if status == 'input_requested':
        if await _fresh_click(state, app, step['point'], observation, image, action=working, intent=step):
            if step.get('action') in {'open_move', 'open_rotate', 'open_reference_rotate'}:
                state['pose_epoch'] += 1
                working.setdefault('view_transitions', []).append(dict(
                    view_epoch=state['pose_epoch'], from_scene=observation['scene'],
                    action=step['action'], at=time.time(), hud=deepcopy(working['before_hud'])))
            state['pending_action'] = _jsonable(working)
            if working.get('frame'):
                state['operation_frame'] = _jsonable(working['frame'])
            _write_json(_directory(state) / 'operations' / working['action_id'] / 'action.json', working)
        return
    state['pending_action'] = _jsonable(working)
    if status == 'entry_selected':
        await _begin_node(state, app, image, observation, working)
    elif status == 'outcome_pending':
        _phase(state, 'operation_outcome')


def _action_outcome(state, image, observation, hud):
    action = state.get('pending_action')
    if action is None:
        _invalidate(state, 'missing_outcome_transaction')
        _phase(state, 'scan_turn')
        return
    before = action['before_hud']
    kind = action['kind']
    expected_spent = 'moves_used' if kind == 'move' else 'rotations_used'
    other_spent = 'rotations_used' if kind == 'move' else 'moves_used'
    last_player_action = before[other_spent] == 1
    enemy = observation.get('scene') == 'enemy_turn'
    boss_entry = observation.get('scene') == 'event_entry' and observation.get('event_type') == 'boss_battle'
    if (enemy or boss_entry) and last_player_action:
        _commit_action(state, dict(accepted_stage=observation['scene'],
                                   bound_target=kind == 'move', verified_preview=kind == 'rotate'))
        _enemy_wait(state, observed=True)
        return
    if hud is None:
        return
    if hud['plane_index'] != before['plane_index']:
        _stop(state, 'unexpected_plane_during_action_outcome', image)
        return
    if hud[expected_spent] == 1:
        if hud[other_spent] != before[other_spent]:
            _stop(state, 'action_quota_changed_unexpectedly', image)
            return
        _commit_action(state, dict(quota=hud, registered_selection=True,
                                   verified_preview=kind == 'rotate', node_completed=kind == 'move'))
        _progress_ledger(state, hud)
        if hud['moves_used'] == hud['rotations_used'] == 1:
            _enemy_wait(state)
        else:
            # A fresh scan also resolves the real player's contact cell after
            # a node without any selected-cell marker in the client.
            _invalidate(state, 'own_action_completed')
            _phase(state, 'scan_turn')
        return
    # If the complete enemy animation was missed, a depleted actual round and
    # replenished quotas are a result observation, not an assumed boss path.
    if (last_player_action and hud['moves_used'] == hud['rotations_used'] == 0
            and hud['rounds_remaining'] < before['rounds_remaining']):
        _commit_action(state, dict(new_round=hud, registered_selection=True,
                                   verified_preview=kind == 'rotate'))
        state['turns_completed'] += 1
        state['plane_turns'] += 1
        _invalidate(state, 'new_player_round_observed')
        _phase(state, 'scan_turn')


async def _events(state, app, image, observation):
    context = state['event_stack'][-1]
    working = deepcopy(context)
    step = advance_context(working, observation, time.monotonic())
    if step['status'] == 'input_requested':
        if await _fresh_click(state, app, step['point'], observation, image):
            state['event_stack'][-1] = _jsonable(working)
        return
    state['event_stack'][-1] = _jsonable(working)
    if step['status'] == 'blocked':
        _stop(state, step.get('reason', 'event_blocked'), image)
        return
    if step['status'] != 'completed':
        return
    state['event_stack'].pop()
    state['event_history'].append({key: deepcopy(working.get(key)) for key in (
        'context_id', 'event_type', 'owner', 'source_action_id', 'victory', 'trace')})
    _log(state, 'event_completed', owner=working['owner'], event_type=working['event_type'])
    if working['owner'] in {'player_move_encounter', 'enemy_move_encounter'}:
        if not working.get('victory'):
            _stop(state, 'boss_battle_not_verified_victory', image)
            return
        if working['owner'] == 'player_move_encounter':
            _commit_action(state, dict(boss_battle_victory=True, context_id=working['context_id']))
        state['awaiting_plane_end'] = True
        _invalidate(state, 'boss_battle_completed')
        _phase(state, 'plane_transition')
    elif working['owner'] == 'player_move_node':
        _phase(state, 'verify_move')
    else:
        if state['event_stack']:
            _phase(state, 'event_dispatch')
        else:
            _phase(state, working.get('resume', 'scan_turn'))


async def _boss_from_enemy(state, app, image, observation):
    if not _stable_scene(state, observation):
        return
    context = create_context('boss_battle', 'enemy_move_encounter', time.monotonic(),
                             resume='plane_transition', observation=observation)
    if await _fresh_click(state, app, observation['click'], observation, image):
        state['event_stack'].append(context)
        _phase(state, 'event_dispatch')


async def _rest(state, app, image, observation):
    if state.get('awaiting_plane_end'):
        _complete_plane(state)
    if state['phase'] not in {'rest_area', 'next_plane_wait'}:
        _phase(state, 'rest_area')
    if not _stable_scene(state, observation):
        return
    pending = state.get('rest_pending')
    now = time.monotonic()
    if pending and now - pending['at'] < 2:
        return
    if pending and pending['tries'] >= 3:
        _stop(state, 'next_plane_control_no_progress', image)
        return
    if await _fresh_click(state, app, observation['next_point'], observation, image):
        state['rest_pending'] = dict(at=now, tries=1 if pending is None else pending['tries'] + 1)
        state['expected_next_plane'] = (state['plane_index'] + 1) if state.get('plane_index') else None
        _invalidate(state, 'rest_continue_requested')
        _phase(state, 'next_plane_wait')


async def _advance_state(state, app, state_store, event_bus, image, observation, raw_hud):
    scene = observation.get('scene')
    if _terminal(state, image, observation):
        return
    if scene == 'uncertain_settlement':
        state.setdefault('uncertain_terminal_since', time.monotonic())
        if time.monotonic() - state['uncertain_terminal_since'] >= 20:
            _stop(state, 'settlement_layout_unconfirmed', image)
        return
    state.pop('uncertain_terminal_since', None)
    if not observation.get('valid'):
        state['hud_stable'] = 0
        state['scene_stable'] = 0
        return
    if scene == 'insufficient_battle_roles':
        if _stable_scene(state, observation):
            _stop(state, 'insufficient_battle_roles', image)
        return
    if scene == 'battle_defeat':
        _stop(state, 'battle_defeat', image)
        return
    if state.get('event_stack'):
        state['input_owner'] = 'event'
        await _events(state, app, image, observation)
        return
    hud = None
    if scene in {'board', 'choose_move', 'choose_rotate'}:
        hud = _hud_consensus(state, raw_hud)
        if state.get('hud_rule_error'):
            _stop(state, state['hud_rule_error'], image)
            return
    if scene in REWARD:
        resume = state.pop('resume_after_reward', state['phase'])
        if resume == 'operation' and (state.get('pending_action') or {}).get('submitted'):
            resume = 'operation_outcome'
        context = create_context('passive_reward', 'plane_entry' if state['phase'] in {
            'plane_transition', 'next_plane_wait'} else 'passive_reward', time.monotonic(), resume=resume)
        state['event_stack'].append(context)
        _phase(state, 'event_dispatch')
        return
    if scene == 'rest_area':
        if state['phase'] not in {'plane_transition', 'rest_area', 'next_plane_wait'}:
            _stop(state, 'unexpected_rest_area_without_boss_owner', image)
        else:
            await _rest(state, app, image, observation)
        return
    if state['phase'] in {'operation_outcome', 'verify_move'}:
        _action_outcome(state, image, observation, hud)
        # A boss card reached immediately after the last action is handled on
        # the next fresh poll by its independent enemy owner.
        return
    if state['phase'] == 'operation':
        state['input_owner'] = 'operation'
        await _operation(state, app, image, observation)
        return
    if scene == 'event_entry':
        if observation.get('event_type') == 'boss_battle' and state['phase'] == 'enemy_wait':
            await _boss_from_enemy(state, app, image, observation)
        else:
            _stop(state, 'event_owner_unknown', image)
        return
    if scene in {'battle_formation', 'battle_victory'}:
        _stop(state, 'battle_owner_unknown', image)
        return
    if scene == 'enemy_turn':
        if state['phase'] == 'enemy_wait':
            state['enemy_seen'] = True
        else:
            _stop(state, 'unexpected_enemy_stage', image)
        return
    if state['phase'] == 'enemy_wait':
        if scene == 'board' and hud and hud['moves_used'] == hud['rotations_used'] == 0:
            previous = state.get('waiting_round')
            if state.get('enemy_seen') or previous is not None and hud['rounds_remaining'] < previous:
                state['turns_completed'] += 1
                state['plane_turns'] += 1
                _invalidate(state, 'new_player_round_observed')
                _phase(state, 'scan_turn')
        return
    if state['phase'] in {'next_plane_wait', 'plane_transition'}:
        if scene == 'board' and hud:
            expected = state.get('expected_next_plane')
            old = state.get('plane_index')
            if expected is not None and hud['plane_index'] != expected:
                return
            if old is not None and hud['plane_index'] == old:
                return
            _adopt_plane(state, hud)
            state.pop('rest_pending', None)
            state.pop('expected_next_plane', None)
            _invalidate(state, 'new_plane_observed')
            _phase(state, 'scan_turn')
        return
    if state['phase'] == 'scan_turn':
        if scene == 'board' and observation.get('player_turn'):
            await _scan_turn(state, app, state_store, event_bus, hud)
        return
    if state['phase'] == 'accept_scan':
        if hud is None or scene != 'board':
            return
        if not _hud_unchanged(state['scan_start_hud'], hud):
            _stop(state, 'game_state_changed_during_scan', image)
            return
        state['layout'] = state.pop('scan_candidate')
        state['layout']['planning_state'] = dict(moves_left=1-hud['moves_used'],
                                                 rotations_left=1-hud['rotations_used'], move_distance=1,
                                                 rounds_remaining=hud['rounds_remaining'],
                                                 collected_count=state['collected_count'])
        _phase(state, 'reset_wide')
        return
    if state['phase'] == 'reset_wide':
        if scene != 'board' or hud is None:
            return
        if not _hud_unchanged(state['scan_start_hud'], hud):
            _stop(state, 'game_state_changed_before_wide_reset', image)
            return
        state['input_owner'] = 'wide_reset'
        try:
            reset = await reset_layout_view(
                app, _directory(state) / 'wide_reference' / f"{state['scan_epoch']:04d}",
                time_budget_sec=10, observe_fn=observe, cancel_check=_cancel_check)
        finally:
            state['input_owner'] = None
        state['wide_reset'] = _jsonable(reset)
        if reset.get('success') is False or reset.get('status') in {'blocked', 'interrupted', 'cancelled'}:
            _stop(state, reset.get('reason', 'wide_reset_unconfirmed'), image,
                  status='cancelled' if reset.get('status') == 'cancelled' else 'blocked')
            return
        state['pose_epoch'] += 1
        state['wide_reference'] = None
        state.pop('wide_reference_stability', None)
        _phase(state, 'wide_reference')
        return
    if state['phase'] == 'wide_reference':
        if scene != 'board' or hud is None:
            return
        if not _hud_unchanged(state['scan_start_hud'], hud):
            _stop(state, 'game_state_changed_during_wide_reference', image)
            return
        reference = await _owned(asyncio.to_thread(
            build_wide_reference_frame, image, state['layout'],
            scan_epoch=state['scan_epoch'], map_revision=state['map_revision'],
            view_epoch=state['pose_epoch']))
        if reference.get('status') != 'ready':
            state['last_mapping_reason'] = reference.get('reason', 'wide_reference_unconfirmed')
            if reference.get('status') == 'blocked':
                _stop(state, state['last_mapping_reason'], image)
            return
        stability = state.setdefault('wide_reference_stability', {})
        if not directed._stable_reference(stability, reference):
            return
        state['wide_reference'] = _jsonable(reference)
        state.pop('wide_reference_stability', None)
        state.pop('last_mapping_reason', None)
        _write_json(_directory(state) / 'wide_reference' / f"{state['scan_epoch']:04d}.json", reference)
        _frame(state, image, 'wide_reference')
        _phase(state, 'planning')
        return
    if state['phase'] == 'planning':
        if hud is None:
            return
        if not _hud_unchanged(state['scan_start_hud'], hud):
            _stop(state, 'game_state_changed_before_planning', image)
            return
        await _plan(state, state_store, event_bus)
        return


async def _release_owned_input(state, app):
    if state.get('input_maybe_held'):
        try:
            await _owned(app.controller.mouse_up_async('left'), propagate_cancel=False)
        except Exception as exc:
            state['release_error'] = str(exc)
            logger.warning('Planned run input release failed: %s', exc)
        state['input_maybe_held'] = False
    state['input_owner'] = None


def _report(state):
    summary = _summary(state)
    rows = [('状态', summary['status']), ('阶段', summary['phase']), ('策略', summary['strategy']),
            ('位面', summary['plane_index']), ('完成回合', summary['turns_completed']),
            ('实际剩余回合', summary['rounds_remaining']), ('终点', summary['terminal']),
            ('游戏结果', summary['outcome']), ('原因', summary['reason'])]
    text = '# 识海深潜关卡内运行记录\n\n| 项目 | 值 |\n|---|---|\n'
    text += '\n'.join('| ' + str(label) + ' | ' + str(value).replace('|', '\\|') + ' |'
                      for label, value in rows)
    text += '\n\n本记录描述本次自动流程状态；到达结算与游戏通关分别记录。\n'
    text += '\n灵感账本依据已确认的地图灵感任务进度，不代表全部战斗货币奖励。\n'
    text += '\n- [完整会话](session.json)\n- [动作与事件](events.jsonl)\n'
    if summary.get('final_frame') or summary.get('last_frame'):
        path = Path(summary.get('final_frame') or summary['last_frame'])
        text += '\n![最后画面](' + path.relative_to(_directory(state)).as_posix() + ')\n'
    Path(state['report_path']).write_text(text, encoding='utf-8')


@action_info(name='resonance_pc.deep_dive_planned_run_advance', public=True,
             read_only=False, timeout=-1, description='Advance one owned in-level stage or bounded complete scan.')
@requires_services(app='plans/aura_base/app', ocr='plans/aura_base/ocr',
                   state_store='core/state_store', event_bus='core/event_bus')
async def advance_deep_dive_planned_run(session_key: str, app: Any = None, ocr: Any = None,
                                       state_store: Any = None, event_bus: Any = None):
    state = await _load(session_key, state_store)
    if state['status'] != 'running':
        return _summary(state)
    image = None
    token = _RUN_OCR.set(ocr)
    try:
        _cancel_check()
        if tuple(app.get_window_size() or ()) != (1280, 720):
            raise ValueError('client_resolution_changed')
        image, observation = await _capture(app, _family(state))
        raw_hud = {key: None for key in HUD_KEYS}
        if observation.get('valid') and observation.get('scene') not in {'settlement', 'uncertain_settlement'}:
            raw_hud = await _owned(asyncio.to_thread(read_hud, image, ocr, observation=observation))
            observation = enrich_observation(observation, raw_hud)
        state['last_scene'] = observation.get('scene', 'unknown')
        state['last_hud_read'] = _jsonable(raw_hud)
        await _advance_state(state, app, state_store, event_bus, image, observation, raw_hud)
        if state['status'] == 'running':
            limit = PHASE_LIMITS.get(state['phase'], 45)
            if state['phase'] == 'scan_turn':
                limit = max(limit, state['scan_time_budget_sec'] + 30)
            elif state['phase'] == 'planning':
                limit = max(limit, state['planning_time_budget_sec'] + 15)
            if time.monotonic() - state['phase_started'] >= limit:
                details = (state.get('last_hud_unknown') or state.get('last_mapping_reason')
                           or (state.get('pending_action') or {}).get('last_mapping_reason')
                           or state.get('last_scene'))
                _stop(state, 'phase_timeout:' + state['phase'] + ':' + str(details), image)
        if state['status'] == 'running':
            await asyncio.sleep(POLL)
    except (asyncio.CancelledError, StopTaskException):
        _stop(state, 'cancel_requested', image, status='cancelled')
    except Exception as exc:
        logger.warning('Deep Dive planned run blocked: %s', exc)
        _stop(state, f'{type(exc).__name__}: {exc}', image)
    finally:
        _RUN_OCR.reset(token)
        if state['status'] != 'running':
            await _release_owned_input(state, app)
            _report(state)
        await _owned(_save(state, state_store, event_bus), propagate_cancel=False)
    return _summary(state)


@action_info(name='resonance_pc.deep_dive_planned_run_finish', public=True,
             read_only=False, timeout=-1, description='Persist an in-level result without settlement clicks or restart.')
@requires_services(app='plans/aura_base/app', state_store='core/state_store', event_bus='core/event_bus')
async def finish_deep_dive_planned_run(session_key: str, app: Any = None,
                                      state_store: Any = None, event_bus: Any = None):
    state = await _load(session_key, state_store)
    if state['status'] == 'running':
        _stop(state, 'scheduler_iteration_limit_reached')
    await _release_owned_input(state, app)
    _report(state)
    await _save(state, state_store, event_bus)
    return _summary(state)
