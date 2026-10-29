"""Execution-only freight trailer options; no game input or global style changes."""
import os

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

import pytest
from PySide6.QtCore import QCoreApplication, QEvent, QSettings
from PySide6.QtWidgets import QApplication

from packages.resonance_gui.config_repository import (
    ResonanceConfigRepository, TRADE_INPUTS_KEY, TRADE_PREVIEW_INPUT_KEYS,
)
from packages.resonance_gui.logic import normalize_trade_task_inputs
from packages.resonance_gui.main_window import ResonanceMainWindow
from packages.resonance_gui.widgets.trade_page import TradePage

KEYS = {"auto_trailer", "trailer_daily_first_n", "trailer_payment"}


@pytest.fixture
def repository(tmp_path):
    return ResonanceConfigRepository(QSettings(str(tmp_path / "trailer.ini"), QSettings.Format.IniFormat))


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


def test_defaults_and_disabled_fields(page, repository):
    values = repository.load_trade_inputs()
    assert {key: values[key] for key in KEYS} == {
        "auto_trailer": False, "trailer_daily_first_n": 1, "trailer_payment": "gold"}
    assert not page.auto_trailer.isChecked()
    assert not page.trailer_daily_first_n.isEnabled()
    assert not page.trailer_payment.isEnabled()
    assert [page.trailer_payment.itemText(i) for i in range(2)] == ["铁盟币", "里程点"]


@pytest.mark.parametrize("mode", ["free", "fixed"])
@pytest.mark.parametrize("payment", ["gold", "mileage"])
@pytest.mark.parametrize("count", [1, 99, 2**80])
def test_persistence_modes_disabled_draft_and_snapshot(page, repository, mode, payment, count):
    page.mode_buttons[mode].setChecked(True)
    if mode == "fixed":
        page.add_route_city("1")
        page.add_route_city("2")
    page.auto_trailer.setChecked(True)
    page.trailer_daily_first_n.setText(str(count))
    page.trailer_payment.setCurrentIndex(page.trailer_payment.findData(payment))
    page.save_ui_state()
    repository.settings.sync()
    reopened = ResonanceConfigRepository(QSettings(repository.settings.fileName(), QSettings.Format.IniFormat))
    page.set_inputs(reopened.load_trade_inputs())
    expected = {"auto_trailer": True, "trailer_daily_first_n": count, "trailer_payment": payment}
    assert {key: page.collect_task_inputs()[key] for key in KEYS} == expected
    emitted = []
    page.startRequested.connect(lambda inputs, timeout: emitted.append(inputs))
    page._request_start()
    page.trailer_daily_first_n.setText("3")
    assert emitted[0]["trailer_daily_first_n"] == count
    page.auto_trailer.setChecked(False)
    page.save_ui_state()
    page.set_inputs(reopened.load_trade_inputs())
    assert page.trailer_daily_first_n.text() == "3"
    assert page.trailer_payment.currentData() == payment
    assert not page.trailer_daily_first_n.isEnabled()
    page.auto_trailer.setChecked(True)
    page.set_busy(True)
    assert not page.auto_trailer.isEnabled()
    assert not page.trailer_daily_first_n.isEnabled()
    assert not page.trailer_payment.isEnabled()
    page.set_busy(False)
    assert page.trailer_daily_first_n.isEnabled()


@pytest.mark.parametrize("key,value", [
    ("trailer_daily_first_n", value) for value in (0, -1, True, False, 1.0, "2", None, [], {})
] + [("trailer_payment", value) for value in ("coin", "", None, 1, [])]
  + [("auto_trailer", value) for value in ("true", 1, None)])
def test_strict_validation_and_failed_save_preserves_configuration(repository, key, value):
    repository.save_trade_inputs({})
    previous = repository.settings.value(TRADE_INPUTS_KEY)
    with pytest.raises(ValueError, match="拖车"):
        repository.save_trade_inputs({key: value})
    assert repository.settings.value(TRADE_INPUTS_KEY) == previous
    with pytest.raises(ValueError, match="拖车"):
        normalize_trade_task_inputs({key: value})


@pytest.mark.parametrize("text", ["", "0", "-1", "1.5", "abc"])
def test_invalid_active_count_blocks_start_but_disabled_count_is_not_applicable(page, text):
    page.set_target_status({"ok": True, "target": {"title": "fake", "hwnd": 123}})
    page.auto_trailer.setChecked(True)
    page.trailer_daily_first_n.setText("12")
    page.trailer_daily_first_n.setText(text)
    assert "正整数" in page._form_error()
    assert not page.start_button.isEnabled()
    with pytest.raises(ValueError, match="正整数"):
        page.collect_task_inputs()
    page.auto_trailer.setChecked(False)
    assert not page._form_error()
    assert page.collect_task_inputs()["trailer_daily_first_n"] == 12


def test_preview_is_separate_and_changes_do_not_invalidate_plan(page, repository):
    page.start_city.setCurrentIndex(page.start_city.findData("1"))
    page._last_inputs = page.collect_task_inputs(preview=True)
    page.finish_preview({"status": "planned", "expected_profit": 123})
    page.auto_trailer.setChecked(True)
    page.trailer_daily_first_n.setText("8")
    page.trailer_payment.setCurrentIndex(1)
    assert not page._plan_stale
    page.save_ui_state()
    assert not KEYS & set(TRADE_PREVIEW_INPUT_KEYS)
    assert not KEYS & repository.load_trade_preview_inputs().keys()
    preview = TradePage(repository, preview_mode=True)
    try:
        preview.start_city.setCurrentIndex(preview.start_city.findData("1"))
        assert preview.auto_trailer.isHidden()
        assert preview.trailer_panel.isHidden()
        assert not KEYS & preview.collect_task_inputs(preview=True).keys()
        emitted = []
        preview.previewRequested.connect(lambda inputs, timeout: emitted.append(inputs))
        preview._request_preview()
        assert emitted and not KEYS & emitted[0].keys()
    finally:
        dispose(preview)


def test_workflow_and_combined_forward_only_to_freight(repository, monkeypatch):
    app = QApplication.instance() or QApplication([])
    monkeypatch.setattr(ResonanceMainWindow, "_wire_bridge", lambda self: None)
    window = ResonanceMainWindow(settings=repository, initialize_on_startup=False)
    try:
        page = window.trade_page
        page.auto_trailer.setChecked(True)
        page.trailer_daily_first_n.setText("7")
        page.trailer_payment.setCurrentIndex(1)
        for task, button in window.workflow_page._task_checks.items():
            button.setChecked(task in ("trade", "passenger"))
        pending = []
        monkeypatch.setattr(window, "_begin_workflow", lambda tasks, *args: pending.extend(tasks))
        window._start_workflow()
        assert [step["dispatch"] for step in pending] == ["pc_task", "combined_commerce"]
        assert pending[0]["step"] == "refresh_recovery"
        assert pending[0]["inputs"]["stages"] == ["prison_products"]
        assert not KEYS & pending[0]["inputs"].keys()
        combined = pending[1]["inputs"]
        assert combined["trade_inputs"]["trailer_daily_first_n"] == 7
        assert combined["trade_inputs"]["trailer_payment"] == "mileage"
        assert not KEYS & combined["passenger_inputs"].keys()
        page.trailer_daily_first_n.setText("4")
        assert combined["trade_inputs"]["auto_trailer"] is True
        assert combined["trade_inputs"]["trailer_daily_first_n"] == 7
        assert not KEYS & combined["passenger_inputs"].keys()
    finally:
        window._close_ready = True
        dispose(window)


@pytest.mark.parametrize("size", [(1180, 720), (800, 600)])
def test_compact_trailer_fields_remain_distinct_and_visible(page, tmp_path, size):
    from PySide6.QtGui import QFont, QFontDatabase
    font_id = QFontDatabase.addApplicationFont("C:/Windows/Fonts/msyh.ttc")
    families = QFontDatabase.applicationFontFamilies(font_id)
    if families:
        page.setFont(QFont(families[0], 10))
    page.recovery_section.toggle.setChecked(True)
    page.resize(*size)
    page.show()
    page.auto_trailer.setChecked(True)
    QApplication.processEvents()
    fields = (page.trailer_daily_first_n, page.trailer_payment)
    from PySide6.QtCore import QRect, QPoint
    rects = [QRect(widget.mapTo(page.trailer_panel, QPoint()), widget.size()) for widget in fields]
    assert not rects[0].intersects(rects[1])
    for widget, rect in zip(fields, rects):
        assert widget.isVisibleTo(page.trailer_panel)
        assert page.trailer_panel.rect().contains(rect)
        assert 120 <= widget.width() <= 220
    assert page.trailer_panel.grab().save(str(tmp_path / "trailer-panel.png"))
