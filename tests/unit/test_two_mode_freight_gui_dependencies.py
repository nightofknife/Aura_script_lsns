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
    assert page.book_waste_limit.isEnabled() is rules
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
    assert page.sparkling_water_start_threshold.isEnabled() is water
    assert page.sparkling_water_stop_threshold.isEnabled() is water
    assert page.bento_start_threshold.isEnabled() is bento
    assert page.bento_stop_threshold.isEnabled() is bento
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
    page.sparkling_water_start_threshold.setValue(650)
    page.sparkling_water_stop_threshold.setValue(231)
    page.bento_start_threshold.setValue(700)
    page.bento_stop_threshold.setValue(200)
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
        assert restored.sparkling_water_start_threshold.value() == 650
        assert restored.sparkling_water_stop_threshold.value() == 231
        assert restored.bento_start_threshold.value() == 700
        assert restored.bento_stop_threshold.value() == 200
        assert restored.fatigue_medicine_max_uses.value() == 7
        assert restored.medicine_checks[medicine].isChecked()
        assert restored.target_profit.value() == 123
        runtime = restored.collect_task_inputs()
        assert not {"book_profit_threshold", "book_policy", "negotiation_max_attempts",
                    "sparkling_water_start_threshold", "sparkling_water_stop_threshold",
                    "bento_start_threshold", "bento_stop_threshold", "bento_priority", "allowed_fatigue_medicines",
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


@pytest.mark.parametrize("preview", [False, True])
def test_waste_limit_saved_in_each_form_and_dispatched_only_for_fill(page, preview):
    widget = TradePage(page._settings, preview_mode=True) if preview else page
    try:
        choose(widget.start_city, "1")
        choose(widget.book_usage, "finite")
        widget.book_budget.setValue(3)
        widget.book_profit_threshold.setValue(999)
        widget.book_waste_limit.setValue(200)
        choose(widget.book_policy, "fill")
        widget.save_ui_state()
        saved = (widget._settings.load_trade_preview_inputs() if preview
                 else widget._settings.load_trade_inputs())
        assert saved["book_waste_limit"] == 200
        assert saved["book_profit_threshold"] == 9990000
        restored = TradePage(widget._settings, preview_mode=preview)
        try:
            assert restored.book_waste_limit.value() == 200
            inputs = restored.collect_task_inputs()
            assert inputs["book_budget"] == 3
            assert inputs["book_waste_limit"] == 200
            assert "book_profit_threshold" not in inputs
            assert "不使用单本收益阈值" in restored.book_strategy_note.text()
            assert "0 表示不允许浪费货物" in restored.book_strategy_note.text()
            choose(restored.book_policy, "profit")
            assert restored.book_profit_threshold.isEnabled()
            assert not restored.book_waste_limit.isEnabled()
            assert "浪费上限不生效" in restored.book_strategy_note.text()
            inputs = restored.collect_task_inputs()
            assert inputs["book_profit_threshold"] == 9990000
            assert "book_waste_limit" not in inputs
            choose(restored.book_usage, "none")
            assert "当前不用书" in restored.book_strategy_note.text()
            assert restored.book_waste_limit.value() == 200
        finally:
            restored.close()
            restored.deleteLater()
            QCoreApplication.sendPostedEvents(None, QEvent.Type.DeferredDelete)
    finally:
        if preview:
            widget.close()
            widget.deleteLater()
            QCoreApplication.sendPostedEvents(None, QEvent.Type.DeferredDelete)



def test_only_active_book_strategy_inputs_invalidate_preview(page):
    choose(page.book_usage, "unlimited")
    choose(page.book_policy, "fill")
    prime_preview(page)
    page.book_profit_threshold.setValue(999)
    assert not page._plan_stale
    page.book_waste_limit.setValue(200)
    assert page._plan_stale
    choose(page.book_policy, "profit")
    prime_preview(page)
    page.book_waste_limit.setValue(201)
    assert not page._plan_stale
    page.book_profit_threshold.setValue(998)
    assert page._plan_stale



@pytest.mark.parametrize("value", [-1, True, 1.5])
def test_repository_rejects_invalid_waste_limit_without_overwriting_saved_state(page, value):
    page.book_waste_limit.setValue(200)
    page.save_ui_state()
    saved = page._settings.load_trade_inputs()
    with pytest.raises(ValueError, match="book_waste_limit"):
        page._settings.save_trade_inputs({**saved, "book_waste_limit": value})
    assert page._settings.load_trade_inputs()["book_waste_limit"] == 200


def test_recovery_draft_can_be_incomplete_and_invalid_relation_but_execution_requires_values(page):
    assert page.collect_ui_state()["sparkling_water_start_threshold"] == 600
    page.sparkling_water_start_threshold.setValue(-1)
    page.auto_sparkling_water.setChecked(True)
    page.save_ui_state()
    assert page._settings.load_trade_inputs()["sparkling_water_start_threshold"] is None
    assert "气泡水" in page._form_error()
    with pytest.raises(ValueError, match="气泡水"):
        page.collect_task_inputs()
    page.sparkling_water_start_threshold.setValue(200)
    page.sparkling_water_stop_threshold.setValue(300)
    page.save_ui_state()
    assert page._settings.load_trade_inputs()["sparkling_water_stop_threshold"] == 300
    with pytest.raises(ValueError, match="气泡水"):
        page.collect_task_inputs()
    page.sparkling_water_start_threshold.setValue(600)
    assert page.collect_task_inputs()["sparkling_water_stop_threshold"] == 300
    page.auto_sparkling_water.setChecked(False)
    page.auto_bento.setChecked(True)
    page.bento_start_threshold.setValue(-1)
    with pytest.raises(ValueError, match="便当"):
        page.collect_task_inputs()
    page.bento_start_threshold.setValue(700)
    page.bento_stop_threshold.setValue(200)
    runtime = page.collect_task_inputs()
    assert runtime["bento_start_threshold"] == 700
    assert "sparkling_water_start_threshold" not in runtime


def test_recovery_thresholds_do_not_invalidate_plans_or_enter_previews(page):
    page._current_plan = {"route": [{"from_city": "A", "to_city": "B"}]}
    page._plan_fingerprint = page._planning_key()
    page.auto_sparkling_water.setChecked(True)
    page.sparkling_water_start_threshold.setValue(700)
    page.sparkling_water_stop_threshold.setValue(200)
    assert not page._plan_stale
    assert page._planning_key() == page._plan_fingerprint
    page.start_city.setCurrentIndex(page.start_city.findData("11"))
    page.sparkling_water_start_threshold.setValue(-1)
    assert "sparkling_water_start_threshold" not in page.collect_task_inputs(preview=True)
    page.set_busy(True)
    assert not page.sparkling_water_start_threshold.isEnabled()
    page.set_busy(False)
    assert page.sparkling_water_start_threshold.isEnabled()


def test_legacy_reserve_uses_explicit_new_defaults_and_never_becomes_a_threshold(page):
    from packages.resonance_gui.config_repository import TRADE_INPUTS_KEY
    from packages.resonance_shared.freight_parameters import FREIGHT_RECOVERY_THRESHOLD_KEYS
    page._settings.settings.setValue(TRADE_INPUTS_KEY, json.dumps({
        "base_fatigue_reserve": 200, "auto_sparkling_water": True, "auto_bento": True}))
    loaded = page._settings.load_trade_inputs()
    assert "base_fatigue_reserve" not in loaded
    assert [loaded[key] for key in FREIGHT_RECOVERY_THRESHOLD_KEYS] == [600, 100, 600, 100]
    page.set_inputs(loaded)
    assert [getattr(page, key).value() for key in FREIGHT_RECOVERY_THRESHOLD_KEYS] == [600, 100, 600, 100]
    assert page.collect_task_inputs()["sparkling_water_stop_threshold"] == 100


@pytest.mark.parametrize("value", [True, 200.5, "200", -1])
def test_threshold_drafts_reject_noninteger_values_without_overwriting_saved_state(page, value):
    before = page._settings.load_trade_inputs()
    with pytest.raises(ValueError, match="sparkling_water_stop_threshold"):
        page._settings.save_trade_inputs({**before, "sparkling_water_stop_threshold": value})
    assert page._settings.load_trade_inputs() == before
    with pytest.raises(ValueError, match="sparkling_water_stop_threshold"):
        page.set_inputs({**before, "sparkling_water_stop_threshold": value})


def test_recovery_threshold_edits_save_as_draft_without_dispatch(page):
    page.auto_sparkling_water.setChecked(True)
    page.sparkling_water_start_threshold.setValue(300)
    page.sparkling_water_stop_threshold.setValue(400)
    saved = page._settings.load_trade_inputs()
    assert saved["sparkling_water_start_threshold"] == 300
    assert saved["sparkling_water_stop_threshold"] == 400
    assert "气泡水" in page._form_error()
