"""Directed input transactions. Only the caller may execute a returned intent."""
from __future__ import annotations

from copy import deepcopy
import json
import math
import time
from uuid import uuid4

from ._deep_dive_planner_rules import cell_to_slot, coord_dict, geometry
from ._deep_dive_operation_frame import (
    build_operation_frame, build_wide_reference_frame, bind_move, bind_rotation, verify_rotation_preview,
)
from ._deep_dive_target_readiness import check_required_cells, is_targets_layout, targets_readiness

MODE_POINTS = {'move': [1100, 406], 'rotate': [1100, 480]}


def snapshot_version(snapshot):
    return {key: snapshot[key] for key in ('plane_epoch', 'scan_epoch', 'map_revision')}


def prepare_operation(mode, snapshot):
    if mode not in {'move', 'rotate'}:
        raise ValueError('Unsupported operation mode')
    layout = snapshot['layout']
    if layout.get('prediction_only'):
        raise ValueError('prediction_only_layout')
    target_mode = is_targets_layout(layout)
    if not (layout.get('success') and layout.get('layout_complete')) and not target_mode:
        raise ValueError('A current complete scan is required')
    if target_mode:
        readiness = targets_readiness(layout)
        if not readiness['ready']:
            raise ValueError(readiness['reason'])
    reference = deepcopy(snapshot.get('wide_reference'))
    return dict(action_id=uuid4().hex, kind=mode,
                phase='open_mode' if reference else 'wide_reference',
                snapshot_version=snapshot_version(snapshot),
                expected_actor_slot=cell_to_slot(layout['player_cell']),
                started=time.monotonic(), progress_at=time.monotonic(),
                mode_attempts=0, select_attempts=0, confirm_attempts=0,
                pending_input=None, frame=None, binding=None, stable_signature=None,
                stable=0, probes=[], submitted=False, confirmed=False, evidence=[],
                registration_frame=reference, reference_attempts=0, view_transitions=[])


def begin_move(destination_slot, expected_origin_slot, snapshot):
    action = prepare_operation('move', snapshot)
    origin = int(expected_origin_slot)
    destination = int(destination_slot)
    if action['expected_actor_slot'] != origin:
        raise ValueError('actor_mismatch')
    geo = geometry()
    if destination not in geo.moves[origin, :geo.counts[origin]]:
        raise ValueError('target_not_adjacent_same_face')
    target = next(row for row in snapshot['layout']['cells'] if cell_to_slot(row) == destination)
    required = check_required_cells(snapshot['layout'], dict(kind='move', destination_slot=destination))
    if not required['ready']:
        raise ValueError(required['reason'])
    action.update(destination_slot=destination, target_occupant=target['occupant'],
                  target_node_kind=target.get('node_kind'), target_icon_id=target.get('icon_id'))
    return action


def begin_layer_rotation(rotation_id, expected_actor_slot, snapshot):
    action = prepare_operation('rotate', snapshot)
    rotation = int(rotation_id)
    if action['expected_actor_slot'] != int(expected_actor_slot):
        raise ValueError('actor_mismatch')
    if rotation not in geometry().actor_rotations[action['expected_actor_slot']]:
        raise ValueError('rotation_not_available_to_actor')
    action['rotation_id'] = rotation
    return action


def rotated_layout(layout, rotation_id):
    """An explicit prediction used only for confirmation, never a fresh scan."""
    result = deepcopy(layout)
    permutation = geometry().rotations[int(rotation_id)]
    cells = [None] * 54
    for old in layout['cells']:
        source = cell_to_slot(old)
        destination = int(permutation[source])
        row = deepcopy(old)
        row.update(coord_dict(destination))
        row['evidence_source'] = 'rotation_prediction'
        cells[destination] = row
    result['cells'] = cells
    result['player_cell'] = coord_dict(int(permutation[cell_to_slot(layout['player_cell'])]))
    result['singularity_cell'] = coord_dict(int(permutation[cell_to_slot(layout['singularity_cell'])]))
    result['inspiration_cells'] = [coord_dict(int(permutation[cell_to_slot(c)]))
                                   for c in layout.get('inspiration_cells', [])]
    result['prediction_only'] = True
    return result


def _stable(action, signature):
    # State stores may round-trip tuples through JSON as lists. Persist a
    # canonical scalar so settling does not restart on every scheduler step.
    signature = json.dumps(signature, sort_keys=True, separators=(',', ':'))
    if signature == action.get('stable_signature'):
        action['stable'] += 1
    else:
        action.update(stable_signature=signature, stable=1)
    return action['stable'] >= 2


def _wait(reason=None):
    return {'status': 'waiting', 'reason': reason}


def _blocked(reason):
    return {'status': 'blocked', 'reason': reason}


def _issue(action, name, point, now, expected_scene):
    action['pending_input'] = dict(name=name, point=list(point), at=now,
                                   expected_scene=expected_scene)
    action['progress_at'] = now
    action['stable_signature'] = None
    action['stable'] = 0
    return dict(status='input_requested', action=name, point=list(map(int, point)),
                expected_scene=expected_scene, action_id=action['action_id'])


def _frame(image, action, snapshot, **detector_kwargs):
    return build_operation_frame(image, snapshot['layout'], action['kind'],
                                 scan_epoch=snapshot['scan_epoch'],
                                 map_revision=snapshot['map_revision'],
                                 view_epoch=snapshot.get('pose_epoch', 0),
                                 registration_frame=action.get('registration_frame') if action['kind'] == 'move' else None,
                                 **detector_kwargs)


def _stable_reference(action, frame):
    previous = action.get('reference_candidate') or {}
    old_grid, new_grid = previous.get('grid_cells', []), frame.get('grid_cells', [])
    same = (len(old_grid) == len(new_grid) == 27
            and previous.get('actor_operation_slot') == frame.get('actor_operation_slot')
            and sorted(row['symmetry_id'] for row in previous.get('Q_candidates', []))
            == sorted(row['symmetry_id'] for row in frame.get('Q_candidates', []))
            and max(math.dist(a['centre'], b['centre']) for a, b in zip(old_grid, new_grid)) <= 6)
    action['stable'] = action.get('stable', 0) + 1 if same else 1
    action['reference_candidate'] = frame
    return action['stable'] >= 2


def _binding(action, frame):
    return (bind_move(frame, action['destination_slot']) if action['kind'] == 'move'
            else bind_rotation(frame, action['rotation_id']))


def _probe_choice(action, frame, desired):
    """Only after a verified mismatch, consider an untried recognized arrow."""
    if not action['probes']:
        return desired
    point = desired.get('point', desired.get('selected_point'))
    if point is not None and not any(math.dist(point, old) < 30 for old in action['probes']):
        return desired
    for row in sorted(frame.get('candidates', []), key=lambda r: (r['point'][1], r['point'][0])):
        if not any(math.dist(row['point'], old) < 30 for old in action['probes']):
            return dict(status='ready', point=row['point'], selected_point=row['point'],
                        probe=True, candidate=row)
    return dict(status='blocked', reason='rotation_probe_candidates_exhausted')


def advance_operation(action, image, observation, snapshot, now=None, *, target_detector=None):
    """Mutate a working copy; the caller commits it only after fresh input guards."""
    now = time.monotonic() if now is None else now
    detector_kwargs = {} if target_detector is None else {'target_detector': target_detector}
    if action['snapshot_version'] != snapshot_version(snapshot):
        return _blocked('stale_layout')
    if snapshot['layout'].get('prediction_only'):
        return _blocked('prediction_only_layout')
    if action['kind'] == 'move' and 'destination_slot' in action:
        required = check_required_cells(snapshot['layout'], action)
        if not required['ready']:
            return dict(_blocked(required['reason']), required_cells=required['required_cells'])
    if not observation.get('valid'):
        action['stable_signature'] = None
        action['stable'] = 0
        return _wait('invalid_frame')
    scene = observation.get('scene')
    mode_scene = 'choose_move' if action['kind'] == 'move' else 'choose_rotate'
    phase = action['phase']
    if now - action['started'] > 90:
        return _blocked('directed_operation_timeout')
    pending = action.get('pending_input')
    if phase == 'wide_reference':
        if scene != 'board' or not observation.get('player_turn'):
            return _wait('ordinary_wide_reference_required')
        frame = build_wide_reference_frame(image, snapshot['layout'],
                                          scan_epoch=snapshot['scan_epoch'],
                                          map_revision=snapshot['map_revision'],
                                          view_epoch=snapshot.get('pose_epoch', 0), **detector_kwargs)
        if frame.get('status') != 'ready':
            action['last_mapping_reason'] = frame.get('reason', 'reference_three_faces_unconfirmed')
            if frame.get('status') == 'blocked' or now - action['progress_at'] > 30:
                return _blocked(action['last_mapping_reason'])
            return _wait(action['last_mapping_reason'])
        if not _stable_reference(action, frame):
            return _wait('reference_view_stability')
        action.update(registration_frame=frame, phase='open_mode', mode_attempts=0,
                      pending_input=None, stable_signature=None, stable=0, progress_at=now)
        action.pop('reference_candidate', None)
        return _wait()
    if phase == 'open_reference_mode':
        if scene == 'choose_rotate':
            action.update(phase='reference_mapping', pending_input=None,
                          stable_signature=None, stable=0, progress_at=now)
            return _wait()
        if scene not in {'board', 'choose_move'} or not observation.get('player_turn'):
            return _wait('waiting_for_reference_mode')
        if pending and now - pending['at'] < 2:
            return _wait()
        if action['reference_attempts'] >= 3:
            return _blocked('reference_mode_confirmation_limit')
        action['reference_attempts'] += 1
        return _issue(action, 'open_reference_rotate', MODE_POINTS['rotate'], now, scene)
    if phase == 'reference_mapping':
        if scene != 'choose_rotate':
            return _wait('waiting_for_reference_mode')
        frame = build_operation_frame(image, snapshot['layout'], 'rotate',
                                      scan_epoch=snapshot['scan_epoch'],
                                      map_revision=snapshot['map_revision'],
                                      view_epoch=snapshot.get('pose_epoch', 0), **detector_kwargs)
        if frame.get('status') != 'ready':
            action['last_mapping_reason'] = frame.get('reason', 'reference_three_faces_unconfirmed')
            if frame.get('status') == 'blocked' or now - action['progress_at'] > 30:
                return _blocked(action['last_mapping_reason'])
            return _wait(action['last_mapping_reason'])
        if not _stable_reference(action, frame):
            return _wait('reference_view_stability')
        action.update(registration_frame=frame, phase='open_mode', mode_attempts=0,
                      pending_input=None, stable_signature=None, stable=0, progress_at=now)
        action.pop('reference_candidate', None)
        action['evidence'].append(dict(kind='three_face_reference',
                                      view_epoch=frame.get('view_epoch'),
                                      image_digest=frame.get('image_digest'),
                                      geometry_quality=frame.get('geometry_quality')))
        return _wait()
    if phase == 'open_mode':
        if scene == mode_scene:
            action.update(phase='mapping', pending_input=None)
            return _wait()
        acceptable = {'board'}
        if action['kind'] == 'move' and action.get('registration_frame'):
            acceptable.add('choose_rotate')
        if scene not in acceptable or not observation.get('player_turn'):
            return _wait()
        if pending and now - pending['at'] < 2:
            return _wait()
        if action['mode_attempts'] >= 3:
            return _blocked('mode_button_confirmation_limit')
        action['mode_attempts'] += 1
        return _issue(action, 'open_' + action['kind'], MODE_POINTS[action['kind']], now, scene)
    if phase == 'mapping':
        if scene != mode_scene:
            return _wait('waiting_for_operation_mode')
        frame = _frame(image, action, snapshot, **detector_kwargs)
        if frame.get('status') != 'ready':
            action['last_mapping_reason'] = frame.get('reason', 'three_face_mapping_unresolved')
            if frame.get('status') == 'blocked':
                return _blocked(action['last_mapping_reason'])
            if now - action['progress_at'] > 30:
                return _blocked(action['last_mapping_reason'])
            return _wait(action['last_mapping_reason'])
        binding = _binding(action, frame)
        if action['kind'] == 'rotate':
            binding = _probe_choice(action, frame, binding)
        point = binding.get('point', binding.get('selected_point'))
        if binding.get('status') != 'ready' or point is None:
            if now - action['progress_at'] > 30:
                return _blocked(binding.get('reason', 'target_not_selectable'))
            return _wait(binding.get('reason'))
        signature = (action['kind'], tuple(round(float(v) / 8) for v in point),
                     binding.get('logical_slot'), binding.get('operation_rotation_id'))
        if not _stable(action, signature):
            return _wait('mapping_stability')
        if action['select_attempts'] >= 3:
            return _blocked('target_selection_confirmation_limit')
        action.update(frame=frame, binding=binding,
                      phase='move_selection_wait' if action['kind'] == 'move' else 'rotation_preview_wait')
        action['select_attempts'] += 1
        action['evidence'].append(dict(kind='three_face_mapping', binding=binding,
                                        geometry_quality=frame.get('geometry_quality')))
        return _issue(action, 'select_' + action['kind'], point, now, mode_scene)
    if phase == 'move_selection_wait':
        if scene == 'event_entry':
            if not _stable(action, ('entry', observation.get('event_type'))):
                return _wait()
            # The client does not draw a distinct selected-tile marker. The
            # registered fresh click establishes selection intent; movement
            # itself is confirmed later by quota and current-layout evidence.
            action['selection_evidence'] = 'registered_fresh_target_and_entry'
            return dict(status='entry_selected', event_type=observation.get('event_type'),
                        action_id=action['action_id'])
        if scene == 'ambiguous_event_entry':
            return _blocked('unsupported_or_ambiguous_entry')
        if scene == 'board' and observation.get('move_done'):
            return dict(status='outcome_pending', action_id=action['action_id'])
        if scene == mode_scene and pending and now - pending['at'] >= 2:
            action.update(phase='mapping', pending_input=None, stable_signature=None, stable=0)
        return _wait()
    if phase == 'rotation_preview_wait':
        if scene != 'rotate_preview':
            if scene == mode_scene and pending and now - pending['at'] >= 2:
                action.update(phase='mapping', pending_input=None, stable_signature=None, stable=0)
            return _wait()
        if not _stable(action, ('preview', action['rotation_id'])):
            return _wait()
        verification = verify_rotation_preview(image, snapshot['layout'], action['rotation_id'], action['frame'],
                                               **detector_kwargs)
        action['preview_evidence'] = verification
        if verification.get('status') == 'matched':
            action.update(phase='rotation_confirm_wait', submitted=True)
            action['confirm_attempts'] = 1
            return _issue(action, 'confirm_rotation', observation['rotate_confirm_point'], now, 'rotate_preview')
        cancel = observation.get('rotate_cancel_point')
        if cancel is None:
            return _blocked('rotation_cancel_control_unavailable')
        action['probes'].append(list(action['binding'].get('point', action['binding'].get('selected_point'))))
        action.update(phase='rotation_cancel_wait',
                      stop_after_restore=verification.get('status') != 'mismatch',
                      restore_reason=verification.get('reason', 'rotation_preview_unresolved'))
        return _issue(action, 'cancel_rotation', cancel, now, 'rotate_preview')
    if phase == 'rotation_cancel_wait':
        if scene == 'rotate_preview' and pending and now - pending['at'] >= 2:
            tries = action.get('cancel_attempts', 1)
            if tries >= 3:
                return _blocked('cancel_restore_unverified')
            action['cancel_attempts'] = tries + 1
            return _issue(action, 'cancel_rotation', observation['rotate_cancel_point'], now, 'rotate_preview')
        if scene != mode_scene:
            return _wait()
        restored = _frame(image, action, snapshot, **detector_kwargs)
        if restored.get('status') != 'ready' or not _stable(action, ('restored', action['action_id'])):
            return _wait('cancel_restore_unverified')
        if action.get('stop_after_restore') or len(action['probes']) >= 4:
            return _blocked(action.get('restore_reason', 'rotation_probe_limit'))
        action.update(phase='mapping', pending_input=None, select_attempts=0,
                      cancel_attempts=0, stable_signature=None, stable=0)
        return _wait()
    if phase == 'rotation_confirm_wait':
        if scene != 'rotate_preview':
            action.update(phase='outcome_wait', pending_input=None)
            return dict(status='outcome_pending', action_id=action['action_id'])
        if pending and now - pending['at'] >= 2:
            if action['confirm_attempts'] >= 3:
                return _blocked('rotation_confirmation_limit')
            verification = verify_rotation_preview(image, snapshot['layout'], action['rotation_id'], action['frame'],
                                                   **detector_kwargs)
            if verification.get('status') != 'matched':
                return _blocked('rotation_confirmation_preview_changed')
            action['confirm_attempts'] += 1
            return _issue(action, 'confirm_rotation', observation['rotate_confirm_point'], now, 'rotate_preview')
        return _wait()
    if phase == 'outcome_wait':
        return dict(status='outcome_pending', action_id=action['action_id'])
    return _blocked('unknown_operation_phase:' + str(phase))
