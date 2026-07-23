from __future__ import annotations

import os

import pytest

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from PySide6.QtCore import QSettings
from PySide6.QtWidgets import QApplication

from packages.resonance_gui.config_repository import ResonanceConfigRepository
from packages.resonance_gui.logic import TRADE_PROGRESS_EVENT, TRADE_PROGRESS_SCHEMA
from packages.resonance_gui.widgets.trade_page import TradePage


def _app() -> QApplication:
    return QApplication.instance() or QApplication([])


def _page(tmp_path) -> TradePage:
    _app()
    settings = QSettings(str(tmp_path / "trade-page.ini"), QSettings.Format.IniFormat)
    page = TradePage(ResonanceConfigRepository(settings=settings))
    page.resize(1112, 760)
    page.show()
    QApplication.processEvents()
    return page


def _progress(cid: str, sequence: int, **payload):
    return {
        "name": TRADE_PROGRESS_EVENT,
        "payload": {
            "schema": TRADE_PROGRESS_SCHEMA,
            "cid": cid,
            "sequence": sequence,
            **payload,
        },
    }


def test_trade_page_collects_typed_inputs_and_mode_rules(tmp_path):
    page = _page(tmp_path)
    try:
        page.set_target_status({"ok": True, "target": {"hwnd": 1, "title": "Resonance", "visible": True}})
        assert page.start_button.isEnabled()
        assert not page.preview_button.isEnabled()
        page.start_city.setCurrentIndex(page.start_city.findData("3"))
        assert page.preview_button.isEnabled()
        page.full_mode.click()
        page.fatigue_budget.setValue(300)
        page.cargo_capacity.setValue(650)
        page.book_budget.setValue(0)
        page.bargain_rates.setText("5000, 6000")
        page.raise_rates.setText("5000")

        inputs = page.collect_inputs()

        assert inputs["runtime_backend"] == "pc"
        assert inputs["all_plan"] == 1
        assert inputs["fatigue_budget"] == 300
        assert inputs["cargo_capacity"] == 650
        assert inputs["bargain_success_rates_bps"] == [5000, 6000]
        assert inputs["available_city_ids"] == ["3", "4", "1", "5", "7", "8", "9", "2"]
        assert inputs["start_city_id"] == "3"
        assert not page.negotiation_budget.isEnabled()

        requests = []
        page.previewRequested.connect(lambda payload, timeout: requests.append((payload, timeout)))
        page._request_preview()
        assert requests[0][0]["start_city_id"] == "3"
        assert "refresh_market" not in requests[0][0]
    finally:
        page.close()


def test_trade_page_switches_runtime_backend_and_accepts_emulator_target(tmp_path):
    page = _page(tmp_path)
    try:
        changes = []
        page.backendChanged.connect(changes.append)
        page.runtime_backend.setCurrentIndex(page.runtime_backend.findData("emulator"))
        page.set_target_status(
            {
                "ok": True,
                "trade_backend": "emulator",
                "target": {"serial": "127.0.0.1:16384", "backend": "scrcpy_stream"},
            }
        )

        assert changes[-1] == "emulator"
        assert page.selected_runtime_backend() == "emulator"
        assert page.collect_inputs()["runtime_backend"] == "emulator"
        assert page.start_button.isEnabled()
        assert "MuMu" in page.ready_hint.text()

        page.set_target_status(
            {
                "ok": True,
                "trade_backend": "pc",
                "target": {"hwnd": 1, "title": "PC target", "visible": True},
            }
        )
        assert page.target_value.text() == "127.0.0.1:16384"
    finally:
        page.close()


def test_trade_page_collects_city_multiselect_and_selected_prestige_only(tmp_path):
    page = _page(tmp_path)
    try:
        page._set_all_cities(False)
        page.city_checks["3"].setChecked(True)
        page.city_checks["1"].setChecked(True)
        page.start_city.setCurrentIndex(page.start_city.findData("3"))
        page.city_prestige["3"].setValue(16)
        page.city_prestige["2"].setValue(12)

        inputs = page.collect_inputs()

        assert inputs["available_city_ids"] == ["3", "1"]
        assert inputs["start_city_id"] == "3"
        assert inputs["city_prestige"]["overrides"] == {"3": 16}
        assert not page.city_prestige["3"].isHidden()
        assert page.city_prestige["2"].isHidden()
    finally:
        page.close()


def test_trade_page_requires_at_least_two_selected_cities(tmp_path):
    page = _page(tmp_path)
    try:
        page._set_all_cities(False)
        page.city_checks["3"].setChecked(True)

        with pytest.raises(ValueError, match="至少需要选择两个"):
            page.collect_inputs()
    finally:
        page.close()


def test_trade_page_clears_start_city_when_it_is_removed_from_planning(tmp_path):
    page = _page(tmp_path)
    try:
        page.start_city.setCurrentIndex(page.start_city.findData("3"))
        assert page.start_city.currentData() == "3"

        page.city_checks["3"].setChecked(False)

        assert page.start_city.currentData() == ""
        assert not page.preview_button.isEnabled()
    finally:
        page.close()


def test_trade_page_renders_route_progress_and_result(tmp_path):
    page = _page(tmp_path)
    try:
        page.set_target_status({"ok": True, "target": {"hwnd": 1, "title": "Resonance", "visible": True}})
        page.begin_run({"cid": "cid-preview"})
        route = [
            {
                "from_city": "A",
                "to_city": "B",
                "buys": [{"product_name": "Ore", "quantity": 7}],
                "books_used": 1,
                "expected_fatigue_cost": 42,
                "expected_profit": 1200,
                "bargain_to_cap": True,
                "raise_to_cap": False,
            },
            {"from_city": "B", "to_city": "C", "buy_products": [], "books_used": 0, "raise_to_cap": True},
        ]
        page.apply_progress(
            _progress(
                "cid-preview",
                1,
                stage="planning",
                state="completed",
                current_city="A",
                snapshot_id="snap-1",
                leg_count=2,
                data={"route": route, "summary": {"expected_profit": 1200, "books_used": 1}},
            )
        )
        page.apply_progress(
            _progress(
                "cid-preview",
                2,
                stage="negotiation",
                state="started",
                operation="bargain",
                leg_index=0,
                leg_count=2,
                from_city="A",
                to_city="B",
            )
        )

        assert page.route_tree.topLevelItemCount() == 2
        assert "Ore x7" in page.route_tree.topLevelItem(0).text(1)
        assert page.route_tree.topLevelItem(0).text(2) == "疲劳 42 / 书 1"
        assert page.route_tree.topLevelItem(0).text(4) == "1,200"
        assert page.stage_title.text() == "砍价"
        assert page.route_tree.topLevelItem(0).text(5) == ""
        assert page.route_tree.topLevelItem(0).toolTip(5) == "进行中"
        assert not page.route_tree.topLevelItem(0).icon(5).isNull()
        assert page.route_tree.topLevelItem(0).background(0).color().name() == "#dff3f2"

        page.finish_run(
            {
                "cid": "cid-preview",
                "status": "success",
                "final_result": {
                    "user_data": {
                        "status": "completed",
                        "route": route,
                        "expected_profit": 1200,
                        "expected_fatigue_used": 88,
                        "remaining_expected_fatigue": 12,
                        "books_used": 1,
                        "full_bargain_count": 1,
                        "full_raise_count": 1,
                        "page_state": "city_main",
                    }
                },
            }
        )
        assert not page.is_busy()
        assert page.result_values["expected_profit"].text() == "1,200"
        assert page.result_values["profit_per_fatigue"].text() == "13.64 / 疲劳"
        assert page.result_values["route"].text() == "2 段 / 3 城"
    finally:
        page.close()


def test_trade_page_preview_renders_current_plan_and_pending_icons(tmp_path):
    page = _page(tmp_path)
    try:
        page.set_target_status({"ok": True, "target": {"hwnd": 1, "title": "Resonance", "visible": True}})
        page.begin_preview({"cid": "cid-plan"})
        route = [{"from_city": "A", "to_city": "B", "expected_profit": 900, "expected_fatigue_cost": 30}]
        page.finish_preview(
            {
                "cid": "cid-plan",
                "status": "success",
                "final_result": {
                    "user_data": {
                        "preview": True,
                        "market_refreshed": True,
                        "market_source": "refresh",
                        "status": "ok",
                        "route": route,
                        "expected_profit": 900,
                        "expected_fatigue_used": 30,
                        "remaining_expected_fatigue": 70,
                        "books_used": 0,
                        "full_bargain_count": 0,
                        "full_raise_count": 0,
                        "initial_city": {"city_name": "A"},
                    }
                },
            }
        )

        assert page.stage_title.text() == "方案已计算"
        assert page.stage_detail.text() == "行情已更新，本方案使用最新快照"
        assert page.route_tree.topLevelItemCount() == 1
        assert page.route_tree.topLevelItem(0).text(5) == ""
        assert page.route_tree.topLevelItem(0).toolTip(5) == "待执行"
        assert page.result_values["profit_per_fatigue"].text() == "30.00 / 疲劳"
        assert page.result_values["remaining_fatigue"].text() == "70"
        assert not page.is_busy()
    finally:
        page.close()


def test_trade_page_fixed_regions_do_not_overlap_at_minimum_viewport(tmp_path):
    page = _page(tmp_path)
    try:
        page.resize(1040, 680)
        QApplication.processEvents()
        route_rect = page.route_tree.geometry()
        result_rect = page.result_band.geometry()
        assert route_rect.bottom() < result_rect.top()
        assert page.start_button.width() >= 70
        assert page.cancel_button.width() >= 70
    finally:
        page.close()
