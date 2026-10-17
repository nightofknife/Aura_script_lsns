"""Actual city tax overrides and settlement presentation, without game input."""
import os
import json
from copy import deepcopy

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

import pytest
from PySide6.QtCore import QCoreApplication, QEvent, QSettings
from PySide6.QtWidgets import QApplication, QDialog, QMessageBox

from packages.resonance_gui.config_repository import (
    ResonanceConfigRepository, TRADE_INPUTS_KEY, TRADE_PREVIEW_INPUT_KEYS, TRADE_PREVIEW_INPUTS_KEY,
)
from packages.resonance_gui.logic import normalize_trade_task_inputs, freight_settlement_lines
from packages.resonance_gui.main_window import ResonanceMainWindow
from packages.resonance_gui.widgets.trade_page import CityPrestigeDialog, TradePage
from packages.resonance_gui.widgets.workflow_page import WorkflowPage


def dispose(widget):
    widget.close()
    widget.deleteLater()
    QCoreApplication.sendPostedEvents(None, QEvent.Type.DeferredDelete)
    QApplication.processEvents()


@pytest.fixture
def repository(tmp_path):
    return ResonanceConfigRepository(QSettings(str(tmp_path / "tax.ini"), QSettings.Format.IniFormat))


@pytest.fixture
def page(repository):
    app = QApplication.instance() or QApplication([])
    widget = TradePage(repository)
    yield widget
    dispose(widget)


def test_default_tax_map_is_empty_and_preview_can_use_overrides(repository):
    assert repository.load_trade_inputs()["city_tax_rates_bps"] == {}
    assert repository.load_trade_preview_inputs()["city_tax_rates_bps"] == {}
    assert "city_tax_rates_bps" in TRADE_PREVIEW_INPUT_KEYS


@pytest.mark.parametrize("rates", [None, [], "7.5", {14: 750}, {"": 750}, {" ": 750},
    {"14": True}, {"14": False}, {"14": 750.0}, {"14": "750"}, {"14": -1},
    {"14": 10001}, {"14": None}, {"14": []}])
def test_invalid_rates_rejected_without_overwriting_config(repository, rates):
    repository.save_trade_inputs({"city_tax_rates_bps": {"14": 750}})
    before = repository.settings.value(TRADE_INPUTS_KEY)
    with pytest.raises(ValueError, match="实际税率"):
        repository.save_trade_inputs({"city_tax_rates_bps": rates})
    assert repository.settings.value(TRADE_INPUTS_KEY) == before
    with pytest.raises(ValueError, match="实际税率"):
        normalize_trade_task_inputs({"city_tax_rates_bps": rates})


@pytest.mark.parametrize("mode", ["free", "fixed"])
def test_tax_map_round_trip_projection_and_immutable_snapshot(page, repository, mode):
    source = {"trade_mode": mode, "city_tax_rates_bps": {"19": 700, "14": 750, "1": 0, "2": 10000},
              "fixed_route_city_ids": ["19", "14"]}
    page.set_inputs(source)
    page.save_ui_state()
    repository.settings.sync()
    restored = ResonanceConfigRepository(QSettings(repository.settings.fileName(), QSettings.Format.IniFormat))
    assert restored.load_trade_inputs()["city_tax_rates_bps"] == source["city_tax_rates_bps"]
    values = page.collect_task_inputs()
    assert values["city_tax_rates_bps"] == source["city_tax_rates_bps"]
    page._city_tax_rates_bps["14"] = 500
    assert source["city_tax_rates_bps"]["14"] == 750
    assert values["city_tax_rates_bps"]["14"] == 750
    source["city_tax_rates_bps"]["19"] = 900
    assert page._city_tax_rates_bps["19"] == 700
    combined = ResonanceMainWindow._combined_commerce_inputs(order="trade_first", trade=values, passenger={})
    assert combined["trade_inputs"]["city_tax_rates_bps"]["14"] == 750
    assert "city_tax_rates_bps" not in combined["passenger_inputs"]


def test_percent_editor_zero_override_and_restore_rules(page):
    dialog = CityPrestigeDialog(tax_rates_bps={"19": 700, "14": 750}, parent=page)
    try:
        assert dialog.city_tax_rates["14"].value() == 7.5
        assert dialog.tax_enabled["14"].isChecked()
        dialog.tax_enabled["14"].setChecked(False)
        assert not dialog.city_tax_rates["14"].isEnabled()
        assert dialog.city_tax_rates["14"].value() == 7.5
        assert "14" not in dialog.tax_rates_value()
        dialog.tax_enabled["14"].setChecked(True)
        dialog.tax_enabled["1"].setChecked(True)
        dialog.city_tax_rates["1"].setValue(0)
        dialog.tax_enabled["2"].setChecked(True)
        dialog.city_tax_rates["2"].setValue(100)
        assert dialog.tax_rates_value() == {"19": 700, "14": 750, "1": 0, "2": 10000}
        dialog._restore_defaults()
        assert dialog.tax_rates_value() == {}
        assert dialog.city_tax_rates["14"].value() == 7.5
    finally:
        dispose(dialog)


def prime_plan(page):
    page.start_city.setCurrentIndex(page.start_city.findData("1"))
    page._last_inputs = page.collect_task_inputs(preview=True)
    page.finish_preview({"status": "planned", "expected_profit": 96})
    assert not page._plan_stale


@pytest.mark.parametrize("preview", [False, True])
def test_tax_dialog_save_invalidates_plan_and_preserves_other_draft(page, repository, monkeypatch, preview):
    widget = TradePage(repository, preview_mode=True) if preview else page
    try:
        prime_plan(widget)
        before = widget._plan_fingerprint
        widget.cargo_capacity.setValue(747)
        prime_plan(widget)
        monkeypatch.setattr(CityPrestigeDialog, "exec", lambda self: QDialog.DialogCode.Accepted)
        monkeypatch.setattr(CityPrestigeDialog, "tax_rates_value", lambda self: {"14": 750, "19": 700})
        widget.city_prestige_button.click()
        assert widget._plan_stale
        assert not widget.preview_state_hint.isHidden()
        assert not widget.preview_result_hint.isHidden()
        assert widget.cargo_capacity.value() == 747
        assert widget.collect_ui_state()["city_tax_rates_bps"] == {"14": 750, "19": 700}
        assert before["city_tax_rates_bps"] == {}
        widget.save_ui_state()
        stored = repository.load_trade_preview_inputs() if preview else repository.load_trade_inputs()
        assert stored["city_tax_rates_bps"] == {"14": 750, "19": 700}
        if preview:
            emitted = []
            widget.previewRequested.connect(lambda inputs, timeout: emitted.append(inputs))
            widget._request_preview()
            assert emitted[0]["city_tax_rates_bps"] == {"14": 750, "19": 700}
        else:
            widget.mode_buttons["fixed"].setChecked(True)
            widget.mode_buttons["free"].setChecked(True)
            assert widget._city_tax_rates_bps == {"14": 750, "19": 700}
    finally:
        if preview:
            dispose(widget)


def test_dialog_cancel_or_save_failure_keeps_current_rates(page, repository, monkeypatch):
    page.set_inputs({"city_tax_rates_bps": {"14": 750}})
    page.save_ui_state()
    monkeypatch.setattr(CityPrestigeDialog, "tax_rates_value", lambda self: {"14": 1000})
    monkeypatch.setattr(CityPrestigeDialog, "exec", lambda self: QDialog.DialogCode.Rejected)
    page.city_prestige_button.click()
    assert page._city_tax_rates_bps == {"14": 750}
    monkeypatch.setattr(CityPrestigeDialog, "exec", lambda self: QDialog.DialogCode.Accepted)
    def fail_save(_values):
        raise OSError("read only")
    monkeypatch.setattr(page, "_save_inputs", fail_save)
    warnings = []
    monkeypatch.setattr(QMessageBox, "warning", lambda *args: warnings.append(args[2]))
    page.city_prestige_button.click()
    assert warnings == ["read only"]
    assert page._city_tax_rates_bps == {"14": 750}
    assert repository.load_trade_inputs()["city_tax_rates_bps"] == {"14": 750}


def test_preview_draft_independent_and_unselected_city_tax_retained(page, repository):
    page.set_inputs({"city_tax_rates_bps": {"14": 750}})
    page.city_checks["14"].setChecked(False)
    page.save_ui_state()
    initial = repository.load_trade_preview_inputs()
    assert initial["city_tax_rates_bps"] == {"14": 750}
    initial["city_tax_rates_bps"] = {"14": 700}
    repository.save_trade_preview_inputs(initial)
    assert repository.load_trade_inputs()["city_tax_rates_bps"] == {"14": 750}
    assert repository.load_trade_preview_inputs()["city_tax_rates_bps"] == {"14": 700}


@pytest.mark.parametrize("preview", [False, True])
def test_invalid_saved_tax_is_reported_not_silently_reset(repository, preview):
    key = TRADE_PREVIEW_INPUTS_KEY if preview else TRADE_INPUTS_KEY
    raw = json.dumps({"cargo_capacity": 747, "city_tax_rates_bps": {"14": "750"}})
    repository.settings.setValue(key, raw)
    with pytest.raises(ValueError, match="实际税率"):
        (repository.load_trade_preview_inputs if preview else repository.load_trade_inputs)()
    assert repository.settings.value(key) == raw


@pytest.mark.parametrize("preview", [False, True])
def test_persistence_failure_rolls_back_tax_and_other_fields(repository, monkeypatch, preview):
    save = repository.save_trade_preview_inputs if preview else repository.save_trade_inputs
    load = repository.load_trade_preview_inputs if preview else repository.load_trade_inputs
    save({"city_tax_rates_bps": {"14": 750}, "cargo_capacity": 747})
    def fail_sync():
        raise OSError("access denied")
    monkeypatch.setattr(repository, "sync_checked", fail_sync)
    with pytest.raises(OSError, match="access denied"):
        save({"city_tax_rates_bps": {"14": 700}, "cargo_capacity": 555})
    assert load()["city_tax_rates_bps"] == {"14": 750}
    assert load()["cargo_capacity"] == 747


@pytest.mark.parametrize("preview", [False, True])
@pytest.mark.parametrize("mode", ["free", "fixed"])
def test_unsupported_city_draft_retained_but_run_blocked(page, repository, preview, mode):
    assert "21" not in repository.load_trade_inputs()["available_city_ids"]
    widget = TradePage(repository, preview_mode=True) if preview else page
    try:
        widget.set_inputs({"trade_mode": mode, "available_city_ids": ["1", "21"],
                           "fixed_route_city_ids": ["1", "21"], "start_city_id": "1"})
        widget.set_target_status({"ok": True, "target": {"title": "fake", "hwnd": 123}})
        widget.save_ui_state()
        stored = repository.load_trade_preview_inputs() if preview else repository.load_trade_inputs()
        assert "21" in stored["available_city_ids"]
        assert stored["fixed_route_city_ids"] == ["1", "21"]
        assert "武林源" in widget.parameter_notice.text()
        assert not (widget.preview_button if preview else widget.start_button).isEnabled()
        with pytest.raises(ValueError, match="武林源"):
            widget.collect_task_inputs(preview=preview)
        widget.mode_buttons["fixed"].setChecked(True)
        widget.fixed_route.clear()
        widget.add_route_city("1")
        widget.add_route_city("2")
        # Inactive free-mode city drafts must not block valid fixed routes.
        assert not widget._form_error()
        assert "available_city_ids" not in widget.collect_task_inputs(preview=preview)
        assert "21" in widget.collect_ui_state()["available_city_ids"]
    finally:
        if preview:
            dispose(widget)


@pytest.mark.parametrize("values", [
    {"trade_mode": "free", "start_city_id": "21"},
    {"trade_mode": "fixed", "start_city_id": "21"},
    {"trade_mode": "free", "required_end_city_ids": ["21"]},
    {"trade_mode": "free", "available_city_ids": ["1", "21"]},
    {"trade_mode": "fixed", "fixed_route_city_ids": ["1", "21"]},
])
def test_unsupported_city_normalization_contract(values):
    with pytest.raises(ValueError, match="武林源"):
        normalize_trade_task_inputs(values)


SAMPLE_LEG = {
    "from_city": "贡露城", "to_city": "远星大桥", "from_city_id": "19", "to_city_id": "14",
    "expected_profit": 96, "expected_fatigue_cost": 3,
    "settlement": {"buy_goods_amount": 1376, "buy_tax": 96, "buy_total": 1472,
                   "sell_goods_amount": 1584, "taxable_profit": 208, "sell_tax": 16,
                   "sell_total": 1568, "net_profit": 96},
}


def test_sample_settlement_display_does_not_confuse_screen_profit_with_net(page, repository):
    original = deepcopy(SAMPLE_LEG)
    page._render_route([SAMPLE_LEG])
    row = page.route_tree.topLevelItem(0)
    assert row.text(4) == "96"
    assert row.childCount() == 1
    assert row.child(0).isFirstColumnSpanned()
    details = row.child(0).text(0)
    for text in ("买入支出 1,472", "买入税 96", "卖出到账 1,568", "卖出利润税 16",
                 "计税利润 208", "预计净收益 96", "未实测"):
        assert text in details
    assert "净收益" in page.result_captions["expected_profit"].text()
    assert "税前利润不等于净收益" in page.net_profit_note.text()
    workflow = WorkflowPage(repository)
    try:
        workflow._freight_progress.route = [SAMPLE_LEG]
        workflow._freight_progress.summary = {"expected_profit": 96}
        workflow._render_runtime_trade_plan()
        card = workflow.runtime_plan_leg_cards[0]
        assert "预计净收益  96" in card.profit_label.text()
        assert card.settlement_label.text() == details
        assert "未确认" not in card.settlement_label.text()  # Expected, not asserted actual income.
    finally:
        dispose(workflow)
    assert SAMPLE_LEG == original


@pytest.mark.parametrize("settlement", [None, {}, "invalid", []])
def test_missing_settlement_is_not_fabricated(settlement):
    assert freight_settlement_lines({"settlement": settlement}) == []


def test_partial_settlement_shows_unknown_without_computing_net():
    details = "\n".join(freight_settlement_lines({"settlement": {"buy_total": 1472, "sell_total": 1568}}))
    assert "预计净收益 未提供" in details
    assert "买入税 未提供" in details
    assert "利润税 未提供" in details


def test_city_dialog_tax_tab_is_readable_and_scrollable(page, tmp_path):
    from PySide6.QtGui import QFont, QFontDatabase
    from PySide6.QtWidgets import QScrollArea
    font_id = QFontDatabase.addApplicationFont("C:/Windows/Fonts/msyh.ttc")
    families = QFontDatabase.applicationFontFamilies(font_id)
    dialog = CityPrestigeDialog(tax_rates_bps={"14": 750, "19": 700}, parent=page)
    try:
        if families:
            dialog.setFont(QFont(families[0], 10))
        dialog.resize(700, 600)
        dialog.tabs.setCurrentIndex(1)
        dialog.show()
        QApplication.processEvents()
        scroll = dialog.tabs.currentWidget().findChild(QScrollArea)
        for field in dialog.city_tax_rates.values():
            assert field.height() >= field.fontMetrics().height()
            scroll.ensureWidgetVisible(field)
            QApplication.processEvents()
            assert scroll.viewport().rect().contains(field.mapTo(scroll.viewport(), field.rect().center()))
        assert scroll.horizontalScrollBar().maximum() == 0
        scroll.verticalScrollBar().setValue(0)
        assert dialog.grab().save(str(tmp_path / "city-tax-overrides.png"))
    finally:
        dispose(dialog)
