"""Eternal Scuffle GUI persistence, progress isolation and result contracts."""

import os

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

import pytest
from PySide6.QtCore import QSettings, Qt
from PySide6.QtWidgets import QApplication, QLabel, QListWidget

from packages.resonance_gui.bridge import RunnerBridge
from packages.resonance_gui.config_repository import ResonanceConfigRepository
from packages.resonance_gui.logic import (
    ETERNAL_SCUFFLE_PROGRESS_EVENT, ETERNAL_SCUFFLE_PROGRESS_SCHEMA,
    PC_ETERNAL_SCUFFLE_TASK_REF, TRADE_PROGRESS_EVENT, TRADE_PROGRESS_SCHEMA,
)
from packages.resonance_gui.main_window import ResonanceMainWindow
from packages.resonance_gui.widgets.small_tasks_page import SmallTasksPage
from packages.resonance_gui.widgets.eternal_scuffle_panel import EternalScufflePanel


@pytest.fixture
def repository(tmp_path):
    app = QApplication.instance() or QApplication(["scuffle-gui-test"])
    yield ResonanceConfigRepository(QSettings(str(tmp_path / "settings.ini"), QSettings.Format.IniFormat))
    app.processEvents()


def test_configuration_and_activity_entry(repository):
    page = SmallTasksPage(repository)
    category = page.category_list.findItems("活动玩法", Qt.MatchFlag.MatchExactly)[0]
    page.category_list.setCurrentItem(category)
    assert [page.task_list.item(i).text() for i in range(page.task_list.count())] == ["识海深潜", "无垠乱斗"]
    panel = page.eternal_scuffle_panel
    panel.coins_spin.setValue(5)
    panel.run_count_spin.setValue(9999)
    assert repository.load_eternal_scuffle_inputs() == {"coins_per_run": 5, "run_count": 9999}
    restored = EternalScufflePanel(repository)
    assert restored.collect_inputs() == panel.collect_inputs()
    for inputs in ({"coins_per_run": 0}, {"coins_per_run": True}, {"run_count": 10000}):
        with pytest.raises(ValueError):
            repository.save_eternal_scuffle_inputs(inputs)


def test_busy_and_failure_preserve_progress(repository):
    panel = EternalScufflePanel(repository)
    panel.set_runner_busy(True)
    assert not panel.run_button.isEnabled()
    assert not panel.coins_spin.isEnabled()
    assert not panel.cancel_button.isEnabled()
    panel.begin_run({"run_count": 2})
    assert panel.cancel_button.isEnabled()
    panel.apply_progress({"payload": {"stage": "battle", "run_index": 2, "completed_runs": 1,
        "round_result": {"run_index": 1, "outcome": "abandoned", "elapsed_ms": 1234}, "log_dir": "logs/example"}})
    panel.show_error("用户取消", cancelled=True)
    panel.set_runner_busy(False)
    assert "已取消" in panel.status_label.text()
    assert "等待战斗" in panel.status_label.text()
    assert "已完成 1 局" in panel.summary_label.text()
    assert not panel.findChildren(QListWidget)
    assert not hasattr(panel, "_rounds")
    assert not hasattr(panel, "diagnostic_label")
    assert "log_dir" not in panel._state
    assert all("运行记录" not in label.text() and "logs/example" not in label.text()
               for label in panel.findChildren(QLabel))
    assert panel.run_button.isEnabled()
    assert not panel.cancel_button.isEnabled()


def test_missing_completion_cannot_show_all_complete(repository):
    panel = EternalScufflePanel(repository)
    panel.begin_run({"run_count": 2})
    panel.apply_progress({"stage": "settlement"})
    panel.apply_result({"success": True, "status": "completed", "completed_runs": 1})
    assert "全部完成" not in panel.status_label.text()
    assert "领取奖励" in panel.status_label.text()


def test_legacy_rounds_and_log_path_are_ignored_in_completed_result(repository):
    panel = EternalScufflePanel(repository)
    panel.begin_run({"run_count": 1})
    panel.apply_result({"success": True, "status": "completed", "run_count": 1,
        "completed_runs": 1, "rounds": [{"run_index": 1, "outcome": "cleared", "elapsed_ms": 2500}],
        "round_result": {"run_index": 1, "outcome": "cleared"}, "log_dir": "legacy/scuffle/logs"})
    assert "全部完成" in panel.status_label.text()
    assert "已完成 1 局" in panel.summary_label.text()
    assert not panel.findChildren(QListWidget)
    assert not hasattr(panel, "_rounds")
    assert not {"rounds", "round_result", "log_dir"}.intersection(panel._state)
    assert all("运行记录" not in label.text() and "legacy/scuffle/logs" not in label.text()
               for label in panel.findChildren(QLabel))


def test_bridge_rejects_stale_wrong_schema_duplicate_and_non_integer_sequence(repository):
    bridge = RunnerBridge(runner_factory=lambda: None)
    bridge._current_cid = "active"
    bridge._current_item = {"task_ref": PC_ETERNAL_SCUFFLE_TASK_REF}
    received, trade = [], []
    bridge.eternalScuffleProgress.connect(received.append)
    bridge.tradeProgress.connect(trade.append)
    def event(**overrides):
        payload = {"schema": ETERNAL_SCUFFLE_PROGRESS_SCHEMA, "cid": "active", "sequence": 1}
        payload.update(overrides)
        return {"name": ETERNAL_SCUFFLE_PROGRESS_EVENT, "payload": payload}
    bridge._consume_events([event(cid="old"), event(schema="v0"), event(sequence=True), event(), event(), event(sequence=0), event(sequence=2)])
    assert [row["payload"]["sequence"] for row in received] == [1, 2]
    bridge._consume_events([{"name": TRADE_PROGRESS_EVENT, "payload": {"schema": TRADE_PROGRESS_SCHEMA, "cid": "active"}}])
    assert len(trade) == 1
    bridge._reset_current()
    bridge._consume_events([event(sequence=3)])
    assert len(received) == 2


def test_main_window_dispatch_and_cancelled_result(repository):
    window = ResonanceMainWindow(settings=repository, initialize_on_startup=False, update_checker=lambda: "")
    try:
        window.requestRunPcTask.disconnect()
        dispatched = []
        window.requestRunPcTask.connect(lambda *args: dispatched.append(args))
        window.timeout_spin.setValue(123)
        window._run_small_task_eternal_scuffle({"coins_per_run": 3, "run_count": 2})
        assert dispatched == [(PC_ETERNAL_SCUFFLE_TASK_REF, {"coins_per_run": 3, "run_count": 2}, "无垠乱斗", 123.0)]
        panel = window.small_tasks_page.eternal_scuffle_panel
        panel.apply_progress({"stage": "battle", "completed_runs": 1, "run_index": 2})
        window._active_game_name = "resonance_pc"
        window._on_task_finished({"status": "cancelled", "gui_item": {"kind": "workflow_task", "task_ref": PC_ETERNAL_SCUFFLE_TASK_REF},
                                  "final_result": {"user_data": {}}})
        assert "已取消" in panel.status_label.text()
        assert "已完成 1 局" in panel.summary_label.text()
        assert window._small_task_active_ref == ""
    finally:
        window.close()
