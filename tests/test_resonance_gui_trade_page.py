from __future__ import annotations

import os

import pytest

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from PySide6.QtCore import QSettings, Qt
from PySide6.QtWidgets import QApplication, QDialog

from packages.resonance_gui.config_repository import (
    ALL_PC_TRADE_CITY_IDS,
    DEFAULT_PC_TRADE_CITY_IDS,
    ResonanceConfigRepository,
)
from packages.resonance_gui.logic import TRADE_PROGRESS_EVENT, TRADE_PROGRESS_SCHEMA
from packages.resonance_gui.trade_catalog import load_trade_product_groups, trade_product_ids
from packages.resonance_gui.widgets import trade_page as trade_page_module
from packages.resonance_gui.widgets.trade_page import (
    CityPrestigeDialog,
    ProductUnlockDialog,
    TradePage,
)


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


def test_trade_page_collects_typed_full_plan_inputs(tmp_path):
    page = _page(tmp_path)
    try:
        assert page.arrival_timeout_minutes.value() == 30
        page.set_target_status({"ok": True, "target": {"hwnd": 1, "title": "Resonance", "visible": True}})
        assert page.start_button.isEnabled()
        assert not page.preview_button.isEnabled()
        page.start_city.setCurrentIndex(page.start_city.findData("3"))
        assert page.preview_button.isEnabled()
        page.fatigue_budget.setValue(300)
        page.cargo_capacity.setValue(650)
        page.book_budget.setValue(0)
        page.arrival_timeout_minutes.setValue(45)
        page.negotiation_max_attempts.setValue(6)
        page.bargain_rates.setText("5000, 6000")
        page.raise_rates.setText("5000")
        page.auto_cape_island_investment.setChecked(True)

        inputs = page.collect_inputs()

        assert "all_plan" not in inputs
        assert "negotiation_budget" not in inputs
        assert inputs["fatigue_budget"] == 300
        assert inputs["cargo_capacity"] == 650
        assert inputs["arrival_timeout_seconds"] == 2700
        assert inputs["negotiation_max_attempts"] == 6
        assert inputs["bargain_success_rates_bps"] == [5000, 6000]
        assert inputs["auto_cape_island_investment"] is True
        assert inputs["available_city_ids"] == DEFAULT_PC_TRADE_CITY_IDS
        assert "21" in page.city_checks
        assert not page.city_checks["21"].isChecked()
        assert inputs["start_city_id"] == "3"
        assert not hasattr(page, "budget_mode")
        assert not hasattr(page, "full_mode")
        assert not hasattr(page, "negotiation_budget")
        assert page.negotiation_max_attempts.isEnabled()
        assert page.arrival_timeout_minutes.isEnabled()
        assert set(page.city_checks) == set(ALL_PC_TRADE_CITY_IDS)
        assert {"14", "17", "19"}.issubset(page.city_checks)

        requests = []
        page.previewRequested.connect(lambda payload, timeout: requests.append((payload, timeout)))
        page._request_preview()
        assert requests[0][0]["start_city_id"] == "3"
        assert "refresh_market" not in requests[0][0]
    finally:
        page.close()


def test_trade_page_shows_effective_pc_capture_profile(tmp_path):
    page = _page(tmp_path)
    try:
        page.set_target_status(
            {
                "ok": True,
                "trade_backend": "pc",
                "target": {"hwnd": 1, "title": "PC target", "visible": True},
                "capture": {
                    "backend": "wgc",
                    "health": {
                        "health": {
                            "capture_profile_effective": "compatible",
                        }
                    },
                },
            }
        )

        assert "WGC 兼容模式" in page.ready_hint.text()
    finally:
        page.close()

def test_trade_page_collects_city_multiselect_and_persisted_prestige(tmp_path):
    page = _page(tmp_path)
    try:
        page._set_all_cities(False)
        page.city_checks["3"].setChecked(True)
        page.city_checks["1"].setChecked(True)
        page.start_city.setCurrentIndex(page.start_city.findData("3"))
        page._city_prestige_default = 18
        page._city_prestige_overrides = {"3": 16, "2": 12}
        page._update_city_prestige_button()

        inputs = page.collect_inputs()

        assert inputs["available_city_ids"] == ["1", "3"]
        assert inputs["start_city_id"] == "3"
        assert inputs["city_prestige"] == {
            "default": 18,
            "overrides": {"3": 16, "2": 12},
        }
        assert page.city_prestige_button.text() == "设置城市声望（2 个自定义）"
        assert not hasattr(page, "city_prestige")
    finally:
        page.close()


def test_city_prestige_dialog_edits_all_cities_and_restores_defaults():
    dialog = CityPrestigeDialog(default_level=18, overrides={"3": 16, "19": 12})
    try:
        assert dialog.prestige_value() == {
            "default": 18,
            "overrides": {"3": 16, "19": 12},
        }
        assert set(dialog.city_prestige) == set(ALL_PC_TRADE_CITY_IDS)

        dialog._restore_defaults()

        assert dialog.prestige_value() == {"default": 20, "overrides": {}}
    finally:
        dialog.close()


def test_trade_page_prestige_dialog_save_updates_settings(tmp_path, monkeypatch):
    page = _page(tmp_path)

    class AcceptedPrestigeDialog:
        def __init__(self, default_level, overrides, parent):
            assert default_level == 20
            assert overrides == {}
            assert parent is page

        @staticmethod
        def exec():
            return QDialog.DialogCode.Accepted

        @staticmethod
        def prestige_value():
            return {"default": 17, "overrides": {"14": 11, "19": 9}}

    monkeypatch.setattr(trade_page_module, "CityPrestigeDialog", AcceptedPrestigeDialog)
    try:
        page._edit_city_prestige()

        assert page.city_prestige_button.text() == "设置城市声望（2 个自定义）"
        assert page._settings.load_trade_inputs()["city_prestige"] == {
            "default": 17,
            "overrides": {"14": 11, "19": 9},
        }
    finally:
        page.close()


def test_trade_product_catalog_only_contains_city_grouped_unlockable_products():
    groups = load_trade_product_groups()

    assert [group.city_id for group in groups] == ["1", "3", "4", "8", "10", "11", "15", "19"]
    assert len(trade_product_ids(groups)) == 52
    assert any(product.product_id == "1" and product.name == "发动机" for product in groups[0].products)
    assert "3" not in trade_product_ids(groups)  # 家电是普通商品，不需要声望解锁。


def test_product_unlock_dialog_switches_individual_products():
    dialog = ProductUnlockDialog(
        groups=load_trade_product_groups(),
        unlocked_product_ids={"1", "2"},
    )
    try:
        item = dialog._items_by_product_id["2"][0]
        assert item.checkState(0) == Qt.CheckState.Checked

        item.setCheckState(0, Qt.CheckState.Unchecked)
        QApplication.processEvents()

        assert dialog.unlocked_product_ids() == {"1"}
        assert dialog.summary.text() == "已解锁 1 / 52 个声望商品"

        dialog._set_all_products(True)
        assert len(dialog.unlocked_product_ids()) == 52
    finally:
        dialog.close()


def test_trade_page_product_unlock_dialog_save_updates_settings(tmp_path, monkeypatch):
    page = _page(tmp_path)

    class AcceptedProductUnlockDialog:
        def __init__(self, groups, unlocked_product_ids, parent):
            assert groups == page._product_groups
            assert unlocked_product_ids is None
            assert parent is page

        @staticmethod
        def exec():
            return QDialog.DialogCode.Accepted

        @staticmethod
        def unlocked_product_ids():
            return {"1", "2"}

    monkeypatch.setattr(trade_page_module, "ProductUnlockDialog", AcceptedProductUnlockDialog)
    try:
        page._edit_product_unlocks()

        assert page.product_unlock_button.text() == "设置商品解锁（2/52）"
        assert page.collect_inputs()["product_unlocks"] == {
            "mode": "only",
            "product_ids": ["1", "2"],
        }
        assert page._settings.load_trade_inputs()["product_unlocks"] == {
            "mode": "only",
            "product_ids": ["1", "2"],
        }
        assert not hasattr(page, "unlock_mode")
        assert not hasattr(page, "product_ids")
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
