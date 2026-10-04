"""Four-mode freight form contracts; no game client or runner actions."""
import os

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

import pytest
from PySide6.QtCore import QPoint, QSettings, Qt
from PySide6.QtWidgets import QApplication

from packages.resonance_gui.config_repository import ResonanceConfigRepository
from packages.resonance_gui.logic import normalize_trade_task_inputs, trade_result_summary
from packages.resonance_gui.widgets.trade_page import TradePage, ProductUnlockDialog
from packages.resonance_gui.widgets.toggle_button import ToggleButton
from packages.resonance_gui.trade_catalog import TradeProduct, TradeProductGroup


@pytest.fixture
def page(tmp_path):
    app = QApplication.instance() or QApplication([])
    repository = ResonanceConfigRepository(QSettings(str(tmp_path / "freight.ini"), QSettings.Format.IniFormat))
    widget = TradePage(repository)
    yield widget
    widget.close()
    app.processEvents()


def test_default_contract(page):
    inputs = page.collect_task_inputs()
    assert inputs["trade_mode"] == "profit"
    assert inputs["fatigue_budget"] == 700
    assert inputs["cargo_capacity"] == 750
    assert inputs["book_budget"] == 0
    assert inputs["book_profit_threshold"] == 500000
    assert inputs["arrival_timeout_seconds"] == 3600
    assert inputs["auto_cape_island_investment"] is True
    assert not {"start_city_id", "auto_book", "trade_level", "active_events"} & inputs.keys()


def test_all_freight_toggle_buttons_have_hover_explanations(page):
    buttons = page.findChildren(ToggleButton)
    assert buttons
    assert all(button.toolTip().strip() for button in buttons)


@pytest.mark.parametrize("preview", [False, True])
def test_key_parameters_precede_collapsed_city_grid_in_both_forms(page, preview):
    widget = TradePage(page._settings, preview_mode=True) if preview else page
    widget.resize(1440, 1000)
    widget.show()
    QApplication.processEvents()
    assert not widget.city_selector_toggle.isChecked()
    assert widget.city_buttons_panel.isHidden()
    assert "20" in widget.city_selector_toggle.text()
    city_top = widget.city_selector_toggle.mapTo(widget.parameter_panel, QPoint()).y()
    for control in (widget.fatigue_budget, widget.cargo_capacity, widget.book_usage,
                    widget.book_profit_threshold, widget.book_policy, widget.negotiation_policy):
        assert control.mapTo(widget.parameter_panel, QPoint()).y() < city_top
    before = widget.selected_city_ids()
    widget.city_selector_toggle.setChecked(True)
    assert not widget.city_buttons_panel.isHidden()
    widget.city_checks["1"].setChecked(False)
    assert "19" in widget.city_selector_toggle.text()
    widget.city_selector_toggle.setChecked(False)
    assert widget.selected_city_ids() == [city for city in before if city != "1"]
    if preview:
        widget.close()


@pytest.mark.parametrize("key,value", [("fatigue_budget", True), ("cargo_capacity", 1.5),
                                        ("book_budget", -1), ("book_budget", False),
                                        ("negotiation_max_attempts", 2.5)])
def test_restore_rejects_noninteger_resource_inputs(page, key, value):
    with pytest.raises(ValueError):
        page.restore_inputs({key: value})


def test_book_limit_unlimited_and_restore(page):
    page.book_budget.setValue(17)
    page.book_usage.setCurrentIndex(page.book_usage.findData("finite"))
    assert page.collect_task_inputs()["book_budget"] == 17
    page.book_usage.setCurrentIndex(page.book_usage.findData("unlimited"))
    assert page.collect_task_inputs()["book_budget"] is None
    assert page.book_budget.isHidden()
    state = page.collect_ui_state()
    page._settings.save_trade_inputs(state)
    loaded = page._settings.load_trade_inputs()
    assert loaded["book_budget"] is None
    assert loaded["finite_book_budget"] == 17
    page.restore_inputs(loaded)
    page.book_usage.setCurrentIndex(page.book_usage.findData("finite"))
    assert page.book_budget.value() == 17
    page.book_usage.setCurrentIndex(page.book_usage.findData("none"))
    assert page.collect_task_inputs()["book_budget"] == 0
    assert page.book_budget.value() == 17


def test_money_uses_four_decimals_and_decimal_conversion(page):
    page.book_profit_threshold.setValue(50.0001)
    assert page.collect_task_inputs()["book_profit_threshold"] == 500001
    page.mode_buttons["target"].setChecked(True)
    with pytest.raises(ValueError, match="正的目标"):
        page.collect_task_inputs()
    page.target_profit.setValue(123.4567)
    assert page.collect_task_inputs()["target_profit"] == 1234567
    page.mode_buttons["profit"].setChecked(True)
    assert "target_profit" not in page.collect_task_inputs()
    page.mode_buttons["target"].setChecked(True)
    assert page.collect_task_inputs()["target_profit"] == 1234567


def test_quick_overrides_policies_but_retains_other_mode_preferences(page):
    page.negotiation_policy.setCurrentIndex(page.negotiation_policy.findData("disabled"))
    page.mode_buttons["quick"].setChecked(True)
    inputs = page.collect_task_inputs()
    assert inputs["book_policy"] == "fill"
    assert inputs["negotiation_policy"] == "required"
    assert inputs["book_profit_threshold"] == 500000
    assert not page.book_policy.isEnabled()
    page.mode_buttons["profit"].setChecked(True)
    assert page.book_policy.currentData() == "profit"
    assert page.negotiation_policy.currentData() == "disabled"


def test_fixed_route_retains_repeats_and_excludes_other_mode_fields(page):
    page.mode_buttons["fixed"].setChecked(True)
    with pytest.raises(ValueError, match="至少"):
        page.collect_task_inputs()
    for city in ("1", "2", "1", "3"):
        page.add_route_city(city)
    item = page.fixed_route.takeItem(3)
    page.fixed_route.insertItem(1, item)
    inputs = page.collect_task_inputs()
    assert inputs["fixed_route_city_ids"] == ["1", "3", "2", "1"]
    assert inputs["reposition_to_route"] is False
    assert not {"available_city_ids", "required_end_city_ids", "target_profit", "fixed_route_repeat_count"} & inputs.keys()
    page.start_city.setCurrentIndex(page.start_city.findData("11"))
    assert page.collect_task_inputs(preview=True)["start_city_id"] == "11"
    assert "start_city_id" not in page.collect_task_inputs()


def test_multiple_endpoints_and_preview_run_share_parameters(page):
    page.automatic_end.setChecked(False)
    page.end_city_checks["1"].setChecked(True)
    page.end_city_checks["11"].setChecked(True)
    page.start_city.setCurrentIndex(page.start_city.findData("1"))
    run = page.collect_task_inputs()
    preview = page.collect_task_inputs(preview=True)
    assert run["required_end_city_ids"] == ["1", "11"]
    assert preview.pop("start_city_id") == "1"
    assert preview == run


def test_normalization_preserves_null_and_drops_only_ui_and_inapplicable_fields():
    inputs = {"trade_mode": "fixed", "book_budget": None, "auto_book": True,
              "available_city_ids": ["1", "2"], "required_end_city_ids": ["1"],
              "target_profit": 1, "fixed_route_city_ids": ["1", "2", "1"]}
    out = normalize_trade_task_inputs(inputs)
    assert out == {"trade_mode": "fixed", "book_budget": None,
                   "fixed_route_city_ids": ["1", "2", "1"]}
    assert inputs["auto_book"] is True


def test_result_distinguishes_plan_actual_reposition_and_error(page):
    page._render_result({"status": "planned", "trade_mode": "target", "planning_status": "target_unreachable",
                         "expected_profit": 500, "expected_fatigue_used": 20,
                         "reposition": {"expected_fatigue": 5}, "actual_profit": None,
                         "error": {"code": "target_unreachable", "message": "预算不足"}})
    assert page.result_values["actual_profit"].text() == "未知"
    assert page.result_values["reposition_fatigue"].text() == "5"
    assert page.result_values["planning_status"].text() == "目标不可达"
    assert "预算不足" in page.reason_label.text()


def test_new_result_fields_survive_summary_projection():
    result = {"status": "planned", "trade_mode": "fixed", "planning_status": "ok",
              "reposition": {"expected_fatigue": 5}, "book_budget": None,
              "actual_profit": None, "target_reached": False,
              "error": {"code": "new_backend_code", "message": "详细原因"}}
    summary = trade_result_summary(result)
    for field in ("trade_mode", "planning_status", "reposition", "book_budget", "actual_profit", "target_reached", "error"):
        assert summary[field] == result[field]


def test_legacy_auto_book_is_extracted_only_to_ui_state(page):
    page._settings.save_trade_inputs({"auto_book": True, "book_budget": 17})
    state = page._settings.load_trade_inputs()
    assert state["books_enabled"] is True
    assert state["books_unlimited"] is True
    assert state["finite_book_budget"] == 17
    assert state["book_budget"] is None
    assert "auto_book" not in state
    page.restore_inputs(state)
    assert page.collect_task_inputs()["book_budget"] is None


def test_inactive_mode_state_persists_and_restores(page):
    page.book_budget.setValue(13)
    page.add_route_city("1")
    page.add_route_city("2")
    page.target_profit.setValue(25.0001)
    page.save_ui_state()
    page.restore_inputs(page._settings.load_trade_inputs())
    page.mode_buttons["fixed"].setChecked(True)
    assert page.collect_task_inputs()["fixed_route_city_ids"] == ["1", "2"]
    page.mode_buttons["target"].setChecked(True)
    assert page.collect_task_inputs()["target_profit"] == 250001
    assert page.book_budget.value() == 13


def test_product_unlock_buttons_sync_shared_products_and_city_counts(page):
    groups = (TradeProductGroup("1", "甲城", (TradeProduct("x", "共享货物"), TradeProduct("y", "甲城货物"))),
              TradeProductGroup("2", "乙城", (TradeProduct("x", "共享货物"), TradeProduct("z", "乙城货物"))))
    dialog = ProductUnlockDialog(groups, set())
    assert dialog.unlocked_product_ids() == set()
    for items in dialog._items_by_product_id.values():
        for item in items:
            assert not item.flags() & Qt.ItemFlag.ItemIsUserCheckable
            assert isinstance(dialog.tree.itemWidget(item, 1), ToggleButton)
    dialog._buttons_by_product_id["x"][0].setChecked(True)
    assert dialog.unlocked_product_ids() == {"x"}
    assert all(button.isChecked() for button in dialog._buttons_by_product_id["x"])
    assert dialog._city_items[0].text(2) == "1/2"
    dialog._city_buttons["2"].setChecked(True)
    assert dialog.unlocked_product_ids() == {"x", "z"}
    dialog._set_all_products(True)
    assert dialog.unlocked_product_ids() == {"x", "y", "z"}
    dialog._set_all_products(False)
    assert dialog.unlocked_product_ids() == set()
    dialog.search.setText("甲城货物")
    assert dialog._city_items[1].isHidden()
    assert not dialog._city_items[0].isHidden()
    dialog.close()
