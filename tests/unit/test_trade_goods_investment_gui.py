"""Goods investment remains a freight option, not a separate GUI task."""
import os
import json

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

import pytest
from PySide6.QtCore import QCoreApplication, QEvent, QSettings
from PySide6.QtWidgets import QApplication, QSpinBox

from packages.resonance_gui.config_repository import ResonanceConfigRepository, TRADE_PREVIEW_INPUT_KEYS
from packages.resonance_gui.logic import (
    TRADE_PROGRESS_EVENT, TRADE_PROGRESS_SCHEMA,
    WorkflowFreightProgressState, normalize_trade_task_inputs,
    reduce_workflow_freight_progress, trade_goods_investment_detail, trade_result_summary,
)
from packages.resonance_gui.main_window import ResonanceMainWindow
from packages.resonance_gui.widgets.small_tasks_page import SMALL_TASKS
from packages.resonance_gui.widgets.trade_page import TradePage
from packages.resonance_gui.widgets.workflow_page import WORKFLOW_TASKS, WorkflowPage
from plans.resonance_pc.src.actions import combined_commerce_pc_actions as combined


@pytest.fixture
def repository(tmp_path):
    app = QApplication.instance() or QApplication([])
    return ResonanceConfigRepository(QSettings(str(tmp_path / "investment.ini"), QSettings.IniFormat))


def dispose(widget):
    widget.close()
    widget.deleteLater()
    QCoreApplication.sendPostedEvents(None, QEvent.Type.DeferredDelete)
    QApplication.processEvents()


@pytest.fixture
def page(repository):
    widget = TradePage(repository)
    yield widget
    dispose(widget)


def event(sequence, stage, state, **fields):
    return {"name": TRADE_PROGRESS_EVENT, "payload": {
        "schema": TRADE_PROGRESS_SCHEMA, "cid": "investment-run", "sequence": sequence,
        "stage": stage, "state": state, **fields,
    }}


def test_default_disabled_and_not_registered_as_gui_task(page, repository):
    assert repository.load_trade_inputs()["auto_trade_goods_investment"] is False
    assert repository.load_trade_inputs()["trade_goods_investment_mode"] == 10
    assert page.collect_task_inputs()["trade_goods_investment_mode"] == 10
    assert isinstance(page.trade_goods_investment_mode, QSpinBox)
    assert page.trade_goods_investment_mode.minimum() == 1
    assert page.trade_goods_investment_mode.maximum() == 20
    assert page.trade_goods_investment_panel.layout().labelForField(page.trade_goods_investment_mode).text() == "目标等级"
    assert not page.auto_trade_goods_investment.isChecked()
    assert not page.trade_goods_investment_mode.isEnabled()
    assert not any("investment" in key for key, _ in SMALL_TASKS + WORKFLOW_TASKS)


@pytest.mark.parametrize("mode", range(1, 21))
def test_enabled_mode_round_trip_and_busy_controls(page, repository, mode):
    page.auto_trade_goods_investment.setChecked(True)
    page.trade_goods_investment_mode.setValue(mode)
    assert page.collect_task_inputs()["auto_trade_goods_investment"] is True
    assert page.collect_task_inputs()["trade_goods_investment_mode"] == mode
    page.save_ui_state()
    loaded = repository.load_trade_inputs()
    assert loaded["auto_trade_goods_investment"] is True
    assert loaded["trade_goods_investment_mode"] == mode
    page.trade_goods_investment_mode.setValue(10)
    page.set_inputs(loaded)
    assert page.trade_goods_investment_mode.value() == mode
    assert normalize_trade_task_inputs(loaded)["trade_goods_investment_mode"] == mode
    page.set_busy(True)
    assert not page.auto_trade_goods_investment.isEnabled()
    assert not page.trade_goods_investment_mode.isEnabled()
    page.set_busy(False)
    assert page.trade_goods_investment_mode.isEnabled()
    page.auto_trade_goods_investment.setChecked(False)
    assert not page.trade_goods_investment_mode.isEnabled()
    assert page.trade_goods_investment_mode.value() == mode


@pytest.mark.parametrize("value", [None, "unlock", "balanced", "full", "10", "invalid", True, False, 1.0, 10.5, [], {}, -1, 0, 21])
def test_invalid_mode_rejected(page, repository, value):
    with pytest.raises(ValueError, match="交易品投资目标等级"):
        normalize_trade_task_inputs({"trade_goods_investment_mode": value})
    page.save_ui_state()
    previous = repository.settings.value("trade/inputs_json")
    with pytest.raises(ValueError, match="交易品投资目标等级"):
        repository.save_trade_inputs({"trade_goods_investment_mode": value})
    assert repository.settings.value("trade/inputs_json") == previous
    with pytest.raises(ValueError, match="交易品投资目标等级"):
        page.set_inputs({"trade_goods_investment_mode": value})
    repository.settings.setValue("trade/inputs_json", json.dumps({"trade_goods_investment_mode": value}))
    with pytest.raises(ValueError, match="交易品投资目标等级"):
        repository.load_trade_inputs()


def test_missing_level_loads_default_and_malformed_json_keeps_existing_fallback(repository):
    repository.settings.setValue("trade/inputs_json", "{}")
    assert repository.load_trade_inputs()["trade_goods_investment_mode"] == 10
    repository.settings.setValue("trade/inputs_json", "malformed json")
    assert repository.load_trade_inputs()["trade_goods_investment_mode"] == 10


def test_combined_forwarding_is_freight_only_and_not_preview():
    values = {"auto_trade_goods_investment": True, "trade_goods_investment_mode": 20}
    assert combined._filtered(values, combined._TRADE_INPUT_KEYS) == values
    assert combined._filtered(values, combined._PASSENGER_INPUT_KEYS) == {}
    assert combined._filtered(values, combined._PREVIEW_INPUT_KEYS) == {}
    assert not set(values) & set(TRADE_PREVIEW_INPUT_KEYS)


def test_preview_emission_does_not_reintroduce_execution_options(repository):
    widget = TradePage(repository, preview_mode=True)
    try:
        widget.start_city.setCurrentIndex(widget.start_city.findData("1"))
        emitted = []
        widget.previewRequested.connect(lambda inputs, timeout: emitted.append(inputs))
        widget._request_preview()
        assert len(emitted) == 1
        assert not {"auto_trade_goods_investment", "trade_goods_investment_mode"} & emitted[0].keys()
        assert widget.auto_trade_goods_investment.isHidden()
        assert widget.trade_goods_investment_panel.isHidden()
    finally:
        dispose(widget)


def test_main_window_preview_filters_after_normalization(repository, monkeypatch):
    monkeypatch.setattr(ResonanceMainWindow, "_wire_bridge", lambda self: None)
    widget = ResonanceMainWindow(settings=repository, initialize_on_startup=False)
    try:
        emitted = []
        widget.requestPreviewPcTrade.connect(lambda inputs, timeout: emitted.append(inputs))
        widget._preview_pc_trade({"start_city_id": "1", "auto_trade_goods_investment": True,
                                 "trade_goods_investment_mode": 20}, 0)
        assert len(emitted) == 1
        assert not {"auto_trade_goods_investment", "trade_goods_investment_mode"} & emitted[0].keys()
    finally:
        widget._close_ready = True
        dispose(widget)


def test_workflow_places_investment_after_arrival_not_initial_city():
    route = [{"from_city": "修格里城", "to_city": "贡露城", "buy_products": ["铁矿石"]},
             {"from_city": "贡露城", "to_city": "远星大桥", "buy_products": ["扇贝"]}]
    state = reduce_workflow_freight_progress(None, event(1, "planning", "completed", data={"route": route}),
                                            goods_investment_enabled=True, rubbish_recycling_enabled=False)
    assert "trade_goods_investment" not in [phase.key for phase in state.cities[0].phases]
    assert [phase.key for phase in state.cities[1].phases] == ["arrival", "trade_goods_investment", "sell", "buy", "travel"]
    assert [phase.key for phase in state.cities[2].phases] == ["arrival", "trade_goods_investment", "final_sale"]
    state = reduce_workflow_freight_progress(state, event(2, "trade_goods_investment", "working", city_index=1,
        data={"product_index": 2, "current_level": 0, "target_level": 10, "preview_level": 6}))
    assert state.active_city_index == 1
    assert state.active_phase == "trade_goods_investment"
    assert "等级 0/10" in state.current_label
    assert "预览 6 级" in state.current_label


def test_workflow_start_preserves_option(repository):
    widget = WorkflowPage(repository)
    try:
        widget.begin_workflow(["trade"], [], trade_inputs={"auto_trade_goods_investment": True})
        assert widget._freight_progress.goods_investment_enabled
    finally:
        dispose(widget)


def test_explicit_backend_phase_and_result_summary_preserved():
    state = reduce_workflow_freight_progress(WorkflowFreightProgressState(), event(1, "planning", "completed", data={
        "route": [{"from_city": "A", "to_city": "B"}], "city_visits": [
            {"city_index": 0, "city_name": "A", "phases": [{"key": "sell"}]},
            {"city_index": 1, "city_name": "B", "phases": [{"key": "trade_goods_investment"}, {"key": "final_sale"}]},
        ],
    }))
    state = reduce_workflow_freight_progress(state, event(2, "trade_goods_investment", "completed", city_index=1,
        data={"transaction_count": 1, "reason": "no_further_upgrade"}))
    assert state.cities[1].phases[0].state == "completed"
    assert "本次无法继续投资" in state.cities[1].phases[0].detail
    result = {"completed_count": 1}
    assert trade_result_summary({"execution": {"trade_goods_investment": result}})["trade_goods_investment"] == result
    assert "交易品投资" in trade_goods_investment_detail({}, "started")


def test_next_product_attempted_reason_is_localized():
    assert "已尝试下一项，结束本次投资" in trade_goods_investment_detail(
        {"reason": "next_product_attempted"}, "completed")
