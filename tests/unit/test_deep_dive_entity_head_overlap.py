"""CPU fakes and saved RGB only; no ORT session, GPU or game input."""
from concurrent.futures import ThreadPoolExecutor
from copy import deepcopy
from pathlib import Path
import threading
from types import SimpleNamespace

import cv2
import numpy as np
import pytest

from plans.resonance_pc.src.services import deep_dive_entity_detector_service as service
from plans.resonance_pc.src.services import _deep_dive_entity_worker_backend as backend
from plans.resonance_pc.src.services._deep_dive_entity_fusion import local_player_heads, fuse_entities
from plans.resonance_pc.src.services._deep_dive_entity_onnx import decode_raw_entities, prepare_rgb_roi


class Config:
    def __init__(self, **values):self.values=values
    def get(self, key, default=None):
        return self.values.get(key.removeprefix('resonance_pc.deep_dive.entity_detector.'),default)


def raw_packet():
    raw=np.zeros((1,7,5),np.float32)
    raw[0,:4]=np.asarray([[320,320,500,200,100],[320,320,400,200,100],
                         [40,40,50,20,10],[40,40,50,20,10]])
    raw[0,4:]=np.asarray([[.9,.8,.01,.01,.01],[.01,.01,.7,.01,.01],
                         [.01,.01,.01,.10,.04]])
    return raw


@pytest.fixture
def build(tmp_path, monkeypatch):
    detectors=[]
    class Worker:
        def __init__(self,*args,**kwargs):
            self.ready=False;self.closed=0;self.frames=[];self.hook=None;self.raw=raw_packet()
        @property
        def status(self):return dict(provider='DmlExecutionProvider',ready=self.ready)
        def initialize(self):self.ready=True;return self.status
        def run(self,rgb):
            self.frames.append(rgb.copy())
            if self.hook:self.hook(rgb)
            return self.raw,prepare_rgb_roi(rgb)[1],dict(request_id=len(self.frames),provider='DmlExecutionProvider')
        def close(self):self.closed+=1;self.ready=False;return dict(drained=True,error=None)
    monkeypatch.setattr(backend,'EntityDmlWorkerBackend',Worker)
    def make(**values):
        model=tmp_path/'model.onnx';model.write_bytes(b'fake worker only')
        detector=service.DeepDiveEntityDetectorService(Config(model_path=str(model),
            execution_provider='dml_worker',hybrid=True,**values))
        detector.initialize();detectors.append(detector);return detector
    yield make
    for detector in detectors:detector.close()


def test_head_and_worker_overlap_on_identical_private_readonly_rgb(build, monkeypatch):
    detector=build();entered=threading.Event();rpc=threading.Event();finished=threading.Event()
    source=np.zeros((720,1280,3),np.uint8);seen=[]
    def heads(rgb):
        seen.append((rgb,threading.get_ident()))
        assert not rgb.flags.writeable
        entered.set();assert rpc.wait(1)
        assert rgb[0,0,0]==0
        finished.set();return [],dict(local_windows=0)
    def worker(rgb):
        assert entered.wait(1)
        assert rgb is seen[0][0] and not rgb.flags.writeable
        assert not finished.is_set()
        source[0,0,0]=255
        rpc.set()
    monkeypatch.setattr(service,'local_player_heads',heads);detector._worker.hook=worker
    packet=detector.detect_packet(source)
    assert finished.is_set() and packet['model_executed']
    assert seen[0][1]!=threading.get_ident() and detector._cached_rgb[0,0,0]==0
    assert detector.status()['last']['head_overlap']['enabled'] is True
    assert detector._head_future is None


def test_exact_cache_never_submits_head_or_model_again_and_is_detached(build, monkeypatch):
    detector=build();calls=[]
    monkeypatch.setattr(service,'local_player_heads',lambda rgb:(calls.append(rgb) or [],{}))
    rgb=np.zeros((720,1280,3),np.uint8);first=detector.detect_packet(rgb);expected=deepcopy(first['targets'])
    first['targets'].clear();again=detector.detect_packet(rgb.copy())
    assert again['targets']==expected and again['cached'] and not again['model_executed']
    assert len(calls)==len(detector._worker.frames)==1


@pytest.mark.parametrize('failure', ['model','decode','head','both'])
def test_any_failure_drains_current_future_and_never_publishes_success(build, monkeypatch, failure):
    detector=build();started=threading.Event();release=threading.Event();finished=threading.Event()
    def heads(rgb):
        started.set();assert release.wait(1);finished.set()
        if failure in ['head','both']:raise RuntimeError('head failed')
        return [],{}
    def worker(rgb):
        assert started.wait(1);release.set()
        if failure in ['model','both']:raise RuntimeError('model failed')
    detector._worker.hook=worker
    if failure=='decode':detector._worker.raw=np.full((1,7,5),np.nan,np.float32)
    monkeypatch.setattr(service,'local_player_heads',heads)
    with pytest.raises((RuntimeError,ValueError),match={'model':'model failed','decode':'nonfinite','head':'head failed','both':'model failed'}[failure]):
        detector.detect_packet(np.zeros((720,1280,3),np.uint8))
    assert finished.is_set() and detector._head_future is None
    assert detector._cached_packet is None and detector.status()['model_executions']==0


def test_decode_error_waits_for_head_to_finish_before_releasing_frame_lock(build, monkeypatch):
    detector=build();started=threading.Event();release=threading.Event();finished=threading.Event()
    def heads(rgb):
        started.set();assert release.wait(1);finished.set();return [],{}
    monkeypatch.setattr(service,'local_player_heads',heads)
    detector._worker.raw=np.full((1,7,5),np.nan,np.float32)
    with ThreadPoolExecutor(max_workers=1) as executor:
        future=executor.submit(detector.detect_packet,np.zeros((720,1280,3),np.uint8))
        try:
            assert started.wait(1)
            assert not future.done()
        finally:release.set()
        with pytest.raises(ValueError,match='nonfinite'):future.result(timeout=1)
    assert finished.is_set() and detector._head_future is None


def test_concurrent_requests_keep_only_one_source_and_one_head_future(build, monkeypatch):
    detector=build();active=[0,0];seen=[];lock=threading.Lock()
    def heads(rgb):
        with lock:
            active[0]+=1;active[1]=max(active);seen.append(int(rgb[0,0,0]))
        with lock:active[0]-=1
        return [],{}
    monkeypatch.setattr(service,'local_player_heads',heads)
    images=[np.full((720,1280,3),i,np.uint8) for i in range(1,5)]
    with ThreadPoolExecutor(max_workers=4) as executor:packets=list(executor.map(detector.detect_packet,images))
    assert all(p['model_executed'] for p in packets)
    assert active[1]==1 and sorted(seen)==[1,2,3,4]
    assert sorted(int(rgb[0,0,0]) for rgb in detector._worker.frames)==[1,2,3,4]


def test_close_waits_for_active_head_and_shuts_owned_thread_then_can_reload(build, monkeypatch):
    detector=build();started=threading.Event();release=threading.Event();closed=threading.Event()
    def heads(rgb):started.set();assert release.wait(1);return [],{}
    monkeypatch.setattr(service,'local_player_heads',heads);worker=detector._worker
    def close():detector.close();closed.set()
    with ThreadPoolExecutor(max_workers=2) as executor:
        running=executor.submit(detector.detect_packet,np.zeros((720,1280,3),np.uint8))
        assert started.wait(1);closing=executor.submit(close)
        try:assert not closed.wait(.01)
        finally:release.set()
        running.result(timeout=1);closing.result(timeout=1)
    assert worker.closed==1 and detector._head_executor is None and detector._head_future is None
    assert detector.status()['head_thread']==dict(owned=False,in_flight=False,close=dict(drained=True,error=None))
    assert not any(t.name.startswith('deep-dive-local-head') for t in threading.enumerate())
    detector.close();detector.detect_packet(np.zeros((720,1280,3),np.uint8))
    assert detector.status()['head_thread']['owned']


def test_worker_close_failure_still_drains_owned_head_pool(build, monkeypatch):
    detector=build();monkeypatch.setattr(service,'local_player_heads',lambda rgb:([],{}))
    detector.detect_packet(np.zeros((720,1280,3),np.uint8))
    original=detector._worker.close
    detector._worker.close=lambda:(_ for _ in ()).throw(RuntimeError('close failed'))
    with pytest.raises(RuntimeError,match='close failed'):detector.close()
    assert detector._head_executor is None and detector._head_future is None
    detector._worker.close=original


def test_nonhybrid_worker_never_creates_owned_head_pool(build, monkeypatch):
    detector=build();detector.hybrid=False
    monkeypatch.setattr(service,'local_player_heads',lambda rgb:(_ for _ in ()).throw(AssertionError('disabled')))
    detector.detect_packet(np.zeros((720,1280,3),np.uint8))
    assert detector._head_executor is None and 'head_overlap' not in detector.status()['last']


def test_cpu_hybrid_stays_synchronous_without_owned_head_thread(build, monkeypatch):
    detector=build();detector._worker.close();detector._worker=None;detector._execution_provider='cpu'
    detector._session=SimpleNamespace(run=lambda outputs,feed:[raw_packet()]);detector._input_name='images'
    caller=threading.get_ident();seen=[]
    def heads(rgb):seen.append((threading.get_ident(),rgb.flags.writeable));return [],{}
    monkeypatch.setattr(service,'local_player_heads',heads)
    detector.detect_packet(np.zeros((720,1280,3),np.uint8))
    assert seen==[(caller,True)] and detector._head_executor is None


@pytest.mark.parametrize('fixture', ['reading_only_binding04.png', 'oblique_630.png'])
def test_saved_rgb_overlap_matches_original_sequential_heads_decode_and_fuse(build, fixture):
    path=Path(__file__).resolve().parents[1]/'fixtures/deep_dive_anchor'/fixture
    if not path.exists():pytest.skip('saved fixture unavailable')
    rgb=cv2.cvtColor(cv2.imread(str(path)),cv2.COLOR_BGR2RGB)
    detector=build();raw=detector._worker.raw
    before=cv2.getNumThreads()
    try:
        cv2.setNumThreads(1)
        reference=decode_raw_entities(raw,prepare_rgb_roi(rgb)[1],weak_confidence=.05,confirm_confidence=.6,nms_iou=.5,max_det=64)
        heads,head_diagnostic=local_player_heads(rgb)
        expected,decisions=fuse_entities(reference,heads,image_rgb=rgb)
        packet=detector.detect_packet(rgb)
    finally:cv2.setNumThreads(before)
    assert packet['targets']==expected and detector.status()['last']['head']==head_diagnostic
    assert detector.status()['last']['fusion']==decisions
    assert packet['coverage_valid']==(len(reference)<64)
