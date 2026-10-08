"""Close projected side-peek aliases using actual direct negative observations.

Projected geometry never observes an occupancy. Explanations only reference an
already independently confirmed owner and preserve all original observations.
"""
from __future__ import annotations

from copy import deepcopy
import math

import numpy as np

from ._deep_dive_four_view_reader import source_key
from ._deep_dive_planner_rules import cell_to_slot


def _relevant(row):
    confidence = row.get('confidence', 0.)
    return row.get('confirmable') is True or (not isinstance(confidence, bool)
        and isinstance(confidence, (int, float)) and math.isfinite(confidence) and confidence >= .25)


def _identity(row):
    packet = row.get('target_packet') or row
    return (source_key(row['source']), row.get('group'), row.get('kind'),
            tuple(packet.get('point') or ()), tuple(packet.get('box') or ()), row.get('track_id'))


def _valid_row(row, epoch, context):
    try:
        return (row.get('scan_epoch') == epoch and source_key(row['source'])[:2] == context
                and type(row.get('group')) is int and row['group'] >= 0)
    except (ValueError, KeyError, TypeError):
        return False


def _owner_groups(owner, epoch, context, primary):
    """Count direct ownership witnesses whose cells were actually visible."""
    if owner.get('occupant_status') != 'confirmed' or owner.get('independent_view_count', 0) < 2:
        return set()
    try:
        slot = cell_to_slot(owner['cell'])
        if slot != owner['cell_index']:
            return set()
    except (ValueError, KeyError, TypeError):
        return set()
    confidence = owner.get('confidence', 0.)
    if not isinstance(confidence, (int, float)) or isinstance(confidence, bool) or not math.isfinite(confidence) or not .70 <= confidence <= 1:
        return set()
    groups = set()
    for evidence in owner.get('evidence', ()):
        if not _valid_row(evidence, epoch, context):
            continue
        key = (source_key(evidence['source']), evidence['group'])
        if slot not in primary.get(key, set()):
            continue
        if any(c.get('cell_index') == slot and c.get('candidate_only') is not True
               for c in evidence.get('candidates', ())):
            groups.add(evidence['group'])
    return groups


def _candidate_evidence_supported(row, context, epoch):
    if (not _valid_row(row, epoch, context) or row.get('body_candidate_fit_supported') is not True
            or row.get('candidate_projection_complete') is not True):
        return False
    try:
        if source_key(row['body_candidate_fit_source']) != source_key(row['source']):
            return False
    except (ValueError, KeyError, TypeError):
        return False
    digest = row.get('source_rgb_sha256')
    fit = row.get('body_candidate_fit_evidence')
    if not isinstance(digest, str) or len(digest) != 64 or not isinstance(fit, dict) or fit.get('source_rgb_sha256') != digest:
        return False
    candidates = row.get('candidates')
    if not isinstance(candidates, list) or not candidates:
        return False
    for candidate in candidates:
        try:
            if cell_to_slot(candidate) != candidate['cell_index']:
                return False
        except (ValueError, KeyError, TypeError):
            return False
        if candidate.get('candidate_only') is True:
            evidence = candidate.get('projection_evidence')
            if (not isinstance(evidence, dict) or evidence.get('source_rgb_sha256') != digest
                    or evidence.get('actual_fit_supported') is not True):
                return False
            rms, count = evidence.get('visible_fit_rms_px'), evidence.get('inlier_count')
            if (isinstance(rms, bool) or not isinstance(rms, (int, float)) or not math.isfinite(rms)
                    or rms < 0 or type(count) is not int or count <= 0
                    or not isinstance(evidence.get('actual_matched_data'), list) or not evidence['actual_matched_data']):
                return False
    return True


def _measured_segment_path(first, second, context, epoch, group_blocks, block_bounds, *, direct_owner_seed=False):
    """Validate actual NN transitions; body-only seeds stay in their view block."""
    if not _valid_row(first, epoch, context) or not _valid_row(second, epoch, context):
        return None
    track_id = first.get('track_id')
    if type(track_id) is not int or track_id != second.get('track_id') or first.get('kind') != second.get('kind'):
        return None
    block = group_blocks.get(first.get('group'))
    other_block = group_blocks.get(second.get('group'))
    if block is None or other_block is None or (block != other_block and not direct_owner_seed):
        return None
    akey, bkey = source_key(first['source']), source_key(second['source'])
    if (akey == bkey or not block_bounds[block][0] <= akey[3] <= block_bounds[block][1]
            or not block_bounds[other_block][0] <= bkey[3] <= block_bounds[other_block][1]):
        return None
    witnesses = {}
    for row in (first, second):
        tracking = row.get('tracking_evidence') or {}
        if (tracking.get('reason') != 'measured_consecutive_vertical_trajectory'
                or tracking.get('track_id') != track_id):
            return None
        for witness in tracking.get('trajectory', ()):
            try:
                key = source_key(witness['source'])
            except (ValueError, KeyError, TypeError):
                return None
            if key[:2] != context or witness.get('vertical_verified') is not True:
                return None
            if key in witnesses and witnesses[key] != witness:
                return None
            witnesses[key] = witness
    if akey not in witnesses or bkey not in witnesses:
        return None
    for row, key in ((first, akey), (second, bkey)):
        packet = row.get('target_packet') or {}
        for field, shape in (('point', (2,)), ('box', (4,))):
            actual, expected = np.asarray(witnesses[key].get(field), float), np.asarray(packet.get(field), float)
            if actual.shape != shape or expected.shape != shape or not np.isfinite(actual).all() or not np.allclose(actual, expected, atol=1e-6):
                return None
    low, high = sorted((akey[3], bkey[3]))
    path = [(key, witness) for key, witness in witnesses.items() if low <= key[3] <= high]
    path.sort(key=lambda item: item[0][3])
    if len(path) < 2:
        return None
    for index, (key, witness) in enumerate(path):
        matches = witness.get('match_candidates', ())
        if len(matches) > 1 or any(len(match) != 2 or match[1] != track_id for match in matches):
            return None
        if index == 0:
            continue
        previous_key, previous = path[index-1]
        if key[2] <= previous_key[2] or not 0 < key[3]-previous_key[3] <= 4.:
            return None
        point, old_point = np.asarray(witness.get('point'), float), np.asarray(previous.get('point'), float)
        box, old_box = np.asarray(witness.get('box'), float), np.asarray(previous.get('box'), float)
        if point.shape != (2,) or old_point.shape != (2,) or box.shape != (4,) or old_box.shape != (4,):
            return None
        if not all(np.isfinite(value).all() for value in (point, old_point, box, old_box)):
            return None
        delta = point-old_point
        if (abs(delta[0]) > max(40., max(box[2], old_box[2], 25)*.9)
                or abs(delta[1]) > max(120., max(box[3], old_box[3], 25)*3.)
                or min(box[2:]) <= 0 or min(old_box[2:]) <= 0
                or abs(math.log(box[2]*box[3]/(old_box[2]*old_box[3]))) > 1.8):
            return None
    return [deepcopy(witness) for _, witness in path]


def _candidate_allows_owner(row, slot):
    """Trajectory identity cannot overwrite contradictory geometric candidates."""
    candidates = row.get('candidates') or []
    return not candidates or any(candidate.get('cell_index') == slot for candidate in candidates)


def resolve_candidate_closure(views, raw_associations, fused_targets, *, scan_epoch):
    """Return explained aliases without adding any positive ownership witness.

    Caller separately validates that view groups are actual distinct poses.
    The output's effective associations are for readiness/reporting, not another
    pass through positive-observation fusion.
    """
    raw = deepcopy(raw_associations)
    empty = dict(unresolved=[r for r in raw if _relevant(r) and r.get('cell_index') is None],
                 explained=[], effective_associations=raw, negative_evidence={}, raw_associations=deepcopy(raw),
                 adds_positive_evidence=False)
    if not isinstance(scan_epoch, str) or not scan_epoch:
        return dict(empty, reason='invalid_closure_scan_epoch')
    contexts = set()
    for view in views:
        try:
            contexts.add(source_key(view['source'])[:2])
        except (ValueError, KeyError, TypeError):
            return dict(empty, reason='invalid_closure_view_source')
    if len(contexts) != 1:
        return dict(empty, reason='mixed_closure_view_source')
    context = next(iter(contexts))
    group_blocks, block_bounds, invalid_groups = {}, {}, set()
    for view in views:
        group, block = view.get('group'), view.get('view')
        if type(group) is not int or type(block) is not int or block not in (1, 2, 3, 4):
            continue
        # Production route encodes block in groups100/101/...400/401. Explicit
        # view identity remains useful for schema-independent pure fixtures.
        if group >= 100 and group//100 != block:
            invalid_groups.add(group)
            continue
        if group in group_blocks and group_blocks[group] != block:
            invalid_groups.add(group)
            continue
        group_blocks[group] = block
        timestamp = source_key(view['source'])[3]
        prior = block_bounds.get(block, (timestamp, timestamp))
        block_bounds[block] = (min(prior[0], timestamp), max(prior[1], timestamp))
    for group in invalid_groups:
        group_blocks.pop(group, None)
    primary, observation_metadata = {}, {}
    for view in views:
        key = source_key(view['source'])
        group = view.get('group')
        if type(group) is not int or group < 0 or view.get('target_detection_complete') is not True:
            continue
        direct = set()
        for cell in view.get('cells', ()):
            if cell.get('candidate_only') is True or cell.get('actually_observed') is False:
                continue
            quad = np.asarray(cell.get('quad'), float)
            if quad.shape != (4, 2) or not np.isfinite(quad).all():
                continue
            try:
                direct.add(cell_to_slot(cell))
            except (ValueError, KeyError, TypeError):
                continue
        for node in view.get('nodes', ()):
            try:
                if (node.get('scan_epoch') != scan_epoch or node.get('view_group') != group
                        or node.get('stable') is not True or source_key(node['source']) != key
                        or node.get('candidate_only') is True):
                    continue
                slot = cell_to_slot(node)
            except (ValueError, KeyError, TypeError):
                continue
            if slot in direct:
                primary.setdefault((key, group), set()).add(slot)
                observation_metadata[(key, group)] = dict(source=deepcopy(view['source']),
                    frame_id=view['source']['frame_id'], group=group, scan_epoch=scan_epoch,
                    target_detection_complete=True, evidence_type='actual_direct_visible_negative')
    blocked = {}
    for row in raw:
        if not _relevant(row) or not _valid_row(row, scan_epoch, context):
            continue
        key = (source_key(row['source']), row['group'])
        for candidate in row.get('candidates', ()):
            if candidate.get('candidate_only') is not True:
                try:
                    if cell_to_slot(candidate) == candidate['cell_index']:
                        blocked.setdefault(key, set()).add(candidate['cell_index'])
                except (ValueError, KeyError, TypeError):
                    pass
    negatives = {}
    for key, slots in primary.items():
        for slot in slots-blocked.get(key, set()):
            record = negatives.setdefault(slot, dict(groups=set(), evidence=[]))
            record['groups'].add(key[1])
            record['evidence'].append(deepcopy(observation_metadata[key]))
    negative_evidence = {slot:dict(group_count=len(value['groups']), groups=sorted(value['groups']),
        evidence=value['evidence'], actual_direct_only=True, projected_cells_count=0)
        for slot, value in negatives.items()}
    owners = []
    fused = fused_targets if isinstance(fused_targets, dict) else dict(targets=fused_targets)
    for owner in fused.get('targets', ()):
        groups = _owner_groups(owner, scan_epoch, context, primary)
        if len(groups) >= 2:
            owners.append((owner, groups))
    tracker_explained = {}
    for row in fused.get('identity_explained_candidates', ()):
        try:
            tracker_explained[_identity(row)] = row
        except (ValueError, KeyError, TypeError):
            continue
    unresolved, explained, effective = [], [], []
    for original in raw:
        row = deepcopy(original)
        if not _relevant(row) or row.get('cell_index') is not None:
            effective.append(row)
            continue
        chosen = None
        proof = None
        if _valid_row(row, scan_epoch, context):
            tracked = tracker_explained.get(_identity(row))
            if tracked is not None:
                matches = [(o, g) for o, g in owners if o['kind'] == row.get('kind')
                    and o['cell_index'] == tracked.get('cell_index')
                    and _candidate_allows_owner(row, o['cell_index'])
                    and type(row.get('track_id')) is int and row['track_id'] in o.get('track_ids', ())]
                direct_paths = []
                if len(matches) == 1:
                    owner, _ = matches[0]
                    for direct in raw:
                        if direct.get('kind') == owner['kind'] and direct.get('cell_index') == owner['cell_index']:
                            path = _measured_segment_path(row, direct, context, scan_epoch, group_blocks, block_bounds,
                                                          direct_owner_seed=True)
                            if path is not None:
                                direct_paths.append((direct, path))
                if len(matches) == 1 and direct_paths:
                    chosen, owner_groups = matches[0]
                    anchor, path = min(direct_paths, key=lambda pair: abs(source_key(pair[0]['source'])[3]-source_key(row['source'])[3]))
                    proof = dict(method='measured_track_identity_with_direct_owner',
                        actual_owner_groups=sorted(owner_groups), actual_segment_path=path,
                        anchor_source=deepcopy(anchor['source']), max_transition_gap_sec=4.,
                        standard_view_block=group_blocks[row['group']],
                        direct_owner_seed=True, permits_cross_block_actual_trajectory=True,
                        actual_transition_count=len(path)-1)
            if chosen is None and _candidate_evidence_supported(row, context, scan_epoch):
                slots = {c['cell_index'] for c in row['candidates']}
                matches = [(o, g) for o, g in owners if o['kind'] == row.get('kind') and o['cell_index'] in slots]
                if len(matches) == 1:
                    owner, owner_groups = matches[0]
                    aliases = slots-{owner['cell_index']}
                    if all(negative_evidence.get(slot, {}).get('group_count', 0) >= 3 for slot in aliases):
                        chosen = owner
                        proof = dict(method='identity_explained_body_candidate',
                            candidate_slots=sorted(slots), actual_owner_groups=sorted(owner_groups),
                            excluded_aliases={slot:deepcopy(negative_evidence[slot]) for slot in aliases},
                            body_fit_source=deepcopy(row['body_candidate_fit_source']),
                            body_fit_evidence=deepcopy(row['body_candidate_fit_evidence']))
        if chosen is None:
            unresolved.append(deepcopy(row)); effective.append(row)
        else:
            row.update(cell_index=chosen['cell_index'], cell=deepcopy(chosen['cell']),
                ownership_status=proof['method'], adds_ownership_group=False,
                closure_evidence=proof)
            explained.append(deepcopy(row)); effective.append(row)
    # An animation frame can lack foot/ring pixels while the NN observes the
    # same continuous segment on nearby frames. Seeds must already have actual
    # direct ownership or body-alias closure; newly propagated explanations are
    # not seeds, avoiding circular identity propagation. Body-only seeds stay
    # within a view block. A direct observed owner can prove intact trajectories
    # across view blocks, without bridging a missing or ambiguous observation.
    seeds = [row for row in effective if row.get('cell_index') is not None and
             (row.get('ownership_status') == 'identity_explained_body_candidate'
              or any(row.get('kind') == owner['kind'] and row['cell_index'] == owner['cell_index']
                     and row.get('adds_ownership_group') is not False for owner, _ in owners))]
    pending = []
    for row in unresolved:
        matches = []
        for seed in seeds:
            if not _candidate_allows_owner(row, seed['cell_index']):
                continue
            direct_seed = (seed.get('ownership_status') != 'identity_explained_body_candidate'
                           and seed.get('adds_ownership_group') is not False)
            path = _measured_segment_path(row, seed, context, scan_epoch, group_blocks, block_bounds,
                                          direct_owner_seed=direct_seed)
            if path is not None:
                matches.append((seed, path))
        slots = {seed['cell_index'] for seed, _ in matches}
        if len(slots) != 1:
            pending.append(row)
            continue
        slot = next(iter(slots))
        confirmed = [(owner, groups) for owner, groups in owners if owner['kind'] == row.get('kind') and owner['cell_index'] == slot]
        if len(confirmed) != 1:
            pending.append(row)
            continue
        owner, groups = confirmed[0]
        seed, path = min(matches, key=lambda pair: abs(source_key(pair[0]['source'])[3]-source_key(row['source'])[3]))
        resolved = deepcopy(row)
        resolved.update(cell_index=slot, cell=deepcopy(owner['cell']),
            ownership_status='identity_explained_measured_segment_neighbor', adds_ownership_group=False,
            closure_evidence=dict(method='identity_explained_measured_segment_neighbor',
                actual_owner_groups=sorted(groups), anchor_source=deepcopy(seed['source']),
                anchor_closure_evidence=deepcopy(seed.get('closure_evidence')),
                actual_segment_path=path, max_transition_gap_sec=4.,
                standard_view_block=group_blocks[row['group']], actual_transition_count=len(path)-1,
                direct_owner_seed=(seed.get('ownership_status') != 'identity_explained_body_candidate'
                                   and seed.get('adds_ownership_group') is not False),
                endpoint_gap_sec=abs(source_key(row['source'])[3]-source_key(seed['source'])[3]),
                track_id=row['track_id']))
        explained.append(deepcopy(resolved))
        identity = _identity(row)
        effective = [deepcopy(resolved) if _identity(existing) == identity else existing for existing in effective]
    unresolved = pending
    return dict(unresolved=unresolved, explained=explained, effective_associations=effective,
                negative_evidence=negative_evidence, raw_associations=raw,
                adds_positive_evidence=False, reason='actual_candidate_alias_closure')
