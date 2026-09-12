"""Offline freight orchestration contracts; no game, GUI or backend execution."""
from __future__ import annotations

import asyncio
from copy import deepcopy
import threading
from types import SimpleNamespace

import pytest

from packages.aura_core.api import ACTION_REGISTRY, ActionDefinition
from packages.aura_core.engine import action_injector
from packages.aura_core.scheduler.cancellation import (
    clear_task_cancel, has_pending_sync_actions,
)
from plans.resonance_pc.src.actions import city_trade_flow_pc_actions as trade
from plans.resonance_pc.src.actions import combined_commerce_pc_actions as combined
from tests.unit.test_resonance_pc_sparkling_water_handoff import harness as combined_harness
from tests.unit.test_resonance_pc_sparkling_water_trade import (
    harness as trade_harness, routes, run_full, snapshot,
)


CALL_RECOVERY_ACTION = trade._call_recovery_action
MEALS = [
    {"kind": "work_meals", "issue_time": "05:00"},
    {"kind": "work_meals", "issue_time": "12:00"},
]


class MemoryStore:
    def __init__(self):
        self.reads = []
        self.document = snapshot(remaining=2)
        self.document["recovery"]["work_meals"] = {
            "available_count": 2, "updated_at": "2026-09-12T05:00:00Z",
            "slots": [{"issue_time": time, "available": time != "18:00"}
                      for time in ("05:00", "12:00", "18:00")],
        }
        # Deliberately unusable: meals-only must not inspect disabled types.
        self.document["recovery"]["love_bentos"] = {"requires_refresh": True}

    def read(self, *, file):
        assert file == "user-info.json"
        self.reads.append(file)
        return deepcopy(self.document)


@pytest.fixture
def recovery_runtime(monkeypatch):
    calls = []
    state = {"fatigue": 300, "consume_result": {
        "success": True, "status": "completed", "page_state": "city_main",
        "consumed_count": 2, "completed_count": 2,
    }}
    context = SimpleNamespace(data={})
    store = MemoryStore()
    package = SimpleNamespace(package=SimpleNamespace(canonical_id="@test/recovery"))
    engine = SimpleNamespace(state_store=object(), services={}, orchestrator=SimpleNamespace(
        loaded_package=package, resolve_service=lambda key: store,
    ))

    class Renderer:
        def __init__(self, actual_context, state_store):
            assert actual_context is context
            assert state_store is engine.state_store

        async def get_render_scope(self):
            return {}

        async def render(self, params, *, scope):
            return deepcopy(params)

    def register(name, function, dependencies=None):
        definition = ActionDefinition(
            func=function, name=name, read_only=False, public=True,
            service_deps=dependencies or {}, plugin=package,
        )
        monkeypatch.setitem(ACTION_REGISTRY._actions_by_fqid, definition.fqid, definition)

    def refresh(stages, profile_sections, context, engine):
        assert context is runtime.context and engine is runtime.engine
        calls.append(("refresh", {"stages": stages, "profile_sections": profile_sections}))
        assert stages == ["profile"] and profile_sections == ["fatigue"]
        return deepcopy(state.get("refresh_result", {
            "status": {"fatigue": {"current": state["fatigue"], "max": 856}},
            "metadata": {"persisted": True, "executed_profile_sections": ["fatigue"]},
        }))

    def go_main():
        calls.append(("go_main", {}))
        return state.get("main_result", {"success": True, "page_state": "city_main"})

    def open_panel(**params):
        assert params == {}
        calls.append(("open_panel", params))
        return deepcopy(state.get("panel_result", {"success": True, "page_state": "city_panel"}))

    def consume(meals, persistent_data):
        assert persistent_data is store
        calls.append(("consume", {"meals": deepcopy(meals)}))
        return deepcopy(state["consume_result"])

    monkeypatch.setattr(trade, "TemplateRenderer", Renderer)
    register("resonance_pc.player_data_refresh", refresh)
    register("resonance_pc.go_city_main_direct", go_main)
    register("resonance_pc.open_city_panel_from_main", open_panel)
    register("resonance_pc.consume_bentos", consume, {"persistent_data": "core/persistent_data"})
    runtime = SimpleNamespace(calls=calls, state=state, context=context, engine=engine,
                              store=store, register=register)
    return runtime


@pytest.fixture
def freight(trade_harness, recovery_runtime, monkeypatch):
    operations, state = trade_harness
    runtime = recovery_runtime
    monkeypatch.setattr(trade, "_call_recovery_action", CALL_RECOVERY_ACTION)
    from plans.resonance_pc.src.actions._player_data_persistence import load_pc_user_info
    monkeypatch.setattr(trade, "load_pc_user_info", load_pc_user_info)
    original_consume = ACTION_REGISTRY.get("test/recovery/resonance_pc.consume_bentos").func

    def consume(meals, persistent_data):
        operations.append("bentos")
        return original_consume(meals, persistent_data)

    runtime.register("resonance_pc.consume_bentos", consume,
                     {"persistent_data": "core/persistent_data"})
    planned = {"status": "ok", "route": routes(), "expected_profit": 123456,
               "expected_fatigue_used": 333, "remaining_expected_fatigue": 567}
    planner_calls = []

    def planner(**kwargs):
        planner_calls.append(kwargs)
        return deepcopy(planned)

    monkeypatch.setattr(trade, "resonance_pc_trade_plan_optimal_route", planner)
    monkeypatch.setattr(trade, "resonance_pc_go_city_main_direct",
                        lambda **kwargs: {"success": True, "page_state": "city_main"})

    def run(**kwargs):
        args = dict(auto_sparkling_water=False, auto_bento=True,
                    bento_priority=["work_meals"], base_fatigue_reserve=200,
                    recovery_snapshot=None, context=runtime.context, engine=runtime.engine,
                    persistent_data=runtime.store, fatigue_budget=900)
        args.update(kwargs)
        return run_full(**args)

    return SimpleNamespace(run=run, operations=operations, state=state,
                           runtime=runtime, planned=planned, planner_calls=planner_calls)


def terminal(runtime, **overrides):
    args = dict(page_state="city_main", reserve=200, priority=["work_meals"],
                context=runtime.context, engine=runtime.engine,
                persistent_data=runtime.store, current_city="City 15", city_index=2)
    args.update(overrides)
    return asyncio.run(trade._execute_terminal_bentos(**args))


@pytest.mark.parametrize("page", ["city_main", "city_panel"])
def test_fatigue_only_registered_action_from_confirmed_pages(recovery_runtime, page):
    runtime = recovery_runtime
    result = asyncio.run(trade._refresh_recovery_fatigue(
        page_state=page, context=runtime.context, engine=runtime.engine,
    ))
    assert result == 300
    expected = [("refresh", {"stages": ["profile"], "profile_sections": ["fatigue"]})]
    assert runtime.calls == ([("go_main", {})] if page == "city_panel" else []) + expected
    assert runtime.store.reads == []


@pytest.mark.parametrize("page", ["unknown", "exchange", ""])
def test_unknown_page_rejected_before_any_recovery_action(recovery_runtime, page):
    runtime = recovery_runtime
    with pytest.raises(Exception) as error:
        asyncio.run(trade._refresh_recovery_fatigue(
            page_state=page, context=runtime.context, engine=runtime.engine,
        ))
    assert error.value.code == "recovery_invalid_start_page"
    assert runtime.calls == []


def test_failed_main_restore_stops_before_refresh(recovery_runtime):
    runtime = recovery_runtime
    runtime.state["main_result"] = {"success": False, "page_state": "unknown"}
    with pytest.raises(Exception) as error:
        asyncio.run(trade._refresh_recovery_fatigue(
            page_state="city_panel", context=runtime.context, engine=runtime.engine,
        ))
    assert error.value.code == "recovery_main_not_restored"
    assert runtime.calls == [("go_main", {})]


@pytest.mark.parametrize("change", [
    {"metadata": {"persisted": False, "executed_profile_sections": ["fatigue"]}},
    {"metadata": {"persisted": True, "executed_profile_sections": ["fatigue", "work_meals"]}},
    {"status": {"fatigue": {"current": True, "max": 856}}},
])
def test_invalid_refresh_never_reads_inventory_or_consumes(recovery_runtime, change):
    runtime = recovery_runtime
    runtime.state["refresh_result"] = {
        "status": {"fatigue": {"current": 300, "max": 856}},
        "metadata": {"persisted": True, "executed_profile_sections": ["fatigue"]},
        **change,
    }
    _, result = terminal(runtime)
    assert result["success"] is False
    assert [name for name, _ in runtime.calls] == ["refresh"]
    assert runtime.store.reads == []


def test_explicit_meals_only_uses_real_cache_and_sends_only_identities(recovery_runtime):
    runtime = recovery_runtime
    original = deepcopy(runtime.store.document)
    plan, result = terminal(runtime)
    assert plan["meals"] == MEALS
    assert plan["recovery_amount"] == 72
    assert plan["planned_remaining_fatigue"] == 228
    assert result["success"] is True
    assert runtime.calls[-1] == ("consume", {"meals": MEALS})
    assert [name for name, _ in runtime.calls] == ["refresh", "consume"]
    assert len(runtime.store.reads) == 1
    assert runtime.store.document == original  # The parent cannot deduct child-owned inventory.


@pytest.mark.parametrize("fatigue", [100, 200])
def test_empty_budget_needs_no_cache_and_no_consumption(recovery_runtime, fatigue):
    runtime = recovery_runtime
    runtime.state["fatigue"] = fatigue
    runtime.store.document = {}
    plan, result = terminal(runtime)
    assert plan["meals"] == [] and result["status"] == "skipped"
    assert result["success"] is True
    assert runtime.store.reads == []
    assert [name for name, _ in runtime.calls] == ["refresh"]


@pytest.mark.parametrize("failure", ["missing", "refresh_required", "invalid"])
def test_cache_failure_reports_without_inventory_refresh_or_rerun(freight, failure):
    runtime = freight.runtime
    work = runtime.store.document["recovery"]["work_meals"]
    if failure == "missing":
        del runtime.store.document["recovery"]["work_meals"]
    elif failure == "refresh_required":
        work["requires_refresh"] = True
    else:
        work["available_count"] = 3
    result = freight.run()
    assert result["status"] == "failed" and result["success"] is False
    assert result["bento_consumption"]["reason"] == "bento_recovery_failed"
    assert [name for name, _ in runtime.calls] == ["refresh"]
    assert len(runtime.store.reads) == 1
    assert freight.operations.count("final_sale") == 1


@pytest.mark.parametrize("page", ["city_main", "unknown"])
def test_partial_failure_keeps_child_details_and_never_retries(freight, page):
    child = {"success": False, "status": "failed", "page_state": page,
             "consumed_count": 1, "completed_count": 0,
             "failure_stage": "rating", "items": [{"meal": MEALS[0], "consumed": True}]}
    freight.runtime.state["consume_result"] = child
    result = freight.run()
    assert result["success"] is False and result["status"] == "failed"
    assert result["page_state"] == page
    assert result["bento_consumption"] == {**child, "triggered": True}
    assert freight.operations.count("bentos") == 1
    assert [name for name, _ in freight.runtime.calls] == ["refresh", "consume"]


def test_success_with_wrong_child_page_is_failure(freight):
    freight.runtime.state["consume_result"]["page_state"] = "city_panel"
    result = freight.run()
    assert result["success"] is False
    assert result["bento_consumption"]["status"] == "failed"
    assert freight.operations.count("bentos") == 1


@pytest.mark.parametrize("water,bento", [(False, False), (False, True), (True, False), (True, True)])
def test_recovery_preserves_route_profit_and_original_900_budget(freight, water, bento):
    original = deepcopy(freight.planned)
    result = freight.run(auto_sparkling_water=water, auto_bento=bento,
                         recovery_snapshot=snapshot() if water else None)
    assert result["status"] == "completed"
    assert len(freight.planner_calls) == 1
    assert freight.planner_calls[0]["fatigue_budget"] == 900
    for key in ("route", "expected_profit", "expected_fatigue_used", "remaining_expected_fatigue"):
        assert result[key] == original[key]
    assert freight.planned == original
    names = [name for name, _ in freight.runtime.calls]
    assert names.count("refresh") == int(water) + int(bento)
    assert names.count("consume") == int(bento)
    assert names.count("open_panel") == int(water)
    if bento:
        assert freight.operations.index("final_sale") < freight.operations.index("bentos")
    if water:
        assert names.index("refresh") < names.index("open_panel")
        water_index = next(i for i, op in enumerate(freight.operations) if op.startswith("water:"))
        assert freight.operations.index("travel:1") < water_index < freight.operations.index("final_sale")
    if not water and not bento:
        assert freight.runtime.store.reads == []
        assert result["bento_plan"]["reason"] == "disabled"


@pytest.mark.parametrize("boundary", ["no_route", "blocked"])
def test_no_route_or_blocked_never_refreshes_or_consumes(freight, boundary):
    if boundary == "no_route":
        freight.planned.update(status="no_plan", route=[])
    else:
        freight.state["block_index"] = 1
    result = freight.run(auto_sparkling_water=True, recovery_snapshot=snapshot())
    assert result["status"] == ("blocked" if boundary == "blocked" else "no_plan")
    assert freight.runtime.calls == [] and freight.runtime.store.reads == []
    assert "final_sale" not in freight.operations and "bentos" not in freight.operations


def test_failed_arrival_blocks_water_final_sale_and_bentos(freight, monkeypatch):
    original = trade._execute_trade_leg

    async def failed(**kwargs):
        result = await original(**kwargs)
        if kwargs["index"] == 1:
            result["travel"] = {"success": False, "status": "failed"}
        return result

    monkeypatch.setattr(trade, "_execute_trade_leg", failed)
    with pytest.raises(Exception) as error:
        freight.run(auto_sparkling_water=True, recovery_snapshot=snapshot())
    assert error.value.code == "trade_arrival_not_confirmed"
    assert freight.runtime.calls == []
    assert "final_sale" not in freight.operations and "bentos" not in freight.operations


@pytest.mark.parametrize("order", ["trade_first", "passenger_first"])
@pytest.mark.parametrize("water", [False, True])
def test_combined_real_freight_finishes_bentos_at_freight_boundary(
    freight, combined_harness, monkeypatch, order, water,
):
    inputs, calls, _ = combined_harness
    inputs.update(context=freight.runtime.context, engine=freight.runtime.engine,
                  total_fatigue_budget=900)
    inputs["trade_inputs"].update(auto_sparkling_water=water, auto_bento=True,
                                  bento_priority=["work_meals"], base_fatigue_reserve=200)
    # Caller-supplied freight fields must not leak into passenger or preview.
    inputs["passenger_inputs"].update(auto_bento=True, bento_priority=["work_meals"],
                                      auto_sparkling_water=True, base_fatigue_reserve=999)
    from tests.unit.test_resonance_pc_sparkling_water_trade import Market
    from plans.resonance_pc.src.services.city_shop_data_pc_service import ResonancePcCityShopDataService
    inputs["resonance_pc_market_data"] = Market()
    inputs["resonance_pc_city_shop_data"] = ResonancePcCityShopDataService()
    passenger = combined.resonance_pc_auto_passenger_trips_flow

    async def passenger_flow(**kwargs):
        freight.operations.append("passenger")
        return await passenger(**kwargs)

    monkeypatch.setattr(combined, "resonance_pc_auto_passenger_trips_flow", passenger_flow)
    monkeypatch.setattr(combined, "resonance_pc_auto_cycle_trade_flow",
                        trade.resonance_pc_auto_cycle_trade_flow)
    result = asyncio.run(combined.resonance_pc_auto_combined_commerce_flow(
        **inputs, order=order, persistent_data=freight.runtime.store,
        recovery_snapshot=snapshot() if water else None,
    ))
    assert result["status"] == "completed", result
    assert freight.operations.count("bentos") == 1
    assert freight.operations.index("final_sale") < freight.operations.index("bentos")
    if order == "trade_first":
        assert freight.operations.index("bentos") < freight.operations.index("passenger")
    else:
        assert freight.operations.index("passenger") < freight.operations.index("trade:0")
    assert freight.planner_calls[0]["fatigue_budget"] == (880 if order == "trade_first" else 873)
    assert result["trade"]["expected_fatigue_used"] == 333
    for child in calls["passenger"] + calls["preview"]:
        assert not {"auto_bento", "bento_priority", "auto_sparkling_water", "base_fatigue_reserve",
                    "recovery_snapshot", "persistent_data"}.intersection(child)


@pytest.mark.parametrize("opened", [
    {"success": False, "page_state": "city_panel"},
    {"success": True, "page_state": "city_main"},
    {"page_state": "city_panel"},
])
def test_unconfirmed_registered_opener_stops_water_and_terminal_work(freight, opened):
    freight.runtime.state["panel_result"] = opened
    with pytest.raises(Exception) as error:
        freight.run(auto_sparkling_water=True, recovery_snapshot=snapshot())
    assert error.value.code == "sparkling_water_panel_not_restored"
    assert [name for name, _ in freight.runtime.calls] == ["refresh", "open_panel"]
    assert not any(op.startswith("water:") for op in freight.operations)
    assert "final_sale" not in freight.operations and "bentos" not in freight.operations


@pytest.mark.parametrize("action_name,params,page", [
    ("resonance_pc.consume_bentos", {"meals": MEALS}, "city_main"),
    ("resonance_pc.open_city_panel_from_main", {}, "city_panel"),
])
def test_registered_recovery_adapter_tracks_cancelled_sync_worker(
    recovery_runtime, monkeypatch, action_name, params, page,
):
    runtime = recovery_runtime
    cid = f"freight-integration-cancel-{action_name}"
    started, release, exited = threading.Event(), threading.Event(), threading.Event()
    monkeypatch.setattr(action_injector, "current_cid", lambda: cid)
    monkeypatch.setattr(action_injector, "get_config_value", lambda *args: -1)

    def worker(**kwargs):
        assert kwargs == params
        started.set()
        try:
            assert release.wait(5)
            return {"success": True, "page_state": page}
        finally:
            exited.set()

    runtime.register(action_name, worker)

    async def exercise():
        task = asyncio.create_task(trade._call_recovery_action(
            action_name, params,
            context=runtime.context, engine=runtime.engine,
        ))
        try:
            assert await asyncio.to_thread(started.wait, 2)
            assert has_pending_sync_actions(cid)
            task.cancel()
            with pytest.raises(asyncio.CancelledError):
                await task
            assert has_pending_sync_actions(cid)
        finally:
            release.set()
            assert await asyncio.to_thread(exited.wait, 2)
            if not task.done():
                await task

    try:
        asyncio.run(exercise())
        assert not has_pending_sync_actions(cid)
    finally:
        release.set()
        clear_task_cancel(cid)


@pytest.mark.parametrize("already_cancelled", [False, True],
                         ids=["cancelled-during-ocr", "cancelled-before-ocr"])
def test_real_city_panel_opener_never_clicks_after_cancellation(monkeypatch, already_cancelled):
    cancelled = {"value": already_cancelled}
    operations = []
    frame = object()
    monkeypatch.setattr(trade, "is_current_task_cancel_requested", lambda: cancelled["value"])

    def capture(*, rect):
        operations.append("capture")
        assert rect == tuple(trade._VISIT_BUTTON_REGION)
        return SimpleNamespace(success=True, image=frame)

    def recognize_all(*, source_image):
        operations.append("ocr")
        assert source_image is frame
        # Model cancellation while OCR was busy, immediately before it returns a valid hit.
        cancelled["value"] = True
        return SimpleNamespace(results=[SimpleNamespace(
            text="\u8bbf\u95ee\u57ce\u5e02", center_point=(20, 20), confidence=1.0,
        )])

    def click(**kwargs):
        pytest.fail("A completed OCR result must not permit a click after cancellation")

    app = SimpleNamespace(capture=capture, click=click)
    ocr = SimpleNamespace(recognize_all=recognize_all)
    with pytest.raises(trade.CityTradeFlowError) as error:
        trade.resonance_pc_open_city_panel_from_main(
            app=app, ocr=ocr, timeout_sec=0, settle_sec=0,
        )
    assert error.value.code == "trade_cancelled"
    assert operations == ([] if already_cancelled else ["capture", "ocr"])


def test_required_navigation_does_not_click_when_cancelled_during_template_wait(monkeypatch):
    cancelled = {"value": False}
    operations = []
    monkeypatch.setattr(trade, "is_current_task_cancel_requested", lambda: cancelled["value"])

    def wait_template(*args, **kwargs):
        operations.append("template_wait")
        cancelled["value"] = True
        return {"found": True, "center": [198, 37]}

    monkeypatch.setattr(trade, "_wait_template", wait_template)
    app = SimpleNamespace(click=lambda **kwargs: pytest.fail("Cancelled navigation must not click"))
    with pytest.raises(trade.CityTradeFlowError) as error:
        trade.resonance_pc_go_city_main_direct(app=app, vision=object(), wait_sec=0)
    assert error.value.code == "trade_cancelled"
    assert operations == ["template_wait"]
