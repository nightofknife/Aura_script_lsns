from __future__ import annotations

import asyncio
import inspect
from pathlib import Path
from unittest.mock import Mock

import pytest
import yaml

from packages.aura_core.config.validator import validate_task_definition
from plans.resonance.src.actions import city_trade_flow_actions as actions


REPO_ROOT = Path(__file__).resolve().parents[1]


def _run(coro):
    return asyncio.run(coro)


def test_exact_trade_task_is_parallel_entrypoint_and_valid():
    task_path = REPO_ROOT / "plans" / "resonance" / "tasks" / "auto_cycle_trade_exact.yaml"
    payload = yaml.safe_load(task_path.read_text(encoding="utf-8"))
    task = payload["auto_cycle_trade_exact"]

    ok, error = validate_task_definition(payload)

    assert ok is True, error
    assert list(task["steps"]) == ["run"]
    assert task["steps"]["run"]["action"] == "resonance.auto_cycle_trade_exact_flow"
    assert task["meta"]["entry_point"] is True
    assert "negotiation_budget" in {item["name"] for item in task["meta"]["inputs"]}


def test_legacy_and_exact_trade_actions_remain_separate():
    legacy_source = inspect.getsource(actions.resonance_auto_cycle_trade_flow)
    exact_source = inspect.getsource(actions.resonance_auto_cycle_trade_exact_flow)

    assert "resonance_trade_plan_next_cycle_execution" in legacy_source
    assert "resonance_trade_plan_optimal_route" not in legacy_source
    assert "resonance_trade_plan_optimal_route" in exact_source
    assert "_execute_exact_route" in exact_source


def test_exact_route_uses_emulator_travel_and_preserves_mumu_encounter_path():
    source = inspect.getsource(actions._execute_exact_trade_leg)

    assert "resonance_intercity_depart_and_wait" in source
    assert 'location_file_path="data/meta/location_mumu.json"' in source
    assert "location_pc.json" not in source
    assert "use_fatigue_medicine=bool(use_fatigue_medicine)" in source


def test_exact_flow_validates_binary_profile_before_services():
    with pytest.raises(ValueError, match="all_plan must be 0 or 1"):
        _run(actions.resonance_auto_cycle_trade_exact_flow(all_plan=2))

    with pytest.raises(ValueError, match="must be an integer"):
        _run(actions.resonance_auto_cycle_trade_exact_flow(negotiation_budget=0.5))


def test_exact_trade_leg_forwards_negotiation_flags_and_uses_emulator_travel(monkeypatch):
    city_trade_calls = []
    travel_calls = []

    monkeypatch.setattr(
        actions,
        "_execute_city_trade_inside_current_city",
        lambda **kwargs: city_trade_calls.append(kwargs)
        or {"success": True, "page_state": "city_main"},
    )
    monkeypatch.setattr(
        actions,
        "resonance_intercity_depart_and_wait",
        lambda **kwargs: travel_calls.append(kwargs)
        or {"status": "ok", "fatigue_medicine_used": []},
    )

    result = _run(
        actions._execute_exact_trade_leg(
            index=1,
            leg={
                "from_city": "七号自由港",
                "to_city": "修格里城",
                "buy_products": ["发动机"],
                "books_used": 2,
                "bargain_to_cap": True,
            },
            sell_raise_to_cap=True,
            page_state="city_panel",
            use_fatigue_medicine=True,
            allowed_fatigue_medicines=["提神剂"],
            fatigue_medicine_max_uses=3,
            app=object(),
            ocr=object(),
            vision=object(),
            controller=object(),
            city_shop_data=object(),
        )
    )

    assert city_trade_calls[0]["buy_bargain_to_cap"] is True
    assert city_trade_calls[0]["sell_raise_to_cap"] is True
    assert city_trade_calls[0]["books_used"] == 2
    assert travel_calls[0]["location_file_path"] == "data/meta/location_mumu.json"
    assert travel_calls[0]["use_fatigue_medicine"] is True
    assert result["page_state"] == "city_main"


def test_city_trade_rejects_bargain_without_buy_products():
    with pytest.raises(actions.CityTradeFlowError) as exc_info:
        actions._execute_city_trade_inside_current_city(
            current_city="七号自由港",
            buy_products=[],
            books_used=0,
            buy_bargain_to_cap=True,
            app=object(),
            ocr=object(),
            vision=object(),
            controller=object(),
            city_shop_data=object(),
        )

    assert exc_info.value.code == "negotiation_without_selected_goods"


def test_buy_negotiates_after_selection_and_before_confirmation(monkeypatch):
    events = []
    monkeypatch.setattr(actions.time, "sleep", lambda _seconds: None)
    monkeypatch.setattr(
        actions,
        "_capture_text_items",
        lambda *_args, **_kwargs: [{"text": "A1", "norm_text": "a1", "kind": "product"}],
    )
    monkeypatch.setattr(
        actions,
        "_click_hit",
        lambda _app, hit: events.append(("click", hit["kind"])) or {"clicked": True},
    )
    monkeypatch.setattr(
        actions,
        "execute_bargain_to_cap",
        lambda **kwargs: events.append(("bargain", kwargs["requested_to_cap"]))
        or {"requested_to_cap": kwargs["requested_to_cap"], "completed_to_cap": kwargs["requested_to_cap"]},
    )
    monkeypatch.setattr(actions, "_wait_for_text_hit", lambda *_args, **_kwargs: {"kind": "buy"})
    monkeypatch.setattr(actions, "_close_settlement", lambda *_args, **_kwargs: {"closed": True})

    result = actions.resonance_buy_goods_on_buy_page(
        product_list=["A1"],
        bargain_to_cap=True,
        app=object(),
        ocr=object(),
        vision=object(),
        controller=object(),
    )

    assert events == [
        ("bargain", False),
        ("click", "product"),
        ("bargain", True),
        ("click", "buy"),
    ]
    assert result["negotiation"]["completed_to_cap"] is True


def test_buy_stops_before_confirmation_when_negotiation_fails(monkeypatch):
    monkeypatch.setattr(actions.time, "sleep", lambda _seconds: None)
    monkeypatch.setattr(
        actions,
        "_capture_text_items",
        lambda *_args, **_kwargs: [{"text": "A1", "norm_text": "a1"}],
    )
    monkeypatch.setattr(actions, "_click_hit", lambda *_args, **_kwargs: {"clicked": True})

    def bargain(**kwargs):
        if kwargs["requested_to_cap"]:
            raise actions.NegotiationExecutionError("negotiation_page_lost", "lost")
        return {"requested_to_cap": False, "completed_to_cap": False}

    monkeypatch.setattr(actions, "execute_bargain_to_cap", bargain)
    monkeypatch.setattr(
        actions,
        "_wait_for_text_hit",
        lambda *_args, **_kwargs: pytest.fail("buy confirmation must not run after negotiation failure"),
    )

    with pytest.raises(actions.CityTradeFlowError) as exc_info:
        actions.resonance_buy_goods_on_buy_page(
            product_list=["A1"],
            bargain_to_cap=True,
            app=object(),
            ocr=object(),
            vision=object(),
            controller=object(),
        )

    assert exc_info.value.code == "negotiation_page_lost"


def test_sell_negotiates_after_sell_all_and_before_confirmation(monkeypatch):
    events = []
    monkeypatch.setattr(actions.time, "sleep", lambda _seconds: None)

    def click_text(_app, _ocr, texts, _region, **_kwargs):
        event = "sell_all" if "全部卖出" in texts else "sell"
        events.append(("click", event))
        return {"clicked": True}

    monkeypatch.setattr(actions, "_wait_and_click_text", click_text)
    monkeypatch.setattr(
        actions,
        "execute_raise_to_cap",
        lambda **kwargs: events.append(("raise", kwargs["requested_to_cap"]))
        or {"requested_to_cap": kwargs["requested_to_cap"], "completed_to_cap": kwargs["requested_to_cap"]},
    )
    monkeypatch.setattr(actions, "_close_settlement", lambda *_args, **_kwargs: {"closed": True})

    result = actions.resonance_sell_goods_on_sell_page(
        raise_to_cap=True,
        app=object(),
        ocr=object(),
        vision=object(),
    )

    assert events == [
        ("click", "sell_all"),
        ("raise", False),
        ("raise", True),
        ("click", "sell"),
    ]
    assert result["negotiation"]["completed_to_cap"] is True


def test_exact_flow_does_not_finalize_sale_after_blocked_execution(monkeypatch):
    service = object()
    final_trade = Mock(side_effect=AssertionError("final sale must not run after blocked execution"))

    monkeypatch.setattr(actions, "resonance_open_city_panel_from_main", lambda **kwargs: {"success": True})
    monkeypatch.setattr(
        actions,
        "resonance_read_city_name_on_city_panel",
        lambda **kwargs: {
            "city_name": "七号自由港",
            "city_key": "freeport",
            "ocr_city_text": "七号自由港",
        },
    )
    monkeypatch.setattr(
        actions,
        "resonance_market_refresh",
        lambda **kwargs: {"snapshot_id": "snapshot-exact"},
    )
    monkeypatch.setattr(
        actions,
        "resonance_trade_plan_optimal_route",
        lambda **kwargs: {
            "status": "ok",
            "route": [
                {
                    "from_city": "七号自由港",
                    "to_city": "修格里城",
                    "buy_products": ["发动机"],
                }
            ],
        },
    )
    monkeypatch.setattr(
        actions,
        "_execute_exact_route",
        lambda **kwargs: asyncio.sleep(
            0,
            result={
                "status": "blocked",
                "reason": "encounter_failed",
                "page_state": "city_main",
                "blocked_at": "travel",
                "blocked_leg": kwargs["route"][0],
                "fatigue_medicine_used": [],
                "fatigue_medicine_use_count": 0,
            },
        ),
    )
    monkeypatch.setattr(actions, "_execute_city_trade_inside_current_city", final_trade)

    result = _run(
        actions.resonance_auto_cycle_trade_exact_flow(
            app=service,
            ocr=service,
            vision=service,
            controller=service,
            resonance_city_shop_data=service,
            resonance_market_data=service,
            resonance_trade_planner=service,
            state_store=service,
        )
    )

    assert result["success"] is False
    assert result["status"] == "blocked"
    assert result["reason"] == "encounter_failed"
    assert result["final_sale"] is None
    final_trade.assert_not_called()


def test_exact_flow_cleans_up_city_panel_when_planner_has_no_route(monkeypatch):
    service = object()
    cleanup_calls = []

    monkeypatch.setattr(actions, "resonance_open_city_panel_from_main", lambda **kwargs: {"success": True})
    monkeypatch.setattr(
        actions,
        "resonance_read_city_name_on_city_panel",
        lambda **kwargs: {
            "city_name": "七号自由港",
            "city_key": "freeport",
            "ocr_city_text": "七号自由港",
        },
    )
    monkeypatch.setattr(actions, "resonance_market_refresh", lambda **kwargs: {"snapshot_id": "snap"})
    monkeypatch.setattr(
        actions,
        "resonance_trade_plan_optimal_route",
        lambda **kwargs: {"status": "no_plan", "reason": "no_profitable_route", "route": []},
    )
    monkeypatch.setattr(
        actions,
        "resonance_go_city_main_direct",
        lambda **kwargs: cleanup_calls.append(kwargs) or {"page_state": "city_main"},
    )

    result = _run(
        actions.resonance_auto_cycle_trade_exact_flow(
            app=service,
            ocr=service,
            vision=service,
            controller=service,
            resonance_city_shop_data=service,
            resonance_market_data=service,
            resonance_trade_planner=service,
            state_store=service,
        )
    )

    assert result["success"] is True
    assert result["status"] == "no_plan"
    assert result["reason"] == "no_profitable_route"
    assert result["page_state"] == "city_main"
    assert len(cleanup_calls) == 1
