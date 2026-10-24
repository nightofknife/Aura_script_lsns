"""Smoke checks for combined-commerce freight endpoint handling."""

from __future__ import annotations

import asyncio
from copy import deepcopy

import pytest

from plans.resonance_pc.src.actions import combined_commerce_pc_actions as combined

RECOVERY_FIELDS = {
    "sparkling_water_start_threshold": 700,
    "sparkling_water_stop_threshold": 300,
    "bento_start_threshold": 800,
    "bento_stop_threshold": 200,
}


def test_passenger_first_preserves_freight_end_city(monkeypatch) -> None:
    captured: dict[str, object] = {}

    monkeypatch.setattr(
        combined,
        "_build_passenger_route",
        lambda **_kwargs: {
            "city_a_id": "11",
            "city_b_id": "15",
            "trip_fatigue": 20,
        },
    )
    monkeypatch.setattr(
        combined,
        "_read_current_city",
        lambda *_args: {"city_key": "cape_city", "city_id": "11"},
    )
    monkeypatch.setattr(
        combined,
        "_passenger_forecast",
        lambda **_kwargs: {
            "expected_fatigue": 20,
            "end_city": {"city_id": "15"},
        },
    )

    async def fake_preview(**kwargs):
        captured["preview_end_city_ids"] = kwargs.get("required_end_city_ids")
        captured["preview_recovery"] = {key: value for key, value in kwargs.items()
                                        if key in RECOVERY_FIELDS or key == "base_fatigue_reserve"}
        return {"success": True, "status": "planned", "planning_status": "ok",
                "route": [{"to_city_id": "3"}]}

    async def fake_passenger(_inputs, **_kwargs):
        return {
            "success": True,
            "status": "completed",
            "requested_trips": 1,
            "completed_trips": 1,
            "requires_manual_completion": False,
            "loaded_destination": None,
            "page_state": "city_main",
            "end_city": {"city_id": "15"},
            "expected_fatigue_used": 20,
        }

    async def fake_trade(inputs, **_kwargs):
        captured["run_end_city_ids"] = inputs.get("required_end_city_ids")
        captured["run_recovery"] = {key: value for key, value in inputs.items()
                                    if key in RECOVERY_FIELDS or key == "base_fatigue_reserve"}
        return {
            "success": True,
            "status": "completed",
            "route": [{"to_city_id": "3"}],
            "selected_end_city_id": "3",
            "execution": {"completed_leg_count": 1},
            "final_sale": {"success": True, "page_state": "city_main"},
            "page_state": "city_main",
            "expected_fatigue_used": 60,
        }

    monkeypatch.setattr(combined, "_preview_trade_plan_from_start_city", fake_preview)
    monkeypatch.setattr(combined, "_run_passenger", fake_passenger)
    monkeypatch.setattr(combined, "_run_trade", fake_trade)

    service = object()
    result = asyncio.run(
        combined.resonance_pc_auto_combined_commerce_flow(
            order="passenger_first",
            total_fatigue_budget=100,
            trade_inputs={
                "available_city_ids": ["3", "11", "15"],
                "required_end_city_ids": ["3"],
                "auto_cape_island_investment": False,
                "auto_rubbish_recycling": False,
                "auto_sparkling_water": True,
                "auto_bento": False,
                **RECOVERY_FIELDS,
                "base_fatigue_reserve": 200,
            },
            passenger_inputs={
                "passenger_city_a_id": "11",
                "passenger_city_b_id": "15",
                "trip_count": 1,
            },
            app=service,
            ocr=service,
            vision=service,
            resonance_pc_city_shop_data=service,
            resonance_pc_market_data=service,
            resonance_pc_trade_planner=service,
            state_store=service,
            event_bus=service,
            context=service,
            engine=service,
        )
    )

    assert captured == {
        "preview_end_city_ids": ["3"],
        "run_end_city_ids": ["3"],
        "preview_recovery": {},
        "run_recovery": RECOVERY_FIELDS,
    }
    assert result["status"] == "completed"
    assert result["end_city_id"] == "3"


def test_recovery_whitelist_is_execution_only_and_does_not_mutate_draft():
    draft = {**RECOVERY_FIELDS, "auto_sparkling_water": True, "auto_bento": False,
             "base_fatigue_reserve": 999, "book_budget": 1}
    before = deepcopy(draft)
    execution = combined._filtered(draft, combined._TRADE_INPUT_KEYS)
    planning = combined._filtered(draft, combined._PREVIEW_INPUT_KEYS)
    assert all(execution[key] == value for key, value in RECOVERY_FIELDS.items())
    assert "base_fatigue_reserve" not in execution
    assert not set(RECOVERY_FIELDS) & planning.keys()
    assert not {"auto_sparkling_water", "auto_bento", "base_fatigue_reserve"} & planning.keys()
    assert planning["book_budget"] == 1
    assert draft == before


@pytest.mark.parametrize("order", ["trade_first", "passenger_first"])
@pytest.mark.parametrize("start,stop", [(None, None), (True, 0), (600.0, 200),
                                       (0, 0), (200, 200), (199, 200), (600, -1)])
def test_invalid_water_thresholds_block_before_any_child_runs(monkeypatch, order, start, stop):
    monkeypatch.setattr(combined, "_build_passenger_route",
                        lambda **_kwargs: pytest.fail("invalid recovery must stop before route planning"))
    service = object()
    result = asyncio.run(combined.resonance_pc_auto_combined_commerce_flow(
        order=order, total_fatigue_budget=100,
        trade_inputs={"auto_sparkling_water": True,
                      "sparkling_water_start_threshold": start,
                      "sparkling_water_stop_threshold": stop},
        app=service, ocr=service, vision=service, resonance_pc_city_shop_data=service,
        resonance_pc_market_data=service, resonance_pc_trade_planner=service,
        state_store=service, event_bus=service, context=service, engine=service))
    assert result["status"] == "blocked"
    assert result["reason"] == "invalid_trade_inputs"
    assert result["failure_stage"] == "preflight"
    assert "气泡水" in result["failure_detail"]["message"]
    assert result["trade"] is None and result["passenger"] is None


@pytest.mark.parametrize("enabled", [True, 1, "true"])
def test_combined_bento_remains_unsupported_even_with_valid_thresholds(monkeypatch, enabled):
    monkeypatch.setattr(combined, "_build_passenger_route",
                        lambda **_kwargs: pytest.fail("unsupported bento must stop before children"))
    service = object()
    result = asyncio.run(combined.resonance_pc_auto_combined_commerce_flow(
        order="passenger_first", total_fatigue_budget=100,
        trade_inputs={"auto_bento": enabled, **RECOVERY_FIELDS},
        app=service, ocr=service, vision=service, resonance_pc_city_shop_data=service,
        resonance_pc_market_data=service, resonance_pc_trade_planner=service,
        state_store=service, event_bus=service, context=service, engine=service))
    assert result["reason"] == "combined_auto_bento_unsupported"
    assert result["failure_stage"] == "preflight"
    assert result["trade"] is None and result["passenger"] is None


@pytest.mark.parametrize("legacy_order", [["bento", "sparkling_water"], ["bento", "bento"], None])
def test_obsolete_recovery_order_is_filtered_without_affecting_bento_priority(legacy_order):
    draft = {"recovery_priority": legacy_order, "bento_priority": ["love_bentos", "work_meals"],
             "auto_bento": True, **RECOVERY_FIELDS}
    execution = combined._filtered(draft, combined._TRADE_INPUT_KEYS)
    assert "recovery_priority" not in execution
    assert execution["bento_priority"] == ["love_bentos", "work_meals"]
    assert "recovery_priority" not in combined._PREVIEW_INPUT_KEYS
