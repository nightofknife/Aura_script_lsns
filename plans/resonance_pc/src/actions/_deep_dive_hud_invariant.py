"""Same-source exact HUD evidence, independent of entity/action permission.

Initialize using two bound, fresh, complete visual HUD reads. Each changed ROI
requires complete current native values for its own fields; unchanged ROIs use
exact current pixels. This module does not execute OCR/capture/input or voting.
"""
from __future__ import annotations

from copy import deepcopy
import hashlib
import math
import time

import numpy as np

from . import _deep_dive_planned_run_vision as vision
from . import _deep_dive_hud_templates as templates

FIELDS = ('plane_index', 'rounds_remaining', 'moves_used', 'moves_total',
          'rotations_used', 'rotations_total', 'collected_count', 'inspiration_total')
REGIONS = dict(plane=vision.REGIONS['plane_1'], rounds=vision.HUD_REGIONS['rounds'], **templates.REGIONS)
REGION_FIELDS = dict(plane=('plane_index',), rounds=('rounds_remaining',), **templates.FIELDS)


def _image(rgb):
    if not isinstance(rgb, np.ndarray) or rgb.shape != (720, 1280, 3) or rgb.dtype != np.uint8:
        raise ValueError('hud_invariant_invalid_rgb')
    return rgb


def _digest(rgb):
    return hashlib.sha256(rgb.tobytes()).hexdigest()


def _source(source, now):
    if not isinstance(source, dict):
        raise ValueError('hud_invariant_source_missing')
    session = source.get('session_id')
    generation = source.get('generation')
    captured = source.get('frame_time')
    backend = str(source.get('capture_backend', '')).lower()
    if (not isinstance(session, (str, int)) or isinstance(session, bool) or session == ''
            or type(generation) is not int or generation < 0
            or isinstance(captured, bool) or not isinstance(captured, (int, float))
            or not math.isfinite(captured)):
        raise ValueError('hud_invariant_source_invalid')
    if backend not in {'wgc', 'windows_graphics_capture', 'windowsgraphicscapture'}:
        raise ValueError('hud_invariant_requires_wgc')
    age = now-float(captured)
    if not 0 <= age <= .5:
        raise ValueError('hud_invariant_source_stale')
    return dict(session_id=session, generation=generation, frame_time=float(captured), capture_backend=backend)


def _board(observation):
    return (isinstance(observation, dict) and observation.get('valid') is True
            and observation.get('scene') == 'board' and observation.get('player_turn') is True
            and not observation.get('enemy_turn') and not observation.get('unknown_modal_evidence'))


def _values(hud):
    if not isinstance(hud, dict) or hud.get('status') != 'complete':
        raise ValueError('hud_invariant_read_incomplete')
    values = {key: hud.get(key) for key in FIELDS}
    if any(type(value) is not int for value in values.values()):
        raise ValueError('hud_invariant_read_incomplete')
    if not (1 <= values['plane_index'] <= 3 and 0 <= values['rounds_remaining'] <= 30):
        raise ValueError('hud_invariant_read_out_of_range')
    # No new quota allowance: keep the current game's one-action contract.
    if (values['moves_total'] != 1 or values['rotations_total'] != 1
            or not 0 <= values['moves_used'] <= 1 or not 0 <= values['rotations_used'] <= 1
            or not 0 <= values['collected_count'] <= values['inspiration_total'] <= 20):
        raise ValueError('hud_invariant_read_out_of_range')
    return values


def _read_changed_native(rgb, observation, regions):
    """Decode only changed current ROIs with the original native components."""
    result = dict(evidence_source={}, region_evidence={}, decoded_regions=[])
    for name in REGIONS:
        if name not in regions:
            continue
        result['decoded_regions'].append(name)
        if name == 'plane':
            evidence = observation.get('plane_evidence', {})
            result['plane_index'] = evidence.get('plane_index')
            result['evidence_source']['plane_index'] = 'native_plane_icon'
        elif name == 'rounds':
            evidence = vision.read_rounds_template(rgb)
            result['rounds_remaining'] = evidence.get('value')
            result['evidence_source']['rounds_remaining'] = 'native_round_glyph_templates'
        else:
            pair, evidence = templates._read_pair(rgb, name)
            fields = REGION_FIELDS[name]
            for index, field in enumerate(fields):
                result[field] = None if pair is None else pair[index]
                result['evidence_source'][field] = 'native_font_pair_templates'
        result['region_evidence'][name] = evidence
    return result


class HudInvariant:
    """One bounded baseline, two-source initialization, strict source advance."""
    def __init__(self):
        self.clear()

    def clear(self):
        self._baseline = None
        self._rois = {}
        self._context = None
        self._last_source = None
        self._initial_sources = []
        self._last_pixel_source = None

    @staticmethod
    def _reply(reason, **kwargs):
        return dict(schema='resonance_pc.deep_dive_hud_invariant.v1', ready=False,
                    unchanged=False, input_authorized=False, reason=reason, **kwargs)

    def _advance(self, source):
        if self._last_source is not None and (
                source['session_id'] != self._last_source['session_id']
                or source['generation'] <= self._last_source['generation']
                or source['frame_time'] <= self._last_source['frame_time']):
            raise ValueError('hud_invariant_source_not_advanced')
        self._last_source = deepcopy(source)

    def _store_rois(self, rgb, source):
        self._rois = {name: rgb[y1:y2, x1:x2].copy() for name, (x1,y1,x2,y2) in REGIONS.items()}
        self._last_pixel_source = deepcopy(source)

    def add_baseline(self, rgb, source, observation, full_hud, *, map_revision=0, now=None):
        """full_hud must carry _source and _rgb_digest from its actual read RGB."""
        current_now = time.monotonic() if now is None else float(now)
        try:
            _image(rgb); source = _source(source, current_now)
            if type(map_revision) is not int or map_revision < 0:
                raise ValueError('hud_invariant_map_revision_invalid')
            if not _board(observation):
                raise ValueError('hud_invariant_player_board_unconfirmed')
            values = _values(full_hud)
            binding = _source(full_hud.get('_source'), current_now)
            if binding != source or full_hud.get('_rgb_digest') != _digest(rgb):
                raise ValueError('hud_invariant_read_source_mismatch')
            context = (source['session_id'], map_revision)
            if self._context is not None and context != self._context:
                self.clear()
            self._context = context
            self._advance(source)
            if self._baseline is not None and values != self._baseline:
                self._initial_sources = []
            self._baseline = deepcopy(values)
            self._initial_sources.append(deepcopy(source))
            self._initial_sources = self._initial_sources[-2:]
            self._store_rois(rgb, source)
            # The full read might have consumed source freshness before this call.
            _source(source, time.monotonic() if now is None else float(now))
            if len(self._initial_sources) < 2:
                return self._reply('hud_invariant_wait_second_fresh_read', baseline_reads=1)
            return dict(schema='resonance_pc.deep_dive_hud_invariant.v1', ready=True,
                        unchanged=True, input_authorized=False, reason='hud_invariant_baseline_ready',
                        hud=deepcopy(values), baseline_reads=2, sources=deepcopy(self._initial_sources))
        except (ValueError, TypeError, KeyError) as exc:
            self.clear()
            return self._reply(str(exc))

    def check(self, rgb, source, observation, *, map_revision=0, now=None):
        current_now = time.monotonic() if now is None else float(now)
        try:
            _image(rgb); source = _source(source, current_now)
            if len(self._initial_sources) != 2:
                raise ValueError('hud_invariant_baseline_not_ready')
            if type(map_revision) is not int or (source['session_id'], map_revision) != self._context:
                raise ValueError('hud_invariant_context_changed')
            if not _board(observation):
                raise ValueError('hud_invariant_player_board_unconfirmed')
            self._advance(source)
            equal = {name: np.array_equal(rgb[y1:y2,x1:x2], self._rois[name])
                     for name,(x1,y1,x2,y2) in REGIONS.items()}
            pixel_source = deepcopy(self._last_pixel_source)
            native = None
            values = deepcopy(self._baseline)
            if not all(equal.values()):
                # Every field gets a current-frame proof: changed ROI native
                # values or unchanged ROI byte-for-byte pixel equivalence. An
                # unchanged yellow quota does not need speculative re-decoding.
                native = _read_changed_native(rgb, observation,
                    {name for name, unchanged in equal.items() if not unchanged})
                if not isinstance(native, dict):
                    raise ValueError('hud_invariant_read_incomplete')
                for name, fields in REGION_FIELDS.items():
                    if equal[name]:
                        continue
                    for field in fields:
                        candidate = native.get(field)
                        if candidate is not None:
                            if type(candidate) is not int:
                                raise ValueError('hud_invariant_read_incomplete')
                            if candidate != self._baseline[field]:
                                return self._reply('hud_invariant_state_changed',
                                    contradictory_field=field, current_value=candidate,
                                    current_source=source, roi_equal=equal)
                        if type(candidate) is not int:
                            raise ValueError('hud_invariant_changed_roi_unknown:'+name)
                        values[field] = candidate
                values = _values(dict(values, status='complete'))
            current_now = time.monotonic() if now is None else float(now)
            _source(source, current_now)
            digest = _digest(rgb)
            field_sources = {}
            for name,fields in REGION_FIELDS.items():
                for field in fields:
                    field_sources[field] = dict(source='exact_current_roi' if equal[name] else 'current_native_roi_read',
                        region=list(REGIONS[name]), current_source=deepcopy(source),
                        baseline_pixel_source=deepcopy(pixel_source) if equal[name] else None,
                        native_source=None if native is None or equal[name] else native.get('evidence_source',{}).get(field))
            proof = dict(current_source=deepcopy(source), source_rgb_digest=digest,
                map_revision=map_revision, roi_equal=equal, all_fields=list(FIELDS),
                spatial_tolerance=0, roi_digests={name: _digest(rgb[y1:y2,x1:x2])
                                              for name,(x1,y1,x2,y2) in REGIONS.items()},
                baseline_sources=deepcopy(self._initial_sources),
                native_regions_decoded=[] if native is None else list(native['decoded_regions']),
                native_region_evidence={} if native is None else deepcopy(native['region_evidence']))
            current_now = time.monotonic() if now is None else float(now)
            _source(source, current_now)
            proof['source_age_sec'] = current_now-source['frame_time']
            # Complete current field proofs permit refreshing pixel cache only;
            # it never changes the expected HUD or its original initialization.
            self._store_rois(rgb, source)
            return dict(schema='resonance_pc.deep_dive_hud_invariant.v1', ready=True,
                        unchanged=True, input_authorized=False, reason='hud_invariant_unchanged',
                        hud=deepcopy(values), field_sources=field_sources, proof=proof)
        except Exception as exc:
            return self._reply(str(exc), source_age_sec=(current_now-float(source.get('frame_time',current_now))
                               if isinstance(source,dict) and isinstance(source.get('frame_time'),(int,float)) else None))
