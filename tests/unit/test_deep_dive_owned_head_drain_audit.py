"""Owned runtime close audit only; no game, ORT session or capture."""
from copy import deepcopy
import json
import sys
from types import SimpleNamespace

import pytest

from tools import deep_dive_live_acceptance as live


@pytest.mark.parametrize('status', [None, {}, {'worker_close':{'drained':True}},
    {'head_thread':{'owned':False,'in_flight':False,'close':None}},
    {'worker_close':{'drained':True},'head_thread':{'owned':False,'in_flight':False,'close':{'drained':True}}}])
def test_no_head_legacy_or_closed_owned_thread_is_legal(status):
    assert live.detector_close_error(status) is None


@pytest.mark.parametrize('head,reason', [
    ({'owned':False,'in_flight':False,'close':{'drained':False}},'entity_head_close_not_drained'),
    ({'owned':False,'in_flight':False,'close':{}},'entity_head_close_not_drained'),
    ({'owned':False,'in_flight':False,'close':[]},'entity_head_close_not_drained'),
    ({'owned':True,'in_flight':False,'close':{'drained':True}},'entity_head_thread_still_owned_or_in_flight'),
    ({'owned':False,'in_flight':True,'close':None},'entity_head_thread_still_owned_or_in_flight'),
    ({'owned':False,'close':{'drained':True}},'entity_head_thread_close_not_proven'),
    ({'owned':0,'in_flight':False,'close':{'drained':True}},'entity_head_thread_close_not_proven'),
    ('unknown','entity_head_close_status_invalid'),
])
def test_owned_head_failure_cannot_be_hidden_by_normal_model_worker(head, reason):
    status={'worker_close':{'drained':True},'head_thread':head}
    assert live.detector_close_error(status)==reason


def test_existing_worker_drain_failure_still_has_original_reason():
    assert live.detector_close_error({'worker_close':{'drained':False},
        'head_thread':{'owned':False,'in_flight':False,'close':{'drained':True}}})=='entity_worker_close_not_drained'


@pytest.mark.parametrize('bad', ['head','exception','none'])
def test_runtime_closed_records_head_failure_or_compatible_nohead(tmp_path, monkeypatch, bad):
    scan_module=SimpleNamespace(run_layout_scan=lambda:None)
    closed=[]
    status=({'worker_close':{'drained':True},'head_thread':
        {'owned':False,'in_flight':False,'close':{'drained':False}}} if bad=='head' else {'worker_close':{'drained':True}})
    class Harness:
        def __init__(self,original,config,**kwargs):self.original=original;self.round=None
        def scan(self,*args,**kwargs):pass
        def close(self):
            if bad=='exception':raise RuntimeError('head shutdown failed')
            return deepcopy(status)
    class Runner:
        def __init__(self,**kwargs):pass
        def start(self):return {}
        def run_task(self,**kwargs):
            state=scan_module.run_layout_scan.__self__.round
            state['wrapper_entered'].set();state['wrapper_done'].set()
            state['outcome']={'layout':{'success':False,'status':'partial'}}
            return {'cid':'unit_fake_dispatch'}
        def get_run(self,cid):return {'status':'failed'}
        def cancel_task(self,cid):pass
        def wait_for_run(self,*args,**kwargs):return {'status':'failed'}
        def close(self):closed.append(True)
    class Ledger:
        data={'fingerprint':{'model_sha256':'1'*64},'runs':[]}
        def record_run(self,payload):return dict(payload,attempt_index=1,passed=False,failures=['business_not_ready'])
        def summary(self):return {'consecutive_passes':0}
    monkeypatch.setattr(live,'ROOT',tmp_path)
    monkeypatch.setattr(live,'_OWNED_RUNTIME_CLOSED',False)
    monkeypatch.setattr(live,'ScanHarness',Harness)
    monkeypatch.setattr(live,'campaign',lambda *args:None)
    monkeypatch.setattr(live,'positive_sources',lambda *args:([],[],[],[]))
    monkeypatch.setitem(sys.modules,'cv2',SimpleNamespace(setNumThreads=lambda n:None,getNumThreads=lambda:1))
    monkeypatch.setitem(sys.modules,'packages.aura_game',SimpleNamespace(EmbeddedGameRunner=Runner))
    monkeypatch.setitem(sys.modules,'plans.resonance_pc.src.actions',
        SimpleNamespace(consciousness_deep_dive_scan_pc_actions=scan_module))
    for name in ['TEMP','TMP','TMPDIR']:monkeypatch.setenv(name,str(tmp_path))
    args=SimpleNamespace(output=str(tmp_path/'output'),count=1,truth='unit_truth',keep_going=False)
    config=dict(opencv_threads=1,scan_inputs=dict(time_budget_sec=90),entity_detector={},runtime_assertions={})
    if bad=='none':assert live.run_campaign(args,Ledger(),config)==1
    else:
        with pytest.raises(RuntimeError,match='runtime_drain_failed'):
            live.run_campaign(args,Ledger(),config)
    record=json.loads((tmp_path/'output/runtime_closed.json').read_text('utf8'))
    assert record['only_owned_runtime'] is True and closed==[True]
    expected={'head':'entity_head_close_not_drained','exception':'entity_detector_close_failed:head shutdown failed','none':None}[bad]
    assert record['drain_error']==expected
    assert record['detector_closed']==(None if bad=='exception' else status)
