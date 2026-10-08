"""The isolated provider must preserve source/cache guards without loading ORT here."""
from copy import deepcopy

import numpy as np
import pytest

from plans.resonance_pc.src.services import deep_dive_entity_detector_service as service
from plans.resonance_pc.src.services import _deep_dive_entity_worker_backend as backend
from plans.resonance_pc.src.services._deep_dive_entity_onnx import EntityOnnxBackend, prepare_rgb_roi


class Config:
    def __init__(self, model):
        self.values = {'model_path': str(model), 'execution_provider': 'dml_worker', 'hybrid': False}

    def get(self, key, default=None):
        return self.values.get(key.removeprefix('resonance_pc.deep_dive.entity_detector.'), default)


class Worker:
    provider = 'DmlExecutionProvider'

    def __init__(self, *args, **kwargs):
        self.ready = False
        self.frames = []
        self.closed = 0

    @property
    def status(self):
        return {'provider': self.provider, 'ort_version': '1.24.4', 'ready': self.ready}

    def initialize(self):
        self.ready = True
        return self.status

    def run(self, rgb):
        self.frames.append(rgb.copy())
        raw = np.zeros((1, 7, 2), np.float32)
        raw[0, :4] = np.array([[300, 500], [300, 400], [40, 40], [40, 40]])
        raw[0, 6] = [.599, .8]
        return raw, prepare_rgb_roi(rgb)[1], {'request_id': len(self.frames), 'provider': self.provider}

    def close(self):
        self.closed += 1
        self.ready = False
        return {'drained': True}


def test_worker_preserves_weak_masks_exact_cache_and_actual_metadata(tmp_path, monkeypatch):
    model = tmp_path/'model.onnx'
    model.write_bytes(b'isolated service test')
    monkeypatch.setattr(backend, 'EntityDmlWorkerBackend', Worker)
    def forbidden(*args):
        raise AssertionError('worker mode must not load the parent ONNX session')
    monkeypatch.setattr(EntityOnnxBackend, 'create_session', forbidden)
    detector = service.DeepDiveEntityDetectorService(Config(model))
    rgb = np.zeros((720, 1280, 3), np.uint8)
    packet = detector.detect_packet(rgb)
    assert packet['model_executed'] and packet['coverage_valid'] and not packet['cached']
    assert sorted(t['confirmable'] for t in packet['targets']) == [False, True]
    original = deepcopy(packet)
    packet['targets'].clear()
    again = detector.detect_packet(rgb.copy())
    assert again['targets'] == original['targets']
    assert again['cached'] and not again['model_executed']
    worker = detector._worker
    rgb[0, 0, 0] = 1  # A change outside the model ROI still requires a new source.
    detector.detect_packet(rgb)
    assert len(worker.frames) == 2 and worker.frames[0][0, 0, 0] == 0
    assert detector.status()['worker']['ort_version'] == '1.24.4'
    assert detector._session is None
    detector.close()
    assert worker.closed == 1 and not detector.status()['ready']


def test_wrong_actual_worker_provider_is_rejected_and_closed(tmp_path, monkeypatch):
    model = tmp_path/'model.onnx'
    model.write_bytes(b'isolated service test')
    workers = []
    class Wrong(Worker):
        provider = 'CPUExecutionProvider'
        def __init__(self, *args, **kwargs):
            super().__init__(*args, **kwargs)
            workers.append(self)
    monkeypatch.setattr(backend, 'EntityDmlWorkerBackend', Wrong)
    detector = service.DeepDiveEntityDetectorService(Config(model))
    with pytest.raises(RuntimeError, match='activate DirectML'):
        detector.initialize()
    assert workers[0].closed == 1
    assert detector._worker is None and detector._session is None
    assert not detector.status()['ready']


def test_failed_close_diagnostics_survive_idempotent_cleanup(tmp_path, monkeypatch):
    model = tmp_path/'model.onnx'
    model.write_bytes(b'isolated service test')
    class FailedClose(Worker):
        def close(self):
            super().close()
            return {'drained': False, 'error': 'owned worker did not acknowledge'}
    monkeypatch.setattr(backend, 'EntityDmlWorkerBackend', FailedClose)
    detector = service.DeepDiveEntityDetectorService(Config(model))
    detector.initialize()
    detector.close()
    failed = detector.status()['worker_close']
    detector.close()
    assert detector.status()['worker_close'] == failed
    assert failed['drained'] is False
