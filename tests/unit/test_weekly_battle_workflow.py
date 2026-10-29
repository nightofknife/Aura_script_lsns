"""A workflow queues the saved whole week, including empty and structural days."""
import os
from copy import deepcopy

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

import pytest
from PySide6.QtCore import QCoreApplication, QEvent, QSettings
from PySide6.QtWidgets import QApplication, QMessageBox

from packages.resonance_gui.battle_plan import normalize_battle_editor_inputs
from packages.resonance_gui.config_repository import ResonanceConfigRepository
from packages.resonance_gui.main_window import ResonanceMainWindow


@pytest.fixture
def window(tmp_path, monkeypatch):
    app = QApplication.instance() or QApplication([])
    monkeypatch.setattr(ResonanceMainWindow, "_wire_bridge", lambda self: None)
    monkeypatch.setattr(QMessageBox, "warning", lambda *args: pytest.fail(str(args[2])))
    repository = ResonanceConfigRepository(QSettings(str(tmp_path / "workflow.ini"), QSettings.Format.IniFormat))
    widget = ResonanceMainWindow(settings=repository, initialize_on_startup=False)
    yield widget
    widget._workflow_active = False
    widget._busy = False
    widget._close_ready = True
    widget.close()
    widget.deleteLater()
    QCoreApplication.sendPostedEvents(None, QEvent.Type.DeferredDelete)
    app.processEvents()


@pytest.mark.parametrize("days", [
    {},
    {"mon": {"structural_types": ["echo_buoy", "hetero_branches"]}},
    {"mon": {"jobs": [{"route_id": "ct.tie_an.shoggolith_city.bounty", "run_count": 3}]},
     "sun": {"structural_types": ["birch_buoy"]}},
])
def test_workflow_captures_whole_week_without_demanding_todays_jobs(window, monkeypatch, days):
    snapshot = normalize_battle_editor_inputs({"weekly_plan": days})
    monkeypatch.setattr(window.battle_page, "collect_inputs", lambda: deepcopy(snapshot))
    for key, check in window.workflow_page._task_checks.items():
        check.setChecked(key == "battle")
    started = []
    monkeypatch.setattr(window, "_begin_workflow", lambda pending, *args: started.append(deepcopy(pending)))
    window._start_workflow()
    assert len(started) == 1
    assert started[0] == [{"step": "battle", "inputs": snapshot,
                           "label": "自动战斗", "dispatch": "battle"}]
    assert window._settings.load_battle_inputs() == snapshot


def test_workflow_summary_is_updated_from_editor_plan_signal(window):
    inputs = normalize_battle_editor_inputs({"structural_types": ["echo_buoy", "disordered_roots"]})
    window.workflow_page._update_battle_plan_summary(inputs)
    text = window.workflow_page.battle_plan_summary.text()
    assert "普通关卡 0 次" in text
    assert "混响浮标 → 乱序根须" in text
    assert "切换编辑星期不会改变执行日" in window.workflow_page.battle_plan_summary.toolTip()
