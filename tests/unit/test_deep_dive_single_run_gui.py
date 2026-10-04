"""The new board-only test must stay separate from the existing entry button."""

import os
import time

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from PySide6.QtCore import QCoreApplication, QEvent, QSettings
from PySide6.QtWidgets import QApplication

from packages.resonance_gui.config_repository import ResonanceConfigRepository
from packages.resonance_gui.logic import (
    PC_CONSCIOUSNESS_DEEP_DIVE_SINGLE_RUN_TASK_REF,
    PC_CONSCIOUSNESS_DEEP_DIVE_TASK_REF,
)
from packages.resonance_gui.main_window import ResonanceMainWindow


def test_single_run_button_dispatches_board_only_task(tmp_path):
    app = QApplication.instance() or QApplication(["deep-dive-single-run-test"])
    settings = ResonanceConfigRepository(
        QSettings(str(tmp_path / "settings.ini"), QSettings.Format.IniFormat)
    )
    window = ResonanceMainWindow(settings=settings, initialize_on_startup=False, update_checker=lambda: "")
    try:
        window.requestRunPcTask.disconnect()
        dispatched = []
        window.requestRunPcTask.connect(lambda *args: dispatched.append(args))
        panel = window.small_tasks_page.consciousness_deep_dive_panel
        panel.round_budget_spin.setValue(7)
        panel.test_button.click()
        assert len(dispatched) == 1
        assert dispatched[0][0] == PC_CONSCIOUSNESS_DEEP_DIVE_SINGLE_RUN_TASK_REF
        assert dispatched[0][1] == {"round_budget": 7}
        assert dispatched[0][0] != PC_CONSCIOUSNESS_DEEP_DIVE_TASK_REF
        panel.set_runner_busy(True)
        assert panel.cancel_button.isEnabled()
        window.small_tasks_page.apply_consciousness_deep_dive_single_run_result(
            {"status": "blocked", "reason": "unsupported_event", "last_frame": "logs/test.png"}
        )
        assert "unsupported_event" in panel.status_label.text()
    finally:
        window.close()
        # closeEvent starts an asynchronous bridge shutdown. One event pump
        # can leave its QThread alive until interpreter teardown, where Qt
        # aborts the process even though pytest reported all tests passed.
        deadline = time.monotonic() + 3.
        while (not window._close_ready or window._bridge_thread.isRunning()) and time.monotonic() < deadline:
            app.processEvents()
            time.sleep(.01)
        assert window._bridge_closed and window._close_ready, "GUI bridge did not finish normal shutdown"
        assert not window._bridge_thread.isRunning(), "GUI bridge thread is still running"
        window.deleteLater()
        QCoreApplication.sendPostedEvents(None, QEvent.Type.DeferredDelete)
