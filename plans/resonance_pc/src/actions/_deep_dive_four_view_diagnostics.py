"""Best-effort observations of the existing four-view control loop.

This recorder never captures pixels, evaluates a controller, or authorizes input.
Image displacement and image-angle rates are diagnostics, not proof of settling.
"""
from __future__ import annotations

from collections import Counter
from datetime import datetime, timezone
import hashlib
import json
import math
from pathlib import Path
import time
import traceback

import numpy as np

from packages.aura_core.observability.logging.core_logger import current_cid, logger


def _json_value(value):
    if isinstance(value, np.ndarray):
        return _json_value(value.tolist())
    if isinstance(value, np.generic):
        return _json_value(value.item())
    if isinstance(value, dict):
        return {str(k): _json_value(v) for k, v in value.items()}
    if isinstance(value, (list, tuple)):
        return [_json_value(v) for v in value]
    if isinstance(value, float) and not math.isfinite(value):
        return None
    if value is None or isinstance(value, (str, int, float, bool)):
        return value
    return str(value)


def input_settings(app):
    """Read configuration only; never call the bridge's stateful enabled getter."""
    try:
        config = getattr(getattr(app, 'target_runtime', None), 'config', None)
        observed = getattr(getattr(getattr(app, 'controller', None), 'bridge', None), '_trace_mode', None)
        return dict(configured_input_mode=config.get('resonance_pc.input.mode', 'system') if config else None,
                    configured_stop_inertia=config.get('resonance_pc.input.stop_inertia', True) if config else None,
                    observed_bridge_state=_json_value(observed))
    except Exception as exc:
        return dict(configuration_read_error=f'{type(exc).__name__}: {exc}')


class FourViewDiagnostics:
    """Append small records; a diagnostic failure cannot stop the game task."""

    def __init__(self, output, *, run_id, scan_epoch, started, settings):
        self.output = Path(output)
        self.started = started
        self.identity = dict(schema='resonance_pc.deep_dive_four_view_diagnostics.v1',
                             run_id=run_id, scan_epoch=scan_epoch, cid=str(current_cid() or ''))
        self.sequence = 0
        self.counts = Counter()
        self.write_elapsed_sec = 0.
        self.errors = []
        self.trace_disabled = False
        self.previous_fits = {}
        self.metadata = dict(self.identity, started_utc=datetime.now(timezone.utc).isoformat(),
                             settings=settings, source_files={}, status='running',
                             trace_path=str(self.output/'control_trace.jsonl'),
                             extra_capture_count=0, used_for_control=False,
                             angle_unit='image_error_degrees_not_physical_rotation')
        try:
            root = Path(__file__).resolve().parent
            paths = [Path(__file__), root/'_deep_dive_four_view_scan.py',
                     root/'_deep_dive_four_view_geometry.py',
                     root/'_deep_dive_four_view_epoch_registration.py',
                     root/'_deep_dive_four_view_reader.py', root/'_deep_dive_layout_semantics.py',
                     root.parents[1]/'data'/'four_views_geometry.json']
            self.metadata['source_files'] = {p.name: hashlib.sha256(p.read_bytes()).hexdigest() for p in paths}
        except Exception as exc:
            self._error('source_fingerprint', exc)
        self._save_metadata()
        self.event('scan_start', settings=settings)

    def _error(self, operation, exc):
        self.errors.append(dict(operation=operation, error=f'{type(exc).__name__}: {exc}'))
        if len(self.errors) == 1:
            logger.warning('[DeepDiveFourViews] diagnostics unavailable: %s', self.errors[-1]['error'])

    def _save_metadata(self):
        try:
            payload = dict(self.metadata, event_counts=dict(self.counts), events_attempted=self.sequence,
                           trace_disabled=self.trace_disabled, diagnostic_errors=list(self.errors),
                           trace_write_elapsed_sec=self.write_elapsed_sec)
            path = self.output/'diagnostics.json'
            temporary = path.with_suffix('.json.part')
            temporary.write_text(json.dumps(_json_value(payload), ensure_ascii=False, indent=2,
                                             allow_nan=False), encoding='utf8')
            temporary.replace(path)
        except Exception as exc:
            self._error('metadata_write', exc)

    def event(self, event, **fields):
        self.sequence += 1
        self.counts[event] += 1
        if self.trace_disabled:
            return
        began = time.monotonic()
        try:
            row = dict(self.identity, sequence=self.sequence, event=event,
                       at_monotonic=began, elapsed_sec=began-self.started, **fields)
            with (self.output/'control_trace.jsonl').open('a', encoding='utf8') as stream:
                stream.write(json.dumps(_json_value(row), ensure_ascii=False, allow_nan=False)+'\n')
        except Exception as exc:
            self.trace_disabled = True
            self._error('trace_write', exc)
        finally:
            self.write_elapsed_sec += time.monotonic()-began

    def feedback(self, *, view, source, frame_path, alignment, feedback, reference_stage,
                 reference_source, elapsed_sec):
        try:
            cells = {(c['face'], c['row'], c['col']): np.asarray(c['quad'], float)
                     for c in alignment.get('cells', [])}
            basis = (reference_stage, (reference_source or {}).get('frame_id'))
            previous = self.previous_fits.get(view)
            motion = dict(used_for_control=False, proves_stopped=False)
            if previous is not None:
                dt = source['frame_time']-previous['source']['frame_time']
                motion.update(previous_source=previous['source'], source_interval_sec=dt,
                              same_error_reference=previous['basis'] == basis)
                error, old_error = feedback.get('signed_error_deg'), previous['error']
                if previous['basis'] == basis and error is not None and old_error is not None:
                    motion['image_error_delta_deg'] = error-old_error
                    if dt > 0:
                        motion['image_error_rate_deg_per_sec'] = (error-old_error)/dt
                shifts = [np.linalg.norm(cells[k]-previous['cells'][k], axis=1)
                          for k in cells.keys() & previous['cells'].keys()
                          if cells[k].shape == previous['cells'][k].shape == (4, 2)]
                if shifts:
                    shifts = np.concatenate(shifts)
                    motion.update(common_cell_count=len(shifts)//4,
                                  quad_corner_shift_median_px=float(np.median(shifts)),
                                  quad_corner_shift_max_px=float(np.max(shifts)))
            self.previous_fits[view] = dict(source=dict(source), cells=cells, basis=basis,
                                           error=feedback.get('signed_error_deg'))
            panels = [{k: p.get(k) for k in ('reference_panel', 'proposal_accepted', 'reason',
                       'structural_support_valid', 'actual_inliers', 'reference_cells',
                       'reference_span_px', 'query_span_px', 'projection_failures',
                       'selection_quality', 'reference_feature_count')}
                      for p in alignment.get('results', [])]
            self.event('feedback', view=view, source=source, frame_path=frame_path,
                       reference_stage=reference_stage, reference_source=reference_source,
                       feedback=feedback, proposal_accepted=alignment.get('proposal_accepted'),
                       alignment_reason=alignment.get('reason'), panels=panels,
                       processing_elapsed_sec=elapsed_sec, observed_motion=motion)
        except Exception as exc:
            self._error('feedback_summary', exc)

    def failure(self, exc, **fields):
        self.event('scan_exception', error_type=type(exc).__name__, error=str(exc),
                   traceback=traceback.format_exc(limit=8), **fields)

    def checkpoint(self, **fields):
        self.metadata.update(fields)
        self._save_metadata()

    def finish(self, *, status, reason, **fields):
        self.event('scan_stop', status=status, reason=reason, **fields)
        self.checkpoint(status=status, reason=reason, **fields)
