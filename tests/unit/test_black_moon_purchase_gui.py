"""Offline GUI/configuration contracts; no game actions or input injection."""

import json
import os
from pathlib import Path

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

import pytest
from PySide6.QtCore import QCoreApplication, QEvent, QSettings, Qt
from PySide6.QtGui import QFont, QFontDatabase
from PySide6.QtTest import QTest
from PySide6.QtWidgets import QApplication, QDialog, QMessageBox

from packages.resonance_gui.black_moon_catalog import (
    BlackMoonCatalogError, CATEGORY_NAMES, EXECUTION_KEYS, load_black_moon_catalog,
    normalize_black_moon_settings, normalize_purchase_table,
)
from packages.resonance_gui.config_repository import ResonanceConfigRepository, TRADE_PREVIEW_INPUT_KEYS
from packages.resonance_gui.logic import normalize_trade_task_inputs
from packages.resonance_gui.main_window import ResonanceMainWindow
from packages.resonance_gui.style import APP_STYLE
from packages.resonance_gui.widgets.black_moon_purchase_dialog import BlackMoonPurchaseDialog, PRODUCT_ROLE
from packages.resonance_gui.widgets.trade_page import TradePage


@pytest.fixture(scope="module")
def app():
    instance = QApplication.instance() or QApplication([])
    # The offscreen Qt platform does not discover Windows fallback fonts.
    font_file = Path(os.environ.get("WINDIR", "C:/Windows")) / "Fonts/msyh.ttc"
    if font_file.is_file():
        QFontDatabase.addApplicationFont(str(font_file))
    yield instance


@pytest.fixture
def catalog():
    return load_black_moon_catalog()


@pytest.fixture
def repository(tmp_path, app):
    return ResonanceConfigRepository(QSettings(str(tmp_path / "gui.ini"), QSettings.Format.IniFormat))


def dispose(widget):
    widget.close()
    widget.deleteLater()
    QCoreApplication.sendPostedEvents(None, QEvent.Type.DeferredDelete)
    QApplication.processEvents()


@pytest.fixture
def dialog(catalog, app):
    widget = BlackMoonPurchaseDialog(catalog, {})
    yield widget
    dispose(widget)


@pytest.fixture
def page(repository):
    widget = TradePage(repository)
    yield widget
    dispose(widget)


def test_canonical_catalog_complete_and_defaults(catalog, repository):
    assert len(catalog.item_ids) == len(set(catalog.item_ids)) == 140
    assert {key: sum(p.category == key for p in catalog.products) for key in CATEGORY_NAMES} == {
        "equipment": 100, "equipment_box": 15, "item": 11, "material": 14,
    }
    values = repository.load_trade_inputs()
    assert values["auto_black_moon_local_purchase"] is False
    assert values["black_moon_purchase_record_scope"] == "default"
    assert set(values["black_moon_purchase_table"]) == set(catalog.item_ids)
    assert not any(values["black_moon_purchase_table"].values())
    assert not set(EXECUTION_KEYS) & set(TRADE_PREVIEW_INPUT_KEYS)


@pytest.mark.parametrize("table", [None, [], ["blackstone_armor"], {"bad_id": True}, {"blackstone_armor": 1},
                                  {"blackstone_armor": "false"}, {1: True}])
def test_invalid_map_rejected_without_overwriting(table, catalog, repository):
    repository.save_trade_inputs({"cargo_capacity": 837})
    before = repository.settings.value("trade/two_mode_inputs_json")
    with pytest.raises(ValueError):
        normalize_purchase_table(table, catalog)
    with pytest.raises(ValueError):
        repository.save_trade_inputs({"black_moon_purchase_table": table})
    assert repository.settings.value("trade/two_mode_inputs_json") == before


def test_invalid_stored_table_does_not_reset_other_settings(repository):
    raw = json.dumps({"cargo_capacity": 837, "black_moon_purchase_table": {"unknown": True}})
    repository.settings.setValue("trade/two_mode_inputs_json", raw)
    with pytest.raises(ValueError, match="未知商品"):
        repository.load_trade_inputs()
    assert repository.settings.value("trade/two_mode_inputs_json") == raw


@pytest.mark.parametrize("scope", [None, 0, True, "", " ", " default", "default ", "\tdefault", "default\n", "x" * 129])
def test_invalid_record_scope_rejected_without_trimming_or_overwriting(repository, scope):
    repository.save_trade_inputs({"cargo_capacity": 837})
    before = repository.settings.value("trade/two_mode_inputs_json")
    inputs = {"black_moon_purchase_record_scope": scope}
    for validator in (normalize_black_moon_settings, normalize_trade_task_inputs,
                      repository.save_trade_inputs, repository.save_black_moon_settings):
        with pytest.raises(ValueError, match="记录范围"):
            validator(inputs)
    assert repository.settings.value("trade/two_mode_inputs_json") == before
    raw = json.dumps({"cargo_capacity": 837, **inputs})
    repository.settings.setValue("trade/two_mode_inputs_json", raw)
    with pytest.raises(ValueError, match="记录范围"):
        repository.load_trade_inputs()
    assert repository.settings.value("trade/two_mode_inputs_json") == raw


@pytest.mark.parametrize("scope", ["default", "a", "scope name", "x" * 128, "汉" * 128])
def test_record_scope_boundary_and_round_trip_preserve_exact_string(repository, scope):
    inputs = {"black_moon_purchase_record_scope": scope}
    assert normalize_black_moon_settings(inputs)["black_moon_purchase_record_scope"] == scope
    assert normalize_trade_task_inputs(inputs)["black_moon_purchase_record_scope"] == scope
    repository.save_black_moon_settings(inputs)
    assert repository.load_trade_inputs()["black_moon_purchase_record_scope"] == scope


def test_all_items_show_once_and_categories_count(dialog):
    assert dialog.proxy.rowCount() == 140
    assert len(set(dialog.proxy.matching_ids())) == 140
    for row, count in enumerate((140, 100, 15, 11, 14)):
        dialog.categories.setCurrentRow(row)
        assert dialog.proxy.rowCount() == count
        assert f"0/{count}" in dialog.categories.item(row).text()


def test_static_purchase_help_is_tooltip_not_body_row(dialog, page):
    assert dialog.notice.isHidden()
    assert dialog.notice.text() == ""
    assert "保存设置不会购买" in dialog.proxy.index(0, 0).data(Qt.ItemDataRole.ToolTipRole)
    assert "不是数量上限" in page.black_moon_settings_button.toolTip()
    dialog.set_read_only(True)
    assert not dialog.notice.isHidden()
    assert "运行期间仅可查看" in dialog.notice.text()
    dialog.set_read_only(False)
    assert dialog.notice.isHidden()


def test_bulk_selection_uses_all_matching_ids_not_visible_viewport(dialog):
    dialog.categories.setCurrentRow(1)
    dialog.quality.setCurrentIndex(dialog.quality.findData("Golden"))
    expected = dialog.proxy.matching_ids()
    assert len(expected) == 56
    dialog._set_matching(True)
    assert sum(dialog.product_model.purchase_table().values()) == 56
    dialog.categories.setCurrentRow(2)
    dialog._set_matching(True)
    assert sum(dialog.product_model.purchase_table().values()) == 61
    dialog.categories.setCurrentRow(1)
    dialog._set_matching(False)
    assert sum(dialog.product_model.purchase_table().values()) == 5
    assert {item_id for item_id, checked in dialog.product_model.purchase_table().items() if checked} == {
        product.item_id for product in dialog.product_model.catalog.products
        if product.category == "equipment_box" and product.quality == "Golden"
    }


def test_search_selected_only_and_clear_snapshot(dialog):
    dialog.categories.setCurrentRow(1)
    dialog._set_matching(True)
    table = dialog.product_model.purchase_table()
    dialog.selected_only.setChecked(True)
    assert dialog.proxy.rowCount() == 100
    dialog.search.setText("黑石")
    matches = dialog.proxy.matching_ids()
    assert matches
    assert all("黑石" in dialog.proxy.index(row, 0).data(PRODUCT_ROLE).name for row in range(dialog.proxy.rowCount()))
    dialog._set_matching(False)
    assert dialog.proxy.rowCount() == 0
    assert all(not dialog.product_model.purchase_table()[key] for key in matches)
    assert all(dialog.product_model.purchase_table()[key] == value for key, value in table.items() if key not in matches)
    dialog.search.clear()
    dialog._set_matching(False)
    assert not any(dialog.product_model.purchase_table().values())
    assert not dialog.select_all.isEnabled()
    assert not dialog.clear_all.isEnabled()


def test_filter_changes_never_change_selection(dialog):
    dialog._toggle_item(dialog.proxy.index(0, 0))
    original = dialog.product_model.purchase_table()
    dialog.categories.setCurrentRow(4)
    dialog.search.setText("不存在的商品")
    dialog.quality.setCurrentIndex(1)
    dialog.selected_only.setChecked(True)
    assert dialog.product_model.purchase_table() == original


def test_real_mouse_and_keyboard_toggle_without_accepting_dialog(dialog, app):
    dialog.show()
    app.processEvents()
    first = dialog.proxy.index(0, 0)
    rect = dialog.grid.visualRect(first)
    QTest.mouseClick(dialog.grid.viewport(), Qt.MouseButton.LeftButton, pos=rect.center())
    assert sum(dialog.product_model.purchase_table().values()) == 1
    dialog.grid.setCurrentIndex(first)
    QTest.keyClick(dialog.grid, Qt.Key.Key_Space)
    assert sum(dialog.product_model.purchase_table().values()) == 0
    QTest.keyClick(dialog.grid, Qt.Key.Key_Return)
    assert sum(dialog.product_model.purchase_table().values()) == 1
    assert dialog.isVisible()
    assert dialog.saved_table() is None


@pytest.mark.parametrize("close_action", ["reject", "close", "escape"])
def test_cancel_discards_draft(catalog, app, close_action):
    original = normalize_purchase_table({}, catalog)
    committed = []
    widget = BlackMoonPurchaseDialog(catalog, original, save=lambda table: committed.append(table))
    try:
        widget.show()
        app.processEvents()
        widget._set_matching(True)
        if close_action == "escape":
            QTest.keyClick(widget, Qt.Key.Key_Escape)
        else:
            getattr(widget, close_action)()
        assert committed == []
        assert not any(original.values())
        assert widget.saved_table() is None
    finally:
        dispose(widget)


def test_save_updates_page_only_after_persist_and_restart(page, repository, catalog):
    table = normalize_purchase_table({catalog.item_ids[0]: True}, catalog)
    widget = BlackMoonPurchaseDialog(catalog, {}, save=page._commit_black_moon_table)
    try:
        widget.product_model.apply_selection((catalog.item_ids[0],), True)
        assert "已选 0 / 140" == page.black_moon_selection_label.text()
        widget.accept()
        assert widget.result() == QDialog.DialogCode.Accepted
        assert "已选 1 / 140" == page.black_moon_selection_label.text()
        reopened = ResonanceConfigRepository(QSettings(repository.settings.fileName(), QSettings.Format.IniFormat))
        assert reopened.load_trade_inputs()["black_moon_purchase_table"] == table
        assert not reopened.load_trade_inputs()["auto_black_moon_local_purchase"]
    finally:
        dispose(widget)


def test_save_failure_retains_draft_and_rolls_back_page(page, repository, catalog, monkeypatch, app):
    repository.save_trade_inputs({"cargo_capacity": 837})
    previous = repository.settings.value("trade/two_mode_inputs_json")
    previous_table = dict(page._black_moon_table)
    errors = []
    monkeypatch.setattr(QMessageBox, "warning", lambda *args: errors.append(args[2]))
    monkeypatch.setattr(repository, "sync_checked", lambda: (_ for _ in ()).throw(OSError("不可写")))
    widget = BlackMoonPurchaseDialog(catalog, previous_table, save=page._commit_black_moon_table)
    try:
        widget.show()
        widget._set_matching(True)
        widget.accept()
        assert widget.isVisible()
        assert widget.saved_table() is None
        assert sum(widget.product_model.purchase_table().values()) == 140
        assert page._black_moon_table == previous_table
        assert page.black_moon_selection_label.text() == "已选 0 / 140"
        assert repository.settings.value("trade/two_mode_inputs_json") == previous
        assert errors == ["不可写"]
        page.auto_black_moon_local_purchase.setChecked(True)
        assert not page.auto_black_moon_local_purchase.isChecked()
        assert repository.settings.value("trade/two_mode_inputs_json") == previous
    finally:
        dispose(widget)


def test_dialog_save_does_not_validate_or_overwrite_unsaved_mode_draft(page, repository, catalog):
    repository.save_trade_inputs({"fatigue_budget": 700})
    page.fatigue_budget.setValue(823)
    page.mode_buttons["fixed"].setChecked(True)  # An incomplete route must not prevent saving a purchase table.
    page._commit_black_moon_table(normalize_purchase_table({catalog.item_ids[0]: True}, catalog))
    assert page.fatigue_budget.value() == 823
    assert repository.load_trade_inputs()["fatigue_budget"] == 700


def test_start_save_failure_does_not_dispatch(page, repository, monkeypatch):
    errors, dispatched = [], []
    page.startRequested.connect(lambda *args: dispatched.append(args))
    monkeypatch.setattr(QMessageBox, "warning", lambda *args: errors.append(args[2]))
    monkeypatch.setattr(repository, "sync_checked", lambda: (_ for _ in ()).throw(OSError("不可写")))
    page._request_start()
    assert errors == ["不可写"]
    assert not dispatched


def test_combined_entry_save_failure_does_not_dispatch(repository, monkeypatch):
    monkeypatch.setattr(ResonanceMainWindow, "_wire_bridge", lambda self: None)
    errors, dispatched = [], []
    widget = ResonanceMainWindow(settings=repository, initialize_on_startup=False)
    try:
        widget.requestRunPcTrade.connect(lambda *args: dispatched.append(args))
        widget.requestRunPcCombinedCommerce.connect(lambda *args: dispatched.append(args))
        monkeypatch.setattr(QMessageBox, "warning", lambda *args: errors.append(args[2]))
        monkeypatch.setattr(repository, "sync_checked", lambda: (_ for _ in ()).throw(OSError("不可写")))
        for task_id, check in widget.workflow_page._task_checks.items():
            check.setChecked(task_id in {"trade", "passenger"})
        widget._start_workflow()
        assert len(errors) == 1 and "不可写" in errors[0]
        assert not dispatched
        assert not widget._workflow_active
    finally:
        widget._close_ready = True
        dispose(widget)


def test_enabled_empty_table_is_valid_and_hover_explanations(page):
    page.auto_black_moon_local_purchase.setChecked(True)
    values = page.collect_task_inputs()
    assert values["auto_black_moon_local_purchase"]
    assert not any(values["black_moon_purchase_table"].values())
    assert page.auto_black_moon_local_purchase.toolTip()


def test_toggle_off_retains_selection_and_settings_remain_available(page, repository, catalog):
    page._commit_black_moon_table(normalize_purchase_table({catalog.item_ids[0]: True}, catalog))
    page.auto_black_moon_local_purchase.setChecked(True)
    assert repository.load_trade_inputs()["auto_black_moon_local_purchase"]
    page.auto_black_moon_local_purchase.setChecked(False)
    assert repository.load_trade_inputs()["black_moon_purchase_table"][catalog.item_ids[0]]
    assert page.black_moon_settings_button.isEnabled()


@pytest.mark.parametrize("mode", ["free", "fixed"])
def test_two_modes_share_configuration_and_snapshot_is_independent(page, catalog, mode):
    values = page._settings.load_trade_inputs()
    values.update(trade_mode=mode, fixed_route_city_ids=["1", "2"], target_profit=100000,
                  auto_black_moon_local_purchase=True,
                  black_moon_purchase_table={catalog.item_ids[0]: True})
    page.set_inputs(values)
    inputs = page.collect_task_inputs()
    assert inputs["black_moon_purchase_table"][catalog.item_ids[0]]
    assert inputs["auto_black_moon_local_purchase"]
    assert inputs["black_moon_purchase_record_scope"] == "default"
    page._black_moon_table[catalog.item_ids[0]] = False
    assert inputs["black_moon_purchase_table"][catalog.item_ids[0]]
    combined = ResonanceMainWindow._combined_commerce_inputs(order="trade_first", trade=inputs,
                                                            passenger={"trip_count": 1})
    inputs["black_moon_purchase_table"][catalog.item_ids[0]] = False
    assert combined["trade_inputs"]["black_moon_purchase_table"][catalog.item_ids[0]]
    assert not set(EXECUTION_KEYS) & combined["passenger_inputs"].keys()


def test_busy_read_only_and_cannot_commit(page, catalog, dialog):
    page._black_moon_dialog = dialog
    page.set_busy(True)
    assert not page.auto_black_moon_local_purchase.isEnabled()
    assert page.black_moon_settings_button.isEnabled()  # Viewing is allowed.
    before = dialog.product_model.purchase_table()
    dialog._set_matching(True)
    dialog._toggle_item(dialog.proxy.index(0, 0))
    dialog.accept()
    assert dialog.product_model.purchase_table() == before
    assert not dialog.save_button.isEnabled()
    with pytest.raises(ValueError, match="运行期间"):
        page._commit_black_moon_table(before)
    page.set_busy(False)
    assert dialog.save_button.isEnabled()
    page._black_moon_dialog = None


def test_preview_has_no_purchase_controls_or_parameters(repository):
    widget = TradePage(repository, preview_mode=True)
    try:
        widget.start_city.setCurrentIndex(widget.start_city.findData("1"))
        assert widget.black_moon_panel.isHidden()
        assert not set(EXECUTION_KEYS) & widget.collect_task_inputs(preview=True).keys()
        emitted = []
        widget.previewRequested.connect(lambda inputs, timeout: emitted.append(inputs))
        widget._request_preview()
        assert len(emitted) == 1
        assert not set(EXECUTION_KEYS) & emitted[0].keys()
        assert not set(EXECUTION_KEYS) & repository.load_trade_preview_inputs().keys()
    finally:
        dispose(widget)


def test_legacy_list_never_selects_products(repository, catalog):
    values = normalize_trade_task_inputs({"black_moon_local_purchase_items": list(catalog.item_ids)})
    assert "black_moon_local_purchase_items" not in values
    repository.save_trade_inputs({"black_moon_local_purchase_items": list(catalog.item_ids)})
    assert not any(repository.load_trade_inputs()["black_moon_purchase_table"].values())


def test_missing_catalog_disables_ui_without_resetting_settings(tmp_path, repository, monkeypatch):
    repository.save_trade_inputs({"cargo_capacity": 837, "black_moon_purchase_table": {"blackstone_armor": True}})
    previous_table = repository.load_trade_inputs()["black_moon_purchase_table"]
    monkeypatch.setenv("AURA_BASE_PATH", str(tmp_path / "missing"))
    with pytest.raises(BlackMoonCatalogError):
        load_black_moon_catalog()
    widget = TradePage(repository)
    try:
        assert not widget.auto_black_moon_local_purchase.isEnabled()
        assert not widget.black_moon_settings_button.isEnabled()
        assert widget.black_moon_selection_label.text() == "商品目录不可用"
        assert widget.cargo_capacity.value() == 837
        assert repository.load_trade_inputs()["black_moon_purchase_table"] == previous_table
        with pytest.raises(BlackMoonCatalogError):
            normalize_black_moon_settings({"auto_black_moon_local_purchase": True}, require_catalog=True)
    finally:
        dispose(widget)


def test_missing_icons_dont_drop_products(tmp_path, catalog):
    path = tmp_path / "plans/resonance_pc/data/meta/black_moon_local_shop_catalog.json"
    path.parent.mkdir(parents=True)
    path.write_text(json.dumps({"products": [dict(item_id=p.item_id, name=p.name, category=p.category, quality=p.quality)
                                              for p in catalog.products]}, ensure_ascii=False), encoding="utf-8")
    loaded = load_black_moon_catalog(tmp_path)
    assert loaded.item_ids == catalog.item_ids
    assert all(product.icon_path is None for product in loaded.products)


@pytest.mark.parametrize("products", [[], "invalid", [{"item_id": "x"}], [
    dict(item_id="x", name="商品", category="equipment", quality="Blue"),
    dict(item_id="x", name="重复商品", category="equipment", quality="Blue"),
]])
def test_malformed_catalog_is_explicitly_unavailable(tmp_path, products):
    path = tmp_path / "plans/resonance_pc/data/meta/black_moon_local_shop_catalog.json"
    path.parent.mkdir(parents=True)
    path.write_text(json.dumps({"products": products}, ensure_ascii=False), encoding="utf-8")
    with pytest.raises(BlackMoonCatalogError):
        load_black_moon_catalog(tmp_path)


@pytest.mark.parametrize("width,height,font_size", [(1000, 680, 10), (760, 550, 10), (680, 460, 12)])
def test_responsive_grid_no_horizontal_scroll_and_render(dialog, app, tmp_path, width, height, font_size):
    dialog.setStyleSheet(APP_STYLE)
    dialog.setFont(QFont("Microsoft YaHei", font_size))
    dialog.resize(width, height)
    dialog.show()
    app.processEvents()
    dialog.grid.update_grid_size()
    app.processEvents()
    assert dialog.grid.horizontalScrollBar().maximum() == 0
    assert dialog.save_button.isVisible()
    assert dialog.grid.gridSize().width() <= dialog.grid.viewport().width()
    assert dialog.proxy.rowCount() == 140
    if dialog.grid.viewport().width() >= 436:
        assert dialog.grid.visualRect(dialog.proxy.index(1, 0)).top() == dialog.grid.visualRect(dialog.proxy.index(0, 0)).top()
    dialog._toggle_item(dialog.proxy.index(0, 0))
    assert dialog.grab().save(str(tmp_path / f"black-moon-{width}.png"))


def test_category_filtering_does_not_oscillate_grid_layout(dialog, app):
    dialog.setStyleSheet(APP_STYLE)
    dialog.setFont(QFont("Microsoft YaHei", 10))
    dialog.resize(1000, 680)
    dialog.show()
    for category_row in (2, 4, 0, 2):
        dialog.categories.setCurrentRow(category_row)
        for _ in range(4):
            app.processEvents()
        samples = []
        for _ in range(12):
            app.processEvents()
            samples.append((dialog.grid.viewport().width(),
                            dialog.grid.visualRect(dialog.proxy.index(2, 0)).top(),
                            dialog.grid.verticalScrollBar().isVisible()))
        assert len(set(samples)) == 1
        assert samples[0][2] is True
        assert dialog.grid.horizontalScrollBar().maximum() == 0
