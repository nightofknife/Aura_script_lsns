"""Pickup timing contracts with a fake clock/services, never real game input."""
import asyncio
import inspect
import os
from pathlib import Path
from types import SimpleNamespace

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

import pytest
import yaml
from jinja2.nativetypes import NativeEnvironment
from PySide6.QtCore import QCoreApplication, QEvent, QSettings
from PySide6.QtWidgets import QApplication, QMessageBox

from packages.aura_core.scheduler.validation import InputValidator
from packages.resonance_gui.config_repository import ResonanceConfigRepository
from packages.resonance_gui.logic import normalize_trade_task_inputs
from packages.resonance_gui.main_window import ResonanceMainWindow
from packages.resonance_gui.widgets.trade_page import TradePage
from plans.resonance_pc.src.actions import city_travel_pc_actions as travel
from plans.resonance_pc.src.actions import city_trade_flow_pc_actions as trade
from plans.resonance_pc.src.actions import combined_commerce_pc_actions as combined
from tests.unit.test_resonance_pc_sparkling_water_trade import harness, run_full

ROOT = Path(__file__).resolve().parents[2]


@pytest.fixture
def clock(monkeypatch):
    state = SimpleNamespace(now=0.0, sleeps=[], clicks=[], present=True)
    def sleep(seconds):
        state.sleeps.append(seconds)
        state.now += seconds
    monkeypatch.setattr(travel.time, "monotonic", lambda: state.now)
    monkeypatch.setattr(travel.time, "sleep", sleep)
    monkeypatch.setattr(travel, "_check_intercity_cancelled", lambda: None)
    monkeypatch.setattr(travel, "_match_template_in_region", lambda **kw: {"found": state.present})
    state.app = SimpleNamespace(click=lambda **kw: state.clicks.append(state.now),
                                get_window_size=lambda: (1280, 720))
    return state


@pytest.mark.parametrize("interval", [0.1, 0.5, 2.5, 60])
def test_click_cooldown_respects_configured_interval(clock, interval):
    pickup = travel._TravelPickup(clock.app, object(), enabled=True, interval_sec=interval)
    pickup.tick(100)
    clock.now = interval - 0.001
    pickup.tick(100)
    assert clock.clicks == [0]
    clock.now = interval
    pickup.tick(100)
    assert clock.clicks == [0, interval]
    assert pickup.click_count == 2


@pytest.mark.parametrize("value", [0, -1, 0.099, 60.01, True, False, "0.5", None,
                                    float("nan"), float("inf"), 10**1000])
def test_invalid_intervals_fail_without_input(clock, value):
    with pytest.raises(ValueError):
        travel._TravelPickup(clock.app, object(), enabled=True, interval_sec=value)
    assert not clock.clicks


def test_horn_missing_and_deadline_prevent_pickup(clock):
    pickup = travel._TravelPickup(clock.app, object(), enabled=True)
    clock.present = False
    pickup.tick(5)
    assert not clock.clicks
    clock.present = True
    clock.now = 5
    pickup.tick(5)
    assert not clock.clicks


def test_long_cooldown_still_checks_arrival_within_poll_interval(clock, monkeypatch):
    checks = []
    monkeypatch.setattr(travel, "_resolve_plan_template_path", lambda _: __file__)
    monkeypatch.setattr(travel, "_click_arrival_template_until_absent", lambda **kw: {"confirmed": False})
    def detect(**kw):
        checks.append(clock.now)
        return {"found": len(checks) > 1}
    monkeypatch.setattr(travel, "_detect_city_main_entry", detect)
    result = travel.resonance_pc_wait_intercity_arrival(
        timeout_sec=120, interval_sec=3, auto_pickup=True, auto_pickup_interval_sec=60,
        app=clock.app, ocr=object(), vision=object())
    assert result["status"] == "arrived" and result["pickup_click_count"] == 1
    assert checks == pytest.approx([0, 3])
    assert clock.clicks == [0]
    assert max(clock.sleeps) <= 0.05


def test_cancel_interrupts_long_cooldown_without_another_click(clock, monkeypatch):
    pickup = travel._TravelPickup(clock.app, object(), enabled=True, interval_sec=60)
    pickup.tick(120)
    def check():
        if clock.now >= 0.1:
            raise asyncio.CancelledError()
    monkeypatch.setattr(travel, "_check_intercity_cancelled", check)
    with pytest.raises(asyncio.CancelledError):
        pickup.wait(120)
    assert clock.now == pytest.approx(0.1) and clock.clicks == [0]


def test_departure_forwards_custom_interval_to_arrival(clock, monkeypatch):
    monkeypatch.setattr(travel, "_resolve_plan_template_path", lambda _: __file__)
    monkeypatch.setattr(travel, "_open_intercity_map", lambda *a: None)
    monkeypatch.setattr(travel, "resonance_pc_select_intercity_destination", lambda **kw: {"to_city_name": "B"})
    monkeypatch.setattr(travel, "_wait_and_click_go_destination", lambda **kw: {"clicked": True})
    monkeypatch.setattr(travel, "_wait_departure_gate", lambda **kw: {"state": "confirm_clicked"})
    calls = []
    monkeypatch.setattr(travel, "resonance_pc_wait_intercity_arrival", lambda **kw: calls.append(kw) or {"status": "arrived"})
    travel.resonance_pc_intercity_depart_and_wait("栖羽站", from_city_name="海角城",
                                               app=clock.app, ocr=object(), vision=object(),
                                               auto_pickup=True, auto_pickup_interval_sec=2.5)
    assert calls[0]["auto_pickup_interval_sec"] == 2.5


def test_task_schema_and_template_forward_interval():
    spec = yaml.safe_load((ROOT/"plans/resonance_pc/tasks/auto_cycle_trade_pc.yaml").read_text("utf-8"))["auto_cycle_trade_pc"]
    validator = InputValidator(None)
    for value in [0.1, 0.5, 3.5, 60]:
        ok, inputs = validator.validate_inputs_against_meta(spec["meta"]["inputs"], {"auto_pickup": True, "auto_pickup_interval_sec": value})
        assert ok, inputs
        params = {key: NativeEnvironment().from_string(expr).render(inputs=inputs) for key, expr in spec["steps"]["run"]["params"].items()}
        inspect.signature(trade.resonance_pc_auto_cycle_trade_flow).bind(**params)
        assert params["auto_pickup_interval_sec"] == value
    ok, defaults = validator.validate_inputs_against_meta(spec["meta"]["inputs"], {})
    assert ok and defaults["auto_pickup_interval_sec"] == 0.5
    assert "auto_pickup_interval_sec" not in combined._filtered({"auto_pickup_interval_sec": 3}, combined._PREVIEW_INPUT_KEYS)
    assert combined._filtered({"auto_pickup_interval_sec": 3}, combined._TRADE_INPUT_KEYS) == {"auto_pickup_interval_sec": 3}
    assert combined._filtered({"auto_pickup_interval_sec": 3}, combined._PASSENGER_INPUT_KEYS) == {}


@pytest.mark.parametrize("order", ["passenger_first", "trade_first"])
@pytest.mark.parametrize("value", [True, 61])
def test_combined_rejects_invalid_interval_before_any_child(order, value, monkeypatch):
    def unexpected(*args, **kwargs):
        pytest.fail("Invalid pickup interval must be rejected before navigation or either task")
    for name in ("_read_current_city", "_build_passenger_route", "_run_passenger", "_run_trade"):
        monkeypatch.setattr(combined, name, unexpected)
    services = {name: object() for name in (
        "app", "ocr", "vision", "resonance_pc_city_shop_data", "resonance_pc_market_data",
        "resonance_pc_trade_planner", "state_store", "event_bus", "context", "engine",
    )}
    result = asyncio.run(combined.resonance_pc_auto_combined_commerce_flow(
        order=order, trade_inputs={"auto_pickup": True, "auto_pickup_interval_sec": value}, **services))
    assert not result["success"]
    assert result["reason"] == "invalid_trade_inputs" and result["failure_stage"] == "preflight"


def test_freight_entry_forwards_interval_to_each_leg(harness, monkeypatch):
    captured = []
    original = trade._execute_trade_leg
    async def leg(**kwargs):
        captured.append(kwargs["auto_pickup_interval_sec"])
        return await original(**kwargs)
    monkeypatch.setattr(trade, "_execute_trade_leg", leg)
    result = run_full(auto_pickup=True, auto_pickup_interval_sec=3.25, auto_sparkling_water=False)
    assert result["success"] and captured and set(captured) == {3.25}


@pytest.mark.parametrize("value", [-1, True, float("nan"), 61])
def test_freight_rejects_bad_interval_before_game_action(harness, value):
    operations, _ = harness
    with pytest.raises(ValueError):
        run_full(auto_pickup=True, auto_pickup_interval_sec=value)
    assert operations == []


def test_leg_passes_interval_to_departure(monkeypatch):
    captured = []
    async def city_trade(**kw):
        return {"success": True, "page_state": "city_main"}
    def depart(**kw):
        captured.append(kw)
        raise RuntimeError("fake departure boundary")
    monkeypatch.setattr(trade, "_execute_freight_city_trade", city_trade)
    monkeypatch.setattr(trade, "resonance_pc_intercity_depart_and_wait", depart)
    with pytest.raises(RuntimeError, match="fake departure boundary"):
        asyncio.run(trade._execute_trade_leg(
            index=0, leg={"from_city": "A", "to_city": "B"}, sell_raise_to_cap=False,
            page_state="city_panel", use_fatigue_medicine=False, allowed_fatigue_medicines=[],
            fatigue_medicine_max_uses=4, negotiation_max_attempts=5,
            auto_pickup=True, auto_pickup_interval_sec=3.25,
            app=object(), ocr=object(), vision=object(), city_shop_data=object()))
    assert captured[0]["auto_pickup_interval_sec"] == 3.25


@pytest.fixture
def repo(tmp_path):
    return ResonanceConfigRepository(QSettings(str(tmp_path/"gui.ini"), QSettings.IniFormat))


def test_gui_enable_disable_collect_save_and_restore(repo):
    app = QApplication.instance() or QApplication([])
    page = TradePage(repo)
    try:
        assert page.auto_pickup_interval_sec.value() == 0.5
        assert not page.auto_pickup_interval_sec.isEnabled()
        page.auto_pickup.setChecked(True)
        assert page.auto_pickup_interval_sec.isEnabled()
        page.auto_pickup_interval_sec.setValue(2.5)
        inputs = page.collect_task_inputs()
        assert inputs["auto_pickup"] and inputs["auto_pickup_interval_sec"] == 2.5
        repo.save_trade_inputs(page.collect_ui_state())
        page.set_inputs(repo.load_trade_inputs())
        assert page.auto_pickup_interval_sec.value() == 2.5
        page.set_busy(True)
        assert not page.auto_pickup_interval_sec.isEnabled()
        page.set_busy(False)
        assert page.auto_pickup_interval_sec.isEnabled()
        page.auto_pickup.setChecked(False)
        assert not page.auto_pickup_interval_sec.isEnabled() and page.auto_pickup_interval_sec.value() == 2.5
        preview = TradePage(repo, preview_mode=True)
        assert preview.pickup_timing_panel.isHidden()
        assert "auto_pickup_interval_sec" not in repo.load_trade_preview_inputs()
        preview.deleteLater()
    finally:
        page.deleteLater()
        QCoreApplication.sendPostedEvents(None, QEvent.DeferredDelete)
        app.processEvents()


def test_main_window_saves_interval_immediately(repo, monkeypatch):
    app = QApplication.instance() or QApplication([])
    monkeypatch.setattr(ResonanceMainWindow, "_wire_bridge", lambda self: None)
    window = ResonanceMainWindow(settings=repo, initialize_on_startup=False)
    try:
        window.trade_page.auto_pickup.setChecked(True)
        window.trade_page.auto_pickup_interval_sec.setValue(3.25)
        assert repo.load_trade_inputs()["auto_pickup_interval_sec"] == 3.25
        errors = []
        monkeypatch.setattr(QMessageBox, "warning", lambda *args: errors.append(args[2]))
        monkeypatch.setattr(repo, "sync_checked", lambda: (_ for _ in ()).throw(OSError("不可写")))
        window.trade_page.auto_pickup_interval_sec.setValue(7.5)
        assert window.trade_page.auto_pickup_interval_sec.value() == 3.25
        assert repo.load_trade_inputs()["auto_pickup_interval_sec"] == 3.25
        assert errors == ["不可写"]
    finally:
        window._close_ready = True
        window.close()
        window.deleteLater()
        QCoreApplication.sendPostedEvents(None, QEvent.DeferredDelete)
        app.processEvents()


@pytest.mark.parametrize("value", [True, "1", 0, 61, float("nan")])
def test_invalid_saved_interval_does_not_overwrite_config(repo, value):
    before = repo.load_trade_inputs()
    with pytest.raises(ValueError):
        repo.save_trade_inputs({**before, "auto_pickup_interval_sec": value})
    assert repo.load_trade_inputs() == before


@pytest.mark.parametrize("mode", ["free", "fixed"])
def test_mode_snapshot_and_combined_payload_preserve_interval(mode):
    values = {"trade_mode": mode, "auto_pickup": True, "auto_pickup_interval_sec": 2.75}
    snapshot = normalize_trade_task_inputs(values)
    values["auto_pickup_interval_sec"] = 1
    assert snapshot["auto_pickup_interval_sec"] == 2.75
    payload = ResonanceMainWindow._combined_commerce_inputs(order="passenger_first", trade=snapshot, passenger={})
    assert payload["trade_inputs"]["auto_pickup_interval_sec"] == 2.75
