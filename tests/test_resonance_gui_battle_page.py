from __future__ import annotations

import os

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from PySide6.QtCore import QSettings
from PySide6.QtWidgets import QApplication

from packages.resonance_gui.battle_catalog import load_battle_routes
from packages.resonance_gui.config_repository import ResonanceConfigRepository
from packages.resonance_gui.widgets.battle_page import BattlePage


def _app() -> QApplication:
    return QApplication.instance() or QApplication([])


def _page(tmp_path) -> BattlePage:
    _app()
    settings = QSettings(str(tmp_path / "battle-page.ini"), QSettings.Format.IniFormat)
    page = BattlePage(ResonanceConfigRepository(settings=settings))
    page.resize(1112, 760)
    page.show()
    QApplication.processEvents()
    return page


def _select(page: BattlePage, category: str, subcategory: str, route_id: str) -> None:
    page.category_buttons[category].click()
    page.subcategory_combo.setCurrentIndex(page.subcategory_combo.findData(subcategory))
    page.route_combo.setCurrentIndex(page.route_combo.findData(route_id))
    QApplication.processEvents()


def test_battle_catalog_exposes_all_route_groups():
    routes = load_battle_routes()

    assert len(routes) == 46
    assert sum(route.subcategory == "tie_an" for route in routes) == 16
    assert sum(route.subcategory == "regional_ops_center" for route in routes) == 10
    assert sum(route.subcategory == "action_summary" for route in routes) == 16
    assert sum(route.subcategory == "structural_exploration" for route in routes) == 4
    assert next(
        route for route in routes if route.route_id == "gp.action_summary.global_supply.magic"
    ).title == "全境特供 · 魔力"


def test_battle_page_switches_dynamic_fields_by_route_type(tmp_path):
    page = _page(tmp_path)
    try:
        _select(page, "ct", "tie_an", "ct.tie_an.shoggolith_city.expel")
        assert page.stage_spin.isVisible()
        assert page.difficulty_combo.isVisible()
        assert not page.threat_spin.isVisible()
        assert page.formation_combo.isVisible()

        _select(page, "ct", "tie_an", "ct.tie_an.shoggolith_city.bounty")
        assert not page.stage_spin.isVisible()
        assert not page.difficulty_combo.isVisible()
        assert not page.threat_spin.isVisible()
        assert page.formation_combo.isVisible()

        _select(
            page,
            "ct",
            "regional_ops_center",
            "ct.regional_ops_center.wilderness_station",
        )
        assert not page.stage_spin.isVisible()
        assert page.difficulty_combo.isVisible()
        assert page.threat_spin.isVisible()
        assert page.threat_spin.maximum() >= 100

        _select(
            page,
            "gp",
            "structural_exploration",
            "gp.structural_exploration.disordered_roots",
        )
        assert not page.stage_spin.isVisible()
        assert not page.difficulty_combo.isVisible()
        assert not page.threat_spin.isVisible()
        assert not page.formation_combo.isVisible()
        assert not page.capture_spin.isVisible()
    finally:
        page.close()


def test_battle_page_builds_reorders_and_persists_mixed_jobs(tmp_path):
    page = _page(tmp_path)
    try:
        _select(page, "ct", "tie_an", "ct.tie_an.shoggolith_city.expel")
        page.stage_spin.setValue(2)
        page.difficulty_combo.setCurrentIndex(page.difficulty_combo.findData(4))
        page.formation_combo.setCurrentIndex(page.formation_combo.findData(1))
        page.capture_spin.setValue(2)
        page._add_job()

        _select(
            page,
            "ct",
            "regional_ops_center",
            "ct.regional_ops_center.wilderness_station",
        )
        page.threat_spin.setValue(101)
        page.difficulty_combo.setCurrentIndex(page.difficulty_combo.findData(3))
        page.formation_combo.setCurrentIndex(0)
        page.capture_spin.setValue(0)
        page._add_job()

        _select(
            page,
            "gp",
            "action_summary",
            "gp.action_summary.global_supply.magic",
        )
        page.difficulty_combo.setCurrentIndex(page.difficulty_combo.findData(6))
        page._add_job()

        assert page.job_table.rowCount() == 3
        assert page.collect_inputs()["jobs"][1]["threat_level"] == 101
        assert page.collect_inputs()["jobs"][2]["difficulty"] == 6

        page.job_table.selectRow(2)
        page._move_selected(-1)
        assert page.collect_inputs()["jobs"][1]["route_id"] == "gp.action_summary.global_supply.magic"

        saved = page._settings.load_battle_inputs()
        assert saved == page.collect_inputs()
        assert "全境特供 · 魔力" in page.job_table.item(1, 1).text()
    finally:
        page.close()


def test_battle_page_validation_and_start_have_separate_readiness_rules(tmp_path):
    page = _page(tmp_path)
    try:
        _select(
            page,
            "gp",
            "structural_exploration",
            "gp.structural_exploration.disordered_roots",
        )
        page._add_job()
        assert page.validate_button.isEnabled()
        assert not page.start_button.isEnabled()

        validations: list[dict] = []
        starts: list[dict] = []
        page.validateRequested.connect(lambda payload, _timeout: validations.append(payload))
        page.startRequested.connect(lambda payload, _timeout: starts.append(payload))
        page._request_validation()
        assert validations[0]["jobs"][0]["route_id"] == "gp.structural_exploration.disordered_roots"

        page.set_target_status(
            {"ok": True, "target": {"hwnd": 1, "title": "雷索纳斯", "visible": True}}
        )
        assert page.start_button.isEnabled()
        page._request_start()
        assert starts[0] == validations[0]
    finally:
        page.close()


def test_battle_page_fixed_regions_do_not_overlap_at_minimum_viewport(tmp_path):
    page = _page(tmp_path)
    try:
        page.resize(1040, 680)
        QApplication.processEvents()
        assert page.job_table.width() > 500
        assert page.job_table.geometry().bottom() < page.result_view.geometry().top()
        assert page.start_button.width() >= 70
        assert page.cancel_button.width() >= 70
    finally:
        page.close()
