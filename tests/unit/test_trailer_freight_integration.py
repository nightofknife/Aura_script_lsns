"""Full freight orchestration with real leg dispatch and offline travel results."""

from copy import deepcopy
import inspect
from pathlib import Path

from jinja2.nativetypes import NativeEnvironment
import pytest
import yaml

from packages.aura_core.scheduler.validation import InputValidator
from plans.resonance_pc.src.actions import city_trade_flow_pc_actions as trade
from plans.resonance_pc.src.actions import combined_commerce_pc_actions as combined
from plans.resonance_pc.src.actions import _freight_trailer_policy as policy
from tests.unit.test_resonance_pc_sparkling_water_trade import harness, run_full
from tests.unit.test_trailer_freight_policy import skip


REAL_LEG = trade._execute_trade_leg
NAMES = {"15": "岚心城", "11": "海角城", "3": "七号自由港"}
TABLE = {"15": {"11": 100}, "11": {"15": 100, "3": 400}, "3": {"11": 400}}
OPTIONS = {"auto_trailer": True, "trailer_daily_first_n": 3, "trailer_payment": "mileage"}


def route():
    return [{"from_city_id": a, "to_city_id": b, "from_city": NAMES[a], "to_city": NAMES[b],
             "buy_products": [], "raise_to_cap": False}
            for a, b in [("15", "11"), ("11", "3"), ("3", "11"), ("11", "15")]]


@pytest.fixture
def freight(harness, monkeypatch):
    calls = []
    monkeypatch.setattr(trade, "_execute_trade_leg", REAL_LEG)
    monkeypatch.setattr(trade, "resonance_pc_trade_plan_optimal_route", lambda **kwargs: {"status": "ok", "route": route()})
    monkeypatch.setattr(policy, "_distances", lambda: deepcopy(TABLE))
    def travel(**kwargs):
        calls.append(kwargs)
        result = {"success": True, "status": "ok", "arrival_status": "arrived"}
        if kwargs.get("use_trailer"):
            result["trailer_service"] = {"success": True, "status": "departed", "submitted": True,
                                         "departed": True, "page_state": "traveling"}
        return result
    monkeypatch.setattr(trade, "resonance_pc_intercity_depart_and_wait", travel)
    return calls


@pytest.mark.parametrize("mode", ["free", "fixed"])
def test_enabled_runtime_marks_longest_x_and_passes_payment_only_to_selected_legs(freight, mode):
    result = run_full(auto_sparkling_water=False, trade_mode=mode, fixed_route_city_ids=["15", "11", "15"], **OPTIONS)
    assert result["success"] and result["status"] == "completed"
    assert [index for index, call in enumerate(freight) if call.get("use_trailer")] == [0, 1, 2]
    assert all(call["trailer_payment"] == "mileage" and call["trailer_daily_first_n"] == 3
               for call in freight[:3])
    assert "trailer_payment" not in freight[3]
    selected = result["trailer_plan"]["selected_legs"]
    assert [leg["distance_km"] for leg in selected] == [100, 400, 400]
    assert len(result["execution"]["trailer_service"]["attempts"]) == 3


@pytest.mark.parametrize("reason", ["quota_exhausted", "daily_limit_reached"])
def test_first_ineligible_selected_leg_turns_off_all_later_candidates(freight, monkeypatch, reason):
    def travel(**kwargs):
        freight.append(kwargs)
        return {"success": True, "status": "ok", "arrival_status": "arrived",
                **({"trailer_service": skip(reason)} if kwargs.get("use_trailer") else {})}
    monkeypatch.setattr(trade, "resonance_pc_intercity_depart_and_wait", travel)
    result = run_full(auto_sparkling_water=False, **OPTIONS)
    assert result["success"] and len(freight) == 4
    assert [index for index, call in enumerate(freight) if call.get("use_trailer")] == [0]
    assert result["execution"]["trailer_service"]["disabled_reason"] == reason
    assert len(result["execution"]["trailer_service"]["attempts"]) == 1


def test_short_early_leg_does_not_query_quota_and_no_replacement_after_exhaustion(freight, monkeypatch):
    def travel(**kwargs):
        freight.append(kwargs)
        return {"success": True, "status": "ok",
                **({"trailer_service": skip()} if kwargs.get("use_trailer") else {})}
    monkeypatch.setattr(trade, "resonance_pc_intercity_depart_and_wait", travel)
    result = run_full(auto_sparkling_water=False, auto_trailer=True, trailer_daily_first_n=2)
    assert result["success"]
    assert [index for index, call in enumerate(freight) if call.get("use_trailer")] == [1]
    assert [leg["leg_index"] for leg in result["trailer_plan"]["selected_legs"]] == [1, 2]


def test_unconfirmed_towing_stops_later_trade_and_final_sale(freight, monkeypatch):
    def travel(**kwargs):
        freight.append(kwargs)
        return {"success": False, "status": "blocked", "reason": "trailer_departure_unconfirmed",
                "page_state": "unknown", "trailer_service": {"success": False, "status": "blocked",
                "reason": "trailer_departure_unconfirmed", "submitted": True, "departed": False,
                "page_state": "unknown"}}
    monkeypatch.setattr(trade, "resonance_pc_intercity_depart_and_wait", travel)
    result = run_full(auto_sparkling_water=False, **OPTIONS)
    assert not result["success"] and result["status"] == "blocked"
    assert len(freight) == 1 and result["final_sale"] is None and result["page_state"] == "unknown"


def test_disabled_does_not_load_distances_or_add_towing_travel_arguments(freight, monkeypatch):
    monkeypatch.setattr(policy, "_distances", lambda: pytest.fail("No distance loading when disabled"))
    result = run_full(auto_sparkling_water=False)
    assert result["success"] and not result["trailer_plan"]["enabled"]
    assert all("use_trailer" not in kwargs for kwargs in freight)


def test_reposition_quota_skip_is_shared_with_remaining_trade_route(freight, harness, monkeypatch):
    _, state = harness
    state["city"] = {"success": True, "city_name": NAMES["11"], "city_key": "cape_city"}
    prefix = {"from_city_id": "11", "to_city_id": "15", "from_city": NAMES["11"], "to_city": NAMES["15"]}
    monkeypatch.setattr(trade, "resonance_pc_trade_plan_optimal_route", lambda **kwargs:
                        {"status": "ok", "route": route(), "reposition_route": [prefix]})
    monkeypatch.setattr(trade, "resonance_pc_go_city_main_direct", lambda **kwargs:
                        {"success": True, "page_state": "city_main"})
    def travel(**kwargs):
        freight.append(kwargs)
        state["city"] = {"success": True, "city_name": kwargs["to_city_name"], "city_key": "lanxin_city"}
        return {"success": True, "status": "ok",
                **({"trailer_service": skip()} if kwargs.get("use_trailer") else {})}
    monkeypatch.setattr(trade, "resonance_pc_intercity_depart_and_wait", travel)
    result = run_full(auto_sparkling_water=False, trade_mode="fixed", reposition_to_route=True,
                      fixed_route_city_ids=["15", "11", "15"], **OPTIONS)
    assert result["success"] and len(freight) == 5
    assert [index for index, call in enumerate(freight) if call.get("use_trailer")] == [0]
    assert result["trailer_plan"]["selected_legs"][0]["phase"] == "reposition"
    assert result["execution"]["trailer_service"]["disabled_reason"] == "daily_limit_reached"


@pytest.mark.parametrize("field,value", [("auto_trailer", 1), ("trailer_daily_first_n", True),
                                        ("trailer_daily_first_n", 0), ("trailer_daily_first_n", 1.5),
                                        ("trailer_payment", "other")])
def test_public_freight_rejects_invalid_configuration_before_game(harness, field, value):
    with pytest.raises(ValueError):
        run_full(**{field: value})
    assert harness[0] == []


def test_combined_forwards_to_freight_only_and_never_planner_preview():
    assert combined._filtered(OPTIONS, combined._TRADE_INPUT_KEYS) == OPTIONS
    assert combined._filtered(OPTIONS, combined._PASSENGER_INPUT_KEYS) == {}
    assert combined._filtered(OPTIONS, combined._PREVIEW_INPUT_KEYS) == {}


def test_task_schemas_and_rendered_action_share_contract():
    tasks = Path(__file__).resolve().parents[2] / "plans/resonance_pc/tasks"
    task = yaml.safe_load((tasks / "auto_cycle_trade_pc.yaml").read_text("utf-8"))["auto_cycle_trade_pc"]
    ok, inputs = InputValidator(None).validate_inputs_against_meta(task["meta"]["inputs"], OPTIONS)
    assert ok
    rendered = {key: NativeEnvironment().from_string(value).render(inputs=inputs)
                for key, value in task["steps"]["run"]["params"].items()}
    inspect.signature(trade.resonance_pc_auto_cycle_trade_flow).bind(**rendered)
    assert {key: rendered[key] for key in OPTIONS} == OPTIONS
    nested = yaml.safe_load((tasks / "auto_combined_commerce_pc.yaml").read_text("utf-8"))["auto_combined_commerce_pc"]
    trade_schema = next(row for row in nested["meta"]["inputs"] if row["name"] == "trade_inputs")
    ok, value, _ = InputValidator(None).validate_input_value(trade_schema, OPTIONS, "trade_inputs")
    assert ok and {key: value[key] for key in OPTIONS} == OPTIONS
    for bad in (True, 0, -1, "3", 1.5):
        invalid = {**OPTIONS, "trailer_daily_first_n": bad}
        assert not InputValidator(None).validate_inputs_against_meta(task["meta"]["inputs"], invalid)[0]
        assert not InputValidator(None).validate_input_value(trade_schema, invalid, "trade_inputs")[0]
