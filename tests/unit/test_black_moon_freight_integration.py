"""Offline freight handoffs for the standalone weekly Black Moon task."""

import asyncio
from copy import deepcopy
import inspect
from types import MethodType, SimpleNamespace

import pytest

from packages.aura_core.context.persistence import PersistentDataService
from packages.aura_core.engine import ExecutionEngine
from plans.resonance_pc.src.actions import _black_moon_local_shop_state as ledger
from plans.resonance_pc.src.actions import city_trade_flow_pc_actions as flow


WEEK = "2026-10-12T05:00:00+08:00"
CITY = {"city_name": "Gronru", "city_key": "gronru_city"}
TABLE = {"purchase_order_book": True, "blackstone_armor": False}
KEY = {"scope": "default", "week_key": WEEK, "city_key": CITY["city_key"]}
CATALOG = {"eligible_cities": [CITY["city_key"]], "refresh_policy": {}}


class Cities:
    def __init__(self, calls):
        self.calls = calls

    def resolve_city(self, _name):
        return dict(CITY)

    def resolve_shop_point(self, city_name, shop_name):
        assert shop_name == "rest"
        self.calls.append("resolve_rest")
        return dict(CITY)


@pytest.fixture
def store(tmp_path):
    return PersistentDataService(tmp_path)


@pytest.fixture(autouse=True)
def fixed_week(monkeypatch):
    monkeypatch.setattr(ledger, "resolve_purchase_week", lambda _policy: WEEK)
    monkeypatch.setattr(flow, "_has_recovery_rest_point", lambda *_args: True)
    monkeypatch.setattr(flow, "_check_trade_cancelled", lambda: None)


def gate(store, selected=None, catalog=None):
    return flow._black_moon_purchase_gate(
        city_name=CITY["city_name"], selected_item_ids=["purchase_order_book"] if selected is None else selected,
        record_scope="default", catalog=CATALOG if catalog is None else catalog,
        city_shop_data=Cities([]), persistent_data=store,
    )


def record(store, *, complete=False, pending=False, confirmed=False):
    saved = ledger.begin_selection_visit(store, **KEY, selected_item_ids=["purchase_order_book"])
    items = [{"item_id": "purchase_order_book", "score": 0.96}]
    if pending or confirmed:
        saved = ledger.mark_pending_batch(store, **KEY, visit_id=saved["visit_id"], batch_id="batch", items=items)
    if confirmed:
        saved = ledger.confirm_batch(store, **KEY, visit_id=saved["visit_id"], batch_id="batch", evidence={
            "type": "wishlist_purchase_settled", "recognized": True,
            "purchase_applied": True, "ui_settled": True,
        })
    if complete:
        saved = ledger.complete_visit(store, **KEY, visit_id=saved["visit_id"],
            reason="wishlist_processed" if confirmed else "no_matching_goods", evidence={
                "type": "wishlist_scan", "recognized": True, "scan_complete": True,
                "end_reason": "unchanged_view", "bottom_confirmed": False,
                "scroll_count": 2, "unchanged_drag_count": 2,
                "selected_item_ids": ["purchase_order_book"],
                "has_selected_goods": confirmed,
                "matched_item_ids": ["purchase_order_book"] if confirmed else [],
            })
    return saved


def child_result(**changes):
    return {
        "success": True, "status": "completed", "page_state": "rest_menu",
        "city_key": CITY["city_key"], "week_key": WEEK, "completed_this_week": True,
        "has_selected_goods": False, "matched_item_ids": [], "reason": "no_matching_goods",
    } | changes


def test_gate_empty_ineligible_and_no_rest_do_not_create_ledger(store, monkeypatch):
    assert gate(store, []) ["reason"] == "empty_selection"
    assert gate(store, catalog={**CATALOG, "eligible_cities": []})["reason"] == "city_not_eligible"
    monkeypatch.setattr(flow, "_has_recovery_rest_point", lambda *_args: False)
    assert gate(store)["reason"] == "no_rest_point"
    assert not store.exists(ledger.PURCHASES_FILE)


def test_gate_exact_completed_snapshot_skips_but_changed_wishlist_runs(store):
    assert gate(store)["triggered"] is True
    record(store, complete=True)
    assert gate(store)["completed_this_week"] is True
    changed = gate(store, ["purchase_order_book", "blackstone_armor"])
    assert changed["triggered"] is True
    assert changed["status"] == "pending"


def test_gate_pending_payment_is_structured_failure_without_replay(store):
    record(store, pending=True)
    before = store.read(ledger.PURCHASES_FILE)
    with pytest.raises(flow.CityTradeFlowError):
        gate(store)
    assert store.read(ledger.PURCHASES_FILE) == before


def run_child(monkeypatch, framework, table=None):
    calls = []
    async def execute_task(**params):
        calls.append(deepcopy(params))
        return {"status": "SUCCESS", "user_data": framework}
    context = SimpleNamespace(data={"cid": "black-moon-freight-parent"})
    engine = SimpleNamespace(services={}, state_store=object(), orchestrator=SimpleNamespace(
        plan_name="resonance_pc", loaded_package=None,
        resolve_service=lambda key: None, execute_task=execute_task,
    ))
    engine.bind_execution = MethodType(ExecutionEngine.bind_execution, engine)
    result = asyncio.run(flow._run_black_moon_local_purchase_task(
        city_name=CITY["city_name"], city_key=CITY["city_key"],
        purchase_table=TABLE if table is None else table, record_scope="account",
        progress_context={}, context=context, engine=engine,
    ))
    return result, calls


def test_child_adapter_uses_declared_receipt_and_frozen_purchase_map(monkeypatch):
    source = dict(TABLE)
    result, calls = run_child(monkeypatch, child_result(), source)
    assert result["reason"] == "no_matching_goods"
    assert len(calls) == 1
    params = calls[0]
    assert params["task_file_path"] == "tasks/black_moon_local_purchase_pc.yaml"
    assert params["task_key"] == "black_moon_local_purchase_pc"
    assert params["parent_cid"] == "black-moon-freight-parent"
    assert params["inputs"] == {"city_name": CITY["city_name"], "purchase_table": TABLE,
                               "record_scope": "account", "exit_target": "rest_menu"}
    source["purchase_order_book"] = False
    assert params["inputs"]["purchase_table"]["purchase_order_book"] is True


@pytest.mark.parametrize("changes", [
    {"has_selected_goods": 1}, {"has_selected_goods": None}, {"has_selected_goods": True},
    {"matched_item_ids": None}, {"matched_item_ids": {}}, {"matched_item_ids": "a"},
    {"matched_item_ids": [None]}, {"matched_item_ids": ["purchase_order_book"]},
    {"has_selected_goods": True, "matched_item_ids": ["blackstone_armor"]},
    {"has_selected_goods": True, "matched_item_ids": ["purchase_order_book", "purchase_order_book"]},
    {"city_key": "other_city"}, {"page_state": "main"}, {"week_key": None},
    {"completed_this_week": 1}, {"completed_this_week": False}, {"success": False},
])
def test_child_adapter_rejects_malformed_receipt_and_wrong_endpoint(monkeypatch, changes):
    with pytest.raises(flow.CityTradeFlowError, match="Local purchase"):
        run_child(monkeypatch, child_result(**changes))


def test_child_adapter_rejects_old_monolithic_node(monkeypatch):
    with pytest.raises(flow.CityTradeFlowError):
        run_child(monkeypatch, {"nodes": {"purchase": {"output": child_result()}}})


def test_receipt_salvage_preserves_product_presence_without_card_counts(store):
    record(store, complete=True, confirmed=True)
    purchase = {"city_key": CITY["city_key"], "week_key": WEEK, "status": "failed"}
    flow._retain_black_moon_purchase_receipt(purchase, persistent_data=store, record_scope="default")
    assert purchase["completed_this_week"] is True
    assert purchase["has_selected_goods"] is True
    assert purchase["matched_item_ids"] == ["purchase_order_book"]
    assert "selected_card_count" not in purchase and "selected_items" not in purchase
    assert purchase["status"] == "failed"


@pytest.fixture
def rest_harness(monkeypatch, store):
    calls = []
    visits = []
    city_data = Cities(calls)
    async def action(name, _params, **_kwargs):
        assert name == "resonance_pc.open_city_panel_from_main"
        calls.append("open_city")
        return {"success": True, "page_state": "city_panel"}
    async def plan(selection, **kwargs):
        calls.append("plan_water")
        return {"status": "planned", "page_state": kwargs["page_state"]}
    class Session:
        def __init__(self, **_kwargs):
            calls.append("session")
        async def enter_rest(self, _point):
            calls.append("enter_rest")
        async def return_to_city(self, _reader, city_key):
            assert city_key == CITY["city_key"]
            calls.append("return_city")
    async def child(**kwargs):
        assert kwargs["purchase_table"] == TABLE
        calls.append("purchase")
        return child_result()
    async def drink(**kwargs):
        calls.append("water")
        return {"success": True, "page_state": "rest_menu", "completed_count": kwargs["drink_count"]}
    monkeypatch.setattr(flow, "_call_recovery_action", action)
    monkeypatch.setattr(flow, "_plan_water_arrival", plan)
    monkeypatch.setattr(flow, "load_sparkling_water_layout", lambda _vision: {})
    monkeypatch.setattr(flow, "SparklingWaterSession", Session)
    monkeypatch.setattr(flow, "_run_black_moon_local_purchase_task", child)
    monkeypatch.setattr(flow, "_drink_sparkling_water_from_rest_menu", drink)
    kwargs = dict(city_name=CITY["city_name"], city_index=1, page_state="city_main",
        purchase_table=dict(TABLE), record_scope="default", catalog=CATALOG,
        purchase_visits=visits, progress_context={}, water_selection=None,
        route=[], travel_costs={}, context=object(), engine=object(), app=object(),
        ocr=object(), vision=object(), city_shop_data=city_data, persistent_data=store)
    return kwargs, calls, visits


def test_no_matching_goods_restores_shared_rest_then_water_and_city(rest_harness):
    kwargs, calls, visits = rest_harness
    kwargs["water_selection"] = {"city_name": CITY["city_name"], "city_index": 1, "drink_count": 2}
    result = asyncio.run(flow._execute_freight_rest_stop(**kwargs))
    assert calls == ["plan_water", "open_city", "resolve_rest", "session", "enter_rest",
                     "purchase", "water", "return_city"]
    assert result["page_state"] == "city_panel"
    assert result["sparkling_water"]["completed_count"] == 2
    assert visits[0]["reason"] == "no_matching_goods"
    assert visits[0]["has_selected_goods"] is False
    assert visits[0]["matched_item_ids"] == []


def test_no_targets_skip_rest_ui_entirely(rest_harness):
    kwargs, calls, visits = rest_harness
    kwargs["purchase_table"] = {"purchase_order_book": False}
    result = asyncio.run(flow._execute_freight_rest_stop(**kwargs))
    assert not calls
    assert result["page_state"] == "city_main"
    assert visits[0]["reason"] == "empty_selection"


def test_purchase_failure_stops_water_and_retains_durable_receipt(rest_harness, monkeypatch, store):
    kwargs, calls, visits = rest_harness
    kwargs["water_selection"] = {"city_name": CITY["city_name"], "city_index": 1, "drink_count": 2}
    record(store, complete=True, confirmed=True)
    # Force the child path so this simulates payment persisting after gate, then exit failing.
    monkeypatch.setattr(flow, "_black_moon_purchase_gate", lambda **_kwargs: {
        "success": True, "triggered": True, "status": "pending", "week_key": WEEK,
        "city_key": CITY["city_key"], "completed_this_week": False, "has_selected_goods": False, "matched_item_ids": [],
    })
    async def failure(**_kwargs):
        calls.append("purchase_failed")
        raise flow.CityTradeFlowError("black_moon_local_purchase_incomplete", "Exit failed")
    monkeypatch.setattr(flow, "_run_black_moon_local_purchase_task", failure)
    with pytest.raises(flow.CityTradeFlowError):
        asyncio.run(flow._execute_freight_rest_stop(**kwargs))
    assert "water" not in calls and "return_city" not in calls
    assert visits[0]["success"] is False
    assert visits[0]["has_selected_goods"] is True
    assert visits[0]["matched_item_ids"] == ["purchase_order_book"]
    assert visits[0]["completed_this_week"] is True
    assert visits[0]["page_state"] == "unknown"


def test_core_freight_signature_uses_new_map_not_legacy_list():
    for function in (flow._execute_route, flow.resonance_pc_auto_cycle_trade_flow):
        names = inspect.signature(function).parameters
        assert "black_moon_purchase_table" in names
        assert "black_moon_local_purchase_items" not in names


@pytest.fixture
def route_harness(monkeypatch):
    calls = []
    async def initialize(**_kwargs):
        return {"run_key": "test_route"}
    async def leg(**kwargs):
        calls.append(("trade", kwargs["index"]))
        return {"page_state": "city_main", "travel": {"success": True, "status": "ok"}}
    async def update(**_kwargs):
        return {"status": "ok"}
    async def summary(*_args, **_kwargs):
        return {"status": "ok"}
    async def cleanup(*_args, **_kwargs):
        calls.append(("cleanup",))
    async def sleep(_seconds):
        pass
    async def rest(**kwargs):
        calls.append(("rest", kwargs["city_index"], kwargs["city_name"]))
        assert kwargs["purchase_table"] == TABLE
        result = child_result() | {"triggered": True, "city_index": kwargs["city_index"]}
        kwargs["purchase_visits"].append(result)
        return {"page_state": "city_panel", "black_moon_local_purchase": result,
                "sparkling_water": None}
    monkeypatch.setattr(flow, "resonance_pc_trade_route_execution_init", initialize)
    monkeypatch.setattr(flow, "_execute_trade_leg", leg)
    monkeypatch.setattr(flow, "resonance_pc_trade_route_execution_update", update)
    monkeypatch.setattr(flow, "resonance_pc_trade_route_execution_summary", summary)
    monkeypatch.setattr(flow, "resonance_pc_trade_route_execution_cleanup", cleanup)
    monkeypatch.setattr(flow, "_execute_freight_rest_stop", rest)
    monkeypatch.setattr(flow.asyncio, "sleep", sleep)
    kwargs = dict(route=[{"from_city": "A", "to_city": "B"}, {"from_city": "B", "to_city": "C"}],
        start_page_state="city_main", use_fatigue_medicine=False,
        allowed_fatigue_medicines=[], fatigue_medicine_max_uses=0,
        app=object(), ocr=object(), vision=object(), city_shop_data=object(), state_store=object(),
        auto_rubbish_recycling=False, auto_black_moon_local_purchase=True,
        black_moon_purchase_table=dict(TABLE), black_moon_catalog=CATALOG)
    return kwargs, calls


def test_all_route_execution_has_first_city_and_confirmed_arrival_hooks(route_harness):
    kwargs, calls = route_harness
    result = asyncio.run(flow._execute_route(**kwargs))
    assert calls == [("rest", 0, "A"), ("trade", 0), ("rest", 1, "B"),
                     ("trade", 1), ("rest", 2, "C"), ("cleanup",)]
    assert result["status"] == "completed"
    assert result["black_moon_local_purchase"]["triggered_count"] == 3


def test_disabled_purchase_has_no_rest_hook_or_extra_ui(route_harness):
    kwargs, calls = route_harness
    kwargs["auto_black_moon_local_purchase"] = False
    result = asyncio.run(flow._execute_route(**kwargs))
    assert calls == [("trade", 0), ("trade", 1), ("cleanup",)]
    assert result["status"] == "completed"
    assert result["black_moon_local_purchase"]["enabled"] is False


@pytest.mark.parametrize("failure_city_index", [0, 1])
def test_purchase_failure_stops_route_before_next_trade_and_cleans_state(route_harness, monkeypatch, failure_city_index):
    kwargs, calls = route_harness
    original = flow._execute_freight_rest_stop
    async def failure(**parameters):
        if parameters["city_index"] == failure_city_index:
            calls.append(("rest_failed", failure_city_index))
            raise flow.CityTradeFlowError("black_moon_local_purchase_incomplete", "Unknown shop endpoint")
        return await original(**parameters)
    monkeypatch.setattr(flow, "_execute_freight_rest_stop", failure)
    result = asyncio.run(flow._execute_route(**kwargs))
    assert result["status"] == "failed"
    assert result["reason"] == "black_moon_local_purchase_incomplete"
    assert result["page_state"] == "unknown"
    assert calls[-1] == ("cleanup",)
    assert ("trade", 1) not in calls
    if failure_city_index == 0:
        assert ("trade", 0) not in calls


def test_child_adapter_accepts_selected_product_presence_without_card_counts(monkeypatch):
    expected = child_result(has_selected_goods=True, matched_item_ids=["purchase_order_book"],
                            reason="wishlist_processed", purchase_verified=True)
    result, _ = run_child(monkeypatch, expected)
    assert result == expected
    assert "selected_card_count" not in result and "selected_items" not in result


def test_route_summary_unions_product_ids_across_city_visits(route_harness, monkeypatch):
    kwargs, _ = route_harness
    original = flow._execute_freight_rest_stop
    async def selected(**parameters):
        result = await original(**parameters)
        receipt = result["black_moon_local_purchase"]
        receipt.update(has_selected_goods=True, matched_item_ids=["purchase_order_book"])
        return result
    monkeypatch.setattr(flow, "_execute_freight_rest_stop", selected)
    result = asyncio.run(flow._execute_route(**kwargs))
    receipt = result["black_moon_local_purchase"]
    assert receipt["triggered_count"] == 3
    assert receipt["has_selected_goods"] is True
    assert receipt["matched_item_ids"] == ["purchase_order_book"]
    assert "selected_card_count" not in receipt and "selected_items" not in receipt


def test_receipt_salvage_does_not_claim_pending_products_were_purchased(store):
    record(store, pending=True)
    purchase = {"city_key": CITY["city_key"], "week_key": WEEK, "status": "failed"}
    flow._retain_black_moon_purchase_receipt(purchase, persistent_data=store, record_scope="default")
    assert purchase["completed_this_week"] is False
    assert purchase["has_selected_goods"] is False
    assert purchase["matched_item_ids"] == []
    assert purchase["status"] == "failed"
