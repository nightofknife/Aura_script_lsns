"""Smoke checks for combined-commerce freight endpoint handling."""

from __future__ import annotations

import asyncio

from plans.resonance_pc.src.actions import combined_commerce_pc_actions as combined


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
        return {"status": "ok", "route": [{"to_city_id": "3"}]}

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
    }
    assert result["status"] == "completed"
    assert result["end_city_id"] == "3"
