from pathlib import Path

import asyncio
import inspect
import pytest
import yaml

from plans.resonance.src.actions import city_trade_flow_actions as actions


REPO_ROOT = Path(__file__).resolve().parents[1]


def _load_task_data():
    task_path = REPO_ROOT / "plans" / "resonance" / "tasks" / "auto_cycle_trade.yaml"
    return yaml.safe_load(task_path.read_text(encoding="utf-8"))


def _load_preview_task_data():
    task_path = REPO_ROOT / "plans" / "resonance" / "tasks" / "preview_trade_plan.yaml"
    return yaml.safe_load(task_path.read_text(encoding="utf-8"))


def test_auto_cycle_trade_yaml_is_single_flow_action_entrypoint():
    task_data = _load_task_data()
    task = task_data["auto_cycle_trade"]
    steps = task["steps"]
    input_names = {item["name"] for item in task["meta"]["inputs"]}

    assert set(task_data) == {"auto_cycle_trade"}
    assert list(steps) == ["run"]
    assert steps["run"]["action"] == "resonance.auto_cycle_trade_flow"
    assert {
        "fatigue_budget",
        "cargo_capacity",
        "book_budget",
        "book_profit_threshold",
        "max_cycle_hops",
        "max_rounds",
        "use_fatigue_medicine",
        "allowed_fatigue_medicines",
        "fatigue_medicine_max_uses",
    }.issubset(input_names)
    assert task["returns"]["route"] == "{{ nodes.run.output.route }}"
    assert task["returns"]["blocked_leg"] == "{{ nodes.run.output.blocked_leg }}"
    assert task["returns"]["fatigue_medicine_used"] == "{{ nodes.run.output.fatigue_medicine_used }}"


def test_auto_cycle_trade_flow_owns_planning_route_execution_and_travel():
    source = inspect.getsource(actions.resonance_auto_cycle_trade_flow)

    assert "resonance_open_city_panel_from_main" in source
    assert "resonance_read_city_name_on_city_panel" in source
    assert "resonance_trade_loop_init" in source
    assert "resonance_market_refresh" in source
    assert "force=True" in source
    assert "resonance_trade_plan_next_cycle_execution" in source
    assert "_execute_route(" in source
    assert "resonance_trade_loop_update" in source
    assert "resonance_trade_loop_summary" in source
    assert "resonance_trade_loop_cleanup" in source


def test_route_execution_reuses_existing_travel_action_and_handles_blocked():
    source = inspect.getsource(actions._execute_route)

    assert "resonance_intercity_depart_and_wait" in source
    assert 'location_file_path="data/meta/location_mumu.json"' in source
    assert "enter_station_timeout_seconds=0" in source
    assert "use_fatigue_medicine=bool(use_fatigue_medicine)" in source
    assert "resonance_trade_route_execution_update" in source
    assert "blocked" in source


def test_final_sell_is_skipped_when_flow_is_blocked():
    source = inspect.getsource(actions.resonance_auto_cycle_trade_flow)

    assert 'str(summary.get("status") or "").lower() != "blocked"' in source
    assert "_execute_city_trade_inside_current_city" in source


def test_preview_trade_plan_task_is_planning_only():
    task_data = _load_preview_task_data()
    task = task_data["preview_trade_plan"]

    assert list(task_data) == ["preview_trade_plan"]
    assert list(task["steps"]) == ["run"]
    assert task["steps"]["run"]["action"] == "resonance.preview_trade_plan_flow"
    inputs = {item["name"]: item for item in task["meta"]["inputs"]}
    assert inputs["start_city_id"]["required"] is True
    assert "refresh_market" not in inputs
    assert "use_fatigue_medicine" not in inputs
    assert task["returns"]["preview"] == "{{ nodes.run.output.preview }}"


def test_preview_flow_refreshes_and_plans_from_user_city_without_game_services(monkeypatch):
    calls = []
    monkeypatch.setattr(
        actions,
        "resonance_market_refresh",
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
        "resonance_trade_plan_optimal_route",
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
        actions.resonance_preview_trade_plan_flow(
            start_city_id="3",
            resonance_market_data=object(),
            resonance_trade_planner=object(),
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

    source = inspect.getsource(actions.resonance_preview_trade_plan_flow)
    assert "resonance_open_city_panel_from_main" not in source
    assert "resonance_read_city_name_on_city_panel" not in source
    assert "resonance_go_city_main_direct" not in source
    assert "_execute_route" not in source


@pytest.mark.parametrize(("all_plan", "message"), [(2, "all_plan must be 0 or 1"), (0.5, "all_plan must be an integer")])
def test_preview_flow_validates_all_plan_before_services(all_plan, message):
    with pytest.raises(ValueError, match=message):
        asyncio.run(actions.resonance_preview_trade_plan_flow(start_city_id="3", all_plan=all_plan))
