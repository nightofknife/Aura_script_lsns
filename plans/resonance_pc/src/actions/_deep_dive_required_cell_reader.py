"""Read only requested ordinary nodes in a freshly registered wide view."""
from __future__ import annotations

from copy import deepcopy
import math
import time

import cv2
import numpy as np

from ._deep_dive_layout_semantics import classify_icon
from ._deep_dive_layout_vision import _crop
from ._deep_dive_operation_frame import _layout_digest
from ._deep_dive_planner_rules import cell_to_slot
from ._deep_dive_target_readiness import targets_readiness


def read_required_cells(image, layout, reference, slots, votes, *, source_id,
                        source_time=None, source_session=None, targets=()):
    """Return reviewed cells; never change the supplied atlas or input state.

    A node needs three fresh registered captures spanning at least .4 seconds.
    All surviving cube registrations must agree on its slot. A sprite overlap,
    stale atlas digest, conflicting glyph, or invalid frame discards its votes.
    """
    now = time.monotonic() if source_time is None else float(source_time)
    cells = {cell_to_slot(row): row for row in layout.get('cells', ())}
    if (source_id is None or not math.isfinite(now)
            or not isinstance(image, np.ndarray) or image.shape != (720, 1280, 3)
            or image.dtype != np.uint8 or not targets_readiness(layout)['ready']
            or reference.get('status') != 'ready'
            or reference.get('layout_digest') != _layout_digest(cells)):
        votes.clear()
        return dict(ready=False, reason='required_cell_registration_invalid', cells=[])
    quality = reference.get('geometry_quality', {})
    if (quality.get('anchors', 0) < 7 or len(quality.get('face_anchors', ())) != 3
            or min(quality['face_anchors']) < 2 or quality.get('rmse_px', 99) > 6
            or quality.get('consensus', 0) < .88):
        votes.clear()
        return dict(ready=False, reason='required_cell_anchor_insufficient', cells=[])
    mappings = reference.get('Q_candidates', ())
    accepted = []
    for slot in slots:
        key = str(int(slot))
        projected = [row for row in reference.get('grid_cells', ())
                     if row.get('logical_slots') == [int(slot)]]
        if len(projected) != 1 or not mappings:
            votes.pop(key, None)
            continue
        item = projected[0]
        if any(mapping['op_to_logical'][item['operation_slot']] != slot for mapping in mappings):
            votes.pop(key, None)
            continue
        quad = np.float32(item['quad'])
        if not np.isfinite(quad).all() or np.any(quad < [298, 82]) or np.any(quad > [955, 619]):
            votes.pop(key, None)
            continue
        blocked = False
        for target in targets:
            x, y, w, h = target['box']
            box = np.float32(((x, y), (x+w, y), (x+w, y+h), (x, y+h)))
            area, _ = cv2.intersectConvexConvex(quad, box)
            if area > 0:
                blocked = True
                break
        if blocked:
            votes.pop(key, None)
            continue
        reading = classify_icon(_crop(image, quad))
        icon, confidence = reading.get('icon_id'), reading.get('confidence', 0)
        if not icon or confidence < .70:
            votes.pop(key, None)
            continue
        previous = votes.get(key, {})
        if (previous.get('icon') != icon or previous.get('session') != source_session
                or now < previous.get('last_at', now) or (previous and np.max(np.linalg.norm(
                quad-np.float32(previous['quad']), axis=1)) > 6)):
            previous = dict(icon=icon, first_at=now, sources=[], confidence=[], session=source_session)
        if source_id not in previous['sources']:
            previous['sources'].append(source_id)
            previous['confidence'].append(float(confidence))
        previous['quad'] = quad.tolist()
        previous['last_at'] = now
        votes[key] = previous
        if len(previous['sources']) >= 3 and now-previous['first_at'] >= .4:
            row = deepcopy(cells[int(slot)])
            row.update(occupant='none', occupant_status='confirmed', icon_id=icon,
                       node_status='known', confidence=min(previous['confidence']),
                       node_read_evidence=dict(source='registered_requested_node',
                           source_ids=previous['sources'][-3:], quad=quad.tolist(),
                           image_digest=reference['image_digest'],
                           target_inventory_closed=True))
            accepted.append(row)
    return dict(ready=len(accepted) == len(slots), cells=accepted,
                reason='required_cells_confirmed' if len(accepted) == len(slots)
                else 'required_cells_need_fresh_registered_votes')


def promote_read_reference(reference, layout, updates, *, map_revision):
    """Carry the same-frame registered pose across proven node-only updates.

    All surviving Q mappings already agreed on each read slot. Adding its
    independently read glyph cannot invalidate the original anchor matches.
    The caller must also require two fresh stable reference frames.
    """
    cells = {cell_to_slot(row): row for row in layout['cells']}
    if (not targets_readiness(layout)['ready'] or reference.get('status') != 'ready'
            or reference.get('layout_digest') != _layout_digest(cells)):
        raise ValueError('requested_read_reference_changed')
    changes = {}
    for row in updates:
        slot = cell_to_slot(row)
        proof = row.get('node_read_evidence') or {}
        projected = [item for item in reference['grid_cells'] if item.get('logical_slots') == [slot]]
        if (len(projected) != 1 or not reference.get('Q_candidates')
                or any(q['op_to_logical'][projected[0]['operation_slot']] != slot
                       for q in reference['Q_candidates'])
                or proof.get('source') != 'registered_requested_node'
                or proof.get('image_digest') != reference.get('image_digest')
                or len(set(proof.get('source_ids', ()))) < 3
                or row.get('occupant') != 'none' or row.get('node_status') != 'known'
                or row.get('occupant_status') != 'confirmed' or not row.get('icon_id')
                or cells[slot].get('occupant') not in ('none', 'unknown')
                or any(cells[slot].get('occupant_evidence_counts', {}).get(kind, 0)
                       for kind in ('player', 'singularity', 'inspiration'))):
            raise ValueError('requested_read_reference_unproven')
        # Only ordinary node contents and the attached node proof may change.
        allowed = {'node_status', 'icon_id', 'node_kind', 'confidence', 'node_read_evidence',
                   'occupant', 'occupant_status'}
        if any(row.get(key) != cells[slot].get(key) for key in set(row) | set(cells[slot]) if key not in allowed):
            raise ValueError('requested_read_changed_non_node_state')
        changes[slot] = deepcopy(row)
    if not changes:
        raise ValueError('requested_read_empty_update')
    cells.update(changes)
    promoted = deepcopy(reference)
    promoted.update(layout_digest=_layout_digest(cells), map_revision=map_revision)
    promoted.setdefault('evidence', {})['requested_node_registration_extension'] = dict(
        cells=sorted(changes), image_digest=reference['image_digest'],
        original_layout_digest=reference['layout_digest'], fresh_stability_required=True)
    return promoted
