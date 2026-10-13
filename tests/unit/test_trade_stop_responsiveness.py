"""Cancellation regressions with fake vision/input; never touches the game."""
import asyncio
from types import SimpleNamespace
from uuid import uuid4

import pytest

from packages.aura_core.observability.logging.core_logger import set_cid, reset_cid
from packages.aura_core.scheduler.cancellation import request_task_cancel, clear_task_cancel
from plans.aura_base.src.services.app_provider_service import AppProviderService
from plans.resonance_pc.src.actions import city_travel_pc_actions as travel
from plans.resonance_pc.src.actions import city_trade_flow_pc_actions as trade


@pytest.fixture
def stop():
    cid = "trade-stop-test-" + uuid4().hex
    token = set_cid(cid)
    try:
        yield lambda: request_task_cancel(cid)
    finally:
        clear_task_cancel(cid)
        reset_cid(token)


@pytest.mark.parametrize("module,error", [
    (travel, asyncio.CancelledError), (trade, trade.CityTradeFlowError),
])
def test_long_settling_wait_stops_at_next_poll(monkeypatch, stop, module, error):
    sleeps = []
    def sleep(seconds):
        sleeps.append(seconds)
        stop()
    monkeypatch.setattr(module.time, "sleep", sleep)
    with pytest.raises(error):
        module._sleep_with_cancel(15)
    assert sleeps == [0.05]


def test_async_route_wait_stops_without_waiting_two_seconds(monkeypatch, stop):
    sleeps = []
    async def sleep(seconds):
        sleeps.append(seconds)
        stop()
    monkeypatch.setattr(trade.asyncio, "sleep", sleep)
    with pytest.raises(trade.CityTradeFlowError, match="Trade input cancelled"):
        asyncio.run(trade._sleep_with_cancel_async(2))
    assert sleeps == [0.05]


@pytest.fixture
def map_scene(monkeypatch):
    events = []
    app = SimpleNamespace(
        get_window_size=lambda: (1280, 720),
        click=lambda **kw: events.append("click"),
        move_to=lambda **kw: events.append("move"),
        drag=lambda **kw: events.append("drag"),
    )
    monkeypatch.setattr(travel, "_load_location_city_table", lambda _: {})
    monkeypatch.setattr(travel, "_build_alias_lookup", lambda _: {})
    monkeypatch.setattr(travel, "_resolve_city_key_from_name", lambda *a: "target")
    monkeypatch.setattr(travel, "_build_target_alias_set", lambda _: {"target"})
    monkeypatch.setattr(travel, "_extract_maploc", lambda *a: (100, 100))
    monkeypatch.setattr(travel, "_capture_and_ocr_city_labels", lambda **kw: [])
    monkeypatch.setattr(travel, "_find_target_hit", lambda **kw: None)
    monkeypatch.setattr(travel, "_build_mappable_city_points", lambda **kw: [{}])
    monkeypatch.setattr(travel, "_choose_anchor_route", lambda **kw: None)
    monkeypatch.setattr(travel, "_plan_directional_drag", lambda **kw: ((500, 350), (700, 350), {}))
    return app, events


def test_cancelled_map_search_never_starts_input(map_scene, stop):
    app, events = map_scene
    stop()
    with pytest.raises(asyncio.CancelledError):
        travel.resonance_pc_select_intercity_destination("target", app=app, ocr=object())
    assert events == []


def test_cancel_during_map_recognition_prevents_target_click(map_scene, monkeypatch, stop):
    app, events = map_scene
    def recognize(**kw):
        stop()
        return []
    monkeypatch.setattr(travel, "_capture_and_ocr_city_labels", recognize)
    monkeypatch.setattr(travel, "_find_target_hit", lambda **kw: {"center": [300, 300]})
    with pytest.raises(asyncio.CancelledError):
        travel.resonance_pc_select_intercity_destination("target", app=app, ocr=object())
    assert events == []


def test_cancel_after_first_drag_prevents_remaining_search(map_scene, monkeypatch, stop):
    app, events = map_scene
    def drag(**kw):
        events.append("drag")
        stop()
    app.drag = drag
    monkeypatch.setattr(travel.time, "sleep", lambda _: pytest.fail("Must stop before settling wait"))
    with pytest.raises(asyncio.CancelledError):
        travel.resonance_pc_select_intercity_destination("target", app=app, ocr=object(), max_search_steps=12)
    assert events == ["move", "drag"]


def test_cancel_during_departure_notice_prevents_confirm_click(monkeypatch, stop):
    clicks = []
    monkeypatch.setattr(travel, "_match_template_in_region", lambda **kw: {"found": False})
    monkeypatch.setattr(travel, "_wait_for_marker_hit", lambda **kw: {"center": [200, 200]})
    def click(**kw):
        clicks.append(kw)
        stop()
    with pytest.raises(asyncio.CancelledError):
        travel._wait_departure_gate(SimpleNamespace(click=click), object(), object(),
                                    timeout_sec=8, interval_sec=0.5)
    assert len(clicks) == 1


def test_cancel_after_destination_match_prevents_go_click(monkeypatch, stop):
    def match(**kw):
        stop()
        return {"found": True, "center": [200, 200]}
    monkeypatch.setattr(travel, "_match_template_in_region", match)
    app = SimpleNamespace(click=lambda **kw: pytest.fail("Must not click after cancellation"))
    with pytest.raises(asyncio.CancelledError):
        travel._wait_and_click_go_destination(app, object(), object(),
                                             template_timeout_sec=3, interval_sec=0.5)


def test_cancelled_shop_menu_has_no_click(stop):
    stop()
    app = SimpleNamespace(click=lambda **kw: pytest.fail("Must not click after cancellation"))
    with pytest.raises(trade.CityTradeFlowError, match="Trade input cancelled"):
        trade.resonance_pc_click_shop_menu_node(1, app=app)


def test_cancelled_freight_does_not_submit_worker(monkeypatch, stop):
    stop()
    monkeypatch.setattr(trade, "_execute_city_trade_inside_current_city",
                        lambda **kw: pytest.fail("Must not start worker after cancellation"))
    with pytest.raises(trade.CityTradeFlowError, match="Trade input cancelled"):
        asyncio.run(trade._execute_freight_city_trade())


def test_inflight_drag_still_releases_button_before_stopping(monkeypatch, stop):
    events = []
    def move(*args, **kwargs):
        if "down" in events:
            stop()
    controller = SimpleNamespace(move_to=move,
        mouse_down=lambda button: events.append("down"),
        mouse_up=lambda button: events.append("up"))
    app = AppProviderService(None, controller, None)
    monkeypatch.setattr(travel.time, "sleep", lambda _: None)
    travel._perform_drag_with_hold(app, (10, 10), (30, 30), 0.1, 0.1)
    assert events == ["down", "up"]
    with pytest.raises(asyncio.CancelledError):
        travel._perform_drag_with_hold(app, (10, 10), (30, 30), 0.1, 0.1)
    assert events == ["down", "up"]
