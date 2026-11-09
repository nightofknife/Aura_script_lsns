"""Offline contracts for combined freight recovery snapshot handoffs."""

from __future__ import annotations

import asyncio
from copy import deepcopy
from pathlib import Path

import pytest
import yaml

from plans.resonance_pc.src.actions import city_trade_flow_pc_actions as trade_flow


@pytest.fixture
def recovery_snapshot():
    return {
        "status": {"fatigue": {"current": 120, "max": 800}, "clarity": {"current": 80}},
        "recovery": {
            "sparkling_water": {"remaining_free_uses": 3, "daily_free_limit": 6},
            "bento": {"available_count": 2},
        },
        "metadata": {"persisted": True, "profile_section_updated_at": {"fatigue": "now"}},
        "future_field": {"keep": [1, 2]},
    }














def test_real_auto_trade_requires_snapshot_before_ui(monkeypatch):
    monkeypatch.setattr(
        trade_flow, "resonance_pc_open_city_panel_from_main",
        lambda **kwargs: pytest.fail("missing snapshot must fail before UI"),
    )
    with pytest.raises(ValueError, match="recovery_snapshot"):
        asyncio.run(trade_flow.resonance_pc_auto_cycle_trade_flow(
            auto_sparkling_water=True,
            sparkling_water_start_threshold=500, sparkling_water_stop_threshold=150,
            context=object(), engine=object(), persistent_data=object(),
        ))


def test_real_auto_trade_requires_persistence_before_ui(monkeypatch, recovery_snapshot):
    monkeypatch.setattr(
        trade_flow, "resonance_pc_open_city_panel_from_main",
        lambda **kwargs: pytest.fail("missing persistence must fail before UI"),
    )
    with pytest.raises(RuntimeError, match="persistent_data"):
        asyncio.run(trade_flow.resonance_pc_auto_cycle_trade_flow(
            auto_sparkling_water=True, recovery_snapshot=recovery_snapshot,
            sparkling_water_start_threshold=500, sparkling_water_stop_threshold=150,
            context=object(), engine=object(),
        ))
