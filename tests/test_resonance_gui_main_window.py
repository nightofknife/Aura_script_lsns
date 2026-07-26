from __future__ import annotations

import os

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from PySide6.QtCore import QSettings
from PySide6.QtWidgets import QApplication

from packages.resonance_gui.bridge import RunnerBridge
from packages.resonance_gui.config_repository import ResonanceConfigRepository
from packages.resonance_gui.main_window import ResonanceMainWindow


class _IdleRunner:
    def close(self) -> None:
        return None


def _app() -> QApplication:
    return QApplication.instance() or QApplication([])


def _window(tmp_path) -> ResonanceMainWindow:
    _app()
    settings = QSettings(str(tmp_path / "main-window.ini"), QSettings.Format.IniFormat)
    window = ResonanceMainWindow(
        bridge=RunnerBridge(runner_factory=_IdleRunner),
        settings=ResonanceConfigRepository(settings=settings),
        initialize_on_startup=False,
    )
    window.resize(1280, 820)
    window.show()
    QApplication.processEvents()
    return window


def test_main_window_exposes_independent_battle_navigation(tmp_path):
    window = _window(tmp_path)
    try:
        assert [button.text() for button in window.nav_buttons] == [
            "跑商",
            "战斗",
            "任务工具",
            "历史",
            "设置",
        ]
        window.nav_buttons[1].click()
        assert window.page_stack.currentWidget() is window.battle_page
        assert window.page_stack.currentWidget() is not window.trade_page
    finally:
        window.close()


def test_main_window_routes_pc_battle_lifecycle_away_from_trade_page(tmp_path):
    window = _window(tmp_path)
    try:
        item = {
            "game_name": "resonance_pc",
            "kind": "battle_run",
            "label": "PC 自动战斗",
        }
        window._on_task_started(item)
        window._on_task_dispatched(
            {
                "item": item,
                "cid": "battle-cid",
                "dispatch": {"cid": "battle-cid", "status": "queued"},
            }
        )

        assert window.page_stack.currentWidget() is window.battle_page
        assert window.battle_page.is_busy()
        assert not window.trade_page.is_busy()
        assert window.battle_page.cid_value.text() == "battle-cid"

        window._on_run_updated({"cid": "battle-cid", "status": "running"})
        assert window.battle_page.run_status_value.text() == "运行中"

        window._on_task_finished(
            {
                "cid": "battle-cid",
                "status": "success",
                "gui_item": item,
                "final_result": {"user_data": {"success": True}},
            }
        )
        assert window.battle_page.run_status_value.text() == "已完成"
        assert not window.battle_page.is_busy()
    finally:
        window.close()


def test_history_filter_routes_battle_rows_to_battle_page(tmp_path):
    window = _window(tmp_path)
    try:
        rows = [
            {
                "cid": "trade-cid",
                "status": "success",
                "task_ref": "tasks:auto_cycle_trade_pc.yaml:auto_cycle_trade_pc",
            },
            {
                "cid": "battle-cid",
                "status": "success",
                "task_ref": "tasks:auto_battle_dispatch_pc.yaml:auto_battle_dispatch_pc",
            },
        ]
        window._on_history_loaded(rows)
        window.history_filter.setCurrentIndex(window.history_filter.findData("battle"))

        assert window.history_table.rowCount() == 1
        assert window.history_table.item(0, 0).text() == "battle-cid"
        window._open_history_row(0, 0)
        assert window.page_stack.currentWidget() is window.battle_page
        assert window.battle_page.cid_value.text() == "battle-cid"
    finally:
        window.close()
