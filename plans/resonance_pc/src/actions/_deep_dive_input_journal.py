"""Bounded diagnostic input/geometry records; never grants recognition credit."""
from copy import deepcopy
import math
from numbers import Real


def _native(value):
    if value is None or type(value) in (str, bool, int):
        return value
    if isinstance(value, Real):
        return float(value) if math.isfinite(value) else None
    if hasattr(value, 'tolist'):
        return _native(value.tolist())
    if isinstance(value, (list, tuple)):
        return [_native(item) for item in value]
    if isinstance(value, dict):
        return {str(key): _native(item) for key, item in value.items()}
    return None


def _finite(value):
    return type(value) in (int, float) and math.isfinite(value)


def _context(snapshot):
    session = snapshot.get('session_id')
    revision = snapshot.get('map_revision')
    if not ((type(session) is int and session >= 0) or (type(session) is str and session)):
        return None
    if type(revision) is not int or revision < 0:
        return None
    return (session, revision)


def _rotation(value):
    if not isinstance(value, list) or len(value) != 3:
        return False
    if any(not isinstance(row, list) or len(row) != 3 for row in value):
        return False
    if any(not _finite(x) for row in value for x in row):
        return False
    for i in range(3):
        for j in range(3):
            if abs(sum(value[k][i]*value[k][j] for k in range(3))-(i == j)) > 1e-5:
                return False
    determinant = sum(value[0][j]*(value[1][(j+1)%3]*value[2][(j+2)%3]
                      -value[1][(j+2)%3]*value[2][(j+1)%3]) for j in range(3))
    return abs(determinant-1.) <= 1e-5


class InputJournal:
    """Controller-owned, append-only per channel; snapshots are copied on export."""
    schema = 'resonance_pc.deep_dive_input_journal.v1'

    def __init__(self, max_records=16000):
        if type(max_records) is not int or max_records < 1:
            raise ValueError('invalid_input_journal_capacity')
        self.max_records = max_records
        self._records = {'input': [], 'geometry': []}
        self._truncated = {'input': False, 'geometry': False}
        self._overflow = {'input': 0, 'geometry': 0}
        self._rejected = {'input': {}, 'geometry': {}}
        self._geometry_keys = set()
        self._contexts = []
        self._contexts_truncated = False

    def _reject(self, channel, reason):
        counts = self._rejected[channel]
        counts[reason] = counts.get(reason, 0)+1
        return False

    def _partition(self, context):
        if context is None:
            return None
        if context not in self._contexts:
            if len(self._contexts) >= 2*self.max_records:
                self._contexts_truncated = True
                return None
            self._contexts.append(context)
        return self._contexts.index(context)

    def _append(self, channel, record):
        if len(self._records[channel]) >= self.max_records:
            self._truncated[channel] = True
            self._overflow[channel] += 1
            return False
        self._records[channel].append(record)
        return True

    def record_input(self, snapshot, grip, displacement, cumulative,
                     before_at, completed_at, cancelled=False):
        """Call only after the actual serial-awaited movement has completed.

        Times describe native injection await boundaries, never render consumption.
        An invalid source context is retained with an explicit diagnostic status.
        """
        if not isinstance(snapshot, dict):
            return self._reject('input', 'snapshot_not_mapping')
        values, totals = _native(displacement), _native(cumulative)
        if any(not isinstance(v, list) or len(v) != 2 or any(type(x) is not int for x in v)
               for v in (values, totals)):
            return self._reject('input', 'actual_native_integer_pair_required')
        if (not _finite(before_at) or not _finite(completed_at)
                or before_at < 0 or completed_at < before_at):
            return self._reject('input', 'invalid_completed_input_interval')
        if type(cancelled) is not bool:
            return self._reject('input', 'cancelled_not_native_bool')
        if not ((type(grip) is int and grip >= 0) or (type(grip) is str and grip)):
            return self._reject('input', 'invalid_grip_id')
        context = _context(snapshot)
        source_valid = (context is not None
                        and all(type(snapshot.get(k)) is int and snapshot[k] >= 0
                                for k in ('frame_id', 'generation'))
                        and _finite(snapshot.get('frame_time')) and snapshot['frame_time'] >= 0)
        record = dict(input_id=len(self._records['input']), grip_id=grip,
                      context_partition=self._partition(context),
                      session_id=_native(snapshot.get('session_id')),
                      map_revision=_native(snapshot.get('map_revision')),
                      source_frame_id=_native(snapshot.get('frame_id')),
                      source_generation=_native(snapshot.get('generation')),
                      source_frame_time=_native(snapshot.get('frame_time')),
                      before_at=float(before_at), completed_at=float(completed_at),
                      displacement=values, cumulative=totals, cancelled=cancelled,
                      status=('invalid_source_context' if context is None else
                              'recorded' if source_valid else 'invalid_atomic_source'))
        return self._append('input', record)

    def record_geometry(self, snapshot):
        """Retain actual geometry once per atomic source, independent of semantics."""
        if not isinstance(snapshot, dict):
            return self._reject('geometry', 'snapshot_not_mapping')
        source = _native({key: snapshot.get(key) for key in
                         ('seq','frame_id','generation','session_id','map_revision','frame_time')})
        context = _context(snapshot)
        source_valid = (context is not None and all(type(snapshot.get(k)) is int and snapshot[k] >= 0
                        for k in ('seq','frame_id','generation'))
                        and _finite(snapshot.get('frame_time')) and snapshot['frame_time'] >= 0)
        if source_valid:
            key = context + (source['frame_id'], source['generation'], source['frame_time'])
            if key in self._geometry_keys:
                return False
        else:
            key = None
        raw = _native(snapshot.get('rotation'))
        basis = _native(snapshot.get('geometry_body_basis'))
        status = ('invalid_atomic_source' if not source_valid else
                  'invalid_rotation' if not _rotation(raw) else
                  'invalid_body_basis' if not _rotation(basis) else 'recorded')
        record = dict(source, context_partition=self._partition(context), status=status,
                      published_at=_native(snapshot.get('published_at')),
                      quality=_native(snapshot.get('quality')),
                      tracking_ok=_native(snapshot.get('tracking_ok')),
                      geometry_tracking_ok=_native(snapshot.get('geometry_tracking_ok')),
                      correction_epoch=_native(snapshot.get('correction_epoch')),
                      rotation=raw, geometry_body_basis=basis)
        if status == 'recorded':
            record['base_rotation'] = [[sum(raw[i][k]*basis[j][k] for k in range(3))
                                       for j in range(3)] for i in range(3)]
        saved = self._append('geometry', record)
        if saved and key is not None:
            self._geometry_keys.add(key)
        return saved

    def summary(self):
        return dict(schema=self.schema, diagnostics_only=True, max_records=self.max_records,
                    counts={key: len(value) for key, value in self._records.items()},
                    truncated=deepcopy(self._truncated), overflow=deepcopy(self._overflow),
                    rejected=deepcopy(self._rejected),
                    contexts_truncated=self._contexts_truncated,
                    contexts=[dict(partition=i, session_id=c[0], map_revision=c[1])
                              for i,c in enumerate(self._contexts)])

    def payload(self):
        return dict(self.summary(), records=deepcopy(self._records))
