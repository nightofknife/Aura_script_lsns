"""Isolated input-effect prototype; deliberately not wired to live control.

Capture sequence/timestamps are freshness metadata, never input acknowledgements.
Only a new actual geometry pose can pay a monotone leg's outstanding debt.
Epoch changes with debt fail closed: this prototype cannot compensate glyph body
corrections because the current snapshot does not expose their exact application.
"""

from collections import deque
import math

import numpy as np


def _rotation(value):
    result = np.asarray(value, dtype=float)
    if (result.shape != (3, 3) or not np.isfinite(result).all()
            or not np.allclose(result.T @ result, np.eye(3), atol=1e-5)
            or abs(np.linalg.det(result)-1.) > 1e-5):
        raise ValueError('invalid_observed_rotation')
    return result.copy()


def _log(rotation):
    angle = math.acos(float(np.clip((np.trace(rotation)-1.)/2., -1., 1.)))
    if angle < 1e-8:
        return np.zeros(3)
    if math.pi-angle < 1e-5:
        values, vectors = np.linalg.eigh((rotation+np.eye(3))/2.)
        return vectors[:, np.argmax(values)]*angle
    return np.array((rotation[2, 1]-rotation[1, 2], rotation[0, 2]-rotation[2, 0],
                     rotation[1, 0]-rotation[0, 1]))*angle/(2.*math.sin(angle))


class InputAckLedger:
    """Single-owner deterministic command/observation ledger.

    ``allowance`` is a query, not a movement command. ``issued`` records an actual
    successful input, before another query. Pass ONLY tracked geometry rotation
    to ``observe``; extrapolated/semantic poses are not admissible.

    Debt is measured in both mouse pixels and model-estimated radians. The
    model converts rendered angular progress to credit; it is not proof that
    the renderer consumed a particular command. Keep all existing independent
    tracking/semantic guards outside this experimental helper.
    """

    def __init__(self, *, angle_cap_deg=16., pixel_cap=50., jitter_deg=.2,
                 no_progress_frames=3, fresh_age_sec=.35, effect_wait_sec=.75):
        parameters = (angle_cap_deg, pixel_cap, jitter_deg, fresh_age_sec, effect_wait_sec)
        if (not all(math.isfinite(float(v)) and float(v) > 0 for v in parameters)
                or jitter_deg >= angle_cap_deg or angle_cap_deg >= 90
                or isinstance(no_progress_frames, bool) or not isinstance(no_progress_frames, int)
                or no_progress_frames < 1):
            raise ValueError('invalid_ack_configuration')
        self.angle_cap = math.radians(angle_cap_deg)
        self.pixel_cap = float(pixel_cap)
        self.jitter = math.radians(jitter_deg)
        self.no_progress_limit = no_progress_frames
        self.fresh_age = float(fresh_age_sec)
        self.effect_wait = float(effect_wait_sec)
        self._chunks = deque()
        self._context = None
        self._seq = -1
        self._observed = None
        self._observed_at = None
        self._clock = -math.inf
        self._leg_direction = None
        self._leg_axis = None
        self._leg_progress = 0.
        self._high_water = 0.
        self._no_progress = 0
        self._frozen_at = None
        self._fault = None
        self._unsafe = None
        self._pending = None

    @property
    def debt_px(self):
        return sum(chunk[0] for chunk in self._chunks)

    @property
    def debt_angle(self):
        return sum(chunk[1] for chunk in self._chunks)

    def _tick(self, now):
        if not math.isfinite(float(now)) or now < self._clock:
            self._fault = 'nonmonotonic_clock'
            return False
        self._clock = float(now)
        if self._frozen_at is not None and now-self._frozen_at >= self.effect_wait:
            self._fault = 'input_effect_timeout'
        return self._fault is None

    def status(self):
        state = ('blocked' if self._fault else 'unsafe' if self._unsafe else
                 'uninitialized' if self._observed is None else
                 'awaiting_effect' if self._frozen_at is not None else
                 'awaiting_direction_change' if self._pending is not None and self._chunks else
                 'issuing' if self._chunks else 'idle')
        return dict(state=state, reason=self._fault or self._unsafe,
                    debt_px=self.debt_px, debt_angle_deg=math.degrees(self.debt_angle),
                    no_progress_frames=self._no_progress,
                    pending_direction=None if self._pending is None else self._pending.tolist())

    @staticmethod
    def _command(direction, axes):
        direction = np.asarray(direction, dtype=float)
        if direction.shape != (2,) or not np.isfinite(direction).all() or np.linalg.norm(direction) < 1e-9:
            raise ValueError('invalid_input_direction')
        direction = direction/np.linalg.norm(direction)
        vectors = [np.asarray(axes[i], dtype=float) for i in (0, 1)]
        if any(v.shape != (3,) or not np.isfinite(v).all() for v in vectors):
            raise ValueError('invalid_response_axes')
        vector = vectors[0]*direction[0]+vectors[1]*direction[1]
        rate = float(np.linalg.norm(vector))
        if rate < 1e-9:
            raise ValueError('unmeasured_response_direction')
        return direction, vector/rate, rate

    def observe(self, rotation, *, seq, session_id, map_revision, correction_epoch,
                now, tracking_ok=True, source_age_sec=0., source_kind='geometry'):
        """Accept distinct actual geometry; never acknowledge body corrections."""
        if not self._tick(now):
            return dict(accepted=False, credited_deg=0., **self.status())
        if (source_kind != 'geometry' or not tracking_ok or
                not math.isfinite(float(source_age_sec)) or not 0 <= source_age_sec <= self.fresh_age):
            self._unsafe = 'not_fresh_tracked_geometry'
            return dict(accepted=False, credited_deg=0., **self.status())
        try:
            rotation = _rotation(rotation)
        except (ValueError, TypeError):
            self._unsafe = 'invalid_observed_rotation'
            return dict(accepted=False, credited_deg=0., **self.status())
        if (isinstance(seq, bool) or not isinstance(seq, int) or seq < 0 or
                any(isinstance(v, bool) or not isinstance(v, int) or v < 0
                    for v in (map_revision, correction_epoch))):
            raise ValueError('invalid_geometry_identity')
        context = (session_id, map_revision, correction_epoch)
        if self._context is not None and context != self._context:
            if self._chunks:
                self._fault = 'context_changed_with_unconfirmed_input'
                return dict(accepted=False, credited_deg=0., **self.status())
            self._unsafe = 'context_requires_rebase'
            return dict(accepted=False, credited_deg=0., **self.status())
        if seq < self._seq:
            self._unsafe = 'geometry_sequence_regressed'
            return dict(accepted=False, credited_deg=0., **self.status())
        if seq == self._seq:
            return dict(accepted=False, credited_deg=0., **self.status())
        self._context = context
        self._seq = seq
        previous = self._observed
        self._observed = rotation
        self._observed_at = now-source_age_sec
        self._unsafe = None
        credit = 0.
        if self._chunks:
            # Integrate actual signed increments, rather than taking the log
            # of an arbitrarily long leg (which wraps at 180 degrees).
            self._leg_progress += float(np.dot(_log(rotation @ previous.T), self._leg_axis))
            progress = self._leg_progress
            # A high-water mark prevents back-and-forth motion earning credit
            # twice. Below-noise changes accumulate as signed actual motion.
            if progress-self._high_water > self.jitter:
                credit = min(progress-self._high_water, self.debt_angle)
                self._high_water = progress
                remaining = credit
                while remaining > 1e-12 and self._chunks:
                    pixels, angle = self._chunks[0]
                    paid = min(remaining, angle)
                    remaining -= paid
                    if paid >= angle-1e-12:
                        self._chunks.popleft()
                    else:
                        self._chunks[0] = [pixels*(1.-paid/angle), angle-paid]
                self._no_progress = 0
                self._frozen_at = None
            else:
                self._no_progress += 1
                if self._no_progress >= self.no_progress_limit and self._frozen_at is None:
                    self._frozen_at = now
            if not self._chunks:
                self._leg_direction = self._leg_axis = None
                self._leg_progress = 0.
                self._high_water = 0.
                self._no_progress = 0
                self._frozen_at = None
        return dict(accepted=True, credited_deg=math.degrees(credit), **self.status())

    def rebase(self, rotation, *, seq, session_id, map_revision, correction_epoch, now):
        """Explicit context rebuild is permitted only with zero input debt."""
        if self._chunks:
            return dict(accepted=False, credited_deg=0., reason='rebase_requires_zero_debt')
        if self._fault:
            return dict(accepted=False, credited_deg=0., **self.status())
        self._context = None
        self._seq = -1
        return self.observe(rotation, seq=seq, session_id=session_id, map_revision=map_revision,
                            correction_epoch=correction_epoch, now=now)

    def allowance(self, direction, axes, *, now, max_px=math.inf):
        """Return bounded credit; a reversal stays queued until prior debt clears."""
        direction, axis, rate = self._command(direction, axes)
        healthy = self._tick(now)
        changing = (self._chunks and (np.dot(direction, self._leg_direction) < .995
                                     or np.dot(axis, self._leg_axis) < .995))
        if changing:
            self._pending = direction.copy()
        reason = self._fault or self._unsafe
        if reason is None and (self._observed_at is None or now-self._observed_at > self.fresh_age):
            reason = 'geometry_age_exceeded'
        if reason is None and self._frozen_at is not None:
            reason = 'waiting_for_actual_effect'
        if reason is None and changing:
            reason = 'queued_direction_change'
        if reason is None and self._pending is not None and np.dot(direction, self._pending) < .995:
            reason = 'pending_direction_must_be_reconsidered'
        pixels = (min(float(max_px), self.pixel_cap-self.debt_px,
                      (self.angle_cap-self.debt_angle)/rate) if healthy and reason is None else 0.)
        if not math.isfinite(pixels) or pixels < 0:
            pixels = 0.
        return dict(allowed_px=pixels, allowed=pixels > 1e-9, decision_reason=reason,
                    **self.status())

    def issued(self, displacement, axes, *, now):
        """Record successful input; refuses debt/cap/direction violations."""
        displacement = np.asarray(displacement, dtype=float)
        direction, axis, rate = self._command(displacement, axes)
        pixels = float(np.linalg.norm(displacement))
        result = self.allowance(direction, axes, now=now)
        if pixels > result['allowed_px']+1e-9 or not result['allowed']:
            # The caller says these pixels already reached the actuator. An
            # accounting violation must stop all future sending, not pretend
            # an unrecorded reversal/cap excess never happened.
            self._fault = self._fault or 'issued_without_credit'
            return dict(recorded=False, decision_reason=result['decision_reason'] or 'input_exceeds_credit',
                        **self.status())
        if not self._chunks:
            self._leg_direction = direction.copy()
            self._leg_axis = axis.copy()
            self._leg_progress = 0.
            self._high_water = 0.
        self._chunks.append([pixels, pixels*rate])
        self._pending = None
        return dict(recorded=True, **self.status())
