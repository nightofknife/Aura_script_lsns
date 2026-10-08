"""Optional execution observations: fake services only, never live-game proof."""

import asyncio
from datetime import datetime
import pytest

from packages.aura_core.context.execution import ExecutionContext
from plans.resonance_pc.src.actions import city_trade_flow_pc_actions as trade
from plans.resonance_pc.src.actions import city_travel_pc_actions as travel
from plans.resonance_pc.src.actions import passenger_flow_pc_actions as passenger
from plans.resonance_pc.src.actions._operation_progress import observe_operation, operation_progress
from tests.unit.test_departure_gate_flow import flow, run
from tests.unit.test_trade_buy_flow import buy_rig, partial_selection


class FakeBus:
    def __init__(self, *, fail=False):
        self.events = []
        self.fail = fail

    async def publish(self, event):
        if self.fail:
            raise RuntimeError("display transport unavailable")
        self.events.append(event)


def plan_data():
    return {"route": [{"from_city": "A", "to_city": "B"}],
            "city_visits": [{"city_index": 0, "phases": [
                {"key": key, "status": "waiting"} for key in ("buy", "travel")]}]}


def test_trade_timestamp_revision_sequence_and_child_progress_are_separate():
    async def exercise():
        bus = FakeBus()
        reporter = trade._TradeProgressReporter(bus, "cid-1", asyncio.get_running_loop())
        await reporter.emit("planning", "started")
        await reporter.emit("planning", "completed", data=plan_data())
        await reporter.emit("buy", "completed", city_index=0, data={"operation": {
            "key": "buy.selection", "label": "核实商品", "state": "completed", "detail": {}}})
        await reporter.emit("buy", "completed", city_index=0)
        await reporter.emit("planning", "failed")
        await reporter.emit("planning", "completed", data=plan_data())
        payloads = [event.payload for event in bus.events]
        assert [row["sequence"] for row in payloads] == list(range(1, 7))
        assert [row["data"]["route_revision"] for row in payloads] == [0, 1, 1, 1, 1, 2]
        assert [row["data"].get("progress", {}).get("completed_units") for row in payloads] == [
            None, 1, 1, 2, 2, 1]
        assert all(row["cid"] == "cid-1" and row["schema"] == trade._TRADE_PROGRESS_SCHEMA
                   for row in payloads)
        assert all(row["timestamp"].endswith("Z") and
                   datetime.fromisoformat(row["timestamp"].replace("Z", "+00:00")).utcoffset().total_seconds() == 0
                   for row in payloads)
        assert "operation" not in payloads[3]["data"]
    asyncio.run(exercise())


@pytest.mark.parametrize("reporter_class", [trade._TradeProgressReporter, passenger._PassengerProgressReporter])
def test_bus_publish_failure_is_nonfatal(reporter_class):
    async def exercise():
        reporter = reporter_class(FakeBus(fail=True), "cid", asyncio.get_running_loop())
        await reporter.emit("travel", "started")
        await reporter.emit("travel", "progress", data={"operation": {
            "key": "travel.destination", "state": "started", "label": "选择目的地", "detail": {}}})
    asyncio.run(exercise())


def test_infeasible_plan_does_not_advance_revision():
    async def exercise():
        bus = FakeBus()
        reporter = trade._TradeProgressReporter(bus, "cid", asyncio.get_running_loop())
        data = plan_data()
        data["summary"] = {"planning_status": "fixed_route_infeasible"}
        await reporter.emit("planning", "completed", data=data)
        assert bus.events[-1].payload["data"]["route_revision"] == 0
    asyncio.run(exercise())


@pytest.mark.parametrize("reporter_class", [trade._TradeProgressReporter, passenger._PassengerProgressReporter])
def test_child_worker_scheduling_does_not_add_two_second_wait(monkeypatch, reporter_class):
    results, callbacks = [], []
    class Future:
        def result(self, **kwargs):
            results.append(kwargs)

        def add_done_callback(self, callback):
            callbacks.append(callback)

    def schedule(coroutine, loop):
        coroutine.close()
        return Future()

    monkeypatch.setattr(asyncio, "run_coroutine_threadsafe", schedule)
    reporter = reporter_class(FakeBus(), "cid", None)
    reporter.emit_from_worker("travel", "progress", data={"operation": {"key": "travel.wait_arrival"}})
    assert results == [] and len(callbacks) == 1
    reporter.emit_from_worker("travel", "started")
    assert results == [{"timeout": 2.0}]


def test_optional_observer_failure_and_scope_restore_do_not_affect_business():
    observations = []
    def failing(operation):
        raise RuntimeError("display failed")
    with operation_progress(observations.append):
        observe_operation("buy.selection", "核实商品", "started")
        with operation_progress(failing):
            observe_operation("buy.selection", "核实商品", "completed")
        observe_operation("buy.confirming", "确认买入", "started")
    observe_operation("buy.confirming", "确认买入", "completed")
    assert [operation["key"] for operation in observations] == ["buy.selection", "buy.confirming"]


@pytest.mark.parametrize("kind", ["trade", "passenger"])
def test_observation_keeps_existing_bus_cid_and_worker_context(kind):
    async def exercise():
        bus = FakeBus()
        if kind == "trade":
            decorator = trade._with_trade_progress
            async def action(**kwargs):
                reporter = trade._ACTIVE_PROGRESS_REPORTER.get()
                await reporter.emit("travel", "started", city_index=2, current_city="A")
                await asyncio.to_thread(observe_operation, "travel.wait_arrival", "等待到站", "started")
                return {"success": True, "status": "completed"}
        else:
            decorator = passenger._with_passenger_progress
            async def action(**kwargs):
                reporter = passenger._ACTIVE_PROGRESS.get()
                await reporter.emit("trade", "started", leg_index=2, source_city="A")
                await asyncio.to_thread(observe_operation, "buy.selection", "核实商品", "completed")
                return {"success": True, "status": "completed"}
        result = await decorator(action)(event_bus=bus, context=ExecutionContext(cid="run-1"))
        await asyncio.sleep(0)  # drain non-blocking observation scheduled by the worker
        assert result["success"] is True
        children = [event.payload for event in bus.events if "operation" in event.payload["data"]]
        assert len(children) == 1
        assert children[0]["cid"] == "run-1" and children[0]["state"] == "progress"
        if kind == "trade":
            assert children[0]["stage"] == "travel" and children[0]["city_index"] == 2
        else:
            assert children[0]["stage"] == "trade" and children[0]["leg_index"] == 2
    asyncio.run(exercise())


@pytest.mark.parametrize("gate_state", ["confirm_clicked", "assume_traveling"])
def test_travel_observations_do_not_promote_gate_to_confirmed_departure(flow, monkeypatch, gate_state):
    observations = []
    monkeypatch.setattr(travel, "_wait_departure_gate", lambda **kwargs: {"state": gate_state})
    monkeypatch.setattr(travel, "resonance_pc_wait_intercity_arrival", lambda **kwargs: {
        "success": True, "status": "arrived", "arrival_mode": "station_button_clicked"})
    with operation_progress(observations.append):
        result = run(flow)
    assert result["success"] is True
    assert flow.events == ["open_map", ("select", "B", "A")]
    gate = next(row for row in observations if row["key"] == "travel.departure_confirming" and row["state"] == "progress")
    assert gate["detail"]["gate_state"] == gate_state and gate["detail"]["confirmed"] is False
    arrival = observations[-1]
    assert arrival["key"] == "travel.wait_arrival" and arrival["detail"]["arrival_confirmed"] is True


def test_fatigue_block_observation_preserves_result_and_map_attempts(flow, monkeypatch):
    observations = []
    monkeypatch.setattr(travel, "_wait_departure_gate", lambda **kwargs: {"state": "fatigue_panel"})
    monkeypatch.setattr(travel, "_click_fatigue_back", lambda **kwargs: {"clicked": True})
    with operation_progress(observations.append):
        result = run(flow)
    assert result["reason"] == "fatigue_recovery_required"
    assert flow.events.count("open_map") == 1
    assert observations[-1]["state"] == "blocked"
    assert observations[-1]["detail"]["reason"] == result["reason"]


def test_buy_selection_and_confirm_observations_retain_partial_purchase_contract(buy_rig):
    partial_selection(buy_rig)
    observations = []
    with operation_progress(observations.append):
        result = buy_rig.run()
    assert result["success"] is True and result["buy_result"] == "partial"
    assert [row["key"] for row in observations] == [
        "buy.selection", "buy.selection", "buy.confirming", "buy.confirming"]
    assert observations[1]["detail"]["missing_products"] == ["product-b"]
    assert observations[-1]["detail"]["bought_confirmed"] is True


def test_buy_empty_selection_never_reports_purchase_confirmation(buy_rig):
    partial_selection(buy_rig, empty=True)
    observations = []
    with operation_progress(observations.append):
        result = buy_rig.run()
    assert result["buy_result"] == "skipped"
    assert all(row["key"] == "buy.selection" for row in observations)


def test_books_and_negotiation_observations_report_existing_confirmed_results(buy_rig):
    observations = []
    with operation_progress(observations.append):
        result = buy_rig.run(books_used=2, bargain_to_cap=True)
    assert result["success"] is True
    books = [row for row in observations if row["key"] == "books.use"]
    negotiation = [row for row in observations if row["key"] == "negotiation.bargain"]
    assert [row["state"] for row in books] == ["started", "completed"]
    assert books[-1]["detail"]["used"] == 2
    assert negotiation[-1]["detail"]["result"]["actual_fatigue_used"] == 16


def test_unconfirmed_purchase_observation_is_failed_without_authorizing_departure(buy_rig):
    buy_rig.settlement = {"closed": False}
    observations = []
    with operation_progress(observations.append):
        with pytest.raises(trade.CityTradeFlowError) as error:
            buy_rig.run()
    assert error.value.code == "buy_transaction_not_confirmed"
    assert observations[-1]["key"] == "buy.confirming" and observations[-1]["state"] == "failed"
    assert "bought_confirmed" not in observations[-1]["detail"]
