"""Purchase integration contracts with all recognition and game input mocked."""

from copy import deepcopy
from types import SimpleNamespace

import pytest

from plans.resonance_pc.src.actions import city_trade_flow_pc_actions as trade
from plans.resonance_pc.src.actions import passenger_flow_pc_actions as passenger
from tests.unit.test_resonance_pc_sparkling_water_trade import harness, routes, run_full


_REAL_EXECUTE_TRADE_LEG = trade._execute_trade_leg
_REAL_CITY_TRADE = trade._execute_city_trade_inside_current_city


@pytest.fixture
def buy_rig(monkeypatch):
    rig = SimpleNamespace(
        calls=[], events=[], catalog=object(), requested=["product-a", "product-b"],
        selection={"selected_products": ["product-a", "product-b"],
                   "missing_products": [], "selected_product_ids": ["101", "102"],
                   "warnings": [], "scan_trace": [{"round": 1}],
                   "stop_reason": "all_confirmed"},
        book_result={"ok": True, "used": 2}, settlement={"closed": True},
    )

    def record(name, result):
        def invoke(*args, **kwargs):
            rig.calls.append((name, args, kwargs))
            return deepcopy(result() if callable(result) else result)
        return invoke

    def load():
        rig.calls.append(("load", (), {}))
        return rig.catalog

    def select(**kwargs):
        rig.calls.append(("select", (), kwargs))
        assert kwargs["catalog"] is rig.catalog
        assert callable(kwargs["check_cancelled"])
        assert callable(kwargs["trace_callback"])
        return deepcopy(rig.selection)

    monkeypatch.setattr(trade, "load_product_templates", load, raising=False)
    monkeypatch.setattr(trade, "select_buy_products", select, raising=False)
    monkeypatch.setattr(trade.time, "sleep", lambda seconds: None)
    monkeypatch.setattr(trade, "_check_trade_cancelled", lambda: None)
    monkeypatch.setattr(trade, "_report_worker", lambda stage, state, **fields:
                        rig.events.append((stage, state, deepcopy(fields))))
    monkeypatch.setattr(trade, "resonance_pc_use_purchase_books",
                        record("books", lambda: rig.book_result))
    monkeypatch.setattr(trade, "execute_bargain_to_cap", record("bargain", {
        "requested_to_cap": True, "actual_fatigue_used": 16}))

    def text_hit(*args, **kwargs):
        rig.calls.append(("text_hit", args, kwargs))
        return {"center": [10, 20]} if args[2] == ("买入",) else None

    monkeypatch.setattr(trade, "_wait_for_text_hit", text_hit)
    monkeypatch.setattr(trade, "_click_hit", record("click", {"clicked": True}))
    monkeypatch.setattr(trade, "_close_settlement",
                        record("settlement", lambda: rig.settlement))
    monkeypatch.setattr(trade, "resonance_pc_tap_back_once", record("back", {
        "success": True, "page_state": "previous", "clicked": True}))
    monkeypatch.setattr(trade, "_wait_for_shop_menu_ready", record("shop_ready", {
        "success": True, "ready": True, "found": True, "stable_matches": 2}))
    monkeypatch.setattr(trade, "_capture_text_items", lambda *args, **kwargs:
                        pytest.fail("Product selection must not invoke OCR"))
    monkeypatch.setattr(trade, "_drag_buy_list", lambda *args, **kwargs:
                        pytest.fail("The action must delegate all scanning to the selector"))
    rig.run = lambda **kwargs: trade.resonance_pc_buy_goods_on_buy_page(
        product_list=rig.requested, app=object(), ocr=object(), vision=object(), **kwargs)
    rig.names = lambda: [call[0] for call in rig.calls]
    return rig


def partial_selection(rig, *, empty=False):
    rig.selection.update(
        selected_products=[] if empty else ["product-a"],
        selected_product_ids=[] if empty else ["101"],
        missing_products=list(rig.requested) if empty else ["product-b"],
        warnings=[{"code": "product_not_confirmed", "product": "product-b"}],
        stop_reason="scan_limit",
    )


def test_complete_purchase_uses_confirmed_selector_result(buy_rig):
    result = buy_rig.run(bargain_to_cap=True, max_scan_rounds=4)
    assert result["success"] is True and result["page_state"] == "shop_page"
    assert result["buy_result"] == "complete"
    assert result["selected_products"] == buy_rig.requested
    assert result["selected_product_ids"] == ["101", "102"]
    assert result["missing_products"] == []
    assert result["settlement"]["closed"] is True
    assert result["scan_trace"] == [{"round": 1}]
    selection_call = next(call for call in buy_rig.calls if call[0] == "select")
    assert selection_call[2]["max_scan_rounds"] == 4
    assert selection_call[2]["product_list"] == buy_rig.requested
    assert buy_rig.names().count("bargain") == 1
    assert "back" not in buy_rig.names()
    event = next(fields for stage, state, fields in buy_rig.events
                 if (stage, state) == ("buy", "completed"))
    assert event["data"]["buy_result"] == "complete"


def test_partial_purchase_settles_and_reports_warning(buy_rig):
    partial_selection(buy_rig)
    result = buy_rig.run(bargain_to_cap=True)
    assert result["success"] is True and result["buy_result"] == "partial"
    assert result["selected_products"] == ["product-a"]
    assert result["missing_products"] == ["product-b"]
    assert result["warnings"] == buy_rig.selection["warnings"]
    assert result["settlement"]["closed"] is True
    assert buy_rig.names().count("bargain") == 1
    assert buy_rig.names().count("settlement") == 1
    event = next(fields for stage, state, fields in buy_rig.events
                 if (stage, state) == ("buy", "completed"))
    assert event["data"]["missing_products"] == ["product-b"]
    assert event["data"]["buy_result"] == "partial"


def test_zero_confirmed_returns_only_after_shop_verification(buy_rig):
    partial_selection(buy_rig, empty=True)
    result = buy_rig.run(bargain_to_cap=True)
    assert result["success"] is True and result["page_state"] == "shop_page"
    assert result["buy_result"] == "skipped"
    assert result["selected_products"] == []
    assert result["missing_products"] == buy_rig.requested
    assert not {"bargain", "text_hit", "click", "settlement"} & set(buy_rig.names())
    assert buy_rig.names().index("back") < buy_rig.names().index("shop_ready")
    event = next(fields for stage, state, fields in buy_rig.events
                 if (stage, state) == ("buy", "skipped"))
    assert event["data"]["buy_result"] == "skipped"


@pytest.mark.parametrize("empty", [False, True])
def test_partial_and_skipped_consume_requested_books_once(buy_rig, empty):
    partial_selection(buy_rig, empty=empty)
    result = buy_rig.run(books_used=2, bargain_to_cap=True)
    names = buy_rig.names()
    assert names.count("books") == 1
    assert names.index("load") < names.index("books") < names.index("select")
    assert result["book_result"] == {"ok": True, "used": 2}
    assert result["books_requested"] == 2


@pytest.mark.parametrize("book_result", [{"ok": False, "used": 0}, {"ok": True, "used": 1}])
def test_unconfirmed_books_fail_before_selection_or_negotiation(buy_rig, book_result):
    buy_rig.book_result = book_result
    with pytest.raises(trade.CityTradeFlowError) as error:
        buy_rig.run(books_used=2, bargain_to_cap=True)
    assert error.value.code == "purchase_books_not_confirmed"
    assert error.value.detail["used"] == book_result["used"]
    assert error.value.detail["book_result"] == book_result
    assert buy_rig.names() == ["load", "books"]


def test_catalog_failure_cannot_consume_books(buy_rig, monkeypatch):
    def broken_catalog():
        raise RuntimeError("broken template catalog")
    monkeypatch.setattr(trade, "load_product_templates", broken_catalog)
    with pytest.raises(RuntimeError, match="broken template catalog"):
        buy_rig.run(books_used=2)
    assert not buy_rig.calls


def test_cancelled_purchase_cannot_load_or_consume_books(buy_rig, monkeypatch):
    def cancelled():
        raise trade.CityTradeFlowError("trade_cancelled", "Cancelled")

    monkeypatch.setattr(trade, "_check_trade_cancelled", cancelled)
    with pytest.raises(trade.CityTradeFlowError) as error:
        buy_rig.run(books_used=2)
    assert error.value.code == "trade_cancelled"
    assert not buy_rig.calls


def test_cancel_during_catalog_load_cannot_consume_books(buy_rig, monkeypatch):
    state = {"cancelled": False}
    original_load = trade.load_product_templates

    def load():
        catalog = original_load()
        state["cancelled"] = True
        return catalog

    def check():
        if state["cancelled"]:
            raise trade.CityTradeFlowError("trade_cancelled", "Cancelled while loading templates")

    monkeypatch.setattr(trade, "load_product_templates", load)
    monkeypatch.setattr(trade, "_check_trade_cancelled", check)
    with pytest.raises(trade.CityTradeFlowError) as error:
        buy_rig.run(books_used=2)
    assert error.value.code == "trade_cancelled"
    assert buy_rig.names() == ["load"]


@pytest.mark.parametrize("cancel_at", ["_click_hit", "_close_settlement"])
def test_cancel_in_purchase_tail_retains_consumed_books(buy_rig, monkeypatch, cancel_at):
    def cancelled(*args, **kwargs):
        raise trade.CityTradeFlowError("trade_cancelled", "Cancelled in purchase tail", {"phase": cancel_at})

    monkeypatch.setattr(trade, cancel_at, cancelled)
    with pytest.raises(trade.CityTradeFlowError) as error:
        buy_rig.run(books_used=2)
    assert error.value.code == "trade_cancelled"
    assert error.value.detail["phase"] == cancel_at
    assert error.value.detail["book_result"] == {"ok": True, "used": 2}
    assert buy_rig.names().count("books") == 1


def test_selection_failure_retains_already_consumed_books(buy_rig, monkeypatch):
    def failed_selection(**kwargs):
        raise trade.CityTradeFlowError("trade_cancelled", "Cancelled during selection", {"round": 2})

    monkeypatch.setattr(trade, "select_buy_products", failed_selection)
    with pytest.raises(trade.CityTradeFlowError) as error:
        buy_rig.run(books_used=2)
    assert error.value.code == "trade_cancelled"
    assert error.value.detail["round"] == 2
    assert error.value.detail["book_result"] == {"ok": True, "used": 2}
    assert buy_rig.names().count("books") == 1
    assert "bargain" not in buy_rig.names()


def test_selected_purchase_without_negotiation_has_no_dummy_call(buy_rig):
    result = buy_rig.run(bargain_to_cap=False)
    assert result["buy_result"] == "complete"
    assert "bargain" not in buy_rig.names()


def test_unconfirmed_settlement_remains_fatal(buy_rig):
    partial_selection(buy_rig)
    buy_rig.settlement = {"closed": False}
    with pytest.raises(trade.CityTradeFlowError) as error:
        buy_rig.run(bargain_to_cap=True)
    assert error.value.code == "buy_transaction_not_confirmed"
    assert error.value.detail["selected_products"] == ["product-a"]
    assert "back" not in buy_rig.names()


def test_skip_cannot_claim_shop_page_when_verification_fails(buy_rig, monkeypatch):
    partial_selection(buy_rig, empty=True)

    def not_ready(*args, **kwargs):
        raise trade.CityTradeFlowError("shop_menu_not_ready", "Not restored")

    monkeypatch.setattr(trade, "_wait_for_shop_menu_ready", not_ready)
    with pytest.raises(trade.CityTradeFlowError) as error:
        buy_rig.run(bargain_to_cap=True)
    assert error.value.code == "shop_menu_not_ready"
    assert not any((stage, state) == ("buy", "skipped") for stage, state, _ in buy_rig.events)


@pytest.fixture
def city_rig(monkeypatch):
    rig = SimpleNamespace(calls=[], buy={
        "success": True, "page_state": "shop_page", "buy_result": "partial",
        "selected_products": ["product-a"], "missing_products": ["product-b"],
        "warnings": [{"code": "product_not_confirmed"}],
        "settlement": {"closed": True}, "settlement_after_confirm": None,
    })

    def result(name, data):
        def invoke(*args, **kwargs):
            rig.calls.append(name)
            return deepcopy(data() if callable(data) else data)
        return invoke

    monkeypatch.setattr(trade, "resonance_pc_click_city_shop_by_name", result("enter", {}))
    monkeypatch.setattr(trade, "_wait_for_shop_menu_ready", result("ready", {"ready": True}))
    monkeypatch.setattr(trade, "resonance_pc_click_shop_menu_node", result("node", {}))
    monkeypatch.setattr(trade, "resonance_pc_sell_goods_on_sell_page", result("sell", {
        "success": True, "page_state": "shop_page", "sold_confirmed": False,
        "sell_result": "empty_cargo", "selection": {"status": "empty_cargo"},
    }))
    monkeypatch.setattr(trade, "resonance_pc_buy_goods_on_buy_page", result("buy", lambda: rig.buy))
    monkeypatch.setattr(trade, "resonance_pc_go_city_main_direct", result("main", {
        "success": True, "page_state": "city_main"}))
    rig.run = lambda: trade._execute_city_trade_inside_current_city_scoped(
        current_city="city-a", buy_products=["product-a", "product-b"], books_used=2,
        sell_raise_to_cap=False, buy_bargain_to_cap=True,
        app=object(), ocr=object(), vision=object(), city_shop_data=object())
    return rig


@pytest.mark.parametrize("buy_result", ["complete", "partial", "skipped"])
def test_city_trade_allows_confirmed_or_explicitly_skipped_purchase(city_rig, buy_result):
    city_rig.buy["buy_result"] = buy_result
    if buy_result == "complete":
        city_rig.buy.update(selected_products=["product-a", "product-b"], missing_products=[])
    if buy_result == "skipped":
        city_rig.buy.update(selected_products=[], missing_products=["product-a", "product-b"],
                            settlement={"closed": False}, back={"success": True})
    result = city_rig.run()
    assert result["success"] is True and result["page_state"] == "city_main"
    assert result["buy"]["buy_result"] == buy_result
    assert result["sell"]["sold_confirmed"] is False
    assert city_rig.calls[-1] == "main"


@pytest.mark.parametrize("changes", [
    {"buy_result": "skipped", "selected_products": ["product-a"]},
    {"buy_result": "skipped", "selected_products": [], "page_state": "unknown"},
    {"buy_result": "partial", "settlement": {"closed": False}},
    {"buy_result": "complete", "settlement": {"closed": False}},
    {"buy_result": "partial", "success": False},
])
def test_city_trade_rejects_unsafe_purchase_result_before_returning(city_rig, changes):
    city_rig.buy.update(changes)
    with pytest.raises(trade.CityTradeFlowError) as error:
        city_rig.run()
    assert error.value.code == "buy_transaction_not_confirmed"
    assert "main" not in city_rig.calls


def test_city_trade_accepts_confirmed_second_settlement(city_rig):
    city_rig.buy.update(settlement={"closed": False}, settlement_after_confirm={"closed": True})
    assert city_rig.run()["success"] is True


@pytest.mark.parametrize("mode", ["profit", "quick", "fixed", "target"])
@pytest.mark.parametrize("empty", [False, True], ids=["partial", "skipped"])
def test_four_modes_continue_route_and_surface_purchase_warnings(
        harness, buy_rig, monkeypatch, mode, empty):
    operations, _ = harness
    partial_selection(buy_rig, empty=empty)
    route = routes()
    for leg in route:
        leg.update(buy_products=list(buy_rig.requested), books_used=2, bargain_to_cap=True)
    monkeypatch.setattr(trade, "resonance_pc_trade_plan_optimal_route", lambda **kwargs: {
        "status": "ok", "route": deepcopy(route), "books_used": 4})
    # Keep the real route leg and city guard; replace only their external UI actions.
    monkeypatch.setattr(trade, "_execute_trade_leg", _REAL_EXECUTE_TRADE_LEG)
    monkeypatch.setattr(trade, "_execute_city_trade_inside_current_city", _REAL_CITY_TRADE)
    monkeypatch.setattr(trade, "resonance_pc_click_city_shop_by_name", lambda **kwargs: {})
    monkeypatch.setattr(trade, "resonance_pc_click_shop_menu_node", lambda **kwargs: {})
    monkeypatch.setattr(trade, "resonance_pc_sell_goods_on_sell_page", lambda **kwargs: {
        "success": True, "page_state": "shop_page", "sold_confirmed": False,
        "sell_result": "empty_cargo"})
    monkeypatch.setattr(trade, "resonance_pc_go_city_main_direct", lambda **kwargs: {
        "success": True, "page_state": "city_main"})

    def travel(**kwargs):
        operations.append(("travel", kwargs["from_city_name"], kwargs["to_city_name"]))
        return {"success": True, "status": "ok"}

    monkeypatch.setattr(trade, "resonance_pc_intercity_depart_and_wait", travel)
    result = run_full(trade_mode=mode, book_budget=4, auto_sparkling_water=False,
                      fixed_route_city_ids=["15", "11", "15"], target_profit=1000000)
    assert result["success"] is True and result["status"] == "completed"
    assert result["execution"]["completed_leg_count"] == 2
    assert len([entry for entry in operations if isinstance(entry, tuple) and entry[0] == "travel"]) == 2
    assert result["final_sale"]["success"] is True
    buys = [row["city_trade"]["buy"] for row in result["execution"]["leg_results"]]
    assert [row["buy_result"] for row in buys] == ["skipped" if empty else "partial"] * 2
    warnings = [warning for warning in result["warnings"] if warning.get("code") == "product_not_confirmed"]
    assert [warning["leg_index"] for warning in warnings] == [0, 1]
    assert buy_rig.names().count("books") == 2
    assert [row["book_result"]["used"] for row in buys] == [2, 2]
    assert buy_rig.names().count("bargain") == (0 if empty else 2)


@pytest.mark.parametrize("empty", [False, True], ids=["partial", "skipped"])
def test_passenger_city_trade_uses_new_shared_purchase_without_books_or_negotiation(
        buy_rig, monkeypatch, empty):
    partial_selection(buy_rig, empty=empty)
    monkeypatch.setattr(passenger, "_prepare_passenger_trade_plan", lambda **kwargs: {
        "status": "planned", "buy_products": list(buy_rig.requested)})
    monkeypatch.setattr(passenger, "resonance_pc_open_city_panel_from_main", lambda **kwargs: {
        "success": True, "page_state": "city_panel"})
    monkeypatch.setattr(trade, "resonance_pc_click_city_shop_by_name", lambda **kwargs: {})
    monkeypatch.setattr(trade, "resonance_pc_click_shop_menu_node", lambda **kwargs: {})
    monkeypatch.setattr(trade, "resonance_pc_sell_goods_on_sell_page", lambda **kwargs: {
        "success": True, "page_state": "shop_page", "sold_confirmed": False,
        "sell_result": "empty_cargo"})
    monkeypatch.setattr(trade, "resonance_pc_go_city_main_direct", lambda **kwargs: {
        "success": True, "page_state": "city_main"})
    result = passenger._execute_passenger_trade_at_city(
        current_city_id="15", destination_city_id="11", final_sale=False,
        route_by_id={"15": {"city_name": "city-a"}, "11": {"city_name": "city-b"}},
        app=object(), ocr=object(), vision=object(), city_shop_data=object(),
        market_data=object(), trade_planner=object())
    assert result["success"] is True
    assert result["execution"]["page_state"] == "city_main"
    purchase = result["execution"]["buy"]
    assert purchase["buy_result"] == ("skipped" if empty else "partial")
    assert purchase["missing_products"] == buy_rig.selection["missing_products"]
    assert purchase["warnings"] == buy_rig.selection["warnings"]
    assert "books" not in buy_rig.names() and "bargain" not in buy_rig.names()
    assert buy_rig.names().count("settlement") == (0 if empty else 1)
