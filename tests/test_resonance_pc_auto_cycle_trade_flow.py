from pathlib import Path

import inspect
import yaml
import pytest

from packages.aura_core.context.execution import ExecutionContext
from plans.resonance_pc.src.actions import city_trade_flow_pc_actions as actions


REPO_ROOT = Path(__file__).resolve().parents[1]


def _load_task_data():
    task_path = REPO_ROOT / "plans" / "resonance_pc" / "tasks" / "auto_cycle_trade_pc.yaml"
    return yaml.safe_load(task_path.read_text(encoding="utf-8"))


def test_auto_cycle_trade_yaml_is_single_flow_action_entrypoint():
    task_data = _load_task_data()
    task = task_data["auto_cycle_trade_pc"]
    steps = task["steps"]
    input_names = {item["name"] for item in task["meta"]["inputs"]}

    assert set(task_data) == {"auto_cycle_trade_pc"}
    assert list(steps) == ["run"]
    assert steps["run"]["action"] == "resonance_pc.auto_cycle_trade_flow"
    assert {
        "all_plan",
        "fatigue_budget",
        "cargo_capacity",
        "book_budget",
        "book_profit_threshold",
        "negotiation_budget",
        "bargain_success_rates_bps",
        "bargain_step_bps",
        "raise_success_rates_bps",
        "raise_step_bps",
        "trade_level",
        "available_city_ids",
        "city_prestige",
        "product_unlocks",
        "active_events",
        "use_fatigue_medicine",
        "allowed_fatigue_medicines",
        "fatigue_medicine_max_uses",
    }.issubset(input_names)
    assert task["returns"]["route"] == "{{ nodes.run.output.route }}"
    assert task["returns"]["blocked_leg"] == "{{ nodes.run.output.blocked_leg }}"
    assert task["returns"]["fatigue_medicine_used"] == "{{ nodes.run.output.fatigue_medicine_used }}"


def test_auto_cycle_trade_flow_owns_planning_route_execution_and_travel():
    source = inspect.getsource(actions.resonance_pc_auto_cycle_trade_flow)

    assert "resonance_pc_open_city_panel_from_main" in source
    assert "resonance_pc_read_city_name_on_city_panel" in source
    assert "resonance_pc_market_refresh" in source
    assert "force=True" in source
    assert "resonance_pc_trade_plan_optimal_route" in source
    assert "_execute_route(" in source
    assert "while " not in source
    assert "resonance_pc_trade_loop_" not in source


def test_route_execution_reuses_existing_travel_action_and_handles_blocked():
    leg_source = inspect.getsource(actions._execute_trade_leg)
    route_source = inspect.getsource(actions._execute_route)

    assert "resonance_pc_intercity_depart_and_wait" in leg_source
    assert 'location_file_path="data/meta/location_pc.json"' in leg_source
    assert "enter_station_timeout_seconds=0" in leg_source
    assert "use_fatigue_medicine=bool(use_fatigue_medicine)" in leg_source
    assert "resonance_pc_trade_route_execution_update" in route_source
    assert "blocked" in route_source


def test_final_sell_is_skipped_when_flow_is_blocked():
    source = inspect.getsource(actions.resonance_pc_auto_cycle_trade_flow)

    assert 'str(execution.get("status") or "").lower() != "blocked"' in source
    assert "_execute_city_trade_inside_current_city" in source


@pytest.mark.parametrize(("all_plan", "negotiation_budget"), [(0, 1), (1, 0)])
def test_negotiation_capable_modes_reach_the_normal_service_boundary(all_plan, negotiation_budget):
    import asyncio

    with pytest.raises(RuntimeError, match="requires app/ocr/vision/controller"):
        asyncio.run(
            actions.resonance_pc_auto_cycle_trade_flow(
                all_plan=all_plan,
                negotiation_budget=negotiation_budget,
            )
        )

    source = inspect.getsource(actions.resonance_pc_auto_cycle_trade_flow)
    assert "negotiation_execution_not_implemented" not in source


def test_auto_flow_validates_binary_profile_before_services_or_ui():
    import asyncio

    with pytest.raises(ValueError, match="must be an integer"):
        asyncio.run(
            actions.resonance_pc_auto_cycle_trade_flow(
                all_plan=1,
                bargain_success_rates_bps=[5000.5],
            )
        )


def test_auto_flow_publishes_structured_failure_progress_with_current_cid():
    import asyncio

    class FakeEventBus:
        def __init__(self):
            self.events = []

        async def publish(self, event):
            self.events.append(event.to_dict())

    event_bus = FakeEventBus()
    context = ExecutionContext(cid="cid-progress")

    with pytest.raises(RuntimeError, match="requires app/ocr/vision/controller"):
        asyncio.run(
            actions.resonance_pc_auto_cycle_trade_flow(
                event_bus=event_bus,
                context=context,
            )
        )

    assert [event["payload"]["state"] for event in event_bus.events] == ["started", "failed"]
    assert all(event["name"] == "task.resonance_pc_trade_progress" for event in event_bus.events)
    assert all(event["payload"]["cid"] == "cid-progress" for event in event_bus.events)
    assert [event["payload"]["sequence"] for event in event_bus.events] == [1, 2]


def test_preview_flow_refreshes_and_plans_from_user_city_without_game_services(monkeypatch):
    import asyncio

    calls = []
    monkeypatch.setattr(
        actions,
        "resonance_pc_market_refresh",
        lambda **kwargs: calls.append("market")
        or {
            "snapshot_id": "snap-preview",
            "fetched_at": "2026-07-21T00:00:00Z",
            "stale": False,
            "cities": {"3": {"name": "七号自由港"}},
        },
    )
    monkeypatch.setattr(
        actions,
        "resonance_pc_trade_plan_optimal_route",
        lambda **kwargs: calls.append(("plan", kwargs["current_city_id"]))
        or {
            "status": "ok",
            "snapshot_id": "snap-preview",
            "route": [{"from_city": "七号自由港", "to_city": "修格里城"}],
            "expected_profit": 1200,
            "expected_fatigue_used": 30,
        },
    )
    monkeypatch.setattr(
        actions,
        "_execute_route",
        lambda **kwargs: (_ for _ in ()).throw(AssertionError("preview must not execute route")),
    )

    result = asyncio.run(
        actions.resonance_pc_preview_trade_plan_flow(
            start_city_id="3",
            resonance_pc_market_data=object(),
            resonance_pc_trade_planner=object(),
        )
    )

    assert calls == ["market", ("plan", "3")]
    assert result["preview"] is True
    assert result["market_refreshed"] is True
    assert result["market_source"] == "refresh"
    assert result["initial_city"] == {
        "city_id": "3",
        "city_name": "七号自由港",
        "source": "user_input",
    }
    assert result["page_state"] == "not_applicable"
    assert result["expected_profit"] == 1200

    source = inspect.getsource(actions.resonance_pc_preview_trade_plan_flow)
    assert "resonance_pc_open_city_panel_from_main" not in source
    assert "resonance_pc_read_city_name_on_city_panel" not in source
    assert "resonance_pc_go_city_main_direct" not in source
    assert "_execute_route" not in source
