"""Cache fallback consent and persistent warnings; no live runner or network."""
import os

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

import pytest
from PySide6.QtCore import QCoreApplication, QEvent, QSettings
from PySide6.QtWidgets import QApplication

from packages.resonance_gui.config_repository import ResonanceConfigRepository, TRADE_PREVIEW_INPUT_KEYS
from packages.resonance_gui.logic import (
    PC_GAME_NAME, TRADE_PROGRESS_EVENT, TRADE_PROGRESS_SCHEMA,
    TradeProgressState, WorkflowFreightProgressState, normalize_trade_task_inputs,
    reduce_trade_progress, reduce_workflow_freight_progress, trade_result_summary,
)
from packages.resonance_gui.main_window import ResonanceMainWindow
from packages.resonance_gui.widgets.trade_page import TradePage
from packages.resonance_gui.widgets.workflow_page import WorkflowPage
from tests.unit.test_auto_book_gui_restore import recording_bridge
from tests.unit.test_resonance_pc_sparkling_water_trade import harness, run_full
from tests.unit.test_freight_market_fallback import Bus, CACHE, install_market


@pytest.fixture
def repository(tmp_path):
    return ResonanceConfigRepository(QSettings(str(tmp_path / "market.ini"), QSettings.Format.IniFormat))


def dispose(widget):
    widget.close()
    widget.deleteLater()
    QCoreApplication.sendPostedEvents(None, QEvent.Type.DeferredDelete)
    QApplication.processEvents()


@pytest.fixture
def page(repository):
    app = QApplication.instance() or QApplication([])
    widget = TradePage(repository)
    yield widget
    dispose(widget)


def event(sequence, stage="market", state="completed", cid="run", data=None):
    return {"name": TRADE_PROGRESS_EVENT, "payload": {
        "schema": TRADE_PROGRESS_SCHEMA, "cid": cid, "sequence": sequence,
        "stage": stage, "state": state, "data": data or {},
    }}


MARKET = {"source": "fallback_cache", "fetched_at": "2026-10-17T01:02:03Z", "stale_reason": "network timeout"}
RESULT = {"market_source": "fallback_cache", "market_fetched_at": MARKET["fetched_at"],
          "market_stale_reason": MARKET["stale_reason"], "market_fallback_blocked": False}


@pytest.mark.parametrize("mode", ["free", "fixed"])
@pytest.mark.parametrize("enabled", [False, True])
def test_switch_default_persistence_snapshot_and_dispatch(page, repository, monkeypatch, mode, enabled):
    assert repository.load_trade_inputs()["allow_stale_market"] is False
    page.mode_buttons[mode].setChecked(True)
    if mode == "fixed":
        page.add_route_city("1")
        page.add_route_city("2")
    page.allow_stale_market.setChecked(enabled)
    page.save_ui_state()
    restored = ResonanceConfigRepository(QSettings(repository.settings.fileName(), QSettings.Format.IniFormat))
    assert restored.load_trade_inputs()["allow_stale_market"] is enabled
    page.set_inputs(restored.load_trade_inputs())
    inputs = page.collect_task_inputs()
    runner, bridge = recording_bridge(monkeypatch)
    bridge.run_pc_trade(inputs)
    assert runner.calls[0]["inputs"]["allow_stale_market"] is enabled
    page.allow_stale_market.setChecked(not enabled)
    assert runner.calls[0]["inputs"]["allow_stale_market"] is enabled
    combined = ResonanceMainWindow._combined_commerce_inputs(order="trade_first", trade=inputs, passenger={})
    assert combined["trade_inputs"]["allow_stale_market"] is enabled
    assert "allow_stale_market" not in combined["passenger_inputs"]
    page.set_busy(True)
    assert not page.allow_stale_market.isEnabled()
    page.set_busy(False)
    assert page.allow_stale_market.isEnabled()


@pytest.mark.parametrize("value", [None, 1, 0, "true", "false", [], {}])
def test_strict_gui_flag_validation(repository, value):
    with pytest.raises(ValueError, match="历史行情"):
        repository.save_trade_inputs({"allow_stale_market": value})
    with pytest.raises(ValueError, match="历史行情"):
        normalize_trade_task_inputs({"allow_stale_market": value})


def test_flag_is_execution_only_not_preview_or_fingerprint(page, repository, monkeypatch):
    page.start_city.setCurrentIndex(page.start_city.findData("1"))
    original_key = page._planning_key()
    page._last_inputs = page.collect_task_inputs(preview=True)
    page.finish_preview({"status": "planned", "expected_profit": 96})
    page.allow_stale_market.setChecked(True)
    assert page._planning_key() == original_key
    assert not page._plan_stale
    runner, bridge = recording_bridge(monkeypatch)
    bridge.preview_pc_trade(page.collect_task_inputs(preview=True))
    assert "allow_stale_market" not in runner.calls[0]["inputs"]
    assert "allow_stale_market" not in TRADE_PREVIEW_INPUT_KEYS
    preview = TradePage(repository, preview_mode=True)
    try:
        assert preview.allow_stale_market.isHidden()
        assert "allow_stale_market" not in repository.load_trade_preview_inputs()
    finally:
        dispose(preview)


@pytest.mark.parametrize("workflow", [False, True])
@pytest.mark.parametrize("blocked", [False, True])
def test_reducers_keep_cache_metadata_and_reject_old_foreign_events(workflow, blocked):
    state = WorkflowFreightProgressState(cid="run") if workflow else TradeProgressState(cid="run")
    reduce = reduce_workflow_freight_progress if workflow else reduce_trade_progress
    state = reduce(state, event(1, state="blocked" if blocked else "completed", data=MARKET), expected_cid="run")
    assert state.market_source == "fallback_cache"
    assert state.market_fallback_blocked is blocked
    assert state.market_fetched_at == MARKET["fetched_at"]
    state = reduce(state, event(2, stage="planning", state="started"), expected_cid="run")
    assert state.market_stale_reason == "network timeout"
    for bad in (event(1, data={"source": "refresh"}), event(99, cid="other", data={"source": "refresh"})):
        state = reduce(state, bad, expected_cid="run")
        assert state.market_source == "fallback_cache"
        assert state.market_fallback_blocked is blocked


@pytest.mark.parametrize("blocked", [False, True])
def test_trade_warning_is_visible_during_later_stages_and_retained_at_finish(page, blocked):
    page.begin_run({"cid": "run"})
    page.apply_progress(event(1, state="blocked" if blocked else "completed", data=MARKET))
    text = page.market_warning_label.text()
    assert not page.market_warning_label.isHidden()
    assert "2026-10-17T01:02:03Z" in text and "network timeout" in text
    assert ("未使用历史行情继续" if blocked else "本次使用历史行情") in text
    page.apply_progress(event(2, stage="planning", state="started"))
    assert page.market_warning_label.text() == text
    page.finish_run({"status": "blocked" if blocked else "success", **RESULT, "market_fallback_blocked": blocked})
    assert page.market_warning_label.text() == text
    page.begin_run({"cid": "next"})
    assert page.market_warning_label.isHidden()
    page.apply_progress(event(1, cid="next", data={"source": "refresh"}))
    assert page.market_warning_label.isHidden()


@pytest.mark.parametrize("blocked", [False, True])
def test_workflow_warning_survives_page_switch_progress_and_finish(page, repository, blocked):
    workflow = WorkflowPage(repository)
    try:
        workflow.begin_workflow(["trade", "passenger"], [])
        workflow.set_active_progress_cid("trade", "run")
        workflow.apply_progress_event("trade", event(1, state="blocked" if blocked else "completed", data=MARKET))
        text = workflow.market_warning_label.text()
        assert "快照时间" in text
        assert ("未使用历史行情继续" if blocked else "本次使用历史行情") in text
        workflow._select_task("passenger")
        assert workflow.market_warning_label.text() == text
        if not blocked:
            workflow.apply_progress_event("trade", event(2, stage="planning", data={
                "route": [{"from_city": "A", "to_city": "B"}], "summary": {"status": "planned"}}))
            assert workflow.runtime_plan_badge.text() == "方案已采用 · 缓存行情"
            assert "历史行情" in workflow.runtime_plan_contract_note.text()
        workflow.finish_workflow(success=not blocked, message="完成" if not blocked else "已阻断")
        assert workflow.market_warning_label.text() == text
        workflow.begin_workflow(["trade"], [])
        assert workflow.market_warning_label.isHidden()
    finally:
        dispose(workflow)


def test_result_summary_distinguishes_refused_cache():
    rejected = trade_result_summary({**RESULT, "market_fallback_blocked": True})
    assert "未使用历史行情继续" in rejected["warnings"][0]
    assert "本次使用历史行情" not in rejected["warnings"][0]


@pytest.mark.parametrize("combined", [False, True])
def test_terminal_result_restores_warning_if_progress_event_was_missed(page, repository, monkeypatch, combined):
    monkeypatch.setattr(ResonanceMainWindow, "_wire_bridge", lambda self: None)
    window = ResonanceMainWindow(settings=repository, initialize_on_startup=False)
    try:
        window._active_game_name = PC_GAME_NAME
        result = {"status": "success", "success": True, **RESULT}
        payload = {"status": "success", "gui_item": {"kind": "trade_run"}, "final_result": {"user_data": result}}
        if combined:
            window._finish_combined_pages(payload, {"trade": result, "passenger": {"status": "success"}})
        else:
            window._on_task_finished(payload)
        assert "本次使用历史行情" in window.workflow_page.market_warning_label.text()
        assert "本次使用历史行情" in window.trade_page.market_warning_label.text()
    finally:
        window._close_ready = True
        dispose(window)


@pytest.mark.parametrize("allowed", [False, True])
def test_real_backend_event_contract_reaches_gui_with_fake_game_services(page, repository, monkeypatch, harness, allowed):
    from packages.aura_core.context.execution import ExecutionContext
    install_market(monkeypatch)
    bus = Bus()
    result = run_full(allow_stale_market=allowed, context=ExecutionContext(cid="run"), event_bus=bus)
    workflow = WorkflowPage(repository)
    try:
        page.begin_run({"cid": "run"})
        workflow.begin_workflow(["trade"], [])
        workflow.set_active_progress_cid("trade", "run")
        for payload in bus.events:
            envelope = {"name": TRADE_PROGRESS_EVENT, "payload": payload}
            page.apply_progress(envelope)
            workflow.apply_progress_event("trade", envelope)
        assert result["market_fallback_blocked"] is not allowed
        for label in (page.market_warning_label, workflow.market_warning_label):
            assert not label.isHidden()
            assert CACHE["fetched_at"] in label.text()
            assert ("本次使用历史行情" if allowed else "未使用历史行情继续") in label.text()
        if not allowed:
            assert harness[0] == ["open_panel"]
    finally:
        dispose(workflow)


def test_warning_layout_keeps_run_control_visible(page, repository, monkeypatch, tmp_path):
    from PySide6.QtGui import QFont, QFontDatabase
    monkeypatch.setattr(ResonanceMainWindow, "_wire_bridge", lambda self: None)
    window = ResonanceMainWindow(settings=repository, initialize_on_startup=False)
    try:
        font_id = QFontDatabase.addApplicationFont("C:/Windows/Fonts/msyh.ttc")
        families = QFontDatabase.applicationFontFamilies(font_id)
        if families:
            window.setFont(QFont(families[0], 10))
        window.resize(1180, 720)
        window.show()
        workflow = window.workflow_page
        workflow.begin_workflow(["trade"], [])
        workflow.set_active_progress_cid("trade", "run")
        workflow.apply_progress_event("trade", event(1, data=MARKET))
        QApplication.processEvents()
        label = workflow.market_warning_label
        assert label.isVisible()
        assert label.height() >= label.fontMetrics().lineSpacing() * 2
        button = workflow.run_button
        assert window.rect().contains(button.mapTo(window, button.rect().center()))
        assert window.grab().save(str(tmp_path / "market-warning.png"))
    finally:
        window._close_ready = True
        dispose(window)
