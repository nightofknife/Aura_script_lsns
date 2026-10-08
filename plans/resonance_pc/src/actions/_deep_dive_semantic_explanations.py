"""Same-packet HUD proof for read-only current back-face explanations.

One semantic owner calls prepare before inference and apply after glyph/model
fusion. No shared scene-thread guard, copied pre/post HUD, or positive votes.
"""
from copy import deepcopy
import hashlib
import math
import time

from . import _deep_dive_planned_run_vision as vision
from ._deep_dive_hud_invariant import HudInvariant
from ._deep_dive_established_target_explainer import (
    explain_established_targets, apply_current_explanations)


_SOURCE_KEYS = ('frame_id', 'frame_time', 'session_id', 'generation', 'map_revision')


def _packet_source(packet):
    source = {key: packet.get(key) for key in _SOURCE_KEYS}
    source['capture_backend'] = packet.get('capture_backend')
    session = source['session_id']
    if (source['capture_backend'] != 'wgc'
            or any(type(source[key]) is not int or source[key] < 0
                   for key in ('frame_id', 'generation', 'map_revision'))
            or not (type(session) is int and session >= 0
                    or isinstance(session, str) and bool(session.strip()))
            or type(source['frame_time']) not in (int, float)
            or not math.isfinite(source['frame_time'])):
        raise ValueError('semantic_explanation_atomic_source_invalid')
    return source


def _same_source(first, second):
    return (isinstance(first, dict) and all(first.get(key) == second[key] for key in _SOURCE_KEYS)
            and first.get('capture_backend', first.get('backend')) == 'wgc')


def _bind_proof(proof, source):
    if not isinstance(proof, dict) or any(
            proof.get('source_'+key) != source[key] for key in ('frame_id', 'frame_time', 'map_revision')):
        raise ValueError('semantic_explanation_observation_source_mismatch')
    if any('source_'+key in proof and proof['source_'+key] != source[key]
           for key in ('session_id', 'generation')):
        raise ValueError('semantic_explanation_observation_identity_mismatch')
    # The three source fields already match the exact atomic packet. Preserve
    # them and complete its capture identity, never use a newer scene source.
    return dict(proof, **{'source_'+key: source[key] for key in _SOURCE_KEYS})


class SemanticTargetExplainer:
    """Guard and prepared proof are confined to one semantic owner."""
    def __init__(self):
        self._hud = HudInvariant()
        self._context = None
        self._baseline_ready = False
        self._prepared = None

    def prepare(self, packet):
        """Read native HUD from this RGB, then establish/advance its proof."""
        self._prepared = None
        try:
            source = _packet_source(packet)
            rgb, scene = packet['image'], packet['scene_observation']
            context = (source['session_id'], source['map_revision'])
            if context != self._context:
                self._hud.clear()
                self._baseline_ready = False
                self._context = context
            if not 0 <= time.monotonic()-source['frame_time'] <= .5:
                raise ValueError('semantic_explanation_hud_source_stale')
            if not self._baseline_ready:
                native = vision.read_hud(rgb, None, observation=scene)
                native = dict(native, _source=deepcopy(source),
                              _rgb_digest=hashlib.sha256(rgb.tobytes()).hexdigest())
                baseline = self._hud.add_baseline(rgb, source, scene, native,
                    map_revision=source['map_revision'])
                self._baseline_ready = baseline.get('ready') is True
                return dict(ready=False, reason=('semantic_explanation_wait_next_source'
                    if self._baseline_ready else baseline['reason']), hud_guard=baseline)
            guarded = self._hud.check(rgb, source, scene, map_revision=source['map_revision'])
            if guarded.get('ready') is not True or guarded.get('unchanged') is not True:
                return dict(ready=False, reason=guarded['reason'], hud_guard=guarded)
            self._prepared = dict(source=source, rgb_digest=hashlib.sha256(rgb.tobytes()).hexdigest(),
                                  hud_guard=deepcopy(guarded))
            return dict(ready=True, reason='semantic_explanation_hud_proven',
                        source=deepcopy(source), hud_guard=deepcopy(guarded))
        except Exception as error:
            return dict(ready=False, reason=str(error))

    def apply(self, result, observation, packet):
        """Explain current unassociated candidates; leave all occupancy intact."""
        reply = dict(status='rejected', layout=result, positive_vote=False,
                     input_authorized=False, diagnostics={})
        def reject(reason):
            reply['diagnostics']['reason'] = reason
            return reply
        try:
            associations = result.get('target_candidate_associations', [])
            if not any(candidate.get('cell_index') is None
                       and candidate.get('kind') in ('player', 'singularity', 'inspiration')
                       and candidate.get('confidence', 0.) >= .70 for candidate in associations):
                reply.update(status='skipped', diagnostics={'reason': 'no_strong_unassociated_current_candidate'})
                return reply
            source = _packet_source(packet)
            if (self._prepared is None or not _same_source(self._prepared['source'], source)
                    or hashlib.sha256(packet['image'].tobytes()).hexdigest() != self._prepared['rgb_digest']):
                return reject('semantic_explanation_same_packet_hud_unproven')
            if not _same_source(result.get('semantic_source'), source):
                return reject('semantic_explanation_result_source_mismatch')
            now = time.monotonic()
            if not 0 <= now-source['frame_time'] <= .8:
                return reject('semantic_explanation_source_stale')
            if observation.get('frame_id') != source['frame_id']:
                return reject('semantic_explanation_observation_frame_mismatch')
            refined = _bind_proof(observation.get('refine_diagnostic'), source)
            if refined.get('renewed') is not True:
                return reject('semantic_explanation_glyph_not_renewed')
            anchor = _bind_proof(observation.get('entity_association_diagnostic'), source)
            coverage = _bind_proof(observation.get('target_coverage'), source)
            guarded = self._prepared['hud_guard']
            if (guarded.get('ready') is not True or guarded.get('unchanged') is not True
                    or not _same_source(dict(guarded['proof']['current_source'],
                        frame_id=source['frame_id'], map_revision=guarded['proof']['map_revision']), source)):
                return reject('semantic_explanation_hud_binding_mismatch')
            hud = dict(valid=True, unchanged=True, scene='board',
                **{'source_'+key: source[key] for key in _SOURCE_KEYS},
                hud_guard=deepcopy(guarded), source_rgb_digest=self._prepared['rgb_digest'])
            targets = result['target_clues']
            if any(type(candidate.get('cell_index')) is not int for candidate in associations
                   if candidate.get('cell_index') is not None):
                return reject('semantic_explanation_front_index_invalid')
            front = {candidate['cell_index']: candidate for candidate in associations
                     if candidate.get('cell_index') is not None}
            explanation = explain_established_targets(result['cells'], targets,
                pose=observation['pose'], source=source, anchor_proof=anchor,
                model_coverage=coverage, front_assigned=front, hud_unchanged=True,
                hud_proof=hud, now=now, max_age_sec=.8)
            if not explanation['matches']:
                reply['diagnostics']['explanation'] = explanation
                return reject(explanation.get('reason') or 'semantic_explanation_no_safe_match')
            applied = apply_current_explanations(result, explanation)
            # Both geometric explanation and atomic apply cost real time.
            if not 0 <= time.monotonic()-source['frame_time'] <= .8:
                return reject('semantic_explanation_source_expired_during_apply')
            if applied['status'] != 'applied':
                return reject(applied['reason'])
            reply.update(status='applied', layout=applied['layout'],
                         diagnostics={'reason': 'current_backface_explained',
                                      'explanation': explanation,
                                      'applied_count': applied['applied_count']})
            return reply
        except Exception as error:
            return reject(str(error))
