import asyncio
import pytest
from plans.resonance_pc.src.actions import consciousness_deep_dive_scan_pc_actions as scan
from plans.resonance_pc.src.actions import _deep_dive_four_view_scan as four


def test_four_route_dispatches_normal_injected_detector_and_full_budget(monkeypatch):
    sentinel=object();received={};app=object()
    async def acquire(value,**kwargs):
        received.update(kwargs);assert value is app
        assert scan._SCAN_CONTROL.get()['scan_route']=='four_views'
        return dict(status='partial',success=False)
    monkeypatch.setattr(four,'run_four_view_scan',acquire)
    result=asyncio.run(scan.run_layout_scan(app,scan_route='four_views',entity_detector=sentinel,
        recognition_goal='targets',expected_inspirations=2))
    assert result['success'] is False
    assert received['entity_detector'] is sentinel and received['time_budget_sec']==300
    assert received['expected_inspirations']==2
    assert scan._SCAN_CONTROL.get() is None


def test_failure_restores_scan_context(monkeypatch):
    async def acquire(*args,**kwargs):raise RuntimeError('actual_failure')
    monkeypatch.setattr(four,'run_four_view_scan',acquire)
    with pytest.raises(RuntimeError,match='actual_failure'):
        asyncio.run(scan.run_layout_scan(object(),scan_route='four_views',entity_detector=object()))
    assert scan._SCAN_CONTROL.get() is None


def test_legacy_route_keeps_its_budget_and_engine(monkeypatch):
    calls=[]
    async def acquire(**kwargs):calls.append(kwargs);return dict(success=False,status='partial')
    monkeypatch.setattr(scan,'_run_layout_scan',acquire)
    asyncio.run(scan.run_layout_scan(object(),scan_route='cells',time_budget_sec=300))
    assert calls[0]['time_budget_sec']==90


def test_four_full_scan_still_requires_real_entity_service():
    with pytest.raises(ValueError,match='model service'):
        asyncio.run(scan.run_layout_scan(object(),scan_route='four_views',recognition_goal='full'))
