"""Current sampling limits reach the metric reader without the retired alias."""
import asyncio
from types import SimpleNamespace

import pytest

from plans.resonance_pc.src.actions import cape_island_investment_pc_actions as island


def test_retired_metric_timeout_parameter_is_rejected():
    with pytest.raises(TypeError, match="metric_timeout_sec"):
        island.resonance_pc_execute_cape_island_investment_from_city_panel(metric_timeout_sec=1)


def test_current_metric_limits_are_forwarded_without_live_input(monkeypatch):
    async def transition(*args, **kwargs):
        return {}
    async def match(**kwargs):
        return SimpleNamespace(found=True)
    calls = []
    def metrics(*args, **kwargs):
        calls.append(kwargs)
        return {"values": {}, "ocr_history": [], "completed_rounds": 2, "elapsed_sec": .3,
                "sample_timeout_sec": kwargs["sample_timeout_sec"], "max_rounds": kwargs["max_rounds"],
                "total_timeout_sec": kwargs["total_timeout_sec"]}
    for name in ("_enter_island", "_open_revenue_overview", "_click_template_required"):
        monkeypatch.setattr(island, name, transition)
    monkeypatch.setattr(island, "wait_for_image", match)
    monkeypatch.setattr(island, "_match_payload", lambda *a, **k: {})
    monkeypatch.setattr(island, "_read_investment_metrics", metrics)
    monkeypatch.setattr(island, "_metric_caps", lambda values: {"investment": True})
    result = asyncio.run(island.resonance_pc_execute_cape_island_investment_from_city_panel(
        app=object(), ocr=object(), vision=object(), resonance_pc_city_shop_data=object(), engine=object(),
        metric_sample_timeout_sec=1.25, metric_max_rounds=5, metric_total_timeout_sec=17, interval_sec=.2,
    ))
    assert calls == [{"sample_timeout_sec": 1.25, "max_rounds": 5, "total_timeout_sec": 17, "interval_sec": .2}]
    assert result["status"] == "skipped" and result["reason"] == "all_metrics_capped"
    assert result["metric_read"]["sample_timeout_sec"] == 1.25
