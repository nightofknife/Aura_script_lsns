"""User-facing two-mode form dependencies, persistence and stale-plan feedback."""
import os
import json

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

import pytest
from PySide6.QtCore import QSettings, QCoreApplication, QEvent
from PySide6.QtWidgets import QApplication, QDialog

from packages.resonance_gui.config_repository import ResonanceConfigRepository
from packages.resonance_gui.widgets import trade_page
from packages.resonance_gui.widgets.trade_page import TradePage


@pytest.fixture
def page(tmp_path):
    app = QApplication.instance() or QApplication([])
    settings = QSettings(str(tmp_path / "two-mode.ini"), QSettings.Format.IniFormat)
    widget = TradePage(ResonanceConfigRepository(settings))
    yield widget
    widget.close()
    widget.deleteLater()
    QCoreApplication.sendPostedEvents(None, QEvent.Type.DeferredDelete)
    app.processEvents()


def choose(combo, value):
    index = combo.findData(value)
    assert index >= 0
    combo.setCurrentIndex(index)


def test_incomplete_drafts_roundtrip_without_switching_to_defaults(page):
    page._set_all_cities(False)
    page.automatic_end.setChecked(False)
    page.target_profit_enabled.setChecked(True)
    page.target_profit.setValue(0)
    page.mode_buttons["fixed"].setChecked(True)
    page.add_route_city("1")
    page.save_ui_state()
    saved = page._settings.load_trade_inputs()
    assert saved["available_city_ids"] == []
    assert saved["required_end_city_ids"] == []
    assert saved["fixed_route_city_ids"] == ["1"]
    assert saved["target_profit_enabled"]
    page.set_inputs(saved)
    assert page.selected_city_ids() == []
    assert not page.automatic_end.isChecked()
    with pytest.raises(ValueError, match="至少"):
        page.collect_task_inputs()
    page.add_route_city("2")
    assert page.collect_task_inputs()["fixed_route_city_ids"] == ["1", "2"]
    page.mode_buttons["free"].setChecked(True)
    assert not page.start_button.isEnabled()


@pytest.mark.parametrize("old_mode", ["profit", "quick", "target"])
def test_old_modes_are_not_loaded_migrated_or_interpreted_as_new_settings(page, old_mode):
    saved = {"trade_mode": old_mode, "target_profit": 1234567, "fatigue_budget": 811,
             "cargo_capacity": 901, "fixed_route_city_ids": ["1", "2"]}
    raw = json.dumps(saved)
    for key in ("trade/inputs_json", "trade_preview/inputs_json"):
        page._settings.settings.setValue(key, raw)
    current = page._settings.load_trade_inputs()
    assert current["trade_mode"] == "free"
    assert current["fatigue_budget"] == 700 and current["cargo_capacity"] == 750
    assert not current["target_profit_enabled"]
    assert page._settings.load_trade_preview_inputs()["fatigue_budget"] == 700
    for key in ("trade/inputs_json", "trade_preview/inputs_json"):
        assert page._settings.settings.value(key) == raw
    with pytest.raises(ValueError, match="free.*fixed"):
        page.set_inputs(saved)
    with pytest.raises(ValueError, match="free.*fixed"):
        page._settings.save_trade_inputs(saved)
    page.fatigue_budget.setValue(809)
    page.save_ui_state()
    assert page._settings.load_trade_inputs()["fatigue_budget"] == 809
    assert page._settings.settings.value("trade/inputs_json") == raw


def test_unused_book_count_is_visible_disabled_and_retained(page):
    page.book_budget.setValue(23)
    choose(page.book_usage, "none")
    assert not page.book_budget.isHidden()
    assert not page.book_budget.isEnabled()
    assert page.book_budget.value() == 23


def test_duplicate_route_is_rejected_even_by_direct_dispatch(page):
    page.mode_buttons["fixed"].setChecked(True)
    page.add_route_city("1")
    page.add_route_city("1")
    with pytest.raises(ValueError, match="连续"):
        page.collect_task_inputs()


def test_preview_result_marks_stale_and_recalculation_clears_both_hints(page):
    preview = TradePage(page._settings, preview_mode=True)
    try:
        choose(preview.start_city, "1")
        emitted = []
        preview.previewRequested.connect(lambda inputs, timeout: emitted.append(inputs))
        result = {"status": "planned", "route": [{"from_city": "修格里城", "to_city": "铁盟哨站"}]}
        preview._request_preview()
        preview.begin_preview({"cid": "one", "inputs": emitted[-1]})
        preview.finish_preview(result)
        assert not preview._plan_stale
        preview.cargo_capacity.setValue(800)
        assert preview._plan_stale
        assert not preview.preview_result_hint.isHidden()
        assert "需要重新试算" in preview.preview_result_hint.text()
        preview._request_preview()
        preview.begin_preview({"cid": "two", "inputs": emitted[-1]})
        assert preview.preview_result_hint.isHidden()
        preview.finish_preview(result)
        assert not preview._plan_stale
        assert preview.preview_state_hint.isHidden()
        assert preview.preview_result_hint.isHidden()
        preview.invalidate_preview("账号数据已更新")
        assert preview._plan_stale
        assert "账号数据" in preview.preview_result_hint.text()
    finally:
        preview.close()
        preview.deleteLater()


def test_preview_fingerprint_ignores_disabled_book_values_and_fixed_free_fields(page):
    page.mode_buttons["fixed"].setChecked(True)
    page.add_route_city("1")
    page.add_route_city("2")
    page._current_plan = {"route": [{"from_city": "A", "to_city": "B"}]}
    page._plan_fingerprint = page._planning_key()
    page.book_profit_threshold.setValue(99)
    page.target_profit.setValue(999)
    page.target_profit_enabled.setChecked(True)
    page.end_city_checks["11"].setChecked(True)
    assert not page._plan_stale
    page.fixed_route.addItem(page.fixed_route.takeItem(0))
    assert page._plan_stale


@pytest.mark.parametrize("usage,count,rules", [
    ("none", False, False), ("finite", True, True), ("unlimited", False, True),
])
def test_book_controls_follow_usage_without_changing_negotiation(page, usage, count, rules):
    choose(page.negotiation_policy, "disabled")
    page.book_budget.setValue(12)
    choose(page.book_policy, "fill")
    choose(page.book_usage, usage)
    assert page.book_budget.isEnabled() is count
    assert page.book_policy.isEnabled() is rules
    assert not page.book_profit_threshold.isEnabled()
    assert page.negotiation_policy.currentData() == "disabled"
    if usage == "unlimited":
        assert page.book_count_note.text() == "无限"
        assert page.collect_task_inputs()["book_budget"] is None
    choose(page.book_usage, "finite")
    assert page.book_budget.value() == 12
    assert page.book_policy.currentData() == "fill"


@pytest.mark.parametrize("usage", ["finite", "unlimited"])
def test_fill_disables_threshold_retains_saved_value_and_restores_profit(page, usage):
    choose(page.book_usage, usage)
    page.book_budget.setValue(7)
    page.book_profit_threshold.setValue(123.4567)
    choose(page.book_policy, "fill")
    assert not page.book_profit_threshold.isEnabled()
    assert "不使用单本收益阈值" in page.book_threshold_note.text()
    inputs = page.collect_task_inputs()
    assert "book_profit_threshold" not in inputs
    assert inputs["book_budget"] == (7 if usage == "finite" else None)
    page.save_ui_state()
    saved = page._settings.load_trade_inputs()
    assert saved["book_profit_threshold"] == 1234567
    page.set_inputs(saved)
    assert not page.book_profit_threshold.isEnabled()
    assert page.book_profit_threshold.value() == 123.4567
    choose(page.book_policy, "profit")
    assert page.book_profit_threshold.isEnabled()
    assert page.collect_task_inputs()["book_profit_threshold"] == 1234567


def test_fill_plan_fingerprint_ignores_threshold_but_not_total_book_limit(page):
    choose(page.book_usage, "finite")
    choose(page.book_policy, "fill")
    page.book_budget.setValue(3)
    page._current_plan = {"route": [{"from_city": "A", "to_city": "B"}]}
    page._plan_fingerprint = page._planning_key()
    page.book_profit_threshold.setValue(9999)
    assert not page._plan_stale
    page.book_budget.setValue(4)
    assert page._plan_stale


@pytest.mark.parametrize("water,bento", [(False, False), (False, True), (True, False), (True, True)])
def test_recovery_dependencies_and_bento_order_controls(page, water, bento):
    page.auto_sparkling_water.setChecked(water)
    page.auto_bento.setChecked(bento)
    assert page.base_fatigue_reserve.isEnabled() is (water or bento)
    assert page.bento_priority_panel.isEnabled() is bento
    if not bento:
        assert all(not button.isEnabled() for button in page.bento_type_checks.values())
        assert all(not button.isEnabled() for button in page.bento_move_buttons.values())


def test_negotiation_and_medicine_controls_unlock_and_relock_after_running(page):
    choose(page.negotiation_policy, "disabled")
    assert not page.negotiation_max_attempts.isEnabled()
    choose(page.negotiation_policy, "auto")
    assert page.negotiation_max_attempts.isEnabled()
    page.use_fatigue_medicine.setChecked(False)
    assert not page.fatigue_medicine_max_uses.isEnabled()
    assert all(not button.isEnabled() for button in page.medicine_checks.values())
    page.use_fatigue_medicine.setChecked(True)
    assert page.fatigue_medicine_max_uses.isEnabled()
    assert all(button.isEnabled() for button in page.medicine_checks.values())
    page.set_busy(True)
    assert not page.fatigue_medicine_max_uses.isEnabled()
    assert not page.negotiation_max_attempts.isEnabled()
    page.set_busy(False)
    assert page.fatigue_medicine_max_uses.isEnabled()
    assert page.negotiation_max_attempts.isEnabled()


def test_inactive_values_survive_repository_reload_but_not_runtime_projection(page):
    page.book_budget.setValue(18)
    page.book_profit_threshold.setValue(72.5)
    choose(page.book_policy, "fill")
    page.negotiation_max_attempts.setValue(3)
    page.base_fatigue_reserve.setValue(231)
    page.fatigue_medicine_max_uses.setValue(7)
    medicine = next(iter(page.medicine_checks))
    page.medicine_checks[medicine].setChecked(True)
    page.target_profit.setValue(123)
    choose(page.book_usage, "none")
    choose(page.negotiation_policy, "disabled")
    page.auto_bento.setChecked(False)
    page.auto_sparkling_water.setChecked(False)
    page.use_fatigue_medicine.setChecked(False)
    page.target_profit_enabled.setChecked(False)
    page.save_ui_state()
    restored = TradePage(page._settings)
    try:
        assert restored.book_budget.value() == 18
        assert restored.book_profit_threshold.value() == 72.5
        assert restored.book_policy.currentData() == "fill"
        assert restored.negotiation_max_attempts.value() == 3
        assert restored.base_fatigue_reserve.value() == 231
        assert restored.fatigue_medicine_max_uses.value() == 7
        assert restored.medicine_checks[medicine].isChecked()
        assert restored.target_profit.value() == 123
        runtime = restored.collect_task_inputs()
        assert not {"book_profit_threshold", "book_policy", "negotiation_max_attempts",
                    "base_fatigue_reserve", "bento_priority", "allowed_fatigue_medicines",
                    "fatigue_medicine_max_uses", "target_profit"} & runtime.keys()
        restored.use_fatigue_medicine.setChecked(True)
        assert medicine in restored.collect_task_inputs()["allowed_fatigue_medicines"]
    finally:
        restored.close()
        restored.deleteLater()


@pytest.mark.parametrize("preview", [False, True])
def test_invalid_endpoint_has_visible_error_and_can_be_cancelled(page, preview):
    original = page
    if preview:
        page = TradePage(original._settings, preview_mode=True)
    page.set_target_status({"ok": True, "target": {"title": "test", "hwnd": 123}})
    page.automatic_end.setChecked(False)
    page.end_city_checks["1"].setChecked(True)
    page.end_city_checks["2"].setChecked(True)
    page.start_city.setCurrentIndex(page.start_city.findData("2"))
    action = page.preview_button if preview else page.start_button
    assert action.isEnabled()
    page.city_checks["1"].setChecked(False)
    assert page.end_city_checks["1"].isChecked()
    assert page.end_city_checks["1"].isEnabled()
    assert not page.parameter_notice.isHidden()
    assert "终点" in page.parameter_notice.text()
    assert not action.isEnabled()
    page.end_city_checks["1"].click()
    assert page.collect_task_inputs()["required_end_city_ids"] == ["2"]
    assert page.parameter_notice.isHidden()
    assert action.isEnabled()
    if preview:
        page.close()
        page.deleteLater()


def test_target_toggle_survives_mode_switch_and_route_hint_tracks_shape(page):
    page.target_profit.setValue(40)
    page.target_profit_enabled.setChecked(True)
    assert page.target_profit.isEnabled()
    page.mode_buttons["fixed"].setChecked(True)
    page.add_route_city("1")
    page.add_route_city("2")
    assert "开放" in page.fixed_route_hint.text()
    assert "target_profit" not in page.collect_task_inputs()
    assert not page.target_profit.isVisibleTo(page.parameter_panel)
    page.add_route_city("1")
    assert "闭环" in page.fixed_route_hint.text()
    page.mode_buttons["free"].setChecked(True)
    assert page.target_profit_enabled.isChecked()
    assert page.collect_task_inputs()["target_profit"] == 400000
    page.target_profit_enabled.setChecked(False)
    assert not page.target_profit.isEnabled()
    assert page.target_profit.value() == 40


def prime_preview(page):
    page.start_city.setCurrentIndex(page.start_city.findData("1"))
    page._last_inputs = page.collect_task_inputs(preview=True)
    page.finish_preview({"status": "planned", "expected_profit": 123})
    assert page.preview_state_hint.isHidden()


@pytest.mark.parametrize("field", ["fatigue_budget", "cargo_capacity", "bargain_step", "raise_step"])
def test_planning_numeric_edits_mark_existing_preview_stale(page, field):
    prime_preview(page)
    widget = getattr(page, field)
    widget.setValue(widget.value() + 1)
    assert not page.preview_state_hint.isHidden()
    assert "重新试算" in page.run_status_value.text()


def test_execution_only_changes_do_not_invalidate_preview(page):
    prime_preview(page)
    page.auto_cape_island_investment.toggle()
    page.auto_rubbish_recycling.toggle()
    assert page.preview_state_hint.isHidden()
    page.fatigue_budget.setValue(page.fatigue_budget.value() + 1)
    assert not page.preview_state_hint.isHidden()


def test_saved_account_prestige_edit_invalidates_preview(page, monkeypatch):
    prime_preview(page)
    monkeypatch.setattr(trade_page.CityPrestigeDialog, "exec", lambda self: QDialog.DialogCode.Accepted)
    monkeypatch.setattr(trade_page.CityPrestigeDialog, "prestige_value",
                        lambda self: {"default": 19, "overrides": {}})
    page.city_prestige_button.click()
    assert not page.preview_state_hint.isHidden()
    assert page.collect_task_inputs()["city_prestige"]["default"] == 19
