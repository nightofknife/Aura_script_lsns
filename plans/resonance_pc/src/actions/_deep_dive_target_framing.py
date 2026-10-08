"""Pure navigation framing constraints; no observer, controller, or vote writes.

Inputs must come from the creation-time atomic semantic packet. This module
does not reconstruct missing historical pixels or invent a navigation budget.
"""
from copy import deepcopy
import math
from numbers import Real

import numpy as np

KINDS = ('player', 'singularity', 'inspiration')


def _number(value):
    return isinstance(value, Real) and not isinstance(value, (bool, np.bool_)) and math.isfinite(value)


def _integer(value):
    return type(value) is int and value >= 0


def _flat(value, count):
    if not isinstance(value, (list, tuple)):
        return None
    result = []
    for item in value:
        if isinstance(item, (list, tuple)):
            result.extend(item)
        else:
            result.append(item)
    if len(result) != count or not all(_number(x) for x in result):
        return None
    return tuple(float(x) for x in result)


def _rotation(value):
    if not isinstance(value, (list, tuple)) or len(value) != 3:
        return None
    if any(not isinstance(row, (list, tuple)) or len(row) != 3 for row in value):
        return None
    flat = _flat(value, 9)
    if flat is None:
        return None
    rows = [flat[i:i+3] for i in (0, 3, 6)]
    for i in range(3):
        for j in range(3):
            if abs(sum(rows[i][k]*rows[j][k] for k in range(3))-(i == j)) > 1e-5:
                return None
    a, b, c, d, e, f, g, h, i = flat
    if abs(a*(e*i-f*h)-b*(d*i-f*g)+c*(d*h-e*g)-1.) > 1e-5:
        return None
    return flat


def _same(left, right):
    return left is not None and right is not None and all(
        abs(a-b) <= 1e-6 for a, b in zip(left, right))


def _context(value):
    return (isinstance(value, (list, tuple)) and len(value) == 2
            and all(_integer(x) for x in value))


def _live_interest(interest, context, now):
    return (isinstance(interest, dict) and _context(context)
            and _context(interest.get('context'))
            and tuple(interest['context']) == tuple(context)
            and type(interest.get('index')) is int and 0 <= interest['index'] < 54
            and interest.get('kind') in KINDS
            and _integer(interest.get('source_frame_id'))
            and _number(interest.get('source_frame_time'))
            and _number(interest.get('expires')) and _number(now)
            and now < interest['expires']
            and type(interest.get('attempts')) is int and 0 <= interest['attempts'] < 2)


def freeze_framing_interest(interest, *, context, now, source_feedback,
                            source_projection, candidate=None, retained_evidence=None):
    """Copy an existing budget and its original source proof; never renew it.

    source_projection is the target quad/cosine projected using the SAME
    creation-time metadata pose, with frame/session/map/time/rotation/tvec.
    Supplying current geometry in place of that source pose is invalid.
    """
    invalid = lambda reason: dict(status='invalid', navigation_only=True, reason=reason)
    if not _live_interest(interest, context, now):
        return invalid('inactive_original_interest')
    if not isinstance(source_feedback, dict) or not isinstance(source_projection, dict):
        return invalid('missing_original_packet_or_projection')
    source = source_feedback.get('accepted_anchor_observation') or {}
    metadata = source_feedback.get('semantic_metadata') or {}
    diagnostic = source_feedback.get('refine_diagnostic') or {}
    coverage = source_feedback.get('target_coverage') or {}
    pose = metadata.get('pose') or {}
    if not all(isinstance(x, dict) for x in (source, metadata, diagnostic, coverage, pose)):
        return invalid('malformed_original_packet')
    index, kind = interest['index'], interest['kind']
    frame, stamp = interest['source_frame_id'], interest['source_frame_time']
    if (not _integer(metadata.get('generation'))
            or any(type(x.get('session_id')) is not int or x.get('session_id') != context[0]
                   for x in (source_feedback, source, metadata, source_projection))
            or any(type(x.get('map_revision')) is not int or x.get('map_revision') != context[1]
                   for x in (source_feedback, source, metadata, pose, source_projection))
            or any(type(x.get('frame_id')) is not int or x.get('frame_id') != frame
                   for x in (source, metadata, source_projection))
            or any(type(x.get('source_frame_id')) is not int or x['source_frame_id'] != frame
                   or type(x.get('source_map_revision')) is not int or x['source_map_revision'] != context[1]
                   for x in (diagnostic, coverage))):
        return invalid('original_source_identity_mismatch')
    times = [source.get('frame_time'), metadata.get('frame_time'),
             source_projection.get('frame_time'), diagnostic.get('source_frame_time'),
             coverage.get('source_frame_time'), source_feedback.get('glyph_anchor_at')]
    if not all(_number(x) and abs(x-stamp) <= 1e-6 for x in times):
        return invalid('original_source_time_mismatch')
    age = source_feedback.get('glyph_anchor_age_sec')
    faces = source.get('accepted_faces') or {}
    if (diagnostic.get('renewed') is not True or source_feedback.get('fusion_paused')
            or not _number(age) or not 0 <= age < 2.
            or coverage.get('model_executed') is not True or coverage.get('coverage_valid') is not True
            or source_feedback.get('glyph_anchor_frame_id') != frame
            or not isinstance(faces, dict) or not faces
            or any(type(x) is not int or x < 0 for x in faces.values())
            or faces != diagnostic.get('accepted_faces') or sum(faces.values()) < 6
            or sum(x >= 2 for x in faces.values()) < 2):
        return invalid('original_anchor_or_model_proof_missing')
    rotation = _rotation(pose.get('rotation'))
    translation = _flat(pose.get('tvec'), 3)
    if (rotation is None or translation is None or translation[2] <= 0
            or _rotation(source.get('body_basis')) is None
            or not _same(rotation, _rotation(source.get('rotation')))
            or not _same(rotation, _rotation(source_projection.get('rotation')))
            or not _same(translation, _flat(source_projection.get('tvec'), 3))):
        return invalid('original_pose_mismatch')
    if (candidate is None) == (retained_evidence is None):
        return invalid('require_one_original_evidence_kind')
    candidates = source_feedback.get('target_candidate_associations')
    if not isinstance(candidates, (list, tuple)):
        return invalid('missing_original_candidates')
    if retained_evidence is not None:
        entry = retained_evidence
        if (not isinstance(entry, dict) or entry.get('frame_id') != frame
                or entry.get('occupant') != kind or not _number(entry.get('confidence'))
                or not .70 <= entry['confidence'] <= 1.):
            return invalid('invalid_retained_positive')
        matches = [x for x in candidates
                   if isinstance(x, dict) and x.get('kind') == kind and x.get('cell_index') == index
                   and x.get('box') == entry.get('target_box')
                   and x.get('point') == entry.get('target_point')
                   and x.get('association_evidence') == entry.get('association_evidence')]
        if len(matches) != 1:
            return invalid('retained_positive_not_bound_to_original_packet')
        candidate = matches[0]
    else:
        if not isinstance(candidate, dict) or candidate not in candidates:
            return invalid('candidate_not_bound_to_original_packet')
    confidence = candidate.get('confidence')
    proof = candidate.get('association_evidence') or {}
    if (candidate.get('kind') != kind or type(candidate.get('cell_index')) is not int
            or candidate.get('cell_index') != index or not _number(confidence)
            or not .25 <= confidence <= 1. or not isinstance(proof, dict)):
        return invalid('invalid_original_assigned_candidate')
    # Preserve the actual association eligibility; this is still only a hint.
    certificate = proof.get('anchor_uncertainty') or {}
    face = source_projection.get('face')
    if (type(proof.get('source_frame_id')) is not int or proof['source_frame_id'] != frame
            or type(proof.get('source_map_revision')) is not int or proof['source_map_revision'] != context[1]
            or not _number(proof.get('source_frame_time')) or abs(proof['source_frame_time']-stamp) > 1e-6
            or not _same(rotation, _rotation(proof.get('rotation')))
            or not _same(translation, _flat(proof.get('tvec'), 3))
            or type(proof.get('target_face_glyph_count')) is not int or proof['target_face_glyph_count'] < 2
            or face != 'URFDLB'[index//9] or faces.get(face, 0) < 2
            or not isinstance(certificate, dict) or certificate.get('ready') is not True
            or type(certificate.get('winner_index')) is not int or certificate['winner_index'] != index
            or not _number(proof.get('error')) or not 0 <= proof['error'] < .30
            or not _number(proof.get('margin')) or proof['margin'] <= .12):
        return invalid('original_association_proof_missing')
    box, point, quad = (_flat(candidate.get('box'), 4), _flat(candidate.get('point'), 2),
                        _flat(source_projection.get('quad'), 8))
    raw_quad = source_projection.get('quad')
    if (box is None or point is None or quad is None or min(box[:2]) < 0
            or min(box[2:]) <= 0 or box[0]+box[2] > 1280 or box[1]+box[3] > 720
            or not isinstance(raw_quad, (list, tuple)) or len(raw_quad) != 4
            or any(not isinstance(row, (list, tuple)) or len(row) != 2 for row in raw_quad)
            or type(source_projection.get('cell_index')) is not int or source_projection['cell_index'] != index
            or not _number(source_projection.get('cosine'))
            or not 0 <= source_projection['cosine'] <= 1.):
        return invalid('missing_original_box_or_target_quad')
    # Store only the fields used to validate this source. The full live
    # controller feedback can be large; no unrelated history is needed.
    packet_fields = ('session_id', 'map_revision', 'accepted_anchor_observation',
                     'semantic_metadata', 'refine_diagnostic', 'target_coverage',
                     'glyph_anchor_at', 'glyph_anchor_frame_id', 'glyph_anchor_age_sec',
                     'fusion_paused')
    packet = {key: deepcopy(source_feedback[key]) for key in packet_fields if key in source_feedback}
    packet['target_candidate_associations'] = [deepcopy(candidate)]
    return dict(status='valid', navigation_only=True, reason='original_proof_copied',
                interest=deepcopy(interest), source_feedback=packet,
                source_projection=deepcopy(source_projection), candidate=deepcopy(candidate),
                retained_evidence=deepcopy(retained_evidence))


def select_target_framing_pair(pair_options, *, readable, endpoint_valid,
                               face_indices, interests, current_budgets, context, now):
    """Rank only caller's original finite, supported page pair options.

    readable must be the ORIGINAL policy's result on private planning rows.
    Each option has left/right, eligible=True, and original utility; caller
    keeps original angular family/path predicates, attempts and waterline.
    """
    partial = lambda reason, **extra: dict(status='partial', navigation_only=True,
        reason=reason, **extra)
    if not _context(context) or not _number(now):
        return partial('invalid_current_context')
    if (not isinstance(face_indices, (list, tuple)) or len(face_indices) != 9
            or len(set(face_indices)) != 9
            or any(type(i) is not int or not 0 <= i < 54 for i in face_indices)):
        return partial('invalid_current_face')
    try:
        # Validate the original native arrays once, without converting the
        # complete endpoint-by-cell matrix to thousands of Python scalars.
        rows, valid = np.asarray(readable), np.asarray(endpoint_valid)
        if (rows.ndim != 2 or rows.shape[1] != 54 or not len(rows)
                or valid.shape != (len(rows),) or valid.dtype.kind != 'b'
                or rows.dtype.kind not in 'fiu' or not np.isfinite(rows).all()):
            return partial('invalid_original_readability')
    except (TypeError, ValueError):
        return partial('invalid_original_readability')
    if not isinstance(current_budgets, dict):
        return partial('missing_authoritative_current_budget')
    active = []
    for record in interests:
        if not isinstance(record, dict) or record.get('status') != 'valid':
            return partial('unvalidated_framing_interest')
        original = record.get('interest')
        if not isinstance(original, dict):
            return partial('missing_original_budget')
        candidate, projection, packet = (record.get('candidate'), record.get('source_projection'),
                                         record.get('source_feedback'))
        proof = candidate.get('association_evidence') if isinstance(candidate, dict) else None
        if (not isinstance(projection, dict) or not isinstance(packet, dict) or not isinstance(proof, dict)
                or candidate.get('kind') != original.get('kind')
                or candidate.get('cell_index') != original.get('index')
                or projection.get('cell_index') != original.get('index')
                or proof.get('source_frame_id') != original.get('source_frame_id')
                or proof.get('source_frame_time') != original.get('source_frame_time')):
            return partial('missing_or_rewritten_frozen_proof')
        interest = current_budgets.get(original.get('index'))
        if interest is None:
            continue
        immutable = ('index', 'kind', 'source_frame_id', 'source_frame_time', 'context', 'expires')
        if (not isinstance(interest, dict) or any(interest.get(k) != original.get(k) for k in immutable)
                or type(interest.get('attempts')) is not int
                or type(original.get('attempts')) is not int
                or interest['attempts'] < original['attempts']):
            return partial('original_budget_rewritten')
        # Expired/foreign contexts cannot impose a new target or restart TTL.
        if not _live_interest(interest, context, now):
            continue
        if interest['index'] in face_indices:
            active.append(interest['index'])
    targets = sorted(set(active))
    best = None
    if not isinstance(pair_options, (list, tuple)):
        return partial('invalid_original_pairs')
    for ordinal, option in enumerate(pair_options):
        if not isinstance(option, dict) or option.get('eligible') is not True:
            continue
        left, right = option.get('left'), option.get('right')
        if (type(left) is not int or type(right) is not int or left == right
                or not 0 <= left < len(rows) or not 0 <= right < len(rows)
                or not valid[left] or not valid[right] or not _number(option.get('utility'))):
            continue
        if any(rows[left][i] <= 0 or rows[right][i] <= 0 for i in targets):
            continue
        union = int(np.count_nonzero((rows[left, face_indices] > 0) | (rows[right, face_indices] > 0)))
        rank = (union == 9, union, option['utility'])
        if best is None or rank > best[0]:
            best = rank, ordinal, union, option
    if best is None:
        return partial('no_target_readable_pair' if targets else 'no_original_eligible_pair',
                       target_indices=targets, fallback_required=True)
    _, ordinal, union, option = best
    return dict(status='selected', navigation_only=True, reason='finite_original_pair',
                option=deepcopy(option), option_ordinal=ordinal, union_cells=union,
                target_indices=targets, full_union=union == 9)
